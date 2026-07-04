import { Check, Copy } from "lucide-react";
import { cx } from "../lib/format";

interface EndpointRowProps {
  compact?: boolean;
  copied?: boolean;
  label: string;
  meta: string;
  value: string;
  onCopy: () => void;
}

export function EndpointRow({ compact, copied, label, meta, onCopy, value }: EndpointRowProps) {
  return (
    <div
      className={cx(
        "grid gap-2 rounded-inner bg-surface text-sm shadow-control lg:items-center",
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
          "focus-ring inline-flex shrink-0 items-center justify-center rounded-control bg-panel text-slate-700 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96]",
          compact ? "h-7 w-7" : "h-8 w-8",
        )}
        aria-label={copied ? `${label} copied` : `Copy ${label}`}
        title={copied ? "Copied" : `Copy ${label}`}
        onClick={onCopy}
      >
        {copied ? <Check size={13} /> : <Copy size={13} />}
      </button>
    </div>
  );
}
