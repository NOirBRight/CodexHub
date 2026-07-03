import type { GatewayClientContract, GatewayClientInfo } from "../lib/types";
import ompIcon from "../assets/omp-icon.png";
import opencodeIcon from "../assets/opencode-icon.png";
import piIcon from "../assets/pi-icon.png";
import zcodeIcon from "../assets/zcode-icon.png";
import { cx } from "../lib/format";
import { SegmentedSwitch } from "./SegmentedSwitch";

interface GatewayClientCardProps {
  busy?: boolean;
  client: GatewayClientContract;
  info?: GatewayClientInfo;
  onSwitchMode: (mode: "official" | "hub") => void;
}

export function GatewayClientCard({
  busy,
  client,
  info,
  onSwitchMode,
}: GatewayClientCardProps) {
  const routeMode = info?.route_mode === "hub" ? "hub" : "official";
  const hasInfo = Boolean(info);
  const installed = Boolean(info?.installed);
  const autoApplySupported = Boolean(info?.auto_apply_supported);
  const configPath = info?.config_path ?? client.config_path;
  const currentVersion = info?.current_version?.trim() || null;
  const latestVersion = info?.latest_version?.trim() || null;
  const hasUpdate = Boolean(currentVersion && latestVersion && currentVersion !== latestVersion);
  const routeDisabledReason = !installed
    ? `${info?.name ?? client.name} is not installed.`
    : !autoApplySupported
      ? "Managed config switching is not available for this client."
      : undefined;
  const statusLabel = !hasInfo
    ? "Checking"
    : installed
      ? "Installed"
      : "Not installed";
  const versionLabel = !hasInfo
    ? "Checking version"
    : !installed
      ? "Not installed"
      : currentVersion || latestVersion
        ? `Current ${currentVersion ?? "unknown"} · Latest ${latestVersion ?? "unknown"}`
        : "Version unknown";
  return (
    <section className="grid h-full min-h-0 content-between gap-1.5 rounded-md border border-line bg-white p-2 shadow-subtle">
      <div className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2">
        <ClientLogo id={client.id} name={info?.name ?? client.name} />
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-ink">{info?.name ?? client.name}</h3>
          <p className="truncate text-xs text-slate-500">{info?.kind ?? client.kind}</p>
        </div>
        <span
          className={cx(
            "rounded-sm border px-1.5 py-0.5 text-[11px] font-semibold",
            !hasInfo
              ? "border-line bg-panel text-slate-500"
              : installed
                ? "border-blue-200 bg-blue-50 text-blue-700"
                : "border-line bg-panel text-slate-500",
          )}
        >
          {statusLabel}
        </span>
      </div>

      <div title={routeDisabledReason}>
        <SegmentedSwitch
          ariaLabel={`${client.name} route mode`}
          className="grid-cols-2 [&_button]:min-h-7 [&_button]:py-1 [&_button]:text-xs"
          disabled={busy || Boolean(routeDisabledReason)}
          value={routeMode}
          options={[
            { value: "official", label: "Official" },
            { value: "hub", label: "CodexHub" },
          ]}
          onChange={onSwitchMode}
        />
      </div>

      <div className="grid min-w-0 gap-1 text-xs text-slate-600">
        <div className="flex min-w-0 items-center justify-between gap-2">
          <span className="shrink-0 font-semibold text-slate-500">Config</span>
          <code className="truncate font-mono">{configPath || "copy-only"}</code>
        </div>
        <div className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2">
          <span className="font-semibold text-slate-500">Version</span>
          <span className="truncate" title={versionLabel}>
            {versionLabel}
          </span>
          <button
            type="button"
            className="focus-ring inline-flex h-6 items-center justify-center rounded-md border border-line bg-panel px-2 text-[11px] font-semibold text-slate-400"
            disabled
            title={
              hasUpdate
                ? "A newer version is available, but a safe updater is not exposed by the backend yet."
                : "No client update is available."
            }
          >
            {hasUpdate ? "Manual" : "Update"}
          </button>
        </div>
      </div>
    </section>
  );
}

function ClientLogo({ id, name }: { id: string; name: string }) {
  const icon = clientIcon(id);
  if (icon) {
    return (
      <div
        className="grid h-7 w-7 shrink-0 place-items-center overflow-hidden rounded-md border border-line bg-white shadow-subtle"
        title={`${name} logo`}
        aria-hidden="true"
      >
        <img
          src={icon}
          alt=""
          className={clientIconClass(id)}
        />
      </div>
    );
  }

  return (
    <div
      className="grid h-7 w-7 shrink-0 place-items-center rounded-md border border-line bg-white text-[9px] font-black tracking-normal text-slate-600 shadow-subtle"
      title={`${name} official logo asset pending`}
      aria-hidden="true"
    >
      {id.slice(0, 2).toUpperCase()}
    </div>
  );
}

function clientIcon(id: string) {
  switch (id) {
    case "opencode":
      return opencodeIcon;
    case "zcode":
      return zcodeIcon;
    case "pi":
      return piIcon;
    case "omp":
      return ompIcon;
    default:
      return null;
  }
}

function clientIconClass(id: string) {
  if (id === "opencode") {
    return "h-6 w-6";
  }
  if (id === "pi") {
    return "h-full w-full scale-125 object-cover";
  }
  return "h-full w-full object-cover";
}
