import { X } from "lucide-react";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";

export interface TakeoverSummary {
  configPath: string;
  currentOwner: string;
  name: string;
  newEndpoint: string;
  nextOwner: string;
  oldEndpoint: string;
}

export function TakeoverSummaryDialog({
  busy = false,
  onCancel,
  onConfirm,
  summary,
}: {
  busy?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  summary: TakeoverSummary | null;
}) {
  const { t } = useTranslation();
  const confirmRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (summary) {
      confirmRef.current?.focus();
    }
  }, [summary]);

  if (!summary) return null;

  const rows = [
    [t("gateway.takeoverTarget"), summary.name],
    [t("gateway.takeoverOwner"), `${summary.currentOwner} → ${summary.nextOwner}`],
    [t("gateway.takeoverEndpoint"), `${summary.oldEndpoint} → ${summary.newEndpoint}`],
    [t("gateway.takeoverConfig"), summary.configPath],
    [t("gateway.takeoverRecovery"), t("gateway.takeoverRecoveryValue")],
  ];

  return (
    <div className="fixed inset-0 z-[80] grid place-items-center bg-slate-950/30 p-4" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onCancel()}>
      <section className="w-full max-w-lg rounded-panel bg-surface p-5 shadow-raised" role="dialog" aria-modal="true" aria-labelledby="takeover-dialog-title">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 id="takeover-dialog-title" className="text-base font-semibold text-ink">{t("gateway.takeoverTitle")}</h2>
            <p className="mt-1 text-sm text-slate-600">{t("gateway.takeoverDescription")}</p>
          </div>
          <button type="button" className="focus-ring rounded-control p-1 text-slate-500 hover:bg-panel" aria-label={t("common.close")} onClick={onCancel}><X size={16} /></button>
        </div>
        <dl className="mt-4 grid gap-2 rounded-control bg-panel p-3 text-sm">
          {rows.map(([label, value]) => (
            <div key={label} className="grid grid-cols-[8rem_minmax(0,1fr)] gap-3">
              <dt className="text-slate-500">{label}</dt><dd className="break-all text-ink">{value}</dd>
            </div>
          ))}
        </dl>
        <div className="mt-5 flex justify-end gap-2">
          <button type="button" className="focus-ring rounded-control bg-panel px-4 py-2 text-sm font-medium text-slate-700" disabled={busy} onClick={onCancel}>{t("common.cancel")}</button>
          <button ref={confirmRef} type="button" className="focus-ring rounded-control bg-action px-4 py-2 text-sm font-semibold text-white" disabled={busy} onClick={onConfirm}>{t("gateway.takeover")}</button>
        </div>
      </section>
    </div>
  );
}
