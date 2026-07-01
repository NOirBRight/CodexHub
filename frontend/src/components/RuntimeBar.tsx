import { Play, Power, Settings as SettingsIcon, Square } from "lucide-react";
import codexLogo from "../assets/codex-logo.svg";
import { cx } from "../lib/format";
import type { AppStatus, Settings } from "../lib/types";
import { SegmentedSwitch } from "./SegmentedSwitch";

interface RuntimeBarProps {
  busy?: string | null;
  exportedCount: number;
  providerSourceCount: number;
  settings: Settings | null;
  status: AppStatus | null;
  onOpenSettings: () => void;
  onStart: () => void;
  onStop: () => void;
  onSwitchMode: (mode: "official" | "custom") => void;
}

export function RuntimeBar({
  busy,
  exportedCount,
  onOpenSettings,
  onStart,
  onStop,
  onSwitchMode,
  providerSourceCount,
  settings,
  status,
}: RuntimeBarProps) {
  const running = status?.proxy_running ?? false;
  const routeMode: "official" | "custom" = status?.mode === "custom" ? "custom" : "official";
  const port = status?.proxy_port ?? settings?.proxy_port ?? 9099;
  const address = `${settings?.gateway_bind_address || "127.0.0.1"}:${port}`;

  return (
    <header className="grid min-h-[62px] grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-4 border-b border-line bg-white px-4 shadow-subtle">
      <div className="flex min-w-0 items-center gap-2">
        <span className="grid h-8 w-8 place-items-center rounded-full bg-ink">
          <img src={codexLogo} alt="" className="h-5 w-5" aria-hidden="true" />
        </span>
        <span className="truncate text-base font-semibold text-ink">CodexHub</span>
      </div>

      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <FlowChip
          ok={providerSourceCount > 0}
          label="Providers"
          value={`Hub · ${providerSourceCount} sources`}
          title="External providers feed the Hub catalog used by Codex App"
        />
        <FlowChip
          ok={exportedCount > 0}
          label="Gateway"
          value={`Clients · ${exportedCount} exported`}
          title="Hub models exposed to external OpenAI-compatible clients"
        />
      </div>

      <div className="flex items-center gap-2">
        <div className="hidden items-center gap-2 xl:flex">
          <span className="text-xs font-semibold text-slate-500">Codex route</span>
          <SegmentedSwitch
            ariaLabel="Codex route"
            className="grid-cols-2"
            disabled={Boolean(busy)}
            value={routeMode}
            options={[
              { value: "official", label: "Official" },
              { value: "custom", label: "Hub" },
            ]}
            onChange={onSwitchMode}
          />
        </div>
        <div className="flex h-8 items-center gap-2 rounded-md border border-line bg-panel px-2 text-xs">
          <span className={cx("h-2 w-2 rounded-full", running ? "bg-ok" : "bg-danger")} />
          <code className="font-mono text-slate-700">{address}</code>
          <span className="text-slate-500">{running ? "running" : "stopped"}</span>
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
      </div>

      <div className="col-span-3 grid xl:hidden">
        <SegmentedSwitch
          ariaLabel="Codex route"
          className="grid-cols-2"
          disabled={Boolean(busy)}
          value={routeMode}
          options={[
            { value: "official", label: "Official" },
            { value: "custom", label: "Hub" },
          ]}
          onChange={onSwitchMode}
        />
      </div>
    </header>
  );
}

function FlowChip({
  label,
  ok,
  title,
  value,
}: {
  label: string;
  ok: boolean;
  title: string;
  value: string;
}) {
  return (
    <span
      className="inline-flex h-8 min-w-0 items-center gap-1.5 rounded-full border border-line bg-white px-3 text-xs text-slate-600"
      title={title}
    >
      <span className={cx("h-2 w-2 rounded-full", ok ? "bg-ok" : "bg-slate-300")} />
      <span className="font-semibold text-ink">{label}</span>
      <span className="text-slate-400">-&gt;</span>
      <span className="truncate">{value}</span>
    </span>
  );
}
