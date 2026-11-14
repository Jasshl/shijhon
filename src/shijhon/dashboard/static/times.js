// Times in the browser's own zone. The server writes each time in its zone inside
// <time datetime="..." data-time="hm|day|when">; this writes it again in the same style:
// "14:05", "12 November", or "14:05" today and "12 November, 14:05" before.
(() => {
  'use strict';
  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
    'September', 'October', 'November', 'December'];
  const two = n => String(n).padStart(2, '0');
  const hm = d => `${two(d.getHours())}:${two(d.getMinutes())}`;
  const day = d => `${d.getDate()} ${MONTHS[d.getMonth()]}`;
  const sameDay = (a, b) => a.getFullYear() === b.getFullYear()
    && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();

  function text(element, now) {
    const at = new Date(element.getAttribute('datetime') || '');
    if (Number.isNaN(at.getTime())) return null;
    switch (element.dataset.time) {
      case 'hm': return hm(at);
      case 'day': return day(at);
      case 'when':
        return sameDay(at, now) && Math.abs(now - at) < 86400000 ? hm(at) : `${day(at)}, ${hm(at)}`;
      default: return null;
    }
  }

  function local(root) {
    const now = new Date();
    for (const element of root.querySelectorAll('time[data-time][datetime]')) {
      const shown = text(element, now);
      if (shown !== null && element.textContent !== shown) element.textContent = shown;
    }
  }

  // (Loaded with defer: the page is parsed.)
  local(document);
})();
