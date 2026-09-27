import { useEffect, useRef, useState } from "react";
import { Check, RefreshCcw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useToasts } from "../PageToast";
import { ProviderLogo } from "../../lib/providerLogos";
import { api, messageFromError } from "../../lib/tauri";
import type { ChatGptWebStatus } from "../../lib/types";

let runtimeActionVersion = 0;

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
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  function readStatus() {
    request.current ??= api.chatgptWebStatus().finally(() => { request.current = null; });
    return request.current;
  }

  function finishLogin(toastId: string, next: ChatGptWebStatus) {
    const failed = Boolean(next.login.error) || next.login.state !== "signed_in";
    updateToast(toastId, { action: null,
      text: t(`providers.${failed ? "chatgptWebLoginFailed" : "chatgptWebLoginVerified"}`),
      tone: failed ? "error" : "success" });
  }

  async function followLogin(toastId: string, version: number) {
    try {
      while (runtimeActionVersion === version) {
        await new Promise((resolve) => setTimeout(resolve, 5000));
        if (runtimeActionVersion !== version) return;
        const next = await readStatus();
        if (runtimeActionVersion !== version) return;
        if (mounted.current) setStatus(next);
        if (next.login.window === "open") continue;
        finishLogin(toastId, next);
        return;
      }
    } catch (cause) {
      if (runtimeActionVersion === version) updateToast(toastId, {
        action: { label: t("common.retry"), onClick: () => {
          if (runtimeActionVersion !== version) return;
          updateToast(toastId, { action: null, text: t("providers.chatgptWebWaitingLogin"), tone: "loading" });
          void followLogin(toastId, version);
        } }, text: messageFromError(cause), tone: "error",
      });
    }
  }

  // Poll serially: doctor may take longer than the refresh interval.
  useEffect(() => {
    if (busy || unsaved) return;
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
  }, [busy, refreshIndex, unsaved]);

  async function run(label: string, action: () => Promise<ChatGptWebStatus>, success: string, monitorLogin = false) {
    if (actionRunning.current || unsaved) return;
    actionRunning.current = true;
    const version = ++runtimeActionVersion;
    setBusy(true);
    const toastId = showToast({ text: label, tone: "loading", dedupeKey: "chatgpt-web-runtime" });
    try {
      await request.current?.catch(() => undefined);
      const next = await action();
      if (mounted.current) setStatus(next);
      setError(null);
      setConfirmDelete(false);
      if (monitorLogin && next.login.window === "open") {
        updateToast(toastId, { action: null, text: t("providers.chatgptWebWaitingLogin"), tone: "loading" });
        void followLogin(toastId, version);
      } else if (monitorLogin) {
        finishLogin(toastId, next);
      } else {
        updateToast(toastId, { action: null, text: success, tone: "success" });
      }
    } catch (cause) {
      updateToast(toastId, {
        action: { label: t("common.retry"), onClick: () => {
          if (runtimeActionVersion === version) void run(label, action, success, monitorLogin);
        } },
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
  const textReady = Boolean(!error && prepared && signedIn && !status?.restart_required && status?.admitting !== false
    && status?.browser_smoke.state === "passed" && status.process.listen_host === "127.0.0.1"
    && status.process.port && status.process.port > 0 && status.process.port <= 65535);
  const stateKey = error ? "chatgptWebStatusUnknown" : !status ? "chatgptWebChecking" : status.disabled ? "chatgptWebDisabled"
    : status.restart_required ? "chatgptWebRestartRequired" : status.ready && textReady ? "chatgptWebReady"
    : textReady ? "chatgptWebTextReady" : "chatgptWebNotReady";
  const disabled = busy || unsaved || !status || Boolean(error);
  const action = (loading: string, task: () => Promise<ChatGptWebStatus>, success: string, monitorLogin = false) =>
    () => void run(t(`providers.${loading}`), task, t(`providers.${success}`), monitorLogin);

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
            onClick={action("chatgptWebOpeningLogin", () => api.chatgptWebOpenLogin(), "chatgptWebLoginOpened", true)}>
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
