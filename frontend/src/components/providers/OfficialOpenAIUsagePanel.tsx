import type { FocusEvent as ReactFocusEvent, PointerEvent as ReactPointerEvent } from "react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { cx } from "../../lib/format";
import type { OpenAIUsageLimit, OpenAIUsageSnapshot } from "../../lib/types";

type Translate = (key: string, options?: Record<string, unknown>) => string;

type StoredOpenAIUsageSnapshot = {
  stored_at: number;
  snapshot: OpenAIUsageSnapshot;
};

const OPENAI_USAGE_DAY_SECONDS = 86_400;
const OPENAI_USAGE_MIN_WINDOW_DAYS = 365;
const OPENAI_USAGE_QUERY_WINDOW_DAYS = 730;
export const OPENAI_USAGE_REFRESH_INTERVAL_MS = 3 * 60 * 1000;
const OPENAI_USAGE_STORAGE_TTL_MS = OPENAI_USAGE_REFRESH_INTERVAL_MS;
const OFFICIAL_OPENAI_USAGE_STORAGE_KEY = "codexhub.officialOpenAIUsageSnapshot.v1";
const OFFICIAL_USAGE_CELL_GAP = 2;
const OFFICIAL_USAGE_CELL_SIZE = 8;
const USAGE_MONTH_LABEL_MIN_GAP_PX = 36;
const OFFICIAL_USAGE_COLOR_STOPS = ["#eff2f5", "#d8ebff", "#acd7ff", "#7cc1ff", "#48a7fb", "#1687e8"];
const OPENAI_USAGE_LIMIT_PLACEHOLDERS: OpenAIUsageLimit[] = [
  { key: "five_hours", name: "5 hours", period: "five_hours" },
  { key: "week", name: "Week", period: "week" },
];

function useElementContentWidth<T extends HTMLElement>(dependencies: ReadonlyArray<unknown> = []) {
  const ref = useRef<T | null>(null);
  const [contentWidth, setContentWidth] = useState(0);

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) {
      setContentWidth(0);
      return;
    }

    const update = () => {
      const style = window.getComputedStyle(element);
      const padding =
        (Number.parseFloat(style.paddingLeft) || 0) +
        (Number.parseFloat(style.paddingRight) || 0);
      setContentWidth(Math.max(0, element.clientWidth - padding));
    };

    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    window.addEventListener("resize", update);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", update);
    };
  }, dependencies);

  return [ref, contentWidth] as const;
}

export function readStoredOfficialOpenAIUsageSnapshot(): OpenAIUsageSnapshot | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const raw = window.localStorage.getItem(OFFICIAL_OPENAI_USAGE_STORAGE_KEY);
    if (!raw) {
      return null;
    }
    const stored = JSON.parse(raw) as unknown;
    if (!isStoredOpenAIUsageSnapshot(stored)) {
      return null;
    }
    if (Date.now() - stored.stored_at > OPENAI_USAGE_STORAGE_TTL_MS) {
      return null;
    }
    return stored.snapshot;
  } catch {
    return null;
  }
}

export function storeOfficialOpenAIUsageSnapshot(snapshot: OpenAIUsageSnapshot) {
  if (typeof window === "undefined") {
    return;
  }
  try {
    const stored = {
      stored_at: Date.now(),
      snapshot,
    };
    window.localStorage.setItem(OFFICIAL_OPENAI_USAGE_STORAGE_KEY, JSON.stringify(stored));
  } catch {
    // Ignore storage quota or privacy-mode failures; backend cache still works.
  }
}

function isStoredOpenAIUsageSnapshot(value: unknown): value is StoredOpenAIUsageSnapshot {
  if (!value || typeof value !== "object") {
    return false;
  }
  const stored = value as Partial<StoredOpenAIUsageSnapshot>;
  return (
    typeof stored.stored_at === "number" &&
    Number.isFinite(stored.stored_at) &&
    isOpenAIUsageSnapshot(stored.snapshot)
  );
}

function isOpenAIUsageSnapshot(value: unknown): value is OpenAIUsageSnapshot {
  if (!value || typeof value !== "object") {
    return false;
  }
  const snapshot = value as Partial<OpenAIUsageSnapshot>;
  return (
    typeof snapshot.start_time === "number" &&
    typeof snapshot.end_time === "number" &&
    typeof snapshot.total_tokens === "number" &&
    Array.isArray(snapshot.buckets) &&
    Array.isArray(snapshot.limits)
  );
}

