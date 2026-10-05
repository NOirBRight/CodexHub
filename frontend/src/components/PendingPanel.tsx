import { Clock3 } from "lucide-react";
import { useTranslation } from "react-i18next";
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
  const { t } = useTranslation();
  const resolvedLabel = label === "pending backend" ? t("usage.pendingData") : label;
  return (
    <div
      className={cx(
        "rounded-inner bg-panel-soft text-muted shadow-hairline",
        compact ? "px-3 py-2" : "p-4",
        className,
      )}
    >
      <div className="flex items-start gap-2">
        <Clock3 size={15} className="mt-0.5 shrink-0 text-muted" />
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate text-sm font-semibold text-ink">{title}</span>
            <span className="rounded-full bg-surface px-2 py-0.5 text-[11px] font-semibold uppercase tracking-[0.04em] text-muted shadow-control">
              {resolvedLabel}
            </span>
          </div>
          <p className={cx("text-xs leading-5 text-muted", compact ? "mt-0.5" : "mt-1")}>
            {message}
          </p>
        </div>
      </div>
    </div>
  );
}
