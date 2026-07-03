import {
  Brain,
  Check,
  Copy,
  Eye,
  EyeOff,
  FlaskConical,
  Link2,
  Link2Off,
  Plus,
  RefreshCcw,
  Save,
  SlidersHorizontal,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { SortableList } from "../components/SortableList";
import { cx, displayModel, mergeDiscoveredModels, renumberModels, slugify } from "../lib/format";
import { api, messageFromError } from "../lib/tauri";
import type {
  AppStatus,
  GatewayStatus,
  Model,
  Provider,
  Settings,
  UpstreamFormat,
  UpstreamFormatProbeResult,
} from "../lib/types";

const OFFICIAL_ID = "__official__";
const ADD_ID = "__add__";
const DEFAULT_FAST_MODEL_VARIANTS = ["openai/gpt-5.5", "openai/gpt-5.4"];
const DEFAULT_OFFICIAL_MODEL_ORDER = [
  "openai/gpt-5.5",
  "openai/gpt-5.4",
  "openai/gpt-5.4-mini",
  "openai/gpt-5.3-codex-spark",
];

const emptyProvider = {
  id: "",
  name: "",
  base_url: "",
  api_key: "",
  upstream_format: "auto" as UpstreamFormat,
  display_prefix: "",
  models: [] as Model[],
};

const reasoningLevelOptions = ["low", "medium", "high", "xhigh", "max"];

type ProviderNavItem =
  { id: string; sort_order: number; provider: Provider };
type CodexAuthState = "authorized" | "missing" | "unknown";
type ToastTone = "info" | "error" | "loading";
type ToastState = { tone: ToastTone; text: string };

export function ProvidersPage({ gatewayStatus: gatewayStatusSnapshot }: { gatewayStatus?: GatewayStatus | null }) {
  const [providers, setProviders] = useState<Provider[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [settingsDraft, setSettingsDraft] = useState<Settings | null>(null);
  const [codexStatus, setCodexStatus] = useState<AppStatus | null>(null);
  const [loadedGatewayStatus, setLoadedGatewayStatus] = useState<GatewayStatus | null>(null);
  const [codexAuthState, setCodexAuthState] = useState<CodexAuthState>("unknown");
  const [officialModels, setOfficialModels] = useState<Model[]>([]);
  const [selectedId, setSelectedId] = useState<string>(OFFICIAL_ID);
  const [form, setForm] = useState(emptyProvider);
  const [probeResult, setProbeResult] = useState<UpstreamFormatProbeResult | null>(null);
  const [busy, setBusy] = useState<string | null>("load");
  const [toast, setToastState] = useState<ToastState | null>(null);
  const [modelDiscoveryError, setModelDiscoveryError] = useState<string | null>(null);

  useEffect(() => {
    void load();
  }, []);

  useEffect(() => {
    if (gatewayStatusSnapshot !== undefined) {
      setCodexAuthState(codexAuthStateFromGatewayStatus(gatewayStatusSnapshot ?? null));
    }
  }, [gatewayStatusSnapshot]);

  useEffect(() => {
    setProbeResult(null);
    setModelDiscoveryError(null);
  }, [selectedId]);

  const selectedProvider = useMemo(
    () => providers.find((provider) => provider.id === selectedId) ?? null,
    [providers, selectedId],
  );
  const providerModelCount = useMemo(
    () =>
      providers.reduce(
        (total, provider) => total + provider.models.length,
        0,
      ),
    [providers],
  );
  const enabledProviderModels = useMemo(
    () =>
      providers.reduce(
        (total, provider) =>
          total +
          provider.models.filter(
            (model) =>
              provider.enabled &&
              model.enabled &&
              model.gateway_exported,
          ).length,
        0,
      ),
    [providers],
  );
  const providerNavItems = useMemo<ProviderNavItem[]>(() => {
    return providers
      .map((provider) => ({
        id: provider.id,
        sort_order: provider.sort_order ?? 0,
        provider,
      }))
      .sort((left, right) => {
        if (left.sort_order !== right.sort_order) {
          return left.sort_order - right.sort_order;
        }
        return left.id.localeCompare(right.id);
      });
  }, [providers]);
  const canAdd = form.name.trim() && form.base_url.trim();
  const gatewayStatus = gatewayStatusSnapshot ?? loadedGatewayStatus;
  const gatewayContextById = useMemo(() => {
    return new Map((gatewayStatus?.official_models ?? []).map((model) => [model.id, model.context_window]));
  }, [gatewayStatus]);
  const error = toast?.tone === "error" ? toast.text : null;
  const message = toast && toast.tone !== "error" ? toast.text : null;

  useEffect(() => {
    if (!toast || toast.tone === "loading") {
      return;
    }
    const timer = window.setTimeout(() => dismissToast(), 8000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  function showToast(text: string, tone: ToastTone = "info") {
    setToastState({ text, tone });
  }

  function dismissToast() {
    setToastState(null);
  }

  function setMessage(value: string | null) {
    if (value) {
      showToast(value, "info");
      return;
    }
    setToastState((current) => (current?.tone === "info" ? null : current));
  }

  function setError(value: string | null) {
    if (value) {
      showToast(value, "error");
      return;
    }
    setToastState((current) => (current?.tone === "error" ? null : current));
  }

  async function load() {
    setBusy("load");
    try {
      const [nextSettings, nextProviders, catalog, modelMetadata, nextCodexStatus, gatewayStatus] = await Promise.all([
        api.getSettings(),
        api.getProviders(),
        api.listModels(),
        api.listModelMetadata().catch(() => []),
        api.getStatus().catch(() => null),
        api.gatewayStatus().catch(() => null),
      ]);
      const normalizedSettings = withDefaultFastVariants(nextSettings);
      setSettings(normalizedSettings);
      setSettingsDraft(normalizedSettings);
      setCodexStatus(nextCodexStatus);
      setLoadedGatewayStatus(gatewayStatus);
      setCodexAuthState(codexAuthStateFromGatewayStatus(gatewayStatus));
      setProviders(nextProviders);
      setOfficialModels(
        sortOfficialModels(
          mergeOfficialModelSources(catalog, modelMetadata),
          normalizedSettings.official_model_sort_order,
        ),
      );
      if (selectedId !== OFFICIAL_ID && selectedId !== ADD_ID && !nextProviders.some((provider) => provider.id === selectedId)) {
        setSelectedId(nextProviders[0]?.id ?? OFFICIAL_ID);
      }
      setError(null);
      setModelDiscoveryError(null);
    } catch (err) {
      setError(messageFromError(err));
    } finally {
      setBusy(null);
    }
  }

  async function saveProviders(next: Provider[], regenerateCatalog = true, successMessage?: string) {
    setBusy("save");
    try {
      const saved = await api.saveProviders(next);
      setProviders(saved);
      if (regenerateCatalog) {
        await api.generateCatalog();
      }
      setMessage(successMessage ?? null);
      setError(null);
      return saved;
    } catch (err) {
      setError(messageFromError(err));
      throw err;
    } finally {
      setBusy(null);
    }
  }

  async function saveSettings(next: Settings, regenerateCatalog = false, successMessage?: string) {
    setBusy("settings");
    try {
      const saved = await api.saveSettings(next);
      setSettings(saved);
      setSettingsDraft(saved);
      if (regenerateCatalog) {
        await api.generateCatalog();
      }
      setMessage(successMessage ?? null);
      setError(null);
    } catch (err) {
      setError(messageFromError(err));
    } finally {
      setBusy(null);
    }
  }

  async function toggleAutostart(enabled: boolean) {
    if (!settingsDraft) {
      return;
    }
    setBusy("autostart");
    try {
      if (enabled) {
        await api.setAutostart(true);
      } else {
        await api.removeAutostart();
      }
      await saveSettings({ ...settingsDraft, auto_start_proxy: enabled });
    } catch (err) {
      setError(messageFromError(err));
      setBusy(null);
    }
  }

  async function syncNow() {
    setBusy("sync");
    try {
      setMessage(await api.syncHistory());
      setError(null);
    } catch (err) {
      setError(messageFromError(err));
    } finally {
      setBusy(null);
    }
  }

  async function updateProvider(next: Provider, successMessage?: string) {
    await saveProviders(
      providers.map((provider) => (provider.id === next.id ? next : provider)),
      true,
      successMessage,
    );
  }

  function toggleProviderEnabled(providerId: string, enabled: boolean) {
    const nextProviders = providers.map((provider) =>
      provider.id === providerId ? { ...provider, enabled } : provider,
    );
    setProviders(nextProviders);
    void saveProviders(nextProviders);
  }

  async function reorderHubProviders(items: ProviderNavItem[]) {
    const nextProviders = providers.map((provider) => provider);

    items.forEach((item, index) => {
      const sortOrder = index + 1;
      const providerIndex = nextProviders.findIndex((provider) => provider.id === item.id);
      if (providerIndex >= 0) {
        nextProviders[providerIndex] = { ...nextProviders[providerIndex], sort_order: sortOrder };
      }
    });

    setProviders(nextProviders);
    await saveProviders(nextProviders);
  }

  function toggleOfficialInclude(value: boolean) {
    if (!settingsDraft) {
      return;
    }
    void saveSettings({ ...settingsDraft, include_official_models: value }, true);
  }

  function toggleOfficialModel(modelId: string, enabled: boolean) {
    if (!settingsDraft) {
      return;
    }
    const current = settingsDraft.official_disabled_models ?? [];
    const nextDisabled = enabled
      ? current.filter((item) => !modelIdMatches(item, modelId))
      : [...new Set([...current, modelId])];
    const nextSettings = { ...settingsDraft, official_disabled_models: nextDisabled };
    setSettings(nextSettings);
    setSettingsDraft(nextSettings);
    setOfficialModels((currentModels) =>
      currentModels.map((model) => (modelIdMatches(model.id, modelId) ? { ...model, enabled } : model)),
    );
    void saveSettings(nextSettings, true);
  }

  async function toggleCodexHubConnection() {
    if (!settingsDraft) {
      return;
    }
    const nextMode = codexStatus?.mode === "custom" ? "official" : "custom";
    setBusy("route");
    try {
      const status = await api.switchMode(nextMode, settingsDraft.auto_sync_history);
      setCodexStatus(status);
      setMessage(status.message);
      setError(null);
    } catch (err) {
      setError(messageFromError(err));
    } finally {
      setBusy(null);
    }
  }

  async function reorderOfficialModels(models: Model[]) {
    const nextModels = renumberModels(models);
    setOfficialModels(nextModels);
    if (!settingsDraft) {
      return;
    }
    await saveSettings(
      {
        ...settingsDraft,
        official_model_sort_order: nextModels.map((model) => model.id),
      },
      true,
    );
  }

  async function refreshProviderModels(provider: Provider) {
    setBusy(provider.id);
    showToast(`Discovering ${provider.name} models...`, "loading");
    try {
      const models = await api.discoverProviderModels(provider.base_url, provider.api_key ?? "");
      const previousModelIds = new Set(provider.models.map((model) => model.id));
      const nextProvider = {
        ...provider,
        models: mergeDiscoveredModels(provider.models, models),
      };
      const nextProviders = providers.map((item) =>
        item.id === provider.id ? nextProvider : item,
      );
      setProviders(nextProviders);
      await saveProviders(nextProviders);
      const addedCount = nextProvider.models.filter((model) => !previousModelIds.has(model.id)).length;
      showToast(
        `${provider.name}: discovered ${models.length} model${models.length === 1 ? "" : "s"}, ${addedCount} new`,
        "info",
      );
      setModelDiscoveryError(null);
    } catch (err) {
      const discoveryError = shortProviderDiscoveryError(err);
      setModelDiscoveryError(discoveryError);
      showToast(discoveryError, "error");
    } finally {
      setBusy(null);
    }
  }

  async function refreshOfficialModels() {
    setBusy("official-refresh");
    try {
      const refreshed = filterCodexVisibleOfficialModels(await api.refreshOfficialModels());
      setOfficialModels(sortOfficialModels(refreshed, settingsDraft?.official_model_sort_order ?? []));
      await api.generateCatalog();
      setMessage("Official models refreshed");
      setError(null);
    } catch (err) {
      setError(messageFromError(err));
    } finally {
      setBusy(null);
    }
  }

  async function deleteProvider(providerId: string) {
    const target = providers.find((provider) => provider.id === providerId);
    if (!target) {
      setError(`Provider not found: ${providerId}`);
      return;
    }
    if (!window.confirm(`Delete provider ${target.name}?`)) {
      return;
    }
    const previousProviders = providers;
    const previousSelectedId = selectedId;
    const next = providers.filter((provider) => provider.id !== providerId);
    setSelectedId(next[0]?.id ?? OFFICIAL_ID);
    setProviders(next);
    try {
      const saved = await saveProviders(next, true, `${target.name} deleted`);
      if (saved.some((provider) => provider.id === providerId)) {
        setProviders(saved);
        setSelectedId(providerId);
        setError(`Provider delete did not persist: ${target.name}`);
        return;
      }
    } catch {
      setProviders(previousProviders);
      setSelectedId(previousSelectedId);
      return;
    }
    setProbeResult(null);
    setModelDiscoveryError(null);
    setError(null);
  }

  async function discoverForForm() {
    setBusy("discover");
    showToast("Discovering models...", "loading");
    try {
      const models = await api.discoverProviderModels(form.base_url, form.api_key);
      setForm((current) => ({
        ...current,
        models: mergeDiscoveredModels(current.models, models),
      }));
      showToast(`Discovered ${models.length} model${models.length === 1 ? "" : "s"}`, "info");
      setModelDiscoveryError(null);
    } catch (err) {
      const discoveryError = shortProviderDiscoveryError(err);
      setModelDiscoveryError(discoveryError);
      showToast(discoveryError, "error");
    } finally {
      setBusy(null);
    }
  }

  async function probeUpstreamFormat(baseUrl: string, apiKey: string, model?: string | null) {
    setBusy("probe");
    setProbeResult(null);
    try {
      const result = await api.probeUpstreamFormat(baseUrl, apiKey, model);
      setProbeResult(result);
      setMessage(`Probe completed: ${upstreamFormatLabel(result.recommended_format)}`);
      setError(null);
      return result;
    } catch (err) {
      setError(messageFromError(err));
      return null;
    } finally {
      setBusy(null);
    }
  }

  function providerProbeModel(provider: Provider) {
    const model = provider.models.find((item) => item.enabled) ?? provider.models[0];
    return model?.upstream_model?.trim() || model?.id || null;
  }

  function formProbeModel() {
    const model = form.models.find((item) => item.enabled) ?? form.models[0];
    return model?.upstream_model?.trim() || model?.id || null;
  }

  async function addProvider() {
    const id = form.id.trim() || slugify(form.name);
    if (!id) {
      setError("Provider name is required");
      return;
    }
    if (providers.some((provider) => provider.id === id)) {
      setError(`Provider already exists: ${form.name.trim()}`);
      return;
    }

    const models = renumberModels(form.models.map((model) => normalizeModel(model)));
    const nextSortOrder =
      Math.max(
        0,
        ...providers.map((provider) => provider.sort_order ?? 0),
      ) + 1;
    const providerName = form.name.trim();
    await saveProviders(
      [
        ...providers,
        {
          id,
          name: providerName,
          base_url: form.base_url.trim(),
          api_key: form.api_key.trim() || null,
          upstream_format: form.upstream_format,
          display_prefix: form.display_prefix.trim() || null,
          sort_order: nextSortOrder,
          enabled: true,
          models,
        },
      ],
      true,
      `${providerName} added`,
    );
    setSelectedId(id);
    setForm(emptyProvider);
  }

  return (
    <main className="relative grid h-full min-h-0 min-w-[980px] grid-cols-[minmax(0,4fr)_minmax(0,6fr)] gap-4">
      <aside className="min-h-0 min-w-0 overflow-hidden rounded-md border border-line bg-white shadow-subtle">
        <ProviderSourceSidebar
          codexAuthState={codexAuthState}
          codexConnected={codexStatus?.mode === "custom"}
          gatewayStatus={gatewayStatus}
          busy={busy}
          enabledProviderModels={enabledProviderModels}
          officialCount={officialModels.length}
          providerModelCount={providerModelCount}
          onAdd={() => setSelectedId(ADD_ID)}
          items={providerNavItems}
          onReorder={(items) => void reorderHubProviders(items)}
          onSelect={setSelectedId}
          onToggleProvider={toggleProviderEnabled}
          onToggleConnection={() => void toggleCodexHubConnection()}
          selectedId={selectedId}
        />
      </aside>

      <section className="min-h-0 min-w-0 overflow-hidden rounded-md border border-line bg-white shadow-subtle">
        <div className="grid h-full min-h-0 grid-rows-[minmax(0,1fr)_auto]">
          <div className="min-h-0 overflow-hidden">
            {selectedId === ADD_ID ? (
              <AddProviderPanel
                busy={busy}
                canAdd={Boolean(canAdd)}
                discoverError={modelDiscoveryError}
                form={form}
                probeResult={probeResult}
                onAdd={() => void addProvider()}
                onDiscover={() => void discoverForForm()}
                onFormChange={setForm}
                onProbe={() =>
                  void probeUpstreamFormat(form.base_url, form.api_key, formProbeModel()).then((result) => {
                    if (result && result.recommended_format !== "auto") {
                      setForm((current) => ({
                        ...current,
                        upstream_format: result.recommended_format,
                      }));
                    }
                  })
                }
              />
            ) : selectedId === OFFICIAL_ID ? (
              <OfficialDetail
                authState={codexAuthState}
                busy={busy}
                included={settings?.include_official_models ?? false}
                gatewayContextById={gatewayContextById}
                models={officialModels}
                officialDisabledModels={settings?.official_disabled_models ?? []}
                onRefresh={() => void refreshOfficialModels()}
                onReorder={(models) => void reorderOfficialModels(models)}
                onToggleInclude={toggleOfficialInclude}
                onToggleModel={toggleOfficialModel}
              />
            ) : selectedProvider ? (
              <ProviderDetail
                busy={busy}
                discoverError={modelDiscoveryError}
                probeResult={probeResult}
                provider={selectedProvider}
                onChange={(provider) => void updateProvider(provider)}
                onDelete={() => void deleteProvider(selectedProvider.id)}
                onProbe={(provider) =>
                  probeUpstreamFormat(provider.base_url, provider.api_key ?? "", providerProbeModel(provider))
                }
                onRefresh={(provider) => void refreshProviderModels(provider)}
              />
            ) : (
              <div className="p-6 text-sm text-slate-500">Select a provider</div>
            )}
          </div>

        </div>
      </section>
      {toast && (
        <div
          className={cx(
            "absolute bottom-3 left-3 z-50 grid max-w-[420px] grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2 rounded-md border px-3 py-2 text-sm shadow-lg",
            toast.tone === "error"
              ? "border-red-200 bg-red-50 text-danger"
              : "border-line bg-white text-slate-700",
          )}
        >
          {toast.tone === "loading" ? (
            <RefreshCcw size={14} className="animate-spin text-action" />
          ) : (
            <span className="h-2 w-2 rounded-full bg-action" />
          )}
          <span className="min-w-0 truncate">{toast.text}</span>
          <button
            type="button"
            className="focus-ring grid h-6 w-6 place-items-center rounded text-slate-500 hover:bg-slate-100 hover:text-ink"
            aria-label="Dismiss notification"
            onClick={dismissToast}
          >
            <X size={14} />
          </button>
        </div>
      )}
    </main>
  );
}

function ProviderSourceSidebar({
  busy,
  codexAuthState,
  codexConnected,
  enabledProviderModels,
  gatewayStatus,
  items,
  officialCount,
  providerModelCount,
  onAdd,
  onReorder,
  onSelect,
  onToggleProvider,
  onToggleConnection,
  selectedId,
}: {
  busy: string | null;
  codexAuthState: CodexAuthState;
  codexConnected: boolean;
  enabledProviderModels: number;
  gatewayStatus: GatewayStatus | null;
  items: ProviderNavItem[];
  officialCount: number;
  providerModelCount: number;
  onAdd: () => void;
  onReorder: (items: ProviderNavItem[]) => void;
  onSelect: (id: string) => void;
  onToggleProvider: (providerId: string, enabled: boolean) => void;
  onToggleConnection: () => void;
  selectedId: string;
}) {
  return (
    <div className="grid h-full min-h-0 grid-rows-[auto_auto_minmax(0,1fr)] gap-3 p-3">
      <OfficialOpenAICard
        authState={codexAuthState}
        active={selectedId === OFFICIAL_ID}
        modelCount={officialCount}
        onSelect={() => onSelect(OFFICIAL_ID)}
      />
      <HubConnectionBridge
        connected={codexConnected}
        disabled={busy === "route"}
        onToggle={onToggleConnection}
      />
      <CodexHubProviderCard
        activeAdd={selectedId === ADD_ID}
        connected={codexConnected}
        enabledModelCount={enabledProviderModels}
        gatewayStatus={gatewayStatus}
        items={items}
        modelCount={providerModelCount}
        selectedId={selectedId}
        onAdd={onAdd}
        onReorder={onReorder}
        onSelect={onSelect}
        onToggleProvider={onToggleProvider}
      />
    </div>
  );
}

function OfficialOpenAICard({
  active,
  authState,
  modelCount,
  onSelect,
}: {
  active: boolean;
  authState: CodexAuthState;
  modelCount: number;
  onSelect: () => void;
}) {
  const authLabel =
    authState === "authorized" ? "Authorized" : authState === "missing" ? "Auth missing" : "Auth unknown";
  const authTone = authState === "authorized" ? "ok" : authState === "missing" ? "pending" : "muted";

  return (
    <section
      className={cx(
        "grid gap-3 rounded-md border p-3",
        active ? "border-action bg-blue-50/70" : "border-line bg-panel",
      )}
    >
      <button type="button" className="focus-ring rounded text-left" onClick={onSelect}>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold">Codex Desktop</h2>
            <p className="mt-1 text-xs text-slate-500">Codex app auth and official models</p>
          </div>
          <SourceStatusChip label={authLabel} tone={authTone} />
        </div>
      </button>

      <div className="flex items-center justify-between rounded-md border border-line bg-white px-2.5 py-2 text-xs">
        <span className="font-semibold text-slate-500">Official models</span>
        <span className="font-semibold text-ink">{modelCount}</span>
      </div>
    </section>
  );
}

function HubConnectionBridge({
  connected,
  disabled,
  onToggle,
}: {
  connected: boolean;
  disabled: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="grid grid-cols-[30px_minmax(0,1fr)] items-center gap-2 px-2 py-1.5">
      <span className="relative grid h-12 w-7 place-items-center">
        <span
          className={cx(
            "absolute -inset-y-2 left-1/2 border-l border-dashed",
            connected ? "border-emerald-300" : "border-slate-300",
          )}
          aria-hidden="true"
        />
        <span
          className={cx(
            "relative z-10 grid h-7 w-7 place-items-center rounded-full border shadow-sm",
            connected
              ? "border-emerald-300 bg-emerald-50 text-emerald-700"
              : "border-slate-300 bg-white text-slate-500",
          )}
          title={connected ? "Codex App connected to Hub" : "Codex App disconnected from Hub"}
          aria-label={connected ? "Codex App connected to Hub" : "Codex App disconnected from Hub"}
        >
          {connected ? <Link2 size={14} /> : <Link2Off size={14} />}
        </span>
      </span>
      <button
        type="button"
        className={cx(
          "focus-ring flex h-8 min-w-0 items-center justify-center rounded-full border px-3 text-sm font-semibold transition-colors",
          connected
            ? "border-emerald-300 bg-white text-emerald-700 hover:bg-emerald-50"
            : "border-action/30 bg-white text-action hover:bg-blue-50",
        )}
        disabled={disabled}
        onClick={onToggle}
        title={connected ? "Disconnect Codex App from Hub" : "Connect Codex App to Hub"}
      >
        {connected ? "Disconnect" : "Connect"}
      </button>
    </div>
  );
}

function CodexHubProviderCard({
  activeAdd,
  connected,
  enabledModelCount,
  gatewayStatus,
  items,
  modelCount,
  onAdd,
  onReorder,
  onSelect,
  onToggleProvider,
  selectedId,
}: {
  activeAdd: boolean;
  connected: boolean;
  enabledModelCount: number;
  gatewayStatus: GatewayStatus | null;
  items: ProviderNavItem[];
  modelCount: number;
  onAdd: () => void;
  onReorder: (items: ProviderNavItem[]) => void;
  onSelect: (id: string) => void;
  onToggleProvider: (providerId: string, enabled: boolean) => void;
  selectedId: string;
}) {
  return (
    <section
      className={cx(
        "grid min-h-0 grid-rows-[auto_auto_minmax(0,1fr)_auto] gap-3 rounded-md border p-3 transition-colors",
        connected ? "border-emerald-200 bg-emerald-50/45" : "border-line bg-slate-50",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold">Codex Hub</h2>
          <p className="mt-1 truncate text-xs text-slate-500">External provider catalog</p>
        </div>
        <SourceStatusChip {...gatewayStatusChip(gatewayStatus)} />
      </div>

      <div className="grid gap-2">
        <div className="grid grid-cols-2 gap-2 text-xs">
          <SourceMetric label="Models" value={String(modelCount)} />
          <SourceMetric label="Enabled" value={String(enabledModelCount)} />
        </div>
      </div>

      <div className="min-h-0 overflow-auto pr-1">
        {items.length ? (
          <SortableList
            className="space-y-2"
            items={items}
            getId={(item) => item.id}
            onReorder={onReorder}
            renderItem={(item) => (
              <ProviderNavButton
                active={selectedId === item.provider.id}
                enabled={item.provider.enabled}
                label={item.provider.name}
                meta={`${item.provider.models.filter((model) => model.enabled).length}/${item.provider.models.length} models`}
                onClick={() => onSelect(item.provider.id)}
                onToggle={(enabled) => onToggleProvider(item.provider.id, enabled)}
              />
            )}
          />
        ) : (
          <div className="grid min-h-[96px] place-items-center rounded-md border border-dashed border-line bg-white px-3 text-center text-xs text-slate-500">
            Add a Hub provider to expose external models.
          </div>
        )}
      </div>

      <button
        type="button"
        className={cx(
          "focus-ring flex h-10 w-full items-center justify-center gap-2 rounded-md border border-dashed border-line text-sm font-medium",
          activeAdd ? "bg-blue-50 text-action" : "bg-white text-slate-600 hover:bg-slate-50",
        )}
        onClick={onAdd}
      >
        <Plus size={15} />
        Add provider
      </button>
    </section>
  );
}

function gatewayStatusChip(status: GatewayStatus | null): { label: string; tone: "ok" | "muted" | "pending" } {
  if (!status) {
    return { label: "Gateway unknown", tone: "pending" };
  }
  return status.proxy_running
    ? { label: "Gateway running", tone: "ok" }
    : { label: "Gateway stopped", tone: "muted" };
}

function codexAuthChip(authState: CodexAuthState): { label: string; tone: "ok" | "muted" | "pending" } {
  if (authState === "authorized") {
    return { label: "Authorized", tone: "ok" };
  }
  if (authState === "missing") {
    return { label: "Auth missing", tone: "pending" };
  }
  return { label: "Auth unknown", tone: "muted" };
}

function SourceStatusChip({ label, tone }: { label: string; tone: "ok" | "muted" | "pending" }) {
  return (
    <span
      className={cx(
        "inline-flex h-6 max-w-[112px] items-center rounded-full border px-2 text-[11px] font-semibold leading-none",
        tone === "ok" && "border-emerald-200 bg-emerald-50 text-emerald-700",
        tone === "muted" && "border-slate-200 bg-white text-slate-500",
        tone === "pending" && "border-amber-200 bg-amber-50 text-amber-700",
      )}
    >
      <span className="truncate whitespace-nowrap">{label}</span>
    </span>
  );
}

function SourceMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-md border border-line bg-white px-2 py-1.5">
      <div className="truncate text-[10px] font-semibold uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-0.5 truncate font-semibold text-ink">{value}</div>
    </div>
  );
}

function ProviderNavButton({
  active,
  enabled,
  label,
  meta,
  onClick,
  onToggle,
}: {
  active: boolean;
  enabled: boolean;
  label: string;
  meta: string;
  onClick: () => void;
  onToggle: (enabled: boolean) => void;
}) {
  return (
    <div
      className={cx(
        "grid min-h-[58px] w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-2 rounded-md px-3 py-2 text-sm",
        active ? "bg-blue-50 text-action" : "hover:bg-panel",
      )}
    >
      <button type="button" className="focus-ring min-w-0 text-left" onClick={onClick}>
        <span className="block truncate font-semibold">{label}</span>
        <span className="block truncate text-xs text-slate-500">{meta}</span>
      </button>
      <SwitchControl
        checked={enabled}
        label={enabled ? "Provider enabled" : "Provider disabled"}
        showLabel={false}
        onChange={onToggle}
      />
    </div>
  );
}

function RuntimePanel({
  busy,
  onDraftChange,
  onSave,
  onSyncNow,
  onToggleAutostart,
  settings,
}: {
  busy: string | null;
  onDraftChange: (settings: Settings) => void;
  onSave: () => void;
  onSyncNow: () => void;
  onToggleAutostart: (enabled: boolean) => void;
  settings: Settings;
}) {
  return (
    <div className="border-t border-line p-3">
      <div className="mb-2 flex items-center gap-2 text-sm font-semibold">
        <SlidersHorizontal size={15} />
        Runtime
      </div>
      <div className="grid gap-2">
        <label className="grid gap-1 text-xs font-medium text-slate-600">
          Proxy port
          <input
            className="field h-9"
            type="number"
            min={1024}
            max={65535}
            value={settings.proxy_port}
            onChange={(event) =>
              onDraftChange({ ...settings, proxy_port: Number(event.target.value) })
            }
          />
        </label>
        <Toggle
          label="Auto-start proxy"
          checked={settings.auto_start_proxy}
          onChange={onToggleAutostart}
        />
        <Toggle
          label="Auto-sync history"
          checked={settings.auto_sync_history}
          onChange={(value) => onDraftChange({ ...settings, auto_sync_history: value })}
        />
        <div className="grid grid-cols-2 gap-2">
          <button
            type="button"
            className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md bg-action px-3 text-sm font-semibold text-white"
            disabled={busy === "settings"}
            onClick={onSave}
          >
            <Save size={15} />
            Save
          </button>
          <button
            type="button"
            className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md border border-line bg-panel px-3 text-sm font-semibold"
            disabled={busy === "sync"}
            onClick={onSyncNow}
          >
            <RefreshCcw size={15} />
            Sync
          </button>
        </div>
      </div>
    </div>
  );
}

function OfficialDetail({
  authState,
  busy,
  gatewayContextById,
  included,
  models,
  officialDisabledModels,
  onRefresh,
  onReorder,
  onToggleInclude,
  onToggleModel,
}: {
  authState: CodexAuthState;
  busy: string | null;
  gatewayContextById: Map<string, number>;
  included: boolean;
  models: Model[];
  officialDisabledModels: string[];
  onRefresh: () => void;
  onReorder: (models: Model[]) => void;
  onToggleInclude: (value: boolean) => void;
  onToggleModel: (modelId: string, enabled: boolean) => void;
}) {
  return (
    <div className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)]">
      <div className="grid gap-4 border-b border-line p-5">
        <HeaderRow
          title="Codex"
          subtitle="OpenAI subscription catalog"
          actions={
            <>
              <SourceStatusChip {...codexAuthChip(authState)} />
              <Toggle label="Include in Codex Hub" checked={included} onChange={onToggleInclude} />
              <IconButton title="Refresh official models" disabled={busy === "official-refresh"} onClick={onRefresh}>
                <RefreshCcw size={16} />
              </IconButton>
            </>
          }
        />
      </div>
      <ModelSection
        contextById={gatewayContextById}
        disabled
        models={models}
        officialDisabledModels={officialDisabledModels}
        onReorder={onReorder}
        onToggleOfficialModel={onToggleModel}
      />
    </div>
  );
}

function ProviderDetail({
  busy,
  discoverError,
  onChange,
  onDelete,
  onProbe,
  onRefresh,
  probeResult,
  provider,
}: {
  busy: string | null;
  discoverError?: string | null;
  onChange: (provider: Provider, successMessage?: string) => void;
  onDelete: () => void;
  onProbe: (provider: Provider) => Promise<UpstreamFormatProbeResult | null>;
  onRefresh: (provider: Provider) => void;
  probeResult: UpstreamFormatProbeResult | null;
  provider: Provider;
}) {
  const [draft, setDraft] = useState(provider);
  const dirty = JSON.stringify(draft) !== JSON.stringify(provider);

  useEffect(() => {
    setDraft(provider);
  }, [provider]);

  function updateModel(modelId: string, patch: Partial<Model>) {
    setDraft((current) => ({
      ...current,
      models: current.models.map((model) =>
        model.id === modelId ? normalizeModel({ ...model, ...patch }) : model,
      ),
    }));
  }

  function addModel() {
    const id = uniqueModelId(draft.models);
    setDraft((current) => ({
      ...current,
      models: [...current.models, createDraftModel(id, current.models.length + 1)],
    }));
    return id;
  }

  function removeModel(modelId: string) {
    const next = {
      ...draft,
      models: renumberModels(draft.models.filter((model) => model.id !== modelId)),
    };
    setDraft(next);
    onChange(next, "Model removed");
  }

  async function runProbe() {
    const result = await onProbe(draft);
    if (result && result.recommended_format !== "auto") {
      setDraft((current) => ({ ...current, upstream_format: result.recommended_format }));
    }
  }

  return (
    <div className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)_auto]">
      <div className="grid gap-2 border-b border-line p-4">
        <HeaderRow
          title={provider.name}
          actions={
            <>
              <button
                type="button"
                className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md border border-line bg-panel px-3 text-sm font-semibold hover:bg-slate-100 disabled:bg-slate-100"
                disabled={busy === "probe" || !draft.base_url.trim()}
                onClick={() => void runProbe()}
              >
                <FlaskConical size={16} />
                Test
              </button>
              <IconButton
                title="Delete provider"
                danger
                disabled={busy === "save"}
                onClick={onDelete}
              >
                <Trash2 size={16} />
              </IconButton>
            </>
          }
        />

        <div className="grid gap-2 lg:grid-cols-2">
          <Field label="Name">
            <input
              className="field field-compact"
              value={draft.name}
              onChange={(event) => setDraft({ ...draft, name: event.target.value })}
            />
          </Field>
          <Field label="API key">
            <ApiKeyInput
              value={draft.api_key ?? ""}
              onChange={(apiKey) => setDraft({ ...draft, api_key: apiKey || null })}
            />
          </Field>
          <Field label="Base URL" className="lg:col-span-2">
            <input
              className="field field-compact"
              value={draft.base_url}
              onChange={(event) => setDraft({ ...draft, base_url: event.target.value })}
            />
          </Field>
          <div className="lg:col-span-2">
            <ProviderCapabilitiesPanel
              format={draft.upstream_format ?? "auto"}
              result={probeResult}
            />
          </div>
        </div>
        {probeResult && (
          <ProbeResultPanel
            result={probeResult}
            onApply={() =>
              setDraft({ ...draft, upstream_format: probeResult.recommended_format })
            }
          />
        )}
      </div>

      <ModelSection
        discoverDisabled={!draft.base_url.trim()}
        discoverBusy={busy === draft.id}
        discoverError={discoverError}
        models={draft.models}
        providerId={draft.id}
        onAdd={addModel}
        onDiscover={() => onRefresh(draft)}
        onReorder={(models) => setDraft({ ...draft, models: renumberModels(models) })}
        onRemove={removeModel}
        onToggle={(modelId, enabled) => updateModel(modelId, { enabled })}
        onUpdate={updateModel}
      />
      <div className="flex items-center justify-end border-t border-line px-5 py-3">
        <button
          type="button"
          className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md bg-action px-3 text-sm font-semibold text-white disabled:bg-slate-300"
          disabled={!dirty || busy === "save"}
          onClick={() => onChange(draft, `${draft.name} saved`)}
        >
          <Save size={16} />
          Save
        </button>
      </div>
    </div>
  );
}

function ModelSection({
  contextById,
  disabled,
  discoverBusy,
  discoverDisabled,
  discoverError,
  models,
  onAdd,
  onDiscover,
  onRemove,
  onReorder,
  officialDisabledModels,
  providerId,
  onToggleOfficialModel,
  onToggle,
  onUpdate,
}: {
  contextById?: Map<string, number>;
  disabled?: boolean;
  discoverBusy?: boolean;
  discoverDisabled?: boolean;
  discoverError?: string | null;
  models: Model[];
  onAdd?: () => string | undefined;
  onDiscover?: () => void;
  onRemove?: (modelId: string) => void;
  onReorder: (models: Model[]) => void;
  officialDisabledModels?: string[];
  providerId?: string;
  onToggleOfficialModel?: (modelId: string, enabled: boolean) => void;
  onToggle?: (modelId: string, enabled: boolean) => void;
  onUpdate?: (modelId: string, patch: Partial<Model>) => void;
}) {
  const [editingModelId, setEditingModelId] = useState<string | null>(null);
  const editingModel = editingModelId ? models.find((model) => model.id === editingModelId) ?? null : null;

  function addAndEdit() {
    const modelId = onAdd?.();
    if (modelId) {
      setEditingModelId(modelId);
    }
  }

  function applyModelUpdate(modelId: string, nextModel: Model) {
    onUpdate?.(modelId, nextModel);
    setEditingModelId(null);
  }

  return (
    <div className="grid min-h-0 grid-rows-[auto_minmax(0,1fr)] gap-3 p-5">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold">Models</h3>
          <p className="mt-1 text-xs text-slate-500">{models.length} configured</p>
        </div>
        <div className="flex min-w-0 flex-wrap items-center justify-end gap-2">
          {discoverError && (
            <span className="max-w-[260px] truncate text-xs font-medium text-danger" title={discoverError}>
              {discoverError}
            </span>
          )}
          {onDiscover && (
            <button
              type="button"
              className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md border border-line bg-panel px-3 text-sm font-semibold hover:bg-slate-100 disabled:bg-slate-100"
              disabled={discoverBusy || discoverDisabled}
              onClick={onDiscover}
            >
              <RefreshCcw size={16} />
              Discover
            </button>
          )}
          {!disabled && (
            <button
              type="button"
              className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md border border-line bg-panel px-3 text-sm font-semibold hover:bg-slate-100"
              onClick={addAndEdit}
            >
              <Plus size={16} />
              Add model
            </button>
          )}
        </div>
      </div>
      <div className="min-h-0 overflow-auto -mr-3 pr-3">
        {models.length === 0 ? (
          <div className="rounded-md border border-line bg-panel p-4 text-sm text-slate-500">
            No models
          </div>
        ) : (
          <SortableList
            className="space-y-2"
            items={models}
            getId={(model) => model.id}
            onReorder={onReorder}
            renderItem={(model) => {
            const contextWindow = contextById?.get(model.id) ?? model.context_window;
            const modelEnabled = disabled
              ? !isOfficialModelDisabled(officialDisabledModels ?? [], model.id)
              : model.enabled;
            const actions = (
              <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500 lg:justify-end">
                {modelCapabilityTags(model).map((tag) => (
                  <ModelCapabilityChip key={tag} tag={tag} />
                ))}
                <CapabilityChip label={formatContextWindow(contextWindow)} />
                {disabled && onToggleOfficialModel && (
                  <SwitchControl
                    checked={modelEnabled}
                    label={modelEnabled ? "Model enabled" : "Model disabled"}
                    showLabel={false}
                    onChange={(checked) => onToggleOfficialModel(model.id, checked)}
                  />
                )}
                {!disabled && onToggle && (
                  <SwitchControl
                    checked={modelEnabled}
                    label={modelEnabled ? "Model enabled" : "Model disabled"}
                    showLabel={false}
                    onChange={(checked) => onToggle(model.id, checked)}
                  />
                )}
              </div>
            );
            return (
              <div
                className={cx(
                  "grid min-h-[52px] gap-3 px-3 py-2 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center",
                  !disabled && "cursor-pointer",
                  !modelEnabled && "opacity-70",
                )}
                role={!disabled ? "button" : undefined}
                tabIndex={!disabled ? 0 : undefined}
                onClick={!disabled ? () => setEditingModelId(model.id) : undefined}
                onKeyDown={
                  !disabled
                    ? (event) => {
                        if (event.target !== event.currentTarget) {
                          return;
                        }
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          setEditingModelId(model.id);
                        }
                      }
                    : undefined
                }
              >
                <ModelIdentity model={model} providerId={providerId} />
                <div
                  onClick={(event) => event.stopPropagation()}
                  onKeyDown={(event) => event.stopPropagation()}
                >
                  {actions}
                </div>
              </div>
            );
            }}
          />
        )}
      </div>
      {!disabled && editingModel && (
        <ModelEditorOverlay
          model={editingModel}
          onApply={(nextModel) => applyModelUpdate(editingModel.id, nextModel)}
          onClose={() => setEditingModelId(null)}
          onRemove={onRemove ? () => {
            onRemove(editingModel.id);
            setEditingModelId(null);
          } : undefined}
        />
      )}
    </div>
  );
}

