import { useEffect, useState, type ReactNode } from "react";
import {
  BarChart3,
  Copy,
  LayoutGrid,
  Layers,
  Link2,
  Moon,
  Power,
  Radio,
  RefreshCw,
  Settings2,
  Sun,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { messageFromError } from "../../lib/tauri";
import { useToasts } from "../PageToast";
import type { AppStatus, Settings } from "../../lib/types";
import "./workspace.css";
export type WorkspacePage =
  | "overview"
  | "statistics"
  | "providers"
  | "clients"
  | "gateway"
  | "settings";
export const workspacePages = [
  "overview",
  "statistics",
  "providers",
  "clients",
  "gateway",
  "settings",
] as const;
const icons = [LayoutGrid, BarChart3, Layers, Link2, Radio, Settings2];
export function useWorkspaceTheme() {
  const { t } = useTranslation();
  const toast = useToasts();
  const [dark, setDark] = useState(() => {
    try {
      return localStorage.getItem("codexhub.appearance") === "dark";
    } catch {
      return false;
    }
  });
  useEffect(() => {
    document.documentElement.dataset.appearance = dark ? "dark" : "light";
  }, [dark]);
  function toggle() {
    const next = !dark;
    setDark(next);
    try {
      localStorage.setItem("codexhub.appearance", next ? "dark" : "light");
      toast.showToast(t("workspace.appearanceSaved"), "success");
    } catch {
      toast.showToast(t("workspace.appearanceSessionOnly"), "error");
    }
  }
  return { dark, toggle };
}
export function WorkspaceFrame({
  children,
  titlebar,
  page,
  onNavigate,
  status,
  settings,
  busy,
  onStart,
  onStop,
  onRestart,
  dark,
  onTheme,
}: {
  children: ReactNode;
  titlebar: ReactNode;
  page: WorkspacePage;
  onNavigate: (page: WorkspacePage) => void;
  status: AppStatus | null;
  settings: Settings | null;
  busy: boolean;
  onStart: () => void;
  onStop: () => void;
  onRestart: () => void;
  dark: boolean;
  onTheme: () => void;
}) {
  const { t } = useTranslation();
  const toast = useToasts();
  const running = Boolean(status?.proxy_running);
  const transitioning = [
    "unavailable",
    "starting",
    "stopping",
    "restarting",
  ].includes(status?.gateway_lifecycle || "");
  const address = `${settings?.gateway_bind_address || "127.0.0.1"}:${status?.proxy_port ?? settings?.proxy_port ?? 9099}`;
  return (
    <div className="workspace-root">
      <div className="ws-title">
        {titlebar}
        <button
          className="ws-theme"
          aria-label={t("workspace.toggleTheme")}
          onClick={onTheme}
        >
          {dark ? <Sun size={14} /> : <Moon size={14} />}
        </button>
      </div>
      <nav className="ws-nav">
        <div className="ws-tab-list">
          {workspacePages.map((id, i) => {
            const Icon = icons[i];
            return (
              <button
                key={id}
                aria-current={page === id ? "page" : undefined}
                className={page === id ? "selected" : ""}
                onClick={() => onNavigate(id)}
              >
                <Icon size={14} />
                {t("workspace." + id)}
              </button>
            );
          })}
        </div>
        <div id="workspace-tab-actions" className="ws-tab-actions" />
      </nav>
      <div className="ws-service">
        <Radio size={16} />
        <b>Gateway</b>
        <span className={running ? "ws-online" : "ws-muted"}>
          {t(
            transitioning
              ? "workspace.transitioning"
              : running
                ? "runtime.running"
                : "runtime.stopped",
          )}
        </span>
        <code>{address}</code>
        <div className="ws-service-actions">
          <button
            aria-label={t("workspace.copyEndpoint")}
            onClick={() =>
              void navigator.clipboard
                .writeText(`http://${address}/v1`)
                .then(() => toast.showToast(t("common.copied"), "success"))
                .catch((e) => toast.showToast(messageFromError(e), "error"))
            }
          >
            <Copy size={13} />
          </button>
          <button
            disabled={busy || !status || transitioning}
            onClick={running ? onStop : onStart}
          >
            <Power size={12} />
            {t(running ? "common.stop" : "common.start")}
          </button>
          <button
            aria-label={t("workspace.restartGateway")}
            disabled={busy || !running || transitioning}
            onClick={onRestart}
          >
            <RefreshCw size={13} />
          </button>
        </div>
      </div>
      <div className="ws-main">{children}</div>
      <footer className="ws-status">
        <span className={running ? "ws-online" : "ws-muted"}>
          ● Gateway {t(running ? "runtime.running" : "runtime.stopped")}
        </span>
        <span>{t("workspace.localWorkspace")}</span>
      </footer>
    </div>
  );
}
export function WorkspaceHeading({
  page,
  actions,
}: {
  page: WorkspacePage;
  actions?: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <header className="ws-heading">
      <div>
        <h1>{t("workspace." + page)}</h1>
        <p>{t("workspace." + page + "Description")}</p>
      </div>
      {actions}
    </header>
  );
}
