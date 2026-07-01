import { PendingPanel } from "./PendingPanel";

interface StackedUsageChartShellProps {
  pendingMessage: string;
}

export function StackedUsageChartShell({ pendingMessage }: StackedUsageChartShellProps) {
  return (
    <section className="grid gap-4 rounded-md border border-line bg-white p-4 shadow-subtle">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-semibold text-ink">Usage &amp; Cost</h2>
        <div className="flex flex-wrap items-center gap-2">
          {["7D", "1M", "Custom"].map((label, index) => (
            <button
              key={label}
              type="button"
              className="h-8 rounded-md border border-line bg-panel px-3 text-xs font-semibold text-slate-500"
              disabled
              aria-pressed={index === 0}
            >
              {label}
            </button>
          ))}
          <button
            type="button"
            className="h-8 rounded-md border border-line bg-panel px-3 text-xs font-semibold text-slate-500"
            disabled
          >
            Group Day
          </button>
        </div>
      </div>

      <div className="grid gap-2 sm:grid-cols-4">
        {["Tokens", "Requests", "Est. cost", "Cache hit"].map((metric) => (
          <div key={metric} className="rounded-md border border-line bg-panel p-3">
            <div className="text-[11px] font-semibold uppercase tracking-[0.04em] text-slate-500">
              {metric}
            </div>
            <div className="mt-2 font-mono text-base font-semibold text-slate-500">Unknown</div>
          </div>
        ))}
      </div>

      <div className="relative min-h-[170px] overflow-hidden rounded-md border border-line bg-panel">
        <svg
          viewBox="0 0 640 180"
          className="absolute inset-0 h-full w-full text-slate-200"
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          <path d="M0 140H640M0 100H640M0 60H640M80 0V180M240 0V180M400 0V180M560 0V180" stroke="currentColor" strokeWidth="1" />
          <path d="M40 145C120 126 185 130 250 112C330 91 390 108 470 82C535 61 585 67 620 54" fill="none" stroke="#cbd5e1" strokeWidth="2" strokeDasharray="5 6" />
        </svg>
        <div className="absolute inset-x-4 bottom-3 flex justify-between font-mono text-[11px] text-slate-400">
          <span>start</span>
          <span>middle</span>
          <span>today</span>
        </div>
        <div className="relative z-10 grid h-full min-h-[170px] place-items-center p-5">
          <PendingPanel title="Usage telemetry" message={pendingMessage} />
        </div>
      </div>
    </section>
  );
}
