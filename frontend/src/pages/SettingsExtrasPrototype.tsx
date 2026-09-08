import { usePrototypeScenario } from './PrototypeTools';
import { pt } from './PrototypeLocale';
// THROWAWAY: full feedback surfaces for diagnostics, history and updater lifecycles.
import { useState } from 'react';
import { RefreshCw } from 'lucide-react';
export function HistoryPrototype({ notify }: {
    notify: (s: string) => void;
}) {
    const [scenario]=usePrototypeScenario('history');
    const [busy, setBusy] = useState(false), [confirmation, setConfirmation] = useState(false), [report, setReport] = useState('');
    function sync() { setBusy(true); notify('正在检查历史对话…'); window.setTimeout(() => { setBusy(false); const result = scenario === 'locked' ? '历史目录被 Codex 占用。请退出 Codex 后重试；原历史保留。' : scenario === 'conflict' ? '检测到其他通道的历史目录。尚未迁移；可取消或明确选择当前工作空间。' : scenario === 'failure' ? '同步失败 · 演示磁盘写入失败，原历史保留。' : '已同步 128 个会话 · 请重启 Codex 以刷新历史列表'; setReport(result); notify(result); }, 500); }
    return <><div className="v-settings-row"><div><b>{pt("立即同步历史对话")}</b><small>{pt("检查占用、历史归属与迁移结果。")}</small></div><button className="v-small-button" disabled={busy} onClick={sync}><RefreshCw size={12}/>{pt(busy ? '正在同步…' : '立即同步')}</button></div>{pt(report && <div className="v-inline-confirm"><p>{pt(report)}</p>{pt(scenario === 'conflict' && <button className="v-small-button" onClick={() => setConfirmation(true)}>{pt("查看迁移确认")}</button>)}{pt(scenario === 'locked' && <button className="v-small-button" onClick={() => { setReport('已延期 · 退出 Codex 后再次同步'); notify('历史同步已延期 · 请退出 Codex 后再次同步'); }}>{pt("稍后同步")}</button>)}</div>)}{pt(confirmation && <div className="v-inline-confirm"><h3>{pt("迁移至当前工作空间")}</h3><p>{pt("将演示历史合并到当前工作空间。原目录保留恢复入口。")}</p><div className="v-account-actions"><button className="v-small-button" onClick={() => setConfirmation(false)}>{pt("取消")}</button><button className="v-primary small" onClick={() => { setConfirmation(false); setReport('迁移已完成 · 请重启 Codex'); notify('历史迁移已完成 · 请重启 Codex'); }}>{pt("确认迁移")}</button></div></div>)}</>;
}
export function DiagnosticsPrototype({ running, notify, retryEnabled, onRetry }: {
    running: boolean;
    notify: (s: string) => void;
    retryEnabled: boolean;
    onRetry: () => void;
}) {
    const [paused, setPaused] = useState(false), [incidents, setIncidents] = useState(['demo-0906-001']), [detail, setDetail] = useState(false), [busy, setBusy] = useState(false);
    const [scenario,setScenario]=usePrototypeScenario('diagnostics');
    const ready = running && scenario !== 'release' && scenario !== 'offline' && scenario !== 'loading' && !busy;
    function action(done: () => void, message: string) { setBusy(true); notify('正在更新诊断…'); window.setTimeout(() => { setBusy(false); if (scenario === 'failure') {
        notify('诊断操作失败 · 原记录保留');
        return;
    } done(); notify(message + ' · 无需重启 Gateway'); }, 400); }
    return <><section className="v-settings-section"><div className="v-settings-section-heading"><h2>{pt("调试诊断")}</h2><p>{pt("调试版提供滚动记录与问题快照。")}</p></div><div>{pt(scenario === 'release' ? <div className="v-empty">{pt("正式构建隐藏调试诊断入口；此处预览隐藏状态。")}</div> : <>{pt(!running || scenario === 'offline' ? <p className="v-dialog-hint">{pt("Gateway 未运行，启动后可使用诊断。")}</p> : scenario === 'loading' ? <p className="v-dialog-hint">{pt("正在读取诊断状态…")}</p> : <><div className="v-diagnostic-metrics"><div><b>24 h</b><small>{pt("滚动窗口")}</small></div><div><b>2.4 MB</b><small>{pt("记录大小")}</small></div><div><b>{pt(incidents.length)}</b><small>{pt("问题快照")}</small></div></div>{pt(scenario === 'lag' && <p className="v-dialog-hint">{pt("诊断状态更新延迟 · 显示上一次快照，可刷新重试。")}</p>)}</>)}<div className="v-account-actions"><button className="v-small-button" disabled={!ready || paused} onClick={() => action(() => setIncidents(s => [...s, `demo-${Date.now().toString().slice(-6)}`]), '已标记问题')}>{pt("标记问题")}</button><button className="v-small-button" disabled={!ready} onClick={() => action(() => setPaused(!paused), paused ? '诊断记录已恢复' : '诊断记录已暂停')}>{pt(paused ? '恢复记录' : '暂停记录')}</button><button className="v-small-button" disabled={!running || busy} onClick={() => action(() => setScenario('normal'), '诊断状态已刷新')}><RefreshCw size={12}/>{pt("刷新")}</button></div>{pt(incidents.map(id => <div className="v-settings-row" key={id}><div><b>{pt(id)}</b><small>{pt("演示问题快照")}</small></div><button className="v-small-button" disabled={!ready} onClick={() => action(() => setIncidents(s => s.filter(x => x !== id)), '问题记录已删除')}>{pt("删除")}</button></div>))}</>)}</div></section><section className="v-settings-section"><div className="v-settings-section-heading"><h2>{pt("请求恢复活动")}</h2><p>{pt("最近请求的重试与恢复过程。")}</p></div><div><div className="v-settings-row"><div><b>{pt("自动重试")}</b><small>{pt(retryEnabled ? '已开启' : '已关闭')}</small></div><button className={`v-switch ${retryEnabled ? 'on' : ''}`} role="switch" aria-label={pt("诊断自动重试")} aria-checked={retryEnabled} onClick={onRetry}><span /></button></div><div className="v-settings-row"><div><b>{pt("14:28 · 请求恢复成功")}</b><small>upstream_timeout → retry → success</small></div><button className="v-small-button" onClick={() => setDetail(!detail)}>{pt(detail ? '收起详情' : '查看详情')}</button></div>{pt(detail && <pre className="v-config-preview">request: demo-request-1284{pt('\n')}provider: DeepSeek{pt('\n')}attempt 1: upstream_timeout · 1000 ms{pt('\n')}attempt 2: HTTP 200 · 280 ms{pt('\n')}result: recovered · total 1280 ms</pre>)}</div></section></>;
}
export function UpdatePrototype({ notify }: {
    notify: (s: string) => void;
}) {
    const [phase, setPhase] = useState('idle'), [confirm, setConfirm] = useState(false);
    const [outcome]=usePrototypeScenario('update');
    function check() { setPhase('checking'); window.setTimeout(() => setPhase(outcome === 'check-failure' ? 'check-failure' : outcome === 'latest' ? 'latest' : 'available'), 450); }
    function install() { setConfirm(false); setPhase('downloading'); notify('正在下载更新…'); window.setTimeout(() => { if (outcome === 'install-failure') {
        setPhase('install-failure');
        notify('安装失败 · 当前版本保留，可重试');
        return;
    } setPhase('installing'); window.setTimeout(() => { setPhase('done'); notify('安装演示完成 · 应用将自动重启'); }, 550); }, 550); }
    return <section className="v-settings-section"><div className="v-settings-section-heading"><h2>{pt("版本与更新")}</h2><p>{pt("查看版本、发布说明与安装进度。")}</p></div><div><div className="v-settings-row"><div><b>{pt("当前版本")}</b><small>{pt("v0.2.0-preview · UI 设计预览")}</small></div><button className="v-small-button" disabled={['checking', 'downloading', 'installing'].includes(phase)} onClick={check}><RefreshCw size={12}/>{pt(phase === 'checking' ? '检查中…' : '检查更新')}</button></div>{pt(phase === 'latest' && <p className="v-dialog-hint">{pt("当前已是最新版本。")}</p>)}{pt(phase === 'check-failure' && <p className="v-dialog-hint" role="alert">{pt("更新检查失败 · 请重试。")}</p>)}{pt(['available', 'downloading', 'installing', 'install-failure', 'done'].includes(phase) && <><div className="v-release-notes"><b>{pt("v0.2.0 · 更新日志")}</b><p>{pt("桌面导航重构；双主题；Provider、客户端与设置重新分组。")}</p></div>{pt(phase === 'install-failure' && <p className="v-error-text" role="alert">{pt("安装失败 · 当前版本可继续使用。")}</p>)}{pt(phase === 'done' ? <p className="v-dialog-hint">{pt("安装演示完成；正式版会自动重新启动 CodexHub。")}</p> : <button className="v-primary" disabled={phase === 'downloading' || phase === 'installing'} onClick={() => setConfirm(true)}>{pt(phase === 'downloading' ? '正在下载 · 48%' : phase === 'installing' ? '正在安装…' : phase === 'install-failure' ? '重试安装' : '安装更新')}</button>)}</>)}{pt(confirm && <div className="v-inline-confirm"><h3>{pt("安装并重启 CodexHub")}</h3><p>{pt("安装会退出应用并重启 Gateway，当前请求可能中断。原型仅模拟此流程。")}</p><div className="v-account-actions"><button className="v-small-button" onClick={() => setConfirm(false)}>{pt("稍后")}</button><button className="v-primary small" onClick={install}>{pt("安装并重启")}</button></div></div>)}</div></section>;
}
