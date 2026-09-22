import { useEffect, useState } from "react";
import { WorkspaceDialog } from "./workspace/WorkspaceDialog";
import { DefaultSubagentPicker } from "./workspace/ProviderWorkspaceView";
import { api, messageFromError } from "../lib/tauri";
import {
  MoreHorizontal,
  RefreshCcw,
  FileText,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import ompIcon from "../assets/omp-icon.png";
import opencodeIcon from "../assets/opencode-icon.png";
import piIcon from "../assets/pi-icon.png";
import dshIcon from "../assets/dsh-icon.svg";
import zcodeIcon from "../assets/zcode-icon.png";
import codexIcon from "../assets/codex-logo.svg";
import grokIcon from "../assets/grok-icon.svg";
import { cx } from "../lib/format";
import {
  connectionStateFromInfo,
  switchCheckedFromState,
  type ClientConnectionState,
} from "../lib/clientConnectionState";
import type { GatewayClientContract, GatewayClientInfo } from "../lib/types";
import { SwitchControl } from "./SettingsDrawer";
import {
  resolveSubagentEffort,
  type DefaultSubagentOption,
} from "../lib/defaultSubagent";

export type { ClientConnectionState };
export { connectionStateFromInfo };

export interface ExportedGatewayModel {
  id: string;
  label: string;
}

interface GatewayClientCardProps {
  busy?: boolean;
  className?: string;
  client: GatewayClientContract;
  enabledModelCount?: number;
  exportedModels?: ExportedGatewayModel[];
  info?: GatewayClientInfo;
  defaultSubagent?: {
    model: string;
    effort: string;
    options: DefaultSubagentOption[];
    onChange: (model: string, effort: string) => void;
  };
  onToggle: (
    connect: boolean,
    model?: string | null,
    roleMappings?: Record<string, string> | null,
  ) => void;
  onRefresh?: () => Promise<void>;
}

export function GatewayClientCard({
  busy,
  className,
  client,
  enabledModelCount,
  exportedModels = [],
  info,
  defaultSubagent,
  onToggle,
  onRefresh,
}: GatewayClientCardProps) {
  const { t } = useTranslation();
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [preview, setPreview] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailBusy, setDetailBusy] = useState(false);
  const [claudeQuery, setClaudeQuery] = useState("");
  const [claudeDefault, setClaudeDefault] = useState(exportedModels[0]?.id ?? "");
  const [claudeConfirmed, setClaudeConfirmed] = useState(false);
  const [claudeRoles, setClaudeRoles] = useState<Record<string, string>>({
    haiku: "",
    sonnet: "",
    opus: "",
    fable: "",
    subagent: "",
  });
  const exportedIds = new Set(exportedModels.map((model) => model.id));
  const claudeMappingInvalid = Object.values(claudeRoles).some(
    (value) => value && !exportedIds.has(value),
  );
  const isClaude = client.id === "claude";
  useEffect(() => {
    if (!claudeDefault && exportedModels[0]) {
      setClaudeDefault(exportedModels[0].id);
    }
  }, [claudeDefault, exportedModels]);
  const visibleClaudeModels = exportedModels.filter((model) => {
    const query = claudeQuery.trim().toLowerCase();
    if (!query) return true;
    return (
      model.id.toLowerCase().includes(query) ||
      model.label.toLowerCase().includes(query)
    );
  });
  function requestToggle(connect: boolean) {
    if (isClaude && connect && !claudeConfirmed) {
      setDetailsOpen(true);
      return;
    }
    onToggle(
      connect,
      isClaude ? claudeDefault || null : null,
      isClaude ? claudeRoles : null,
    );
  }
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
  const checked = switchCheckedFromState(state);
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
        <div className="ws-client-footer">
          {defaultSubagent ? (
            <DefaultSubagentPicker
              disabled={Boolean(busy)}
              model={defaultSubagent.model}
              effort={
                defaultSubagent.model
                  ? resolveSubagentEffort(
                      defaultSubagent.options.find(
                        (option) => option.id === defaultSubagent.model,
                      ),
                      defaultSubagent.effort,
                    )
                  : ""
              }
              options={defaultSubagent.options}
              selected={defaultSubagent.options.find(
                (option) => option.id === defaultSubagent.model,
              )}
              emptyLabel={t("workspace.defaultSubagentCliDefault")}
              onChange={defaultSubagent.onChange}
            />
          ) : null}
          <div className="ws-client-bottom">
            <div className={cx("ws-client-status", labelTone)}>
              <ConnectionNarrative
                clientId={client.id}
                enabledModelCount={enabledModelCount}
                installed={installed}
                state={state}
                onRepair={() => requestToggle(true)}
              />
            </div>
            <SwitchControl
              ariaLabel={t("gateway.routeMode", { name })}
              checked={checked}
              disabled={disabled}
              tone={state === "drift" ? "warn" : "action"}
              onChange={requestToggle}
            />
          </div>
        </div>
      </section>
      <WorkspaceDialog
        open={detailsOpen}
        title={t("workspace.clientDetails", { name })}
        onClose={() => setDetailsOpen(false)}
        actions={
          <button
            className="ws-primary"
            disabled={
              disabled ||
              detailBusy ||
              (isClaude && !checked && (!claudeConfirmed || claudeMappingInvalid))
            }
            onClick={() => requestToggle(!checked)}
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
          {isClaude ? (
            <div>
              <dt>{t("workspace.note")}</dt>
              <dd>{t("gateway.claudeCompatibilityState")}</dd>
            </div>
          ) : null}
          {info?.status?.includes("allowed_models") ? (
            <div>
              <dt>{t("workspace.note")}</dt>
              <dd>{t("gateway.grokAllowedModelsMayHide")}</dd>
            </div>
          ) : null}
        </dl>
        {isClaude ? (
          <div className="ws-detail-list" style={{ display: "grid", gap: 8 }}>
            <p>{t("gateway.claudeConnectScope")}</p>
            <p>{t("gateway.claudeRestartRequired")}</p>
            <label>
              {t("gateway.claudeSearchModels")}
              <input
                value={claudeQuery}
                onChange={(event) => setClaudeQuery(event.target.value)}
                aria-label={t("gateway.claudeSearchModels")}
              />
            </label>
            <label>
              {t("gateway.claudeDefaultModel")}
              <select
                value={claudeDefault}
                onChange={(event) => setClaudeDefault(event.target.value)}
                aria-label={t("gateway.claudeDefaultModel")}
              >
                {visibleClaudeModels.length === 0 ? (
                  <option value="">{t("gateway.claudeNoModels")}</option>
                ) : (
                  visibleClaudeModels.map((model) => (
                    <option key={model.id} value={model.id}>
                      {model.label}
                    </option>
                  ))
                )}
              </select>
            </label>
            {(
              [
                ["haiku", "gateway.claudeRoleHaiku"],
                ["sonnet", "gateway.claudeRoleSonnet"],
                ["opus", "gateway.claudeRoleOpus"],
                ["fable", "gateway.claudeRoleFable"],
                ["subagent", "gateway.claudeRoleSubagent"],
              ] as const
            ).map(([role, labelKey]) => {
              const value = claudeRoles[role] ?? "";
              const invalid = Boolean(value) && !exportedIds.has(value);
              return (
                <label key={role}>
                  {t(labelKey)}
                  <select
                    value={value}
                    aria-invalid={invalid}
                    aria-label={t(labelKey)}
                    onChange={(event) =>
                      setClaudeRoles((current) => ({
                        ...current,
                        [role]: event.target.value,
                      }))
                    }
                  >
                    <option value="">{t("gateway.claudeRoleUnmapped")}</option>
                    {exportedModels.map((model) => (
                      <option key={model.id} value={model.id}>
                        {model.label}
                      </option>
                    ))}
                  </select>
                  {invalid ? (
                    <small role="status">{t("gateway.claudeRoleInvalid")}</small>
                  ) : null}
                </label>
              );
            })}
            <label>
              <input
                type="checkbox"
                checked={claudeConfirmed}
                onChange={(event) => setClaudeConfirmed(event.target.checked)}
              />{" "}
              {t("gateway.claudeConfirmConnect")}
            </label>
          </div>
        ) : null}
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
    return t("gateway.updatingClientConfig");
  }
  if (state === "drift") {
    return (
      <button
        type="button"
        className="text-left text-amber-700 underline-offset-2 hover:underline"
        onClick={onRepair}
      >
        {t("gateway.configDriftRepair")}
      </button>
    );
  }
  if (state === "unavailable" || !installed) {
    return t("gateway.installToConnect");
  }
  if (state === "connected") {
    return clientId === "dsh"
      ? t("gateway.injectedProvider", { count: enabledModelCount ?? 0 })
      : t("gateway.connectedViaHub");
  }
  return `${t("gateway.connectionDisconnected")} · ${t("gateway.configUnchanged")}`;
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
    case "grok":
      return grokIcon;
    default:
      return null;
  }
}

function clientIconClass(id: string) {
  if (id === "codex" || id === "dsh") {
    return "h-8 w-8 object-contain";
  }
  if (id === "grok") {
    return "h-6 w-6 object-contain";
  }
  if (id === "pi") {
    return "h-full w-full scale-125 object-cover";
  }
  return "h-5 w-5 object-cover";
}
