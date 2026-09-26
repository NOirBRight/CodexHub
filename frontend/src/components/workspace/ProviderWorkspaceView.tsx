import { readQuotaCache } from "../../lib/quotaCache";
import { createPortal } from "react-dom";
import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import {
  ArrowRight,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Layers,
  Sparkles,
  Link2,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  Unplug,
  Zap,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { providerLogoSrc } from "../../lib/providerLogos";
import { subscriptionAuthAdapter } from "../../lib/providerCatalog";
import { api, messageFromError } from "../../lib/tauri";
import type {
  Model,
  GatewayUsageSnapshot,
  OpenAIUsageLimit,
  Provider,
} from "../../lib/types";
import {
  displayModelName,
  enabledPreviewModels,
} from "../../lib/modelDisplay";
import {
  listDefaultSubagentOptions,
  resolveSubagentEffort,
  defaultSubagentSummary,
  formatSubagentEffort,
  CODEX_SUBAGENT_EFFORTS,
  type DefaultSubagentOption,
} from "../../lib/defaultSubagent";
import { meanResponseDurationLabel } from "../../lib/workspaceResources";
import { SwitchControl } from "../SettingsDrawer";
import { WorkspaceHeading, type WorkspacePage } from "./WorkspaceShell";
import { ResourceLimits } from "./ResourceLimits";
import brand from "../../assets/brand/codexhub-icon.svg";
import codex from "../../assets/codex-logo.svg";
import { useToasts } from "../PageToast";

type Props = {
  page: WorkspacePage;
  children?: ReactNode;
  providers: Provider[];
  officialCount: number;
  officialModels: Model[];
  officialDisabledModels: string[];
  officialEnabled: number;
  officialIncluded: boolean;
  limits: OpenAIUsageLimit[];
  quotaPending: boolean;
  quotaError: string | null;
  authorized: boolean;
  connected: boolean;
  running: boolean;
  connectionBusy: boolean;
  restartPending: boolean;
  onDismissRestartReminder: () => void;
  busy: boolean;
  ownerLabel?: string;
  onToggleConnection: () => void;
  onSelect: (id: string, tab?: string) => void;
  onAdd: () => void;
  onToggle: (id: string, enabled: boolean) => void;
  onReorder: (ids: string[]) => void;
  onRefresh: () => Promise<unknown>;
  onNavigate: (page: WorkspacePage) => void;
  officialId: string;
  defaultSubagentModel: string;
  defaultSubagentEffort: string;
  onDefaultSubagentChange?: (model: string, effort: string) => void;
};
let dailyCache: { day: string; snapshot: GatewayUsageSnapshot } | null = null;

export function ProviderWorkspaceView(props: Props) {
  const { t } = useTranslation();
  const toast = useToasts();
  const [query, setQuery] = useState("");
  const [xaiLimits, setXaiLimits] = useState<OpenAIUsageLimit[]>(() => readQuotaCache("xai")?.limits ?? []);
  const [xaiError, setXaiError] = useState<string | null>(null);
  const [xaiPending, setXaiPending] = useState(false);
  const [daily, setDaily] = useState<GatewayUsageSnapshot | null>(() => dailyCache?.day === new Date().toDateString() ? dailyCache.snapshot : null);
  const [dailyError, setDailyError] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const [providerQuotas, setProviderQuotas] = useState<
    Record<
      string,
      {
        limits: OpenAIUsageLimit[];
        balance?: number | null;
        error?: string;
        pending?: boolean;
      }
    >
  >(() => Object.fromEntries(props.providers.flatMap((provider) => {
    const cached = readQuotaCache(provider.id);
    return cached ? [[provider.id, cached]] : [];
  })));
  const quotaProviderIds = props.providers
    .filter((p) => ["commandcode", "opencode-go"].includes(p.id))
    .map((p) => p.id)
    .join(",");
  useEffect(() => {
    if (props.page !== "overview") return;
    let active = true;
    let loading = false;
    async function loadQuotas() {
      if (loading) return;
      loading = true;
      await Promise.all(
        quotaProviderIds
          .split(",")
          .filter(Boolean)
          .map(async (id) => {
            if (active)
              setProviderQuotas((previous) => ({
                ...previous,
                [id]: {
                  ...previous[id],
                  limits: previous[id]?.limits ?? [],
                  pending: true,
                },
              }));
            try {
              const result = await api.providerUsage(id);
              if (active)
                setProviderQuotas((previous) => ({
                  ...previous,
                  [id]: result,
                }));
            } catch (error) {
              if (active)
                setProviderQuotas((previous) => ({
                  ...previous,
                  [id]: { ...previous[id], limits: previous[id]?.limits ?? [], pending: false, error: messageFromError(error) },
                }));
            }
          }),
      );
      loading = false;
    }
    void loadQuotas();
    const timer = window.setInterval(() => void loadQuotas(), 180000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [props.page, quotaProviderIds, refreshKey]);
  const hasXai = props.providers.some(
    (p) => subscriptionAuthAdapter(p) === "xai_oauth",
  );
  useEffect(() => {
    if (props.page !== "overview") return;
    let active = true;
    async function load() {
      const start = new Date();
      start.setHours(0, 0, 0, 0);
      try {
        const snapshot = await api.gatewayUsageSnapshot({
          startTs: start.toISOString(),
          endTs: new Date().toISOString(),
        });
        dailyCache = { day: start.toDateString(), snapshot };
        if (active) {
          setDaily(snapshot);
          setDailyError(false);
        }
      } catch {
        if (active) setDailyError(dailyCache?.day !== start.toDateString());
      }
      if (hasXai) {
        if (active) setXaiPending(true);
        try {
          const auth = await api.xaiAuthStatus();
          const snapshot = auth.signed_in ? await api.xaiUsageSnapshot() : null;
          if (active) {
            setXaiLimits(snapshot?.limits ?? []);
            setXaiError(snapshot ? null : t("workspace.signInForQuota"));
          }
        } catch (e) {
          if (active) setXaiError(messageFromError(e));
        } finally {
          if (active) setXaiPending(false);
        }
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 60000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [props.page, hasXai, refreshKey, t]);
  const totalModels = props.providers
    .filter((p) => p.enabled)
    .reduce(
      (n, p) => n + p.models.filter((m) => m.enabled !== false).length,
      props.officialIncluded ? props.officialEnabled : 0,
    );
  const subagentOptions = listDefaultSubagentOptions({
    includeFastVariants: true,
    officialId: props.officialId,
    officialIncluded: props.officialIncluded,
    officialModels: props.officialModels,
    officialDisabledModels: props.officialDisabledModels,
    providers: props.providers,
  });
  const selectedSubagent =
    subagentOptions.find((option) => option.id === props.defaultSubagentModel) ??
    (props.defaultSubagentModel
      ? {
          id: props.defaultSubagentModel,
          label: props.defaultSubagentModel,
          efforts: [],
          defaultEffort: "medium",
        }
      : undefined);
  const subagentEfforts = selectedSubagent
    ? resolveSubagentEffort(selectedSubagent, props.defaultSubagentEffort)
    : "";
  const subagentDisabled =
    props.connectionBusy || !props.onDefaultSubagentChange;
  const bridge = (
    <section className="ws-bridge">
      <img src={codex} alt="Codex" />
      <div>
        <b>Codex</b>
        <small>
          {t(
            props.restartPending ? "workspace.configSaved" : props.connected
              ? "workspace.externalModels"
              : "workspace.officialConfig",
          )}
        </small>
      </div>
      <span className="ws-bridge-line">
        <i />
        <Link2 size={13} />
        <i />
      </span>
      <img src={brand} alt="CodexHub" />
      <div>
        <b>CodexHub</b>
        <small>{t("workspace.modelCount", { count: totalModels })}</small>
      </div>
      <div className="ws-bridge-action">
        <DefaultSubagentPicker
          disabled={subagentDisabled}
          model={props.defaultSubagentModel}
          effort={subagentEfforts}
          options={subagentOptions}
          selected={selectedSubagent}
          onChange={(nextModel, nextEffort) =>
            props.onDefaultSubagentChange?.(nextModel, nextEffort)
          }
        />
        {props.restartPending ? (
          <button
            type="button"
            className="ws-restart-pending"
            title={t("workspace.restartPendingHint")}
            onClick={props.onDismissRestartReminder}
          >
            {t("workspace.restartPending")}
          </button>
        ) : (
          <span
            className={props.connected && props.running ? "ws-online" : "ws-muted"}
          >
            {props.ownerLabel ||
              t(
                props.connected
                  ? props.running
                    ? "workspace.connected"
                    : "workspace.serviceOffline"
                  : "workspace.disconnected",
              )}
          </span>
        )}
        <button
          className="ws-button"
          disabled={props.busy || props.connectionBusy}
          onClick={props.onToggleConnection}
        >
          {props.connectionBusy ? (
            <RefreshCw size={13} className="animate-spin" />
          ) : props.connected ? (
            <Unplug size={13} />
          ) : (
            <Link2 size={13} />
          )}{" "}
          {t(props.connected ? "workspace.disconnect" : "workspace.connect")}
        </button>
      </div>
    </section>
  );
  const all = [
    {
      id: props.officialId,
      name: "OpenAI",
      enabled: props.officialIncluded,
      models: [],
      base_url: "",
      api_key: null,
    },
    ...props.providers,
  ] as Provider[];
  const identity = (p: Provider) => (
    <>
      <span className="ws-logo">
        {providerLogoSrc(p.id === props.officialId ? "openai" : p.id) ? (
          <img
            src={providerLogoSrc(p.id === props.officialId ? "openai" : p.id)!}
            alt=""
          />
        ) : (
          <Layers size={18} />
        )}
      </span>
      <span>
        <b>{p.name}</b>
        <small>
          {t(
            p.id === props.officialId
              ? "workspace.codexSubscription"
              : subscriptionAuthAdapter(p) === "xai_oauth"
                ? "workspace.subscription"
                : "workspace.apiProvider",
          )}
        </small>
      </span>
    </>
  );
  const quota = (p: Provider) =>
    p.id === props.officialId ? (
      <ResourceLimits
        limits={props.authorized ? props.limits : []}
        pending={props.quotaPending}
        message={
          !props.authorized ? t("workspace.signInForQuota") : props.quotaError
        }
      />
    ) : subscriptionAuthAdapter(p) === "xai_oauth" ? (
      <ResourceLimits
        limits={xaiLimits}
        pending={xaiPending}
        message={xaiError}
      />
    ) : ["commandcode", "opencode-go"].includes(p.id) ? (
      <div className="ws-provider-quota">
        <ResourceLimits
          limits={(providerQuotas[p.id]?.limits ?? []).filter(
            (limit) => p.id !== "opencode-go" || limit.key !== "rolling",
          )}
          pending={providerQuotas[p.id]?.pending}
          message={providerQuotas[p.id]?.error}
        />
      </div>
    ) : (
      <span className="ws-muted">{t("workspace.quotaUnsupported")}</span>
    );
  const dailySummary = dailyError ? null : daily?.summary;
  const durations = dailyError
    ? []
    : (daily?.events
        .map((e) => e.duration_ms)
        .filter(
          (v): v is number =>
            typeof v === "number" && Number.isFinite(v) && v >= 0,
        ) ?? []);
  const number = (n: number | null | undefined) =>
    n == null
      ? "—"
      : new Intl.NumberFormat(undefined, {
          notation: "compact",
          maximumFractionDigits: 2,
        }).format(n);
  return (
    <main className={"ws-page ws-" + props.page}>
      {props.page !== "overview" &&
        props.page !== "statistics" &&
        props.page !== "gateway" && (
          <WorkspaceHeading
            page={props.page}
            actions={
              props.page === "providers" ? (
                <button
                  className="ws-primary"
                  disabled={props.busy}
                  onClick={props.onAdd}
                >
                  <Plus size={13} />
                  {t("providers.addProvider")}
                </button>
              ) : undefined
            }
          />
        )}
      {(props.page === "overview" || props.page === "clients") && bridge}
      {props.page === "overview" && (
        <>
          <div className="ws-metrics">
            {[
              [t("workspace.todayRequests"), number(dailySummary?.requests)],
              [t("workspace.tokens"), number(dailySummary?.total_tokens)],
              [
                t("workspace.successRate"),
                dailySummary?.requests
                  ? (
                      (dailySummary.successful_requests /
                        dailySummary.requests) *
                      100
                    ).toFixed(1) + "%"
                  : "—",
              ],
              [
                t("workspace.responseDuration"),
                meanResponseDurationLabel(durations),
              ],
            ].map(([label, value]) => (
              <div key={label}>
                <span>{label}</span>
                <strong>{value}</strong>
              </div>
            ))}
          </div>
          <section className="ws-resource-table">
            <header>
              <b>{t("workspace.resources")}</b>
              <span className="ws-muted">
                {t("workspace.providerCount", {
                  count: all.filter(
                    (p) => p.id !== props.officialId || props.officialIncluded,
                  ).length,
                })}
              </span>
              <button
                className="ws-icon"
                aria-label={t("workspace.refreshQuota")}
                onClick={() => {
                  setRefreshKey((v) => v + 1);
                  void props
                    .onRefresh()
                    .catch((e) =>
                      toast.showToast(messageFromError(e), "error"),
                    );
                }}
              >
                <RefreshCw size={13} />
              </button>
              <button
                className="ws-button"
                disabled={props.busy}
                onClick={props.onAdd}
              >
                <Plus size={12} />
                {t("common.add")}
              </button>
            </header>
            <div className="ws-resource-labels">
              <span>Provider</span>
              <span>{t("workspace.resourceDetails")}</span>
              <span>{t("common.enabled")}</span>
            </div>
            <div className="ws-scroll">
              {all
                .filter(
                  (p) => p.id !== props.officialId || props.officialIncluded,
                )
                .map((p) => (
                  <div
                    className={"ws-resource-row " + (!p.enabled ? "muted" : "")}
                    key={p.id}
                  >
                    <button
                      className="ws-identity"
                      onClick={() => props.onSelect(p.id)}
                    >
                      {identity(p)}
                    </button>
                    <button
                      className="ws-resource-action"
                      aria-label={t("workspace.providerResources", {
                        name: p.name,
                      })}
                      onClick={() => props.onSelect(p.id, "account")}
                    >
                      {quota(p)}
                    </button>
                    <SwitchControl
                      checked={p.enabled}
                      disabled={props.busy}
                      ariaLabel={t("workspace.enableProvider", {
                        name: p.name,
                      })}
                      onChange={(enabled) => props.onToggle(p.id, enabled)}
                    />
                  </div>
                ))}
            </div>
          </section>
        </>
      )}
      {props.page === "providers" && (
        <>
          <div className="ws-provider-tools">
            <label>
              <Search size={13} />
              <input
                aria-label={t("workspace.searchProviders")}
                placeholder={t("workspace.searchProviders")}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
            <span>{t("workspace.modelCount", { count: totalModels })}</span>
          </div>
          <section className="ws-management ws-scroll">
            {all
              .filter((p) => p.name.toLowerCase().includes(query.toLowerCase()))
              .map((p) => {
                const official = p.id === props.officialId;
                return (
                  <article key={p.id} className="ws-management-row">
                    <button
                      className="ws-identity"
                      onClick={() => props.onSelect(p.id)}
                    >
                      {identity(p)}
                    </button>
                    <div className="ws-endpoint">
                      <span>
                        {t(
                          official
                            ? "workspace.codexAuth"
                            : subscriptionAuthAdapter(p) === "xai_oauth"
                              ? "workspace.subscriptionAccount"
                              : p.api_key
                                ? "workspace.keyEntered"
                                : "workspace.credentialsNeeded",
                        )}
                      </span>
                      <code title={p.base_url}>
                        {official ? t("workspace.officialService") : p.base_url}
                      </code>
                    </div>
                    <div className="ws-actions">
                      <button
                        className="ws-button"
                        onClick={() =>
                          props.onSelect(
                            p.id,
                            official ? "account" : "connection",
                          )
                        }
                      >
                        <Settings2 size={12} />
                        {t(
                          official
                            ? "workspace.account"
                            : "workspace.configure",
                        )}
                      </button>
                      <SwitchControl
                        ariaLabel={t("workspace.enableProvider", {
                          name: p.name,
                        })}
                        checked={p.enabled}
                        disabled={props.busy}
                        onChange={(enabled) => props.onToggle(p.id, enabled)}
                      />
                    </div>
                    <div className="ws-model-preview">
                      <button onClick={() => props.onSelect(p.id)}>
                        <Layers size={12} />
                        {official
                          ? props.officialEnabled
                          : p.models.filter((m) => m.enabled !== false)
                              .length}{" "}
                        / {official ? props.officialCount : p.models.length}{" "}
                        {t("workspace.modelsEnabled")}
                        <ArrowRight size={12} />
                      </button>
                      <span className="ws-model-names">
                      {enabledPreviewModels(
                        official ? props.officialModels : p.models,
                        official ? props.officialDisabledModels : undefined,
                      ).map((m) => (
                        <code key={m.id} title={m.id}>{displayModelName(m, p)}</code>
                      ))}
                      </span>
                      {!official && (
                        <span className="ws-order">
                          {[-1, 1].map((direction) => (
                            <button
                              key={direction}
                              disabled={
                                props.busy ||
                                props.providers.indexOf(p) + direction < 0 ||
                                props.providers.indexOf(p) + direction >=
                                  props.providers.length
                              }
                              aria-label={t(
                                direction < 0
                                  ? "workspace.moveUp"
                                  : "workspace.moveDown",
                                { name: p.name },
                              )}
                              onClick={() => {
                                const ids = props.providers.map((v) => v.id),
                                  i = ids.indexOf(p.id);
                                [ids[i], ids[i + direction]] = [
                                  ids[i + direction],
                                  ids[i],
                                ];
                                props.onReorder(ids);
                              }}
                            >
                              {direction < 0 ? "↑" : "↓"}
                            </button>
                          ))}
                        </span>
                      )}
                    </div>
                  </article>
                );
              })}
            {!all.some((p) =>
              p.name.toLowerCase().includes(query.toLowerCase()),
            ) && <p className="ws-empty">{t("workspace.noProviders")}</p>}
          </section>
        </>
      )}
      {props.children}
    </main>
  );
}

export function DefaultSubagentPicker({
  disabled,
  model,
  effort,
  options,
  selected,
  emptyLabel,
  onChange,
}: {
  disabled: boolean;
  model: string;
  effort: string;
  options: DefaultSubagentOption[];
  selected?: DefaultSubagentOption;
  emptyLabel?: string;
  onChange: (model: string, effort: string) => void;
}) {
  const { t } = useTranslation();
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [panel, setPanel] = useState<"menu" | "model" | "effort">("menu");
  const [draftModel, setDraftModel] = useState(model);
  const [draftEffort, setDraftEffort] = useState(effort);
  const [menuBox, setMenuBox] = useState<CSSProperties>({});
  const draftRef = useRef({ model: draftModel, effort: draftEffort });
  const propsRef = useRef({ model, effort, onChange });
  const openRef = useRef(open);
  draftRef.current = { model: draftModel, effort: draftEffort };
  propsRef.current = { model, effort, onChange };
  openRef.current = open;
  const fallback = emptyLabel ?? t("workspace.defaultSubagentCodexDefault");
  const activeSelected =
    options.find((option) => option.id === draftModel) ??
    (draftModel && selected?.id === draftModel ? selected : undefined);
  const summary = defaultSubagentSummary(
    draftModel ? activeSelected?.label || draftModel : "",
    draftEffort,
    fallback,
  );
  const effortChoices = activeSelected?.efforts.length
    ? activeSelected.efforts
    : activeSelected
      ? [...CODEX_SUBAGENT_EFFORTS]
      : [];
  const modelChoices =
    activeSelected && !options.some((option) => option.id === activeSelected.id)
      ? [activeSelected, ...options]
      : options;

  useEffect(() => {
    setDraftModel(model);
    setDraftEffort(effort);
  }, [model, effort]);

  function commitIfChanged(nextModel: string, nextEffort: string) {
    const current = propsRef.current;
    if (nextModel !== current.model || nextEffort !== current.effort) {
      current.onChange(nextModel, nextEffort);
    }
  }

  function closeMenu() {
    if (!openRef.current) return;
    const draft = draftRef.current;
    commitIfChanged(draft.model, draft.effort);
    setOpen(false);
    setPanel("menu");
  }

  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const rect = triggerRef.current?.getBoundingClientRect();
      if (!rect) return;
      const width = Math.min(
        Math.max(rect.width, 220),
        Math.min(320, window.innerWidth - 16),
      );
      let left = rect.left;
      if (left + width > window.innerWidth - 8) {
        left = Math.max(8, window.innerWidth - 8 - width);
      }
      if (left < 8) left = 8;
      const openUp =
        window.innerHeight - rect.bottom < 132 && rect.top > window.innerHeight - rect.bottom;
      setMenuBox(
        openUp
          ? { left, width, bottom: window.innerHeight - rect.top + 6 }
          : { left, width, top: rect.bottom + 6 },
      );
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, panel]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (
        rootRef.current?.contains(target) ||
        menuRef.current?.contains(target)
      ) {
        return;
      }
      closeMenu();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        closeMenu();
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  function chooseModel(nextModel: string) {
    if (activeSelected?.fast) {
      nextModel = options.find((item) => item.id === nextModel)?.speedVariant ?? nextModel;
    }
    const option =
      options.find((item) => item.id === nextModel) ??
      (nextModel && selected?.id === nextModel ? selected : undefined);
    const nextEffort = nextModel ? resolveSubagentEffort(option, draftEffort) : "";
    setDraftModel(nextModel);
    setDraftEffort(nextEffort);
    setPanel("menu");
    commitIfChanged(nextModel, nextEffort);
  }

  function chooseEffort(nextEffort: string) {
    setDraftEffort(nextEffort);
    setPanel("menu");
    commitIfChanged(draftModel, nextEffort);
  }

  const modelLabel = draftModel ? activeSelected?.label || draftModel : fallback;

  return (
    <div
      ref={rootRef}
      className="ws-bridge-subagent"
      onKeyDown={(event) => {
        if (event.key === "Escape" && open) {
          event.stopPropagation();
          closeMenu();
        }
      }}
    >
      <button
        ref={triggerRef}
        type="button"
        className="ws-bridge-subagent-trigger"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={t("workspace.defaultSubagent")}
        title={summary}
        disabled={disabled}
        onClick={() => {
          if (open) {
            closeMenu();
            return;
          }
          setDraftModel(model);
          setDraftEffort(effort);
          setPanel("menu");
          setOpen(true);
        }}
      >
        <Sparkles size={11} className="ws-bridge-subagent-mark" />
        <span className="ws-bridge-subagent-kicker">
          {t("workspace.defaultSubagentLabel")}
        </span>
        <span className="ws-bridge-subagent-value">{summary}</span>
        <ChevronDown
          size={11}
          className={
            open
              ? "ws-bridge-subagent-chevron open"
              : "ws-bridge-subagent-chevron"
          }
        />
      </button>
      {open &&
        (menuBox.top != null || menuBox.bottom != null) &&
        createPortal(
          <div
            ref={menuRef}
            className="select-popover ws-bridge-subagent-menu"
            role="dialog"
            aria-label={t("workspace.defaultSubagent")}
            style={menuBox}
          >
          {panel === "menu" ? (
            <>
              <div className="ws-bridge-subagent-heading">
                {t("workspace.defaultSubagent")}
              </div>
              <button
                type="button"
                className="ws-bridge-subagent-row"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => setPanel("model")}
              >
                <span>{t("workspace.defaultSubagentModel")}</span>
                <span>
                  <span className="ws-bridge-subagent-row-value" title={modelLabel}>
                    {modelLabel}
                  </span>
                  <ChevronRight size={11} />
                </span>
              </button>
              <button
                type="button"
                className="ws-bridge-subagent-row"
                disabled={!draftModel}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => setPanel("effort")}
              >
                <span>{t("workspace.defaultSubagentEffort")}</span>
                <span>
                  {draftModel ? formatSubagentEffort(draftEffort) || "—" : "—"}
                  <ChevronRight size={11} />
                </span>
              </button>
              {options.some((option) => option.speedVariant) && <button
                type="button"
                className="ws-bridge-subagent-row"
                role="switch"
                aria-checked={Boolean(activeSelected?.fast)}
                disabled={!activeSelected?.speedVariant}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  const nextModel = activeSelected?.speedVariant;
                  if (!nextModel) return;
                  setDraftModel(nextModel);
                  commitIfChanged(nextModel, draftEffort);
                }}
              >
                <span>{t("workspace.defaultSubagentFast")}</span>
                <span>
                  {t(activeSelected?.fast
                    ? "workspace.defaultSubagentFastOn"
                    : "workspace.defaultSubagentFastOff")}
                  <Zap size={11} aria-hidden="true" />
                </span>
              </button>}
            </>
          ) : (
            <>
              <button
                type="button"
                className="ws-bridge-subagent-back"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => setPanel("menu")}
              >
                <ChevronLeft size={11} />
                {t(
                  panel === "model"
                    ? "workspace.defaultSubagentModel"
                    : "workspace.defaultSubagentEffort",
                )}
              </button>
              <div className="ws-bridge-subagent-options" role="listbox">
                {panel === "model" ? (
                  <>
                    <button
                      type="button"
                      className="select-option"
                      role="option"
                      aria-selected={!draftModel}
                      title={fallback}
                      onMouseDown={(event) => event.preventDefault()}
                      onClick={() => chooseModel("")}
                    >
                      {fallback}
                    </button>
                    {modelChoices.filter((option) => !option.fast).map((option) => (
                      <button
                        key={option.id}
                        type="button"
                        className="select-option"
                        role="option"
                        aria-selected={option.id === draftModel || (activeSelected?.fast && option.id === activeSelected.speedVariant)}
                        title={option.label}
                        onMouseDown={(event) => event.preventDefault()}
                        onClick={() => chooseModel(option.id)}
                      >
                        {option.label}
                      </button>
                    ))}
                  </>
                ) : (
                  effortChoices.map((item) => (
                    <button
                      key={item}
                      type="button"
                      className="select-option"
                      role="option"
                      aria-selected={item === draftEffort}
                      onMouseDown={(event) => event.preventDefault()}
                      onClick={() => chooseEffort(item)}
                    >
                      {formatSubagentEffort(item)}
                    </button>
                  ))
                )}
              </div>
            </>
          )}
          </div>,
          document.body,
        )}
    </div>
  );
}
