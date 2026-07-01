import {
  AlertTriangle,
  Eye,
  EyeOff,
  FlaskConical,
  Plus,
  RefreshCcw,
  Save,
  SlidersHorizontal,
  Trash2,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { SortableList } from "../components/SortableList";
import { cx, displayModel, formatLimit, mergeDiscoveredModels, renumberModels, slugify } from "../lib/format";
import { api, messageFromError } from "../lib/tauri";
import type { Model, Provider, Settings, UpstreamFormat, UpstreamFormatProbeResult } from "../lib/types";

const OFFICIAL_ID = "__official__";
const ADD_ID = "__add__";

const emptyProvider = {
  id: "",
  name: "",
  base_url: "",
  api_key: "",
  upstream_format: "auto" as UpstreamFormat,
  display_prefix: "",
};

const upstreamFormatOptions: Array<{ value: UpstreamFormat; label: string }> = [
  { value: "auto", label: "Auto detect" },
  { value: "responses", label: "Responses native (/v1/responses)" },
  { value: "chat_completions", label: "Chat Completions translate (/v1/chat/completions)" },
];

const reasoningLevelOptions = ["low", "medium", "high", "xhigh", "max"];

type ProviderNavItem =
  | { kind: "official"; id: typeof OFFICIAL_ID; sort_order: number }
  | { kind: "provider"; id: string; sort_order: number; provider: Provider };

export function ProvidersPage() {
  const [providers, setProviders] = useState<Provider[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [settingsDraft, setSettingsDraft] = useState<Settings | null>(null);
  const [officialModels, setOfficialModels] = useState<Model[]>([]);
  const [selectedId, setSelectedId] = useState<string>(OFFICIAL_ID);
  const [form, setForm] = useState(emptyProvider);
  const [discovered, setDiscovered] = useState<Model[]>([]);
  const [selectedDiscovered, setSelectedDiscovered] = useState<Set<string>>(new Set());
  const [probeResult, setProbeResult] = useState<UpstreamFormatProbeResult | null>(null);
  const [busy, setBusy] = useState<string | null>("load");
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    void load();
  }, []);

  useEffect(() => {
    setProbeResult(null);
  }, [selectedId]);

  const selectedProvider = useMemo(
    () => providers.find((provider) => provider.id === selectedId) ?? null,
    [providers, selectedId],
  );
  const enabledProviderModels = useMemo(
    () =>
      providers.reduce(
        (total, provider) => total + provider.models.filter((model) => model.enabled).length,
        0,
      ),
    [providers],
  );
  const providerNavItems = useMemo<ProviderNavItem[]>(() => {
    const officialOrder = settings?.official_provider_sort_order ?? 0;
    return [
      { kind: "official" as const, id: OFFICIAL_ID as typeof OFFICIAL_ID, sort_order: officialOrder },
      ...providers.map((provider) => ({
        kind: "provider" as const,
        id: provider.id,
        sort_order: provider.sort_order ?? 0,
        provider,
      })),
    ].sort((left, right) => {
      if (left.sort_order !== right.sort_order) {
        return left.sort_order - right.sort_order;
      }
      if (left.kind === "official") {
        return -1;
      }
      if (right.kind === "official") {
        return 1;
      }
      return left.id.localeCompare(right.id);
    });
  }, [providers, settings?.official_provider_sort_order]);
  const canAdd = form.name.trim() && form.base_url.trim();

  async function load() {
    setBusy("load");
    try {
      const [nextSettings, nextProviders, catalog] = await Promise.all([
        api.getSettings(),
        api.getProviders(),
        api.listModels(),
      ]);
      setSettings(nextSettings);
      setSettingsDraft(nextSettings);
      setProviders(nextProviders);
      setOfficialModels(
        sortOfficialModels(
          catalog.filter((model) => model.id.startsWith("openai/") || model.id.startsWith("gpt-")),
          nextSettings.official_model_sort_order,
        ),
      );
      if (selectedId !== OFFICIAL_ID && selectedId !== ADD_ID && !nextProviders.some((provider) => provider.id === selectedId)) {
        setSelectedId(nextProviders[0]?.id ?? OFFICIAL_ID);
      }
      setError(null);
    } catch (err) {
      setError(messageFromError(err));
    } finally {
      setBusy(null);
    }
  }

  async function saveProviders(next: Provider[], regenerateCatalog = true) {
    setBusy("save");
    try {
      const saved = await api.saveProviders(next);
      setProviders(saved);
      if (regenerateCatalog) {
        await api.generateCatalog();
      }
      setMessage("Provider settings saved");
      setError(null);
      return saved;
    } catch (err) {
      setError(messageFromError(err));
      throw err;
    } finally {
      setBusy(null);
    }
  }

  async function saveSettings(next: Settings, regenerateCatalog = false) {
    setBusy("settings");
    try {
      const saved = await api.saveSettings(next);
      setSettings(saved);
      setSettingsDraft(saved);
      if (regenerateCatalog) {
        await api.generateCatalog();
      }
      setMessage("Runtime settings saved");
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

  async function updateProvider(next: Provider) {
    await saveProviders(providers.map((provider) => (provider.id === next.id ? next : provider)));
  }

  async function reorderProviderNav(items: ProviderNavItem[]) {
    if (!settingsDraft) {
      return;
    }
    const nextProviders = providers.map((provider) => provider);
    let officialSortOrder = settingsDraft.official_provider_sort_order;

    items.forEach((item, index) => {
      const sortOrder = index + 1;
      if (item.kind === "official") {
        officialSortOrder = sortOrder;
        return;
      }
      const providerIndex = nextProviders.findIndex((provider) => provider.id === item.id);
      if (providerIndex >= 0) {
        nextProviders[providerIndex] = { ...nextProviders[providerIndex], sort_order: sortOrder };
      }
    });

    await saveProviders(nextProviders);
    if (officialSortOrder !== settingsDraft.official_provider_sort_order) {
      await saveSettings({ ...settingsDraft, official_provider_sort_order: officialSortOrder }, true);
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
    try {
      const models = await api.discoverProviderModels(provider.base_url, provider.api_key ?? "");
      await saveProviders(
        providers.map((item) =>
          item.id === provider.id
            ? { ...item, models: mergeDiscoveredModels(item.models, models) }
            : item,
        ),
      );
      setMessage(`${provider.name} models refreshed`);
    } catch (err) {
      setError(messageFromError(err));
    } finally {
      setBusy(null);
    }
  }

  async function refreshOfficialModels() {
    setBusy("official-refresh");
    try {
      setOfficialModels(await api.refreshOfficialModels());
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
    if (!window.confirm(`Delete provider ${providerId}?`)) {
      return;
    }
    const next = providers.filter((provider) => provider.id !== providerId);
    await saveProviders(next);
    setSelectedId(next[0]?.id ?? OFFICIAL_ID);
  }

  async function discoverForForm() {
    setBusy("discover");
    try {
      const models = await api.discoverProviderModels(form.base_url, form.api_key);
      setDiscovered(models);
      setSelectedDiscovered(new Set(models.map((model) => model.id)));
      setError(null);
    } catch (err) {
      setError(messageFromError(err));
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
    const selectedModel = discovered.find((model) => selectedDiscovered.has(model.id));
    return selectedModel?.id ?? discovered[0]?.id ?? null;
  }

  async function addProvider() {
    const id = form.id.trim() || slugify(form.name);
    if (!id) {
      setError("Provider id is required");
      return;
    }
    if (providers.some((provider) => provider.id === id)) {
      setError(`Provider id already exists: ${id}`);
      return;
    }

    const models = discovered
      .filter((model) => selectedDiscovered.has(model.id))
      .map((model, index) => ({ ...model, enabled: true, sort_order: index + 1 }));
    const nextSortOrder =
      Math.max(
        settingsDraft?.official_provider_sort_order ?? 0,
        0,
        ...providers.map((provider) => provider.sort_order ?? 0),
      ) + 1;
    await saveProviders([
      ...providers,
      {
        id,
        name: form.name.trim(),
        base_url: form.base_url.trim(),
        api_key: form.api_key.trim() || null,
        upstream_format: form.upstream_format,
        display_prefix: form.display_prefix.trim() || null,
        sort_order: nextSortOrder,
        enabled: true,
        models,
      },
    ]);
    setSelectedId(id);
    setForm(emptyProvider);
    setDiscovered([]);
    setSelectedDiscovered(new Set());
  }

  return (
    <main className="grid h-full min-h-0 min-w-[980px] grid-cols-[330px_minmax(0,1fr)] gap-4">
      <aside className="grid min-h-0 grid-rows-[auto_minmax(0,1fr)] overflow-hidden rounded-md border border-line bg-white shadow-subtle">
        <SidebarHeader
          enabledProviderModels={enabledProviderModels}
          officialCount={officialModels.length}
          providerCount={providers.length}
          onAdd={() => setSelectedId(ADD_ID)}
          onRefresh={() => void load()}
        />
        <ProviderNav
          officialEnabled={settings?.include_official_models ?? false}
          officialModelCount={officialModels.length}
          items={providerNavItems}
          selectedId={selectedId}
          onReorder={(items) => void reorderProviderNav(items)}
          onSelect={setSelectedId}
        />
      </aside>

      <section className="min-h-0 overflow-hidden rounded-md border border-line bg-white shadow-subtle">
        <div className="grid h-full min-h-0 grid-rows-[minmax(0,1fr)_auto]">
          <div className="min-h-0 overflow-auto">
            {selectedId === ADD_ID ? (
              <AddProviderPanel
                busy={busy}
                canAdd={Boolean(canAdd)}
                discovered={discovered}
                form={form}
                probeResult={probeResult}
                selected={selectedDiscovered}
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
                onSelectedChange={setSelectedDiscovered}
              />
            ) : selectedId === OFFICIAL_ID ? (
              <OfficialDetail
                busy={busy}
                included={settings?.include_official_models ?? false}
                models={officialModels}
                onRefresh={() => void refreshOfficialModels()}
                onReorder={(models) => void reorderOfficialModels(models)}
                onToggleInclude={(value) => {
                  if (settingsDraft) {
                    void saveSettings({ ...settingsDraft, include_official_models: value }, true);
                  }
                }}
              />
            ) : selectedProvider ? (
              <ProviderDetail
                busy={busy}
                probeResult={probeResult}
                provider={selectedProvider}
                onChange={(provider) => void updateProvider(provider)}
                onDelete={(providerId) => void deleteProvider(providerId)}
                onProbe={(provider) =>
                  probeUpstreamFormat(provider.base_url, provider.api_key ?? "", providerProbeModel(provider))
                }
                onRefresh={(provider) => void refreshProviderModels(provider)}
              />
            ) : (
              <div className="p-6 text-sm text-slate-500">Select a provider</div>
            )}
          </div>

          {(error || message) && (
            <div className="border-t border-line px-4 py-2 text-sm">
              {error ? <span className="text-danger">{error}</span> : <span>{message}</span>}
            </div>
          )}
        </div>
      </section>
    </main>
  );
}

function SidebarHeader({
  enabledProviderModels,
  officialCount,
  onAdd,
  onRefresh,
  providerCount,
}: {
  enabledProviderModels: number;
  officialCount: number;
  onAdd: () => void;
  onRefresh: () => void;
  providerCount: number;
}) {
  return (
    <div className="border-b border-line p-3">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Providers</h2>
          <p className="mt-1 text-xs text-slate-500">
            {providerCount} Hub, {officialCount} official, {enabledProviderModels} enabled
          </p>
        </div>
        <div className="flex items-center gap-1">
          <IconButton title="Refresh providers" onClick={onRefresh}>
            <RefreshCcw size={15} />
          </IconButton>
          <IconButton title="Add provider" onClick={onAdd}>
            <Plus size={16} />
          </IconButton>
        </div>
      </div>
    </div>
  );
}

function ProviderNav({
  officialEnabled,
  officialModelCount,
  items,
  onReorder,
  onSelect,
  selectedId,
}: {
  officialEnabled: boolean;
  officialModelCount: number;
  items: ProviderNavItem[];
  onReorder: (items: ProviderNavItem[]) => void;
  onSelect: (id: string) => void;
  selectedId: string;
}) {
  return (
    <div className="min-h-0 overflow-auto p-3">
      <div>
          <SortableList
            className="space-y-2"
            items={items}
            getId={(item) => item.id}
            onReorder={onReorder}
            renderItem={(item) =>
              item.kind === "official" ? (
                <ProviderNavButton
                  active={selectedId === OFFICIAL_ID}
                  enabled={officialEnabled}
                  label="Official OpenAI"
                  meta={`${officialModelCount} models`}
                  onClick={() => onSelect(OFFICIAL_ID)}
                />
              ) : (
                <ProviderNavButton
                  active={selectedId === item.provider.id}
                  enabled={item.provider.enabled}
                  label={item.provider.name}
                  meta={`${item.provider.models.filter((model) => model.enabled).length}/${item.provider.models.length} models`}
                  onClick={() => onSelect(item.provider.id)}
                />
              )
            }
          />
      </div>
      <button
        type="button"
        className={cx(
          "focus-ring mt-3 flex h-10 w-full items-center justify-center gap-2 rounded-md border border-dashed border-line text-sm font-medium",
          selectedId === ADD_ID ? "bg-blue-50 text-action" : "bg-panel text-slate-600 hover:bg-white",
        )}
        onClick={() => onSelect(ADD_ID)}
      >
        <Plus size={15} />
        Add provider
      </button>
    </div>
  );
}

function ProviderNavButton({
  active,
  enabled,
  label,
  meta,
  onClick,
}: {
  active: boolean;
  enabled: boolean;
  label: string;
  meta: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className={cx(
        "focus-ring flex min-h-[58px] w-full items-center justify-between rounded-md px-3 py-2 text-left text-sm",
        active ? "bg-blue-50 text-action" : "hover:bg-panel",
      )}
      onClick={onClick}
    >
      <span className="min-w-0">
        <span className="block truncate font-semibold">{label}</span>
        <span className="block truncate text-xs text-slate-500">{meta}</span>
      </span>
      <StatusDot enabled={enabled} />
    </button>
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
  busy,
  included,
  models,
  onRefresh,
  onReorder,
  onToggleInclude,
}: {
  busy: string | null;
  included: boolean;
  models: Model[];
  onRefresh: () => void;
  onReorder: (models: Model[]) => void;
  onToggleInclude: (value: boolean) => void;
}) {
  return (
    <div className="grid gap-0">
      <div className="grid gap-4 border-b border-line p-5">
        <HeaderRow
          title="Official OpenAI"
          subtitle="Official model catalog"
          actions={
            <>
              <Toggle label="Include in catalog" checked={included} onChange={onToggleInclude} />
              <IconButton title="Refresh official models" disabled={busy === "official-refresh"} onClick={onRefresh}>
                <RefreshCcw size={16} />
              </IconButton>
            </>
          }
        />
      </div>
      <ModelSection disabled models={models} onReorder={onReorder} />
    </div>
  );
}

function ProviderDetail({
  busy,
  onChange,
  onDelete,
  onProbe,
  onRefresh,
  probeResult,
  provider,
}: {
  busy: string | null;
  onChange: (provider: Provider) => void;
  onDelete: (providerId: string) => void;
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
      models: [
        ...current.models,
        {
          id,
          display_name: "",
          upstream_model: "",
          context_window: null,
          max_output_tokens: null,
          input_modalities: ["text"],
          supported_reasoning_levels: [],
          default_reasoning_level: null,
          sort_order: current.models.length + 1,
          enabled: true,
        },
      ],
    }));
  }

  async function runProbe() {
    const result = await onProbe(draft);
    if (result && result.recommended_format !== "auto") {
      setDraft((current) => ({ ...current, upstream_format: result.recommended_format }));
    }
  }

  return (
    <div className="grid gap-0">
      <div className="grid gap-4 border-b border-line p-5">
        <HeaderRow
          title={provider.name}
          subtitle={provider.id}
          actions={
            <>
              <Toggle
                label="Enabled"
                checked={draft.enabled}
                onChange={(enabled) => setDraft({ ...draft, enabled })}
              />
              <button
                type="button"
                className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md bg-action px-3 text-sm font-semibold text-white disabled:bg-slate-300"
                disabled={!dirty || busy === "save"}
                onClick={() => onChange(draft)}
              >
                <Save size={16} />
                Save
              </button>
              <IconButton
                title="Refresh models"
                disabled={busy === draft.id}
                onClick={() => onRefresh(draft)}
              >
                <RefreshCcw size={16} />
              </IconButton>
              <IconButton title="Delete provider" danger onClick={() => onDelete(draft.id)}>
                <Trash2 size={16} />
              </IconButton>
            </>
          }
        />

        <div className="grid gap-x-4 gap-y-3 lg:grid-cols-2">
          <Field label="Provider name">
            <input
              className="field"
              value={draft.name}
              onChange={(event) => setDraft({ ...draft, name: event.target.value })}
            />
          </Field>
          <Field label="Display prefix">
            <input
              className="field"
              value={draft.display_prefix ?? ""}
              onChange={(event) =>
                setDraft({ ...draft, display_prefix: event.target.value || null })
              }
            />
          </Field>
          <Field label="Base URL">
            <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-2">
              <input
                className="field"
                value={draft.base_url}
                onChange={(event) => setDraft({ ...draft, base_url: event.target.value })}
              />
              <button
                type="button"
                className="focus-ring inline-flex h-10 items-center justify-center gap-2 rounded-md border border-line bg-panel px-3 text-sm font-semibold hover:bg-slate-100"
                disabled={busy === "probe" || !draft.base_url.trim()}
                onClick={() => void runProbe()}
              >
                <FlaskConical size={16} />
                Probe
              </button>
            </div>
          </Field>
          <Field label="Upstream format">
            <select
              className="field"
              value={draft.upstream_format ?? "auto"}
              onChange={(event) =>
                setDraft({ ...draft, upstream_format: event.target.value as UpstreamFormat })
              }
            >
              {upstreamFormatOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <p className="text-xs font-normal text-slate-500">
              Codex always connects to CodexHub using Responses. This controls how CodexHub connects upstream.
            </p>
          </Field>
          <Field label="API key">
            <input
              className="field"
              type="password"
              autoComplete="off"
              value={draft.api_key ?? ""}
              onChange={(event) =>
                setDraft({ ...draft, api_key: event.target.value || null })
              }
            />
          </Field>
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
        models={draft.models}
        onAdd={addModel}
        onReorder={(models) => setDraft({ ...draft, models: renumberModels(models) })}
        onRemove={(modelId) =>
          setDraft({ ...draft, models: draft.models.filter((model) => model.id !== modelId) })
        }
        onToggle={(modelId, enabled) => updateModel(modelId, { enabled })}
        onUpdate={updateModel}
      />
    </div>
  );
}

function ModelSection({
  disabled,
  models,
  onAdd,
  onRemove,
  onReorder,
  onToggle,
  onUpdate,
}: {
  disabled?: boolean;
  models: Model[];
  onAdd?: () => void;
  onRemove?: (modelId: string) => void;
  onReorder: (models: Model[]) => void;
  onToggle?: (modelId: string, enabled: boolean) => void;
  onUpdate?: (modelId: string, patch: Partial<Model>) => void;
}) {
  return (
    <div className="grid gap-3 p-5">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold">Models</h3>
          <p className="mt-1 text-xs text-slate-500">{models.length} configured</p>
        </div>
        {!disabled && (
          <button
            type="button"
            className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md border border-line bg-panel px-3 text-sm font-semibold hover:bg-slate-100"
            onClick={onAdd}
          >
            <Plus size={16} />
            Add model
          </button>
        )}
      </div>
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
          renderItem={(model) => (
            <details className="group" open={!disabled && models.length === 1}>
              <summary className="grid min-h-[52px] cursor-pointer list-none gap-3 px-3 py-2 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center">
                <label className="flex min-w-0 items-start gap-3">
                  <input
                    className="mt-1"
                    type="checkbox"
                    checked={disabled ? true : model.enabled}
                    disabled={disabled}
                    onChange={(event) => onToggle?.(model.id, event.target.checked)}
                  />
                  <span className="min-w-0">
                    <span className="block truncate text-sm font-medium">{displayModel(model)}</span>
                    <span className="block truncate text-xs text-slate-500">{model.id}</span>
                  </span>
                </label>
                <div className="flex flex-wrap gap-2 text-xs text-slate-500 lg:justify-end">
                  {hasVision(model) && <span>Vision</span>}
                  <span>Context {formatLimit(model.context_window)}</span>
                  <span>Output {formatLimit(model.max_output_tokens)}</span>
                </div>
              </summary>
              {!disabled && (
                <div className="grid gap-3 border-t border-line px-3 pb-3 pt-3">
                  <div className="grid gap-3 lg:grid-cols-3">
                    <Field label="Display name">
                      <input
                        className="field h-9"
                        value={model.display_name ?? ""}
                        onChange={(event) =>
                          onUpdate?.(model.id, { display_name: event.target.value || null })
                        }
                      />
                    </Field>
                    <Field label="Catalog model id">
                      <input
                        className="field h-9"
                        value={model.id}
                        onChange={(event) => onUpdate?.(model.id, { id: event.target.value })}
                      />
                    </Field>
                    <Field label="Upstream model">
                      <input
                        className="field h-9"
                        value={model.upstream_model ?? ""}
                        onChange={(event) =>
                          onUpdate?.(model.id, { upstream_model: event.target.value || null })
                        }
                      />
                    </Field>
                    <Field label="Context window">
                      <input
                        className="field h-9"
                        type="number"
                        min={0}
                        value={model.context_window ?? ""}
                        onChange={(event) =>
                          onUpdate?.(model.id, { context_window: optionalPositiveNumber(event.target.value) })
                        }
                      />
                    </Field>
                    <Field label="Max output tokens">
                      <input
                        className="field h-9"
                        type="number"
                        min={0}
                        value={model.max_output_tokens ?? ""}
                        onChange={(event) =>
                          onUpdate?.(model.id, { max_output_tokens: optionalPositiveNumber(event.target.value) })
                        }
                      />
                    </Field>
                    <label className="flex h-[58px] items-end justify-between gap-3 rounded-md border border-line bg-panel px-3 pb-2 text-sm font-medium">
                      <span>Vision input</span>
                      <input
                        type="checkbox"
                        checked={hasVision(model)}
                        onChange={(event) =>
                          onUpdate?.(model.id, {
                            input_modalities: event.target.checked ? ["text", "image"] : ["text"],
                          })
                        }
                      />
                    </label>
                  </div>
                  <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_220px_auto] lg:items-end">
                    <div className="grid gap-2">
                      <span className="text-sm font-medium text-slate-700">Reasoning levels</span>
                      <div className="flex flex-wrap gap-2">
                        {reasoningLevelOptions.map((level) => (
                          <label
                            key={level}
                            className="flex h-8 items-center gap-2 rounded-md border border-line bg-panel px-2 text-xs font-medium"
                          >
                            <input
                              type="checkbox"
                              checked={(model.supported_reasoning_levels ?? []).includes(level)}
                              onChange={(event) =>
                                onUpdate?.(model.id, {
                                  supported_reasoning_levels: toggleReasoningLevel(
                                    model.supported_reasoning_levels ?? [],
                                    level,
                                    event.target.checked,
                                  ),
                                })
                              }
                            />
                            {level}
                          </label>
                        ))}
                      </div>
                    </div>
                    <Field label="Default reasoning">
                      <select
                        className="field h-9"
                        value={model.default_reasoning_level ?? ""}
                        onChange={(event) =>
                          onUpdate?.(model.id, { default_reasoning_level: event.target.value || null })
                        }
                      >
                        <option value="">None</option>
                        {(model.supported_reasoning_levels ?? []).map((level) => (
                          <option key={level} value={level}>
                            {level}
                          </option>
                        ))}
                      </select>
                    </Field>
                    <button
                      type="button"
                      className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md border border-danger/40 bg-red-50 px-3 text-sm font-semibold text-danger"
                      onClick={() => onRemove?.(model.id)}
                    >
                      <Trash2 size={15} />
                      Remove
                    </button>
                  </div>
                </div>
              )}
            </details>
          )}
        />
      )}
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
    input_modalities: model.input_modalities?.length ? model.input_modalities : ["text"],
    supported_reasoning_levels: levels,
    default_reasoning_level:
      model.default_reasoning_level && levels.includes(model.default_reasoning_level)
        ? model.default_reasoning_level
        : null,
  };
}

