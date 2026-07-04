import { AlertTriangle, CheckCircle2, Circle } from "lucide-react";
import { cx } from "../lib/format";

type StatusTone = "ok" | "warn" | "danger" | "idle";

interface StatusCardProps {
  compact?: boolean;
  detail?: string;
  label: string;
  tone?: StatusTone;
  value: string;
}

export function StatusCard({ compact, detail, label, tone = "idle", value }: StatusCardProps) {
  const Icon = tone === "ok" ? CheckCircle2 : tone === "idle" ? Circle : AlertTriangle;
  return (
    <div
      className={cx(
        "grid min-w-0 content-between rounded-inner bg-panel-soft shadow-control",
        compact ? "min-h-11 gap-0.5 p-1.5" : "min-h-[104px] gap-3 p-4",
      )}
    >
      <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-1.5">
        <span className="min-w-0 truncate text-[11px] font-semibold uppercase tracking-[0.04em] text-slate-500">
          {label}
        </span>
        <Icon
          size={compact ? 13 : 15}
          className={cx(
            "shrink-0",
            tone === "ok" && "text-ok",
            tone === "warn" && "text-warn",
            tone === "danger" && "text-danger",
            tone === "idle" && "text-slate-400",
          )}
        />
      </div>
      <div className="min-w-0">
        <div className={cx("truncate font-semibold text-ink", compact ? "text-sm" : "text-base")}>
          {value}
        </div>
        {detail && (
        <div className={cx("mt-0.5 truncate text-slate-500", compact ? "text-[11px]" : "text-xs")}>
            {detail}
          </div>
        )}
      </div>
    </div>
  );
}
