/* Shijhon native library transport integration. No credentials are stored or logged. */
(() => {
  "use strict";
  const original = window.fetch.bind(window);
  const nativeReads = /^\/api\/(song|album|artist|genre|tag)(\/[^/]+)?\/?$/;
  const restReads = /^\/rest\/(getAlbumList2?|getAlbum|getArtist|getArtists|getIndexes|getGenres|getMusicDirectory|getRandomSongs|getSongsByGenre|search2|search3|getScanStatus)(\.view)?$/;
  const wait = (milliseconds, signal) => new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason || new DOMException("Aborted", "AbortError"));
      return;
    }
    const abort = () => {
      clearTimeout(timer);
      reject(signal.reason || new DOMException("Aborted", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", abort);
      resolve();
    }, milliseconds);
    signal?.addEventListener("abort", abort, { once: true });
  });
  window.fetch = async (input, options) => {
    const request = input instanceof Request ? input : null;
    const method = (options?.method || request?.method || "GET").toUpperCase();
    let target;
    try { target = new URL(request ? request.url : String(input), window.location.href); }
    catch { return original(input, options); }
    if (!['GET', 'HEAD'].includes(method) || target.origin !== window.location.origin ||
        !(nativeReads.test(target.pathname) || restReads.test(target.pathname))) {
      return original(input, options);
    }
    const signal = options && Object.prototype.hasOwnProperty.call(options, 'signal')
      ? options.signal : request?.signal;
    const deadline = performance.now() + 30000;
    for (let attempt = 0; ; attempt += 1) {
      const response = await original(input, options);
      const remaining = deadline - performance.now();
      if (response.status !== 503 ||
          response.headers.get('X-Shijhon-View-State') !== 'indexing' ||
          attempt >= 31 || remaining <= 0) return response;
      const raw = response.headers.get('Retry-After');
      const seconds = Number(raw);
      const milliseconds = raw && Number.isFinite(seconds)
        ? seconds * 1000 : raw ? Date.parse(raw) - Date.now() : 250;
      const delay = Math.min(remaining, Math.max(100, Math.min(2000,
        Number.isFinite(milliseconds) ? milliseconds : 250)));
      if (delay >= remaining) return response;
      await response.body?.cancel();
      await wait(delay, signal);
    }
  };
})();