function sortOfficialModels(models: Model[], sortOrder: string[]) {
  if (!sortOrder.length) {
    return models;
  }
  const order = new Map<string, number>();
  sortOrder.forEach((id, index) => {
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

function AddProviderPanel({
  busy,
  canAdd,
  discovered,
  form,
  onAdd,
  onDiscover,
  onFormChange,
  onProbe,
  onSelectedChange,
  probeResult,
  selected,
}: {
  busy: string | null;
  canAdd: boolean;
  discovered: Model[];
  form: typeof emptyProvider;
  onAdd: () => void;
  onDiscover: () => void;
  onFormChange: (form: typeof emptyProvider) => void;
  onProbe: () => void;
  onSelectedChange: (selected: Set<string>) => void;
  probeResult: UpstreamFormatProbeResult | null;
  selected: Set<string>;
}) {
  return (
    <div className="grid gap-4 p-5">
      <HeaderRow
        title="Add provider"
        subtitle="Discover models before saving the provider."
        actions={
          <>
            <IconButton
              title="Discover models"
              disabled={busy === "discover" || !form.base_url.trim()}
              onClick={onDiscover}
            >
              <RefreshCcw size={16} />
            </IconButton>
            <button
              type="button"
              className="focus-ring inline-flex h-9 items-center gap-2 rounded-md bg-action px-3 text-sm font-semibold text-white"
              disabled={!canAdd || Boolean(busy)}
              onClick={onAdd}
            >
              <Plus size={16} />
              Add
            </button>
          </>
        }
      />
      <div className="grid gap-3 lg:grid-cols-2">
        <Field label="Provider id">
          <input
            className="field"
            value={form.id}
            onChange={(event) => onFormChange({ ...form, id: event.target.value })}
          />
        </Field>
        <Field label="Name">
          <input
            className="field"
            value={form.name}
            onChange={(event) => onFormChange({ ...form, name: event.target.value })}
          />
        </Field>
        <Field label="Base URL">
          <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-2">
            <input
              className="field"
              value={form.base_url}
              onChange={(event) => onFormChange({ ...form, base_url: event.target.value })}
            />
            <button
              type="button"
              className="focus-ring inline-flex h-10 items-center justify-center gap-2 rounded-md border border-line bg-panel px-3 text-sm font-semibold hover:bg-slate-100"
              disabled={busy === "probe" || !form.base_url.trim()}
              onClick={onProbe}
            >
              <FlaskConical size={16} />
              Probe
            </button>
          </div>
        </Field>
        <Field label="Upstream format">
          <select
            className="field"
            value={form.upstream_format}
            onChange={(event) =>
              onFormChange({ ...form, upstream_format: event.target.value as UpstreamFormat })
            }
          >
            {upstreamFormatOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <p className="text-xs font-normal text-slate-500">
            Codex always connects to CodexHub using Responses. This controls how CodexHub connects upstream.
          </p>
        </Field>
        <Field label="API key">
          <input
            className="field"
            type="password"
            autoComplete="off"
            value={form.api_key}
            onChange={(event) => onFormChange({ ...form, api_key: event.target.value })}
          />
        </Field>
        <Field label="Display prefix">
          <input
            className="field"
            value={form.display_prefix}
            onChange={(event) => onFormChange({ ...form, display_prefix: event.target.value })}
          />
        </Field>
      </div>
      {probeResult && (
        <ProbeResultPanel
          result={probeResult}
          onApply={() => onFormChange({ ...form, upstream_format: probeResult.recommended_format })}
        />
      )}
      {discovered.length > 0 && (
        <div className="grid gap-2">
          <h3 className="text-sm font-semibold">Discovered models</h3>
          <div className="grid gap-2 lg:grid-cols-2">
            {discovered.map((model) => (
              <label
                key={model.id}
                className="flex min-w-0 items-center gap-2 rounded-md border border-line bg-panel px-3 py-2 text-sm"
              >
                <input
                  type="checkbox"
                  checked={selected.has(model.id)}
                  onChange={(event) => {
                    const next = new Set(selected);
                    if (event.target.checked) {
                      next.add(model.id);
                    } else {
                      next.delete(model.id);
                    }
                    onSelectedChange(next);
                  }}
                />
                <span className="truncate">{displayModel(model)}</span>
              </label>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function ProbeResultPanel({
  onApply,
  result,
}: {
  onApply: () => void;
  result: UpstreamFormatProbeResult;
}) {
  const toolStreamFailed = !result.responses_tool_stream_ok && !result.chat_tool_stream_ok;
  const canApply = result.recommended_format !== "auto";

  return (
    <div className="border-t border-line pt-3">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold">Probe result</h3>
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
      <div className="grid gap-2 text-xs sm:grid-cols-2 xl:grid-cols-3">
        <ProbeCheck label="Models" ok={result.models_ok} />
        <ProbeCheck label="Responses text" ok={result.responses_text_ok} />
        <ProbeCheck label="Responses tools" ok={result.responses_tool_ok} />
        <ProbeCheck label="Responses stream" ok={result.responses_tool_stream_ok} />
        <ProbeCheck label="Chat text" ok={result.chat_text_ok} />
        <ProbeCheck label="Chat tools" ok={result.chat_tool_ok} />
        <ProbeCheck label="Chat stream" ok={result.chat_tool_stream_ok} />
      </div>
      {toolStreamFailed && (
        <div className="mt-3 flex gap-2 text-xs text-warn">
          <AlertTriangle size={15} className="mt-0.5 shrink-0" />
          <span>Tool streaming failed; normal chat may work while Codex tools or subagents fail.</span>
        </div>
      )}
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
  return upstreamFormatOptions.find((option) => option.value === value)?.label ?? "Auto detect";
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

function Field({ children, label }: { children: React.ReactNode; label: string }) {
  return (
    <label className="grid gap-1 text-sm font-medium text-slate-700">
      {label}
      {children}
    </label>
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

function StatusDot({ enabled }: { enabled: boolean }) {
  return enabled ? (
    <Eye size={15} className="shrink-0 text-ok" />
  ) : (
    <EyeOff size={15} className="shrink-0 text-slate-400" />
  );
}
