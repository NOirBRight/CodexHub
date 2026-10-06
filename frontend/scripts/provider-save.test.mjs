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
  const source = { getStatus: async () => { throw Error('readback unavailable'); } };
  let notice;
  const c = createWorkspaceSaveCoordinator();
  const op = operation({ publish: async () => { notice = await readCodexRestartNotice(source); } });
  assert.equal((await c.save(op)).kind, 'ok');
  assert.equal(notice, 'unknown');
  assert.equal(op.events.at(-1).stage, 'complete');
  assert.equal(op.events.at(-1).retry, undefined);
  assert.ok(op.calls.includes('sync'));
});
test('restart notice is required for a connected Codex regardless of Desktop', async () => {
  assert.equal(await readCodexRestartNotice({
    getStatus: async () => ({ mode: 'custom' }),
  }), 'required');
  assert.equal(await readCodexRestartNotice({
    getStatus: async () => ({ mode: 'official' }),
  }), 'none');
});

// Execute the actual hook action with deterministic hook scheduling and I/O.
// The reducer dispatch log observes the same busy state consumed by the page.
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
import { catalogOverrideToastMessage } from '../src/lib/providerWorkspace/feedback.ts';
test('provider auto-sync retains applied client feedback in the same completion or error toast', async t => {
  const source = await readFile(new URL('../src/hooks/useProviderWorkspace.ts', import.meta.url), 'utf8');
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const claudeMessage = 'Claude Code now routes new sessions through the CodexHub Gateway. Restart Claude Code.';
  const claude = { client_id: 'claude', name: 'Claude Code', status: 'applied', applied: true, skipped: false, message: claudeMessage };
  const skipped = { client_id: 'dsh', name: 'DSH', status: 'skipped', applied: false, skipped: true, message: 'Skipped DSH fixture. Restart DSH.' };
  const failed = { client_id: 'cursor', name: 'Cursor', status: 'failed', applied: false, skipped: false, message: 'Failed Cursor fixture. Restart Cursor.' };
  for (const scenario of [
    { name: 'Claude applied with Codex disconnected', mode: 'official', results: [claude] },
    { name: 'partial failure retains the applied Claude reminder', mode: 'official', results: [claude, skipped, failed] },
    { name: 'Codex required reminder and override diagnostics survive alongside Claude', mode: 'custom', results: [claude], notice: 'providers.catalogOverrideRestartCodex', diagnostics: { accepted: 1, rejected: 2, migrated: 3, reasons: {} } },
    { name: 'Codex unknown notice survives alongside Claude', mode: 'unknown', results: [claude], notice: 'providers.codexRestartStatusUnknown' },
    { name: 'skipped and failed clients do not acquire apply feedback or restart requirements', mode: 'official', results: [skipped, failed] },
  ]) {
    await t.test(scenario.name, async () => {
      const calls = [], shown = [], updated = [];
      const provider = { id: 'p', name: 'P', models: [] };
      const state = { providers: [provider], settings: { auto_sync_clients: true }, selectedId: 'p', form: {}, pendingNewProvider: null };
      const summary = {
        applied: scenario.results.filter(result => result.applied).length,
        skipped: scenario.results.filter(result => result.skipped).length,
        failed: scenario.results.filter(result => result.status === 'failed').length,
        results: scenario.results,
        message: 'Bound client sync failed',
        catalog_override_diagnostics: scenario.diagnostics,
      };
      const deps = {
        react: {
          useRef: current => ({ current }), useCallback: callback => callback,
          useMemo: factory => factory(), useEffect: () => {}, useReducer: () => [state, () => {}],
        },
        '../lib/providerWorkspace/core': { selectSelectedProvider: () => provider },
        '../lib/providerWorkspace/save': { createWorkspaceSaveCoordinator },
        '../lib/providerWorkspace/restart': { readCodexRestartNotice },
        '../lib/providerWorkspace/feedback': { catalogOverrideToastMessage },
        '../lib/tauri': { messageFromError: String, api: {
          saveProviders: async providers => { calls.push('persist'); return providers; },
          generateCatalog: async () => { calls.push('publish'); },
          getStatus: async () => { calls.push('restart-readback'); if (scenario.mode === 'unknown') throw Error('unavailable'); return { mode: scenario.mode }; },
          syncGatewayClients: async () => { calls.push('sync'); return summary; },
        } },
      };
      const exported = {};
      new Function('exports', 'require', js)(exported, name => deps[name] ?? {});
      const translate = (key, options) => key === 'providers.providerSavedCatalogWarning'
        ? `${options.saved}; ${options.message}` : options ? `${key} ${JSON.stringify(options)}` : key;
      const workspace = exported.useProviderWorkspace({
        getSource: () => ({ ...state, catalogModels: [], modelMetadata: [] }),
        refreshGatewayState: async () => { calls.push('refresh'); },
        toast: { showToast: input => { shown.push(input); return 'save-toast'; }, updateToast: (id, patch) => updated.push({ id, ...patch }) },
        t: translate, tr: translate,
      });
      assert.equal((await workspace.saveProviders([provider])).kind, 'ok');
      assert.deepEqual(calls, ['persist', 'publish', 'restart-readback', 'sync', ...(summary.failed ? [] : ['refresh'])]);
      assert.equal(shown.length, 1);
      assert.equal(shown[0].tone, 'loading');
      assert.equal(shown[0].timeoutMs, null);
      assert.ok(updated.every(patch => patch.id === 'save-toast'));
      const completed = updated.at(-1);
      assert.equal(completed.tone, summary.failed ? 'error' : 'success');
      assert.equal(completed.timeoutMs, summary.failed ? null : 6000);
      assert.equal(typeof completed.action?.onClick, summary.failed ? 'function' : 'undefined');
      if (summary.applied) assert.ok(completed.text.includes(`Claude Code: ${claudeMessage}`), completed.text);
      assert.ok(completed.text.includes(summary.failed
        ? translate('providers.syncClientsFailed', { count: summary.failed })
        : translate('providers.syncedClients', { count: summary.applied, plural: summary.applied === 1 ? '' : 's' })));
      if (summary.failed) assert.ok(completed.text.includes(summary.message));
      if (scenario.notice) assert.ok(completed.text.includes(scenario.notice));
      else assert.doesNotMatch(completed.text, /providers\.(catalogOverrideRestartCodex|codexRestartStatusUnknown)/);
      if (scenario.diagnostics) assert.ok(completed.text.includes(catalogOverrideToastMessage(scenario.diagnostics, translate)));
      assert.doesNotMatch(completed.text, /DSH|Cursor/);
      if (!summary.applied) assert.doesNotMatch(completed.text, /Restart|Claude Code|providers.syncedClients/);
    });
  }
});
test('discovery uses the current draft and keeps editing locked until publication finishes', async () => {
  const source = await readFile(new URL('../src/hooks/useProviderWorkspace.ts', import.meta.url), 'utf8');
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  let finishPersist, finishPublish;
  let busy;
  const provider = { id: 'p', name: 'P', base_url: 'https://old.test', api_key: '{env:OLD_KEY}', models: [] };
  const draft = { ...provider, name: 'Edited', base_url: 'https://new.test', api_key: 'fixture-key' };
  let discoveryArgs, savedProviders;
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
    applyDiscoveredModelsForProvider: base => ({ provider: base, addedCount: 1 }),
  };
  const backend = {
    discoverProviderModels: async (...args) => { discoveryArgs = args; return [{ id: 'new' }]; },
    getBundledProviders: async () => [],
    saveProviders: providers => { savedProviders = providers; return new Promise(resolve => { finishPersist = resolve; }); },
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
  const pending = workspace.discoverProviderModels('p', draft);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(busy, 'save');
  assert.deepEqual(discoveryArgs, [draft.base_url, draft.api_key, draft.id]);
  assert.deepEqual(savedProviders, [draft]);
  finishPersist([draft]);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(busy, 'save');
  finishPublish([]);
  assert.equal((await pending).kind, 'ok');
  assert.equal(busy, null);
});

test('published provider change persists and announces the pending restart reminder', async () => {
  const values = new Map();
  const events = [];
  globalThis.localStorage = { getItem: k => values.get(k) ?? null, setItem: (k,v) => values.set(k,v), removeItem: k => values.delete(k) };
  globalThis.window = { dispatchEvent: event => { events.push(event.type); } };
  try {
    assert.equal(await readCodexRestartNotice({
      getStatus: async () => ({ mode: 'custom' }),
    }), 'required');
    assert.equal(values.get('codexhub.restartReminder.v1'), 'true');
    assert.ok(events.includes('codexhub:pending-restart-changed'));
  } finally {
    delete globalThis.localStorage;
    delete globalThis.window;
  }
});
