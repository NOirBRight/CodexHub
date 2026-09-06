// THROWAWAY resource fixtures. Each metric is an independent resource, not a duplicate status.
import { pt } from './PrototypeLocale';
import { prototypeLimits, prototypeXaiLimits } from './SubscriptionPrototype';

type ResourceMetric = { label: string; value: string; unit?: string; note?: string; percent?: number };
const resourceFixtures: Record<string, ResourceMetric[]> = {
  openai: [
    { label: '5 小时剩余', value: String(prototypeLimits[0].remaining), unit: '%', percent: prototypeLimits[0].remaining, note: '2 小时 18 分后重置' },
    { label: '每周剩余', value: String(prototypeLimits[1].remaining), unit: '%', percent: prototypeLimits[1].remaining, note: '3 天后重置' },
  ],
  xai: [{ label: '每周剩余', value: String(prototypeXaiLimits[0].remaining), unit: '%', percent: prototypeXaiLimits[0].remaining, note: '4 天后重置' }],
  anthropic: [{ label: '可用余额', value: '$48.60' }, { label: '今日消耗', value: '$3.24' }],
  deepseek: [{ label: '可用余额', value: '¥126.80' }, { label: '今日消耗', value: '¥2.18' }],
  kimi: [{ label: 'Token 剩余', value: '1.2M', unit: '/ 10M', percent: 12 }],
};
export function PrototypeResourceDetails({ providerId }: { providerId: string }) {
  const metrics = resourceFixtures[providerId];
  if (!metrics?.length) return <span className="v-resource-unknown">{pt('尚未查询额度')}</span>;
  return <div className="v-resource-details" data-count={metrics.length}>
    {metrics.map(metric => <div className={`v-resource-metric ${metric.percent === undefined ? 'balance' : 'quota'}`} key={metric.label}>
      <div className="v-resource-value"><span>{pt(metric.label)}</span><strong>{metric.value}{metric.unit && <small>{metric.unit}</small>}</strong></div>
      {metric.percent !== undefined && <div className="v-resource-meter" aria-hidden="true"><i style={{width:`${metric.percent}%`}}/></div>}
      {metric.note && <small className="v-resource-reset">{pt(metric.note)}</small>}
    </div>)}
  </div>;
}
