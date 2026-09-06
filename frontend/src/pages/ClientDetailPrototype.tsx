import { pt } from './PrototypeLocale';
// THROWAWAY: client detection, version and routing states from GatewayClientCard.
import { useState } from 'react';
import { Copy, RefreshCw } from 'lucide-react';
import { clientFixtures } from './PrototypeData';
export type PrototypeClientCondition = 'normal' | 'drift' | 'unavailable' | 'foreign';
export function ClientDetailPrototype({ id, connected, condition, onCondition, onToggle, pending, port, notify }: {
    id: string;
    connected: boolean;
    condition: PrototypeClientCondition;
    onCondition: (s: PrototypeClientCondition) => void;
    onToggle: () => void;
    pending: boolean;
    port: number;
    notify: (s: string) => void;
}) {
    const client = clientFixtures.find(c => c.id === id)!;
    const [version, setVersion] = useState('idle');
    const [preview, setPreview] = useState(false);
    const [confirm, setConfirm] = useState(false);
    return <div className="v-client-detail"><div className="v-provider-detail"><span className="v-logo large"><img src={client.icon} alt={client.name}/></span><div><h2>{client.name}</h2><small>{pt(client.kind)}</small></div></div><div className="v-detail-row"><span>{pt("检测状态")}</span><b>{pt(condition === 'unavailable' ? '未安装 / 路径不可用' : '已检测到客户端')}</b></div><label>{pt("配置位置")}<input readOnly value={client.path}/></label><div className="v-detail-row"><span>{pt("当前版本")}</span><code>{pt(condition === 'unavailable' ? '—' : 'v1.2.0 · 演示')}</code></div><div className="v-detail-row"><span>{pt("最新版本")}</span><span>{pt(version === 'done' ? 'v1.3.0 · 可更新' : version === 'checking' ? '检查中…' : '尚未检查')}</span><button className="v-small-button" disabled={version === 'checking' || condition === 'unavailable'} onClick={() => { setVersion('checking'); window.setTimeout(() => { setVersion('done'); notify(`${client.name} 版本已检查 · 无需重启`); }, 500); }}><RefreshCw size={12}/>{pt("检查版本")}</button></div><div className="v-detail-row"><span>{pt("接入配置")}</span><b>{pt(condition === 'drift' ? '配置已偏离，需要修复' : condition === 'foreign' ? '由另一个 CodexHub 通道管理' : connected ? '已连接 Gateway' : '使用客户端原有配置')}</b></div>{pt(condition === 'drift' && <p className="v-dialog-hint">{pt("检测到受管模型或端点发生变化。修复将重新生成 CodexHub 管理的配置。")}</p>)}{pt(condition === 'foreign' && <p className="v-dialog-hint">{pt("当前配置归属 Beta 通道。接管后将使用此工作空间的模型与 Gateway。")}</p>)}<div className="v-account-actions"><button className="v-small-button" onClick={() => setPreview(!preview)}>{pt(preview ? '收起配置预览' : '预览接入配置')}</button><button className="v-small-button" onClick={() => { void navigator.clipboard.writeText(JSON.stringify({ base_url: `http://127.0.0.1:${port}/v1`, configuration_path: client.path, managed_by: 'CodexHub' }, null, 2)).then(() => notify('配置预览已复制')).catch(() => notify('复制失败')); }}><Copy size={12}/>{pt("复制")}</button></div>{pt(preview && <pre className="v-config-preview">{pt(JSON.stringify({ action: connected ? 'remove_managed_provider' : 'merge_managed_provider', provider: 'CodexHub', base_url: `http://127.0.0.1:${port}/v1`, preserve_other_providers: true }, null, 2))}</pre>)}<button className="v-primary" disabled={pending || condition === 'unavailable'} onClick={() => condition === 'foreign' ? setConfirm(true) : onToggle()}>{pt(pending ? '正在更新…' : condition === 'unavailable' ? '安装后可连接' : condition === 'foreign' ? '接管并连接' : condition === 'drift' ? '修复连接' : connected ? '断开连接' : '连接 Gateway')}</button>{pt(confirm && <div className="v-delete-confirm"><span>{pt("确认将此客户端从 Beta 通道切换到当前工作空间？")}</span><button onClick={() => setConfirm(false)}>{pt("取消")}</button><button onClick={() => { setConfirm(false); onToggle(); }}>{pt("接管连接")}</button></div>)}<details className="v-scenario-details"><summary>{pt("原型状态预览")}</summary><label>{pt("客户端场景")}<select value={condition} onChange={e => onCondition(e.target.value as PrototypeClientCondition)}><option value="normal">{pt("正常")}</option><option value="drift">{pt("配置偏离")}</option><option value="unavailable">{pt("未安装 / 不可用")}</option><option value="foreign">{pt("其他通道管理")}</option></select></label></details></div>;
}
