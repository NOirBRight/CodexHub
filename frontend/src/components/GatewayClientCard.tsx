import { AlertTriangle, FileText } from "lucide-react";
import { useTranslation } from "react-i18next";
import ompIcon from "../assets/omp-icon.png";
import opencodeIcon from "../assets/opencode-icon.png";
import piIcon from "../assets/pi-icon.png";
import zcodeIcon from "../assets/zcode-icon.png";
import { cx } from "../lib/format";
import type { GatewayClientContract, GatewayClientInfo } from "../lib/types";
import { SwitchControl } from "./SettingsDrawer";

export type ClientConnectionState = "connected" | "disconnected" | "busy" | "drift" | "unavailable";

interface GatewayClientCardProps {
  busy?: boolean;
  client: GatewayClientContract;
  enabledModelCount?: number;
  info?: GatewayClientInfo;
  onToggle: (connect: boolean) => void;
}

export function GatewayClientCard({
  busy,
  client,
  enabledModelCount,
  info,
  onToggle,
}: GatewayClientCardProps) {
  const { t } = useTranslation();
  const state = connectionStateFromInfo(info, busy);
  const installed = Boolean(info?.installed);
  const configPath = info?.config_path ?? client.config_path;
  const currentVersion = info?.current_version?.trim() || null;
  const kindLabel = info?.kind ?? t("gateway.clientKind." + client.id);
  const name = info?.name ?? client.name;
  const checked = state === "connected" || state === "busy" || state === "drift";
  const disabled = state === "unavailable" || state === "busy" || !info;
  const label = busy
    ? t("gateway.connectionUpdating")
    : state === "connected"
      ? t("gateway.connected")
      : state === "drift"
        ? t("gateway.connectionRepair")
        : state === "unavailable"
          ? t("gateway.connectionUnavailable")
          : t("gateway.connectionDisconnected");
  const labelTone =
    state === "connected" || state === "busy"
      ? "text-action"
      : state === "drift"
        ? "text-warn"
        : "text-muted";

  return (
    <section
      className={cx(
        "rounded-inner bg-surface px-3 py-2.5 shadow-control transition-[box-shadow,opacity,background-color]",
        state === "connected" && "shadow-raised",
        state === "drift" && "bg-amber-50/30 ring-1 ring-amber-300/70",
        state === "unavailable" && "opacity-55",
      )}
    >
      <div className="flex items-center gap-2.5">
        <span className="grid h-9 w-9 shrink-0 place-items-center rounded-control bg-panel-soft shadow-control">
          <ClientLogo id={client.id} name={name} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="truncate text-sm font-semibold text-ink">{name}</h3>
            {currentVersion && (
              <span className="rounded-control bg-panel px-1.5 py-0.5 text-[9px] font-medium text-muted">
                {currentVersion}
              </span>
            )}
          </div>
          <p className="mt-0.5 truncate text-[11px] text-muted">{kindLabel}</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span className={cx("text-xs font-medium", labelTone)}>{label}</span>
          <SwitchControl
            ariaLabel={t("gateway.routeMode", { name })}
            checked={checked}
            disabled={disabled}
            tone={state === "drift" ? "warn" : "action"}
            onChange={onToggle}
          />
        </div>
      </div>
      <div className="mt-2 flex items-center gap-2 rounded-inner bg-panel-soft px-2.5 py-1.5">
        <FileText className="h-3 w-3 shrink-0 text-muted" />
        <code className="min-w-0 flex-1 truncate font-mono text-[10px] text-ink">
          {configPath || t("common.copyOnly")}
        </code>
      </div>
      <div className="mt-1.5 flex min-h-3.5 items-center gap-1.5 text-[10px] text-muted">
        <ConnectionNarrative
          clientId={client.id}
          enabledModelCount={enabledModelCount}
          installed={installed}
          state={state}
          onRepair={() => onToggle(true)}
        />
      </div>
    </section>
  );
}

function ConnectionNarrative({
  clientId,
  enabledModelCount,
  installed,
  onRepair,
  state,
}: {
  clientId: string;
  enabledModelCount?: number;
  installed: boolean;
  onRepair: () => void;
  state: ClientConnectionState;
}) {
  const { t } = useTranslation();
  if (state === "busy") {
    return (
      <>
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500" />
        <span>{t("gateway.updatingClientConfig")}</span>
      </>
    );
  }
  if (state === "drift") {
    return (
      <>
        <AlertTriangle className="h-3 w-3 text-amber-600" />
        <button type="button" className="text-left text-amber-700 underline-offset-2 hover:underline" onClick={onRepair}>
          {t("gateway.configDriftRepair")}
        </button>
      </>
    );
  }
  if (state === "unavailable" || !installed) {
    return (
      <>
        <span className="h-1.5 w-1.5 rounded-full bg-slate-300" />
        <span>{t("gateway.installToConnect")}</span>
      </>
    );
  }
  if (state === "connected") {
    return (
      <>
        <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
        <span>
          {clientId === "dsh"
            ? t("gateway.injectedProvider", { count: enabledModelCount ?? 0 })
            : t("gateway.connectedViaHub")}
        </span>
      </>
    );
  }
  return (
    <>
      <span className="h-1.5 w-1.5 rounded-full bg-slate-300" />
      <span>{t("gateway.configUnchanged")}</span>
    </>
  );
}

export function connectionStateFromInfo(
  info: GatewayClientInfo | undefined,
  busy?: boolean,
): ClientConnectionState {
  if (busy) {
    return "busy";
  }
  if (!info) {
    return "disconnected";
  }
  if (!info.installed) {
    return "unavailable";
  }
  if (info.route_mode === "stale") {
    return "drift";
  }
  if (
    info.route_mode === "other_channel" &&
    (info.route_owner === "release" || info.route_owner === "beta")
  ) {
    return "connected";
  }
  if (info.route_mode === "hub" || info.route_mode === "release" || info.route_mode === "beta") {
    return "connected";
  }
  return "disconnected";
}

function ClientLogo({ id, name }: { id: string; name: string }) {
  if (id === "dsh") {
    return <DshIcon className="h-5 w-5" />;
  }
  const icon = clientIcon(id);
  if (icon) {
    return (
      <img src={icon} alt="" title={name + " logo"} className={clientIconClass(id)} aria-hidden="true" />
    );
  }
  return (
    <span className="text-[9px] font-black tracking-normal text-slate-600" aria-hidden="true">
      {id.slice(0, 2).toUpperCase()}
    </span>
  );
}

function DshIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      <path d="M12 2.5 20 12l-8 9.5L4 12z" fill="#4D6BFE" />
      <path d="M12 7 16 12l-4 5-4-5z" fill="#fff" opacity="0.85" />
    </svg>
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
  if (id === "pi") {
    return "h-full w-full scale-125 object-cover";
  }
  return "h-5 w-5 object-cover";
}
