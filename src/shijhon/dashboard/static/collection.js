/* Bounded row selection. Every-match selections and mutations remain server-side. */
(() => {
  const root = document.querySelector('[data-collection]');
  if (!root) return;
  const source = root.querySelector('[data-source-progress]');
  if (source) {
    const deadline = Date.now() + 10 * 60 * 1000;
    const text = source.querySelector('[data-source-text]');
    const pollSource = async () => {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 12000);
      let again = true;
      try {
        const response = await fetch(source.dataset.url, {cache:'no-store', signal:controller.signal});
        if (response.redirected && new URL(response.url).origin === location.origin) { location.assign(response.url); return; }
        if (!response.ok) throw new Error();
        const state = await response.json();
        if (state.state === 'ready' && state.ready) { location.reload(); return; }
        if (state.failed && state.state !== 'indexing') {
          text.textContent = 'Couldn’t refresh your music. Your previous complete library is retained. Try Refresh music again.';
          again = false;
        } else {
          text.textContent = `Preparing your library… · ${state.indexed.toLocaleString()} songs read. You can leave this page.`;
        }
      } catch (_) {
        text.textContent = 'Preparing your library. Progress is temporarily unavailable; reconnecting…';
      } finally { clearTimeout(timeout); }
      if (again && Date.now() < deadline) setTimeout(pollSource, 2000);
      else if (again) text.textContent = 'Your library is still preparing. Open Collection again to check its progress.';
    };
    setTimeout(pollSource, 1000);
  }
  const key = root.dataset.selectionKey;
  let chosen = new Set();
  let filtersCleared = false;
  try {
    const old = JSON.parse(sessionStorage.getItem('collection-selection') || '{}');
    filtersCleared = old.key !== key && Array.isArray(old.ids) && old.ids.length > 0;
    if (old.key === key && Array.isArray(old.ids) && old.ids.length <= 1000) chosen = new Set(old.ids);
  } catch (_) { /* Storage is an optional convenience. */ }
  const rows = [...root.querySelectorAll('[data-row-select]')];
  const page = root.querySelector('[data-page-select]');
  const input = root.querySelector('[data-selected-input]');
  const count = root.querySelector('[data-selection-count]');
  const summary = root.querySelector('[data-selection-summary]');
  const selected = root.querySelector('[name=selection][value=selected]');
  const matching = root.querySelector('[name=selection][value=matching]');
  const scope = root.dataset.scope || 'album';
  const units = total => `${scope}${total === 1 ? '' : 's'}`;
  const matchingTotal = Number(root.dataset.matchingTotal || 0);
  // With scripting, the bulk bar appears only once something is selected or every match
  // is chosen; without it, the bar is always there (collection.css).
  const bar = root.querySelector('[data-bulk-bar]');
  const everyMatch = root.querySelector('[data-select-matching]');
  root.dataset.enhanced = '';
  if (everyMatch) everyMatch.hidden = false;
  const clear = root.querySelector('[data-clear-selection]');
  if (clear) clear.hidden = false;
  function selectRows() { if (selected) selected.checked = true; }
  // A message that is not the selection count: shown beside the checkboxes and in the bar.
  function say(message) {
    if (count) { count.textContent = message; count.classList.add('is-notice'); }
    if (summary) summary.textContent = message;
  }
  function update() {
    rows.forEach(row => { row.checked = chosen.has(row.dataset.rowSelect); });
    if (page) {
      page.checked = rows.length > 0 && rows.every(row => row.checked);
      page.indeterminate = rows.some(row => row.checked) && !page.checked;
    }
    const active = chosen.size > 0 || Boolean(matching?.checked);
    const status = matching?.checked
      ? `All ${matchingTotal.toLocaleString()} matching ${units(matchingTotal)} across all pages`
      : `${chosen.size.toLocaleString()} ${units(chosen.size)} selected${chosen.size > rows.filter(r => r.checked).length ? ' across pages' : ''}`;
    if (count) { count.textContent = active ? status : ''; count.classList.remove('is-notice'); }
    if (summary) summary.textContent = status;
    bar?.toggleAttribute('data-active', active);
    if (everyMatch) everyMatch.hidden = Boolean(matching?.checked);
    const selectedLabel = root.querySelector('[data-selected-label]');
    if (selectedLabel) selectedLabel.textContent = `(${chosen.size})`;
    root.querySelectorAll('[data-bulk-action]').forEach(button => { button.disabled = chosen.size === 0 && !matching?.checked; });
    if (input) input.value = JSON.stringify([...chosen]);
    try { sessionStorage.setItem('collection-selection', JSON.stringify({key,ids:[...chosen]})); } catch (_) {}
  }
  rows.forEach(row => row.addEventListener('change', () => {
    selectRows();
    if (row.checked && chosen.size >= 1000 && !chosen.has(row.dataset.rowSelect)) {
      update();
      say(`Select up to 1,000 ${units(1000)}. Choose Every match for larger changes.`);
      return;
    }
    if (row.checked) chosen.add(row.dataset.rowSelect); else chosen.delete(row.dataset.rowSelect);
    update();
  }));
  page?.addEventListener('change', () => {
    selectRows();
    if (page.checked && new Set([...chosen,...rows.map(row => row.dataset.rowSelect)]).size > 1000) {
      update();
      say(`This page would exceed the limit of 1,000 selected ${units(1000)}. Choose Every match for larger changes.`);
      return;
    }
    rows.forEach(row => {
      if (page.checked && chosen.size < 1000) chosen.add(row.dataset.rowSelect);
      else if (!page.checked) chosen.delete(row.dataset.rowSelect);
    });
    update();
  });
  clear?.addEventListener('click', () => {
    chosen.clear(); selectRows(); update();
    (page || everyMatch)?.focus();
  });
  everyMatch?.addEventListener('click', () => {
    if (!matching) return;
    matching.checked = true; update(); matching.focus();
  });
  root.querySelector('[data-selection-form]')?.addEventListener('submit', () => {
    try { sessionStorage.removeItem('collection-selection'); } catch (_) {}
  });
  root.querySelectorAll('[name=selection]').forEach(radio => radio.addEventListener('change', update));
  root.querySelectorAll('select[name=scope]').forEach(select => select.addEventListener('change', () => {
    if (select.value === 'song') return;
    for (const name of ['album_id', 'artist_id']) {
      const input = select.form?.querySelector(`input[name=${name}]`);
      if (input) input.value = '';
    }
  }));
  root.querySelector('[data-music-pool]')?.addEventListener('change', event => {
    // Everything on the server is another pool of music: switching to or from it starts a
    // fresh result set, including without JavaScript.
    const form = event.target.form;
    const before = form?.querySelector('input[name=previous_pool]')?.value;
    if (before === (event.target.value === 'server' ? 'server' : 'personal')) return;
    for (const name of ['album_id', 'artist_id']) {
      const input = form?.querySelector(`input[name=${name}]`);
      if (input) input.value = '';
    }
  });
  root.querySelectorAll('[data-create-profile]').forEach(form => {
    const start = form.querySelector('select[name=start]');
    const source = form.querySelector('[data-copy-source]');
    if (!start || !source) return;
    const updateSource = () => {
      source.hidden = start.value !== 'copy';
      source.style.display = source.hidden ? 'none' : '';
    };
    start.addEventListener('change', updateSource);
    updateSource();
  });
  // The "More" menu closes like a menu: outside it, or with Escape.
  const menu = root.querySelector('[data-collection-menu]');
  if (menu) {
    document.addEventListener('click', event => { if (menu.open && !menu.contains(event.target)) menu.open = false; });
    menu.addEventListener('keydown', event => {
      if (event.key === 'Escape' && menu.open) { menu.open = false; menu.querySelector('summary')?.focus(); }
    });
  }
  root.querySelectorAll('[data-cover-image]').forEach(img => { const failed = () => { if (img.dataset.coverFallback) { const fallback = img.dataset.coverFallback; delete img.dataset.coverFallback; img.src = fallback; } else img.hidden = true; }; img.addEventListener('error', failed); if (img.complete && !img.naturalWidth) failed(); });
  update();
  if (filtersCleared) say('Selection cleared because the music, library or filters changed.');
  const operation = root.querySelector('[data-operation]');
  if (operation && ['preparing','queued','applying'].includes(operation.dataset.state)) {
    const poll = async () => {
      try {
        const response = await fetch(`${location.pathname.replace('/changes/','/progress/')}`, {cache:'no-store'});
        if (!response.ok) throw new Error();
        const state = await response.json();
        if (!['preparing','queued','applying'].includes(state.state)) { location.reload(); return; }
        const text = operation.querySelector('[data-progress-text]');
        if (text) text.textContent = `${state.processed.toLocaleString()} of ${state.total.toLocaleString()} ${state.total === 1 ? 'song' : 'songs'} processed. You can leave this page.`;
        const progress = operation.querySelector('progress');
        if (progress) { progress.max = state.total || 1; progress.value = state.processed; }
        setTimeout(poll, 1200);
      } catch (_) {
        const text = operation.querySelector('[data-progress-text]');
        if (text) text.textContent = 'Progress is temporarily unavailable. Your change is retained. Reload to reconnect.';
      }
    };
    setTimeout(poll, 1000);
  }
})();
