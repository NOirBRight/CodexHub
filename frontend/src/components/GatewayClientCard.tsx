import type { GatewayClientContract, GatewayClientInfo } from "../lib/types";
import { useTranslation } from "react-i18next";
import ompIcon from "../assets/omp-icon.png";
import opencodeIcon from "../assets/opencode-icon.png";
import piIcon from "../assets/pi-icon.png";
import zcodeIcon from "../assets/zcode-icon.png";
import { cx } from "../lib/format";
import { SegmentedSwitch, type SegmentedOption } from "./SegmentedSwitch";

type RouteMode = "official" | "hub";
type DisplayRouteMode = RouteMode | "unknown";

interface GatewayClientCardProps {
  busy?: boolean;
  busyMode?: RouteMode | null;
  client: GatewayClientContract;
  info?: GatewayClientInfo;
  onSwitchMode: (mode: RouteMode) => void;
}

export function GatewayClientCard({
  busy,
  busyMode,
  client,
  info,
  onSwitchMode,
}: GatewayClientCardProps) {
  const { t } = useTranslation();
  const routeMode = routeModeFromInfo(info);
  const routeValue = routeMode === "unknown" ? null : routeMode;
  const pendingRouteValue = busy ? busyMode ?? null : null;
  const hasInfo = Boolean(info);
  const installed = Boolean(info?.installed);
  const autoApplySupported = Boolean(info?.auto_apply_supported);
  const configPath = info?.config_path ?? client.config_path;
  const currentVersion = info?.current_version?.trim() || null;
  const latestVersion = info?.latest_version?.trim() || null;
  const hasUpdate = Boolean(currentVersion && latestVersion && currentVersion !== latestVersion);
  const kindLabel = info?.kind ?? t(`gateway.clientKind.${client.id}`);
  const routeOptions: Array<SegmentedOption<RouteMode>> = [
    { value: "official", label: t("common.official") },
    { value: "hub", label: t("common.codexHub") },
  ];
  const updateLabel = hasUpdate ? t("gateway.manualUpdateAvailable") : t("gateway.noUpdateAction");
  const routeDisabledReason = !installed
    ? t("gateway.notInstalled")
    : !autoApplySupported
      ? t("gateway.configUnavailable")
      : undefined;
  const routeTitle = busy
    ? t("gateway.switchingRoute", { name: info?.name ?? client.name })
    : routeDisabledReason ??
      (routeMode === "unknown" ? t("gateway.routeUnknownTitle") : undefined);
  const statusLabel = busy
    ? t("gateway.switching")
    : !hasInfo
    ? t("gateway.checking")
    : !installed
      ? t("gateway.notInstalled")
      : routeMode === "unknown"
        ? t("gateway.routeUnknown")
        : t("gateway.installed");
  const statusClass = busy
    ? "border-amber-200 bg-amber-50 text-amber-700"
    : !hasInfo
      ? "border-line bg-panel text-slate-500"
      : !installed
        ? "border-line bg-panel text-slate-500"
        : routeMode === "unknown"
          ? "border-amber-200 bg-amber-50 text-amber-700"
          : "border-blue-200 bg-blue-50 text-blue-700";
  const versionLabel = !hasInfo
    ? t("gateway.checkingVersion")
    : !installed
      ? t("gateway.notInstalled")
      : currentVersion || latestVersion
        ? t("gateway.currentLatest", {
            current: currentVersion ?? t("common.unknown").toLowerCase(),
            latest: latestVersion ?? t("common.unknown").toLowerCase(),
          })
        : t("gateway.versionUnknown");
  return (
    <section className="grid h-full min-h-[136px] content-between gap-1.5 rounded-panel bg-surface p-2 shadow-card">
      <div className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2">
        <ClientLogo id={client.id} name={info?.name ?? client.name} />
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-ink">{info?.name ?? client.name}</h3>
          <p className="truncate text-xs text-slate-500">{kindLabel}</p>
        </div>
        <span
          className={cx(
            "rounded-full px-2 py-0.5 text-[11px] font-semibold shadow-control",
            statusClass,
          )}
        >
          {statusLabel}
        </span>
      </div>

      <div title={routeTitle}>
        <SegmentedSwitch
          ariaLabel={t("gateway.routeMode", { name: client.name })}
          className="grid-cols-2 [&_button]:min-h-7 [&_button]:py-1 [&_button]:text-xs"
          disabled={busy || Boolean(routeDisabledReason)}
          pendingValue={pendingRouteValue}
          value={routeValue}
          options={routeOptions}
          onChange={onSwitchMode}
        />
      </div>

      <div className="grid min-w-0 gap-1 text-xs text-slate-600">
        <div className="flex min-w-0 items-center justify-between gap-2">
          <span className="shrink-0 font-semibold text-slate-500">{t("common.config")}</span>
          <code className="truncate font-mono">{configPath || t("common.copyOnly")}</code>
        </div>
        <div className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2">
          <span className="font-semibold text-slate-500">{t("common.version")}</span>
          <span className="truncate" title={versionLabel}>
            {versionLabel}
          </span>
          <span
            className={cx(
              "inline-flex h-6 items-center justify-center rounded-md border px-2 text-[11px] font-semibold",
              hasUpdate
                ? "bg-amber-50 text-amber-700"
                : "bg-panel text-slate-400",
            )}
            title={hasUpdate ? t("gateway.installUpdateManually") : t("gateway.noClientUpdateAction")}
          >
            {updateLabel}
          </span>
        </div>
      </div>
    </section>
  );
}

function routeModeFromInfo(info?: GatewayClientInfo): DisplayRouteMode {
  if (info?.route_mode === "official" || info?.route_mode === "hub") {
    return info.route_mode;
  }
  return "unknown";
}

function ClientLogo({ id, name }: { id: string; name: string }) {
  const icon = clientIcon(id);
  if (icon) {
    return (
      <div
        className="grid h-7 w-7 shrink-0 place-items-center overflow-hidden rounded-control bg-surface shadow-control"
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
      className="grid h-7 w-7 shrink-0 place-items-center rounded-control bg-surface text-[9px] font-black tracking-normal text-slate-600 shadow-control"
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
