import { usePrototypeScenario } from './PrototypeTools';
import { pt } from './PrototypeLocale';
// THROWAWAY auth states corresponding to XaiLoginCard. No device or network requests.
import { useState } from 'react';
import { OfficialOpenAIUsageLimitBars } from '../components/providers/OfficialOpenAIUsagePanel';
export const prototypeLimits = [{ key: 'primary', name: '5 hours', period: '5h', limit: 100, used: 28, remaining: 72, resets_at: new Date(Date.now() + 8280000).toISOString() }, { key: 'week', name: 'Weekly', period: 'week', limit: 100, used: 46, remaining: 54, resets_at: new Date(Date.now() + 259200000).toISOString() }];
export const prototypeXaiLimits = [{ key: 'week', name: 'Weekly', period: 'week', limit: 100, used: 32, remaining: 68, resets_at: new Date(Date.now() + 345600000).toISOString() }];
let demoXaiSignedIn=true;
export function SubscriptionPrototype({ onSignedIn, notify }: {
    onSignedIn: () => void;
    notify: (s: string) => void;
}) {
    const [state, setState] = useState(demoXaiSignedIn?'signedin':'signedout');
    const [outcome]=usePrototypeScenario('subscription');
    function login() { setState('waiting'); notify('设备授权码已生成 · 等待演示授权'); }
    return <div><div className="v-auth-status"><h3>{pt(state === 'signedin' ? 'xAI 订阅已连接' : '连接 xAI 订阅')}</h3><p>{pt(state === 'signedin' ? '账户：demo@example.test · 订阅凭据由账户授权管理。' : '通过设备授权连接订阅。也可以在连接配置中填写 API Key。')}</p>{pt(state === 'waiting' && <><div className="v-device-code">DEMO-8X4P</div><div className="v-account-actions"><button className="v-small-button" onClick={() => { void navigator.clipboard.writeText('DEMO-8X4P').then(() => notify('演示授权码已复制')).catch(() => notify('复制失败')); }}>{pt("复制授权码")}</button><button className="v-small-button" onClick={() => notify('演示授权页已就绪 · 点击「完成演示授权」继续')}>{pt("打开授权页面")}</button></div><p>{pt("等待授权 · 10 分钟内有效")}</p></>)}{pt(state === 'expired' && <p role="alert">{pt("授权码已过期，请重新开始登录。")}</p>)}{pt(state === 'denied' && <p role="alert">{pt("此账户暂不具备订阅接入资格（403）。可以改用 API Key。")}</p>)}{pt(state === 'error' && <p role="alert">{pt("授权服务不可用。请重试，或使用 API Key。")}</p>)}<div className="v-account-actions">{pt(state === 'signedin' ? <><button className="v-small-button" onClick={() => notify('xAI 账户与额度已刷新 · 无需重启')}>{pt("刷新账户与用量")}</button><button className="v-small-button" onClick={() => { demoXaiSignedIn=false;setState('signedout'); notify('xAI 已退出登录 · API Key 配置保留'); }}>{pt("退出登录")}</button></> : state === 'waiting' ? <><button className="v-primary small" onClick={() => { setState(outcome === 'success' ? 'signedin' : outcome); if (outcome === 'success') {demoXaiSignedIn=true;
        onSignedIn();
        notify('xAI 已授权 · 已发现订阅模型');
    }
    else
        notify('授权失败 · 可重试或改用 API Key'); }}>{pt("完成演示授权")}</button><button className="v-small-button" onClick={() => { demoXaiSignedIn=false;setState('signedout'); notify('已取消设备授权'); }}>{pt("取消")}</button></> : <button className="v-primary small" onClick={login}>{pt("开始设备授权")}</button>)}<button className="v-small-button" onClick={() => notify('认证状态已刷新 · 演示状态')}>{pt("刷新认证")}</button></div></div>{pt(state === 'signedin' && <div className="v-original-controls v-quota-windows"><OfficialOpenAIUsageLimitBars limits={prototypeXaiLimits} busy={false}/></div>)}</div>;
}
