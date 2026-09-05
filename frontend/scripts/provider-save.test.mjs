import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createWorkspaceSaveCoordinator } from '../src/lib/providerWorkspace/save.ts';
function operation(overrides = {}) {
  const events = [];
  const calls = [];
  return {
    events, calls,
    persist: async () => { calls.push('persist'); return 'saved'; },
    committed: value => calls.push(value),
    publish: async () => { calls.push('publish'); },
    sync: async () => { calls.push('sync'); },
    feedback: event => events.push(event),
    setBusy: value => calls.push(value),
    ...overrides,
  };
}
test('save failure does not commit, publish or sync; releases editing lock', async () => {
  const c = createWorkspaceSaveCoordinator();
  const op = operation({ persist: async () => { throw Error('disk'); } });
  assert.equal((await c.save(op)).kind, 'error');
  assert.deepEqual(op.calls, [true, false]);
  assert.equal(op.events.at(-1).saved, false);
  assert.equal(op.events.at(-1).retry, undefined);
  assert.equal(c.busy, false);
});
test('publication failure is a saved result and retry does not persist again', async () => {
  const c = createWorkspaceSaveCoordinator(); let attempts = 0;
  const op = operation({ publish: async () => { if (++attempts === 1) throw Error('publish'); } });
  assert.deepEqual(await c.save(op), { kind: 'ok', value: 'saved' });
  assert.equal(op.events.at(-1).saved, true);
  await op.events.at(-1).retry();
  assert.equal(attempts, 2);
  assert.equal(op.calls.filter(x => x === 'persist').length, 1);
  assert.equal(op.calls.filter(x => x === 'sync').length, 1);
  assert.equal(op.events.at(-1).stage, 'complete');
});
test('sync failure retries only sync', async () => {
  const c = createWorkspaceSaveCoordinator(); let attempts = 0;
  const op = operation({ sync: async () => { if (++attempts === 1) throw Error('sync'); } });
  await c.save(op); await op.events.at(-1).retry();
  assert.equal(attempts, 2);
  assert.equal(op.calls.filter(x => x === 'publish').length, 1);
  assert.equal(op.calls.filter(x => x === 'persist').length, 1);
});
test('new save retires old retry, including previously captured callback', async () => {
  const c = createWorkspaceSaveCoordinator(); let attempts = 0;
  const old = operation({ publish: async () => { attempts++; throw Error('old'); } });
  await c.save(old); const retry = old.events.at(-1).retry;
  await c.save(operation());
  assert.equal(old.events.at(-1).retry, undefined);
  await retry(); assert.equal(attempts, 1);
});
test('overlapping save and retry cannot overwrite the active save', async () => {
  const c = createWorkspaceSaveCoordinator(); let release;
  const pending = c.save(operation({ persist: () => new Promise(r => { release = r; }) }));
  assert.equal(c.busy, true);
  const other = operation();
  assert.equal((await c.save(other)).kind, 'blocked');
  assert.deepEqual(other.calls, []);
  release('saved'); await pending; assert.equal(c.busy, false);
});
test('publication order follows durable commit, including success without publication', async () => {
  const c = createWorkspaceSaveCoordinator(); const op = operation();
  await c.save(op);
  assert.deepEqual(op.calls, [true, 'persist', 'saved', 'publish', 'sync', false]);
  const local = operation({ publish: undefined, sync: undefined });
  await c.save(local);
  assert.deepEqual(local.calls, [true, 'persist', 'saved', false]);
});

test('failed replacement save preserves the previous publication retry', async () => {
  const c = createWorkspaceSaveCoordinator(); let attempts = 0;
  const old = operation({ publish: async () => { if (++attempts === 1) throw Error('publish'); } });
  await c.save(old);
  const retry = old.events.at(-1).retry;
  let rejectPersist;
  const pending = c.save(operation({ persist: () => new Promise((_, reject) => { rejectPersist = reject; }) }));
  await retry(); // The retry remains available but cannot overlap the new save.
  assert.equal(attempts, 1);
  rejectPersist(Error('disk full'));
  assert.equal((await pending).kind, 'error');
  assert.equal(old.events.at(-1).retry, retry);
  await retry();
  assert.equal(attempts, 2);
  assert.equal(old.events.at(-1).stage, 'complete');
  assert.equal(old.calls.filter(x => x === 'persist').length, 1);
});

