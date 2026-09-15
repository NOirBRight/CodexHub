import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
import { readRestartReminder, storeRestartReminder } from '../src/lib/providerWorkspace/restart.ts';
test("restart reminder is a stored flag, not a Desktop instance watch", () => {
  const map=new Map();
  globalThis.localStorage={getItem:k=>map.get(k) ?? null,setItem:(k,v)=>map.set(k,v),removeItem:k=>map.delete(k)};
  try {
    storeRestartReminder(true);
    assert.equal(readRestartReminder(),true);
    storeRestartReminder(false);
    assert.equal(readRestartReminder(),false);
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

test('force cache writes commit without waiting for startTransition', async () => {
  const source=fs.readFileSync(new URL('../src/App.tsx',import.meta.url),'utf8');
  const body=source.slice(source.indexOf('  const runCachedRequest = useCallback('),source.indexOf('  const setRuntimeCacheData = useCallback('));
  assert.match(body, /const publishCacheUpdate = \(force: boolean \| undefined, commit: \(\) => void\) => \{/);
  assert.match(body, /publishCacheUpdate\(options\?\.force, commit\);/);
  const code=ts.transpileModule(body+'\nglobalThis.run = runCachedRequest;', {compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
  let current={};
  let transitioned=false;
  const context={useCallback:f=>f,runtimeInflight:{current:{}},runtimeRef:{current:{}},startUiTransition:f=>{transitioned=true;f()},setRuntime:f=>{current=f(current)},setCacheData:(o,k,v)=>({...o,[k]:v}),setCacheError:(o,k,v)=>({...o,[k]:v}),messageFromError:String,setBanner:()=>{}};
  vm.createContext(context);vm.runInContext(code,context);
  await context.run('gatewayClients',()=>Promise.resolve('connected'),{force:true,quiet:true});
  assert.equal(current.gatewayClients,'connected');
  assert.equal(transitioned,false);
  await context.run('gatewayEvents',()=>Promise.resolve('later'),{quiet:true});
  assert.equal(current.gatewayEvents,'later');
  assert.equal(transitioned,true);
});

test('workspace reminder does not restart Codex or watch Desktop', () => {
  const source=fs.readFileSync(new URL('../src/pages/ProvidersPage.tsx',import.meta.url),'utf8');
  assert.match(source, /onDismissRestartReminder=\{\(\) => updateRestartReminder\(false\)\}/);
  assert.doesNotMatch(source, /onRestartCodex/);
  assert.doesNotMatch(source, /setInterval\(\(\) => void check\(\), 5000\)/);
  assert.doesNotMatch(source, /codexRestartObserved/);
  assert.doesNotMatch(source, /restartCodex = false/);
});
