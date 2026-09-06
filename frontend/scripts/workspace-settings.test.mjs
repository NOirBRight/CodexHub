import assert from 'node:assert/strict';
import test from 'node:test';
import { rebaseSettingsDraft } from '../src/lib/workspaceSettings.ts';
test('background refresh preserves unsaved fields and updates clean fields',()=>{
  const baseline={proxy_port:9099,include_official_models:true,locale:'en-US',official_model_sort_order:['a']};
  const draft={...baseline,proxy_port:8080};
  const incoming={...baseline,include_official_models:false,official_model_sort_order:['b','a']};
  assert.deepEqual(rebaseSettingsDraft(draft,baseline,incoming),{...incoming,proxy_port:8080});
  assert.deepEqual(baseline.official_model_sort_order,['a']);
  assert.equal(rebaseSettingsDraft(null,null,incoming),incoming);
  assert.equal(rebaseSettingsDraft(draft,baseline,null),draft);
});
test('an accepted save becomes clean without reverting local values',()=>{
  const baseline={proxy_port:9099,locale:'en-US'},draft={...baseline,proxy_port:8080};
  assert.deepEqual(rebaseSettingsDraft(draft,baseline,draft),draft);
  assert.deepEqual(rebaseSettingsDraft(draft,draft,{...draft,locale:'zh-CN'}),{...draft,locale:'zh-CN'});
});
