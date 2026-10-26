(() => {
  'use strict';
  const root = document.querySelector('[data-music-imports]');
  const panel = document.querySelector('[data-import-removal]');
  if (!root || !panel) return;
  const el = name => panel.querySelector(`[data-removal-${name}]`);
  let state = null, offset = 0, pending = false, timer = null, approvalKey = '', requestError = '';
  const n = value => Number(value || 0).toLocaleString();
  const labels = {none: 'No removal reviewed.', reviewing: 'Checking files and shared use…',
    review: 'Ready for your review.', checking: 'Rechecking the approved files and sharing…',
    removing: 'Removing the approved files from the library…', removed: 'Removal complete. Retained originals can be restored.',
    restoring: 'Restoring original files and verifying native identities…', restored: 'Files restored at their original paths.',
    stale: 'Sharing changed. Review again before removing files.'};
  const url = () => `${root.dataset.api}/${encodeURIComponent(root.dataset.job)}/removal`;
  function error(message) { el('error').textContent = message; el('error').hidden = !message; }
  async function request(action, body) {
    const response = await fetch(url() + (action ? `/${action}` : `?offset=${offset}`), {
      method: action ? 'POST' : 'GET', cache: 'no-store', credentials: 'same-origin',
      headers: action ? {'Content-Type':'application/json', 'X-CSRF-Token':root.dataset.csrf} : {},
      body: action ? JSON.stringify(body || {}) : undefined
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'The removal could not continue.');
    return result;
  }
  function render(value) {
    state = value;
    if (el('used')) el('used').checked = !!value.include_used;
    panel.hidden = false;
    const kept = Number(value.total || 0) - Number(value.remove_files || 0);
    el('summary').textContent = `${labels[value.state] || value.state} ${value.total ? `${n(value.remove_files)} ${Number(value.remove_files) === 1 ? 'file' : 'files'} to remove; ${n(kept)} files kept available; ${n(value.removed_files)} currently removed.` : ''}`;
    if (value.newly_indexed_files) el('summary').textContent += ` ${n(value.newly_indexed_files)} files indexed after review. Review removal again to check their current identities and use.`;
    // Songs the library already had: this import only linked them, so they leave (and come
    // back to) the importer's own imports; nobody else's music changes.
    if (value.linked_songs) el('summary').textContent += ` ${n(value.linked_songs)} ${Number(value.linked_songs) === 1 ? 'song was' : 'songs were'} already in the library: ${value.state === 'removed' ? 'they are out of' : 'removal takes them out of'} the importer's own imports only, and Restore brings them back.`;
    error(requestError || value.error || '');
    el('stop').hidden = ['done','cancelled'].includes(value.import_state);
    if (value.state === 'removed' && value.audio_withdrawing) el('summary').textContent += ` ${n(value.audio_withdrawing)} catalog ${Number(value.audio_withdrawing) === 1 ? 'song still plays' : 'songs still play'} this import's audio: ${Number(value.audio_withdrawing) === 1 ? 'it goes' : 'they go'} back to the add-ons shortly.`;
    el('approve').hidden = value.state !== 'review' || !(value.remove_files || value.linked_active);
    el('approve').disabled = pending;
    el('approve').textContent = value.remove_files
      ? `Confirm removal of ${n(value.remove_files)} ${Number(value.remove_files) === 1 ? 'file' : 'files'} for everyone`
      : `Confirm removal of ${n(value.linked_active)} linked ${Number(value.linked_active) === 1 ? 'song' : 'songs'} from your imports`;
    el('restore').hidden = !['removing','removed','restoring'].includes(value.state);
    el('retry').hidden = !value.error || !['reviewing','checking','removing','restoring'].includes(value.state);
    el('review').hidden = !['none','review','restored','stale'].includes(value.state);
    const fragment = document.createDocumentFragment();
    for (const item of value.items || []) {
      const li = document.createElement('li'), info = document.createElement('div');
      const title = document.createElement('strong'), detail = document.createElement('small');
      title.textContent = item.path;
      detail.textContent = `${item.remove_file ? 'Remove from shared library' : 'Keep available'}${!item.song_id ? ' · Published file has no native index entry' : ''}${item.reasons.length ? ' · ' + item.reasons.join(', ') : ' · No recorded use'}`;
      if (item.restored_song_id) detail.textContent = `Restored · Indexed after review as ${item.restored_song_id} · Review removal again for current use`;
      info.append(title, detail); li.append(info); fragment.append(li);
    }
    el('files').replaceChildren(fragment);
    el('prev').disabled = !offset || pending;
    el('next').disabled = value.next_offset === null || value.next_offset === undefined || pending;
    el('page').textContent = value.total ? `${n(offset + 1)}–${n(Math.min(offset + 100, value.total))} of ${n(value.total)}` : '';
    clearTimeout(timer);
    if (['reviewing','checking','removing','restoring'].includes(value.state) && !value.error) {
      timer = setTimeout(() => refresh().catch(failure), 3000);
    }
  }
  function failure(exc) { requestError = exc.message || 'Connection interrupted. Reload to check saved progress.'; error(requestError); }
  async function refresh() { if (root.dataset.job) render(await request()); }
  async function act(action, body) {
    if (pending) return;
    pending = true;
    for (const button of panel.querySelectorAll('button')) button.disabled = true;
    try {
      requestError = ''; offset = 0; render(await request(action, body));
      if (action === 'stop') root.dispatchEvent(new Event('music-import-stopped'));
    }
    catch (exc) { failure(exc); }
    finally {
      pending = false;
      for (const name of ['review','restore','retry','stop']) el(name).disabled = false;
      if (state) render(state);
    }
  }
  el('stop').addEventListener('click', () => act('stop'));
  el('review').addEventListener('click', () => {
    approvalKey = `removal.${Date.now()}.${Math.random().toString(36).slice(2)}`;
    act('review', {include_used: !!el('used')?.checked});
  });
  el('used')?.addEventListener('change', () => { el('approve').hidden = true; });
  el('approve').addEventListener('click', () => {
    approvalKey ||= `removal.${Date.now()}.${Math.random().toString(36).slice(2)}`;
    act('approve', {fingerprint:state.fingerprint, request_key:approvalKey});
  });
  el('restore').addEventListener('click', () => act('restore'));
  el('retry').addEventListener('click', () => act('retry'));
  el('prev').addEventListener('click', () => { offset = Math.max(0, offset - 100); refresh().catch(failure); });
  el('next').addEventListener('click', () => { offset = state.next_offset; refresh().catch(failure); });
  new MutationObserver(() => { offset = 0; refresh().catch(failure); }).observe(root, {attributes:true, attributeFilter:['data-job']});
  if (root.dataset.job) refresh().catch(failure);
})();
