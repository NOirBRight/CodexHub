import { useEffect, useRef, useState } from "react";
import { Check, RefreshCcw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useToasts } from "../PageToast";
import { ProviderLogo } from "../../lib/providerLogos";
import { api, messageFromError } from "../../lib/tauri";
import type { ChatGptWebStatus } from "../../lib/types";

export function ChatGptWebRuntimeCard({ unsaved = false }: { unsaved?: boolean }) {
  const { t } = useTranslation();
  const { showToast, updateToast } = useToasts();
  const [status, setStatus] = useState<ChatGptWebStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [refreshIndex, setRefreshIndex] = useState(0);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const request = useRef<Promise<ChatGptWebStatus> | null>(null);
  const actionRunning = useRef(false);

  // Poll serially: doctor may take longer than the refresh interval.
  useEffect(() => {
    if (busy || unsaved) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        request.current ??= api.chatgptWebStatus().finally(() => { request.current = null; });
        const next = await request.current;
        if (!cancelled) { setStatus(next); setError(null); }
      } catch (cause) {
        if (!cancelled) setError(messageFromError(cause));
      } finally {
        if (!cancelled) timer = setTimeout(() => void refresh(), 5000);
      }
    }
    void refresh();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [busy, refreshIndex, unsaved]);

  async function run(label: string, action: () => Promise<ChatGptWebStatus>, success: string) {
    if (actionRunning.current || unsaved) return;
    actionRunning.current = true;
    setBusy(true);
    const toastId = showToast({ text: label, tone: "loading", dedupeKey: "chatgpt-web-runtime" });
    try {
      await request.current?.catch(() => undefined);
      setStatus(await action());
      setError(null);
      setConfirmDelete(false);
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

  const prepared = Boolean(status?.installed && status.component.compatible && status.process.running && !status.disabled);
  const signedIn = status?.login.state === "signed_in";
  const loginOpen = status?.login.window === "open";
  const textReady = Boolean(prepared && signedIn && !status?.restart_required && status?.admitting !== false
    && status?.browser_smoke.state === "passed" && status.process.listen_host === "127.0.0.1"
    && status.process.port && status.process.port > 0 && status.process.port <= 65535);
  const stateKey = !status ? "chatgptWebChecking" : status.disabled ? "chatgptWebDisabled"
    : status.restart_required ? "chatgptWebRestartRequired" : status.ready ? "chatgptWebReady"
    : textReady ? "chatgptWebTextReady" : "chatgptWebNotReady";
  const disabled = busy || unsaved || !status || Boolean(error);
  const action = (loading: string, task: () => Promise<ChatGptWebStatus>, success: string) =>
    () => void run(t(`providers.${loading}`), task, t(`providers.${success}`));

  return (
    <section className="grid gap-5" aria-label={t("providers.chatgptWebTitle")}>
      <div className="flex items-start gap-3">
        <div className="rounded-control border border-line bg-white p-3"><ProviderLogo providerId="chatgpt-web" /></div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-base font-semibold text-ink">ChatGPT</h3>
            {!unsaved ? <span className="rounded-full bg-panel px-2 py-1 text-xs" role="status">{t(`providers.${stateKey}`)}</span> : null}
          </div>
          <p className="mt-1 text-xs leading-5 text-slate-600">{t("providers.chatgptWebBody")}</p>
        </div>
        {!unsaved ? <button type="button" className="ws-button" disabled={busy} onClick={() => setRefreshIndex((value) => value + 1)}>
          <RefreshCcw size={14} />{t("providers.chatgptWebRefresh")}
        </button> : null}
      </div>
      {error ? <p role="alert" className="text-xs text-red-700">{error}</p> : null}
      <ol className="grid gap-3">
        <li className="rounded-control border border-line p-4">
          <h4 className="flex items-center gap-2 text-sm font-semibold">{prepared ? <Check size={16} /> : <span>1.</span>}{t("providers.chatgptWebPrepareTitle")}</h4>
          <p className="my-2 text-xs leading-5 text-slate-600">{t("providers.chatgptWebPrepareBody")}</p>
          {!prepared || status?.restart_required ? <button type="button" className="ws-button" disabled={disabled || loginOpen}
            onClick={action("chatgptWebInstalling", () => api.chatgptWebEnable(), "chatgptWebInstalled")}>
            {t(`providers.${status?.installed ? "chatgptWebStart" : "chatgptWebEnable"}`)}
          </button> : null}
        </li>
        <li className="rounded-control border border-line p-4">
          <h4 className="flex items-center gap-2 text-sm font-semibold">{signedIn ? <Check size={16} /> : <span>2.</span>}{t("providers.chatgptWebSignInTitle")}</h4>
          <p className="my-2 text-xs leading-5 text-slate-600">{t(`providers.${loginOpen ? "chatgptWebLoginInstructions" : signedIn ? "chatgptWebSignedIn" : "chatgptWebSignInBody"}`)}</p>
          {status?.login.error ? <p role="alert" className="mb-2 text-xs text-red-700">{t("providers.chatgptWebLoginFailed")}</p> : null}
          {!signedIn || loginOpen ? <button type="button" className="ws-button" disabled={disabled || !prepared || loginOpen}
            onClick={action("chatgptWebOpeningLogin", () => api.chatgptWebOpenLogin(), "chatgptWebLoginOpened")}>
            {t(`providers.${loginOpen ? "chatgptWebWaitingLogin" : "chatgptWebOpenLogin"}`)}
          </button> : null}
        </li>
        <li className="rounded-control border border-line p-4">
          <h4 className="flex items-center gap-2 text-sm font-semibold">{textReady ? <Check size={16} /> : <span>3.</span>}{t("providers.chatgptWebModelsTitle")}</h4>
          <p className="my-2 text-xs leading-5 text-slate-600">{t(`providers.${textReady ? "chatgptWebModelsBody" : "chatgptWebModelsPending"}`)}</p>
          {textReady && !status?.ready ? <p className="mb-2 text-xs leading-5 text-amber-700">{t("providers.chatgptWebToolsPending")}</p> : null}
          {textReady && status?.models?.length ? <ul className="divide-y divide-line">
            {status.models.map((model) => <li key={model.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
              <span>{model.display_name || model.id}</span><code className="break-all text-xs text-slate-500">{model.id}</code>
            </li>)}
          </ul> : textReady ? <p className="text-xs text-slate-600">{t("providers.chatgptWebNoModels")}</p> : null}
        </li>
      </ol>
      {!unsaved && status ? <details className="rounded-control border border-line p-3">
        <summary className="cursor-pointer text-xs font-medium">{t("providers.chatgptWebAdvanced")}</summary>
        <dl className="my-3 grid gap-2 break-all text-xs text-slate-600">
          {[["chatgptWebLogin", status.login.state], ["chatgptWebBrowserSmoke", status.browser_smoke.state],
            ["chatgptWebTunnel", status.tunnel.state], ["chatgptWebConnector", String(status.connector.selectable)],
            ["chatgptWebOwnership", status.process.private_home ?? "—"]].map(([label, value]) =>
            <div key={label}><dt className="inline font-medium">{t(`providers.${label}`)}: </dt><dd className="inline">{value}</dd></div>)}
          <div>{status.component.version} · {status.component.commit}</div>
        </dl>
        <div className="flex flex-wrap gap-2">
          <button type="button" className="ws-button" disabled={disabled || !status.process.running} onClick={action("chatgptWebStopping", () => api.chatgptWebStop(), "chatgptWebStopped")}>{t("providers.chatgptWebStop")}</button>
          <button type="button" className="ws-button" disabled={disabled || status.disabled} onClick={action("chatgptWebDisabling", () => api.chatgptWebDisable(), "chatgptWebDisabledDone")}>{t("providers.chatgptWebDisable")}</button>
          <button type="button" className="ws-button" disabled={disabled || loginOpen} onClick={action("chatgptWebUpgrading", () => api.chatgptWebUpgrade(), "chatgptWebUpgraded")}>{t("providers.chatgptWebUpgrade")}</button>
          {loginOpen ? <button type="button" className="ws-button" disabled={disabled} onClick={action("chatgptWebClosingLogin", () => api.chatgptWebCloseLogin(), "chatgptWebLoginClosed")}>{t("providers.chatgptWebCloseLogin")}</button> : null}
        </div>
        <div className="mt-4 border-t border-line pt-3">
          <p className="mb-2 text-xs leading-5 text-slate-600">{t("providers.chatgptWebDeleteHelp")}</p>
          {confirmDelete ? <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs">{t("providers.chatgptWebDeleteConfirm")}</span>
            <button type="button" className="ws-button text-red-700" disabled={disabled} onClick={action("chatgptWebDeletingAccount", () => api.chatgptWebDeleteAccount(), "chatgptWebAccountDeleted")}>{t("providers.chatgptWebDeleteAccount")}</button>
            <button type="button" className="ws-button" disabled={busy} onClick={() => setConfirmDelete(false)}>{t("common.cancel")}</button>
          </div> : <button type="button" className="ws-button text-red-700" disabled={disabled} onClick={() => setConfirmDelete(true)}>{t("providers.chatgptWebDeleteAccount")}</button>}
        </div>
      </details> : null}
    </section>
  );
}
