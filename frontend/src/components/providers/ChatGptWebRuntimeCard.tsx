import { useEffect, useRef, useState } from "react";
import { ExternalLink, RefreshCcw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useToasts } from "../PageToast";
import { Field } from "./ProviderFormControls";
import { SwitchControl } from "./ProviderModelSection";
import { ProviderLogo } from "../../lib/providerLogos";
import { api, messageFromError } from "../../lib/tauri";
import type { Model, Provider, ChatGptWebStatus } from "../../lib/types";

type ChatGptWebModel = NonNullable<ChatGptWebStatus["models"]>[number];

export function ChatGptWebRuntimeCard({
  provider,
  onProviderChange,
  unsaved = false,
}: {
  provider: Provider;
  onProviderChange: (provider: Provider) => void;
  unsaved?: boolean;
}) {
  const { t } = useTranslation();
  const { dismissToast, showToast, updateToast } = useToasts();
  const [status, setStatus] = useState<ChatGptWebStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [connectionBusy, setConnectionBusy] = useState(false);
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [refreshIndex, setRefreshIndex] = useState(0);
  const request = useRef<Promise<ChatGptWebStatus> | null>(null);
  const actionRunning = useRef(false);
  const settingsRestartToast = useRef<string | null>(null);

  useEffect(() => {
    if (status?.settings_pending_restart) {
      const reminder = {
        text: t("providers.chatgptWebSettingsPendingRestart"),
        tone: "info" as const,
        timeoutMs: null,
        dedupeKey: "chatgpt-web-settings-restart",
      };
      if (settingsRestartToast.current) {
        updateToast(settingsRestartToast.current, reminder);
      } else {
        settingsRestartToast.current = showToast(reminder);
      }
    } else if (settingsRestartToast.current) {
      dismissToast(settingsRestartToast.current);
      settingsRestartToast.current = null;
    }
  }, [dismissToast, showToast, status?.settings_pending_restart, t, updateToast]);

  useEffect(() => () => {
    if (settingsRestartToast.current) dismissToast(settingsRestartToast.current);
  }, [dismissToast]);

  function readStatus() {
    request.current ??= api.chatgptWebStatus().finally(() => { request.current = null; });
    return request.current;
  }

  // Refresh serially because the runtime doctor can take longer than the interval.
  useEffect(() => {
    if (busy) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const next = await readStatus();
        if (!cancelled) { setStatus(next); setError(null); }
      } catch (cause) {
        if (!cancelled) setError(messageFromError(cause));
      } finally {
        if (!cancelled) timer = setTimeout(() => void refresh(), 5000);
      }
    }
    void refresh();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [busy, refreshIndex]);

  async function run(label: string, action: () => Promise<ChatGptWebStatus>, success: string) {
    if (actionRunning.current || unsaved) return;
    actionRunning.current = true;
    setBusy(true);
    const toastId = showToast({ text: label, tone: "loading", dedupeKey: "chatgpt-web-runtime" });
    try {
      await request.current?.catch(() => undefined);
      const next = await action();
      setStatus(next);
      setError(null);
      updateToast(toastId, { action: null, text: success, tone: "success" });
    } catch (cause) {
      updateToast(toastId, {
        action: { label: t("common.retry"), onClick: () => void run(label, action, success) },
        text: messageFromError(cause), tone: "error",
      });
    } finally {
      actionRunning.current = false;
      setBusy(false);
    }
  }

  async function testConnection() {
    setConnectionBusy(true);
    setConnectionError(null);
    const toastId = showToast({ text: t("providers.chatgptWebConnectionChecking"), tone: "loading" });
    try {
      const result = await api.chatgptWebConnectionCheck(provider.base_url.trim(), provider.api_key ?? "");
      updateToast(toastId, {
        action: null,
        text: t("providers.chatgptWebConnectionSuccess", { address: result.base_url }),
        tone: "success",
      });
    } catch (cause) {
      const message = messageFromError(cause);
      setConnectionError(message);
      updateToast(toastId, { action: null, text: message, tone: "error" });
    } finally {
      setConnectionBusy(false);
    }
  }

  async function openRuntimeSettings() {
    if (settingsBusy) return;
    setSettingsBusy(true);
    const toastId = showToast({ text: t("providers.chatgptWebSettingsOpening"), tone: "loading", dedupeKey: "chatgpt-web-settings" });
    try {
      await api.chatgptWebOpenSettings();
      updateToast(toastId, { action: null, text: t("providers.chatgptWebSettingsOpened"), tone: "success" });
    } catch (cause) {
      updateToast(toastId, {
        action: { label: t("common.retry"), onClick: () => void openRuntimeSettings() },
        text: messageFromError(cause), tone: "error",
      });
    } finally {
      setSettingsBusy(false);
    }
  }

  const process = status?.process;
  const managedAddress = process?.listen_host === "127.0.0.1" && process.port
    ? `http://127.0.0.1:${process.port}` : "";
  const prepared = Boolean(status?.installed && status.component.compatible && process?.running && !status.disabled);
  const textReady = Boolean(!error && prepared && status?.login.state === "signed_in"
    && !status?.restart_required && status?.admitting !== false
    && status?.browser_smoke.state === "passed"
    && status?.readiness_checks?.capabilities_match === true && managedAddress);
  const toolsReady = Boolean(textReady && status?.ready);
  const stateKey = error ? "chatgptWebStatusUnknown" : !status ? "chatgptWebChecking"
    : status.disabled ? "chatgptWebDisabled" : status.restart_required ? "chatgptWebRestartRequired"
    : toolsReady ? "chatgptWebReady" : textReady ? "chatgptWebTextReady" : "chatgptWebNotReady";
  const models = status?.models ?? [];

  function updateConnection(patch: Partial<Pick<Provider, "base_url" | "api_key">>) {
    onProviderChange({ ...provider, ...patch });
  }

  function modelEnabled(model: ChatGptWebModel) {
    if (!provider.models.length) return provider.enabled;
    const bareId = model.id.replace(/^chatgpt-web\//, "");
    const configured = provider.models.find((item) =>
      item.id === bareId || item.id === model.id || item.upstream_model === model.id,
    );
    return Boolean(configured?.enabled && configured.gateway_exported !== false);
  }

  function toggleModel(model: ChatGptWebModel, enabled: boolean) {
    const id = model.id.replace(/^chatgpt-web\//, "");
    if (!provider.models.length) {
      const nextModels = models.map((candidate): Model => {
        const candidateId = candidate.id.replace(/^chatgpt-web\//, "");
        return {
          id: candidateId,
          upstream_model: candidate.id,
          display_name: candidate.display_name || candidateId,
          supported_reasoning_levels: candidate.efforts,
          default_reasoning_level: candidate.efforts[0] ?? null,
          thinking_mode: candidate.efforts.length ? "toggle" : "none",
          input_modalities: candidate.image_input ? ["text", "image"] : ["text"],
          gateway_exported: true,
          enabled: candidate.id === model.id ? enabled : true,
        };
      });
      onProviderChange({ ...provider, models: nextModels });
      return;
    }
    const existing = provider.models.find((item) =>
      item.id === id || item.id === model.id || item.upstream_model === model.id,
    );
    const nextModel: Model = existing
      ? { ...existing, enabled, gateway_exported: true }
      : {
        id,
        upstream_model: model.id,
        display_name: model.display_name || id,
        supported_reasoning_levels: model.efforts,
        default_reasoning_level: model.efforts[0] ?? null,
        thinking_mode: model.efforts.length ? "toggle" : "none",
        input_modalities: model.image_input ? ["text", "image"] : ["text"],
        gateway_exported: true,
        enabled,
      };
    const nextModels = existing
      ? provider.models.map((item) => item === existing ? nextModel : item)
      : [...provider.models, nextModel];
    onProviderChange({ ...provider, models: nextModels });
  }

  const disabled = busy || unsaved || !status || Boolean(error);
  const lifecycleAction = (loading: string, task: () => Promise<ChatGptWebStatus>, success: string) =>
    () => void run(t(`providers.${loading}`), task, t(`providers.${success}`));

  return (
    <section className="grid gap-5" aria-label={t("providers.chatgptWebTitle")}>
      <div className="flex items-start gap-3">
        <div className="rounded-control border border-line bg-white p-3"><ProviderLogo providerId="chatgpt-web" /></div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-base font-semibold text-ink">ChatGPT</h3>
            <span className="ws-status-chip inline-flex h-6 items-center border border-line bg-panel px-2 text-xs" role="status">{t(`providers.${stateKey}`)}</span>
          </div>
          <p className="mt-1 text-xs leading-5 text-slate-600">{t("providers.chatgptWebBody")}</p>
        </div>
        <button type="button" className="ws-button" disabled={busy} onClick={() => setRefreshIndex((value) => value + 1)}>
          <RefreshCcw size={14} />{t("providers.chatgptWebRefresh")}
        </button>
      </div>
      {error ? <p role="alert" className="text-xs text-red-700">{error}</p> : null}

      <section className="grid gap-3 rounded-control border border-line p-4" aria-label={t("providers.chatgptWebConnectionTitle")}>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <h4 className="text-sm font-semibold">{t("providers.chatgptWebConnectionTitle")}</h4>
            <p className="mt-1 text-xs leading-5 text-slate-600">{t("providers.chatgptWebConnectionBody")}</p>
          </div>
          <button type="button" className="ws-button" disabled={settingsBusy} onClick={() => void openRuntimeSettings()}>
            <ExternalLink size={14} />{t("providers.chatgptWebOpenSettings")}
          </button>
        </div>
        <Field label={t("providers.chatgptWebServiceAddress")}>
          <input className="field field-compact" value={provider.base_url}
            placeholder={managedAddress || t("providers.chatgptWebAddressUnavailable")}
            onChange={(event) => updateConnection({ base_url: event.target.value })} />
        </Field>
        <p className="-mt-2 text-xs leading-5 text-slate-500">{t("providers.chatgptWebServiceAddressHelp", { address: managedAddress || "—" })}</p>
        <Field label={t("providers.chatgptWebServiceCredential")}>
          <input className="field field-compact" type="password" autoComplete="off" spellCheck={false}
            value={provider.api_key ?? ""} placeholder={t("providers.chatgptWebCredentialPlaceholder")}
            onChange={(event) => updateConnection({ api_key: event.target.value })} />
        </Field>
        <p className="-mt-2 text-xs leading-5 text-slate-500">{t("providers.chatgptWebServiceCredentialHelp")}</p>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className="ws-button" disabled={connectionBusy || !status?.process.running}
            onClick={() => void testConnection()}>{t("providers.chatgptWebConnectionCheck")}</button>
          <span className="text-xs text-slate-600">{textReady ? t("providers.chatgptWebTextReady") : t("providers.chatgptWebNotReady")}</span>
          <span className="text-xs text-slate-600">{toolsReady ? t("providers.chatgptWebToolsReady") : t("providers.chatgptWebToolsPending")}</span>
        </div>
        {connectionError ? <p role="alert" className="text-xs text-red-700">{connectionError}</p> : null}
        <p className="text-xs leading-5 text-slate-500">{t("providers.chatgptWebRuntimeSettingsHint")}</p>
      </section>

      <section className="grid gap-3 rounded-control border border-line p-4" aria-label={t("providers.chatgptWebModelsTitle")}>
        <div>
          <h4 className="text-sm font-semibold">{t("providers.chatgptWebModelsTitle")}</h4>
          <p className="mt-1 text-xs leading-5 text-slate-600">{t("providers.chatgptWebModelsBody")}</p>
        </div>
        {models.length ? <ul className="divide-y divide-line">
          {models.map((model) => <li key={model.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
            <SwitchControl checked={modelEnabled(model)} label={model.display_name || model.id}
              onChange={(enabled) => toggleModel(model, enabled)} />
            <code className="break-all text-xs text-slate-500">{model.id}</code>
          </li>)}
        </ul> : <p className="text-xs text-slate-600">{t("providers.chatgptWebNoModels")}</p>}
      </section>

      <section className="flex flex-wrap items-center gap-2 rounded-control border border-line p-4">
        <div className="mr-auto min-w-0">
          <h4 className="text-sm font-semibold">{t("providers.chatgptWebPrepareTitle")}</h4>
          <p className="mt-1 text-xs leading-5 text-slate-600">{t("providers.chatgptWebPrepareBody")}</p>
        </div>
        {!prepared || status?.restart_required ? <button type="button" className="ws-button" disabled={disabled}
          onClick={lifecycleAction("chatgptWebInstalling", () => api.chatgptWebEnable(), "chatgptWebInstalled")}>
          {t(`providers.${status?.installed ? "chatgptWebStart" : "chatgptWebEnable"}`)}
        </button> : null}
        <details className="w-full border-t border-line pt-3">
          <summary className="cursor-pointer text-xs font-medium">{t("providers.chatgptWebAdvanced")}</summary>
          {status ? <dl className="my-3 grid gap-2 break-all text-xs text-slate-600">
            <div><dt className="inline font-medium">{t("providers.chatgptWebLogin")}: </dt><dd className="inline">{status.login.state}</dd></div>
            <div><dt className="inline font-medium">{t("providers.chatgptWebBrowserSmoke")}: </dt><dd className="inline">{status.browser_smoke.state}</dd></div>
            <div><dt className="inline font-medium">{t("providers.chatgptWebTunnel")}: </dt><dd className="inline">{status.tunnel.state}</dd></div>
            <div><dt className="inline font-medium">{t("providers.chatgptWebConnector")}: </dt><dd className="inline">{String(status.connector.selectable)}</dd></div>
            <div><dt className="inline font-medium">{t("providers.chatgptWebProcess")}: </dt><dd className="inline">{status.process.running ? status.process.pid ?? "running" : t("providers.chatgptWebNotReady")}</dd></div>
            <div>{status.component.version} · {status.component.commit}</div>
          </dl> : null}
          <div className="flex flex-wrap gap-2">
            <button type="button" className="ws-button" disabled={disabled || !status?.process.running}
              onClick={lifecycleAction("chatgptWebStopping", () => api.chatgptWebStop(), "chatgptWebStopped")}>{t("providers.chatgptWebStop")}</button>
            <button type="button" className="ws-button" disabled={disabled || status?.disabled}
              onClick={lifecycleAction("chatgptWebDisabling", () => api.chatgptWebDisable(), "chatgptWebDisabledDone")}>{t("providers.chatgptWebDisable")}</button>
            <button type="button" className="ws-button" disabled={disabled}
              onClick={lifecycleAction("chatgptWebUpgrading", () => api.chatgptWebUpgrade(), "chatgptWebUpgraded")}>{t("providers.chatgptWebUpgrade")}</button>
          </div>
        </details>
      </section>
    </section>
  );
}
