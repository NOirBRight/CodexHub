import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { WorkspaceDialog } from "./workspace/WorkspaceDialog";
import { useToasts } from "./PageToast";
import { api, messageFromError } from "../lib/tauri";
import {
  aliasDefaultChanges,
  claudeDefaultTarget,
  claudeDraft,
  claudeDraftChanged,
  claudeDraftValid,
  claudeClearDefault,
  claudePreserveDefault,
  claudeResumeCommand,
  claudeRoles,
  filterClaudeModels,
  rebaseClaudeDraft,
} from "../lib/claudeSettings";
import type { GatewayClientInfo } from "../lib/types";
import type { ExportedGatewayModel } from "./GatewayClientCard";
import claudeIcon from "../assets/claude-code-icon.svg";

export function ClaudeSettingsDialog({
  info,
  models,
  busy,
  connected,
  onClose,
  onToggle,
  onRefresh,
}: {
  info?: GatewayClientInfo;
  models: ExportedGatewayModel[];
  busy?: boolean;
  connected: boolean;
  onClose: () => void;
  onRefresh?: () => Promise<void>;
  onToggle: (
    connect: boolean,
    model?: string | null,
    roles?: Record<string, string> | null,
  ) => void;
}) {
  const { t } = useTranslation();
  const toast = useToasts();
  const fallback = connected ? "" : (models[0]?.id ?? "");
  const saved = useMemo(
    () => claudeDraft(info?.claude_settings, fallback),
    [info?.claude_settings, fallback],
  );
  const [draft, setDraft] = useState(saved);
  const baseline = useRef(saved);
  useEffect(() => {
    const previous = baseline.current;
    setDraft((current) => rebaseClaudeDraft(current, previous, saved));
    baseline.current = saved;
    setPreview(null);
  }, [saved]);
  const [query, setQuery] = useState("");
  const [resumeModelId, setResumeModelId] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [preview, setPreview] = useState<{
    text: string;
    message: string;
    canApply: boolean;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const ids = new Set(models.map((model) => model.id));
  const dirty = claudeDraftChanged(draft, saved);
  const conflicts = info?.claude_settings?.conflicts ?? [];
  const unavailable = !info?.installed || !info?.claude_settings;
  const nativeModels = info?.claude_settings?.native_models ?? [];
  const nativeIds = new Set(nativeModels.map((model) => model.id));
  const invalid = !claudeDraftValid(draft, ids, saved, nativeIds);
  const aliasChanges = aliasDefaultChanges(
    info?.claude_settings?.default_model ?? "",
    saved.roles,
    draft.roles,
  );
  const selectedDefault =
    draft.model === claudePreserveDefault
      ? (info?.claude_settings?.default_model ?? "")
      : draft.model === claudeClearDefault
        ? ""
        : draft.model;
  const defaultTarget = claudeDefaultTarget(
    selectedDefault,
    models,
    draft.roles,
    nativeModels,
  ).map((part) =>
    part === "builtin"
      ? t("gateway.claudeDefaultBuiltin")
      : part === "subscription"
        ? t("gateway.claudeSubscriptionDefault")
        : part,
  );
  const locked = Boolean(busy || previewBusy);
  const resumeCommand = claudeResumeCommand(resumeModelId);
  const needsActivation = !connected || !info?.managed_by_current_app;
  const canApply =
    !locked &&
    !unavailable &&
    !invalid &&
    conflicts.length === 0 &&
    (needsActivation ? confirmed : dirty || info?.route_mode === "stale") &&
    preview?.canApply !== false;
  const close = onClose;
  function update(next: typeof draft) {
    setDraft(next);
    setPreview(null);
    setError(null);
  }
  async function loadPreview() {
    setPreviewBusy(true);
    setError(null);
    try {
      const result = await api.previewGatewayClientConfig(
        "claude",
        draft.model,
        { ...draft.roles, subagent: draft.subagent },
      );
      setPreview({
        text: result.next_redacted,
        message: result.message,
        canApply: result.can_apply,
      });
    } catch (error) {
      setError(messageFromError(error));
    } finally {
      setPreviewBusy(false);
    }
  }
  function picker(
    value: string,
    onChange: (value: string) => void,
    label: string,
    optional = false,
  ) {
    const invalid = Boolean(value) && !ids.has(value);
    return (
      <label className="ws-claude-field">
        <span>{label}</span>
        <select
          value={value}
          disabled={locked}
          aria-invalid={invalid || (!optional && !value)}
          onChange={(event) => onChange(event.target.value)}
        >
          {optional ? (
            <option value="">{t("gateway.claudeRoleUnmapped")}</option>
          ) : !value ? (
            <option value="">{t("gateway.claudeNoModels")}</option>
          ) : null}
          {invalid && (
            <option value={value}>
              {value} — {t("gateway.claudeUnavailableModel")}
            </option>
          )}
          {filterClaudeModels(models, query, value).map((model) => (
            <option key={model.id} value={model.id}>
              {model.label}
            </option>
          ))}
        </select>
        {invalid && (
          <small className="text-danger">
            {t("gateway.claudeRoleInvalid")}
          </small>
        )}
      </label>
    );
  }
  return (
    <WorkspaceDialog
      open
      title={t("gateway.claudeSettingsTitle")}
      onClose={close}
      actions={
        <>
          {connected && (
            <button
              className="ws-button text-danger"
              disabled={locked}
              onClick={() => onToggle(false)}
            >
              {t("workspace.disconnect")}
            </button>
          )}
          <span className="ws-claude-pending" role="status">
            {dirty ? t("gateway.claudeUnsaved") : ""}
          </span>
          <button className="ws-button" onClick={close}>
            {t("common.cancel")}
          </button>
          <button
            className="ws-primary"
            disabled={!canApply}
            onClick={() =>
              onToggle(true, draft.model, {
                ...draft.roles,
                subagent: draft.subagent,
              })
            }
          >
            {busy
              ? t("gateway.connectionUpdating")
              : t(
                  needsActivation ? "workspace.connect" : "gateway.claudeApply",
                )}
          </button>
        </>
      }
    >
      <div className="ws-client-detail-heading">
        <img src={claudeIcon} alt="" />
        <div>
          <b>Claude Code</b>
          <small>
            {t(
              info?.route_mode === "stale"
                ? "gateway.connectionRepair"
                : connected
                  ? "gateway.connected"
                  : "gateway.connectionDisconnected",
            )}{" "}
            · {info?.current_version || t("common.unknown")}
          </small>
        </div>
      </div>
      <p className="ws-claude-note">{t("gateway.claudeRestartRequired")}</p>
      {unavailable && (
        <p role="alert" className="text-danger">
          {t("gateway.claudeReadbackUnavailable")}
        </p>
      )}
      {conflicts.length > 0 && (
        <section className="ws-claude-conflicts" role="alert">
          <h3>{t("gateway.claudeConflicts")}</h3>
          <ul>
            {conflicts.map((conflict) => (
              <li key={conflict}>{conflict}</li>
            ))}
          </ul>
        </section>
      )}
      <section className="ws-claude-section">
        <h3>{t("gateway.claudeModelsTitle")}</h3>
        <label className="ws-claude-field">
          <span>{t("gateway.claudeSearchModels")}</span>
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <label className="ws-claude-field">
          <span>{t("gateway.claudeDefaultModel")}</span>
          <select
            value={draft.model}
            disabled={locked}
            onChange={(event) => update({ ...draft, model: event.target.value })}
          >
            <option value={claudePreserveDefault}>
              {t("gateway.claudeKeepDefault", {
                model:
                  info?.claude_settings?.default_model ||
                  t("gateway.claudeCliDefault"),
              })}
            </option>
            <option value={claudeClearDefault}>
              {t("gateway.claudeUseCliDefault")}
            </option>
            {filterClaudeModels(models, query, draft.model).map((model) => (
              <option key={model.id} value={model.id}>
                {model.label}
              </option>
            ))}
            {filterClaudeModels(nativeModels, query, draft.model)
              .filter((model) => !ids.has(model.id))
              .map((model) => (
                <option key={model.id} value={model.id}>
                  {model.label}
                </option>
              ))}
          </select>
        </label>
        <p className="ws-claude-note" role="status">
          {t("gateway.claudeDefaultTarget", { model: defaultTarget.join(" / ") })}
        </p>
        <p className="ws-claude-note">{t("gateway.claudeDefaultOneMillionBoundary")}</p>
        <details className="ws-claude-catalog">
          <summary>
            {t("gateway.claudeCatalog", { count: models.length })}
          </summary>
          <ul>
            {filterClaudeModels(models, query).map((model) => (
              <li key={model.id}>
                <span>{model.label}</span>
                <code>{model.id}</code>
              </li>
            ))}
          </ul>
          {!filterClaudeModels(models, query).length && (
            <p>{t("gateway.claudeNoModels")}</p>
          )}
        </details>
      </section>
      <section className="ws-claude-section">
        <h3>{t("gateway.claudeRolesTitle")}</h3>
        <p className="ws-claude-note">{t("gateway.claudeRolesHelp")}</p>
        <div className="ws-claude-role-grid">
          {claudeRoles.map((role) => (
            <div key={role}>
              {picker(
                draft.roles[role],
                (value) =>
                  update({
                    ...draft,
                    roles: { ...draft.roles, [role]: value },
                  }),
                t(`gateway.claudeRole${role[0].toUpperCase()}${role.slice(1)}`),
                true,
              )}
            </div>
          ))}
        </div>
        {aliasChanges.length > 0 && (
          <ul className="ws-claude-note" role="status">
            {aliasChanges.map(({ alias, from, to }) => (
              <li key={`${alias}:${from}:${to}`}>
                {t("gateway.claudeAliasDefaultPreview", { alias, from, to })}
              </li>
            ))}
          </ul>
        )}
      </section>
      <section className="ws-claude-section">
        <h3>{t("gateway.claudeSubagentTitle")}</h3>
        <p className="ws-claude-note">{t("gateway.claudeSubagentHelp")}</p>
        {picker(
          draft.subagent,
          (subagent) => update({ ...draft, subagent }),
          t("gateway.claudeRoleSubagent"),
          true,
        )}
      </section>
      {connected && (
        <section className="ws-claude-section">
          <h3>{t("gateway.claudeResumeTitle")}</h3>
          <p className="ws-claude-note">{t("gateway.claudeResumeHelp")}</p>
          <label className="ws-claude-field">
            <span>{t("gateway.claudeResumeModelId")}</span>
            <input
              value={resumeModelId}
              placeholder="claude-opus-5-5"
              spellCheck={false}
              aria-invalid={Boolean(resumeModelId) && !resumeCommand}
              onChange={(event) => setResumeModelId(event.target.value)}
            />
          </label>
          {resumeCommand && <code className="ws-claude-path">{resumeCommand}</code>}
          <button
            className="ws-button"
            disabled={!resumeCommand}
            onClick={() =>
              void navigator.clipboard.writeText(resumeCommand)
                .then(() => toast.showToast(t("common.copied"), "success"))
                .catch((error) => toast.showToast(messageFromError(error), "error"))
            }
          >
            {t("gateway.claudeCopyResumeCommand")}
          </button>
        </section>
      )}
      {needsActivation && (
        <section className="ws-claude-section">
          <p className="ws-claude-note">{t("gateway.claudeConnectScope")}</p>
          <label className="ws-claude-confirm">
            <input
              type="checkbox"
              checked={confirmed}
              disabled={locked}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            <span>{t("gateway.claudeConfirmConnect")}</span>
          </label>
        </section>
      )}
      <details className="ws-claude-section">
        <summary>{t("gateway.claudeDiagnostics")}</summary>
        <p className="ws-claude-note">
          {t("gateway.claudeCompatibilityState")}
        </p>
        <p className="ws-claude-note">{t("gateway.claudeOverrideScope")}</p>
        <code className="ws-claude-path">{info?.config_path}</code>
        {onRefresh && (
          <button
            className="ws-button"
            disabled={locked}
            onClick={async () => {
              setPreviewBusy(true);
              setPreview(null);
              setError(null);
              try {
                await onRefresh();
              } catch (error) {
                setError(messageFromError(error));
              } finally {
                setPreviewBusy(false);
              }
            }}
          >
            {t("gateway.refreshClients")}
          </button>
        )}
        <button
          className="ws-button"
          disabled={locked || invalid || unavailable}
          onClick={() => void loadPreview()}
        >
          {t("workspace.configPreview")}
        </button>
        {preview && (
          <>
            <p
              role="status"
              className={preview.canApply ? "ws-claude-note" : "text-danger"}
            >
              {preview.message}
            </p>
            <pre className="ws-config-preview">{preview.text}</pre>
          </>
        )}
        {error && (
          <p role="alert" className="text-danger">
            {error}
          </p>
        )}
      </details>
    </WorkspaceDialog>
  );
}
