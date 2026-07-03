import { Minus, Play, Settings as SettingsIcon, Square, X } from "lucide-react";
import type { ReactNode } from "react";
import codexLogo from "../assets/codex-logo.svg";
import { cx } from "../lib/format";
import { api } from "../lib/tauri";
import type { AppStatus, Settings } from "../lib/types";

interface RuntimeBarProps {
  busy?: string | null;
  message?: string | null;
  settings: Settings | null;
  status: AppStatus | null;
  onOpenSettings: () => void;
  onStart: () => void;
  onStop: () => void;
}

export function RuntimeBar({
  busy,
  message,
  onOpenSettings,
  onStart,
  onStop,
  settings,
  status,
}: RuntimeBarProps) {
  const running = status?.proxy_running ?? false;
  const port = status?.proxy_port ?? settings?.proxy_port ?? 9099;
  const address = `${settings?.gateway_bind_address || "127.0.0.1"}:${port}`;
  const runtimeHint = formatRuntimeHint(message);

  return (
    <header
      className="flex min-h-[56px] items-center gap-3 overflow-hidden border-b border-line bg-white pl-4 shadow-subtle"
      data-tauri-drag-region
    >
      <div className="flex shrink-0 items-center gap-2">
        <span className="grid h-8 w-8 place-items-center rounded-full border border-line bg-white shadow-subtle">
          <img src={codexLogo} alt="" className="h-5 w-5" aria-hidden="true" />
        </span>
        <span className="truncate text-base font-semibold text-ink">CodexHub</span>
      </div>

      <div className="min-w-0 flex-1" data-tauri-drag-region />

      <div className="flex shrink-0 items-center gap-2">
        <div
          className="flex h-8 max-w-[360px] items-center gap-2 rounded-md border border-line bg-panel px-2 text-xs"
          title={message ?? `${address} ${running ? "running" : "stopped"}`}
        >
          <span className={cx("h-2 w-2 rounded-full", running ? "bg-ok" : "bg-danger")} />
          <code className="font-mono text-slate-700">{address}</code>
          <span className="text-slate-500">{running ? "running" : "stopped"}</span>
          {runtimeHint && (
            <span className="min-w-0 truncate border-l border-line pl-2 font-medium text-slate-600">
              {runtimeHint}
            </span>
          )}
        </div>
        <button
          type="button"
          className="focus-ring inline-flex h-8 items-center justify-center gap-1 rounded-md border border-line bg-white px-2 text-xs font-semibold text-slate-700 hover:bg-panel"
          disabled={Boolean(busy)}
          onClick={running ? onStop : onStart}
          title={running ? "Stop the local Gateway runtime" : "Start the local Gateway runtime"}
        >
          {running ? <Square size={13} /> : <Play size={13} />}
          {running ? "Stop" : "Start"}
        </button>
        <button
          type="button"
          className="focus-ring grid h-8 w-8 place-items-center rounded-md border border-line bg-white text-slate-600 hover:bg-panel"
          onClick={onOpenSettings}
          title="Settings"
        >
          <SettingsIcon size={15} />
        </button>
        <div className="ml-1 flex h-10 items-center border-l border-line pl-1">
          <WindowControlButton
            label="Minimize"
            title="Minimize"
            onClick={() => void api.windowMinimize()}
          >
            <Minus size={14} />
          </WindowControlButton>
          <WindowControlButton
            label="Maximize or restore"
            title="Maximize or restore"
            onClick={() => void api.windowToggleMaximize()}
          >
            <Square size={12} />
          </WindowControlButton>
          <WindowControlButton
            label="Close to tray"
            title="Close to tray"
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
        "focus-ring grid h-9 w-10 place-items-center text-slate-600 hover:bg-panel hover:text-ink",
        danger && "hover:bg-red-50 hover:text-danger",
      )}
      aria-label={label}
      title={title}
      onClick={onClick}
    >
      {children}
    </button>
  );
}
