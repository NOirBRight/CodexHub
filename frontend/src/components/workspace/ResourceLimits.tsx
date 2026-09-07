import { useTranslation } from "react-i18next";
import type { OpenAIUsageLimit } from "../../lib/types";
import {
  quotaPercent,
  isWeeklyLimit,
  quotaResetDate,
  resourceQuotaLayout,
} from "../../lib/workspaceResources";
export function ResourceLimits({
  limits,
  pending,
  message,
}: {
  limits: OpenAIUsageLimit[];
  pending?: boolean;
  message?: string | null;
}) {
  const { t, i18n } = useTranslation();
  if (message)
    return (
      <span className="ws-muted" title={message}>
        {t("workspace.quotaUnavailable")}
      </span>
    );
  if (!limits.length)
    return (
      <span className="ws-muted">
        {t(pending ? "workspace.loading" : "workspace.quotaUnknown")}
      </span>
    );
  const { ordered, single, weekOnly } = resourceQuotaLayout(limits);
  return (
    <div className="ws-limits" data-single={single} data-week-only={weekOnly}>
      {ordered.map((limit) => {
        const percent = quotaPercent(limit);
        const date = quotaResetDate(limit.resets_at);
        return (
          <div key={limit.key} className="ws-limit">
            <div>
              <span>
                {isWeeklyLimit(limit)
                  ? t("workspace.weekRemaining")
                  : /5h|5.hour/i.test(limit.period + " " + limit.name)
                    ? t("workspace.fiveHourRemaining")
                    : /month/i.test(limit.period + " " + limit.name)
                      ? t("workspace.monthRemaining")
                      : limit.name || limit.period}
              </span>
              <strong>
                {percent === null ? "—" : Math.round(percent)}
                {percent !== null && <small>%</small>}
              </strong>
            </div>
            <div className="ws-meter" aria-hidden="true">
              <i style={{ width: (percent ?? 0) + "%" }} />
            </div>
            <small>
              {date && Number.isFinite(date.getTime())
                ? t("workspace.resetsAt", {
                    time: date.toLocaleString(i18n.language, {
                      month: "numeric",
                      day: "numeric",
                      hour: "2-digit",
                      minute: "2-digit",
                    }),
                  })
                : t("workspace.resetUnknown")}
            </small>
          </div>
        );
      })}
    </div>
  );
}
