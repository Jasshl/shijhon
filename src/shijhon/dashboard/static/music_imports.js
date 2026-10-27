(() => {
  'use strict';
  const root = document.querySelector('[data-music-imports]');
  if (!root) return;
  const el = name => root.querySelector(`[data-${name}]`);
  const caps = JSON.parse(root.dataset.capabilities);
  const limits = caps.limits || {};
  const base = root.dataset.api;
  const word = (value, singular) => Number(value) === 1 ? singular : singular + 's';
  const count = value => Number(value || 0).toLocaleString();
  const bytes = value => {
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let n = Number(value || 0), unit = 0;
    while (n >= 1024 && unit < units.length - 1) { n /= 1024; unit++; }
    return `${n.toLocaleString(undefined, {maximumFractionDigits: unit ? 1 : 0})} ${units[unit]}`;
  };
  let jobId = root.dataset.job, job = null, selected = [], sources = new Map();
  let offset = 0, busy = false, stopped = false, poll = null, selectionVersion = 0, fileFilterChosen = false;
  const activeUploads = new Set();
  const finished = new Set(['received', 'eligible', 'rejected', 'excluded', 'placing', 'placed', 'complete',
    'matched', 'linked', 'skipped', 'copying', 'joined', 'catalog']);
  const states = {
    declared: 'Waiting for upload', uploading: 'Uploading', received: 'Uploaded',
    eligible: 'Ready to import', rejected: 'Cannot import', excluded: 'Skipped',
    placing: 'Importing', placed: 'Making available', complete: 'Imported',
    matched: 'Already in the library', linked: 'Already in the library · linked',
    skipped: 'Not added', copying: 'Preparing', joined: 'Ready to import',
    catalog: 'Added from the catalog instead'
  };
  // What the review decided for a file the library already has (music_imports/overlap.py).
  const outcomes = {
    linked: 'Already in the library. Not added again; it counts as your import.',
    catalog_song: 'Already in the library as a song Shijhon added from the catalog. It counts as your import.',
    duplicate: 'Twice in this selection: the better copy is added.',
    held: 'Not added: it would show as a second album of the same name.',
    unsure: 'Added. It may already be in the library.',
    copy: 'Added to the library\'s album as a copy with that album\'s details. Your file stays unchanged.',
    low_quality: 'Below your minimum quality: the catalog\'s version is added instead and plays from the add-ons.'
  };
  // Where the file's audio is for the catalog song it matched (music_imports/catalog_audio.py).
  const catalogAudio = {
    pending: 'Your file will play for this catalog song shortly.',
    placed: 'Your file now plays for this catalog song.',
    kept: 'The catalog song already has audio of its own: your file was not used for it.',
    untaggable: 'Your file\'s format cannot carry this catalog song: it still plays from the add-ons.',
    low_quality: 'Your file is below your lowest quality: the catalog song still plays from the add-ons.',
    failed: 'Your file could not be put in place: the catalog song still plays from the add-ons.'
  };
  const errors = {
    unsupported: 'Unsupported format', invalid_audio: 'File could not be read',
    damaged: 'File is damaged', size: 'File size does not match',
    too_large: 'File exceeds the upload limit', expired: 'Upload expired; choose this file again',
    changed: 'The stored file changed. Stop and ask your administrator to check it.',
    interrupted: 'Work was interrupted. Completed files are kept; resume to continue.',
    access: 'Current account access could not be verified. Sign in again, then resume.',
    indexing: 'The imported file is waiting for its library record. Resume to check again.',
    identity: 'A library identity needs checking. Ask your administrator before resuming.',
    storage: 'Import storage is unavailable. Ask your administrator to check it.',
    catalog: 'The catalog\'s version of an album could not be added yet. Resume to retry.',
    space: 'There is not enough free space. Ask your administrator, then resume.',
    collision: 'An existing file blocks this import. Ask your administrator to check it.',
    path_collision: 'Selected names or folders conflict. Use separate imports or choose distinct paths.',
    native_ignored: 'Navidrome ignores this filename or folder. Choose a supported source path.',
    native_unsupported: 'The stock Navidrome scanner cannot import this format.'
  };
  class ApiError extends Error {
    constructor(message, status = 0) { super(message); this.status = status; }
  }
  const showError = message => {
    el('import-error').textContent = message;
    el('import-error').hidden = !message;
  };
  function explain(error) {
    if (error.status === 401) {
      const target = new URL(`${root.dataset.page}/${encodeURIComponent(jobId)}`, location.origin);
      el('import-error').replaceChildren(document.createTextNode('Sign in to continue this import. '));
      const link = document.createElement('a');
      link.href = `${root.dataset.page.split('/collection/')[0]}/sign-in?next=${encodeURIComponent(target.pathname)}`;
      link.textContent = 'Sign in'; el('import-error').append(link); el('import-error').hidden = false;
    } else showError(error.message || 'Import paused. Check its status before trying again.');
  }
  async function request(path, data) {
    let response;
    try {
      response = await fetch(base + path, {
        method: data === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: data === undefined ? {} : {'Content-Type': 'application/json', 'X-CSRF-Token': root.dataset.csrf},
        body: data === undefined ? undefined : JSON.stringify(data)
      });
    } catch (_) { throw new ApiError('Connection interrupted. Check the saved import status, then resume.'); }
    let result;
    try { result = await response.json(); }
    catch (_) { throw new ApiError('Could not read the import response. Reload this page to check progress.', response.status); }
    if (!response.ok) throw new ApiError(result.error || 'Import could not continue.', response.status);
    return result;
  }
  const action = (name, data = {}) => request(`/${encodeURIComponent(jobId)}/${name}`, data);
  const page = (start, filter = 'all') => request(`/${encodeURIComponent(jobId)}?offset=${start}&limit=100&problems_only=${filter === 'problems' ? 1 : 0}&overlap_only=${filter === 'overlap' ? 1 : 0}`);
  function setBusy(value) {
    busy = value;
    for (const name of ['upload', 'approve', 'resume', 'review', 'reselect']) el(name).disabled = value;
    el('upload').disabled = value || !selected.length;
    el('file-filter').disabled = value;
  }
  function describeMatch(item) {
    const match = item.match || {};
    const lines = [];
    // A song you said is not the same is still added into the album of its name.
    if (item.distinct) lines.push(match.reason && match.reason !== 'You said this is not the same song.' ? match.reason : 'You said this is not the same song: it is added.');
    else if (outcomes[item.outcome]) lines.push(match.reason && item.outcome !== 'linked' ? match.reason : outcomes[item.outcome]);
    if (match.title && ['linked', 'catalog_song'].includes(item.outcome) && !item.distinct) {
      lines.push(`Library song: ${match.title}${match.album ? ` · ${match.album}` : ''}`);
    }
    if (match.library_quality && match.quality && !item.distinct) {
      lines.push(`Library copy: ${match.library_quality} · Your file: ${match.quality}`);
    }
    if (match.below_minimum) {
      if (match.reason && !lines.includes(match.reason)) lines.push(match.reason);
      if (match.quality) lines.push(`Your file: ${match.quality} · Your minimum: ${match.below_minimum}`);
    }
    if (item.outcome === 'catalog_song' && catalogAudio[item.audio]) lines.push(catalogAudio[item.audio]);
    if (item.upgraded) {
      lines.push(`Upgraded from the add-ons after you played it: ${item.upgraded.to}${item.upgraded.from ? ` (your file: ${item.upgraded.from}, kept privately)` : ''}.`);
    }
    return lines;
  }
  async function keepFiles(item, value) {
    if (busy) return;
    setBusy(true); showError(''); clearTimeout(poll);
    try { await action('keep', {item:item.id, keep:value}); }
    catch (error) { explain(error); }
    finally { setBusy(false); await refresh().catch(explain); }
  }
  async function checkAgain() {
    if (busy) return;
    setBusy(true); showError(''); clearTimeout(poll);
    try { await action('check_again'); }
    catch (error) { explain(error); }
    finally { setBusy(false); await refresh().catch(explain); }
  }
  async function markDistinct(item, value) {
    if (busy) return;
    setBusy(true); showError(''); clearTimeout(poll);
    try { await action('distinct', {item:item.id, distinct:value}); }
    catch (error) { explain(error); }
    finally { setBusy(false); await refresh().catch(explain); }
  }
  function renderFiles(status) {
    const filter = el('file-filter').value;
    const problemsOnly = filter === 'problems';
    const rows = status.items || [];
    const shown = rows;
    const fragment = document.createDocumentFragment();
    for (const item of shown) {
      const li = document.createElement('li'), info = document.createElement('div');
      const title = document.createElement('strong'); title.textContent = item.path;
      const detail = document.createElement('small');
      detail.textContent = `${bytes(item.size)} · ${(item.path.split('.').pop() || '').toUpperCase()}`;
      info.append(title, detail);
      const state = document.createElement('div'); state.className = 'music-import-file-state';
      const shownState = item.state === 'joined' ? (item.copy_state || 'eligible') : item.state;
      state.textContent = status.state === 'cancelled' && ['placing', 'placed'].includes(shownState)
        ? 'Original retained' : states[shownState] || 'Waiting';
      for (const line of describeMatch(item)) {
        const note = document.createElement('small'); note.textContent = line; state.append(note);
      }
      const reviewing = ['review', 'checking'].includes(status.state);
      // A held file has no choice: on its own it would be a second album of its name.
      if (reviewing && (item.distinct || ['linked', 'catalog_song', 'duplicate', 'unsure'].includes(item.outcome))) {
        const toggle = document.createElement('button');
        toggle.className = 'btn-small'; toggle.disabled = busy || status.state !== 'review';
        toggle.textContent = item.distinct ? 'Undo' : 'Not the same song';
        toggle.addEventListener('click', () => markDistinct(item, !item.distinct));
        state.append(toggle);
      }
      const match = item.match || {};
      if (reviewing && (item.keep_files || match.below_minimum)) {
        const keep = document.createElement('button');
        keep.className = 'btn-small'; keep.disabled = busy || status.state !== 'review';
        keep.textContent = item.keep_files ? 'Use the catalog\'s version' : 'Add my files anyway';
        keep.addEventListener('click', () => keepFiles(item, !item.keep_files));
        state.append(keep);
      }
      if (reviewing && item.outcome === 'low_quality' && match.route === 'unchecked') {
        const again = document.createElement('button');
        again.className = 'btn-small'; again.disabled = busy || status.state !== 'review';
        again.textContent = 'Check again';
        again.addEventListener('click', () => checkAgain());
        state.append(again);
      }
      if (item.error) {
        const reason = document.createElement('small');
        reason.textContent = errors[item.error] || `Could not continue (${item.error}).`;
        state.append(reason);
      }
      for (const warning of item.warnings || []) {
        const note = document.createElement('small'); note.textContent = warning; state.append(note);
      }
      li.append(info, state); fragment.append(li);
    }
    if (!shown.length) {
      const li = document.createElement('li');
      li.textContent = problemsOnly ? 'No files need attention.'
        : filter === 'overlap' ? 'Every file is added as it is.' : 'No files on this page.';
      fragment.append(li);
    }
    el('file-rows').replaceChildren(fragment);
    const filteredTotal = status.filtered_total ?? status.total_files;
    const end = Math.min(offset + rows.length, filteredTotal);
    const label = problemsOnly ? 'Files needing attention' : filter === 'overlap' ? 'Music the library already had' : 'Files';
    el('file-range').textContent = `${label} ${rows.length ? count(offset + 1) : '0'}–${count(end)} of ${count(filteredTotal)}`;
    el('file-page').textContent = `Page ${count(Math.floor(offset / 100) + 1)}`;
    el('files-previous').disabled = busy || offset === 0;
    el('files-next').disabled = busy || status.next_offset === null || status.next_offset === undefined;
  }
  function renderUploadProgress(status, extraBytes = 0, extraFiles = 0) {
    const total = status.total_bytes;
    const uploaded = Math.min(total, status.received_bytes + extraBytes);
    const percent = (total ? 100 * uploaded / total : 0).toLocaleString(undefined, {maximumFractionDigits:1});
    const progress = el('import-progress');
    progress.hidden = false; progress.max = total || 1; progress.value = uploaded;
    el('import-summary').textContent = `${count(status.received_files + extraFiles)} of ${count(status.total_files)} ${word(status.total_files, 'file')} received`;
    const hint = busy ? 'Keep this tab open.' : status.received_files < status.total_files
      ? 'Choose the same files or folder to continue. Completed files will not upload again.'
      : 'Choose Check uploaded files to continue.';
    el('import-helper').textContent = `${bytes(uploaded)} of ${bytes(total)} uploaded · ${percent}%. ${hint}`;
  }
  function render(status) {
    job = status;
    el('job-panel').hidden = false;
    const state = status.state;
    const importing = ['approved','publishing'].includes(state);
    const checking = state === 'checking';
    const checked = status.checked_files ?? (status.eligible_files + status.rejected_files + status.excluded_files);
    const complete = state === 'done', cancelled = state === 'cancelled';
    const uploadStage = ['declaring','uploading'].includes(state);
    el('stage').textContent = ({declaring:'Preparing upload', uploading:'Upload your music', checking:'Checking files…', review:'Review your import', approved:'Importing your music', publishing:'Making imported songs available…', paused:'Import paused', done:'Import complete', cancelled:'Import stopped'})[state] || 'Import progress';
    el('stage-count').textContent = `${count(status.total_files)} ${word(status.total_files, 'file')} · ${bytes(status.total_bytes)}`;
    el('import-summary').textContent = checking
      ? `${count(checked)} of ${count(status.total_files)} ${word(status.total_files, 'file')} checked`
      : state === 'review'
      ? `${count(status.eligible_files)} ${word(status.eligible_files, 'song')} to add · ${count(status.linked_files)} already in the library${status.low_quality_files ? ` · ${count(status.low_quality_files)} from the catalog instead (below your minimum quality)` : ''} · ${count(status.rejected_files)} cannot be imported · ${count(status.excluded_files + status.duplicate_files + status.held_files + (status.unchecked_files || 0))} skipped`
      : complete || cancelled
        ? `${count(status.complete_files)} ${word(status.complete_files, 'song')} imported${status.linked_files ? ` · ${count(status.linked_files)} already in the library` : ''}${status.low_quality_files ? ` · ${count(status.low_quality_files)} from the catalog instead` : ''}${status.skipped_files ? ` · ${count(status.skipped_files)} not added` : ''}`
        : importing ? `${count(status.complete_files)} ${word(status.complete_files, 'song')} imported of ${count(status.eligible_files)} approved`
          : `${count(status.received_files)} of ${count(status.total_files)} ${word(status.total_files, 'file')} received`;
    el('import-helper').textContent = cancelled
      ? 'Importing has stopped. Stop keeps files already published, including those awaiting indexing. Use Review removal below to change their availability.'
      : complete ? (status.linked_files || status.skipped_files
        ? 'Your imported music is in Original imported library and All, songs that were already in the library included. Show "Music the library already had" to see what was not added again, what joined the library\'s albums, and why.'
        : 'Your imported music is in Original imported library and All.')
        : checking ? 'You can leave this page while files are checked. Nothing is imported until you approve the review.'
        : importing ? 'You can leave this page. Import progress is saved. Songs appear after their library records are verified.'
          : state === 'review' && (status.low_quality_files || status.unchecked_files || status.below_minimum_files)
            ? 'Check the files below. Albums below your minimum quality are added from the catalog instead and play from the add-ons; choose Add my files anyway to add your own files. Songs already in the library are linked and count as your imports.'
          : state === 'review' ? (status.linked_files || status.duplicate_files || status.held_files || status.copy_files
            ? 'Check the files below. Songs already in the library are not added again: they are linked and count as your imports. Choose Not the same song if a match is wrong.'
            : 'Check the files below. Only the ready songs in this review will be imported.')
            : state === 'paused' ? (errors[status.error] || 'Import could not finish. Completed files and placed originals are kept. Resume to retry unfinished work.')
              : busy ? 'Keep this tab open while files upload.' : 'Choose the same files or folder to continue. Completed files will not upload again.';
    el('approve').hidden = state !== 'review' || !(status.eligible_files || status.linked_files || status.low_quality_files);
    el('approve').textContent = `Import ${count(status.eligible_files)} ${word(status.eligible_files, 'song')}${status.linked_files ? `; link ${count(status.linked_files)} already in the library` : ''}${status.low_quality_files ? `; add ${count(status.low_quality_files)} from the catalog` : ''}${status.skipped_files ? `; skip ${count(status.skipped_files)} ${word(status.skipped_files, 'file')}` : ''}`;
    el('review').hidden = state !== 'uploading' || status.received_files < status.total_files;
    el('resume').hidden = state !== 'paused';
    el('reselect').hidden = !uploadStage || busy || (state !== 'declaring' && status.received_files >= status.total_files);
    el('upload').textContent = jobId ? 'Continue upload' : 'Upload for review';
    el('stop').hidden = complete || cancelled;
    el('stop').textContent = importing || state === 'paused' || status.complete_files ? 'Stop import' : 'Discard upload';
    el('open-original').hidden = !(status.complete_files || status.linked_done_files);
    el('open-all').hidden = !(status.complete_files || status.linked_done_files);
    el('import-more').hidden = !complete && !cancelled;
    const progress = el('import-progress');
    progress.hidden = !importing && !checking;
    if (checking) { progress.max = status.total_files || 1; progress.value = checked; }
    if (importing) { progress.max = status.eligible_files || 1; progress.value = status.complete_files; }
    if (state === 'uploading') renderUploadProgress(status);
    renderFiles(status);
    clearTimeout(poll);
    if ((importing || checking) && !busy) poll = setTimeout(() => refresh().catch(explain), 3000);
  }
  async function refresh() {
    let result = await page(offset, el('file-filter').value);
    if (!fileFilterChosen && ['review', 'done'].includes(result.state)) {
      fileFilterChosen = true;
      for (const filter of result.state === 'review' ? ['problems', 'overlap'] : ['overlap']) {
        const first = await page(0, filter);
        if (first.filtered_total > 0) {
          offset = 0; el('file-filter').value = filter; result = first; break;
        }
      }
    }
    render(result); return result;
  }
  // SHA-256 round constants; this fallback hashes at most 8 KiB of metadata, never audio.
  // Keep the existing keys on HTTP origins where SubtleCrypto is unavailable.
  const sha256Rounds = new Uint32Array([
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
  ]);
  function metadataDigest(metadata) {
    if (metadata.length > 8192) throw new ApiError('The selected file path is too long.');
    const padded = new Uint8Array(Math.ceil((metadata.length + 9) / 64) * 64);
    padded.set(metadata); padded[metadata.length] = 0x80;
    const view = new DataView(padded.buffer);
    // The explicit 8 KiB bound keeps the high 32 bits of the bit length zero.
    view.setUint32(padded.length - 4, metadata.length * 8);
    const state = new Uint32Array([
      0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19
    ]);
    const words = new Uint32Array(64);
    const rotate = (value, bits) => (value >>> bits) | (value << (32 - bits));
    for (let block = 0; block < padded.length; block += 64) {
      for (let i = 0; i < 16; i++) words[i] = view.getUint32(block + i * 4);
      for (let i = 16; i < 64; i++) {
        const a = words[i - 15], b = words[i - 2];
        const s0 = rotate(a, 7) ^ rotate(a, 18) ^ (a >>> 3);
        const s1 = rotate(b, 17) ^ rotate(b, 19) ^ (b >>> 10);
        words[i] = words[i - 16] + s0 + words[i - 7] + s1;
      }
      let [a,b,c,d,e,f,g,h] = state;
      for (let i = 0; i < 64; i++) {
        const sum1 = rotate(e, 6) ^ rotate(e, 11) ^ rotate(e, 25);
        const choose = (e & f) ^ (~e & g);
        const t1 = h + sum1 + choose + sha256Rounds[i] + words[i];
        const sum0 = rotate(a, 2) ^ rotate(a, 13) ^ rotate(a, 22);
        const majority = (a & b) ^ (a & c) ^ (b & c);
        h=g; g=f; f=e; e=(d+t1)>>>0; d=c; c=b; b=a; a=(t1+sum0+majority)>>>0;
      }
      const result = [a,b,c,d,e,f,g,h];
      for (let i = 0; i < 8; i++) state[i] += result[i];
    }
    return Array.from(state, value => value.toString(16).padStart(8, '0')).join('');
  }
  function requestKey() {
    const random = globalThis.crypto;
    if (typeof random?.getRandomValues !== 'function') {
      throw new ApiError('This browser cannot create import requests. Try a current browser.');
    }
    return Array.from(random.getRandomValues(new Uint8Array(16)),
      value => value.toString(16).padStart(2, '0')).join('');
  }
  async function keyFor(file) {
    const path = file.webkitRelativePath || file.name;
    if (path.length > 1024) throw new ApiError('The selected file path is too long.');
    // This key is a reselection hint, never evidence of the audio content.
    const metadata = new TextEncoder().encode(JSON.stringify([path, file.size, file.lastModified]));
    const subtle = globalThis.crypto?.subtle;
    if (typeof subtle?.digest === 'function') {
      const digest = await subtle.digest('SHA-256', metadata);
      return Array.from(new Uint8Array(digest), n => n.toString(16).padStart(2, '0')).join('');
    }
    return metadataDigest(metadata);
  }
  async function choose(files) {
    if (busy) return;
    const version = ++selectionVersion;
    selected = []; sources = new Map();
    el('upload').disabled = true;
    let skipped = 0, repeated = 0, total = 0, inspected = 0;
    showError(''); el('selection-summary').textContent = 'Reading files…';
    for (const file of files) {
      // A resolved digest promise only yields microtasks. Let input/rendering run too.
      if (++inspected % 128 === 0) {
        el('selection-summary').textContent = `${count(inspected)} files checked…`;
        await new Promise(resolve => setTimeout(resolve, 0));
        if (version !== selectionVersion) return;
      }
      const format = (file.name.split('.').pop() || '').toLowerCase();
      if (!caps.formats.includes(format)) { skipped++; continue; }
      if (file.size > limits.max_file_bytes) {
        showError(`${file.name} exceeds the ${bytes(limits.max_file_bytes)} file limit. Choose a smaller selection.`);
        el('upload').disabled = true; return;
      }
      if (selected.length >= limits.max_job_files || total + file.size > limits.max_job_bytes) {
        showError(`Choose at most ${count(limits.max_job_files)} files and ${bytes(limits.max_job_bytes)} per import.`);
        el('upload').disabled = true; return;
      }
      const clientKey = await keyFor(file);
      if (version !== selectionVersion) return;
      if (sources.has(clientKey)) {
        if (sources.get(clientKey) === file) { repeated++; continue; }
        showError('Two distinct selected files have the same path and file details. Use separate imports; no file will be silently skipped.');
        el('upload').disabled = true; return;
      }
      sources.set(clientKey, file);
      selected.push({client_key:clientKey, path:file.webkitRelativePath || file.name, size:file.size});
      total += file.size;
    }
    selected.sort((a,b) => a.client_key < b.client_key ? -1 : a.client_key > b.client_key ? 1 : 0);
    el('selection-summary').textContent = `${count(selected.length)} audio ${word(selected.length, 'file')} · ${bytes(total)} · ${count(skipped)} unsupported ${word(skipped, 'file')} skipped${repeated ? ` · ${count(repeated)} repeated ${word(repeated, 'selection')} skipped` : ''}`;
    el('upload').disabled = !selected.length;
  }
  function sendFile(item, file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest(); activeUploads.add(xhr);
      xhr.open('POST', `${base}/${encodeURIComponent(jobId)}/files/${encodeURIComponent(item.id)}?attempt=${item.attempt}`);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.setRequestHeader('X-CSRF-Token', root.dataset.csrf);
      xhr.timeout = Math.max(1000, (limits.upload_seconds || 1800) * 1000);
      xhr.upload.onprogress = event => onProgress(event.loaded);
      const finish = error => { activeUploads.delete(xhr); error ? reject(error) : resolve(); };
      xhr.onerror = xhr.ontimeout = () => finish(new ApiError('Upload paused. Your completed files are kept.'));
      xhr.onabort = () => finish(new ApiError('Upload stopped. Completed files are kept.'));
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) { finish(); return; }
        let message = 'File upload could not continue.';
        try { message = JSON.parse(xhr.responseText).error || message; } catch (_) { /* status is checked next */ }
        finish(new ApiError(message, xhr.status));
      };
      xhr.send(file);
    });
  }
  async function uploadItem(initial, start, progress) {
    let item = initial;
    if (finished.has(item.state)) return;
    const file = sources.get(item.client_key);
    if (!file || file.size !== item.size) return;
    for (let retry = 0; retry < 3 && !stopped; retry++) {
      progress(0);
      try { await sendFile(item, file, progress); progress(item.size, true); return; }
      catch (error) {
        if (stopped || [401,403].includes(error.status)) throw error;
        // A missing response does not prove failure. Read the durable item before replay.
        const saved = await page(start);
        item = saved.items.find(row => row.id === initial.id);
        if (!item) throw new ApiError('The saved file could not be found. Reload this import.');
        if (finished.has(item.state)) { progress(item.size, true); return; }
        if (item.state === 'uploading' || retry === 2 || (error.status && error.status < 500)) throw error;
      }
    }
  }
  async function upload() {
    if (busy || !selected.length) return;
    setBusy(true); stopped = false; showError(''); clearTimeout(poll);
    el('file-filter').value = 'all';
    try {
      if (!jobId) {
        const storageKey = `music-import-request:${root.dataset.page}`;
        let key;
        try { key = sessionStorage.getItem(storageKey); } catch (_) { /* storage optional */ }
        key = key || requestKey();
        try { sessionStorage.setItem(storageKey, key); } catch (_) { /* storage optional */ }
        const created = await request('', {request_key:key}); jobId = created.id;
        root.dataset.job = jobId;
        history.replaceState(null, '', `${root.dataset.page}/${encodeURIComponent(jobId)}`);
        root.dataset.job = jobId;
        try { sessionStorage.removeItem(storageKey); } catch (_) { /* storage optional */ }
      }
      let current = await page(0);
      if (current.state === 'declaring') {
        const pageSize = Math.min(500, caps.manifest_page_size || 500);
        let entries = [], pageNumber = 0, pageBytes = 0;
        for (const entry of selected) {
          const entryBytes = new TextEncoder().encode(JSON.stringify(entry)).length + 1;
          if (entries.length && (entries.length >= pageSize || pageBytes + entryBytes > 768 * 1024)) {
            if (stopped) return;
            await action('manifest', {page_key:`page-${pageNumber++}`, entries});
            entries = []; pageBytes = 0;
          }
          entries.push(entry); pageBytes += entryBytes;
        }
        if (entries.length && !stopped) await action('manifest', {page_key:`page-${pageNumber}`, entries});
        if (stopped) return;
        await action('seal');
      }
      el('picker').hidden = true;
      for (let start = 0; !stopped;) {
        current = await page(start); offset = start; render(current);
        const pending = current.items.filter(item => !finished.has(item.state));
        let cursor = 0;
        // Each status has whole-batch receipts. Track only this page's new transfers
        // on top, so pagination and resume never reset the bar or count bytes twice.
        const pageProgress = new Map();
        const update = () => {
          let uploaded = 0, received = 0;
          for (const entry of pageProgress.values()) {
            uploaded += entry.loaded;
            if (entry.received) received++;
          }
          renderUploadProgress(current, uploaded, received);
        };
        const workers = Array.from({length:Math.min(2, limits.max_uploads || 1)}, async () => {
          while (cursor < pending.length && !stopped) {
            const item = pending[cursor++];
            await uploadItem(item, start, (loaded, received = false) => {
              pageProgress.set(item.id, {loaded:Math.min(item.size, loaded), received}); update();
            });
          }
        });
        const results = await Promise.allSettled(workers);
        const failed = results.find(result => result.status === 'rejected');
        if (failed) throw failed.reason;
        if (current.next_offset === null || current.next_offset === undefined) break;
        start = current.next_offset;
      }
      if (stopped) return;
      current = await page(0);
      if (current.received_files < current.total_files) {
        render(current);
        throw new ApiError('Some files still need uploading. Choose the same files or folder to continue. Changed source files need a new import. Completed uploads keep their received bytes.');
      }
      el('stage').textContent = 'Checking files…';
      el('import-helper').textContent = 'Checking the uploaded audio. Files are not imported yet.';
      await action('review');
      offset = 0;
    } catch (error) { explain(error); }
    finally {
      setBusy(false);
      if (jobId) await refresh().catch(explain);
    }
  }
  async function perform(name) {
    if (busy) return;
    setBusy(true); showError(''); clearTimeout(poll);
    try {
      const data = name === 'approve' ? {revision:job.revision,request_key:`approve-${job.id}-${job.revision}`} : {};
      await action(name, data);
    } catch (error) { explain(error); }
    finally { setBusy(false); await refresh().catch(explain); }
  }
  for (const name of ['file-picker','folder-picker']) {
    el(name).addEventListener('change', event => choose(event.target.files).catch(explain));
    if (name === 'file-picker') el(name).accept = caps.formats.map(format => `.${format}`).join(',');
  }
  el('upload').addEventListener('click', upload);
  el('reselect').addEventListener('click', () => {
    el('picker').hidden = false; el('picker-heading').textContent = 'Resume your upload';
    el('selection-summary').textContent = 'Choose the same files or folder. Completed files will not upload again.';
    el('picker').scrollIntoView({block:'start'});
  });
  el('approve').addEventListener('click', () => perform('approve'));
  el('review').addEventListener('click', () => perform('review'));
  el('resume').addEventListener('click', () => perform('resume'));
  function stopTransfers() {
    stopped = true; clearTimeout(poll);
    for (const xhr of activeUploads) xhr.abort();
  }
  root.addEventListener('music-import-stopped', () => {
    stopTransfers(); refresh().catch(explain);
  });
  el('stop').addEventListener('click', async () => {
    stopTransfers();
    try { await action('cancel'); await refresh(); } catch (error) { explain(error); }
  });
  el('file-filter').addEventListener('change', () => { fileFilterChosen = true; offset = 0; refresh().catch(explain); });
  el('files-previous').addEventListener('click', () => { offset = Math.max(0,offset-100); refresh().catch(explain); });
  el('files-next').addEventListener('click', () => { if (job.next_offset != null) { offset = job.next_offset; refresh().catch(explain); } });
  if (jobId) refresh().catch(explain);
})();
