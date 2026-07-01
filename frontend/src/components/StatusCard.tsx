import { AlertTriangle, CheckCircle2, Circle } from "lucide-react";
import { cx } from "../lib/format";

type StatusTone = "ok" | "warn" | "danger" | "idle";

interface StatusCardProps {
  detail?: string;
  label: string;
  tone?: StatusTone;
  value: string;
}

export function StatusCard({ detail, label, tone = "idle", value }: StatusCardProps) {
  const Icon = tone === "ok" ? CheckCircle2 : tone === "idle" ? Circle : AlertTriangle;
  return (
    <div className="grid min-h-[104px] content-between gap-3 rounded-md border border-line bg-panel p-4">
      <div className="flex items-center justify-between gap-3">
        <span className="truncate text-[11px] font-semibold uppercase tracking-[0.04em] text-slate-500">
          {label}
        </span>
        <Icon
          size={15}
          className={cx(
            tone === "ok" && "text-ok",
            tone === "warn" && "text-warn",
            tone === "danger" && "text-danger",
            tone === "idle" && "text-slate-400",
          )}
        />
      </div>
      <div>
        <div className="truncate text-base font-semibold text-ink">{value}</div>
        {detail && <div className="mt-1 truncate text-xs text-slate-500">{detail}</div>}
      </div>
    </div>
  );
}
