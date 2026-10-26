/* Run in the real native website through the collaborative browser. Synthetic reads only. */
async function checkNativeLibraryRetries(source) {
  const checks = [];
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const pending = () => new Response('indexing', {
    status: 503, headers: { 'X-Shijhon-View-State': 'indexing', 'Retry-After': '0' },
  });
  const install = (fetch, clock = performance) => {
    const target = { fetch, location: window.location };
    new Function('window', 'performance', source)(target, clock);
    return target.fetch;
  };
  let calls = [];
  const request = new Request(new URL('/api/song?_sort=title', location.href), {
    credentials: 'include', headers: { 'X-Fixture': 'kept' },
  });
  const options = { headers: { 'X-Fixture-Options': 'kept' } };
  const read = install(async (...args) => {
    calls.push(args);
    return calls.length === 1 ? pending() : new Response('[]');
  });
  assert((await read(request, options)).status === 200 && calls.length === 2, 'tagged retry');
  assert(calls.every(([input, init]) => input === request && init === options), 'original request/options');
  checks.push('tagged GET retries without replacing Request, credentials or headers');
  for (const [path, method, tagged] of [
    ['/api/song', 'GET', false], ['/api/song', 'POST', true],
    ['/auth/login', 'GET', true], ['/rest/stream', 'GET', true],
    ['/shijhon/collection', 'GET', true], ['/oauth/token', 'GET', true],
    ['https://example.invalid/api/song', 'GET', true],
  ]) {
    let count = 0;
    const fetch = install(async () => { count += 1; return tagged ? pending() : new Response('', {status: 503}); });
    assert((await fetch(path, {method})).status === 503 && count === 1, 'unrelated request retried');
  }
  checks.push('writes, auth, audio, dashboard, OAuth, cross-origin and unrelated 503 do not retry');
  for (const method of ['GET', 'HEAD']) {
    let count = 0;
    const fetch = install(async () => ++count === 1 ? pending() : new Response());
    assert((await fetch('/api/album', {method})).status === 200 && count === 2, 'read method retry');
  }
  checks.push('GET and HEAD supported');
  const controller = new AbortController();
  let cancelledCalls = 0;
  const cancel = install(async () => { cancelledCalls += 1; return pending(); });
  const aborting = cancel(new Request(new URL('/api/song?title=old', location.href), {signal: controller.signal}));
  setTimeout(() => controller.abort(), 20);
  let aborted = false;
  try { await aborting; } catch (error) { aborted = error.name === 'AbortError'; }
  assert(aborted && cancelledCalls === 1, 'abort during retry timer');
  checks.push('AbortSignal cancels the retry timer after one read');
  let clock = 0, timedCalls = 0;
  const timed = install(async () => { timedCalls += 1; clock += 15000; return pending(); }, {now: () => clock});
  assert((await timed('/api/song')).status === 503 && timedCalls === 2, 'deadline returned final error');
  checks.push('30-second deadline returns the final native error');
  return checks;
}
