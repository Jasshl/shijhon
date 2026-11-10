const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const flush = () => new Promise(resolve => setImmediate(resolve));
class Element {
  constructor() {
    this.listeners = {}; this.children = []; this.dataset = {}; this.style = {};
    this.hidden = false; this.disabled = false; this.checked = false;
    this.classList = {add() {}, remove() {}, contains() {return false;}};
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute() {}
  removeAttribute() {}
  toggleAttribute() {}
  focus() {}
}
function run(name, context) {
  vm.runInNewContext(fs.readFileSync(path.join(process.argv[2], name), 'utf8'), context);
}
async function removals() {
  const root = new Element(), panel = new Element(), elements = new Map(), calls = [];
  const el = name => {if (!elements.has(name)) elements.set(name, new Element()); return elements.get(name);};
  root.dataset = {api:'/imports',job:'job',csrf:'synthetic'};
  panel.querySelector = selector => el(selector.match(/data-removal-([^\]]+)/)[1]);
  panel.querySelectorAll = () => [...elements.values()];
  let state = {state:'none',import_state:'review',include_used:false,items:[],next_offset:null};
  let refusal = null;
  const context = {
    document:{querySelector:selector => selector === '[data-music-imports]' ? root : panel,
      createElement:() => new Element(),createDocumentFragment:() => new Element()},
    MutationObserver:class {observe() {}}, setTimeout() {},clearTimeout() {},
    fetch:async(url, options) => {
      calls.push({url,options});
      if (refusal) return {ok:false,json:async() => refusal};
      if (url.endsWith('/review')) state = {...state,state:'review',include_used:JSON.parse(options.body).include_used};
      return {ok:true,json:async() => ({...state})};
    }
  };
  run('music_import_removals.js', context); await flush();
  assert.equal(panel.hidden,true,'Unpublished imports have no removal panel.');
  for (const import_state of ['declaring','uploading','checking','review','paused']) {
    state.import_state = import_state; el('next').listeners.click(); await flush();
    assert.equal(panel.hidden,true);
  }
  for (const import_state of ['approved','publishing','done','cancelled']) {
    state.import_state = import_state; el('prev').listeners.click(); await flush();
    assert.equal(panel.hidden,false);
  }
  state = {...state,state:'review',include_used:true,total:101,remove_files:101,next_offset:100,fingerprint:'review-with-used'};
  run('music_import_removals.js', context); await flush();
  assert.equal(el('used').checked,true); assert.equal(el('approve').hidden,false);
  el('used').checked = false; el('used').listeners.change();
  el('next').listeners.click(); await flush();
  assert.equal(el('used').checked,false); assert.equal(el('approve').hidden,true);
  el('prev').listeners.click(); await flush();
  assert.equal(el('used').checked,false); assert.equal(el('approve').hidden,true);
  const before = calls.length; el('approve').listeners.click(); await flush();
  assert.equal(calls.length,before,'A stale review cannot be approved even by a synthetic click.');
  el('review').listeners.click(); await flush();
  assert.equal(state.include_used,false); assert.equal(el('approve').hidden,false);
  el('used').checked = true; el('used').listeners.change();
  el('used').checked = false; el('used').listeners.change();
  el('next').listeners.click(); await flush();
  assert.equal(el('approve').hidden,true,'Changing back still needs a new review.');
  refusal = {error:'sign_in_required',message:'Sign in again to continue.'};
  el('prev').listeners.click(); await flush();
  assert.equal(el('error').textContent,refusal.message);
  state = {...state,import_state:'paused'}; refusal = null;
  el('prev').listeners.click(); await flush();
  assert.equal(panel.hidden,false,'Existing removals stay available even for a paused import.');
}
async function collection(dirty) {
  const root = new Element(), source = new Element(), text = new Element(), timers = [];
  let reloads = 0;
  root.dataset = {};
  root.querySelector = selector => selector === '[data-source-progress]' ? source : null;
  root.querySelectorAll = () => [];
  source.querySelector = () => text;
  const context = {
    document:{querySelector:() => root,createElement:() => new Element()},
    sessionStorage:{getItem() {},setItem() {}},
    setTimeout:callback => {timers.push(callback);return timers.length;}, clearTimeout() {},AbortController,
    location:{reload() {reloads++;}},
    fetch:async() => ({ok:true,json:async() => ({state:'ready',ready:true})})
  };
  run('collection.js',context);
  if (dirty) root.listeners.input({target:{closest:() => ({})}});
  await timers.shift()();
  assert.equal(reloads,dirty ? 0 : 1);
  if (dirty) {
    assert.equal(text.children[0].textContent,'Reload results');
    text.children[0].listeners.click(); assert.equal(reloads,1);
  }
}
(async() => {
  await removals(); await collection(false); await collection(true);
})().catch(error => {console.error(error);process.exitCode = 1;});
