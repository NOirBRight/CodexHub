import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

test('update hook recreates its lifecycle when StrictMode replays effects',async()=>{
  const source=await readFile(new URL('../src/hooks/useAppUpdateLifecycle.ts',import.meta.url),'utf8');
  const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
  const effects=[],instances=[],exports={};
  const react={useRef:value=>({current:value}),useState:init=>[typeof init==='function'?init():init,()=>{}],useMemo:fn=>fn(),useEffect:effect=>effects.push(effect)};
  const createAppUpdateLifecycle=()=>{const instance={disposed:false,checks:0,subscribe:()=>()=>{},refreshCompletion:()=>{},dispose(){this.disposed=true;},checkForUpdates(){assert.equal(this.disposed,false);this.checks++;},startScheduling(){assert.equal(this.disposed,false);},startInstall(){}};instances.push(instance);return instance;};
  const require=id=>id==='react'?react:id.endsWith('PageToast')?{useToasts:()=>({})}:id.endsWith('appUpdateLifecycle')?{createAppUpdateLifecycle}:{api:{}};
  new Function('exports','require',compiled)(exports,require);
  const hook=exports.useAppUpdateLifecycle({confirm:()=>{},getRuntime:()=>({}),setRuntime:()=>{},translate:k=>k});
  assert.equal(instances.length,0,'render must not own disposable effects');
  const cleanup=effects[0]();hook.startScheduling(true);cleanup();
  const secondCleanup=effects[0]();hook.checkForUpdates();
  assert.equal(instances.length,2);assert.equal(instances[0].disposed,true);assert.equal(instances[1].checks,1);
  secondCleanup();assert.equal(instances[1].disposed,true);
});