function providerQualifiedModelId(providerId: string, modelId: string) {
  const cleanProviderId = providerId.trim();
  const cleanModelId = modelId.trim();
  if (!cleanProviderId || !cleanModelId || cleanModelId.startsWith(`${cleanProviderId}/`)) {
    return cleanModelId;
  }
  return `${cleanProviderId}/${cleanModelId}`;
}

function ModelIdentity({ model, providerId }: { model: Model; providerId?: string }) {
  const [copied, setCopied] = useState(false);
  const copyValue = providerId ? providerQualifiedModelId(providerId, model.id) : model.id;

  async function copyModelId(event: React.MouseEvent<HTMLButtonElement>) {
    event.stopPropagation();
    try {
      await navigator.clipboard.writeText(copyValue);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="min-w-0">
      <span className="block truncate text-sm font-medium">{displayModel(model)}</span>
      <span className="mt-0.5 flex min-w-0 items-center gap-1 text-xs text-slate-500">
        <span className="min-w-0 truncate font-mono">{model.id}</span>
        <button
          type="button"
          className="focus-ring inline-flex h-6 min-w-[66px] shrink-0 items-center justify-center gap-1 rounded border border-transparent px-1.5 text-[11px] font-semibold text-slate-500 hover:border-line hover:bg-panel hover:text-ink"
          onClick={copyModelId}
          title={`Copy model ID: ${copyValue}`}
          aria-label={`Copy model ID ${copyValue}`}
        >
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? "Copied" : "Copy"}
        </button>
      </span>
    </div>
  );
}

