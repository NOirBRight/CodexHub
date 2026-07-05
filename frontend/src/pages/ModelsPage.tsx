import { RefreshCcw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { displayModel, mergeDiscoveredModels } from "../lib/format";
import { api, messageFromError } from "../lib/tauri";
import type { Model, Provider, Settings } from "../lib/types";

export function ModelsPage() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [officialModels, setOfficialModels] = useState<Model[]>([]);
  const [metadata, setMetadata] = useState<Model[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void load();
  }, []);

  const enabledProviderModels = useMemo(
    () =>
      providers.reduce(
        (total, provider) => total + provider.models.filter((model) => model.enabled).length,
        0,
      ),
    [providers],
  );

  async function load() {
    setBusy("load");
    try {
      const [nextSettings, nextProviders, catalog] = await Promise.all([
        api.getSettings(),
        api.getProviders(),
        api.listModels(),
      ]);
      const nextMetadata = await api.listModelMetadata();
      setSettings(nextSettings);
      setProviders(nextProviders);
      setMetadata(nextMetadata);
      setOfficialModels(
        catalog.filter((model) => model.id.startsWith("openai/") || model.id.startsWith("gpt-")),
      );
      setError(null);
    } catch (err) {
      setError(messageFromError(err));
    } finally {
      setBusy(null);
    }
  }

  async function saveProviders(next: Provider[]) {
    setProviders(next);
    const saved = await api.saveProviders(next);
    setProviders(saved);
    await api.generateCatalog();
  }

  async function toggleOfficial(value: boolean) {
    if (!settings) {
      return;
    }
    setBusy("official-toggle");
    try {
      const next = await api.saveSettings({ ...settings, include_official_models: value });
      setSettings(next);
      await api.generateCatalog();
      setError(null);
    } catch (err) {
      setError(messageFromError(err));
    } finally {
      setBusy(null);
    }
  }

  async function refreshOfficial() {
    setBusy("official-refresh");
    try {
      setOfficialModels(await api.refreshOfficialModels());
      await api.generateCatalog();
      setError(null);
    } catch (err) {
      setError(messageFromError(err));
    } finally {
      setBusy(null);
    }
  }

  async function refreshMetadata() {
    setBusy("metadata-refresh");
    try {
      setMetadata(await api.refreshModelMetadata());
      setError(null);
    } catch (err) {
      setError(messageFromError(err));
    } finally {
      setBusy(null);
    }
  }

  async function refreshProvider(provider: Provider) {
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
      setError(null);
    } catch (err) {
      setError(messageFromError(err));
    } finally {
      setBusy(null);
    }
  }

  async function toggleProviderModel(providerId: string, modelId: string, enabled: boolean) {
    try {
      await saveProviders(
        providers.map((provider) =>
          provider.id === providerId
            ? {
                ...provider,
                models: provider.models.map((model) =>
                  model.id === modelId ? { ...model, enabled } : model,
                ),
              }
            : provider,
        ),
      );
      setError(null);
    } catch (err) {
      setError(messageFromError(err));
    }
  }

  async function patchProviderModel(providerId: string, modelId: string, patch: Partial<Model>) {
    try {
      await saveProviders(
        providers.map((provider) =>
          provider.id === providerId
            ? {
                ...provider,
                models: provider.models.map((model) =>
                  model.id === modelId ? { ...model, ...patch } : model,
                ),
              }
            : provider,
        ),
      );
      setError(null);
    } catch (err) {
      setError(messageFromError(err));
    }
  }

  function metadataFor(modelId: string) {
    return metadata.find((item) => item.id === modelId);
  }

  return (
    <main className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)] gap-4">
      <section className="grid gap-3 rounded-md border border-line bg-white p-4 shadow-subtle md:grid-cols-[minmax(0,1fr)_auto] md:items-center">
        <div>
          <h2 className="text-base font-semibold">Model Catalog</h2>
          <p className="mt-1 text-sm text-slate-500">
            {officialModels.length} official, {enabledProviderModels} third-party enabled, {metadata.length} metadata rows
          </p>
        </div>
        <button
          type="button"
          className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-line bg-panel px-3 text-sm font-semibold"
          disabled={busy === "load"}
          onClick={() => void load()}
        >
          <RefreshCcw size={16} />
          Refresh
        </button>
        <button
          type="button"
          className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-line bg-panel px-3 text-sm font-semibold"
          disabled={busy === "metadata-refresh"}
          onClick={() => void refreshMetadata()}
        >
          <RefreshCcw size={16} />
          Refresh metadata
        </button>
        {error && (
          <div className="rounded-md bg-red-50 px-3 py-2 text-sm text-danger md:col-span-2">
            {error}
          </div>
        )}
      </section>

      <section className="min-h-0 overflow-auto">
        <div className="grid gap-4">
          <section className="rounded-md border border-line bg-white shadow-subtle">
            <div className="grid gap-3 border-b border-line p-4 md:grid-cols-[minmax(0,1fr)_auto] md:items-center">
              <label className="flex items-center gap-3 font-semibold">
                <input
                  type="checkbox"
                  checked={settings?.include_official_models ?? false}
                  onChange={(event) => void toggleOfficial(event.target.checked)}
                />
                Official OpenAI models
              </label>
              <button
                type="button"
                className="focus-ring inline-flex h-9 items-center gap-2 rounded-md border border-line bg-panel px-3 text-sm"
                disabled={busy === "official-refresh"}
                onClick={() => void refreshOfficial()}
              >
                <RefreshCcw size={15} />
                Refresh
              </button>
            </div>
            <ModelGrid models={officialModels} disabled metadataFor={metadataFor} />
          </section>

          {providers.map((provider) => (
            <section key={provider.id} className="rounded-md border border-line bg-white shadow-subtle">
              <div className="grid gap-3 border-b border-line p-4 md:grid-cols-[minmax(0,1fr)_auto] md:items-center">
                <div>
                  <h3 className="font-semibold">{provider.name}</h3>
                  <p className="mt-1 truncate text-sm text-slate-500">{provider.base_url}</p>
                </div>
                <button
                  type="button"
                  className="focus-ring inline-flex h-9 items-center gap-2 rounded-md border border-line bg-panel px-3 text-sm"
                  disabled={busy === provider.id}
                  onClick={() => void refreshProvider(provider)}
                >
                  <RefreshCcw size={15} />
                  Refresh
                </button>
              </div>
              <ModelGrid
                models={provider.models}
                metadataFor={metadataFor}
                onToggle={(modelId, enabled) => void toggleProviderModel(provider.id, modelId, enabled)}
                onPatch={(modelId, patch) => void patchProviderModel(provider.id, modelId, patch)}
              />
            </section>
          ))}
        </div>
      </section>
    </main>
  );
}

