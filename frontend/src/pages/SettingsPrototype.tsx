import { pt } from './PrototypeLocale';
// THROWAWAY: every user-facing field from SettingsDrawer + GatewayPage network settings.
import { useState, type ReactNode } from 'react';
import { Check, Copy, Eye, EyeOff, RefreshCw, Save } from 'lucide-react';
import { ContextGuardStatusPrototype, DiagnosticsPrototype, HistoryPrototype, UpdatePrototype } from './SettingsExtrasPrototype';
import type { Provider, Settings } from '../lib/types';
import brandIcon from './prototype-assets/codexhub-icon.svg';
export function SettingsPrototype({ value, onSave, dark, onTheme, notify, running, providers }: {
    value: Settings;
    onSave: (s: Settings) => Promise<boolean>;
    providers: Provider[];
    dark: boolean;
    onTheme: () => void;
    notify: (s: string) => void;
    running: boolean;
}) {
    const [draft, setDraft] = useState(value);
    const [tab, setTab] = useState('通用');
    const [visible, setVisible] = useState(false);
    const [saving, setSaving] = useState(false);
    const [confirmSave, setConfirmSave] = useState(false);
    const [saveError, setSaveError] = useState('');
    const dirty = JSON.stringify(draft) !== JSON.stringify(value);
    function field<K extends keyof Settings>(key: K, v: Settings[K]) { setDraft(s => ({ ...s, [key]: v })); }
    function Toggle({ name, label, description }: {
        name: keyof Settings;
        label: string;
        description: string;
    }) { return <Row title={pt(label)} description={pt(description)}><button className={`v-switch ${draft[name] ? 'on' : ''}`} role="switch" aria-checked={Boolean(draft[name])} aria-label={pt(label)} onClick={() => field(name, !draft[name])}><span /></button></Row>; }
    function copy(s: string) { void navigator.clipboard.writeText(s).then(() => notify('已复制')).catch(() => notify('复制失败，请手动选择文本')); }
    const runtimeChanged = ['gateway_client_key', 'proxy_port', 'gateway_request_timeout_seconds', 'gateway_auto_retry_enabled', 'gateway_auto_retry_max_attempts', 'gateway_image_proxy_enabled', 'gateway_image_proxy_model', 'openai_context_guard_enabled'].some(k => draft[k as keyof Settings] !== value[k as keyof Settings]);
    async function save() { setSaving(true); setConfirmSave(false); await onSave(draft); setSaving(false); }
    return <div className="v-settings"><div className="v-settings-nav">{pt(['通用', 'Codex 与客户端', 'Gateway', '请求策略', '诊断', '关于'].map(t => <button key={t} className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>{pt(t)}</button>))}</div><div className="v-settings-body">
    <div hidden={tab !== '通用'}><><Section title={pt("外观与语言")} caption={pt("让工作空间适合你的使用习惯。")}><Row title={pt("外观")} description={pt("浅色与深色共享布局和功能。")}><div className="v-segment"><button className={!dark ? 'active' : ''} onClick={() => dark && onTheme()}>{pt("浅色")}</button><button className={dark ? 'active' : ''} onClick={() => !dark && onTheme()}>{pt("深色")}</button></div></Row><Row title={pt("语言")} description={pt("保存后切换界面语言。")}><select aria-label={pt("语言")} value={draft.locale} onChange={e => field('locale', e.target.value as Settings['locale'])}><option value="zh-CN">{pt("简体中文")}</option><option value="en-US">English</option></select></Row></Section><Section title={pt("启动行为")} caption={pt("软件自启动与 Gateway 自启动分别管理。")}><Toggle name="auto_start_software" label={pt("开机自启动软件")} description={pt("登录系统后启动 CodexHub。")}/><Toggle name="auto_start_gateway" label={pt("打开软件后启动 Gateway")} description={pt("启动软件时同时启动本地模型服务。")}/></Section></></div>
    <div hidden={tab !== 'Codex 与客户端'}><><Section title={pt("模型与连接")} caption={pt("影响 Codex 模型列表以及已接入的客户端。")}><Toggle name="include_official_models" label={pt("包含 OpenAI 官方模型")} description={pt("在共享模型目录中包含官方订阅模型。")}/><Toggle name="auto_sync_clients" label={pt("自动同步已绑定客户端")} description={pt("模型目录更新后，同步已连接客户端的受管配置。")}/><Toggle name="openai_context_guard_enabled" label={pt("OpenAI 上下文保护")} description={pt("统一管理官方模型的上下文策略。")}/><ContextGuardStatusPrototype/></Section><Section title={pt("历史对话")} caption={pt("在模型来源切换时保留完整的对话记录。")}><Toggle name="unified_codex_history" label={pt("历史对话同步")} description={pt("在支持的 Codex 配置之间统一显示历史对话。")}/><HistoryPrototype notify={notify}/></Section></></div>
    <div hidden={tab !== 'Gateway'}><><Section title={pt("本地服务")} caption={pt("Gateway 向 Codex 与外部客户端提供同一个服务入口。")}><Row title={pt("绑定地址")} description={pt("当前版本仅接受本机 127.0.0.1。")}><input aria-label={pt("绑定地址")} value={draft.gateway_bind_address} readOnly/></Row><Row title={pt("端口")} description={pt("1024–65535；运行中修改后需要重启 Gateway。")}><input aria-label={pt("端口")} type="number" min={1024} max={65535} value={draft.proxy_port} onChange={e => field('proxy_port', Number(e.target.value))}/></Row><Row title={pt("请求超时")} description={pt("单位：秒。范围 5–600。")}><input aria-label={pt("请求超时")} type="number" min={5} max={600} value={draft.gateway_request_timeout_seconds} onChange={e => field('gateway_request_timeout_seconds', Number(e.target.value))}/></Row></Section><Section title={pt("客户端访问密钥")} caption={pt("这是本地客户端密钥，与 Provider 上游 API Key 分开管理。")}><div className="v-key-editor"><input aria-label={pt("本地客户端密钥")} type={visible ? 'text' : 'password'} value={draft.gateway_client_key} onChange={e => field('gateway_client_key', e.target.value)}/><button aria-label={pt(visible ? '隐藏密钥' : '显示密钥')} onClick={() => setVisible(!visible)}>{pt(visible ? <EyeOff size={14}/> : <Eye size={14}/>)}</button><button aria-label={pt("复制密钥")} onClick={() => copy(draft.gateway_client_key)}><Copy size={14}/></button><button aria-label={pt("重新生成密钥")} onClick={() => field('gateway_client_key', 'demo-' + crypto.randomUUID().slice(0, 12))}><RefreshCw size={14}/></button></div></Section><Section title={pt("连接端点")} caption={pt("按已保存配置生成，可直接复制。")}>{pt([['Models', '/v1/models'], ['Responses', '/v1/responses'], ['Chat Completions', '/v1/chat/completions']].map(([label, path]) => <Row key={label} title={pt(label)} description={pt(`http://127.0.0.1:${value.proxy_port}${path}`)}><button className="v-small-button" onClick={() => copy(`http://127.0.0.1:${value.proxy_port}${path}`)}><Copy size={12}/>{pt("复制")}</button></Row>))}</Section></></div>
    <div hidden={tab !== '请求策略'}><><Section title={pt("自动重试")} caption={pt("统一管理上游请求的重试行为。")}><Toggle name="gateway_auto_retry_enabled" label={pt("自动重试")} description={pt("上游请求失败时按重试策略处理。")}/><Row title={pt("最大重试次数")} description={pt("范围 1–30。")}><input aria-label={pt("最大重试次数")} type="number" min={1} max={30} disabled={!draft.gateway_auto_retry_enabled} value={draft.gateway_auto_retry_max_attempts} onChange={e => field('gateway_auto_retry_max_attempts', Number(e.target.value))}/></Row></Section><Section title={pt("图片代理 · Vision Proxy")} caption={pt("用支持视觉的模型，为非视觉目标模型生成图片上下文。")}><Toggle name="gateway_image_proxy_enabled" label={pt("图片代理")} description={pt("开启后选择用于理解图片的视觉模型。")}/><Row title={pt("视觉模型")} description={pt("仅列出具备图像输入能力的模型。")}><select aria-label={pt("视觉模型")} disabled={!draft.gateway_image_proxy_enabled} value={draft.gateway_image_proxy_model} onChange={e => field('gateway_image_proxy_model', e.target.value)}><option value="">{pt("选择模型")}</option>{pt(providers.flatMap(p => p.models.filter(m => m.enabled && m.input_modalities?.includes('image')).map(m => <option key={p.id + '/' + m.id} value={p.id + '/' + m.id}>{pt(m.display_name || m.id)} · {p.name}</option>)))}</select></Row></Section></></div>
    <div hidden={tab !== '诊断'}><DiagnosticsPrototype running={running} notify={notify} retryEnabled={draft.gateway_auto_retry_enabled} onRetry={() => field('gateway_auto_retry_enabled', !draft.gateway_auto_retry_enabled)}/></div>
    <div hidden={tab !== '关于'}><><div className="v-about-brand"><img src={brandIcon} alt={pt("CodexHub 应用图标")}/><div><h2>CodexHub <span>0.2.0</span></h2><p>{pt("一个模型中枢，连接你的工作工具。")}</p></div></div><UpdatePrototype notify={notify}/><div className="v-icon-study"><img src={brandIcon} width="16" height="16" alt={pt("16px 图标")}/><img src={brandIcon} width="24" height="24" alt={pt("24px 图标")}/><img src={brandIcon} width="32" height="32" alt={pt("32px 图标")}/><img src={brandIcon} width="48" height="48" alt={pt("48px 图标")}/><span>{pt("双端口 · 单桥接")}<br />{pt("可缩放的 SVG 标识")}</span></div></></div>
  </div>{pt(saveError && <p role="alert" className="v-error-text">{pt(saveError)}</p>)}{pt(confirmSave && <div className="v-inline-confirm"><h3>{pt("应用设置")}</h3><p>{pt(runtimeChanged && running ? '这些设置需要重启 Gateway，当前请求将中断。确认后将模拟保存并重启 Gateway。' : '历史对话设置变更将检查目录归属；应用后请重启 Codex。')}</p><div className="v-account-actions"><button className="v-small-button" onClick={() => setConfirmSave(false)}>{pt("继续编辑")}</button><button className="v-primary small" disabled={saving} onClick={() => void save()}>{pt("保存并应用")}</button></div></div>)}<div className="v-settings-save"><span>{pt(dirty ? '有未保存的更改 · 切换页面保留草稿' : '所有设置已保存于演示会话')}</span><div><button className="v-small-button" disabled={!dirty} onClick={() => setDraft(value)}>{pt("放弃更改")}</button><button className="v-primary small" disabled={!dirty || saving} onClick={() => { setSaveError(''); if (!Number.isInteger(draft.proxy_port) || draft.proxy_port < 1024 || draft.proxy_port > 65535 || !Number.isInteger(draft.gateway_request_timeout_seconds) || draft.gateway_request_timeout_seconds < 5 || draft.gateway_request_timeout_seconds > 600 || !Number.isInteger(draft.gateway_auto_retry_max_attempts) || draft.gateway_auto_retry_max_attempts < 1 || draft.gateway_auto_retry_max_attempts > 30) {
        setSaveError('请检查端口、超时与重试次数的整数范围');
        return;
    } if (draft.gateway_image_proxy_enabled && !draft.gateway_image_proxy_model) {
        setSaveError('请选择视觉模型');
        return;
    } if (runtimeChanged && running || draft.unified_codex_history !== value.unified_codex_history) {
        setConfirmSave(true);
        return;
    } void save(); }}>{pt(dirty ? <Save size={13}/> : <Check size={13}/>)}{pt("保存设置")}</button></div></div></div>;
}
function Section({ title, caption, children }: {
    title: string;
    caption: string;
    children: ReactNode;
}) { return <section className="v-settings-section"><div className="v-settings-section-heading"><h2>{pt(title)}</h2><p>{pt(caption)}</p></div><div>{pt(children)}</div></section>; }
function Row({ title, description, children }: {
    title: string;
    description: string;
    children: ReactNode;
}) { return <div className="v-settings-row"><div><b>{pt(title)}</b><small>{pt(description)}</small></div><div>{pt(children)}</div></div>; }
