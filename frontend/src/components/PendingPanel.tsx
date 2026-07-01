import { Clock3 } from "lucide-react";
import { cx } from "../lib/format";

interface PendingPanelProps {
  className?: string;
  compact?: boolean;
  label?: string;
  message: string;
  title: string;
}

export function PendingPanel({
  className,
  compact,
  label = "pending backend",
  message,
  title,
}: PendingPanelProps) {
  return (
    <div
      className={cx(
        "rounded-md border border-dashed border-line bg-slate-50 text-slate-600",
        compact ? "px-3 py-2" : "p-4",
        className,
      )}
    >
      <div className="flex items-start gap-2">
        <Clock3 size={15} className="mt-0.5 shrink-0 text-slate-400" />
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate text-sm font-semibold text-ink">{title}</span>
            <span className="rounded-sm border border-line bg-white px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-[0.04em] text-slate-500">
              {label}
            </span>
          </div>
          <p className={cx("text-xs leading-5 text-slate-500", compact ? "mt-0.5" : "mt-1")}>
            {message}
          </p>
        </div>
      </div>
    </div>
  );
}
