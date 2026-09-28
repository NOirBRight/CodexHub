import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { test } from 'node:test';

const source = readFileSync(new URL('../browser-extension/popup.js', import.meta.url), 'utf8');
async function run({ tabUrl = 'http://127.0.0.1:1234/', navigate = false, authenticated = true } = {}) {
  let click;
  const requests = [];
  const cookieReads = [];
  const nodes = { connect: { addEventListener: (_, fn) => { click = fn; } }, status: {} };
  const context = {
    URL, document: { getElementById: id => nodes[id] },
    location: new URL(tabUrl),
    sessionStorage: { getItem: () => 'synthetic-session' },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    window: { dispatchEvent() {} },
    fetch: async (path, options) => {
      requests.push({ path, options });
      return { ok: true, json: async () => path === '/api/status'
        ? { runtime: { installed: true }, settings: {} }
        : { authenticated, restart_required: true } };
    },
    chrome: {
      tabs: { query: async () => [{ id: 1, url: tabUrl }] },
      cookies: { getAll: async query => {
        cookieReads.push(query.url);
        if (navigate) context.location = new URL('https://attacker.test/');
        return [{ domain: '.chatgpt.com', path: '/', secure: true, value: 'synthetic' },
                { domain: '.google.com', path: '/', secure: true, value: 'excluded' }];
      } },
      scripting: { executeScript: async ({ func, args }) => [{ result: await func(...args) }] },
    },
  };
  vm.runInNewContext(source, context);
  await click();
  return { requests, cookieReads, message: nodes.status.textContent };
}
test('only ChatGPT cookies cross the authenticated settings seam', async () => {
  const result = await run();
  assert.deepEqual(result.cookieReads, ['https://chatgpt.com/']);
  const sent = JSON.parse(result.requests[1].options.body).cookies;
  assert.equal(sent.length, 1);
  assert.equal(sent[0].domain, '.chatgpt.com');
  assert.match(result.message, /没有自动重启/);
});
test('wrong tab cannot read cookies', async () => {
  const result = await run({ tabUrl: 'https://attacker.test/' });
  assert.equal(result.cookieReads.length, 0);
  assert.equal(result.requests.length, 0);
});
test('tab navigation during capture cannot send credentials to another origin', async () => {
  const result = await run({ navigate: true });
  assert.deepEqual(result.requests.map(request => request.path), ['/api/status']);
});
test('unverified response never reports connected', async () => {
  const result = await run({ authenticated: false });
  assert.match(result.message, /连接未完成/);
});
