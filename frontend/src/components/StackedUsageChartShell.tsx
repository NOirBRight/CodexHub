import { Check, ChevronDown, ChevronLeft, ChevronRight } from "lucide-react";
import { useLayoutEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { PendingPanel } from "./PendingPanel";
import type { GatewayUsageEvent, GatewayUsageSummary, Provider } from "../lib/types";

interface StackedUsageChartShellProps {
  events: GatewayUsageEvent[];
  pendingMessage: string;
  providers: Provider[];
  summary: GatewayUsageSummary | null;
}

type UsageRange = "7d" | "1m" | "custom";
type UsageGroup = "day" | "week";
type UsageMetric = "token" | "request";
type UsageBreakdown = "provider" | "model";

interface DateSpan {
  start: Date;
  end: Date;
}

interface BucketSpec {
  endExclusive: Date;
  label: string;
  start: Date;
}

interface StackSegment {
  color: string;
  key: string;
  label: string;
  value: number;
}

interface StackBucket extends BucketSpec {
  segments: StackSegment[];
  total: number;
}

interface StackSeries {
  color: string;
  key: string;
  label: string;
  total: number;
}

interface ChartPoint {
  value: number;
  x: number;
  y: number;
}

interface StackLayer extends StackSeries {
  basePoints: ChartPoint[];
  topPoints: ChartPoint[];
}

interface ChartHover {
  cursorY: number;
  hostHeight: number;
  hostWidth: number;
  index: number;
  x: number;
  y: number;
}

const STACK_COLORS = ["#3941ff", "#00a8a8", "#7c3aed", "#0ea5e9", "#b1a7ff", "#10b981", "#1e293b"];
const TOOLTIP_GAP = 16;
const TOOLTIP_WIDTH = 190;
const TOOLTIP_EDGE_MARGIN = 12;
const OTHER_SERIES_KEY = "__other__";

export function StackedUsageChartShell({
  events,
  pendingMessage,
  providers,
  summary,
}: StackedUsageChartShellProps) {
  const initialCustomRange = useMemo(() => defaultCustomRange(), []);
  const [range, setRange] = useState<UsageRange>("7d");
  const [groupBy, setGroupBy] = useState<UsageGroup>("day");
  const [metric, setMetric] = useState<UsageMetric>("token");
  const [breakdown, setBreakdown] = useState<UsageBreakdown>("provider");
  const [groupOpen, setGroupOpen] = useState(false);
  const [metricOpen, setMetricOpen] = useState(false);
  const [breakdownOpen, setBreakdownOpen] = useState(false);
  const [customOpen, setCustomOpen] = useState(false);
  const [customRange, setCustomRange] = useState<DateSpan>(initialCustomRange);
  const [calendarMonth, setCalendarMonth] = useState(() => startOfMonth(initialCustomRange.start));

  const filteredEvents = useMemo(
    () => filterEventsByRange(events, range, customRange),
    [customRange, events, range],
  );
  const providerLabels = useMemo(() => providerLabelMap(providers), [providers]);
  const stacked = useMemo(
    () => buildStackedBuckets(filteredEvents, range, groupBy, customRange, metric, breakdown, providerLabels),
    [breakdown, customRange, filteredEvents, groupBy, metric, providerLabels, range],
  );
  const axis = useMemo(
    () => stacked.buckets.map((bucket) => bucket.label),
    [stacked.buckets],
  );

  function selectRange(nextRange: UsageRange) {
    setRange(nextRange);
    setBreakdownOpen(false);
    setGroupOpen(false);
    setMetricOpen(false);
    setCustomOpen(nextRange === "custom" ? (open) => !open : false);
  }

  function selectCustomDay(day: Date) {
    const selected = startOfDay(day);
    setRange("custom");
    if (!isSameDay(customRange.start, customRange.end)) {
      setCustomRange({ start: selected, end: selected });
      return;
    }
    if (selected.getTime() < customRange.start.getTime()) {
      setCustomRange({ start: selected, end: customRange.start });
      return;
    }
    setCustomRange({ start: customRange.start, end: selected });
  }

  return (
    <section className="grid h-full min-h-0 grid-rows-[auto_auto_minmax(0,1fr)] gap-3 rounded-md border border-line bg-white p-4 shadow-subtle">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-ink">Usage &amp; Cost</h2>
          <p className="mt-1 text-xs text-slate-500">
            Local Gateway usage events; missing token usage stays unknown.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <div className="relative">
            <div className="grid grid-cols-3 rounded-full border border-line bg-panel p-1 text-xs shadow-subtle">
              {[
                { value: "7d", label: "7D" },
                { value: "1m", label: "1M" },
                { value: "custom", label: "Custom" },
              ].map((option) => (
                <button
                  key={option.value}
                  type="button"
                  className={
                    range === option.value
                      ? "h-7 rounded-full bg-white px-4 font-semibold text-ink shadow-subtle"
                      : "h-7 rounded-full px-4 font-semibold text-slate-500 hover:text-ink"
                  }
                  aria-pressed={range === option.value}
                  onClick={() => selectRange(option.value as UsageRange)}
                >
                  {option.label}
                </button>
              ))}
            </div>
            {customOpen && (
              <CalendarRangePopover
                month={calendarMonth}
                range={customRange}
                onMonthChange={setCalendarMonth}
                onSelect={selectCustomDay}
              />
            )}
          </div>

          <UsageDropdown
            label="Metric"
            open={metricOpen}
            options={[
              { value: "token", label: "Token" },
              { value: "request", label: "Request" },
            ]}
            value={metric}
            valueLabel={metric === "token" ? "Token" : "Request"}
            onToggle={() => {
              setMetricOpen((open) => !open);
              setBreakdownOpen(false);
              setGroupOpen(false);
              setCustomOpen(false);
            }}
            onSelect={(value) => {
              setMetric(value);
              setMetricOpen(false);
            }}
          />

          <UsageDropdown
            label="By"
            open={breakdownOpen}
            options={[
              { value: "provider", label: "Provider" },
              { value: "model", label: "Model" },
            ]}
            value={breakdown}
            valueLabel={breakdown === "model" ? "Model" : "Provider"}
            onToggle={() => {
              setBreakdownOpen((open) => !open);
              setMetricOpen(false);
              setGroupOpen(false);
              setCustomOpen(false);
            }}
            onSelect={(value) => {
              setBreakdown(value);
              setBreakdownOpen(false);
            }}
          />

          <UsageDropdown
            label="Group"
            open={groupOpen}
            options={[
              { value: "day", label: "Day" },
              { value: "week", label: "Week" },
            ]}
            value={groupBy}
            valueLabel={groupBy === "week" ? "Week" : "Day"}
            onToggle={() => {
              setGroupOpen((open) => !open);
              setBreakdownOpen(false);
              setMetricOpen(false);
              setCustomOpen(false);
            }}
            onSelect={(value) => {
              setGroupBy(value);
              setGroupOpen(false);
            }}
          />
        </div>
      </div>

      <div className="grid gap-2 sm:grid-cols-4">
        <Metric label="Tokens" value={summary?.total_tokens ? formatNumber(summary.total_tokens) : "Unknown"} />
        <Metric label="Requests" value={summary ? formatNumber(summary.requests) : "Unknown"} />
        <Metric label="Est. cost" value={costLabel(summary)} title={summary?.cost_label ?? undefined} />
        <Metric label="Cached input" value={cachedInputLabel(summary)} title={cachedInputTitle(summary)} />
      </div>

      <div className="relative min-h-0 overflow-hidden rounded-md border border-line bg-panel">
        {metric === "token" && !stacked.hasData ? (
          <NoTokenChart axis={axis} pendingMessage={pendingMessage} summary={summary} />
        ) : (
          <StackedUsageChart
            breakdown={breakdown}
            buckets={stacked.buckets}
            metric={metric}
            pendingMessage={pendingMessage}
            series={stacked.series}
            summary={summary}
          />
        )}
      </div>
    </section>
  );
}

function UsageDropdown<T extends string>({
  label,
  onSelect,
  onToggle,
  open,
  options,
  value,
  valueLabel,
}: {
  label: string;
  onSelect: (value: T) => void;
  onToggle: () => void;
  open: boolean;
  options: Array<{ label: string; value: T }>;
  value: T;
  valueLabel: string;
}) {
  return (
    <div className="relative">
      <button
        type="button"
        className="focus-ring flex h-9 w-[132px] items-center justify-between gap-2 rounded-full border border-line bg-white px-4 text-xs font-semibold text-slate-600 shadow-subtle"
        aria-expanded={open}
        onClick={onToggle}
      >
        <span>{label}</span>
        <span className="text-ink">{valueLabel}</span>
        <ChevronDown size={15} className="text-ink" />
      </button>
      {open && (
        <div className="absolute right-0 top-10 z-20 w-[132px] space-y-1 rounded-[16px] border border-line bg-white p-1 shadow-xl">
          {options.map((option) => {
            const selected = value === option.value;
            return (
              <button
                key={option.value}
                type="button"
                className={
                  selected
                    ? "flex h-8 w-full items-center justify-between rounded-[11px] bg-panel px-3 text-left text-xs font-semibold text-ink"
                    : "flex h-8 w-full items-center justify-between rounded-[11px] px-3 text-left text-xs font-semibold text-ink hover:bg-panel"
                }
                onClick={() => onSelect(option.value)}
              >
                {option.label}
                {selected && <Check size={16} />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function CalendarRangePopover({
  month,
  onMonthChange,
  onSelect,
  range,
}: {
  month: Date;
  onMonthChange: (month: Date) => void;
  onSelect: (day: Date) => void;
  range: DateSpan;
}) {
  const nextMonth = addMonths(month, 1);
  return (
    <div className="absolute right-0 top-10 z-30 w-[400px] rounded-[16px] border border-line bg-white p-3 shadow-2xl">
      <div className="mb-2 flex items-center justify-between">
        <button
          type="button"
          className="focus-ring grid h-7 w-7 place-items-center rounded-full text-ink hover:bg-panel"
          onClick={() => onMonthChange(addMonths(month, -1))}
          aria-label="Previous month"
        >
          <ChevronLeft size={16} />
        </button>
        <div className="text-[10px] font-semibold text-slate-500">
          {formatDate(range.start)} - {formatDate(range.end)}
        </div>
        <button
          type="button"
          className="focus-ring grid h-7 w-7 place-items-center rounded-full text-ink hover:bg-panel"
          onClick={() => onMonthChange(addMonths(month, 1))}
          aria-label="Next month"
        >
          <ChevronRight size={16} />
        </button>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <MonthGrid month={month} range={range} onSelect={onSelect} />
        <MonthGrid month={nextMonth} range={range} onSelect={onSelect} />
      </div>
    </div>
  );
}

function MonthGrid({
  month,
  onSelect,
  range,
}: {
  month: Date;
  onSelect: (day: Date) => void;
  range: DateSpan;
}) {
  const cells = monthCells(month);
  return (
    <div className="min-w-0">
      <h3 className="mb-2 text-center text-sm font-semibold text-ink">{formatMonthTitle(month)}</h3>
      <div className="mb-1 grid grid-cols-7 text-center text-[10px] font-semibold text-slate-400">
        {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((day) => (
          <span key={day}>{day}</span>
        ))}
      </div>
      <div className="grid grid-cols-7 gap-y-0.5 text-center text-xs">
        {cells.map((day, index) => {
          if (!day) {
            return <span key={`empty-${index}`} className="h-6" />;
          }
          const selected = isSameDay(day, range.start) || isSameDay(day, range.end);
          const inRange = day >= startOfDay(range.start) && day <= endOfDay(range.end);
          return (
            <button
              key={day.toISOString()}
              type="button"
              className={
                inRange
                  ? "mx-0 grid h-6 place-items-center rounded-full bg-slate-100 text-ink"
                  : "mx-0 grid h-6 place-items-center rounded-full text-slate-500 hover:bg-panel hover:text-ink"
              }
              onClick={() => onSelect(day)}
            >
              <span
                className={
                  selected
                    ? "grid h-5 w-5 place-items-center rounded-full bg-ink text-white"
                    : "grid h-5 w-5 place-items-center rounded-full"
                }
              >
                {day.getDate()}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function StackedUsageChart({
  breakdown,
  buckets,
  metric,
  pendingMessage,
  series,
  summary,
}: {
  breakdown: UsageBreakdown;
  buckets: StackBucket[];
  metric: UsageMetric;
  pendingMessage: string;
  series: StackSeries[];
  summary: GatewayUsageSummary | null;
}) {
  const [hover, setHover] = useState<ChartHover | null>(null);
  const tooltipRef = useRef<HTMLDivElement | null>(null);
  const [tooltipHeight, setTooltipHeight] = useState(0);
  const maxTotal = niceMax(Math.max(1, ...buckets.map((bucket) => bucket.total)));
  const hasData = buckets.some((bucket) => bucket.total > 0);
  const columns = chartColumns(buckets.length);
  const valueLabel = metric === "request" ? "requests" : "tokens";
  const layers = buildChartLayers(buckets, series, maxTotal);
  const isModelBreakdown = breakdown === "model";
  const tooltipWidth = isModelBreakdown ? 320 : TOOLTIP_WIDTH;
  const activeIndex = hover?.index ?? Math.max(0, buckets.findIndex((bucket) => bucket.total > 0));
  const activeBucket = buckets[activeIndex];
  const activeSegments = activeBucket?.segments.filter((segment) => segment.value > 0) ?? [];
  const activeSegmentSignature = activeSegments
    .map((segment) => `${segment.key}:${segment.label}:${segment.value}`)
    .join("|");
  const activeTopPoints = layers
    .map((layer) => ({ color: layer.color, point: layer.topPoints[activeIndex] }))
    .filter((item) => item.point && item.point.value > 0);
  const tooltipOnLeft = hover ? hover.x + tooltipWidth + TOOLTIP_GAP + 8 > hover.hostWidth : false;
  const measuredTooltipHeight = tooltipHeight || 86;
  const tooltipFitsAbove = hover
    ? hover.cursorY - measuredTooltipHeight - TOOLTIP_GAP >= TOOLTIP_EDGE_MARGIN
    : false;
  const tooltipTop = hover
    ? tooltipFitsAbove
      ? hover.cursorY - measuredTooltipHeight - TOOLTIP_GAP
      : Math.min(
          hover.cursorY + TOOLTIP_GAP,
          Math.max(TOOLTIP_EDGE_MARGIN, hover.hostHeight - measuredTooltipHeight - TOOLTIP_EDGE_MARGIN),
        )
    : TOOLTIP_EDGE_MARGIN;
  const tooltipLeft = hover
    ? tooltipOnLeft
      ? hover.x - tooltipWidth - TOOLTIP_GAP
      : hover.x + TOOLTIP_GAP
    : TOOLTIP_EDGE_MARGIN;
  const boundedTooltipLeft = hover
    ? Math.min(
        Math.max(TOOLTIP_EDGE_MARGIN, tooltipLeft),
        Math.max(TOOLTIP_EDGE_MARGIN, hover.hostWidth - tooltipWidth - TOOLTIP_EDGE_MARGIN),
      )
    : TOOLTIP_EDGE_MARGIN;
  const activeBucketDateLabel = activeBucket ? formatBucketTooltipDate(activeBucket) : "";

  useLayoutEffect(() => {
    if (!hover || !tooltipRef.current) {
      return;
    }
    const nextHeight = tooltipRef.current.getBoundingClientRect().height;
    setTooltipHeight((current) => (Math.abs(current - nextHeight) > 0.5 ? nextHeight : current));
  }, [activeBucketDateLabel, activeSegmentSignature, hover]);

  function handleHover(event: MouseEvent<HTMLDivElement>) {
    if (buckets.length === 0) {
      return;
    }
    const plotRect = event.currentTarget.getBoundingClientRect();
    const hostRect = event.currentTarget.parentElement?.getBoundingClientRect();
    if (!hostRect) {
      return;
    }
    const percent = Math.min(1, Math.max(0, (event.clientX - plotRect.left) / plotRect.width));
    const index = nearestBucketIndex(percent, buckets.length);
    const point = layerTopPoint(layers, index) ?? { x: bucketX(index, buckets.length), y: 100 };
    setHover({
      cursorY: event.clientY - hostRect.top,
      hostHeight: hostRect.height,
      hostWidth: hostRect.width,
      index,
      x: plotRect.left - hostRect.left + (point.x / 100) * plotRect.width,
      y: plotRect.top - hostRect.top + (point.y / 100) * plotRect.height,
    });
  }

  return (
    <div className="grid h-full min-h-[300px] p-4">
      <div className="relative h-full min-h-[250px] overflow-hidden rounded-md border border-dashed border-line bg-white/70">
        <div className="absolute bottom-14 left-3 top-6 grid w-9 grid-rows-[auto_1fr_auto] text-[10px] font-semibold text-slate-400">
          <span>{formatNumber(maxTotal)}</span>
          <span className="self-center">{formatNumber(Math.round(maxTotal / 2))}</span>
          <span>0</span>
        </div>
        <div
          className="absolute inset-x-14 bottom-14 top-6"
          onMouseMove={handleHover}
          onMouseLeave={() => setHover(null)}
        >
          <svg className="h-full w-full overflow-visible" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label={`${breakdown} ${valueLabel} usage`}>
            {[0, 25, 50, 75, 100].map((y) => (
              <line
                key={y}
                x1="0"
                x2="100"
                y1={y}
                y2={y}
                stroke="#e2e8f0"
                strokeWidth="0.45"
                vectorEffect="non-scaling-stroke"
                strokeDasharray={y === 0 ? "0" : y === 25 ? "3 3" : "0"}
              />
            ))}
            {hasData && layers.map((layer) => (
              <path
                key={`${layer.key}:area`}
                d={areaPath(layer.topPoints, layer.basePoints)}
                fill={layer.color}
                fillOpacity="0.18"
              />
            ))}
            {hasData && layers.map((layer) => (
              <path
                key={`${layer.key}:line`}
                d={linePath(layer.topPoints)}
                fill="none"
                stroke={layer.color}
                strokeWidth="2"
                vectorEffect="non-scaling-stroke"
              />
            ))}
            {hasData && activeBucket && hover && (
              <line
                x1={bucketX(activeIndex, buckets.length)}
                x2={bucketX(activeIndex, buckets.length)}
                y1="0"
                y2="100"
                stroke="#94a3b8"
                strokeWidth="1"
                strokeDasharray="4 4"
                vectorEffect="non-scaling-stroke"
              />
            )}
          </svg>
          {hasData && hover && activeTopPoints.map(({ color, point }) => (
            <span
              key={`${point.x}-${point.y}-${point.value}`}
              className="pointer-events-none absolute h-2 w-2 rounded-full border-2 border-white shadow-sm"
              style={{
                backgroundColor: color,
                left: `${point.x}%`,
                top: `${point.y}%`,
                transform: "translate(-50%, -50%)",
              }}
            />
          ))}
          {hasData ? (
            <div
              className="pointer-events-none absolute inset-x-0 bottom-0 grid h-2 items-end gap-1 opacity-0"
              style={{ gridTemplateColumns: columns }}
            >
              {buckets.map((bucket) => (
                <span
                  key={`${bucket.label}-${bucket.start.toISOString()}`}
                  title={`${bucket.label} - ${formatNumber(bucket.total)} ${valueLabel}`}
                />
              ))}
            </div>
          ) : (
            <div className="relative z-10 grid h-full min-h-[226px] place-items-center p-5">
            <PendingPanel
              compact
              className="w-full max-w-[480px] py-3"
              label={summary && summary.requests > 0 ? "event window empty" : "pending data"}
              title={metric === "request" ? "Request usage" : "Token usage"}
              message={
                  summary && summary.requests > 0
                    ? `No ${valueLabel} in the selected range for this ${breakdown} breakdown.`
                    : pendingMessage
                }
              />
            </div>
          )}
        </div>
        <div className="absolute inset-x-14 bottom-8 h-5 text-center text-[10px] font-semibold text-slate-400">
          {buckets.map((bucket, index) => (
            <span
              key={`${bucket.label}-${bucket.start.toISOString()}`}
              className="absolute top-0 -translate-x-1/2 truncate"
              style={{ left: `${bucketX(index, buckets.length)}%` }}
            >
              {bucket.label}
            </span>
          ))}
        </div>
        {series.length > 0 && (
          <div className="absolute inset-x-10 bottom-2 flex min-h-4 flex-wrap items-center justify-center gap-x-4 gap-y-1 text-[10px] font-semibold text-slate-500">
            {series.map((item) => (
              <span
                key={item.key}
                className={
                  isModelBreakdown
                    ? "inline-flex max-w-[360px] items-start gap-1.5 leading-4"
                    : "inline-flex min-w-0 items-center gap-1.5"
                }
              >
                <i className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: item.color }} />
                <span
                  className={
                    isModelBreakdown
                      ? "min-w-0 whitespace-normal break-words text-left"
                      : "max-w-32 truncate"
                  }
                  title={item.label}
                >
                  {item.label}
                </span>
              </span>
            ))}
          </div>
        )}
        {hasData && hover && activeBucket && (
          <div
            ref={tooltipRef}
            className="pointer-events-none absolute z-20 rounded-md border border-line bg-white p-3 text-xs shadow-xl"
            style={{
              left: boundedTooltipLeft,
              top: tooltipTop,
              width: tooltipWidth,
            }}
          >
            <div className="mb-2 font-semibold text-ink">{activeBucketDateLabel}</div>
            <div className="grid gap-1">
              {activeSegments.map((segment) => (
                <div
                  key={segment.key}
                  className={
                    isModelBreakdown
                      ? "grid grid-cols-[auto_minmax(0,1fr)_auto] items-start gap-2"
                      : "grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2"
                  }
                >
                  <i className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: segment.color }} />
                  <span
                    className={
                      isModelBreakdown
                        ? "min-w-0 whitespace-normal break-words leading-4 text-slate-600"
                        : "truncate text-slate-600"
                    }
                  >
                    {segment.label}
                  </span>
                  <span className="font-mono font-semibold leading-4 text-ink">{formatNumber(segment.value)}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function NoTokenChart({
  axis,
  pendingMessage,
  summary,
}: {
  axis: string[];
  pendingMessage: string;
  summary: GatewayUsageSummary | null;
}) {
  const columns = chartColumns(axis.length);
  return (
    <div className="grid h-full min-h-[300px] p-4">
      <div className="grid h-full min-h-[258px] grid-rows-[minmax(0,1fr)_22px] overflow-hidden rounded-md border border-dashed border-line bg-white/70">
        <div className="relative overflow-hidden">
          <div className="absolute inset-x-8 bottom-0 top-4 grid grid-rows-4">
            {Array.from({ length: 4 }).map((_, index) => (
              <span key={index} className="border-b border-slate-200/80" />
            ))}
          </div>
          <div
            className="absolute inset-x-8 bottom-0 grid items-end gap-1 opacity-30"
            style={{ gridTemplateColumns: columns }}
          >
            {axis.map((label) => (
              <span
                key={label}
                className="h-2 min-w-0 rounded-t-sm border border-line bg-panel"
              />
            ))}
          </div>
          <div className="relative z-10 grid h-full min-h-[226px] place-items-center p-5">
            <PendingPanel
              compact
              className="w-full max-w-[480px] py-3"
              label={summary && summary.requests > 0 ? "no token usage" : "pending data"}
              title={summary && summary.requests > 0 ? "Usage tokens unavailable" : "Usage telemetry"}
              message={
                summary && summary.requests > 0
                  ? `${summary.requests} requests are recorded, with ${summary.missing_usage_requests} missing usage records. Tokens and cost stay unknown until upstream usage exists.`
                  : pendingMessage
              }
            />
          </div>
        </div>
        <div
          className="mx-8 grid items-start gap-1 pt-1 text-center text-[10px] font-semibold text-slate-400"
          style={{ gridTemplateColumns: columns }}
        >
          {axis.map((label) => (
            <span key={label} className="truncate">
              {label}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

function filterEventsByRange(events: GatewayUsageEvent[], range: UsageRange, customRange: DateSpan) {
  const span = rangeToSpan(range, customRange);
  return events.filter((event) => {
    if (!event.ts) {
      return true;
    }
    const ts = Date.parse(event.ts);
    if (Number.isNaN(ts)) {
      return true;
    }
    return ts >= span.start.getTime() && ts <= endOfDay(span.end).getTime();
  });
}

function buildStackedBuckets(
  events: GatewayUsageEvent[],
  range: UsageRange,
  groupBy: UsageGroup,
  customRange: DateSpan,
  metric: UsageMetric,
  breakdown: UsageBreakdown,
  providerLabels: Map<string, string>,
): { buckets: StackBucket[]; hasData: boolean; series: StackSeries[] } {
  const rawBuckets = bucketSpecs(range, groupBy, customRange).map((bucket) => ({
    ...bucket,
    totals: new Map<string, number>(),
  }));
  const labels = new Map<string, string>();
  const seriesTotals = new Map<string, number>();

  for (const event of events) {
    if (!event.ts) {
      continue;
    }
    const time = Date.parse(event.ts);
    if (Number.isNaN(time)) {
      continue;
    }
    const value = metricValue(event, metric);
    if (value <= 0) {
      continue;
    }
    const bucket = rawBuckets.find(
      (candidate) => time >= candidate.start.getTime() && time < candidate.endExclusive.getTime(),
    );
    if (bucket) {
      const segment = breakdownSegment(event, breakdown, providerLabels);
      labels.set(segment.key, segment.label);
      bucket.totals.set(segment.key, (bucket.totals.get(segment.key) ?? 0) + value);
      seriesTotals.set(segment.key, (seriesTotals.get(segment.key) ?? 0) + value);
    }
  }

  const sortedSeries = [...seriesTotals.entries()]
    .filter(([, total]) => total > 0)
    .sort((left, right) => right[1] - left[1]);
  const topKeys = sortedSeries.slice(0, 6).map(([key]) => key);
  const hasOther = sortedSeries.length > topKeys.length;
  const seriesKeys = hasOther ? [...topKeys, OTHER_SERIES_KEY] : topKeys;
  const topKeySet = new Set(topKeys);
  const series = seriesKeys.map((key, index) => {
    const total =
      key === OTHER_SERIES_KEY
        ? sortedSeries
            .filter(([candidate]) => !topKeySet.has(candidate))
            .reduce((sum, [, value]) => sum + value, 0)
        : seriesTotals.get(key) ?? 0;
    return {
      color: STACK_COLORS[index % STACK_COLORS.length],
      key,
      label: key === OTHER_SERIES_KEY ? "Other" : labels.get(key) ?? key,
      total,
    };
  });

  const buckets = rawBuckets.map((bucket) => {
    const segments = series.map((item) => {
      const value =
        item.key === OTHER_SERIES_KEY
          ? [...bucket.totals.entries()]
              .filter(([candidate]) => !topKeySet.has(candidate))
              .reduce((sum, [, total]) => sum + total, 0)
          : bucket.totals.get(item.key) ?? 0;
      return { ...item, value };
    });
    return {
      endExclusive: bucket.endExclusive,
      label: bucket.label,
      segments,
      start: bucket.start,
      total: [...bucket.totals.values()].reduce((sum, value) => sum + value, 0),
    };
  });

  return {
    buckets,
    hasData: buckets.some((bucket) => bucket.total > 0),
    series: series.filter((item) => item.total > 0),
  };
}

function bucketSpecs(range: UsageRange, groupBy: UsageGroup, customRange: DateSpan): BucketSpec[] {
  const span = rangeToSpan(range, customRange);
  const dayCount = Math.max(1, differenceInDays(span.start, span.end) + 1);
  const formatter = new Intl.DateTimeFormat("en-US", { month: "numeric", day: "numeric" });
  const buckets: BucketSpec[] = [];

  if (groupBy === "day") {
    for (let offset = 0; offset < dayCount; offset += 1) {
      const start = addDays(span.start, offset);
      buckets.push({
        start,
        endExclusive: addDays(start, 1),
        label: formatter.format(start),
      });
    }
    return buckets;
  }

  const firstWeekStart = startOfWeekMonday(span.start);
  const lastWeekStart = startOfWeekMonday(span.end);
  for (let start = firstWeekStart; start <= lastWeekStart; start = addDays(start, 7)) {
    const endExclusive = addDays(start, 7);
    const endInclusive = addDays(endExclusive, -1);
    buckets.push({
      start,
      endExclusive,
      label: `${formatter.format(start)}-${formatter.format(endInclusive)}`,
    });
  }

  return buckets;
}

function chartColumns(count: number) {
  return `repeat(${Math.max(1, count)}, minmax(0, 1fr))`;
}

function buildChartLayers(buckets: StackBucket[], series: StackSeries[], maxTotal: number): StackLayer[] {
  return series.map((item, seriesIndex) => {
    const basePoints: ChartPoint[] = [];
    const topPoints: ChartPoint[] = [];

    buckets.forEach((bucket, bucketIndex) => {
      const base = bucket.segments
        .slice(0, seriesIndex)
        .reduce((sum, segment) => sum + segment.value, 0);
      const segment = bucket.segments.find((candidate) => candidate.key === item.key);
      const top = base + (segment?.value ?? 0);
      const x = bucketX(bucketIndex, buckets.length);
      basePoints.push({ value: base, x, y: valueToY(base, maxTotal) });
      topPoints.push({ value: top, x, y: valueToY(top, maxTotal) });
    });

    return {
      ...item,
      basePoints,
      topPoints,
    };
  });
}

function areaPath(topPoints: ChartPoint[], basePoints: ChartPoint[]) {
  if (!topPoints.length || !basePoints.length) {
    return "";
  }
  const reversedBase = [...basePoints].reverse();
  return [
    linePath(topPoints),
    smoothPath(reversedBase, false),
    "Z",
  ].join(" ");
}

function linePath(points: ChartPoint[]) {
  if (!points.length) {
    return "";
  }
  return smoothPath(points, true);
}

function smoothPath(points: ChartPoint[], moveToFirst: boolean) {
  if (!points.length) {
    return "";
  }
  if (points.length === 1) {
    return `${moveToFirst ? "M" : "L"} ${formatPoint(points[0])}`;
  }
  const commands = [`${moveToFirst ? "M" : "L"} ${formatPoint(points[0])}`];
  for (let index = 0; index < points.length - 1; index += 1) {
    const previous = points[index - 1] ?? points[index];
    const current = points[index];
    const next = points[index + 1];
    const afterNext = points[index + 2] ?? next;
    const minX = Math.min(current.x, next.x);
    const maxX = Math.max(current.x, next.x);
    const minY = Math.min(current.y, next.y);
    const maxY = Math.max(current.y, next.y);
    const c1 = {
      x: current.x + (next.x - previous.x) / 6,
      y: current.y + (next.y - previous.y) / 6,
    };
    const c2 = {
      x: next.x - (afterNext.x - current.x) / 6,
      y: next.y - (afterNext.y - current.y) / 6,
    };
    commands.push(
      `C ${formatPoint(clampPoint(c1, minX, maxX, minY, maxY))} ${formatPoint(clampPoint(c2, minX, maxX, minY, maxY))} ${formatPoint(next)}`,
    );
  }
  return commands.join(" ");
}

function clampPoint(point: Pick<ChartPoint, "x" | "y">, minX: number, maxX: number, minY: number, maxY: number) {
  return {
    x: Math.min(maxX, Math.max(minX, point.x)),
    y: Math.min(maxY, Math.max(minY, point.y)),
  };
}

function formatPoint(point: Pick<ChartPoint, "x" | "y">) {
  return `${point.x.toFixed(3)} ${point.y.toFixed(3)}`;
}

function layerTopPoint(layers: StackLayer[], index: number) {
  return [...layers]
    .reverse()
    .map((layer) => layer.topPoints[index])
    .find((point) => point && point.value > 0);
}

function nearestBucketIndex(percent: number, count: number) {
  if (count <= 1) {
    return 0;
  }
  return Math.min(count - 1, Math.max(0, Math.round(percent * (count - 1))));
}

function bucketX(index: number, count: number) {
  if (count <= 1) {
    return 50;
  }
  return (index / (count - 1)) * 100;
}

function valueToY(value: number, maxTotal: number) {
  return 100 - (value / Math.max(1, maxTotal)) * 100;
}

function niceMax(value: number) {
  if (value <= 1) {
    return 1;
  }
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const normalized = value / magnitude;
  const nice = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return nice * magnitude;
}

function rangeToSpan(range: UsageRange, customRange: DateSpan): DateSpan {
  if (range === "custom") {
    return {
      start: startOfDay(customRange.start),
      end: endOfDay(customRange.end),
    };
  }
  const end = startOfDay(new Date());
  const days = range === "7d" ? 6 : 30;
  return {
    start: addDays(end, -days),
    end,
  };
}

function Metric({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div className="rounded-md border border-line bg-panel p-3" title={title}>
      <div className="text-[11px] font-semibold uppercase tracking-[0.04em] text-slate-500">
        {label}
      </div>
      <div className="mt-2 truncate font-mono text-base font-semibold text-ink">{value}</div>
    </div>
  );
}

function metricValue(event: GatewayUsageEvent, metric: UsageMetric) {
  if (metric === "request") {
    return 1;
  }
  return tokenTotal(event) ?? 0;
}

function breakdownSegment(event: GatewayUsageEvent, breakdown: UsageBreakdown, providerLabels: Map<string, string>) {
  const provider = event.upstream?.trim() || "Unknown provider";
  const providerName = providerLabel(provider, providerLabels);

  if (breakdown === "model") {
    const model = event.model?.trim() || "Unknown model";
    return {
      key: `model:${provider.toLowerCase()}:${model}`,
      label: `${providerName} / ${model}`,
    };
  }

  return {
    key: `provider:${provider}`,
    label: providerName,
  };
}

function providerLabelMap(providers: Provider[]) {
  const labels = new Map<string, string>([
    ["official", "Official OpenAI"],
    ["official_openai", "Official OpenAI"],
  ]);
  for (const provider of providers) {
    labels.set(provider.id.toLowerCase(), provider.name);
  }
  return labels;
}

function providerLabel(provider: string, providerLabels: Map<string, string>) {
  const normalized = provider.toLowerCase();
  const mapped = providerLabels.get(normalized);
  if (mapped) {
    return mapped;
  }
  if (normalized.startsWith("unknown")) {
    return "Unknown";
  }
  return titleizeProviderId(provider);
}

function titleizeProviderId(provider: string) {
  return provider
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((part) => {
      const lower = part.toLowerCase();
      if (lower === "openai") {
        return "OpenAI";
      }
      if (lower === "cn") {
        return "CN";
      }
      return `${part.slice(0, 1).toUpperCase()}${part.slice(1)}`;
    })
    .join(" ");
}

function tokenTotal(event: GatewayUsageEvent) {
  if (event.total_tokens !== null && event.total_tokens !== undefined) {
    return event.total_tokens;
  }
  const input = event.input_tokens ?? 0;
  const output = event.output_tokens ?? 0;
  const total = input + output;
  return total > 0 ? total : null;
}

function formatNumber(value: number) {
  return new Intl.NumberFormat("en-US").format(value);
}

function costLabel(summary: GatewayUsageSummary | null) {
  if (!summary) {
    return "Unknown";
  }
  if (summary.estimated_cost_usd !== null && summary.estimated_cost_usd !== undefined) {
    return `$${summary.estimated_cost_usd.toFixed(2)}`;
  }
  return "Unknown";
}

function cachedInputLabel(summary: GatewayUsageSummary | null) {
  if (!summary) {
    return "Unknown";
  }
  if (summary.cache_hit_rate !== null && summary.cache_hit_rate !== undefined) {
    return `${summary.cache_hit_rate.toFixed(1)}%`;
  }
  return summary.requests > 0 ? "N/A" : "Unknown";
}

function cachedInputTitle(summary: GatewayUsageSummary | null) {
  if (!summary || summary.cache_hit_rate === null || summary.cache_hit_rate === undefined) {
    return undefined;
  }
  return "Cached input tokens divided by input tokens for requests that reported cache usage.";
}

function defaultCustomRange(): DateSpan {
  const end = startOfDay(new Date());
  return {
    start: addDays(end, -29),
    end,
  };
}

function monthCells(month: Date): Array<Date | null> {
  const first = startOfMonth(month);
  const firstWeekday = (first.getDay() + 6) % 7;
  const days = daysInMonth(first);
  return [
    ...Array.from({ length: firstWeekday }, () => null),
    ...Array.from({ length: days }, (_, index) => new Date(first.getFullYear(), first.getMonth(), index + 1)),
  ];
}

function formatMonthTitle(date: Date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

function formatDate(date: Date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function formatBucketTooltipDate(bucket: StackBucket) {
  const endInclusive = addDays(bucket.endExclusive, -1);
  if (isSameDay(bucket.start, endInclusive)) {
    return formatLongDate(bucket.start);
  }
  return `${formatLongDate(bucket.start)} - ${formatLongDate(endInclusive)}`;
}

function formatLongDate(date: Date) {
  return new Intl.DateTimeFormat("en-US", {
    day: "numeric",
    month: "short",
    year: "numeric",
  }).format(date);
}

function startOfDay(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function endOfDay(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate(), 23, 59, 59, 999);
}

function startOfMonth(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), 1);
}

function startOfWeekMonday(date: Date) {
  const start = startOfDay(date);
  const weekday = (start.getDay() + 6) % 7;
  return addDays(start, -weekday);
}

function addDays(date: Date, days: number) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() + days);
}

function addMonths(date: Date, months: number) {
  return new Date(date.getFullYear(), date.getMonth() + months, 1);
}

function daysInMonth(date: Date) {
  return new Date(date.getFullYear(), date.getMonth() + 1, 0).getDate();
}

function differenceInDays(start: Date, end: Date) {
  const dayMs = 24 * 60 * 60 * 1000;
  return Math.round((startOfDay(end).getTime() - startOfDay(start).getTime()) / dayMs);
}

function isSameDay(left: Date, right: Date) {
  return startOfDay(left).getTime() === startOfDay(right).getTime();
}