import { readCodexRestartNotice } from '../src/lib/providerWorkspace/restart.ts';
test('restart readback failure does not turn a durable save into a failure or retry publication', async () => {
  for (const failingRead of ['getStatus', 'getCodexDesktopStatus']) {
    const source = { getStatus: async () => ({ mode: 'custom' }), getCodexDesktopStatus: async () => ({ running: true }) };
    source[failingRead] = async () => { throw Error('readback unavailable'); };
    let notice;
    const c = createWorkspaceSaveCoordinator();
    const op = operation({ publish: async () => { notice = await readCodexRestartNotice(source); } });
    assert.equal((await c.save(op)).kind, 'ok');
    assert.equal(notice, 'unknown');
    assert.equal(op.events.at(-1).stage, 'complete');
    assert.equal(op.events.at(-1).retry, undefined);
    assert.ok(op.calls.includes('sync'));
  }
});
test('restart notice is required only for a connected running Codex App', async () => {
  for (const running of [true, false]) {
    assert.equal(await readCodexRestartNotice({
      getStatus: async () => ({ mode: 'custom' }),
      getCodexDesktopStatus: async () => ({ running }),
    }), running ? 'required' : 'none');
  }
  assert.equal(await readCodexRestartNotice({
    getStatus: async () => ({ mode: 'official' }),
    getCodexDesktopStatus: async () => { throw Error('must not query disconnected desktop'); },
  }), 'none');
});

// Execute the actual hook action with deterministic hook scheduling and I/O.
// The reducer dispatch log observes the same busy state consumed by the page.
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
test('discovered-model save keeps editing locked until publication finishes', async () => {
  const source = await readFile(new URL('../src/hooks/useProviderWorkspace.ts', import.meta.url), 'utf8');
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  let finishPersist, finishPublish;
  let busy;
  const provider = { id: 'p', name: 'P', base_url: 'local', models: [] };
  const state = { providers: [provider], settings: {}, selectedId: 'p', form: {}, pendingNewProvider: null };
  const react = {
    useRef: current => ({ current }),
    useCallback: callback => callback,
    useMemo: factory => factory(),
    useEffect: () => {},
    useReducer: () => [state, intent => { if (intent.type === 'setBusy') busy = intent.busy; }],
  };
  const core = {
    selectSelectedProvider: () => provider,
    applyDiscoveredModelsForProvider: () => ({ provider, addedCount: 1 }),
  };
  const backend = {
    discoverProviderModels: async () => [{ id: 'new' }],
    getBundledProviders: async () => [],
    saveProviders: () => new Promise(resolve => { finishPersist = resolve; }),
    generateCatalog: () => new Promise(resolve => { finishPublish = resolve; }),
    getStatus: async () => ({ mode: 'official' }),
  };
  const deps = {
    react,
    '../lib/providerWorkspace/core': core,
    '../lib/providerWorkspace/save': { createWorkspaceSaveCoordinator },
    '../lib/providerWorkspace/restart': { readCodexRestartNotice },
    '../lib/tauri': { api: backend, messageFromError: String },
  };
  const exported = {};
  new Function('exports', 'require', js)(exported, name => deps[name] ?? {});
  const workspace = exported.useProviderWorkspace({
    getSource: () => ({ ...state, catalogModels: [], modelMetadata: [] }),
    refreshGatewayState: async () => {},
    toast: { showToast: () => 'toast', updateToast: () => {} },
    t: key => key, tr: key => key,
  });
  const pending = workspace.discoverProviderModels('p');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(busy, 'save');
  finishPersist([provider]);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(busy, 'save');
  finishPublish([]);
  assert.equal((await pending).kind, 'ok');
  assert.equal(busy, null);
});
