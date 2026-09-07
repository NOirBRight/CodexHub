import { pt } from './PrototypeLocale';
// THROWAWAY: maintained catalog snapshot + local onboarding; never authenticates upstream.
import { useState } from 'react';
import { ArrowLeft, Check, Plus, Search } from 'lucide-react';
import type { Provider } from '../lib/types';
import { providerLogoSrc } from '../lib/providerLogos';
import catalog from './PrototypeCatalog.json';
import { providerFixtures } from './PrototypeData';
export const prototypeCatalog = catalog as Provider[];
export function prototypeProviderIcon(id: string) { return providerFixtures.find(p => p.id === id)?.icon || providerLogoSrc(id); }
export function ProviderAddPrototype({ existing, onAdd }: {
    existing: Provider[];
    onAdd: (provider: Provider) => Promise<boolean>;
}) {
    const [selected, setSelected] = useState<Provider | null>(null);
    const [query, setQuery] = useState('');
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [showKey, setShowKey] = useState(false);
    if (!selected)
        return <div className="v-catalog"><p>{pt("从内置目录选择，或添加兼容服务。")}</p><label className="v-catalog-search"><Search size={14}/><input aria-label={pt("搜索供应商目录")} placeholder={pt("搜索名称或服务地址")} value={query} onChange={e => setQuery(e.target.value)}/></label><div className="v-catalog-grid">{pt(prototypeCatalog.filter(p => (p.name + p.base_url).toLowerCase().includes(query.toLowerCase())).map(p => <button className="v-catalog-card" key={p.id} onClick={() => setSelected(structuredClone(p))}><span className="v-logo">{pt(prototypeProviderIcon(p.id) && <img src={prototypeProviderIcon(p.id)!} alt={p.name}/>)}</span><span><b>{p.name}</b><small>{pt(p.auth_capabilities?.includes('subscription:xai_oauth') ? '订阅授权 / API Key' : `${p.models.length} 个预设模型`)}</small></span>{pt(existing.some(x => x.id === p.id) && <small>{pt("已添加")}</small>)}</button>))}</div><button className="v-catalog-custom" onClick={() => setSelected({ id: 'custom', name: '', base_url: '', api_key: '', enabled: true, upstream_format: 'auto', models: [] })}><Plus size={16}/><span><b>{pt("自定义 Provider")}</b><small>{pt("服务地址、API Key 与模型自由配置")}</small></span></button></div>;
    return <form className="v-onboarding" onSubmit={async (e) => { e.preventDefault(); setError(''); if (!selected.name.trim()) {
        setError('请填写 Provider 名称');
        return;
    } try {
        const url = new URL(selected.base_url);
        if (!['http:', 'https:'].includes(url.protocol))
            throw new Error();
    }
    catch {
        setError('请填写有效的 HTTP 或 HTTPS 服务地址');
        return;
    } setBusy(true); const baseId = selected.id === 'custom' ? 'custom-' + crypto.randomUUID().slice(0, 8) : selected.id; const id = existing.some(p => p.id === baseId) ? baseId + '-' + crypto.randomUUID().slice(0, 4) : baseId; await onAdd({ ...selected, id, name: selected.name.trim(), api_key: selected.api_key || 'demo-not-a-real-key' }); setBusy(false); }}><button type="button" className="v-small-button" disabled={busy} onClick={() => setSelected(null)}><ArrowLeft size={12}/>{pt("返回目录")}</button><label>{pt("显示名称")}<input required value={selected.name} onChange={e => setSelected({ ...selected, name: e.target.value })}/></label><label>Base URL<input required value={selected.base_url} onChange={e => setSelected({ ...selected, base_url: e.target.value })}/></label><label>API Key<div className="v-key-editor"><input type={showKey ? 'text' : 'password'} value={selected.api_key || ''} placeholder={pt("仅使用演示凭据")} onChange={e => setSelected({ ...selected, api_key: e.target.value })}/><button type="button" onClick={() => setShowKey(!showKey)}>{pt(showKey ? '隐藏' : '显示')}</button></div></label><label>{pt("上游端点")}<select value={selected.upstream_format || 'auto'} onChange={e => setSelected({ ...selected, upstream_format: e.target.value as Provider['upstream_format'] })}><option value="auto">{pt("自动探测")}</option><option value="responses">Responses</option><option value="chat_completions">Chat Completions</option><option value="anthropic_messages">Anthropic Messages</option></select></label><div className="v-dialog-hint">{pt(selected.auth_capabilities?.includes('subscription:xai_oauth') ? '添加后可在「订阅账户」中使用设备授权，或继续使用 API Key。' : `将导入 ${selected.models.length} 个预设模型；添加后可发现、编辑和测试模型。`)}</div>{pt(error && <p role="alert" className="v-error-text">{pt(error)}</p>)}<button className="v-primary" disabled={busy}><Check size={14}/>{pt(busy ? '正在添加…' : '添加并配置')}</button></form>;
}
