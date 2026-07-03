import { Copy } from "lucide-react";
import { cx } from "../lib/format";

interface EndpointRowProps {
  compact?: boolean;
  label: string;
  meta: string;
  value: string;
  onCopy: () => void;
}

export function EndpointRow({ compact, label, meta, onCopy, value }: EndpointRowProps) {
  return (
    <div
      className={cx(
        "grid gap-2 rounded-md border border-line bg-white text-sm lg:items-center",
        compact
          ? "min-h-9 px-2 py-0.5 lg:grid-cols-[122px_minmax(0,1fr)_auto]"
          : "px-3 py-2 lg:grid-cols-[130px_minmax(0,1fr)_auto]",
      )}
    >
      <div className="min-w-0">
        <div className={cx("truncate font-semibold text-ink", compact && "leading-4")}>{label}</div>
        <div className={cx("truncate text-slate-500", compact ? "text-[10px] leading-3" : "text-[11px]")}>
          {meta}
        </div>
      </div>
      <code className="truncate font-mono text-xs text-slate-600">{value}</code>
      <button
        type="button"
        className={cx(
          "focus-ring inline-flex items-center justify-center gap-1 rounded-md border border-line bg-panel px-2 text-xs font-semibold text-slate-700 hover:bg-slate-100",
          compact ? "h-7" : "h-8",
        )}
        onClick={onCopy}
      >
        <Copy size={13} />
        Copy
      </button>
    </div>
  );
}