type OpenAIUsageMode = "day" | "week";
type OfficialOpenAIUsageDay = {
  date: Date;
  dateKey: string;
  inputTokens: number;
  outputTokens: number;
  requests: number;
  startTime: number;
  totalTokens: number;
};
type OfficialOpenAIUsageChartColumn = {
  date: Date;
  days: Array<OfficialOpenAIUsageDay | null>;
  endTime: number;
  inputTokens: number;
  index: number;
  key: string;
  outputTokens: number;
  requests: number;
  startTime: number;
  totalTokens: number;
};
type OfficialOpenAIUsageChartCell = {
  column: OfficialOpenAIUsageChartColumn;
  columnKey: string;
  day: OfficialOpenAIUsageDay | null;
  filled: boolean;
  intensity: number;
  key: string;
  mode: OpenAIUsageMode;
  rowIndex: number;
  selectionKey: string;
  value: number;
};
type OfficialOpenAIUsageTooltipState = {
  cell: OfficialOpenAIUsageChartCell;
  cursorX: number;
  cursorY: number;
  hostWidth: number;
};

function UsageMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid min-w-0 place-items-center rounded-inner bg-surface px-2 py-1.5 text-center shadow-control">
      <div className="text-[9px] font-semibold uppercase leading-3 text-slate-500">{label}</div>
      <div className="mt-0.5 font-semibold leading-4 text-ink">{value}</div>
    </div>
  );
}

