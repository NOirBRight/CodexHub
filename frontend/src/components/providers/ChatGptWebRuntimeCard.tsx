import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useToasts } from "../PageToast";
import { api, messageFromError } from "../../lib/tauri";
import type { ChatGptWebStatus } from "../../lib/types";

export function ChatGptWebRuntimeCard() {
  const { t } = useTranslation();
  const { showToast, updateToast } = useToasts();
  const [status, setStatus] = useState<ChatGptWebStatus | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void refresh();
  }, []);

  async function refresh() {
    try {
      setStatus(await api.chatgptWebStatus());
    } catch {
      setStatus(null);
    }
  }

  async function run(
    label: string,
    action: () => Promise<ChatGptWebStatus>,
    success: string,
    retry: () => void,
  ) {
    const toastId = showToast({ text: label, tone: "loading", dedupeKey: "chatgpt-web-runtime" });
    setBusy(true);
    try {
      setStatus(await action());
      updateToast(toastId, { action: null, text: success, tone: "success" });
    } catch (error) {
      updateToast(toastId, {
        action: { label: t("common.retry"), onClick: retry },
        text: messageFromError(error),
        tone: "error",
      });
    } finally {
      setBusy(false);
    }
  }

  function enable() {
    void run(t("providers.chatgptWebInstalling"), () => api.chatgptWebEnable(), t("providers.chatgptWebInstalled"), enable);
  }

  return (
    <section className="grid gap-2 rounded-control bg-panel px-3 py-3" aria-label={t("providers.chatgptWebTitle")}>
      <div className="grid gap-1">
        <b className="text-sm text-ink">{t("providers.chatgptWebTitle")}</b>
        <p className="text-xs leading-5 text-slate-600">{t("providers.chatgptWebBody")}</p>
      </div>
      <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs text-slate-700">
        <div>{t("providers.chatgptWebLogin")}: {status?.login.state ?? "—"} / {status?.login.window ?? "—"}</div>
        <div>{t("providers.chatgptWebBrowserSmoke")}: {status?.browser_smoke.state ?? "—"}</div>
        <div>{t("providers.chatgptWebTunnel")}: {status?.tunnel.state ?? "—"}</div>
        <div>{t("providers.chatgptWebConnector")}: {status ? String(status.connector.selectable) : "—"}</div>
        <div>
          {t("providers.chatgptWebProcess")}: {status?.process.ownership ?? "—"}
          {status?.process.pid ? ` #${status.process.pid}` : ""}
          {status?.process.running ? "" : " (stopped)"}
        </div>
        <div>
          {status?.ready ? t("providers.chatgptWebReady") : t("providers.chatgptWebNotReady")}
          {status?.restart_required ? ` · ${t("providers.chatgptWebRestartRequired")}` : ""}
          {status?.disabled ? ` · ${t("providers.chatgptWebDisabled")}` : ""}
        </div>
        <div className="col-span-2 truncate">
          {t("providers.chatgptWebOwnership")}: {status?.process.private_home ?? "—"}
        </div>
        <div className="col-span-2 truncate">
          {status?.component.version ?? "—"} {status?.component.commit ?? ""}
          {status?.component.compatible === false ? " · pin mismatch" : ""}
        </div>
      </dl>
      <div className="flex flex-wrap gap-2">
        <button className="ws-button" type="button" disabled={busy} onClick={enable}>
          {t("providers.chatgptWebEnable")}
        </button>
        <button
          className="ws-button"
          type="button"
          disabled={busy}
          onClick={() => void run(t("providers.chatgptWebStopping"), () => api.chatgptWebStop(), t("providers.chatgptWebStopped"), () => void refresh())}
        >
          {t("providers.chatgptWebStop")}
        </button>
        <button
          className="ws-button"
          type="button"
          disabled={busy}
          onClick={() => void run(t("providers.chatgptWebDisabling"), () => api.chatgptWebDisable(), t("providers.chatgptWebDisabledDone"), () => void refresh())}
        >
          {t("providers.chatgptWebDisable")}
        </button>
        <button
          className="ws-button"
          type="button"
          disabled={busy}
          onClick={() => void run(t("providers.chatgptWebOpeningLogin"), () => api.chatgptWebOpenLogin(), t("providers.chatgptWebLoginOpened"), () => void refresh())}
        >
          {t("providers.chatgptWebOpenLogin")}
        </button>
        <button
          className="ws-button"
          type="button"
          disabled={busy}
          onClick={() => void run(t("providers.chatgptWebClosingLogin"), () => api.chatgptWebCloseLogin(), t("providers.chatgptWebLoginClosed"), () => void refresh())}
        >
          {t("providers.chatgptWebCloseLogin")}
        </button>
      </div>
    </section>
  );
}
