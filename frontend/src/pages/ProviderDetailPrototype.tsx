import { usePrototypeScenario } from './PrototypeTools';
import { pt } from './PrototypeLocale';
// THROWAWAY: real model controls with in-memory provider callbacks, never the live API.
import { useEffect, useRef, useState } from 'react';
import { Copy, Eye, EyeOff, RefreshCw, Save, Trash2 } from 'lucide-react';
import { ModelSection } from '../components/providers/ProviderModelSection';
import { OfficialOpenAIUsagePanel, OfficialOpenAIUsageLimitBars } from '../components/providers/OfficialOpenAIUsagePanel';
import type { Model, Provider } from '../lib/types';
import { SubscriptionPrototype, prototypeLimits } from './SubscriptionPrototype';
import { providerFixtures } from './PrototypeData';
export function makePrototypeProvider(id: string): Provider {
    const p = providerFixtures.find(x => x.id === id)!;
    const modelNames = id === 'openai' ? ['gpt-5.4', 'gpt-5.5', 'gpt-5.4-mini', 'gpt-5.3-codex-spark'] : [p.model];
    return { id, name: p.name, base_url: p.base, api_key: 'demo-not-a-real-key', enabled: true, upstream_format: 'auto', models: modelNames.map((name, i) => ({ id: name, display_name: name, enabled: true, context_window: 128000, input_modalities: id === 'deepseek' ? ['text'] : ['text', 'image'], thinking_mode: 'toggle', supported_reasoning_levels: ['low', 'medium', 'high'], default_reasoning_level: 'medium', multi_agent_version: 'v2', sort_order: i })) };
}
export function ProviderDetailPrototype({ provider, initialTab = '模型', onSave, onDelete, notify, onDirty }: {
    provider: Provider;
    initialTab?: string;
    onDirty: (dirty: boolean) => void;
    onSave: (p: Provider) => Promise<boolean>;
    onDelete: () => void;
    notify: (s: string) => void;
}) {
    const refreshTimer=useRef<number>();
    useEffect(()=>()=>window.clearTimeout(refreshTimer.current),[]);
    const [draft, setDraft] = useState(provider);
    const [showKey, setShowKey] = useState(false);
    const [tab, setTab] = useState(initialTab);
    const [officialAuth]=usePrototypeScenario('officialAuth');
    const signedIn=officialAuth==='authorized';
    const [confirmDelete, setConfirmDelete] = useState(false);
    const [testing, setTesting] = useState(false);
    const [busy, setBusy] = useState(false);
    const [usageState]=usePrototypeScenario('usage');
    const [operationResult]=usePrototypeScenario('model');
    const [saveBusy, setSaveBusy] = useState(false);
    const [formError, setFormError] = useState('');
    const [baseline, setBaseline] = useState<Record<string, 'v1' | 'v2'>>({});
    const dirty = JSON.stringify(draft) !== JSON.stringify(provider);
    const subscription = provider.auth_capabilities?.includes('subscription:xai_oauth');
    const official = provider.id === 'openai';
    useEffect(() => { onDirty(dirty); }, [dirty, onDirty]);
    function update(id: string, patch: Partial<Model>) { setDraft(p => ({ ...p, models: p.models.map(m => m.id === id ? { ...m, ...patch } : m) })); }
    const copy = (s: string) => { void navigator.clipboard.writeText(s).then(() => notify('已复制')).catch(() => notify('复制失败')); };
    return <div className="v-provider-editor"><div className="v-editor-tabs">{pt((official ? ['模型', '账户与用量'] : subscription ? ['模型', '连接配置', '订阅账户'] : ['模型', '连接配置']).map(t => <button key={t} className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>{pt(t)}</button>))}{pt(!official && <button className="v-editor-delete" onClick={() => setConfirmDelete(true)}><Trash2 size={13}/>{pt("移除 Provider")}</button>)}</div>
    <div className="v-editor-content" key={tab}>{pt(confirmDelete && <div className="v-delete-confirm"><span>{pt("移除 ")}{provider.name}{pt(" 及其演示模型？")}</span><button onClick={() => setConfirmDelete(false)}>{pt("取消")}</button><button onClick={onDelete}>{pt("确认移除")}</button></div>)}
    {pt(tab === '连接配置' && <div className="v-provider-form"><label>{pt("名称")}<input value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })}/></label><label>API Key<div className="v-key-editor"><input type={showKey ? 'text' : 'password'} value={draft.api_key || ''} onChange={e => setDraft({ ...draft, api_key: e.target.value })}/><button aria-label={pt("显示或隐藏 Provider 密钥")} onClick={() => setShowKey(!showKey)}>{pt(showKey ? <EyeOff size={14}/> : <Eye size={14}/>)}</button><button aria-label={pt("复制 Provider 密钥")} onClick={() => copy(draft.api_key || '')}><Copy size={14}/></button></div></label><label>Base URL<input value={draft.base_url} onChange={e => setDraft({ ...draft, base_url: e.target.value })}/></label><label>{pt("上游端点")}<select value={draft.upstream_format || 'auto'} onChange={e => setDraft({ ...draft, upstream_format: e.target.value as Provider['upstream_format'] })}><option value="auto">{pt("自动探测")}</option><option value="responses">Responses</option><option value="chat_completions">Chat Completions</option><option value="anthropic_messages">Anthropic Messages</option></select></label><button className="v-small-button" disabled={testing} onClick={() => { setTesting(true); window.setTimeout(() => { setTesting(false); notify(operationResult === 'success' ? '端点探测通过 · 演示结果：Responses / Chat Completions' : '端点探测失败 · 演示 HTTP 401，请检查凭据'); }, 450); }}>{pt(testing ? '正在探测…' : '探测端点')}</button><div className="v-dialog-hint">{pt("修改凭据或端点后，可先探测连接，再保存设置。")}</div></div>)}
    {pt(subscription && <div hidden={tab !== '订阅账户'}><SubscriptionPrototype notify={notify} onSignedIn={() => { if (!draft.models.length)
        setDraft(p => ({ ...p, models: [{ id: 'grok-4', enabled: true, context_window: 256000 }] })); }}/></div>)}
    {pt(tab === '账户与用量' && <><div className="v-setting-row"><span>{pt("Codex 官方账户")}<small>{pt(signedIn ? '已授权 · 演示账户' : '需要登录')}</small></span><span className={signedIn?'v-state-badge':'v-state-badge muted'}>{pt(signedIn?'已授权':'需要登录')}</span></div><div className="v-account-actions"><button className="v-small-button" onClick={() => notify('正式版将打开 Codex 应用；原型不启动外部进程')}>{pt("打开 Codex")}</button><button className="v-small-button" onClick={() => copy('codex login')}>{pt("复制登录命令")}</button><button className="v-small-button" onClick={() => notify('认证状态已刷新 · 演示账户')}>{pt("刷新认证")}</button><button className="v-small-button" onClick={() => notify('官方用量已刷新 · 演示数据')}><RefreshCw size={12}/>{pt("刷新用量")}</button></div><div className="v-original-controls v-quota-windows"><OfficialOpenAIUsageLimitBars busy={usageState === 'loading'} limits={signedIn && usageState === 'normal' ? prototypeLimits : []}/></div><div className="v-original-controls v-official-usage"><OfficialOpenAIUsagePanel busy={usageState === 'loading'} error={usageState === 'error' ? '演示查询失败，请刷新重试' : null} usageHidden={!signedIn} snapshot={{ start_time: 0, end_time: Math.floor(Date.now() / 1000), total_tokens: 8420000, input_tokens: 7000000, output_tokens: 1420000, input_cached_tokens: 2600000, num_model_requests: 1284, limits: prototypeLimits, buckets: usageState === 'empty' ? [] : Array.from({ length: 90 }, (_, i) => { const d = new Date(); d.setDate(d.getDate() - i); const t = Math.floor(d.getTime() / 1000); return { date: d.toISOString().slice(0, 10), start_time: t, end_time: t + 86400, total_tokens: 10000 + (i * 7531) % 120000, input_tokens: 9000, output_tokens: 1000, input_cached_tokens: 3000, num_model_requests: 12 }; }) }}/></div></>)}
    {pt(tab === '模型' && <div className="v-original-controls v-model-controls"><ModelSection models={draft.models} providerId={provider.id} disabled={official} interactionDisabled={official && !signedIn} officialDisabledModels={draft.models.filter(m => !m.enabled).map(m => m.id)} officialCollaborationBaselines={Object.fromEntries(draft.models.map(m => [m.id, 'v2' as const]))} officialCollaborationOverrides={baseline} onOfficialCollaborationVersionChange={(id, version) => { setBaseline(s => ({ ...s, [id]: version || 'v2' })); update(id, { multi_agent_version: version }); notify('协作版本已在演示草稿中更新'); }} onToggleOfficialModel={(id, enabled) => update(id, { enabled })} onRefresh={() => { setBusy(true); refreshTimer.current=window.setTimeout(() => { setBusy(false); notify('官方模型目录已刷新 · 演示操作'); }, 450); }} onCancelRefresh={() => { window.clearTimeout(refreshTimer.current);setBusy(false); notify('已取消刷新'); }} refreshBusy={busy} discoverBusy={busy} onAdd={() => { const id = `new-model-${draft.models.length + 1}`; setDraft(p => ({ ...p, models: [...p.models, { id, enabled: true, context_window: 128000, input_modalities: ['text'], thinking_mode: 'none' }] })); return id; }} onCancelNewModel={id => setDraft(p => ({ ...p, models: p.models.filter(m => m.id !== id) }))} onDiscover={() => { setBusy(true); window.setTimeout(() => { setBusy(false); if (operationResult === 'failure') {
        notify('模型发现失败 · 演示 HTTP 401，原模型列表保留');
        return;
    } const id = `discovered-model-${draft.models.length + 1}`; setDraft(p => ({ ...p, models: [...p.models, { id, enabled: true, context_window: 128000 }] })); notify('已发现 1 个演示模型'); }, 450); }} onReorder={models => setDraft({ ...draft, models })} onRemove={id => setDraft(p => ({ ...p, models: p.models.filter(m => m.id !== id) }))} onToggle={(id, enabled) => update(id, { enabled })} onUpdate={update} onTestModel={async () => { await new Promise(resolve => window.setTimeout(resolve, 400)); const ok = operationResult === 'success'; notify(ok ? '模型测试成功 · 演示 HTTP 200' : '模型测试失败 · 演示 HTTP 401，请检查凭据'); return ok; }}/></div>)}
    {pt(formError && <p className="v-error-text" role="alert">{pt(formError)}</p>)}
    </div><div className="v-editor-footer"><span>{pt(dirty ? '有未保存更改' : '设置与模型已同步')}</span><button className="v-primary small" disabled={!dirty || saveBusy} onClick={async () => { setFormError(''); if (!draft.name.trim()) {
        setFormError('请填写名称');
        return;
    } try {
        const url = new URL(draft.base_url);
        if (!['http:', 'https:'].includes(url.protocol))
            throw new Error();
    }
    catch {
        setFormError('请填写有效的 HTTP 或 HTTPS 服务地址');
        return;
    } setSaveBusy(true); await onSave(draft); setSaveBusy(false); }}><Save size={13}/>{pt("保存")}</button></div>
  </div>;
}