function ModelGrid({
  disabled,
  models,
  onToggle,
  onPatch,
  metadataFor,
}: {
  disabled?: boolean;
  models: Model[];
  onToggle?: (modelId: string, enabled: boolean) => void;
  onPatch?: (modelId: string, patch: Partial<Model>) => void;
  metadataFor?: (modelId: string) => Model | undefined;
}) {
  const { t } = useTranslation();

  if (models.length === 0) {
    return <div className="p-4 text-sm text-slate-500">{t("common.noModels")}</div>;
  }

  return (
    <div className="grid gap-2 p-4">
      {models.map((model) => (
        <div
          key={model.id}
          className="grid min-w-0 gap-3 rounded-md border border-line bg-panel px-3 py-3 text-sm lg:grid-cols-[minmax(0,1fr)_auto]"
        >
          <span className="min-w-0">
            <span className="block truncate font-medium">{displayModel(model)}</span>
            <span className="block truncate text-xs text-slate-500">{model.id}</span>
            <span className="mt-1 flex flex-wrap gap-2 text-xs text-slate-500">
              <span>Context {formatMaybe(metadataFor?.(model.id)?.context_window ?? model.context_window)}</span>
              <span>Output {formatMaybe(metadataFor?.(model.id)?.max_output_tokens ?? model.max_output_tokens)}</span>
              <span>{metadataFor?.(model.id)?.metadata_provenance?.source ?? "local"} metadata</span>
              <span>{priceLabel(metadataFor?.(model.id) ?? model)}</span>
            </span>
          </span>
          <span className="grid grid-cols-3 gap-3 text-xs text-slate-600 sm:grid-cols-3">
            <ToggleCell
              label="Enabled"
              checked={disabled ? true : model.enabled}
              disabled={disabled}
              onChange={(checked) => onToggle?.(model.id, checked)}
            />
            <ToggleCell
              label="Codex"
              checked={model.codex_enabled ?? true}
              disabled={disabled}
              onChange={(checked) => onPatch?.(model.id, { codex_enabled: checked })}
            />
            <ToggleCell
              label="Export"
              checked={model.gateway_exported ?? true}
              disabled={disabled}
              onChange={(checked) => onPatch?.(model.id, { gateway_exported: checked })}
            />
          </span>
        </div>
      ))}
    </div>
  );
}

function ToggleCell({
  checked,
  disabled,
  label,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="inline-flex items-center gap-2">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      {label}
    </label>
  );
}

function formatMaybe(value?: number | null) {
  return value == null ? "Unknown" : value.toLocaleString();
}

function priceLabel(model: Model) {
  if (!model.pricing) {
    return "Pricing unknown";
  }
  const input = model.pricing.input_per_million;
  const output = model.pricing.output_per_million;
  if (input == null || output == null) {
    return "Pricing partial";
  }
  return `$${input}/$${output} per 1M`;
}
