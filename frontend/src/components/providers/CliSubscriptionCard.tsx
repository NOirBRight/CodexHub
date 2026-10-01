import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, messageFromError } from "../../lib/tauri";
import { canEnableCliSubscription, SYSTEM_CONTEXT_CONSENT, type CliSubscriptionStatus } from "../../lib/cliSubscription";
import type { Provider } from "../../lib/types";
import { useToasts } from "../PageToast";
import { SwitchControl } from "./ProviderModelSection";

export function CliSubscriptionCard({ provider, onChange }: { provider: Provider; onChange: (provider: Provider) => void }) {
  const { t } = useTranslation();
  const { showToast, updateToast } = useToasts();
  const [status, setStatus] = useState<CliSubscriptionStatus | null>(null);
  const [busy, setBusy] = useState(false);

  async function detect() {
    if (busy) return;
    setBusy(true);
    const toast = showToast({ text: t("providers.cliDetecting"), tone: "loading", timeoutMs: null });
    try {
      const next = await api.cliSubscriptionStatus(provider.id);
      setStatus(next);
      updateToast(toast, { action: null, tone: next.state === "available" ? "success" : "error", text: t(`providers.cliState.${next.state}`, { defaultValue: t("providers.cliState.discovery-failed") }) });
    } catch (error) {
      setStatus(null);
      updateToast(toast, { action: null, tone: "error", text: messageFromError(error) });
    } finally { setBusy(false); }
  }

  return <div className="grid gap-2 rounded-md border border-line bg-panel p-3 text-xs text-slate-600">
    <p>{t("providers.cliCurrentAccount")}</p>
    <div className="flex flex-wrap items-center gap-3">
      <button type="button" className="ws-button" disabled={busy} onClick={() => void detect()}>{t("providers.cliDetect")}</button>
      {status ? <span role="status">{t(`providers.cliState.${status.state}`, { defaultValue: t("providers.cliState.discovery-failed") })} · {status.cli_version ?? "—"} · {status.models.length}</span> : null}
      <SwitchControl checked={provider.enabled} disabled={!canEnableCliSubscription(provider)} label={t("providers.cliEnabled")} onChange={(enabled) => onChange({ ...provider, enabled })} />
    </div>
    <p>{t("providers.cliNotQualified")}</p>
    <p>{t("providers.cliParameterLimits")}</p>
    {provider.id === "claude-subscription" ? <>
      <p>{t("providers.claudeCliAdaptation")}</p>
      <label className="flex items-start gap-2">
        <input type="checkbox" checked={provider.system_context_consent === SYSTEM_CONTEXT_CONSENT} onChange={(event) => onChange({ ...provider, system_context_consent: event.target.checked ? SYSTEM_CONTEXT_CONSENT : null, enabled: event.target.checked ? provider.enabled : false })} />
        <span>{t("providers.claudeCliConsent")}</span>
      </label>
    </> : null}
  </div>;
}
