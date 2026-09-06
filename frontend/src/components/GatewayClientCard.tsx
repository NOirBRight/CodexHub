import { useState } from "react";
import { WorkspaceDialog } from "./workspace/WorkspaceDialog";
import { api, messageFromError } from "../lib/tauri";
import {
  MoreHorizontal,
  RefreshCcw,
  AlertTriangle,
  FileText,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import ompIcon from "../assets/omp-icon.png";
import opencodeIcon from "../assets/opencode-icon.png";
import piIcon from "../assets/pi-icon.png";
import dshIcon from "../assets/dsh-icon.svg";
import zcodeIcon from "../assets/zcode-icon.png";
import codexIcon from "../assets/codex-logo.svg";
import { cx } from "../lib/format";
import type { GatewayClientContract, GatewayClientInfo } from "../lib/types";
import { SwitchControl } from "./SettingsDrawer";

export type ClientConnectionState =
  | "connected"
  | "disconnected"
  | "busy"
  | "drift"
  | "unavailable";

interface GatewayClientCardProps {
  busy?: boolean;
  className?: string;
  client: GatewayClientContract;
  enabledModelCount?: number;
  info?: GatewayClientInfo;
  onToggle: (connect: boolean) => void;
  onRefresh?: () => Promise<void>;
}

export function GatewayClientCard({
  busy,
  className,
  client,
  enabledModelCount,
  info,
  onToggle,
  onRefresh,
}: GatewayClientCardProps) {
  const { t } = useTranslation();
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [preview, setPreview] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailBusy, setDetailBusy] = useState(false);
  async function loadPreview() {
    setDetailBusy(true);
    setDetailError(null);
    try {
      const result = await api.previewGatewayClientConfig(client.id);
      setPreview(result.next_redacted);
    } catch (error) {
      setDetailError(messageFromError(error));
    } finally {
      setDetailBusy(false);
    }
  }
  const state = connectionStateFromInfo(info, busy);
  const installed = Boolean(info?.installed);
  const configPath = info?.config_path ?? client.config_path;
  const kindLabel = info?.kind ?? t("gateway.clientKind." + client.id);
  const name = info?.name ?? client.name;
  const checked = state === "connected" || state === "busy";
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
    <>
      <section
        className={cx(
          "ws-client-card",
          state === "connected" && "connected",
          state === "unavailable" && "unavailable",
          className,
        )}
      >
        <div className="ws-client-heading">
          <span className="ws-client-logo">
            <ClientLogo id={client.id} name={name} />
          </span>
          <div>
            <h3>{name}</h3>
            <small>{kindLabel}</small>
          </div>
          <button
            className="ws-icon"
            aria-label={t("workspace.clientDetails", { name })}
            onClick={() => setDetailsOpen(true)}
          >
            <MoreHorizontal size={15} />
          </button>
        </div>
        <div className="ws-client-config">
          <FileText size={12} />
          <code title={configPath || ""}>
            {configPath || t("common.copyOnly")}
          </code>
        </div>
        <div className="ws-client-bottom">
          <div>
            <span className={labelTone}>{label}</span>
            <small>
              <ConnectionNarrative
                clientId={client.id}
                enabledModelCount={enabledModelCount}
                installed={installed}
                state={state}
                onRepair={() => onToggle(true)}
              />
            </small>
          </div>
          <SwitchControl
            ariaLabel={t("gateway.routeMode", { name })}
            checked={checked}
            disabled={disabled}
            tone={state === "drift" ? "warn" : "action"}
            onChange={onToggle}
          />
        </div>
      </section>
      <WorkspaceDialog
        open={detailsOpen}
        title={t("workspace.clientDetails", { name })}
        onClose={() => setDetailsOpen(false)}
        actions={
          <button
            className="ws-primary"
            disabled={disabled || detailBusy}
            onClick={() => onToggle(!checked)}
          >
            {label} ·{" "}
            {t(checked ? "workspace.disconnect" : "workspace.connect")}
          </button>
        }
      >
        <div className="ws-client-detail-heading">
          <ClientLogo id={client.id} name={name} />
          <div>
            <b>{name}</b>
            <small>{kindLabel}</small>
          </div>
        </div>
        <dl className="ws-detail-list">
          <div>
            <dt>{t("workspace.connectionState")}</dt>
            <dd>{label}</dd>
          </div>
          <div>
            <dt>{t("workspace.configPath")}</dt>
            <dd>
              <code>{configPath || "—"}</code>
            </dd>
          </div>
          <div>
            <dt>{t("workspace.installedVersion")}</dt>
            <dd>{info?.current_version || t("common.unknown")}</dd>
          </div>
          <div>
            <dt>{t("workspace.latestVersion")}</dt>
            <dd>{info?.latest_version || t("workspace.notChecked")}</dd>
          </div>
          <div>
            <dt>{t("workspace.ownership")}</dt>
            <dd>{info?.route_owner || "—"}</dd>
          </div>
        </dl>
        <div className="ws-actions">
          {onRefresh && (
            <button
              className="ws-button"
              disabled={detailBusy}
              onClick={async () => {
                setDetailBusy(true);
                try {
                  await onRefresh();
                } finally {
                  setDetailBusy(false);
                }
              }}
            >
              <RefreshCcw size={12} />
              {t("gateway.refreshClients")}
            </button>
          )}
          <button
            className="ws-button"
            disabled={detailBusy}
            onClick={() =>
              preview !== null ? setPreview(null) : void loadPreview()
            }
          >
            {t(
              preview !== null
                ? "workspace.hidePreview"
                : "workspace.configPreview",
            )}
          </button>
        </div>
        {detailError && (
          <p className="text-danger" role="alert">
            {detailError}
          </p>
        )}
        {preview !== null && <pre className="ws-config-preview">{preview}</pre>}
      </WorkspaceDialog>
    </>
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
        <button
          type="button"
          className="text-left text-amber-700 underline-offset-2 hover:underline"
          onClick={onRepair}
        >
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
  if (
    info.route_mode === "hub" ||
    info.route_mode === "release" ||
    info.route_mode === "beta"
  ) {
    return "connected";
  }
  return "disconnected";
}

function ClientLogo({ id, name }: { id: string; name: string }) {
  const icon = clientIcon(id);
  if (icon) {
    return (
      <img
        src={icon}
        alt=""
        title={name + " logo"}
        className={clientIconClass(id)}
        aria-hidden="true"
      />
    );
  }
  return (
    <span
      className="text-[9px] font-black tracking-normal text-slate-600"
      aria-hidden="true"
    >
      {id.slice(0, 2).toUpperCase()}
    </span>
  );
}

function clientIcon(id: string) {
  switch (id) {
    case "codex":
    case "codex-app":
    case "chatgpt":
      return codexIcon;
    case "dsh":
      return dshIcon;
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
  if (id === "codex" || id === "dsh") {
    return "h-8 w-8 object-contain";
  }
  if (id === "pi") {
    return "h-full w-full scale-125 object-cover";
  }
  return "h-5 w-5 object-cover";
}
