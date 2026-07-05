import { Play, Power, RefreshCcw, RotateCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { cx } from "../lib/format";
import { api, messageFromError } from "../lib/tauri";
import type { AppStatus } from "../lib/types";

interface ProxyStatusBarProps {
  refreshSignal: number;
  onStatusChange: (status: AppStatus | null) => void;
}

export function ProxyStatusBar({ refreshSignal, onStatusChange }: ProxyStatusBarProps) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<AppStatus | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await api.getStatus();
      setStatus(next);
      onStatusChange(next);
      setError(null);
    } catch (err) {
      setStatus(null);
      onStatusChange(null);
      setError(messageFromError(err));
    }
  }, [onStatusChange]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(timer);
  }, [load, refreshSignal]);

  async function run(label: string, action: () => Promise<AppStatus>) {
    setBusy(label);
    try {
      const next = await action();
      setStatus(next);
      onStatusChange(next);
      setError(null);
    } catch (err) {
      setError(messageFromError(err));
    } finally {
      setBusy(null);
    }
  }

  const running = status?.proxy_running ?? false;

  return (
    <footer className="grid min-h-[68px] grid-cols-1 gap-3 border-t border-line bg-white px-4 py-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
      <div className="flex min-w-0 flex-wrap items-center gap-3 text-sm">
        <span
          className={cx(
            "h-2.5 w-2.5 rounded-full",
            running ? "bg-ok" : "bg-danger",
          )}
        />
        <span className="font-semibold">{running ? t("runtime.proxyRunning") : t("runtime.proxyStopped")}</span>
        <span className="text-slate-500">{t("common.port")} {status?.proxy_port ?? 9099}</span>
        <span className="text-slate-500">{status?.mode ?? t("common.unknown")}</span>
        {status?.proxy_build && <span className="text-slate-500">{status.proxy_build}</span>}
        {error ? (
          <span className="min-w-0 truncate text-danger">{error}</span>
        ) : (
          <span className="min-w-0 truncate text-slate-500">{status?.message}</span>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <ActionButton
          icon={<Play size={16} />}
          label={t("common.start")}
          title={t("runtime.startRuntime")}
          disabled={Boolean(busy) || running}
          onClick={() => run("start", api.startProxy)}
        />
        <ActionButton
          icon={<Power size={16} />}
          label={t("common.stop")}
          title={t("runtime.stopRuntime")}
          disabled={Boolean(busy) || !running}
          onClick={() => run("stop", api.stopProxy)}
        />
        <ActionButton
          icon={<RotateCw size={16} />}
          label={t("common.restart")}
          title={t("runtime.restartProxy")}
          disabled={Boolean(busy)}
          onClick={() => run("restart", api.restartProxy)}
        />
        <ActionButton
          icon={<RefreshCcw size={16} />}
          label={t("common.refresh")}
          title={t("runtime.refreshStatus")}
          disabled={Boolean(busy)}
          onClick={load}
        />
      </div>
    </footer>
  );
}

function ActionButton({
  disabled,
  icon,
  label,
  onClick,
  title,
}: {
  disabled?: boolean;
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
  title: string;
}) {
  return (
    <button
      type="button"
      className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md border border-line bg-panel px-3 text-sm font-semibold text-ink hover:bg-slate-100"
      disabled={disabled}
      onClick={onClick}
      title={title}
    >
      {icon}
      {label}
    </button>
  );
}