function ModelEditorOverlay({
  model,
  onApply,
  onClose,
  onRemove,
}: {
  model: Model;
  onApply: (model: Model) => void;
  onClose: () => void;
  onRemove?: () => void;
}) {
  const [draft, setDraft] = useState<Model>(() => normalizeModel(model));
  const reasoningEnabled = (draft.supported_reasoning_levels ?? []).length > 0;

  useEffect(() => {
    setDraft(normalizeModel(model));
  }, [model]);

  function setReasoningEnabled(enabled: boolean) {
    setDraft((current) => {
      const levels = current.supported_reasoning_levels?.length
        ? current.supported_reasoning_levels
        : reasoningLevelOptions;
      return {
        ...current,
        supported_reasoning_levels: enabled ? levels : [],
        default_reasoning_level: enabled
          ? current.default_reasoning_level && levels.includes(current.default_reasoning_level)
            ? current.default_reasoning_level
            : "medium"
          : null,
      };
    });
  }

  function setReasoningLevel(level: string, checked: boolean) {
    setDraft((current) => {
      const levels = toggleReasoningLevel(current.supported_reasoning_levels ?? [], level, checked);
      return {
        ...current,
        supported_reasoning_levels: levels,
        default_reasoning_level:
          current.default_reasoning_level && levels.includes(current.default_reasoning_level)
            ? current.default_reasoning_level
            : levels.includes("medium")
              ? "medium"
              : levels[0] ?? null,
      };
    });
  }

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-slate-950/20 p-6">
      <div className="grid w-full max-w-[760px] overflow-hidden rounded-md border border-line bg-white shadow-xl">
        <div className="flex items-start justify-between gap-3 border-b border-line px-5 py-4">
          <div className="min-w-0">
            <h3 className="truncate text-base font-semibold">Model settings</h3>
            <p className="mt-1 truncate text-xs text-slate-500">{model.id}</p>
          </div>
          <button
            type="button"
            className="focus-ring grid h-8 w-8 place-items-center rounded-md border border-line bg-panel hover:bg-slate-100"
            onClick={onClose}
            aria-label="Close model settings"
          >
            <X size={16} />
          </button>
        </div>

        <div className="grid gap-4 p-5">
          <section className="grid gap-3 rounded-md border border-line bg-panel p-3">
            <div>
              <h4 className="text-sm font-semibold">Identity</h4>
              <p className="mt-0.5 text-xs text-slate-500">Gateway-facing model name and limits</p>
            </div>
            <Field label="Model ID">
              <input
                className="field h-9"
                value={draft.id}
                onChange={(event) => setDraft({ ...draft, id: event.target.value })}
              />
            </Field>
            <Field label="Display name">
              <input
                className="field h-9"
                value={draft.display_name ?? ""}
                onChange={(event) => setDraft({ ...draft, display_name: event.target.value || null })}
              />
            </Field>
            <Field label="Context window">
              <input
                className="field h-9"
                min={0}
                type="number"
                value={draft.context_window ?? ""}
                onChange={(event) =>
                  setDraft({ ...draft, context_window: optionalPositiveNumber(event.target.value) })
                }
              />
            </Field>
          </section>

          <section className="grid gap-3 rounded-md border border-line bg-panel p-3">
            <div>
              <div className="text-sm font-semibold">Capabilities</div>
              <div className="mt-0.5 text-xs text-slate-500">Gateway-facing model metadata</div>
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              <label className="flex h-9 items-center justify-between rounded-md border border-line bg-white px-3 text-sm font-medium">
                <span className="inline-flex items-center gap-2">
                  <Eye size={15} />
                  Vision
                </span>
                <input
                  type="checkbox"
                  checked={hasVision(draft)}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      input_modalities: event.target.checked ? ["text", "image"] : ["text"],
                    })
                  }
                />
              </label>
              <label className="flex h-9 items-center justify-between rounded-md border border-line bg-white px-3 text-sm font-medium">
                <span className="inline-flex items-center gap-2">
                  <Brain size={15} />
                  Thinking
                </span>
                <input
                  type="checkbox"
                  checked={reasoningEnabled}
                  onChange={(event) => setReasoningEnabled(event.target.checked)}
                />
              </label>
            </div>
            {reasoningEnabled && (
              <div className="grid gap-3 rounded-md border border-line bg-white p-3 lg:grid-cols-[minmax(0,1fr)_190px]">
                <div className="grid gap-2">
                  <span className="text-xs font-semibold uppercase text-slate-500">Reasoning levels</span>
                  <div className="flex flex-wrap gap-2">
                    {reasoningLevelOptions.map((level) => (
                      <label
                        key={level}
                        className="flex h-8 items-center gap-2 rounded-md border border-line bg-white px-2 text-xs font-medium"
                      >
                        <input
                          type="checkbox"
                          checked={(draft.supported_reasoning_levels ?? []).includes(level)}
                          onChange={(event) => setReasoningLevel(level, event.target.checked)}
                        />
                        {level}
                      </label>
                    ))}
                  </div>
                </div>
                <Field label="Default reasoning">
                  <select
                    className="field h-9"
                    value={draft.default_reasoning_level ?? ""}
                    onChange={(event) =>
                      setDraft({ ...draft, default_reasoning_level: event.target.value || null })
                    }
                  >
                    {(draft.supported_reasoning_levels ?? []).map((level) => (
                      <option key={level} value={level}>
                        {level}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
            )}
          </section>
        </div>

        <div className="flex items-center justify-between gap-3 border-t border-line px-5 py-4">
          {onRemove ? (
            <button
              type="button"
              className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md border border-danger/40 bg-red-50 px-3 text-sm font-semibold text-danger"
              onClick={onRemove}
            >
              <Trash2 size={15} />
              Remove
            </button>
          ) : (
            <span />
          )}
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="focus-ring inline-flex h-9 items-center justify-center rounded-md border border-line bg-panel px-3 text-sm font-semibold hover:bg-slate-100"
              onClick={onClose}
            >
              Cancel
            </button>
            <button
              type="button"
              className="focus-ring inline-flex h-9 items-center justify-center rounded-md bg-action px-3 text-sm font-semibold text-white"
              onClick={() => onApply(normalizeModel(draft))}
            >
              Apply
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function optionalPositiveNumber(value: string) {
  if (!value.trim()) {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function CapabilityChip({ icon, label }: { icon?: React.ReactNode; label: string }) {
  return (
    <span className="inline-flex h-6 items-center gap-1.5 rounded-full border border-line bg-panel px-2 text-xs font-semibold text-slate-600">
      {icon}
      {label}
    </span>
  );
}

function ModelCapabilityChip({ tag }: { tag: "vision" | "thinking" }) {
  if (tag === "vision") {
    return <CapabilityChip icon={<Eye size={13} />} label="Vision" />;
  }
  return <CapabilityChip icon={<Brain size={13} />} label="Thinking" />;
}

function SwitchControl({
  checked,
  label,
  onChange,
  showLabel = true,
}: {
  checked: boolean;
  label: string;
  onChange: (checked: boolean) => void;
  showLabel?: boolean;
}) {
  return (
    <label
      className={cx(
        "inline-flex h-6 items-center gap-2 text-xs font-semibold text-slate-600",
        showLabel && "rounded-full border border-line bg-panel pl-2 pr-1",
      )}
    >
      <span className={showLabel ? "truncate" : "sr-only"}>{label}</span>
      <span className="relative inline-flex h-5 w-9 shrink-0 items-center">
        <input
          type="checkbox"
          className="peer sr-only"
          checked={checked}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span className="absolute inset-0 rounded-full border border-line bg-slate-200 transition-colors peer-checked:border-action peer-checked:bg-action" />
        <span className="absolute left-0.5 h-4 w-4 rounded-full bg-white shadow-sm transition-transform peer-checked:translate-x-4" />
      </span>
    </label>
  );
}

function modelCapabilityTags(model: Model): Array<"vision" | "thinking"> {
  const tags: Array<"vision" | "thinking"> = [];
  if (hasVision(model)) {
    tags.push("vision");
  }
  if ((model.supported_reasoning_levels ?? []).length || model.default_reasoning_level) {
    tags.push("thinking");
  }
  return tags;
}

function isOfficialModelDisabled(disabledModels: string[], modelId: string) {
  return disabledModels.some((item) => modelIdMatches(item, modelId));
}

function modelIdMatches(left: string, right: string) {
  const normalize = (value: string) => value.trim().replace(/^openai\//, "");
  return normalize(left) === normalize(right);
}

function withDefaultFastVariants(settings: Settings): Settings {
  const base = {
    ...settings,
    official_disabled_models: settings.official_disabled_models ?? [],
  };
  if (settings.gateway_fast_model_variants?.length) {
    return base;
  }
  return { ...base, gateway_fast_model_variants: DEFAULT_FAST_MODEL_VARIANTS };
}

function formatContextWindow(value?: number | null) {
  if (!value) {
    return "Unknown";
  }
  if (value >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(1)}M`;
  }
  if (value >= 1000) {
    const rounded = Math.round(value / 1000);
    return `${new Intl.NumberFormat("en-US").format(rounded)}K`;
  }
  return new Intl.NumberFormat("en-US").format(value);
}

function hasVision(model: Model) {
  return (model.input_modalities ?? ["text"]).includes("image");
}

function toggleReasoningLevel(current: string[], level: string, checked: boolean) {
  const next = checked ? [...new Set([...current, level])] : current.filter((item) => item !== level);
  return reasoningLevelOptions.filter((item) => next.includes(item));
}

function normalizeModel(model: Model): Model {
  const levels = model.supported_reasoning_levels ?? [];
  return {
    ...model,
    context_window: model.context_window ?? null,
    input_modalities: model.input_modalities?.length ? model.input_modalities : ["text"],
    supported_reasoning_levels: levels,
    default_reasoning_level:
      model.default_reasoning_level && levels.includes(model.default_reasoning_level)
        ? model.default_reasoning_level
        : null,
  };
}

function sortOfficialModels(models: Model[], sortOrder: string[]) {
  const order = new Map<string, number>();
  const effectiveOrder = sortOrder.length ? sortOrder : DEFAULT_OFFICIAL_MODEL_ORDER;
  effectiveOrder.forEach((id, index) => {
    for (const key of officialModelSortKeys(id)) {
      order.set(key, index);
    }
  });
  return [...models].sort((left, right) => {
    const leftIndex = officialModelSortKeys(left.id).reduce(
      (current, key) => Math.min(current, order.get(key) ?? Number.MAX_SAFE_INTEGER),
      Number.MAX_SAFE_INTEGER,
    );
    const rightIndex = officialModelSortKeys(right.id).reduce(
      (current, key) => Math.min(current, order.get(key) ?? Number.MAX_SAFE_INTEGER),
      Number.MAX_SAFE_INTEGER,
    );
    if (leftIndex !== rightIndex) {
      return leftIndex - rightIndex;
    }
    return (left.sort_order ?? Number.MAX_SAFE_INTEGER) - (right.sort_order ?? Number.MAX_SAFE_INTEGER);
  });
}

function mergeOfficialModelSources(catalog: Model[], metadata: Model[]) {
  const merged = new Map<string, Model>();
  for (const model of metadata.filter(isOfficialModel)) {
    merged.set(model.id, {
      ...model,
      enabled: true,
    });
  }
  for (const model of catalog.filter(isOfficialModel)) {
    const existing = merged.get(model.id);
    merged.set(model.id, {
      ...existing,
      ...model,
      context_window: existing?.context_window ?? model.context_window,
      max_output_tokens: existing?.max_output_tokens ?? model.max_output_tokens,
      input_modalities: existing?.input_modalities ?? model.input_modalities,
      supported_reasoning_levels: existing?.supported_reasoning_levels ?? model.supported_reasoning_levels,
      default_reasoning_level: existing?.default_reasoning_level ?? model.default_reasoning_level,
      enabled: true,
    });
  }
  return filterCodexVisibleOfficialModels(Array.from(merged.values()));
}

function isOfficialModel(model: Model) {
  return model.id.startsWith("openai/") || model.id.startsWith("gpt-");
}

function filterCodexVisibleOfficialModels(models: Model[]) {
  return models.filter((model) => !isOfficialGatewayFastVariant(model));
}

function isOfficialGatewayFastVariant(model: Model) {
  const normalizedId = model.id.trim().replace(/^openai\//, "");
  return normalizedId === "gpt-5.5-fast" || normalizedId === "gpt-5.4-fast";
}

function officialModelSortKeys(id: string) {
  const prefix = "openai/";
  return id.startsWith(prefix) ? [id, id.slice(prefix.length)] : [`${prefix}${id}`, id];
}

function uniqueModelId(models: Model[]) {
  const existing = new Set(models.map((model) => model.id));
  let index = models.length + 1;
  let id = `new-model-${index}`;
  while (existing.has(id)) {
    index += 1;
    id = `new-model-${index}`;
  }
  return id;
}

function createDraftModel(id: string, sortOrder: number): Model {
  return {
    id,
    display_name: "",
    upstream_model: "",
    context_window: 200_000,
    max_output_tokens: null,
    input_modalities: ["text"],
    supported_reasoning_levels: reasoningLevelOptions,
    default_reasoning_level: "medium",
    source_kind: "manual",
    locked: false,
    codex_enabled: true,
    gateway_exported: true,
    sort_order: sortOrder,
    enabled: true,
  };
}

function AddProviderPanel({
  busy,
  canAdd,
  discoverError,
  form,
  onAdd,
  onDiscover,
  onFormChange,
  onProbe,
  probeResult,
}: {
  busy: string | null;
  canAdd: boolean;
  discoverError?: string | null;
  form: typeof emptyProvider;
  onAdd: () => void;
  onDiscover: () => void;
  onFormChange: (form: typeof emptyProvider) => void;
  onProbe: () => void;
  probeResult: UpstreamFormatProbeResult | null;
}) {
  function updateModel(modelId: string, patch: Partial<Model>) {
    onFormChange({
      ...form,
      models: form.models.map((model) =>
        model.id === modelId ? normalizeModel({ ...model, ...patch }) : model,
      ),
    });
  }

  function addModel() {
    const id = uniqueModelId(form.models);
    onFormChange({
      ...form,
      models: [...form.models, createDraftModel(id, form.models.length + 1)],
    });
    return id;
  }

  return (
    <div className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)_auto]">
      <div className="grid gap-4 border-b border-line p-5">
        <HeaderRow
          title="Add provider"
          subtitle="Discover models before saving the provider."
          actions={
            <button
              type="button"
              className="focus-ring inline-flex h-9 items-center gap-2 rounded-md border border-line bg-panel px-3 text-sm font-semibold hover:bg-slate-100 disabled:bg-slate-100"
              disabled={busy === "probe" || !form.base_url.trim()}
              onClick={onProbe}
            >
              <FlaskConical size={16} />
              Test
            </button>
          }
        />
        <div className="grid gap-3">
          <Field label="Name">
            <input
              className="field"
              value={form.name}
              onChange={(event) => onFormChange({ ...form, name: event.target.value })}
            />
          </Field>
          <Field label="Base URL">
            <input
              className="field"
              value={form.base_url}
              onChange={(event) => onFormChange({ ...form, base_url: event.target.value })}
            />
          </Field>
          <Field label="API key">
            <ApiKeyInput
              value={form.api_key}
              onChange={(apiKey) => onFormChange({ ...form, api_key: apiKey })}
            />
          </Field>
          <ProviderCapabilitiesPanel format={form.upstream_format} result={probeResult} />
        </div>
        {probeResult && (
          <ProbeResultPanel
            result={probeResult}
            onApply={() => onFormChange({ ...form, upstream_format: probeResult.recommended_format })}
          />
        )}
      </div>

      <ModelSection
        discoverDisabled={!form.base_url.trim()}
        discoverBusy={busy === "discover"}
        discoverError={discoverError}
        models={form.models}
        onAdd={addModel}
        onDiscover={onDiscover}
        onReorder={(models) => onFormChange({ ...form, models: renumberModels(models) })}
        onRemove={(modelId) =>
          onFormChange({ ...form, models: form.models.filter((model) => model.id !== modelId) })
        }
        onToggle={(modelId, enabled) => updateModel(modelId, { enabled })}
        onUpdate={updateModel}
      />

      <div className="flex items-center justify-end border-t border-line px-5 py-3">
        <button
          type="button"
          className="focus-ring inline-flex h-9 items-center gap-2 rounded-md bg-action px-3 text-sm font-semibold text-white disabled:bg-slate-300"
          disabled={!canAdd || Boolean(busy)}
          onClick={onAdd}
        >
          <Plus size={16} />
          Add provider
        </button>
      </div>
    </div>
  );
}

type ProviderCapabilityState = "ok" | "fail" | "unknown" | "configured";

function ProviderCapabilitiesPanel({
  format,
  result,
}: {
  format?: UpstreamFormat | null;
  result: UpstreamFormatProbeResult | null;
}) {
  const configuredFormat = format ?? "auto";
  const hasProbe = Boolean(result);
  const responsesState = hasProbe
    ? boolCapabilityState(
        Boolean(result?.responses_text_ok || result?.responses_tool_ok || result?.responses_tool_stream_ok),
      )
    : configuredFormat === "responses"
      ? "configured"
      : "unknown";
  const chatState = hasProbe
    ? boolCapabilityState(Boolean(result?.chat_text_ok || result?.chat_tool_ok || result?.chat_tool_stream_ok))
    : configuredFormat === "chat_completions"
      ? "configured"
      : "unknown";
  const items: Array<{ label: string; state: ProviderCapabilityState }> = [
    { label: "Responses", state: responsesState },
    { label: "Chat Completions", state: chatState },
  ];

  return (
    <div className="flex min-w-0 items-center justify-between gap-2 rounded-md border border-line bg-panel px-3 py-2">
      <div className="flex min-w-0 items-center gap-2">
        <span className="shrink-0 text-xs font-semibold uppercase tracking-wide text-slate-500">
          Provider capabilities
        </span>
        <div className="flex shrink-0 gap-2">
          {items.map((item) => (
            <ProviderCapabilityChip key={item.label} label={item.label} state={item.state} />
          ))}
        </div>
      </div>
      <span className="shrink-0 rounded-full border border-line bg-white px-2 py-0.5 text-xs font-semibold text-slate-500">
        Adapter {shortUpstreamFormatLabel(result?.recommended_format ?? configuredFormat)}
      </span>
    </div>
  );
}

function boolCapabilityState(value: boolean): ProviderCapabilityState {
  return value ? "ok" : "fail";
}

function ProviderCapabilityChip({
  label,
  state,
}: {
  label: string;
  state: ProviderCapabilityState;
}) {
  const stateLabel =
    state === "ok"
      ? "OK"
      : state === "fail"
        ? "Fail"
        : state === "configured"
          ? "Configured"
          : "Unknown";
  return (
    <span
      className={cx(
        "inline-flex h-7 items-center gap-2 rounded-full border px-2.5 text-xs font-semibold",
        state === "ok" && "border-emerald-200 bg-emerald-50 text-emerald-700",
        state === "fail" && "border-red-200 bg-red-50 text-red-700",
        state === "configured" && "border-blue-200 bg-blue-50 text-blue-700",
        state === "unknown" && "border-line bg-white text-slate-500",
      )}
    >
      <span>{label}</span>
      <span className="text-[10px] uppercase tracking-wide opacity-70">{stateLabel}</span>
    </span>
  );
}

function ProbeResultPanel({
  onApply,
  result,
}: {
  onApply: () => void;
  result: UpstreamFormatProbeResult;
}) {
  const canApply = result.recommended_format !== "auto";
  const responsesOk = Boolean(
    result.responses_text_ok || result.responses_tool_ok || result.responses_tool_stream_ok,
  );
  const chatOk = Boolean(result.chat_text_ok || result.chat_tool_ok || result.chat_tool_stream_ok);

  return (
    <div className="border-t border-line pt-3">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold">Provider test result</h3>
          <p className="mt-1 truncate text-xs text-slate-500">
            {result.model ? `Model ${result.model}` : "No model selected"} - Recommended:{" "}
            {upstreamFormatLabel(result.recommended_format)}
          </p>
        </div>
        <button
          type="button"
          className="focus-ring inline-flex h-8 items-center justify-center rounded-md bg-action px-3 text-xs font-semibold text-white disabled:bg-slate-300"
          disabled={!canApply}
          onClick={onApply}
        >
          Apply recommendation
        </button>
      </div>
      <div className="grid gap-2 text-xs sm:grid-cols-2">
        <ProbeCheck label="Responses" ok={responsesOk} />
        <ProbeCheck label="Chat Completions" ok={chatOk} />
      </div>
      {result.notes.length > 0 && (
        <div className="mt-3 max-h-24 overflow-auto border-l-2 border-line pl-3 text-xs leading-5 text-slate-600">
          {result.notes.map((note, index) => (
            <div key={`${index}-${note}`} className="truncate">
              {note}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ProbeCheck({ label, ok }: { label: string; ok: boolean }) {
  return (
    <div className="flex items-center justify-between gap-3 bg-panel px-2 py-1.5">
      <span className="truncate text-slate-600">{label}</span>
      <span className={cx("font-semibold tabular-nums", ok ? "text-ok" : "text-danger")}>
        {ok ? "OK" : "Fail"}
      </span>
    </div>
  );
}

function upstreamFormatLabel(value?: UpstreamFormat | null) {
  if (value === "responses") {
    return "Responses native";
  }
  if (value === "chat_completions") {
    return "Chat Completions translate";
  }
  return "Auto detect";
}

function shortUpstreamFormatLabel(value?: UpstreamFormat | null) {
  if (value === "responses") {
    return "Responses";
  }
  if (value === "chat_completions") {
    return "Chat Completions";
  }
  return "Auto";
}

function shortProviderDiscoveryError(err: unknown) {
  const message = messageFromError(err);
  const missingEnv = message.match(/\b([A-Z_][A-Z0-9_]*_API_KEY)\b[^.]*\bis not set\b/i);
  if (missingEnv) {
    return `Discovery failed: ${missingEnv[1]} is not set`;
  }
  if (/unauthorized|401/i.test(message)) {
    return "Discovery failed: unauthorized";
  }
  if (/timeout|timed out/i.test(message)) {
    return "Discovery timed out";
  }
  if (/not found|404/i.test(message)) {
    return "Discovery failed: models endpoint missing";
  }
  if (/builder error|invalid/i.test(message)) {
    return "Discovery failed: invalid request";
  }
  return "Discovery failed";
}

function codexAuthStateFromGatewayStatus(status: GatewayStatus | null): CodexAuthState {
  if (!status) {
    return "unknown";
  }
  const auth = status.codex_auth;
  if (auth.logged_in || auth.access_token_present || auth.account_id_present) {
    return "authorized";
  }
  return "missing";
}

function HeaderRow({
  actions,
  subtitle,
  title,
}: {
  actions?: React.ReactNode;
  subtitle?: string;
  title: string;
}) {
  return (
    <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center">
      <div className="min-w-0">
        <h2 className="truncate text-base font-semibold">{title}</h2>
        {subtitle && <p className="mt-1 truncate text-sm text-slate-500">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

function Toggle({
  checked,
  label,
  onChange,
}: {
  checked: boolean;
  label: string;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className="flex h-9 items-center justify-between gap-3 rounded-md border border-line bg-panel px-3 text-sm font-medium">
      <span className="truncate">{label}</span>
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
    </label>
  );
}

function Field({
  children,
  className,
  label,
}: {
  children: React.ReactNode;
  className?: string;
  label: string;
}) {
  return (
    <label className={cx("grid gap-1 text-sm font-medium text-slate-700", className)}>
      {label}
      {children}
    </label>
  );
}

function ApiKeyInput({
  onChange,
  value,
}: {
  onChange: (value: string) => void;
  value: string;
}) {
  const [visible, setVisible] = useState(false);

  return (
    <div className="relative">
      <input
        className="field field-compact pr-10"
        type={visible ? "text" : "password"}
        autoComplete="off"
        spellCheck={false}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      <button
        type="button"
        className="focus-ring absolute right-1 top-1 grid h-7 w-7 place-items-center rounded-md text-slate-500 hover:bg-panel hover:text-ink"
        onClick={() => setVisible((current) => !current)}
        title={visible ? "Hide API key" : "Show API key"}
        aria-label={visible ? "Hide API key" : "Show API key"}
      >
        {visible ? <EyeOff size={15} /> : <Eye size={15} />}
      </button>
    </div>
  );
}

function IconButton({
  children,
  danger,
  disabled,
  onClick,
  title,
}: {
  children: React.ReactNode;
  danger?: boolean;
  disabled?: boolean;
  onClick: () => void;
  title: string;
}) {
  return (
    <button
      type="button"
      className={cx(
        "focus-ring grid h-9 w-9 place-items-center rounded-md border bg-panel",
        danger ? "border-danger/40 bg-red-50 text-danger" : "border-line text-ink hover:bg-slate-100",
      )}
      disabled={disabled}
      onClick={onClick}
      title={title}
    >
      {children}
    </button>
  );
}
