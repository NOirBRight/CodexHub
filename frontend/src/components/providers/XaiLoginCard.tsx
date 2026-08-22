import { ExternalLink, KeyRound, LogOut, RefreshCcw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useToasts } from "../PageToast";
import { api, messageFromError } from "../../lib/tauri";
import type { XaiAuthStatus, XaiDeviceLogin } from "../../lib/types";

type Translate = (key: string, options?: Record<string, unknown>) => string;

export function XaiLoginCard() {
  const { t } = useTranslation();
  const translate = t as Translate;
  const { showToast, updateToast } = useToasts();
  const [status, setStatus] = useState<XaiAuthStatus | null>(null);
  const [device, setDevice] = useState<XaiDeviceLogin | null>(null);
  const [busy, setBusy] = useState(false);
  const pollCancel = useRef(false);

  useEffect(() => {
    void refreshStatus();
    return () => {
      pollCancel.current = true;
    };
  }, []);

  async function refreshStatus() {
    try {
      setStatus(await api.xaiAuthStatus());
    } catch {
      setStatus({ signed_in: false });
    }
  }

  async function startLogin() {
    const toastId = showToast(translate("providers.xaiStartingDeviceLogin"), "loading");
    setBusy(true);
    pollCancel.current = false;
    try {
      const started = await api.xaiStartDeviceLogin();
      setDevice(started);
      updateToast(toastId, {
        action: null,
        text: translate("providers.xaiDeviceCodeReady", { code: started.user_code }),
        tone: "loading",
      });
      if (started.verification_url) {
        window.open(started.verification_url, "_blank", "noopener,noreferrer");
      }
      await api.xaiPollDeviceLogin(started);
      if (pollCancel.current) {
        return;
      }
      await refreshStatus();
      setDevice(null);
      updateToast(toastId, {
        action: null,
        text: translate("providers.xaiSignedIn"),
        tone: "success",
      });
    } catch (err) {
      updateToast(toastId, {
        action: null,
        text: fallbackApiKeyHint(messageFromError(err), translate),
        tone: "error",
      });
    } finally {
      setBusy(false);
    }
  }

  async function logout() {
    const toastId = showToast(translate("providers.xaiSigningOut"), "loading");
    setBusy(true);
    try {
      await api.xaiLogout();
      setDevice(null);
      await refreshStatus();
      updateToast(toastId, {
        action: null,
        text: translate("providers.xaiSignedOut"),
        tone: "success",
      });
    } catch (err) {
      updateToast(toastId, {
        action: null,
        text: messageFromError(err),
        tone: "error",
      });
    } finally {
      setBusy(false);
    }
  }

  const signedIn = status?.signed_in === true;

  return (
    <section className="grid gap-3 rounded-inner bg-amber-50/70 p-3 text-sm shadow-hairline">
      <div className="min-w-0">
        <h3 className="truncate text-sm font-semibold text-ink">
          {signedIn ? translate("providers.xaiSignedInTitle") : translate("providers.xaiSignInTitle")}
        </h3>
        <p className="mt-1 text-xs leading-5 text-slate-700">{translate("providers.xaiSignInBody")}</p>
        {device && (
          <p className="mt-2 font-mono text-sm tracking-wide text-ink">
            {translate("providers.xaiUserCode", { code: device.user_code })}
          </p>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {!signedIn && (
          <button
            type="button"
            className="focus-ring flex h-9 min-w-0 items-center gap-2 rounded-control bg-ink px-3 text-xs font-semibold text-white shadow-control hover:bg-slate-800 disabled:bg-slate-300"
            disabled={busy}
            onClick={() => void startLogin()}
          >
            <KeyRound size={15} />
            <span className="truncate">{translate("providers.xaiStartDeviceLogin")}</span>
          </button>
        )}
        {device?.verification_url && (
          <a
            className="focus-ring flex h-9 min-w-0 items-center gap-2 rounded-control bg-surface px-3 text-xs font-semibold text-slate-700 shadow-control hover:bg-white"
            href={device.verification_url}
            target="_blank"
            rel="noreferrer"
          >
            <ExternalLink size={15} />
            <span className="truncate">{translate("providers.xaiOpenVerificationUrl")}</span>
          </a>
        )}
        <button
          type="button"
          className="focus-ring flex h-9 min-w-0 items-center gap-2 rounded-control bg-surface px-3 text-xs font-semibold text-slate-700 shadow-control hover:bg-white disabled:text-slate-300"
          disabled={busy}
          onClick={() => void refreshStatus()}
        >
          <RefreshCcw size={15} className={busy ? "animate-spin" : undefined} />
          <span className="truncate">{translate("providers.xaiRefreshAuth")}</span>
        </button>
        {signedIn && (
          <button
            type="button"
            className="focus-ring flex h-9 min-w-0 items-center gap-2 rounded-control bg-surface px-3 text-xs font-semibold text-slate-700 shadow-control hover:bg-white disabled:text-slate-300"
            disabled={busy}
            onClick={() => void logout()}
          >
            <LogOut size={15} />
            <span className="truncate">{translate("providers.xaiSignOut")}</span>
          </button>
        )}
      </div>
    </section>
  );
}

function fallbackApiKeyHint(message: string, t: Translate): string {
  if (message.toLowerCase().includes("403") || message.toLowerCase().includes("not-eligible")) {
    return t("providers.xaiAllowlistFallback");
  }
  return message;
}
