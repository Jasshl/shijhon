// Run the shipped browser controller with a bounded fake DOM and durable API ledger.
// Synthetic File objects deliberately have no audio-reading method.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
class Element {
  constructor() { this.listeners={}; this.children=[]; this.value=''; this.hidden=true; this.textContent=''; }
  addEventListener(name, callback) { this.listeners[name]=callback; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children=items; }
  scrollIntoView() {}
}
async function main() {
  const elements=new Map(), rows=[], calls=[], uploads=[], progressSamples=[];
  const insecure = process.argv[3] === 'insecure';
  let randomRequests = 0, eventLoopYields = 0;
  const browserCrypto = {getRandomValues(values) { randomRequests++; return crypto.webcrypto.getRandomValues(values); }};
  if (!insecure) browserCrypto.subtle = crypto.webcrypto.subtle;
  let state='declaring', concurrency=0, peak=0;
  const element=name => { if(!elements.has(name)) elements.set(name,new Element()); return elements.get(name); };
  Object.defineProperty(element('import-progress'),'value',{
    get(){return this._value||0;},
    set(value){
      this._value=value;
      if(state==='uploading') progressSamples.push({value,max:this.max});
    }
  });
  const root=new Element();
  root.dataset={api:'/shijhon/collection/import/api',page:'/shijhon/collection/import',csrf:'synthetic',job:'',capabilities:JSON.stringify({ready:true,formats:['mp3','flac'],manifest_page_size:500,limits:{max_file_bytes:1000,max_job_bytes:1000000,max_job_files:100000,max_uploads:2}})};
  root.querySelector=selector=>element(selector.match(/data-([^\]]+)/)[1]);
  element('file-filter').value='all';
  const status=(offset=0, problemsOnly=false, overlapOnly=false)=>{ const filtered=problemsOnly?rows.filter(row=>row.state==='rejected'||row.warnings.length):overlapOnly?rows.filter(row=>row.outcome):rows; return ({id:'job',state,revision:3,total_files:rows.length,total_bytes:rows.length*9,received_files:rows.filter(row=>row.state==='received').length,received_bytes:rows.filter(row=>row.state==='received').length*9,eligible_files:0,eligible_bytes:0,complete_files:0,rejected_files:0,excluded_files:0,skipped_files:0,linked_files:rows.filter(row=>row.state==='matched').length,duplicate_files:0,held_files:0,copy_files:0,error:'',items:filtered.slice(offset,offset+100),filtered_total:filtered.length,next_offset:offset+100<filtered.length?offset+100:null}); };
  const context={document:{querySelector:()=>root,createElement:()=>new Element(),createDocumentFragment:()=>new Element(),createTextNode:text=>({textContent:text})},crypto:browserCrypto,TextEncoder,URL,location:{origin:'http://fixture'},history:{replaceState(){}},sessionStorage:{getItem(){return null;},setItem(){},removeItem(){}},setTimeout(callback,delay){if(delay===0){eventLoopYields++;setImmediate(callback);}return 1;},clearTimeout(){},console};
  context.fetch=async (url,options)=>{
    const body=options.body?JSON.parse(options.body):null;
    const parsed=new URL(url,'http://fixture'), path=parsed.pathname;
    calls.push({path,body});
    if(options.method==='POST') assert.equal(options.headers['X-CSRF-Token'],'synthetic');
    if(path.endsWith('/api')) {
      assert.match(body.request_key,/^[0-9a-f]{32}$/);
      return {ok:true,json:async()=>({id:'job'})};
    }
    if(path.endsWith('/manifest')) {
      assert(body.entries.length<=500);
      for(const entry of body.entries) {
        assert.equal(entry.client_key,crypto.createHash('sha256').update(JSON.stringify([entry.path,entry.size,100])).digest('hex'));
      }
      for(const entry of body.entries) rows.push({...entry,id:String(rows.length),state:'declared',attempt:0,warnings:[],error:''});
    } else if(path.endsWith('/seal')) state='uploading';
    else if(path.endsWith('/review')) state='checking';
    else if(path.endsWith('/distinct')) { assert.deepEqual(body,{item:'5',distinct:true}); state='checking'; }
    else assert(!path.endsWith('/approve'),'Upload must never approve the review.');
    return {ok:true,json:async()=>status(Number(parsed.searchParams.get('offset')||0),parsed.searchParams.get('problems_only')==='1',parsed.searchParams.get('overlap_only')==='1')};
  };
  context.XMLHttpRequest=class {
    constructor(){this.upload={};}
    open(method,url){assert.equal(method,'POST');this.url=url;}
    setRequestHeader(){}
    send(file){
      assert.equal(file.size,9);
      const url=new URL(this.url,'http://fixture'),id=url.pathname.split('/').pop(),row=rows.find(item=>item.id===id);
      const attempt=Number(url.searchParams.get('attempt'));
      assert.equal(attempt,row.attempt);
      uploads.push({id,attempt,progress:element('import-progress').value,summary:element('import-summary').textContent,range:element('file-range').textContent}); concurrency++; peak=Math.max(peak,concurrency);
      setImmediate(()=>{
        concurrency--;
        this.upload.onprogress({loaded:4});
        if(id==='0' && attempt===0) { row.attempt++; this.onerror(); return; }
        row.state='received';
        // Lost success response: the receipt is durable, so this file must not replay.
        if(id==='1'){this.onerror();return;}
        // A successful response also completes progress when no final event arrives.
        if(id!=='2') this.upload.onprogress({loaded:9});
        this.status=200;this.onload();
      });
    }
    abort(){this.onabort();}
  };
  vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),context);
  const files=Array.from({length:1001},(_,i)=>({name:i<130?`${'x'.repeat(i)}song-${i}.flac`:`song-${i}.flac`,size:9,lastModified:100}));
  for(let i=130;i<140;i++) files[i].webkitRelativePath = Array.from({length:9},()=> '音'.repeat(50)).join('/') + '/' + files[i].name;
  files.push({name:'cover.jpg',size:9,lastModified:100});
  await element('file-picker').listeners.change({target:{files}});
  assert(element('selection-summary').textContent.includes('1,001 audio files'));
  assert(element('selection-summary').textContent.includes('1 unsupported file skipped'));
  assert.equal(element('upload').disabled,false);
  assert(eventLoopYields>=7,'Large selections must yield actual event-loop turns.');
  await element('upload').listeners.click();
  assert.equal(element('import-error').textContent,'');
  assert.equal(rows.length,1001);
  assert.equal(new Set(rows.map(row=>row.client_key)).size,1001);
  assert.equal(randomRequests,1);
  assert.equal(rows.filter(row=>row.state==='received').length,1001);
  assert.equal(peak,2);
  assert.equal(uploads.filter(item=>item.id==='0').length,2);
  assert.deepEqual(uploads.filter(item=>item.id==='0').map(item=>item.attempt),[0,1]);
  assert.equal(uploads.filter(item=>item.id==='1').length,1);
  assert(progressSamples.every(sample=>sample.max===1001*9),'Upload progress must use the whole batch, not the current 100 files.');
  assert(progressSamples.some(sample=>sample.value===4),'Bytes in an unfinished transfer must be visible.');
  assert.equal(uploads.find(item=>item.id==='0'&&item.attempt===1).progress,0,'A retry must discard the failed attempt\'s progress.');
  assert.equal(uploads.find(item=>item.id==='100').progress,100*9,'Crossing a page must keep completed bytes.');
  assert.equal(uploads.find(item=>item.id==='100').summary,'100 of 1,001 files received');
  assert.equal(uploads.find(item=>item.id==='100').range,'Files 101–200 of 1,001');
  assert.equal(uploads.find(item=>item.id==='200').progress,200*9);
  assert.equal(progressSamples.at(-1).value,1001*9);
  assert(progressSamples.every(sample=>sample.value<=sample.max),'Lost responses and retries must not double-count bytes.');
  assert.deepEqual(calls.filter(item=>item.path.endsWith('/manifest')).map(item=>item.body.entries.length),[500,500,1]);
  assert.equal(state,'checking');
  assert.equal(element('stage').textContent,'Checking files…');
  assert.equal(element('approve').hidden,true);
  assert(element('file-rows').children[0].children.length<=100);
  // Reload has no File references and cannot start another transfer automatically.
  const before=uploads.length;root.dataset.job='job';
  vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(uploads.length,before);
  // A connection lost before the first manifest still offers resume, not a review.
  const savedRows=rows.splice(0); state='declaring';
  vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(element('reselect').hidden,false);
  assert.equal(element('review').hidden,true);
  rows.push(...savedRows);
  // An interrupted upload exposes a resume action, then reselects sources and skips receipts.
  state='uploading'; rows[0].state='declared'; rows[0].attempt++;
  vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(element('reselect').hidden,false);
  assert.equal(element('import-progress').hidden,false);
  assert.equal(element('import-progress').max,1001*9);
  assert.equal(element('import-progress').value,1000*9,'Reload must restore whole-batch received bytes, including later pages.');
  assert(element('import-helper').textContent.includes('99.9%'));
  await element('reselect').listeners.click();
  assert.equal(element('picker').hidden,false);
  assert.equal(element('picker-heading').textContent,'Resume your upload');
  assert(element('selection-summary').textContent.includes('Completed files will not upload again'));
  assert.equal(element('upload').textContent,'Continue upload');
  await element('folder-picker').listeners.change({target:{files}});
  await element('upload').listeners.click();
  assert.equal(uploads.length,before+1);
  assert.equal(uploads.at(-1).id,'0');
  assert.equal(uploads.at(-1).progress,1000*9,'Reselection must retain the saved baseline.');
  assert.equal(rows.filter(row=>row.state==='received').length,1001);
  assert.equal(element('reselect').hidden,true);
  // A discarded batch has no resume action.
  state='cancelled';
  vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(element('reselect').hidden,true);
  // Problems on the final manifest page are found by one bounded filtered request.
  state='review'; rows[1000].state='rejected'; rows[1000].error='invalid_audio';
  const beforeRequests=calls.length;
  vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(element('file-filter').value,'problems');
  assert.equal(element('file-rows').children[0].children.length,1);
  assert(element('file-range').textContent.endsWith('1–1 of 1'));
  assert.equal(calls.length-beforeRequests,2);
  // A song the library already has: shown with both qualities and "Not the same song".
  Object.assign(rows[5],{state:'matched',outcome:'linked',match:{reason:'Already in the library.',title:'Lantern 1',album:'Night Letters',library_quality:'FLAC 16-bit 44.1 kHz',quality:'MP3 128 kbps'},distinct:false});
  element('file-filter').value='overlap';
  await element('file-filter').listeners.change();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(element('file-rows').children[0].children.length,1);
  const linkedRow=element('file-rows').children[0].children[0].children[1];
  const texts=linkedRow.children.map(child=>child.textContent);
  assert(texts.some(text=>text.startsWith('Already in the library. Not added again')));
  assert(texts.includes('Library song: Lantern 1 · Night Letters'));
  assert(texts.includes('Library copy: FLAC 16-bit 44.1 kHz · Your file: MP3 128 kbps'));
  const sameSong=linkedRow.children.find(child=>child.textContent==='Not the same song');
  assert(sameSong);
  await sameSong.listeners.click();
  await new Promise(resolve=>setImmediate(resolve));
  assert(calls.some(call=>call.path.endsWith('/distinct')));
  assert.equal(element('approve').hidden,true); // checking again after the change
  delete rows[5].outcome; rows[5].state='received'; state='review';
  // Reselection recomputes exactly the same keys on either browser crypto surface.
  root.dataset.job=''; state='declaring';
  vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),context);
  await element('folder-picker').listeners.change({target:{files}});
  assert(element('selection-summary').textContent.includes('1,001 audio files'));
  assert.equal(element('upload').disabled,false);
  // A newer selection must interrupt a large synchronous-fallback selection.
  const earlier = element('file-picker').listeners.change({target:{files}});
  await new Promise(resolve=>setImmediate(resolve));
  await element('file-picker').listeners.change({target:{files:[{name:'replacement.flac',size:9,lastModified:100}]}});
  await earlier;
  assert(element('selection-summary').textContent.startsWith('1 audio file ·'));
  if(insecure) {
    const previousYields=eventLoopYields, previousUploads=uploads.length;
    const largeFolder=Array.from({length:100000},(_,i)=>({name:`scale-${i}.flac`,size:9,lastModified:100}));
    await element('folder-picker').listeners.change({target:{files:largeFolder}});
    assert(element('selection-summary').textContent.startsWith('100,000 audio files ·'));
    assert(eventLoopYields-previousYields>=781);
    assert.equal(uploads.length,previousUploads);
  }
  // Reject overlong source paths before metadata allocation and disable stale choices.
  await element('file-picker').listeners.change({target:{files:[{name:'x'.repeat(1025)+'.flac',size:9,lastModified:100}]}});
  assert.equal(element('upload').disabled,true);
  assert(element('import-error').textContent.includes('path is too long'));
  console.log(`${insecure ? 'HTTP-compatible crypto' : 'SubtleCrypto'}: SHA-256 metadata keys match Node, bounded paths and random request IDs.`);
  console.log('1001 files: manifests 500/500/1; <=2 uploads; lost-response receipt checked; unfinished retry uses current attempt; checking awaits approval; reload uploads nothing.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
