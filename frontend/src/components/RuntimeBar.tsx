import { getCurrentWindow } from "@tauri-apps/api/window";
import { Minus, Play, Settings as SettingsIcon, Square, X } from "lucide-react";
import type { MouseEvent, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import codexLogo from "../assets/codex-logo.svg";
import { cx } from "../lib/format";
import { api } from "../lib/tauri";
import type { AppFlavorInfo, AppStatus, Settings } from "../lib/types";

interface RuntimeBarProps {
  appFlavor?: AppFlavorInfo | null;
  busy?: string | null;
  message?: string | null;
  settings: Settings | null;
  status: AppStatus | null;
  onOpenSettings: () => void;
  onStart: () => void;
  onStop: () => void;
}

export function RuntimeBar({
  appFlavor,
  busy,
  message,
  onOpenSettings,
  onStart,
  onStop,
  settings,
  status,
}: RuntimeBarProps) {
  const { t } = useTranslation();
  const running = status?.proxy_running ?? false;
  const port = status?.proxy_port ?? settings?.proxy_port ?? 9099;
  const address = `${settings?.gateway_bind_address || "127.0.0.1"}:${port}`;
  const runtimeHint = formatRuntimeHint(message);

  return (
    <header
      className="flex min-h-[56px] items-center gap-3 overflow-hidden bg-surface pl-4 shadow-hairline"
      data-tauri-drag-region
      onMouseDownCapture={startWindowDrag}
    >
      <div
        className="flex shrink-0 select-none items-center gap-2 [&_*]:pointer-events-none"
        data-tauri-drag-region
      >
        <span className="grid h-8 w-8 place-items-center rounded-full bg-surface shadow-control">
          <img src={codexLogo} alt="" className="h-5 w-5" aria-hidden="true" />
        </span>
        <span className="truncate text-base font-semibold text-ink">
          {appFlavor?.product_name ?? "CodexHub"}
        </span>
        {appFlavor?.flavor === "beta" ? (
          <span className="rounded-control border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-[10px] font-semibold text-amber-800">
            Beta
          </span>
        ) : null}
      </div>

      <div className="min-w-0 flex-1 self-stretch" data-tauri-drag-region />

      <div className="flex shrink-0 items-center gap-2">
        <div
          className="flex h-8 max-w-[360px] select-none items-center gap-2 rounded-control bg-panel px-2 text-xs shadow-control [&_*]:pointer-events-none"
          data-tauri-drag-region
          title={message ?? `${address} ${running ? t("runtime.running") : t("runtime.stopped")}`}
        >
          <span className={cx("h-2 w-2 rounded-full", running ? "bg-ok" : "bg-danger")} />
          <code className="font-mono text-slate-700">{address}</code>
          <span className="text-slate-500">{running ? t("runtime.running") : t("runtime.stopped")}</span>
          {runtimeHint && (
            <span className="min-w-0 truncate border-l border-line pl-2 font-medium text-slate-600">
              {runtimeHint}
            </span>
          )}
        </div>
        <button
          type="button"
          className="focus-ring inline-flex h-8 w-8 items-center justify-center rounded-control bg-surface text-slate-700 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96]"
          disabled={Boolean(busy)}
          onClick={running ? onStop : onStart}
          aria-label={running ? t("runtime.stopRuntime") : t("runtime.startRuntime")}
          title={running ? t("runtime.stopRuntime") : t("runtime.startRuntime")}
        >
          {running ? <Square size={13} /> : <Play size={13} />}
        </button>
        <button
          type="button"
          className="focus-ring grid h-8 w-8 place-items-center rounded-control bg-surface text-slate-600 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96]"
          onClick={onOpenSettings}
          aria-label={t("common.settings")}
          title={t("common.settings")}
        >
          <SettingsIcon size={15} />
        </button>
        <div className="ml-1 flex h-10 items-center border-l border-line pl-1">
          <WindowControlButton
            label={t("runtime.minimize")}
            title={t("runtime.minimize")}
            onClick={() => void api.windowMinimize()}
          >
            <Minus size={14} />
          </WindowControlButton>
          <WindowControlButton
            label={t("runtime.maximizeOrRestore")}
            title={t("runtime.maximizeOrRestore")}
            onClick={() => void api.windowToggleMaximize()}
          >
            <Square size={12} />
          </WindowControlButton>
          <WindowControlButton
            label={t("runtime.closeToTray")}
            title={t("runtime.closeToTray")}
            danger
            onClick={() => void api.windowCloseToTray()}
          >
            <X size={14} />
          </WindowControlButton>
        </div>
      </div>
    </header>
  );
}

function formatRuntimeHint(message?: string | null) {
  if (!message) {
    return null;
  }
  const pid = message.match(/\bPID\s+(\d+)\b/i)?.[1];
  if (pid) {
    return `PID ${pid}`;
  }
  return message;
}

function startWindowDrag(event: MouseEvent<HTMLElement>) {
  if (event.button !== 0 || isInteractiveWindowControl(event.target)) {
    return;
  }

  event.preventDefault();
  try {
    void getCurrentWindow().startDragging().catch(() => undefined);
  } catch {
    // Browser preview has no Tauri window to drag.
  }
}

function isInteractiveWindowControl(target: EventTarget | null) {
  if (!(target instanceof Element)) {
    return false;
  }
  return Boolean(target.closest("button,a,input,select,textarea,[role='button'],[data-window-control]"));
}

function WindowControlButton({
  children,
  danger = false,
  label,
  onClick,
  title,
}: {
  children: ReactNode;
  danger?: boolean;
  label: string;
  onClick: () => void;
  title: string;
}) {
  return (
    <button
      type="button"
      className={cx(
        "focus-ring grid h-9 w-10 place-items-center rounded-control text-slate-600 transition-[background-color,color,transform] duration-150 ease-out hover:bg-panel hover:text-ink active:scale-[0.96]",
        danger && "hover:bg-red-50 hover:text-danger",
      )}
      aria-label={label}
      data-window-control
      title={title}
      onClick={onClick}
    >
      {children}
    </button>
  );
}