export function OfficialOpenAIUsageLimitBars({
  busy,
  limits,
}: {
  busy: boolean;
  limits: OpenAIUsageLimit[];
}) {
  const { i18n, t } = useTranslation();
  const locale = resolvedUsageLocale(i18n.language || "en-US");
  const visibleLimits = preferredOpenAIUsageLimits(limits);
  const renderedLimits = visibleLimits.length ? visibleLimits : OPENAI_USAGE_LIMIT_PLACEHOLDERS;
  const usingPlaceholders = !visibleLimits.length;

  return (
    <div className="grid w-[252px] shrink-0 grid-cols-2 gap-2">
      {renderedLimits.map((limit) => {
        const label = usageLimitPeriodLabel(limit, t as Translate);
        const endTime = usingPlaceholders
          ? busy
            ? t("providers.limitRefreshing")
            : t("providers.limitEndUnknown")
          : formatUsageLimitEnd(limit.resets_at, locale, t as Translate);
        const percent = usingPlaceholders ? null : remainingPercent(limit);
        const value =
          percent === null
            ? busy
              ? t("providers.limitRefreshing")
              : t("providers.limitEndUnknown")
            : t("providers.limitRemainingPercent", { percent: Math.round(percent) });
        return (
          <div
            key={limit.key}
            className="min-w-0 rounded-control bg-surface px-2 py-1.5 shadow-control"
            title={`${label} · ${value} · ${endTime}`}
            aria-label={
              percent === null
                ? t("providers.limitPendingAria", { label, endTime })
                : t("providers.limitRemainingAria", {
                    label,
                    percent: Math.round(percent),
                    endTime,
                  })
            }
          >
            <div className="flex min-w-0 items-baseline justify-between gap-2">
              <span className="whitespace-nowrap text-[10px] font-semibold leading-3 text-ink">{label}</span>
              <span
                className={cx(
                  "shrink-0 whitespace-nowrap text-[11px] font-bold leading-3",
                  percent === null ? "text-slate-400" : "text-emerald-700",
                )}
              >
                {value}
              </span>
            </div>
            <div className="mt-0.5 whitespace-nowrap text-[9px] font-medium leading-3 text-slate-400">{endTime}</div>
            <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-slate-200">
              <div
                className={cx(
                  "h-full rounded-full transition-[width] duration-200 ease-out",
                  percent === null ? "w-full bg-slate-300/70" : "bg-emerald-500",
                  percent === null && busy && "animate-pulse",
                )}
                style={percent === null ? undefined : { width: `${percent}%` }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function OfficialOpenAIUsagePanel({
  busy,
  error,
  snapshot,
  usageHidden,
}: {
  busy: boolean;
  error: string | null;
  snapshot: OpenAIUsageSnapshot | null;
  usageHidden: boolean;
}) {
  const { i18n, t } = useTranslation();
  const locale = resolvedUsageLocale(i18n.language || "en-US");
  const [mode, setMode] = useState<OpenAIUsageMode>("day");
  const [hoveredUsageCell, setHoveredUsageCell] = useState<OfficialOpenAIUsageTooltipState | null>(null);
  const [selectedUsageCellKey, setSelectedUsageCellKey] = useState<string | null>(null);
  const [chartHostRef, chartContentWidth] = useElementContentWidth<HTMLDivElement>([usageHidden, Boolean(snapshot)]);
  const visibleUsageColumnCount = responsiveUsageColumnCount(chartContentWidth);
  const days = useMemo(
    () => buildOfficialOpenAIUsageDays(snapshot, visibleUsageColumnCount),
    [snapshot, visibleUsageColumnCount],
  );
  const chart = useMemo(
    () => buildOfficialOpenAIUsageChart(days, mode, visibleUsageColumnCount),
    [days, mode, visibleUsageColumnCount],
  );
  const streaks = useMemo(() => usageStreaks(days), [days]);
  const peakTokens = snapshot?.peak_daily_tokens ?? days.reduce((peak, day) => Math.max(peak, day.totalTokens), 0);
  const currentStreak = snapshot?.current_streak_days ?? streaks.current;
  const longestStreak = snapshot?.longest_streak_days ?? streaks.longest;
  const modeOptions: Array<{ label: string; value: OpenAIUsageMode }> = [
    { label: t("usage.day"), value: "day" },
    { label: t("usage.week"), value: "week" },
  ];
  const selectedUsageColumnKey = selectedUsageCellKey?.startsWith("week-") ? selectedUsageCellKey : null;
  const hoveredUsageColumnKey = hoveredUsageCell?.cell.mode === "week" ? hoveredUsageCell.cell.columnKey : null;
  const highlightedUsageCellKey = hoveredUsageCell?.cell.selectionKey ?? selectedUsageCellKey;

  useEffect(() => {
    setHoveredUsageCell(null);
    setSelectedUsageCellKey(null);
  }, [mode, snapshot]);

  function activateUsageCell(event: ReactPointerEvent<HTMLButtonElement>, cell: OfficialOpenAIUsageChartCell) {
    const host = event.currentTarget.closest("[data-openai-usage-chart]");
    if (!(host instanceof HTMLElement)) {
      return;
    }
    const hostRect = host.getBoundingClientRect();
    setHoveredUsageCell({
      cell,
      cursorX: event.clientX - hostRect.left,
      cursorY: event.clientY - hostRect.top,
      hostWidth: hostRect.width,
    });
  }

  function focusUsageCell(event: ReactFocusEvent<HTMLButtonElement>, cell: OfficialOpenAIUsageChartCell) {
    const host = event.currentTarget.closest("[data-openai-usage-chart]");
    if (!(host instanceof HTMLElement)) {
      return;
    }
    const hostRect = host.getBoundingClientRect();
    const cellRect = event.currentTarget.getBoundingClientRect();
    setHoveredUsageCell({
      cell,
      cursorX: cellRect.left - hostRect.left + cellRect.width / 2,
      cursorY: cellRect.top - hostRect.top,
      hostWidth: hostRect.width,
    });
  }

  if (usageHidden) {
    return null;
  }

  return (
    <section className="grid gap-3 rounded-inner bg-panel-soft p-3 shadow-hairline">
      <div className="flex min-w-0 items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <h3 className="truncate text-sm font-semibold text-ink">{t("providers.openaiUsage")}</h3>
        </div>
        <div className="flex shrink-0 rounded-full bg-surface p-0.5 shadow-control">
          {modeOptions.map((option) => (
            <button
              key={option.value}
              type="button"
              className={cx(
                "focus-ring h-6 rounded-full px-2 text-[11px] font-semibold transition-[background-color,color]",
                mode === option.value ? "bg-ink text-white" : "text-slate-500 hover:bg-panel hover:text-ink",
              )}
              onClick={() => setMode(option.value)}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      {error ? (
        <div className="rounded-inner bg-amber-50 px-3 py-2 text-xs font-medium text-amber-800 shadow-hairline">
          {error}
        </div>
      ) : busy && !snapshot ? (
        <OfficialOpenAIUsageSkeleton label={t("providers.loadingOpenAIUsage")} />
      ) : (
        <>
          <div className="grid grid-cols-5 gap-2 text-xs">
            <UsageMetric
              label={t("gateway.tokens")}
              value={snapshot ? formatUsageNumber(snapshot.total_tokens, locale) : t("common.unknown")}
            />
            <UsageMetric
              label={t("providers.peakDayTokens")}
              value={snapshot ? formatUsageNumber(peakTokens, locale) : t("common.unknown")}
            />
            <UsageMetric
              label={t("providers.longestTaskDuration")}
              value={snapshot ? formatUsageDuration(snapshot.longest_running_turn_sec, locale, t as Translate) : t("common.unknown")}
            />
            <UsageMetric
              label={t("providers.currentStreak")}
              value={snapshot ? t("providers.daysCount", { count: currentStreak }) : t("common.unknown")}
            />
            <UsageMetric
              label={t("providers.longestStreak")}
              value={snapshot ? t("providers.daysCount", { count: longestStreak }) : t("common.unknown")}
            />
          </div>

          <div
            ref={chartHostRef}
            className="relative min-w-0 overflow-visible rounded-inner bg-surface px-3 py-2 shadow-control"
            data-openai-usage-chart
            onPointerLeave={() => setHoveredUsageCell(null)}
          >
            {snapshot && days.length ? (
              <div className="overflow-hidden">
                <div
                  className="grid"
                  role="img"
                  aria-label={t("providers.openaiUsageActivity")}
                  style={{
                    gridAutoFlow: "column",
                    gridTemplateColumns: `repeat(${Math.max(1, chart.columns.length)}, ${OFFICIAL_USAGE_CELL_SIZE}px)`,
                    gridTemplateRows: `repeat(7, ${OFFICIAL_USAGE_CELL_SIZE}px)`,
                    gap: `${OFFICIAL_USAGE_CELL_GAP}px`,
                    height: usageGridHeight(),
                    width: usageGridWidth(chart.columns.length),
                  }}
                >
                  {chart.cells.map((cell, index) => {
                    if (!cell) {
                      return <span key={`empty-${index}`} className="h-full w-full" />;
                    }
                    const highlighted =
                      cell.mode === "week"
                        ? cell.columnKey === (hoveredUsageColumnKey ?? selectedUsageColumnKey)
                        : cell.selectionKey === highlightedUsageCellKey;
                    return (
                      <button
                        key={cell.key}
                        type="button"
                        className={cx(
                          "focus-ring h-full w-full rounded-[3px] border-0 p-0 hover:brightness-[0.97]",
                          highlighted && "ring-1 ring-action/20 brightness-[0.96]",
                        )}
                        style={{ backgroundColor: usageCellColor(cell.intensity, cell.filled) }}
                        aria-label={formatUsageCellLabel(cell, locale, t as Translate)}
                        onPointerEnter={(event) => activateUsageCell(event, cell)}
                        onPointerMove={(event) => activateUsageCell(event, cell)}
                        onFocus={(event) => focusUsageCell(event, cell)}
                        onBlur={() => setHoveredUsageCell(null)}
                        onClick={() => setSelectedUsageCellKey(cell.selectionKey)}
                      />
                    );
                  })}
                </div>
                <div
                  className="relative mt-1 h-4 text-[10px] text-slate-400"
                  style={{ width: usageGridWidth(chart.columns.length) }}
                >
                  {usageMonthLabels(chart.columns, locale, usageGridWidth(chart.columns.length)).map((label) => (
                    <span
                      key={label.key}
                      data-openai-usage-month-label
                      className={cx(
                        "absolute top-0 truncate",
                        label.align === "start" && "translate-x-0",
                        label.align === "center" && "-translate-x-1/2",
                        label.align === "end" && "-translate-x-full",
                      )}
                      style={{ left: `${label.leftPercent}%` }}
                    >
                      {label.label}
                    </span>
                  ))}
                </div>
                <OfficialOpenAIUsageTooltip tooltip={hoveredUsageCell} locale={locale} t={t as Translate} />
              </div>
            ) : (
              <div className="grid min-h-[82px] place-items-center text-xs font-medium text-slate-500">
                {busy ? t("providers.loadingOpenAIUsage") : t("providers.openaiUsageNoData")}
              </div>
            )}
          </div>
        </>
      )}
    </section>
  );
}

function OfficialOpenAIUsageSkeleton({ label }: { label: string }) {
  const columns = 42;
  const cells = Array.from({ length: columns * 7 }, (_, index) => index);

  return (
    <div className="grid gap-3 animate-pulse" role="status" aria-label={label}>
      <div className="grid grid-cols-5 gap-2 text-xs" aria-hidden="true">
        {Array.from({ length: 5 }, (_, index) => (
          <div
            key={`metric-${index}`}
            className="grid min-w-0 place-items-center rounded-inner bg-surface px-2 py-1.5 shadow-control"
          >
            <span className="h-2 w-10 rounded-full bg-slate-200" />
            <span className={cx("mt-2 h-3 rounded-full bg-slate-200", index === 0 ? "w-12" : "w-9")} />
          </div>
        ))}
      </div>
      <div className="min-w-0 overflow-hidden rounded-inner bg-surface px-3 py-2 shadow-control" aria-hidden="true">
        <div
          className="grid"
          style={{
            gridAutoFlow: "column",
            gridTemplateColumns: `repeat(${columns}, ${OFFICIAL_USAGE_CELL_SIZE}px)`,
            gridTemplateRows: `repeat(7, ${OFFICIAL_USAGE_CELL_SIZE}px)`,
            gap: `${OFFICIAL_USAGE_CELL_GAP}px`,
            height: usageGridHeight(),
            width: usageGridWidth(columns),
          }}
        >
          {cells.map((index) => (
            <span
              key={`cell-${index}`}
              className={cx(
                "h-full w-full rounded-[3px] bg-slate-200",
                index % 11 === 0 && "bg-slate-300/80",
                index % 17 === 0 && "bg-slate-300",
              )}
            />
          ))}
        </div>
        <div className="mt-2 flex gap-5">
          {Array.from({ length: 6 }, (_, index) => (
            <span key={`month-${index}`} className="h-2 w-7 rounded-full bg-slate-200" />
          ))}
        </div>
      </div>
    </div>
  );
}

function OfficialOpenAIUsageTooltip({
  locale,
  t,
  tooltip,
}: {
  locale: string;
  t: Translate;
  tooltip: OfficialOpenAIUsageTooltipState | null;
}) {
  if (!tooltip) {
    return null;
  }
  const { cell } = tooltip;
  const isWeek = cell.mode === "week";
  const tooltipWidth = Math.min(184, Math.max(148, tooltip.hostWidth - 16));
  const left = Math.min(
    Math.max(tooltipWidth / 2 + 8, tooltip.cursorX),
    Math.max(tooltipWidth / 2 + 8, tooltip.hostWidth - tooltipWidth / 2 - 8),
  );
  const top = isWeek ? -8 : tooltip.cursorY - 8;
  const title = isWeek
    ? formatUsageDateRange(cell.column.startTime, cell.column.endTime, locale)
    : formatUsageDate(cell.day?.date ?? cell.column.date, locale);
  const tokens = isWeek ? cell.column.totalTokens : cell.value;

  return (
    <div
      className="pointer-events-none absolute z-20 rounded-inner bg-surface px-2.5 py-1.5 text-center text-xs font-medium text-ink shadow-floating"
      style={{ left, top, width: tooltipWidth, transform: "translate(-50%, -100%)" }}
    >
      <span className="block whitespace-nowrap">
        {t("providers.openaiUsageTooltipCompact", { date: title, tokens: formatUsageNumber(tokens, locale) })}
      </span>
    </div>
  );
}

function preferredOpenAIUsageLimits(limits: OpenAIUsageLimit[]) {
  const usable = limits.filter(limitHasUsageData);
  const fiveHour = usable.find(isFiveHourUsageLimit);
  const weekly = usable.find(isWeeklyUsageLimit);
  const selected = [fiveHour, weekly].filter((limit): limit is OpenAIUsageLimit => Boolean(limit));
  for (const limit of usable) {
    if (selected.length >= 2) {
      break;
    }
    if (!selected.some((item) => item.key === limit.key)) {
      selected.push(limit);
    }
  }
  return selected.slice(0, 2);
}

function limitHasUsageData(limit: OpenAIUsageLimit) {
  return (
    finiteUsageNumber(limit.limit) !== null ||
    finiteUsageNumber(limit.used) !== null ||
    finiteUsageNumber(limit.remaining) !== null ||
    Boolean(limit.resets_at?.trim())
  );
}

function isFiveHourUsageLimit(limit: OpenAIUsageLimit) {
  const value = usageLimitSearchText(limit);
  return (
    /\b5\s*h(?:our)?s?\b/.test(value) ||
    /\bfive[-_\s]?h(?:our)?s?\b/.test(value) ||
    ((value.includes("5") || value.includes("five")) && value.includes("hour")) ||
    /\bprimary\b/.test(value)
  );
}

function isWeeklyUsageLimit(limit: OpenAIUsageLimit) {
  const value = usageLimitSearchText(limit);
  return value.includes("week") || value.includes("weekly") || /\bsecondary\b/.test(value);
}

function usageLimitSearchText(limit: OpenAIUsageLimit) {
  return `${limit.key} ${limit.period} ${limit.name}`.trim().toLowerCase().replace(/_/g, " ");
}

function usageLimitPeriodLabel(limit: OpenAIUsageLimit, t: Translate) {
  if (isFiveHourUsageLimit(limit)) {
    return t("providers.fiveHourLimit");
  }
  if (isWeeklyUsageLimit(limit)) {
    return t("providers.weeklyLimit");
  }
  return limit.name?.trim() || limit.period?.trim() || limit.key;
}

function remainingPercent(limit: OpenAIUsageLimit) {
  const total = finiteUsageNumber(limit.limit);
  const used = finiteUsageNumber(limit.used);
  const explicitRemaining = finiteUsageNumber(limit.remaining);
  const remaining =
    explicitRemaining !== null
      ? explicitRemaining
      : total !== null && used !== null
        ? total - used
        : null;
  if (total === null || total <= 0 || remaining === null) {
    return 0;
  }
  return Math.max(0, Math.min(100, (remaining / total) * 100));
}

function finiteUsageNumber(value?: number | null) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function formatUsageLimitEnd(value: string | null | undefined, locale: string, t: Translate) {
  const date = parseUsageLimitEnd(value);
  if (!date) {
    return value?.trim() || t("providers.limitEndUnknown");
  }
  return new Intl.DateTimeFormat(resolvedUsageLocale(locale), {
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    month: "short",
  }).format(date);
}

function parseUsageLimitEnd(value: string | null | undefined) {
  const trimmed = value?.trim();
  if (!trimmed) {
    return null;
  }
  const numeric = Number(trimmed);
  if (Number.isFinite(numeric) && /^\d+(?:\.\d+)?$/.test(trimmed)) {
    return new Date(numeric < 10_000_000_000 ? numeric * 1000 : numeric);
  }
  const parsed = Date.parse(trimmed);
  return Number.isNaN(parsed) ? null : new Date(parsed);
}

export function defaultOfficialOpenAIUsageWindow() {
  const endTime = Math.floor(Date.now() / 1000);
  return {
    startTime: endTime - (OPENAI_USAGE_QUERY_WINDOW_DAYS - 1) * OPENAI_USAGE_DAY_SECONDS,
    endTime,
  };
}

function buildOfficialOpenAIUsageDays(
  snapshot: OpenAIUsageSnapshot | null,
  visibleColumnCount: number,
): OfficialOpenAIUsageDay[] {
  if (!snapshot || snapshot.start_time >= snapshot.end_time) {
    return [];
  }
  const endDay = localDayStartSeconds(Math.max(snapshot.start_time, snapshot.end_time - 1));
  const displayWindowDays = Math.max(
    OPENAI_USAGE_MIN_WINDOW_DAYS,
    visibleColumnCount * 7,
  );
  const startDay = addLocalDays(endDay, -(displayWindowDays - 1));
  const totals = new Map<string, Omit<OfficialOpenAIUsageDay, "date" | "dateKey" | "startTime">>();
  for (const bucket of snapshot.buckets) {
    const bucketDate = bucket.date ? parseLocalUsageDate(bucket.date) : new Date(bucket.start_time * 1000);
    if (!bucketDate) {
      continue;
    }
    const day = localDayStartSeconds(bucketDate);
    if (day < startDay || day > endDay) {
      continue;
    }
    const dateKey = localDateKey(bucketDate);
    const current = totals.get(dateKey) ?? {
      inputTokens: 0,
      outputTokens: 0,
      requests: 0,
      totalTokens: 0,
    };
    totals.set(dateKey, {
      inputTokens: current.inputTokens + bucket.input_tokens,
      outputTokens: current.outputTokens + bucket.output_tokens,
      requests: current.requests + bucket.num_model_requests,
      totalTokens: current.totalTokens + bucket.total_tokens,
    });
  }

  const days: OfficialOpenAIUsageDay[] = [];
  for (let time = startDay; time <= endDay; time = addLocalDays(time, 1)) {
    const date = new Date(time * 1000);
    const dateKey = localDateKey(date);
    const total = totals.get(dateKey) ?? {
      inputTokens: 0,
      outputTokens: 0,
      requests: 0,
      totalTokens: 0,
    };
    days.push({
      date,
      dateKey,
      startTime: time,
      ...total,
    });
  }
  return days;
}

function buildOfficialOpenAIUsageChart(
  days: OfficialOpenAIUsageDay[],
  mode: OpenAIUsageMode,
  visibleColumnCount: number,
) {
  const allColumns = buildOfficialOpenAIUsageWeekColumns(days);
  const columns = visibleUsageColumns(allColumns, visibleColumnCount);
  if (mode === "week") {
    const maxWeekTotal = Math.max(1, ...allColumns.map((column) => column.totalTokens));
    const cells = columns.flatMap((column) => {
      const intensity = column.totalTokens > 0 ? Math.max(0.18, Math.min(1, column.totalTokens / maxWeekTotal)) : 0;
      const filledRows = column.totalTokens > 0 ? Math.max(1, Math.ceil(intensity * 7)) : 0;
      return column.days.map((day, rowIndex): OfficialOpenAIUsageChartCell => {
        const filled = filledRows > 0 && rowIndex >= 7 - filledRows;
        return {
          column,
          columnKey: column.key,
          day,
          filled,
          intensity: filled ? intensity : 0,
          key: `${column.key}-row-${rowIndex}`,
          mode,
          rowIndex,
          selectionKey: column.key,
          value: column.totalTokens,
        };
      });
    });
    return { cells, columns };
  }

  const maxDayTotal = Math.max(1, ...days.map((day) => day.totalTokens));
  const cells = columns.flatMap((column) =>
    column.days.map((day, rowIndex): OfficialOpenAIUsageChartCell | null => {
      if (!day) {
        return null;
      }
      const intensity = day.totalTokens > 0 ? Math.max(0.18, Math.min(1, day.totalTokens / maxDayTotal)) : 0;
      return {
        column,
        columnKey: column.key,
        day,
        filled: day.totalTokens > 0,
        intensity,
        key: `day-${day.startTime}`,
        mode,
        rowIndex,
        selectionKey: `day-${day.startTime}`,
        value: day.totalTokens,
      };
    }),
  );
  return { cells, columns };
}

function responsiveUsageColumnCount(contentWidth: number) {
  const minimumColumns = Math.ceil(OPENAI_USAGE_MIN_WINDOW_DAYS / 7);
  if (contentWidth <= 0) {
    return minimumColumns;
  }
  return Math.max(
    1,
    Math.floor((contentWidth + OFFICIAL_USAGE_CELL_GAP) / (OFFICIAL_USAGE_CELL_SIZE + OFFICIAL_USAGE_CELL_GAP)),
  );
}

function visibleUsageColumns(columns: OfficialOpenAIUsageChartColumn[], visibleColumnCount: number) {
  const start = Math.max(0, columns.length - visibleColumnCount);
  return columns.slice(start).map((column, index) => ({ ...column, index }));
}

function usageGridWidth(columnCount: number) {
  if (columnCount <= 0) {
    return 0;
  }
  return columnCount * OFFICIAL_USAGE_CELL_SIZE + (columnCount - 1) * OFFICIAL_USAGE_CELL_GAP;
}

function usageGridHeight() {
  return 7 * OFFICIAL_USAGE_CELL_SIZE + 6 * OFFICIAL_USAGE_CELL_GAP;
}

function buildOfficialOpenAIUsageWeekColumns(days: OfficialOpenAIUsageDay[]): OfficialOpenAIUsageChartColumn[] {
  if (!days.length) {
    return [];
  }
  const leadingBlanks = mondayWeekdayIndex(days[0].date);
  const rawSlots: Array<OfficialOpenAIUsageDay | null> = [
    ...Array.from({ length: leadingBlanks }, () => null),
    ...days,
  ];
  const trailingBlanks = (7 - (rawSlots.length % 7)) % 7;
  const slots = [...rawSlots, ...Array.from({ length: trailingBlanks }, () => null)];
  const columns: OfficialOpenAIUsageChartColumn[] = [];
  for (let index = 0; index < slots.length; index += 7) {
    const weekSlots = slots.slice(index, index + 7);
    const actualDays = weekSlots.filter((day): day is OfficialOpenAIUsageDay => Boolean(day));
    const firstDay = actualDays[0] ?? days[0];
    const weekStart = addLocalDays(firstDay.startTime, -mondayWeekdayIndex(firstDay.date));
    const totals = actualDays.reduce(
      (sum, day) => ({
        inputTokens: sum.inputTokens + day.inputTokens,
        outputTokens: sum.outputTokens + day.outputTokens,
        requests: sum.requests + day.requests,
        totalTokens: sum.totalTokens + day.totalTokens,
      }),
      { inputTokens: 0, outputTokens: 0, requests: 0, totalTokens: 0 },
    );
    columns.push({
      date: new Date(weekStart * 1000),
      days: weekSlots,
      endTime: addLocalDays(weekStart, 6),
      index: columns.length,
      key: `week-${weekStart}`,
      startTime: weekStart,
      ...totals,
    });
  }
  return columns;
}

function usageStreaks(days: OfficialOpenAIUsageDay[]) {
  let currentRun = 0;
  let longest = 0;
  let run = 0;
  for (const day of days) {
    if (day.totalTokens > 0) {
      run += 1;
      longest = Math.max(longest, run);
    } else {
      run = 0;
    }
  }
  for (let index = days.length - 1; index >= 0; index -= 1) {
    if (days[index].totalTokens <= 0) {
      break;
    }
    currentRun += 1;
  }
  return { current: currentRun, longest };
}

function usageMonthLabels(columns: OfficialOpenAIUsageChartColumn[], locale: string, gridWidth: number) {
  const timeZone = localUsageTimeZone();
  const formatter = new Intl.DateTimeFormat(resolvedUsageLocale(locale), {
    month: "short",
    ...(timeZone ? { timeZone } : {}),
  });
  const labels: Array<{ align: "start" | "center" | "end"; key: string; label: string; leftPercent: number }> = [];
  let previous = "";
  for (const column of columns) {
    for (const day of column.days) {
      if (!day) {
        continue;
      }
      const key = `${day.date.getFullYear()}-${day.date.getMonth()}`;
      if (key === previous) {
        continue;
      }
      previous = key;
      const leftPercent = columns.length <= 1 ? 0 : (column.index / (columns.length - 1)) * 100;
      labels.push({
        align: leftPercent <= 3 ? "start" : leftPercent >= 97 ? "end" : "center",
        key,
        label: formatter.format(day.date),
        leftPercent,
      });
    }
  }
  return filterCrowdedUsageMonthLabels(labels, gridWidth);
}

function filterCrowdedUsageMonthLabels<
  TLabel extends { leftPercent: number },
>(labels: TLabel[], gridWidth: number) {
  if (labels.length <= 1 || gridWidth <= 0) {
    return labels;
  }

  const filtered: TLabel[] = [];
  for (let index = 0; index < labels.length; index += 1) {
    const label = labels[index];
    const currentLeftPx = (label.leftPercent / 100) * gridWidth;
    const next = labels[index + 1];
    if (next) {
      const nextLeftPx = (next.leftPercent / 100) * gridWidth;
      if (nextLeftPx - currentLeftPx < USAGE_MONTH_LABEL_MIN_GAP_PX) {
        continue;
      }
    }
    const previous = filtered[filtered.length - 1];
    if (previous) {
      const previousLeftPx = (previous.leftPercent / 100) * gridWidth;
      if (currentLeftPx - previousLeftPx < USAGE_MONTH_LABEL_MIN_GAP_PX) {
        continue;
      }
    }
    filtered.push(label);
  }
  return filtered;
}

function usageCellColor(intensity: number, filled = true) {
  if (!filled || intensity <= 0) {
    return OFFICIAL_USAGE_COLOR_STOPS[0];
  }
  const index = Math.min(
    OFFICIAL_USAGE_COLOR_STOPS.length - 1,
    Math.max(1, Math.ceil(Math.min(1, intensity) * (OFFICIAL_USAGE_COLOR_STOPS.length - 1))),
  );
  return OFFICIAL_USAGE_COLOR_STOPS[index];
}

function resolvedUsageLocale(locale: string) {
  const normalized = locale.replace(/_/g, "-").toLowerCase();
  return normalized === "zh" || normalized.startsWith("zh-") ? "zh-CN" : "en-US";
}

function formatUsageCellLabel(cell: OfficialOpenAIUsageChartCell, locale: string, t: Translate) {
  if (cell.mode === "week") {
    return `${formatUsageDateRange(cell.column.startTime, cell.column.endTime, locale)}: ${formatUsageNumber(cell.column.totalTokens, locale)} ${t("gateway.tokens")}`;
  }
  const date = cell.day?.date ?? cell.column.date;
  return `${formatUsageDate(date, locale)}: ${formatUsageNumber(cell.value, locale)} ${t("gateway.tokens")}`;
}

function formatUsageDateRange(startTime: number, endTime: number, locale: string) {
  return `${formatUsageDate(new Date(startTime * 1000), locale)} - ${formatUsageDate(new Date(endTime * 1000), locale)}`;
}

function formatUsageNumber(value: number, locale: string) {
  return new Intl.NumberFormat(locale, {
    maximumFractionDigits: value >= 10_000 ? 1 : 0,
    notation: value >= 10_000 ? "compact" : "standard",
  }).format(value);
}

function formatUsageDuration(seconds: number | null | undefined, locale: string, t: Translate) {
  if (seconds == null) {
    return t("common.unknown");
  }
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  const formatter = new Intl.NumberFormat(locale, { maximumFractionDigits: 0 });
  if (days > 0) {
    return `${formatter.format(days)} ${t("providers.daysShort")} ${formatter.format(hours)} ${t("providers.hoursShort")}`;
  }
  if (hours > 0) {
    return `${formatter.format(hours)} ${t("providers.hoursShort")} ${formatter.format(minutes)} ${t("providers.minutesShort")}`;
  }
  return `${formatter.format(Math.max(1, minutes))} ${t("providers.minutesShort")}`;
}

function formatUsageDate(date: Date, locale: string) {
  const timeZone = localUsageTimeZone();
  return new Intl.DateTimeFormat(resolvedUsageLocale(locale), {
    day: "numeric",
    month: "short",
    ...(timeZone ? { timeZone } : {}),
  }).format(date);
}

function localDateKey(date: Date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function localUsageTimeZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;
  } catch {
    return undefined;
  }
}

function parseLocalUsageDate(value: string) {
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match) {
    return null;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  return new Date(year, month - 1, day);
}

function localDayStartSeconds(value: number | Date) {
  const date = value instanceof Date ? value : new Date(value * 1000);
  return Math.floor(new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime() / 1000);
}

function addLocalDays(timestamp: number, days: number) {
  const date = new Date(timestamp * 1000);
  date.setDate(date.getDate() + days);
  return Math.floor(date.getTime() / 1000);
}

function mondayWeekdayIndex(date: Date) {
  return (date.getDay() + 6) % 7;
}
