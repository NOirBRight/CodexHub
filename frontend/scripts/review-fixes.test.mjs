import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
import { codexRestartObserved, readPendingCodexRestart, storePendingCodexRestart } from '../src/lib/providerWorkspace/restart.ts';

test('manual Codex restart clears pending only for a new running instance', () => {
  const pending = { mode:'custom', instanceId:100 };
  assert.equal(codexRestartObserved(pending,{running:true,instance_id:100}),false);
  assert.equal(codexRestartObserved(pending,{running:false,instance_id:null}),false);
  assert.equal(codexRestartObserved(pending,{running:true,instance_id:101}),true);
  assert.equal(codexRestartObserved(pending,{running:true}),false);
});

test('pending connection survives reload and confirmed restart removes it', () => {
  const map=new Map();
  globalThis.localStorage={getItem:k=>map.get(k),setItem:(k,v)=>map.set(k,v),removeItem:k=>map.delete(k)};
  try {
    const pending={mode:'official',instanceId:123};
    storePendingCodexRestart(pending);
    assert.deepEqual(readPendingCodexRestart(),pending);
    storePendingCodexRestart(null);
    assert.equal(readPendingCodexRestart(),null);
  } finally { delete globalThis.localStorage; }
});

for (const oldFails of [false,true]) test(`older usage ${oldFails?'error':'response'} cannot replace the new query`,async()=>{
  const source=fs.readFileSync(new URL('../src/App.tsx',import.meta.url),'utf8');
  const body=source.slice(source.indexOf('  const runCachedRequest = useCallback('),source.indexOf('  const setRuntimeCacheData = useCallback('));
  const code=ts.transpileModule(body+'\nglobalThis.run = runCachedRequest;', {compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
  let current={};
  const context={useCallback:f=>f,runtimeInflight:{current:{}},runtimeRef:{current:{}},startUiTransition:f=>f(),setRuntime:f=>{current=f(current)},setCacheData:(o,k,v)=>({...o,[k]:v}),setCacheError:(o,k,v)=>({...o,[k]:v}),messageFromError:String,setBanner:()=>{throw Error('stale error banner')}};
  vm.createContext(context);vm.runInContext(code,context);
  let finishOld,failOld,finishNew;
  const old=context.run('gatewayUsageSnapshot',()=>new Promise((resolve,reject)=>{finishOld=resolve;failOld=reject}),{force:true,quiet:true});
  const newer=context.run('gatewayUsageSnapshot',()=>new Promise(resolve=>finishNew=resolve),{force:true,quiet:true});
  finishNew('new 1m window');await newer;
  if(oldFails){failOld(Error('old error'));await assert.rejects(old);}else{finishOld('old 7d window');await old;}
  assert.equal(current.gatewayUsageSnapshot,'new 1m window');
});

test('restart retains custom routing while Gateway is stopped', () => {
  const source=fs.readFileSync(new URL('../src/pages/ProvidersPage.tsx',import.meta.url),'utf8');
  const start=source.indexOf('onRestartCodex={() => {');
  const handler=source.slice(start,source.indexOf('}}',start)).replace('onRestartCodex={() => {','');
  for (const mode of ['custom','official']) {
    let requested;
    new Function('pendingCodexRestart','codexStatus','realCodexConnected','applyCodexHubConnection',handler)(
      {mode,instanceId:1},{mode,proxy_running:false},false,(...args)=>{requested=args;});
    assert.deepEqual(requested,[mode,false,true]);
  }
});
