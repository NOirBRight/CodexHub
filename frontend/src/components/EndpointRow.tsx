import { Check, Copy } from "lucide-react";
import { useTranslation } from "react-i18next";
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
  const { t } = useTranslation();
  return (
    <div
      className={cx(
        "grid items-center gap-2 rounded-inner bg-surface text-sm shadow-control",
        compact
          ? "h-8 grid-cols-[104px_minmax(0,1fr)_auto] px-2 py-0"
          : "grid-cols-[130px_minmax(0,1fr)_auto] px-3 py-2",
      )}
    >
      <div className="min-w-0">
        <div className={cx("truncate font-semibold text-ink", compact && "text-xs leading-4")}>{label}</div>
        <div className={cx("truncate text-slate-500", compact ? "text-[9px] leading-3" : "text-[11px]")}>
          {meta}
        </div>
      </div>
      <code className={cx("truncate font-mono text-slate-600", compact ? "text-[11px]" : "text-xs")}>{value}</code>
      <button
        type="button"
        className={cx(
          "focus-ring inline-flex shrink-0 items-center justify-center rounded-control bg-panel text-slate-700 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96]",
          compact ? "h-6 w-6" : "h-8 w-8",
        )}
        aria-label={copied ? t("gateway.copyEndpointCopied", { label }) : t("gateway.copyEndpoint", { label })}
        title={copied ? t("common.copied") : t("gateway.copyEndpoint", { label })}
        onClick={onCopy}
      >
        {copied ? <Check size={13} /> : <Copy size={13} />}
      </button>
    </div>
  );
}
