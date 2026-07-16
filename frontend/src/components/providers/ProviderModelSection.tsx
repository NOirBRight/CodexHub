import { Brain, Cable, Check, Copy, Eye, Plus, RefreshCcw, Trash2, X } from "lucide-react";
import type * as React from "react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { SortableList } from "../SortableList";
import { useVerticalOverflow } from "../../hooks/useVerticalOverflow";
import i18n from "../../i18n";
import { cx, displayModel } from "../../lib/format";
import { resolveOfficialModelContextWindow } from "../../lib/officialModels";
import { normalizeOfficialModelId } from "../../lib/settings";
import { reasoningLevelOptions, type InlineTestState } from "../../lib/providerForm";
import { normalizeModel } from "../../lib/providerModel";
import type { Model } from "../../lib/types";
import { Field } from "./ProviderFormControls";


export function ModelSection({
  contextById,
  disabled,
  discoverBusy,
  discoverDisabled,
  discoverError,
  headerControl,
  interactionDisabled = false,
  models,
  modelTestDisabled,
  onAdd,
  onCancelNewModel,
  onDiscover,
  onRefresh,
  onRemove,
  onReorder,
  onTestModel,
  officialDisabledModels,
  providerId,
  reorderable = true,
  refreshBusy,
  onToggleOfficialModel,
  onToggle,
  onUpdate,
}: {
  contextById?: Map<string, number>;
  disabled?: boolean;
  discoverBusy?: boolean;
  discoverDisabled?: boolean;
  discoverError?: string | null;
  headerControl?: React.ReactNode;
  interactionDisabled?: boolean;
  models: Model[];
  modelTestDisabled?: boolean;
  onAdd?: () => string | undefined;
  onCancelNewModel?: (modelId: string) => void;
  onDiscover?: () => void;
  onRefresh?: () => void;
  onRemove?: (modelId: string) => void;
  onReorder: (models: Model[]) => void;
  onTestModel?: (model: Model) => Promise<boolean>;
  officialDisabledModels?: string[];
  providerId?: string;
  reorderable?: boolean;
  refreshBusy?: boolean;
  onToggleOfficialModel?: (modelId: string, enabled: boolean) => void;
  onToggle?: (modelId: string, enabled: boolean) => void;
  onUpdate?: (modelId: string, patch: Partial<Model>) => void;
}) {
  const { t } = useTranslation();
  const [editingModelId, setEditingModelId] = useState<string | null>(null);
  const [pendingNewModelId, setPendingNewModelId] = useState<string | null>(null);
  const [modelTestStates, setModelTestStates] = useState<Record<string, InlineTestState>>({});
  const [testingModelId, setTestingModelId] = useState<string | null>(null);
  const editingModel = editingModelId ? models.find((model) => model.id === editingModelId) ?? null : null;
  const editingModelIsNew = pendingNewModelId !== null && pendingNewModelId === editingModelId;
  const [modelListRef, modelListHasOverflow] = useVerticalOverflow<HTMLDivElement>([
    disabled,
    editingModelId,
    models.length,
    providerId,
    reorderable,
    interactionDisabled,
  ]);

  function addAndEdit() {
    const modelId = onAdd?.();
    if (modelId) {
      setPendingNewModelId(modelId);
      setEditingModelId(modelId);
    }
  }

  function applyModelUpdate(modelId: string, nextModel: Model) {
    onUpdate?.(modelId, nextModel);
    if (pendingNewModelId === modelId) {
      setPendingNewModelId(null);
    }
    setEditingModelId(null);
  }

  function closeModelEditor() {
    if (editingModelIsNew && pendingNewModelId) {
      onCancelNewModel?.(pendingNewModelId);
      setPendingNewModelId(null);
    }
    setEditingModelId(null);
  }

  async function runModelTest(model: Model) {
    if (!onTestModel || testingModelId) {
      return;
    }
    setTestingModelId(model.id);
    setModelTestStates((current) => ({ ...current, [model.id]: "testing" }));
    const ok = await onTestModel(model);
    setModelTestStates((current) => ({ ...current, [model.id]: ok ? "success" : "error" }));
    setTestingModelId(null);
  }

  function renderModelRow(model: Model) {
    const contextWindow = resolveOfficialModelContextWindow(
      model.context_window,
      contextById?.get(model.id),
    );
    const modelEnabled = disabled
      ? !isOfficialModelDisabled(officialDisabledModels ?? [], model.id)
      : model.enabled;
    const rowInteractable = !interactionDisabled && !disabled;
    function activateModelRow() {
      if (interactionDisabled) {
        return;
      }
      setEditingModelId(model.id);
    }
    const actions = (
      <div className="flex shrink-0 flex-nowrap items-center justify-end gap-2 whitespace-nowrap text-xs text-slate-500">
        {modelCapabilityTags(model).map((tag) => (
          <ModelCapabilityChip key={tag} tag={tag} />
        ))}
        <CapabilityChip
          label={formatContextWindow(contextWindow)}
          title={modelLimitDetails(model, contextWindow)}
        />
        {disabled && onToggleOfficialModel && (
          <SwitchControl
            checked={modelEnabled}
            label={modelEnabled ? t("providers.modelEnabled") : t("providers.modelDisabled")}
            disabled={interactionDisabled}
            showLabel={false}
            onChange={(checked) => onToggleOfficialModel(model.id, checked)}
          />
        )}
        {!disabled && onToggle && (
          <SwitchControl
            checked={modelEnabled}
            label={modelEnabled ? t("providers.modelEnabled") : t("providers.modelDisabled")}
            showLabel={false}
            onChange={(checked) => onToggle(model.id, checked)}
          />
        )}
      </div>
    );
    return (
      <div
        className={cx(
          "grid min-h-[52px] grid-cols-[minmax(0,1fr)_auto] items-center gap-3 px-3 py-2",
          rowInteractable && "cursor-pointer",
          !modelEnabled && "opacity-70",
        )}
        role={rowInteractable ? "button" : undefined}
        tabIndex={rowInteractable ? 0 : undefined}
        onClick={rowInteractable ? activateModelRow : undefined}
        onKeyDown={
          rowInteractable
            ? (event) => {
                if (event.target !== event.currentTarget) {
                  return;
                }
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  activateModelRow();
                }
              }
            : undefined
        }
      >
        <ModelIdentity
          model={model}
          actionsDisabled={interactionDisabled}
          providerId={providerId}
          testDisabled={interactionDisabled || modelTestDisabled || Boolean(testingModelId)}
          testState={modelTestStates[model.id] ?? "idle"}
          onTest={onTestModel ? () => void runModelTest(model) : undefined}
        />
        <div
          onClick={(event) => event.stopPropagation()}
          onKeyDown={(event) => event.stopPropagation()}
        >
          {actions}
        </div>
      </div>
    );
  }

  return (
    <div
      className={cx(
        "grid min-h-0 grid-rows-[auto_minmax(0,1fr)] gap-3 p-5",
        interactionDisabled && "text-slate-400",
      )}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold">{t("common.models")}</h3>
          <p className="mt-1 text-xs text-slate-500">{t("providers.configured", { count: models.length })}</p>
          <p className="mt-1 truncate whitespace-nowrap text-xs leading-4 text-slate-500">
            {t("providers.appsMaySortModels")}
          </p>
        </div>
        <div className="flex shrink-0 items-center justify-end gap-2 whitespace-nowrap">
          {headerControl}
          {discoverError && (
            <span className="max-w-[260px] truncate text-xs font-medium text-danger" title={discoverError}>
              {discoverError}
            </span>
          )}
          {onRefresh && (
            <button
              type="button"
              className={cx(
                "focus-ring inline-flex shrink-0 items-center justify-center gap-2 border border-line bg-panel px-3 font-semibold hover:bg-slate-100 disabled:bg-slate-100",
                headerControl ? "h-7 rounded-full text-xs" : "h-9 rounded-md text-sm",
              )}
              disabled={interactionDisabled || refreshBusy}
              onClick={onRefresh}
            >
              <RefreshCcw size={16} />
              {t("common.refresh")}
            </button>
          )}
          {onDiscover && (
            <button
              type="button"
              className="focus-ring inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-md border border-line bg-panel px-3 text-sm font-semibold hover:bg-slate-100 disabled:bg-slate-100"
              disabled={discoverBusy || discoverDisabled}
              onClick={onDiscover}
            >
              <RefreshCcw size={16} />
              {t("providers.discoverModels")}
            </button>
          )}
          {!disabled && (
            <button
              type="button"
              className="focus-ring inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-md border border-line bg-panel px-3 text-sm font-semibold hover:bg-slate-100"
              onClick={addAndEdit}
            >
              <Plus size={16} />
              {t("providers.addModel")}
            </button>
          )}
        </div>
      </div>
      <div
        ref={modelListRef}
        className={cx(
          "min-h-0 overflow-auto",
          interactionDisabled && "opacity-60 grayscale",
          modelListHasOverflow && "-mr-5 pr-1",
        )}
      >
        {models.length === 0 ? (
          <div className="rounded-inner bg-panel-soft p-4 text-sm text-slate-500 shadow-hairline">
            {t("common.noModels")}
          </div>
        ) : reorderable ? (
          <SortableList
            className="space-y-2"
            items={models}
            getId={(model) => model.id}
            onReorder={onReorder}
            renderItem={renderModelRow}
          />
        ) : (
          <div className="space-y-2">
            {models.map((model) => (
              <div key={model.id} className="rounded-md border border-line bg-white shadow-subtle">
                {renderModelRow(model)}
              </div>
            ))}
          </div>
        )}
      </div>
      {!disabled && editingModel && (
        <ModelEditorOverlay
          model={editingModel}
          onApply={(nextModel) => applyModelUpdate(editingModel.id, nextModel)}
          onClose={closeModelEditor}
          onRemove={onRemove ? () => {
            if (pendingNewModelId === editingModel.id) {
              setPendingNewModelId(null);
            }
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

function ModelIdentity({
  actionsDisabled = false,
  model,
  onTest,
  providerId,
  testDisabled,
  testState = "idle",
}: {
  actionsDisabled?: boolean;
  model: Model;
  onTest?: () => void;
  providerId?: string;
  testDisabled?: boolean;
  testState?: InlineTestState;
}) {
  const { t } = useTranslation();
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

  function testCurrentModel(event: React.MouseEvent<HTMLButtonElement>) {
    event.stopPropagation();
    onTest?.();
  }

  return (
    <div className="min-w-0">
      <span className="block truncate text-sm font-medium">{displayModel(model)}</span>
      <span className="mt-0.5 flex min-w-0 items-center gap-1 text-xs text-slate-500">
        <span className="min-w-0 truncate font-mono">{model.id}</span>
        <button
          type="button"
          className="focus-ring inline-flex h-6 w-6 shrink-0 items-center justify-center rounded border border-transparent text-slate-500 transition-[background-color,border-color,color] duration-150 ease-out hover:border-line hover:bg-panel hover:text-ink"
          disabled={actionsDisabled}
          onClick={copyModelId}
          title={copied ? t("common.copied") : t("providers.copyModelIdTitle", { id: copyValue })}
          aria-label={copied ? t("providers.copiedModelId", { id: copyValue }) : t("providers.copyModelId", { id: copyValue })}
        >
          {copied ? <Check size={12} /> : <Copy size={12} />}
        </button>
        {onTest && (
          <button
            type="button"
            className={cx(
              "focus-ring inline-flex h-6 w-6 shrink-0 items-center justify-center rounded border text-slate-500 transition-[background-color,border-color,color,transform] duration-150 ease-out active:scale-[0.96]",
              testState === "success"
                ? "status-pop border-emerald-200 bg-emerald-50 text-emerald-700"
                : testState === "error"
                  ? "status-pop border-red-200 bg-red-50 text-danger"
                  : "border-transparent text-slate-500 hover:border-line hover:bg-panel hover:text-ink",
            )}
            disabled={testDisabled || testState === "testing"}
            onClick={testCurrentModel}
            title={t("providers.testModelTitle", { id: copyValue })}
            aria-label={t("providers.testModelTitle", { id: copyValue })}
          >
            <ModelTestStateIcon state={testState} size={13} />
          </button>
        )}
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
  const { t } = useTranslation();
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
            <h3 className="truncate text-base font-semibold">{t("providers.modelSettings")}</h3>
            <p className="mt-1 truncate text-xs text-slate-500">{model.id}</p>
          </div>
          <button
            type="button"
            className="focus-ring grid h-8 w-8 place-items-center rounded-md border border-line bg-panel hover:bg-slate-100"
            onClick={onClose}
            aria-label={t("providers.closeModelSettings")}
          >
            <X size={16} />
          </button>
        </div>

        <div className="grid gap-4 p-5">
          <section className="grid gap-3 rounded-md border border-line bg-panel p-3">
            <div>
              <h4 className="text-sm font-semibold">{t("providers.identity")}</h4>
              <p className="mt-0.5 text-xs text-slate-500">{t("providers.gatewayFacingModelName")}</p>
            </div>
            <Field label={t("common.modelId")}>
              <input
                className="field h-9"
                value={draft.id}
                onChange={(event) => setDraft({ ...draft, id: event.target.value })}
              />
            </Field>
            <Field label={t("common.displayName")}>
              <input
                className="field h-9"
                value={draft.display_name ?? ""}
                onChange={(event) => setDraft({ ...draft, display_name: event.target.value || null })}
              />
            </Field>
            <Field label={t("providers.context")}>
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
              <div className="text-sm font-semibold">{t("providers.capabilities")}</div>
              <div className="mt-0.5 text-xs text-slate-500">{t("providers.gatewayFacingMetadata")}</div>
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              <label className="flex h-9 items-center justify-between rounded-md border border-line bg-white px-3 text-sm font-medium">
                <span className="inline-flex items-center gap-2">
                  <Eye size={15} />
                  {t("providers.vision")}
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
                  {t("providers.thinking")}
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
                  <span className="text-xs font-semibold uppercase text-slate-500">{t("providers.reasoningLevels")}</span>
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
                <Field label={t("common.defaultReasoning")}>
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
              {t("providers.removeModel")}
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
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="focus-ring inline-flex h-9 items-center justify-center rounded-md bg-action px-3 text-sm font-semibold text-white"
              onClick={() => onApply(normalizeModel(draft))}
            >
              {t("common.apply")}
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

function CapabilityChip({ icon, label, title }: { icon?: React.ReactNode; label: string; title?: string }) {
  return (
    <span title={title} className="inline-flex h-6 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border border-line bg-panel px-2 text-xs font-semibold text-slate-600">
      {icon}
      {label}
    </span>
  );
}

function ModelCapabilityChip({ tag }: { tag: "vision" | "thinking" }) {
  const { t } = useTranslation();
  if (tag === "vision") {
    return <CapabilityChip icon={<Eye size={13} />} label={t("providers.vision")} />;
  }
  return <CapabilityChip icon={<Brain size={13} />} label={t("providers.thinking")} />;
}

export function SwitchControl({
  ariaDescribedBy,
  checked,
  className,
  disabled = false,
  label,
  onChange,
  showLabel = true,
}: {
  ariaDescribedBy?: string;
  checked: boolean;
  className?: string;
  disabled?: boolean;
  label: string;
  onChange: (checked: boolean) => void;
  showLabel?: boolean;
}) {
  return (
    <label
      className={cx(
        "inline-flex h-6 shrink-0 items-center gap-2 whitespace-nowrap text-xs font-semibold text-slate-600",
        showLabel && "rounded-full border border-line bg-panel pl-2 pr-1",
        className,
      )}
      aria-describedby={ariaDescribedBy}
    >
      <span className={showLabel ? "truncate" : "sr-only"}>{label}</span>
      <span className="relative inline-flex h-5 w-9 shrink-0 items-center">
        <input
          type="checkbox"
          className="peer sr-only"
          checked={checked}
          disabled={disabled}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span
          className={cx(
            "absolute inset-0 rounded-full border transition-colors",
            disabled
              ? "border-slate-200 bg-slate-200"
              : "border-line bg-slate-200 peer-checked:border-action peer-checked:bg-action",
          )}
        />
        <span
          className={cx(
            "absolute left-0.5 h-4 w-4 rounded-full shadow-sm transition-transform",
            checked && "translate-x-4",
            disabled ? "bg-slate-100" : "bg-white",
          )}
        />
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

export function isOfficialModelDisabled(disabledModels: string[], modelId: string) {
  return disabledModels.some((item) => modelIdMatches(item, modelId));
}

export function modelIdMatches(left: string, right: string) {
  return normalizeOfficialModelId(left) === normalizeOfficialModelId(right);
}


function formatContextWindow(value?: number | null) {
  if (!value) {
    return i18n.t("providers.contextDynamic");
  }
  if (value >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(1)}M`;
  }
  if (value >= 1000) {
    const rounded = Math.round(value / 1000);
    return `${new Intl.NumberFormat(i18n.language || "en-US").format(rounded)}K`;
  }
  return new Intl.NumberFormat(i18n.language || "en-US").format(value);
}

function modelLimitDetails(model: Model, effective?: number | null) {
  const effectiveLabel = formatContextWindow(effective);
  const maxLabel = model.max_context_window
    ? formatContextWindow(model.max_context_window)
    : i18n.t("common.unknown");
  const verified = model.verified_at ?? i18n.t("common.unknown");
  return i18n.t("providers.contextDetails", {
    effective: effectiveLabel,
    max: maxLabel,
    source: model.effective_source ?? model.max_source ?? i18n.t("common.unknown"),
    verified,
  });
}

function hasVision(model: Model) {
  return (model.input_modalities ?? ["text"]).includes("image");
}

function toggleReasoningLevel(current: string[], level: string, checked: boolean) {
  const next = checked ? [...new Set([...current, level])] : current.filter((item) => item !== level);
  return reasoningLevelOptions.filter((item) => next.includes(item));
}

function ModelTestStateIcon({ size, state }: { size: number; state: InlineTestState }) {
  if (state === "testing") {
    return <RefreshCcw size={size} className="shrink-0 animate-spin" />;
  }
  if (state === "success") {
    return <Check size={size} className="shrink-0" />;
  }
  if (state === "error") {
    return <X size={size} className="shrink-0" />;
  }
  return <Cable size={size} className="shrink-0" />;
}


export function uniqueModelId(models: Model[]) {
  const existing = new Set(models.map((model) => model.id));
  let index = models.length + 1;
  let id = `new-model-${index}`;
  while (existing.has(id)) {
    index += 1;
    id = `new-model-${index}`;
  }
  return id;
}

export function createDraftModel(id: string, sortOrder: number): Model {
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
