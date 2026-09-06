import { PrototypeScenarioProvider, PrototypeTools, usePrototypeScenario } from './PrototypeTools';
import { useTranslation } from 'react-i18next';
import { pt } from './PrototypeLocale';
// THROWAWAY revision 3: compact desktop UI with real chart controls and simulated connection lifecycles.
import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowRight, ArrowUpRight, BarChart3, Check, CircleHelp, Copy, FileText, Layers, LayoutGrid, Link2, LoaderCircle, Minus, Moon, MoreHorizontal, Plus, Power, Radio, RefreshCw, Search, Settings2, ShieldCheck, Sun, Unplug, X } from 'lucide-react';
import { StackedUsageChartShell } from '../components/StackedUsageChartShell';
import type { Provider, UsageQueryWindow } from '../lib/types';
import i18n from '../i18n';
import { chartProviders, clientFixtures, codexIcon, makeEvents, providerFixtures } from './PrototypeData';
import './DesktopPrototype.css';
import './DesktopPrototype.finish.css';
import { SettingsPrototype } from './SettingsPrototype';
import { ProviderDetailPrototype, makePrototypeProvider } from './ProviderDetailPrototype';
import { normalizeSettings } from '../lib/settings';
import { ProviderAddPrototype, prototypeProviderIcon } from './ProviderAddPrototype';
import { ClientDetailPrototype, type PrototypeClientCondition } from './ClientDetailPrototype';
import './PrototypeFeatures.css';
import './DesktopPrototype.polish.css';
import brandIcon from './prototype-assets/codexhub-icon.svg';
export default function DesktopPrototype(){return <PrototypeScenarioProvider><DesktopPrototypeWorkspace/></PrototypeScenarioProvider>;}
function DesktopPrototypeWorkspace() {
    useTranslation();
    const [silent,setSilent]=useState(false);
    const [maximized,setMaximized]=useState(false);
    const [dark, setDark] = useState(new URLSearchParams(location.search).get('theme') !== 'light');
    const [page, setPage] = useState(new URLSearchParams(location.search).get('page') || '概览');
    const [gateway, setGateway] = useState(true);
    const [codex, setCodex] = useState(true);
    const [clients, setClients] = useState(Object.fromEntries(clientFixtures.map(c => [c.id, c.on])));
    const [providerEnabled, setProviderEnabled] = useState(Object.fromEntries(providerFixtures.map(p => [p.id, true])));
    const [busy, setBusy] = useState<string[]>([]);
    const [panel, setPanel] = useState('');
    const [providerDirty, setProviderDirty] = useState(false);
    const [closeRequested, setCloseRequested] = useState(false);
    const [toast, setToast] = useState('');
    const [query, setQuery] = useState('');
    const [providerOrder, setProviderOrder] = useState(providerFixtures.map(p => p.id));
    const [conditions, setConditions] = useState<Record<string, PrototypeClientCondition>>({});
    const [scenario, setScenario] = usePrototypeScenario('action');
    const [toastTone, setToastTone] = useState('success');
    const [retry, setRetry] = useState<null | (() => void)>(null);
    const [codexScenario, setCodexScenario] = usePrototypeScenario('codex');
    const [confirmCodex, setConfirmCodex] = useState(false);
    const [versionsChecked, setVersionsChecked] = useState(false);
    const [clientFilter, setClientFilter] = useState('全部');
    const [settings, setSettings] = useState(() => normalizeSettings({ locale: 'zh-CN', proxy_port: 18080, gateway_client_key: 'demo-key-not-a-real-credential' }));
    const [providerDrafts, setProviderDrafts] = useState(() => Object.fromEntries(providerFixtures.map(p => [p.id, makePrototypeProvider(p.id)])));
    const [removed, setRemoved] = useState<string[]>([]);
    const displayProviders = providerOrder.filter(id => !removed.includes(id)).map(id => {
        const p = providerDrafts[id];
        const fixture = providerFixtures.find(f => f.id === id);
        return { id, name: p.name, icon: prototypeProviderIcon(id) || prototypeProviderIcon(id.replace(/-[a-f0-9]{4}$/, '')) || '', kind: p.auth_capabilities?.includes('subscription:xai_oauth') ? '订阅 / API' : fixture?.kind || '自定义 API', value: fixture?.value || '—', unit: fixture?.unit || '', caption: fixture?.caption || '尚未查询额度', percent: fixture?.percent || 0, note: fixture?.note || '可在详情中配置与查询', models: p.models.length, model: p.models[0]?.id || '', color: fixture?.color || '#8170dd', base: p.base_url };
    });
    const activeProvider = panel.startsWith('provider:') ? providerDrafts[panel.slice(9)] : null;
    const panelTitle = activeProvider?.name || (panel === '演示场景' ? '原型工具' : panel);
    const [windowRange, setWindowRange] = useState<UsageQueryWindow | null>(null);
    const [refreshed, setRefreshed] = useState('刚刚');
    const timers = useRef<number[]>([]);
    const events = useMemo(makeEvents, []);
    const currentEvents = useMemo(() => events.filter(e => (!windowRange?.startTs || Date.parse(e.ts!) >= Date.parse(windowRange.startTs)) && (!windowRange?.endTs || Date.parse(e.ts!) <= Date.parse(windowRange.endTs))), [events, windowRange]);
    const summary = useMemo(() => ({ requests: currentEvents.length, successful_requests: currentEvents.filter(e => e.status === 200).length, missing_usage_requests: 0, cache_hit_rate: 38, input_tokens: currentEvents.reduce((s, e) => s + (e.input_tokens || 0), 0), cached_input_tokens: currentEvents.reduce((s, e) => s + (e.cached_input_tokens || 0), 0), output_tokens: currentEvents.reduce((s, e) => s + (e.output_tokens || 0), 0), total_tokens: currentEvents.reduce((s, e) => s + (e.total_tokens || 0), 0), estimated_cost_usd: currentEvents.reduce((s, e) => s + (e.total_tokens || 0) * .000002, 0), cost_label: '演示成本 · 按 Token 估算' }), [currentEvents]);
    const modelCount = displayProviders.reduce((n, p) => n + (!removed.includes(p.id) && providerEnabled[p.id] && (p.id !== 'openai' || settings.include_official_models) ? providerDrafts[p.id].models.filter(m => m.enabled).length : 0), 0);
    const clientConnected=(id:string)=>clients[id]&&(!conditions[id]||conditions[id]==='normal');
    const connectedCount = Object.keys(clients).filter(clientConnected).length;
    const activeProviders = displayProviders.filter(p => !removed.includes(p.id) && providerEnabled[p.id] && (p.id !== 'openai' || settings.include_official_models)).length;
    useEffect(() => { return () => timers.current.forEach(clearTimeout); }, []);
    useEffect(() => { void i18n.changeLanguage(settings.locale); }, [settings.locale]);
    useEffect(() => { const u = new URL(location.href); u.searchParams.set('theme', dark ? 'dark' : 'light'); u.searchParams.set('page', page); u.searchParams.delete('variant'); history.replaceState(null, '', u); }, [dark, page]);
    useEffect(() => { if (!panel)
        return; const key = (e: KeyboardEvent) => { if (e.key === 'Escape') {
        if (providerDirty)
            setCloseRequested(true);
        else
            setPanel('');
    } }; window.addEventListener('keydown', key); return () => window.removeEventListener('keydown', key); }, [panel, providerDirty]);
    function closePanel() { if (providerDirty)
        setCloseRequested(true);
    else
        setPanel(''); }
    function notify(s: string, tone?:string) { const nextTone=tone||(/^正在/.test(s)?'loading':/失败|错误/.test(s)?'error':'success');setToastTone(nextTone);setToast(s);if(nextTone==='success')timers.current.push(window.setTimeout(() => setToast(current => current === s ? '' : current), 5500)); }
    async function applyChange(key: string, done: () => void, message: string, forcedOutcome?: string): Promise<boolean> {
        if (busy.includes(key))
            return false;
        const outcome = forcedOutcome || scenario;
        setScenario('normal');
        setRetry(null);
        setBusy(b => [...b, key]);
        setToastTone('loading');
        setToast('正在处理…');
        await new Promise<void>(resolve => timers.current.push(window.setTimeout(resolve, 550)));
        setBusy(b => b.filter(x => x !== key));
        if (outcome === 'failure') {
            notify('操作失败 · 演示文件写入被拒绝，原配置保留');
            setRetry(() => () => { void applyChange(key, done, message, 'normal'); });
            return false;
        }
        done();
        notify(message + (outcome === 'partial' ? ' · 2 个客户端已同步，Pi 同步失败，可重试' : outcome === 'restart' ? ' · 配置已保存，Codex 重启失败，请手动打开 Codex' : ''),outcome==='partial'||outcome==='restart'?'warning':'success');
        if (outcome === 'partial' || outcome === 'restart')
            setToastTone('warning');
        if (outcome === 'partial')
            setRetry(() => () => { setRetry(null); notify('Pi 已重新同步 · 请重启 Pi'); });
        return true;
    }
    function action(key: string, done: () => void, message: string) { void applyChange(key, done, message); }
    function toggleGateway() { action('gateway', () => setGateway(!gateway), gateway ? 'Gateway 已停止 · 接入配置保留' : 'Gateway 已启动 · 无需重启客户端'); }
    function toggleCodex() { if (codexScenario !== 'normal') {
        setConfirmCodex(true);
        return;
    } action('codex', () => {setCodex(!codex);if(!codex)setGateway(true);}, codex ? 'Codex 已断开 CodexHub · 请重启 Codex' : 'Codex 已连接 CodexHub · 请重启 Codex'); }
    function toggleClient(id: string) { const client = clientFixtures.find(c => c.id === id)!; const repair = conditions[id] === 'drift' || conditions[id] === 'foreign'; action(id, () => { setClients(c => ({ ...c, [id]: repair || !c[id] })); setConditions(c => ({ ...c, [id]: 'normal' })); }, client.name + ' 连接配置已更新 · 请重启 ' + client.name); }
    function moveProvider(id: string, direction: number) { setProviderOrder(ids => { const next = [...ids], a = next.indexOf(id), b = a + direction; if (b < 0 || b >= next.length || next[b] === 'openai')
        return ids; [next[a], next[b]] = [next[b], next[a]]; return next; }); notify('Provider 顺序已保存 · 无需重启'); }
    async function addProvider(p: Provider) { return applyChange('add', () => { setProviderDrafts(d => ({ ...d, [p.id]: p })); setRemoved(ids => ids.filter(id => id !== p.id)); setProviderOrder(ids => ids.includes(p.id) ? ids : [...ids, p.id]); setProviderEnabled(d => ({ ...d, [p.id]: true })); setPanel('provider:' + p.id); }, 'Provider 已添加 · 可继续授权、发现和编辑模型'); }
    function Switch({ checked, label, pending, disabled, onClick }: {
        checked: boolean;
        label: string;
        pending?: boolean;
        disabled?: boolean;
        onClick: () => void;
    }) { return <button className={`v-switch ${checked ? 'on' : ''}`} role="switch" aria-checked={checked} aria-label={pt(label)} disabled={disabled||pending} onClick={onClick}><span>{pt(pending && <LoaderCircle size={9} className="v-spin"/>)}</span></button>; }
    function Logo({ src, name, large = false }: {
        src: string;
        name: string;
        large?: boolean;
    }) { return <span className={`v-logo ${large ? 'large' : ''}`}>{pt(src ? <img src={src} alt={pt(`${name} 图标`)}/> : <Layers size={18}/>)}</span>; }
    const modelStats = <div className="v-mini-stats">{pt([['今日请求', '2,848', '+12.8%'], ['Token 用量', '18.44M', '+8.2%'], ['成功率', '99.7%', '稳定'], ['首字延迟', '720', 'ms']].map(([l, n, d]) => <div key={l}><span>{pt(l)}</span><strong>{pt(n)}<small>{pt(d === 'ms' ? d : '')}</small></strong><em>{pt(d !== 'ms' ? d : '较昨日 −42 ms')}</em></div>))}</div>;
    const providerList = <section className="v-provider-table"><div className="v-section-header"><h2>{pt("资源余量 ")}<span>{pt(displayProviders.length)}{pt(" 个 Provider")}</span></h2><div><span className="v-subtle">{pt(refreshed)}{pt("更新")}</span><button className="v-icon-button" aria-label={pt("刷新额度")} onClick={() => action('refresh', () => setRefreshed(new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })), '演示额度已刷新')}><RefreshCw size={13} className={busy.includes('refresh') ? 'v-spin' : ''}/></button><button className="v-small-button" onClick={() => setPanel('添加 Provider')}><Plus size={12}/>{pt(" 添加")}</button></div></div><div className="v-table-labels"><span>Provider</span><span>{pt("剩余额度 / 余额")}</span><span>{pt("资源状态")}</span><span>{pt("启用")}</span></div>{pt(displayProviders.filter(p => !removed.includes(p.id) && (p.id !== 'openai' || settings.include_official_models) && providerDrafts[p.id].name.toLowerCase().includes(query.toLowerCase())).map(p => <div className={`v-provider-row ${!providerEnabled[p.id] ? 'muted' : ''}`} key={p.id}><button className="v-provider-identity" onClick={() => setPanel('provider:' + p.id)}><Logo src={p.icon} name={p.name}/><span><b>{providerDrafts[p.id].name}</b><small>{pt(p.kind)} <i>·</i> {pt(providerDrafts[p.id].models.length)}{pt(" 个模型")}</small></span></button><button className="v-balance" onClick={() => setPanel('provider:' + p.id)}><strong>{pt(p.unit !== '%' && <em>{pt(p.unit)}</em>)}{pt(p.value)}{pt(p.unit === '%' && <em>%</em>)}</strong><small>{pt(p.caption)}</small></button><div className={`v-quota ${p.id === 'kimi' && page !== '概览' ? 'warning' : ''}`}>{pt(p.percent ? <div className="v-track"><i style={{ width: p.percent + '%' }}/></div> : <span className="v-account-ok">{pt(p.value === '—' ? '未知余量' : <><Check size={11}/>{pt(" 余额正常")}</>)}</span>)}<small>{pt(p.id === 'kimi' && page === '概览' ? '10M Token 总额度' : p.note)}</small></div><div className="v-provider-order">{pt(page === 'Provider' && p.id !== 'openai' && <span><button aria-label={pt(`上移 ${p.name}`)} disabled={providerOrder.indexOf(p.id) <= 1} onClick={() => moveProvider(p.id, -1)}>↑</button><button aria-label={pt(`下移 ${p.name}`)} disabled={providerOrder.indexOf(p.id) === providerOrder.length - 1} onClick={() => moveProvider(p.id, 1)}>↓</button></span>)}<Switch label={pt(`启用 ${p.name}`)} checked={providerEnabled[p.id]} onClick={() => { setProviderEnabled(s => ({ ...s, [p.id]: !s[p.id] })); notify(`${p.name} 已${providerEnabled[p.id] ? '停用' : '启用'}`); }}/></div></div>))}</section>;
    const codexBridge = <section className="v-codex-bridge"><Logo src={codexIcon} name="Codex"/><div className="v-bridge-name"><b>Codex</b><small>{pt(codex ? '外部模型已接入' : '使用官方配置')}</small></div><div className={`v-bridge-line ${codex ? 'connected' : ''}`}><i /><Link2 size={13}/><i /></div><img className="v-app-symbol" src={brandIcon} alt={pt("CodexHub 图标")}/><div className="v-bridge-name"><b>CodexHub</b><small>{pt(activeProviders)}{pt(" 个 Provider · ")}{pt(modelCount)}{pt(" 个模型")}</small></div><div className="v-bridge-action"><span className={codex && gateway ? 'v-green' : 'v-subtle'}>{pt(busy.includes('codex') ? '正在更新…' : codex ? (gateway ? '已连接' : '已接入 · 服务离线') : '未连接')}</span><button className={`v-connect-button ${codex ? 'connected' : ''}`} onClick={toggleCodex} disabled={busy.includes('codex')}>{pt(busy.includes('codex') ? <LoaderCircle size={13} className="v-spin"/> : codex ? <Unplug size={13}/> : <Link2 size={13}/>)} {pt(codex ? '断开' : '连接')}</button></div></section>;
    const clientsGrid = <div className="v-client-grid">{pt(clientFixtures.filter(c => clientFilter === '全部' || (clientFilter === '已连接' ? clientConnected(c.id) : !clientConnected(c.id))).map(c => <section key={c.id} className={`v-client-card ${clientConnected(c.id) ? 'connected' : ''}`}><div className="v-client-heading"><Logo src={c.icon} name={c.name} large/><div><h2>{c.name}</h2><small>{pt(c.kind)}{pt(versionsChecked ? ' · v1.2.0 → 1.3.0' : '')}</small></div><button aria-label={pt(`${c.name} 详情`)} onClick={() => setPanel(c.name)} className="v-icon-button"><MoreHorizontal size={16}/></button></div><div className="v-client-config"><FileText size={12}/><code title={pt(c.path)}>{pt(c.path)}</code></div><div className="v-client-bottom"><div><span className={clientConnected(c.id) && gateway ? 'v-green' : 'v-subtle'}><i className={`v-dot ${!clientConnected(c.id) || !gateway ? 'off' : ''}`}/>{pt(conditions[c.id] === 'unavailable' ? '客户端不可用' : conditions[c.id] === 'drift' ? '配置需要修复' : conditions[c.id] === 'foreign' ? '其他通道管理' : busy.includes(c.id) ? '正在更新配置…' : clientConnected(c.id) ? (gateway ? '已连接' : '已接入 · 服务离线') : '未连接')}</span><small>{pt(clientConnected(c.id) ? `${modelCount} 个 Gateway 模型可见` : '保留客户端现有配置')}</small></div><Switch checked={clientConnected(c.id)} pending={busy.includes(c.id)} disabled={conditions[c.id] === 'unavailable'} label={pt(`${conditions[c.id]==='drift'?'修复':clientConnected(c.id)?'断开':'连接'} ${c.name}`)} onClick={() => conditions[c.id] === 'foreign' ? setPanel(c.name) : toggleClient(c.id)}/></div></section>))}<section className="v-client-manual"><span><Plus size={20}/></span><h2>{pt("其他客户端")}</h2><p>{pt("使用兼容 OpenAI 的连接信息手动接入")}</p><button onClick={() => setPanel('手动接入')}>{pt("查看连接信息 ")}<ArrowUpRight size={12}/></button></section></div>;
    const nav = [['概览', LayoutGrid], ['使用统计', BarChart3], ['Provider', Layers], ['客户端', Link2], ['设置', Settings2]] as const;
    return <div className={`v-stage ${dark ? 'v-dark' : 'v-light'}`}><div className={`v-window ${maximized?'v-maximized':''}`} hidden={silent}><header className="v-titlebar"><div className="v-brand"><img className="v-brand-svg" src={brandIcon} alt="CodexHub"/>CodexHub <small>0.2</small></div><span className="v-title-context">LOCAL MODEL WORKSPACE</span><div className="v-title-right"><button className="v-prototype-label" onClick={() => setPanel('演示场景')}>{pt("原型工具")}</button><button aria-label={pt("切换主题")} onClick={() => setDark(!dark)}>{pt(dark ? <Sun size={14}/> : <Moon size={14}/>)}</button><div className="v-window-controls"><button aria-label={pt("最小化窗口")} onClick={()=>setSilent(true)}><Minus size={12}/></button><button aria-label={pt("切换窗口大小")} onClick={()=>setMaximized(!maximized)}><span>□</span></button><button aria-label={pt("关闭到托盘")} onClick={()=>setSilent(true)}><X size={13}/></button></div></div></header>
    <div className="v-app"><div className="v-topnav"><nav>{pt(nav.map(([n, Icon]) => <button key={n} className={page === n ? 'selected' : ''} onClick={() => { setPage(n); setQuery(''); }}><Icon size={15}/>{pt(n)}</button>))}</nav><button aria-label={pt("帮助与反馈")} onClick={() => setPanel('关于原型')}><CircleHelp size={15}/></button></div>
    <div className="v-body"><div className="v-service-strip"><div><span className="v-service-icon"><Radio size={16}/></span><b>Gateway</b><span className={gateway ? 'v-green' : 'v-subtle'}><i className={`v-dot ${!gateway ? 'off' : ''}`}/>{pt(busy.includes('gateway') ? '切换中…' : gateway ? '运行中' : '已停止')}</span></div><code>127.0.0.1:{pt(settings.proxy_port)}</code><div className="v-service-actions"><button title={pt("演示复制端点")} aria-label={pt("复制端点")} onClick={() => { void navigator.clipboard.writeText(`http://127.0.0.1:${settings.proxy_port}/v1`).then(() => notify('端点已复制')).catch(() => notify(`演示端点：http://127.0.0.1:${settings.proxy_port}/v1`)); }}><Copy size={12}/></button><span>{pt(gateway ? '3 个活跃请求' : '无活跃请求')}</span><button className="v-power-button" aria-label={pt(gateway ? '停止 Gateway' : '启动 Gateway')} onClick={toggleGateway} disabled={busy.includes('gateway')}>{pt(busy.includes('gateway') ? <LoaderCircle size={12} className="v-spin"/> : <Power size={12}/>)} {pt(gateway ? '停止' : '启动')}</button><button aria-label={pt("重启 Gateway")} disabled={!gateway || busy.includes('gateway')} onClick={() => action('gateway', () => setGateway(true), 'Gateway 已重启 · 演示操作')}><RefreshCw size={12}/></button></div></div>
    <main className={`v-content ${page === '使用统计' ? 'analytics-page' : page === '设置' ? 'settings-page' : ''}`}><div className="v-page-heading"><div><h1>{pt(page === '概览' ? '工作空间' : page === 'Provider' ? 'Provider 管理' : page === '客户端' ? '客户端连接' : page === '设置' ? '偏好设置' : '使用统计')}</h1><p>{pt(page === '概览' ? '服务、模型与资源，一眼掌握。' : page === 'Provider' ? '管理模型来源与账户资源。' : page === '客户端' ? '将模型送到你工作的地方。' : page === '设置' ? '让工作空间按你的习惯运行。' : '从每一次请求，了解实际使用。')}</p></div><div>{pt(page === '客户端' ? <span className="v-heading-stat"><b>{pt(connectedCount)}</b>{pt(" / 5 已连接")}</span> : page === 'Provider' ? <button className="v-primary small" onClick={() => setPanel('添加 Provider')}><Plus size={13}/>{pt(" 添加 Provider")}</button> : <span className="v-page-date">{pt("本地工作空间 ")}<span>·</span> {pt(new Date().toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' }))}</span>)}</div></div>
    {pt(page === '概览' && <>{pt(codexBridge)}<div className="v-overview-metrics">{pt(modelStats)}<svg className="v-spark" viewBox="0 0 140 45" aria-label={pt("演示请求趋势")}><path d="M1 40 L12 35 L22 38 L32 27 L43 32 L54 17 L65 22 L76 10 L87 19 L98 14 L109 21 L121 7 L139 2" fill="none" stroke="currentColor" strokeWidth="2"/></svg></div>{pt(providerList)}<div className="v-overview-bottom"><span><Link2 size={12}/>{pt(connectedCount)}{pt(" 个外部客户端已连接")}</span><button onClick={() => setPage('客户端')}>{pt("管理连接 ")}<ArrowRight size={12}/></button><button onClick={() => setPage('使用统计')}>{pt("查看完整统计 ")}<ArrowUpRight size={12}/></button></div></>)}
    {pt(page === '客户端' && <>{pt(codexBridge)}<div className="v-client-section-head"><h2>{pt("外部客户端 ")}<span>5</span><button className="v-small-button" disabled={busy.includes('versions')} onClick={() => action('versions', () => setVersionsChecked(true), '客户端版本已检查 · 无需重启')}><RefreshCw size={12}/>{pt("检查版本")}</button></h2><div className="v-segment">{pt(['全部', '已连接', '未连接'].map(x => <button key={x} className={clientFilter === x ? 'active' : ''} onClick={() => setClientFilter(x)}>{pt(x)}</button>))}</div></div>{pt(clientsGrid)}<div className="v-client-note"><ShieldCheck size={13}/><span>{pt("连接时添加 CodexHub 的 Provider 配置；断开时仅移除其管理的配置。")}</span></div></>)}
    {pt(page === 'Provider' && <><div className="v-provider-tools"><label><Search size={14}/><input aria-label={pt("搜索 Provider")} placeholder={pt("搜索 Provider")} value={query} onChange={e => setQuery(e.target.value)}/></label><span>{pt(activeProviders)}{pt(" 个已启用 · ")}{pt(modelCount)}{pt(" 个可用模型")}</span></div>{pt(providerList)}<section className="v-provider-tip"><img className="v-app-symbol" src={brandIcon} alt={pt("CodexHub 图标")}/><div><h2>{pt("一个模型库，连接所有工作工具")}</h2><p>{pt("启用的 Provider 模型可供 Codex 和已接入的外部客户端使用。")}</p></div><button onClick={() => setPage('客户端')}><ArrowUpRight size={16}/></button></section></>)}
    {pt(page === '使用统计' && <><div className="v-analytics"><StackedUsageChartShell events={events} providers={chartProviders} summary={summary} onWindowChange={setWindowRange} pendingMessage={pt("所选范围内没有演示请求")}/></div><div className="v-analytics-note"><span>{pt("点击图例筛选系列 · 悬浮查看明细")}</span><span>{pt("演示数据覆盖最近 62 天")}</span></div></>)}
    <div className="v-settings-host" hidden={page !== '设置'}><SettingsPrototype value={settings} onSave={async (next) => applyChange('settings', () => setSettings(next), (['proxy_port', 'gateway_client_key', 'gateway_request_timeout_seconds', 'gateway_auto_retry_enabled', 'gateway_auto_retry_max_attempts', 'gateway_image_proxy_enabled', 'gateway_image_proxy_model', 'openai_context_guard_enabled'] as const).some(k => settings[k] !== next[k]) && gateway ? '设置已保存 · 已模拟重启 Gateway' : next.unified_codex_history !== settings.unified_codex_history ? '历史同步设置已保存 · 请重启 Codex' : '设置已保存 · 无需重启')} providers={Object.values(providerDrafts).filter(p => !removed.includes(p.id) && providerEnabled[p.id])} dark={dark} onTheme={() => setDark(!dark)} notify={notify} running={gateway}/></div>
    </main><footer className="v-statusbar"><span><i className={`v-dot ${!gateway ? 'off' : ''}`}/>{pt(gateway ? 'Gateway 在线' : 'Gateway 离线')}<i className="v-divider"/>Codex {pt(codex ? '已接入' : '未连接')}</span><span><ShieldCheck size={11}/>{pt("所有操作仅用于原型演示")}</span></footer></div></div></div>
    {silent&&<div className="v-silent-preview"><img src={brandIcon} alt="CodexHub"/><h2>{pt("静默运行")}</h2><p>{pt(gateway?"Gateway 继续提供服务，连接配置保留。":"Gateway 已停止，连接配置保留。")}</p><button className="v-primary" onClick={()=>setSilent(false)}>{pt("打开 CodexHub")}</button><small>{pt("托盘行为演示 · 不关闭浏览器")}</small></div>}
    {pt(toast && <div className={`v-toast ${toastTone}`} role={toastTone === 'error' ? 'alert' : 'status'}>{pt(toastTone === 'loading' ? <LoaderCircle size={14} className="v-spin"/> : toastTone === 'error' ? <X size={14}/> : <Check size={14}/>)}<span>{pt(toast)}</span>{pt(retry && <button onClick={retry}>{pt("重试")}</button>)}<button aria-label={pt("关闭反馈")} onClick={() => setToast('')}><X size={12}/></button></div>)}
    {pt(panel && <div className="v-overlay" onClick={closePanel}><section className={`v-dialog ${activeProvider ? 'v-dialog-provider' : panel === '添加 Provider' ? 'v-dialog-catalog' : panel === '演示场景' ? 'v-dialog-tools' : ''}`} role="dialog" aria-modal="true" aria-label={panelTitle} onClick={e => e.stopPropagation()}><div className="v-dialog-heading"><img className="v-app-symbol" src={activeProvider?prototypeProviderIcon(activeProvider.id)||brandIcon:clientFixtures.find(c=>c.name===panel)?.icon||brandIcon} alt={panelTitle}/><div><h2>{panelTitle}</h2><small>{pt("CodexHub 工作空间")}</small></div><button aria-label={pt("关闭")} onClick={closePanel}><X size={16}/></button></div>
    <div className={activeProvider?'v-dialog-body v-dialog-body-provider':'v-dialog-body'}>{pt(panel === '添加 Provider' ? <ProviderAddPrototype existing={Object.values(providerDrafts).filter(p => !removed.includes(p.id))} onAdd={addProvider}/> : panel === '手动接入' ? <><p>{pt("在兼容 OpenAI 的客户端填写以下信息。")}</p><label>Base URL<input readOnly value={`http://127.0.0.1:${settings.proxy_port}/v1`}/></label><label>API Key<input readOnly value={settings.gateway_client_key}/></label><div className="v-dialog-hint">{pt("演示凭据，仅展示填写方式。")}</div></> : activeProvider ? <ProviderDetailPrototype onDirty={setProviderDirty} key={activeProvider.id} provider={activeProvider} onSave={async (next) => applyChange('provider-save', () => setProviderDrafts(d => ({ ...d, [next.id]: next })), 'Provider 与模型已保存 · 请重启 Codex；已连接客户端将在下次启动时读取目录')} onDelete={() => action('provider-delete', () => { setRemoved(ids => [...ids, activeProvider.id]); setProviderDirty(false); setPanel(''); }, 'Provider 已移除 · 请重启已连接客户端')} notify={notify}/> : clientFixtures.some(c => c.name === panel) ? (() => { const c = clientFixtures.find(c => c.name === panel)!; return <ClientDetailPrototype id={c.id} condition={conditions[c.id] || 'normal'} connected={clients[c.id]} onToggle={() => toggleClient(c.id)} pending={busy.includes(c.id)} port={settings.proxy_port} notify={notify}/>; })() : panel === '演示场景' ? <PrototypeTools conditions={conditions} onCondition={(id,value)=>setConditions(s=>({...s,[id]:value}))} onClose={()=>setPanel('')}/> : <><p>{pt("紧凑桌面界面交互原型。Gateway 运行、Codex 接入和外部客户端连接分别管理。")}</p><div className="v-dialog-hint">{pt("统计直接使用项目现有组件，数据为内存生成的演示请求。")}</div></>)}
    </div></section></div>)}
    {pt(confirmCodex && <div className="v-overlay v-close-overlay"><section className="v-dialog" role="dialog" aria-modal="true" aria-label={pt("Codex 连接确认")}><h2>{pt(codexScenario === 'foreign' ? '接管 Codex 配置' : '切换 Codex 连接')}</h2><p>{pt(codexScenario === 'foreign' ? '当前配置由 Beta 通道管理。继续会切换至当前 CodexHub 工作空间。' : codexScenario === 'unsupported' ? '当前平台无法自动重启。配置更新后请手动重启 Codex。' : 'Codex 正在运行。切换模型来源需要关闭并重启 Codex，请先结束当前任务。')}</p><div className="v-account-actions"><button className="v-small-button" onClick={() => setConfirmCodex(false)}>{pt("取消")}</button><button className="v-primary small" onClick={() => { setConfirmCodex(false); action('codex', () => { setCodex(!codex); setCodexScenario('normal'); }, codexScenario === 'unsupported' ? '配置已更新 · 请手动重启 Codex' : '配置已更新 · 已模拟重启 Codex'); }}>{pt(codexScenario === 'foreign' ? '接管并切换' : codexScenario === 'unsupported' ? '保存连接配置' : '重启 Codex 并继续')}</button></div></section></div>)}
    {pt(closeRequested && <div className="v-overlay v-close-overlay"><section className="v-dialog" role="dialog" aria-modal="true" aria-label={pt("未保存的 Provider 更改")}><h2>{pt("有未保存的更改")}</h2><p>{pt("Provider 与模型设置尚未保存。")}</p><div className="v-account-actions"><button className="v-small-button" onClick={() => setCloseRequested(false)}>{pt("继续编辑")}</button><button className="v-small-button" onClick={() => { setCloseRequested(false); setProviderDirty(false); setPanel(''); }}>{pt("放弃更改并关闭")}</button></div></section></div>)}
  </div>;
}
