import {
  ArrowDown,
  ArrowUp,
  ChevronDown,
  ChevronRight,
  RefreshCcw,
  Save,
  Trash2,
} from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { displayModel, formatLimit, renumberModels } from "../lib/format";
import type { Model, Provider } from "../lib/types";

interface ProviderCardProps {
  provider: Provider;
  busy?: boolean;
  onChange: (provider: Provider) => void;
  onDelete: (providerId: string) => void;
  onRefreshModels: (provider: Provider) => void;
}

export function ProviderCard({
  provider,
  busy,
  onChange,
  onDelete,
  onRefreshModels,
}: ProviderCardProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(provider);

  useEffect(() => {
    setDraft(provider);
  }, [provider]);

  const enabledModels = provider.models.filter((model) => model.enabled).length;

  function updateModel(modelId: string, patch: Partial<Model>) {
    onChange({
      ...provider,
      models: provider.models.map((model) =>
        model.id === modelId ? { ...model, ...patch } : model,
      ),
    });
  }

  function reorderModel(modelId: string, direction: -1 | 1) {
    const index = provider.models.findIndex((model) => model.id === modelId);
    const nextIndex = index + direction;
    if (index < 0 || nextIndex < 0 || nextIndex >= provider.models.length) {
      return;
    }
    const next = [...provider.models];
    const [model] = next.splice(index, 1);
    next.splice(nextIndex, 0, model);
    onChange({ ...provider, models: renumberModels(next) });
  }

  return (
    <section className="min-w-0">
      <div className="grid gap-3 p-4 md:grid-cols-[minmax(0,1fr)_auto] md:items-start">
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <button
              type="button"
              className="focus-ring grid h-8 w-8 place-items-center rounded-md border border-line bg-panel"
              onClick={() => setExpanded((value) => !value)}
              title={expanded ? t("common.collapse") : t("common.expand")}
            >
              {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
            </button>
            <span className="truncate text-base font-semibold">{provider.name}</span>
            <span className="rounded-sm bg-slate-100 px-2 py-1 text-xs text-slate-600">
              {provider.id}
            </span>
            <label className="flex items-center gap-2 text-sm text-slate-700">
              <input
                type="checkbox"
                checked={provider.enabled}
                onChange={(event) => onChange({ ...provider, enabled: event.target.checked })}
              />
              {t("common.enabled")}
            </label>
          </div>
          <p className="mt-2 truncate text-sm text-slate-500">{provider.base_url}</p>
          <p className="mt-1 text-sm text-slate-500">
            {t("providers.modelsEnabled", { enabled: enabledModels, total: provider.models.length })}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="focus-ring grid h-9 w-9 place-items-center rounded-md border border-line bg-panel"
            title={t("providers.refreshModels")}
            disabled={busy}
            onClick={() => onRefreshModels(provider)}
          >
            <RefreshCcw size={16} />
          </button>
          <button
            type="button"
            className="focus-ring grid h-9 w-9 place-items-center rounded-md border border-line bg-panel"
            title={t("providers.editProvider")}
            onClick={() => setEditing((value) => !value)}
          >
            <Save size={16} />
          </button>
          <button
            type="button"
            className="focus-ring grid h-9 w-9 place-items-center rounded-md border border-danger/40 bg-red-50 text-danger"
            title={t("providers.deleteProvider")}
            onClick={() => onDelete(provider.id)}
          >
            <Trash2 size={16} />
          </button>
        </div>
      </div>

      {editing && (
        <div className="grid gap-3 border-t border-line bg-slate-50 p-4 md:grid-cols-2">
          <Field label={t("common.name")}>
            <input
              className="field"
              value={draft.name}
              onChange={(event) => setDraft({ ...draft, name: event.target.value })}
            />
          </Field>
          <Field label={t("providers.displayPrefix")}>
            <input
              className="field"
              value={draft.display_prefix ?? ""}
              onChange={(event) => setDraft({ ...draft, display_prefix: event.target.value })}
            />
          </Field>
          <Field label={t("common.baseUrl")}>
            <input
              className="field"
              value={draft.base_url}
              onChange={(event) => setDraft({ ...draft, base_url: event.target.value })}
            />
          </Field>
          <Field label={t("common.apiKey")}>
            <input
              className="field"
              value={draft.api_key ?? ""}
              onChange={(event) => setDraft({ ...draft, api_key: event.target.value })}
            />
          </Field>
          <div className="md:col-span-2">
            <button
              type="button"
              className="focus-ring rounded-md bg-action px-3 py-2 text-sm font-semibold text-white"
              onClick={() => {
                onChange(draft);
                setEditing(false);
              }}
            >
              {t("common.save")}
            </button>
          </div>
        </div>
      )}

      {expanded && (
        <div className="border-t border-line">
          {provider.models.length === 0 ? (
            <div className="px-4 py-5 text-sm text-slate-500">{t("common.noModels")}</div>
          ) : (
            <div className="divide-y divide-line">
              {provider.models.map((model, index) => (
                <div
                  key={model.id}
                  className="grid gap-3 px-4 py-3 md:grid-cols-[minmax(0,1fr)_auto_auto]"
                >
                  <label className="flex min-w-0 items-start gap-3">
                    <input
                      className="mt-1"
                      type="checkbox"
                      checked={model.enabled}
                      onChange={(event) =>
                        updateModel(model.id, { enabled: event.target.checked })
                      }
                    />
                    <span className="min-w-0">
                      <span className="block truncate text-sm font-medium">
                        {displayModel(model)}
                      </span>
                      <span className="block truncate text-xs text-slate-500">{model.id}</span>
                    </span>
                  </label>
                  <div className="text-xs text-slate-500 md:text-right">
                    <div>{t("providers.context")} {formatLimit(model.context_window)}</div>
                    <div>{t("common.output")} {formatLimit(model.max_output_tokens)}</div>
                  </div>
                  <div className="flex items-center gap-1">
                    <button
                      type="button"
                      className="mini-button grid w-8 place-items-center px-0"
                      disabled={index === 0}
                      onClick={() => reorderModel(model.id, -1)}
                      title={t("common.moveUp")}
                    >
                      <ArrowUp size={14} />
                    </button>
                    <button
                      type="button"
                      className="mini-button grid w-8 place-items-center px-0"
                      disabled={index === provider.models.length - 1}
                      onClick={() => reorderModel(model.id, 1)}
                      title={t("common.moveDown")}
                    >
                      <ArrowDown size={14} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="grid gap-1 text-sm font-medium text-slate-700">
      {label}
      {children}
    </label>
  );
}
