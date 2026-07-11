import type { GatewayClientContract, GatewayClientInfo, RoutingOwner } from "../lib/types";
import { useTranslation } from "react-i18next";
import ompIcon from "../assets/omp-icon.png";
import opencodeIcon from "../assets/opencode-icon.png";
import piIcon from "../assets/pi-icon.png";
import zcodeIcon from "../assets/zcode-icon.png";
import { cx } from "../lib/format";
import { SegmentedSwitch, type SegmentedOption } from "./SegmentedSwitch";

type RouteAction = "official" | "current_owner" | "takeover";
type DisplayRouteMode = RoutingOwner | "hub" | "stale" | "unknown";
type ClientStatusKind = "checking" | "not_installed" | "installed" | "ready" | "pending_sync" | "unknown";

interface GatewayClientCardProps {
  busy?: boolean;
  busyMode?: RouteAction | null;
  client: GatewayClientContract;
  info?: GatewayClientInfo;
  onSwitchMode: (mode: RouteAction) => void;
  runtimeOwner: RoutingOwner | null;
}

export function GatewayClientCard({
  busy,
  busyMode,
  client,
  info,
  onSwitchMode,
  runtimeOwner,
}: GatewayClientCardProps) {
  const { t } = useTranslation();
  const routeMode = routeModeFromInfo(info);
  const routeOwner = info?.route_owner ?? "unknown_external";
  const runtimeOwnerAvailable = runtimeOwner !== null;
  const routeValue = routeOwner === "official" ? "official" : "current_owner";
  const pendingRouteValue = busy && busyMode !== "takeover" ? busyMode ?? null : null;
  const hasInfo = Boolean(info);
  const installed = Boolean(info?.installed);
  const autoApplySupported = Boolean(info?.auto_apply_supported);
  const configPath = info?.config_path ?? client.config_path;
  const currentVersion = info?.current_version?.trim() || null;
  const latestVersion = info?.latest_version?.trim() || null;
  const versionsChecked = Boolean(info?.versions_checked);
  const kindLabel = info?.kind ?? t(`gateway.clientKind.${client.id}`);
  const takeoverRequired = routeOwner !== "official" && info?.managed_by_current_app === false;
  const routeOwnerLabel = takeoverRequired ? ownerDisplayName(routeOwner, t) : ownerDisplayName(runtimeOwner, t);
  const routeOptions: Array<SegmentedOption<RouteAction>> = [
    { value: "official", label: t("common.official") },
    {
      value: "current_owner",
      label: runtimeOwnerAvailable
        ? `${t("common.codexHub")} · ${routeOwnerLabel}`
        : t("gateway.ownerUnavailable"),
    },
  ];
  const routeDisabledReason = !runtimeOwnerAvailable
    ? t("gateway.ownerUnavailable")
    : !installed
      ? t("gateway.notInstalled")
      : !autoApplySupported
        ? t("gateway.configUnavailable")
        : undefined;
  const routeTitle = busy
    ? t("gateway.switchingRoute", { name: info?.name ?? client.name })
    : routeDisabledReason ??
      (routeMode === "stale"
        ? t("gateway.routePendingSyncTitle")
        : routeMode === "unknown"
          ? t("gateway.routeUnknownTitle")
          : undefined);
  const statusKind: ClientStatusKind = !hasInfo
    ? "checking"
    : !installed
      ? "not_installed"
      : routeMode === "stale"
        ? "pending_sync"
        : routeMode === "hub" || routeMode === "release" || routeMode === "beta"
          ? "ready"
          : routeMode === "official"
            ? "installed"
            : "unknown";
  const statusLabel = busy
    ? t("gateway.switching")
    : statusKind === "checking"
    ? t("gateway.checking")
    : statusKind === "not_installed"
      ? t("gateway.notInstalled")
      : statusKind === "pending_sync"
        ? t("gateway.routePendingSync")
      : statusKind === "ready"
        ? t("gateway.routeReady")
      : statusKind === "unknown"
        ? t("gateway.routeUnknown")
        : t("gateway.installed");
  const statusClass = busy
    ? "border-amber-200 bg-amber-50 text-amber-700"
    : statusKind === "checking"
      ? "border-line bg-panel text-slate-500"
      : statusKind === "not_installed"
        ? "border-line bg-panel text-slate-500"
        : statusKind === "pending_sync"
          ? "border-amber-200 bg-amber-50 text-amber-700"
        : statusKind === "ready"
          ? "border-emerald-200 bg-emerald-50 text-emerald-700"
        : statusKind === "unknown"
          ? "border-amber-200 bg-amber-50 text-amber-700"
          : "border-blue-200 bg-blue-50 text-blue-700";
  const statusTitle =
    statusKind === "pending_sync"
      ? routeTitle
      : statusKind === "ready"
        ? info?.status
        : statusKind === "unknown"
          ? routeTitle
          : undefined;
  const versionLabel = !hasInfo
    ? t("gateway.checkingVersion")
    : !installed
      ? t("gateway.notInstalled")
      : !versionsChecked
        ? t("gateway.versionNotChecked")
      : currentVersion || latestVersion
        ? t("gateway.currentLatest", {
            current: currentVersion ?? t("common.unknown").toLowerCase(),
            latest: latestVersion ?? t("common.unknown").toLowerCase(),
          })
        : t("gateway.versionUnknown");
  return (
    <section
      className={cx(
        "grid h-full min-h-[136px] content-between gap-1.5 rounded-panel p-2 shadow-card",
        statusKind === "not_installed" ? "bg-panel opacity-75 grayscale" : "bg-surface",
      )}
    >
      <div className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2">
        <ClientLogo id={client.id} name={info?.name ?? client.name} />
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-ink">{info?.name ?? client.name}</h3>
          <p className="truncate text-xs text-slate-500">{kindLabel}</p>
        </div>
        {statusKind === "pending_sync" ? (
          <button
            type="button"
            className={cx(
              "focus-ring rounded-full border px-2 py-0.5 text-[11px] font-semibold shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96]",
              statusClass,
            )}
            disabled={busy || Boolean(routeDisabledReason)}
            onClick={() => onSwitchMode("current_owner")}
            title={statusTitle}
          >
            {statusLabel}
          </button>
        ) : (
          <span
            className={cx(
              "rounded-full border px-2 py-0.5 text-[11px] font-semibold shadow-control",
              statusClass,
            )}
            title={statusTitle}
          >
            {statusLabel}
          </span>
        )}
      </div>

      <div className="grid min-w-0 gap-1 text-xs text-slate-600">
        <div className="grid min-w-0 grid-cols-[56px_minmax(0,1fr)] items-center gap-2">
          <span className="font-semibold text-slate-500">{t("common.config")}</span>
          <code className="truncate text-left font-mono">{configPath || t("common.copyOnly")}</code>
        </div>
        <div className="grid min-w-0 grid-cols-[56px_minmax(0,1fr)] items-center gap-2">
          <span className="font-semibold text-slate-500">{t("common.version")}</span>
          <span className="truncate" title={versionLabel}>
            {versionLabel}
          </span>
        </div>
      </div>

      <div title={routeTitle}>
        <SegmentedSwitch
          activeTone={takeoverRequired ? "foreign" : "default"}
          ariaLabel={t("gateway.routeMode", { name: client.name })}
          className="grid-cols-2 [&_button]:min-h-8 [&_button]:py-1 [&_button]:text-xs"
          disabled={busy || Boolean(routeDisabledReason)}
          pendingValue={pendingRouteValue}
          value={routeValue}
          options={routeOptions}
          onChange={(mode) => onSwitchMode(takeoverRequired && mode === "current_owner" ? "takeover" : mode)}
        />
      </div>
    </section>
  );
}

function routeModeFromInfo(info?: GatewayClientInfo): DisplayRouteMode {
  if (
    info?.route_mode === "official" ||
    info?.route_mode === "release" ||
    info?.route_mode === "beta" ||
    info?.route_mode === "hub" ||
    info?.route_mode === "stale"
  ) {
    return info.route_mode;
  }
  return "unknown";
}

function ownerDisplayName(owner: RoutingOwner | null, t: (key: string) => string) {
  if (owner === "release") {
    return t("gateway.ownerRelease");
  }
  if (owner === "beta") {
    return t("gateway.ownerBeta");
  }
  if (owner === "unknown_external") {
    return t("gateway.ownerExternal");
  }
  return owner === "official" ? t("common.official") : t("gateway.ownerUnavailable");
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
