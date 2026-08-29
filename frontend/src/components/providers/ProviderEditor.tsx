import { Check, ChevronDown, FlaskConical, LogOut, Plus, RefreshCcw, Save, Trash2, X } from "lucide-react";
import { OfficialOpenAIUsageLimitBars } from "./OfficialOpenAIUsagePanel";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useToasts } from "../PageToast";
import { ModelSection, createDraftModel, uniqueModelId } from "./ProviderModelSection";
import { ApiKeyInput, Field, HeaderRow, IconButton } from "./ProviderFormControls";
import { XaiLoginCard } from "./XaiLoginCard";
import {
  applyAddProviderProbeResult,
  applyProviderProbeResult,
  hasAvailableEndpointFormats,
  mergeEndpointFormats,
  modelProbeId,
  normalizedEndpointFormat,
  normalizeEndpointFormats,
  normalizeProviderEndpointSelection,
  probeSucceeded,
  probeAvailableFormats,
  toolProtocolLabel,
  upstreamFormatLabel,
} from "../../lib/providerEndpoint";
import {
  applyCatalogPresetDefaults,
  bundledPresetFor,
  subscriptionAuthAdapter,
} from "../../lib/providerCatalog";
import { endpointSelectionOptions, type AddProviderForm, type InlineTestState } from "../../lib/providerForm";
import { normalizeModel } from "../../lib/providerModel";
import { isProviderDirty } from "../../lib/providerComparison";
import { cx, displayModel, renumberModels, slugify } from "../../lib/format";
import { api, messageFromError } from "../../lib/tauri";
import type {
  Model,
  OpenAIUsageLimit,
  Provider,
  ToolProtocol,
  UpstreamFormat,
  UpstreamFormatProbeResult,
} from "../../lib/types";
import type { ProviderDraftState } from "../../hooks/useProviderNavigationGuard";

type Translate = (key: string, options?: Record<string, unknown>) => string;


export function ProviderDetail({
  busy,
  discoverError,
  onChange,
  onDelete,
  onDraftStateChange,
  onProbe,
  onRefresh,
  probeResult,
  provider,
  unsaved = false,
}: {
  busy: string | null;
  discoverError?: string | null;
  onChange: (provider: Provider, successMessage?: string) => void;
  onDelete: () => void;
  onDraftStateChange: (state: ProviderDraftState<Provider>) => void;
  onProbe: (provider: Provider) => Promise<UpstreamFormatProbeResult | null>;
  onRefresh: (provider: Provider) => void;
  probeResult: UpstreamFormatProbeResult | null;
  provider: Provider;
  unsaved?: boolean;
}) {
  const { t } = useTranslation();
  const { showToast, updateToast } = useToasts();
  const normalizedProvider = useMemo<Provider>(() => {
    const normalized = normalizeProviderEndpointSelection(provider);
    return { ...normalized, upstream_format: provider.upstream_format };
  }, [provider]);
  const [draft, setDraft] = useState(() => normalizedProvider);
  const [endpointTestState, setEndpointTestState] = useState<InlineTestState>("idle");
  const [bundledPresets, setBundledPresets] = useState<Provider[]>([]);
  const [usageLimits, setUsageLimits] = useState<OpenAIUsageLimit[]>([]);
  const [usageBusy, setUsageBusy] = useState(false);
  const [signedIn, setSignedIn] = useState<boolean | null>(null);
  const dirty = unsaved || isProviderDirty(normalizedProvider, draft);
  const draftRef = useRef(draft);
  const ensuringXaiCatalog = useRef(false);
  draftRef.current = draft;
  const preset = bundledPresetFor(provider.id, bundledPresets);
  const subscriptionAuth = subscriptionAuthAdapter(provider) ?? subscriptionAuthAdapter(preset);
  const xaiSubscriptionAuth = subscriptionAuth === "xai_oauth";

  useEffect(() => {
    setDraft(normalizedProvider);
    setEndpointTestState(hasAvailableEndpointFormats(normalizedProvider.available_upstream_formats) ? "success" : "idle");
  }, [provider.id]);

  useEffect(() => {
    let cancelled = false;
    void api.getBundledProviders().then((presets) => {
      if (!cancelled) {
        setBundledPresets(presets);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!preset) {
      return;
    }
    setDraft((current) =>
      applyCatalogPresetDefaults(current, preset, { includeModels: current.models.length === 0 }),
    );
  }, [preset, provider.id]);

  useEffect(() => {
    const availableFormats = normalizeEndpointFormats(provider.available_upstream_formats);
    setDraft((current) =>
      current.id === provider.id
        ? {
            ...current,
            available_upstream_formats: availableFormats,
          }
        : current,
    );
    setEndpointTestState(availableFormats.length ? "success" : "idle");
  }, [provider.id, provider.available_upstream_formats]);

  // Keep the editor model list in sync with the persisted provider so that
  // successful discovery is reflected immediately and a stale draft cannot be
  // saved back over the discovered models.
  useEffect(() => {
    setDraft((current) => {
      if (current.id !== normalizedProvider.id || current.models === normalizedProvider.models) {
        return current;
      }
      return { ...current, models: normalizedProvider.models };
    });
  }, [normalizedProvider.id, normalizedProvider.models]);

  useEffect(() => {
    if (probeResult) {
      setEndpointTestState(probeSucceeded(probeResult) ? "success" : "error");
    }
  }, [probeResult]);

  useEffect(() => {
    if (!xaiSubscriptionAuth) {
      setSignedIn(null);
      setUsageLimits([]);
      setUsageBusy(false);
      return;
    }
    let cancelled = false;
    setUsageBusy(true);
    void (async () => {
      try {
        const status = await api.xaiAuthStatus();
        if (cancelled) {
          return;
        }
        const ok = status.signed_in === true;
        setSignedIn(ok);
        if (!ok) {
          setUsageLimits([]);
          return;
        }
        try {
          const snapshot = await api.xaiUsageSnapshot();
          if (!cancelled) {
            setUsageLimits(snapshot.limits ?? []);
          }
        } catch {
          if (!cancelled) {
            setUsageLimits([]);
          }
        }
        void ensureXaiCatalogReady();
      } catch {
        if (!cancelled) {
          setSignedIn(false);
          setUsageLimits([]);
        }
      } finally {
        if (!cancelled) {
          setUsageBusy(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [provider.id, xaiSubscriptionAuth]);

  useEffect(() => {
    onDraftStateChange({ providerId: provider.id, draft, dirty });
    return () => {
      onDraftStateChange({ providerId: provider.id, draft: normalizedProvider, dirty: false });
    };
  }, [dirty, draft, normalizedProvider, onDraftStateChange, provider.id]);

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
    if (!unsaved) {
      onChange(next, t("providers.modelRemoved"));
    }
  }

  async function ensureXaiCatalogReady() {
    if (!xaiSubscriptionAuth || ensuringXaiCatalog.current) {
      return;
    }
    ensuringXaiCatalog.current = true;
    try {
      let current = draftRef.current;
      if (!current.base_url.trim()) {
        const bundled = await api.getBundledProviders();
        const next = applyCatalogPresetDefaults(current, bundledPresetFor(current.id, bundled), {
          includeModels: false,
        });
        if (next !== current) {
          current = next;
          setDraft(next);
        }
      }
      const catalogOnly =
        current.models.length === 0 || current.models.every((model) => model.id === "grok-4");
      if (catalogOnly && current.base_url.trim()) {
        onRefresh(current);
      } else if (current !== draftRef.current) {
        if (unsaved) {
          setDraft(current);
        } else {
          await onChange(current, t("providers.xaiCatalogReady"));
        }
      }
    } catch {
      return;
    } finally {
      ensuringXaiCatalog.current = false;
    }
  }

  async function refreshXaiUsage() {
    setUsageBusy(true);
    try {
      const snapshot = await api.xaiUsageSnapshot();
      setUsageLimits(snapshot.limits ?? []);
      setSignedIn(true);
    } catch {
      setUsageLimits([]);
    } finally {
      setUsageBusy(false);
    }
  }

  async function signOutXai() {
    const toastId = showToast(t("providers.xaiSigningOut"), "loading");
    setUsageBusy(true);
    try {
      await api.xaiLogout();
      setSignedIn(false);
      setUsageLimits([]);
      updateToast(toastId, {
        action: null,
        text: t("providers.xaiSignedOut"),
        tone: "success",
      });
    } catch (err) {
      updateToast(toastId, {
        action: null,
        text: messageFromError(err),
        tone: "error",
      });
    } finally {
      setUsageBusy(false);
    }
  }

  async function runProbe() {
    setEndpointTestState("testing");
    const result = await onProbe(draft);
    if (result) {
      setDraft((current) => applyProviderProbeResult(current, result));
    }
    setEndpointTestState(result && probeSucceeded(result) ? "success" : "error");
  }

  async function testModel(model: Model) {
    const label = displayModel(model);
    const upstreamFormat = normalizedEndpointFormat(draft.upstream_format);
    const endpointLabel = upstreamFormatLabel(upstreamFormat, t as Translate);
    const toastId = showToast(t("providers.testingModel", { label, endpoint: endpointLabel }), "loading");
    try {
      const result = await api.testModelEndpoint(
        draft.base_url,
        draft.api_key ?? "",
        modelProbeId(model),
        upstreamFormat,
      );
      updateToast(toastId, {
        action: null,
        text: t("gateway.connectedHttp", { label, endpoint: endpointLabel, status: result.status }),
        tone: "success",
      });
      return true;
    } catch (err) {
      updateToast(toastId, {
        action: null,
        text: t("gateway.connectionFailed", { label, endpoint: endpointLabel, message: messageFromError(err) }),
        tone: "error",
      });
      return false;
    }
  }

  return (
    <div className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)_auto]">
      <div className="grid gap-2 border-b border-line p-4">
        <HeaderRow
          title={provider.name}
          titleAccessory={
            xaiSubscriptionAuth ? <SubscriptionAuthChip signedIn={signedIn} /> : null
          }
          actions={
            <>
              {xaiSubscriptionAuth && signedIn ? (
                <OfficialOpenAIUsageLimitBars busy={usageBusy} limits={usageLimits} />
              ) : null}
              {xaiSubscriptionAuth && signedIn ? (
                <IconButton
                  title={t("providers.xaiRefreshUsage")}
                  disabled={usageBusy}
                  onClick={() => void refreshXaiUsage()}
                >
                  <RefreshCcw size={16} className={usageBusy ? "animate-spin" : undefined} />
                </IconButton>
              ) : null}
              <IconButton
                title={t("providers.deleteProvider")}
                danger
                disabled={busy === "save"}
                onClick={onDelete}
              >
                <Trash2 size={16} />
              </IconButton>
            </>
          }
        />

        {xaiSubscriptionAuth && signedIn === false ? (
          <XaiLoginCard
            onAuthChange={setSignedIn}
            onSignedIn={() => void ensureXaiCatalogReady()}
            onUsage={(limits) => {
              setUsageLimits(limits);
              setUsageBusy(false);
            }}
          />
        ) : null}
        {xaiSubscriptionAuth && signedIn ? (
          <div className="flex min-w-0 items-center justify-between gap-2 rounded-inner bg-panel px-3 py-2 text-xs leading-5 text-slate-600 shadow-hairline">
            <p className="min-w-0 truncate">{t("providers.xaiSignedInBody")}</p>
            <button
              type="button"
              className="focus-ring inline-flex h-8 shrink-0 items-center gap-1.5 rounded-control bg-surface px-2.5 text-xs font-semibold text-slate-700 shadow-control hover:bg-white disabled:text-slate-300"
              disabled={usageBusy}
              onClick={() => void signOutXai()}
            >
              <LogOut size={14} />
              {t("providers.xaiSignOut")}
            </button>
          </div>
        ) : null}

        {xaiSubscriptionAuth ? null : (
        <div className="grid grid-cols-2 gap-2">
          <Field label={t("common.name")}>
            <input
              className="field field-compact"
              value={draft.name}
              onChange={(event) => setDraft({ ...draft, name: event.target.value })}
            />
          </Field>
          <Field label={t("common.apiKey")}>
            <ApiKeyInput
              value={draft.api_key ?? ""}
              onChange={(apiKey) => setDraft({ ...draft, api_key: apiKey || null })}
            />
          </Field>
          <Field label={t("common.baseUrl")} className="col-span-2">
            <input
              className="field field-compact"
              value={draft.base_url}
              onChange={(event) => setDraft({ ...draft, base_url: event.target.value })}
            />
          </Field>
          <div className="col-span-2">
            <EndpointSelectionPanel
              value={draft.upstream_format ?? "auto"}
              result={probeResult}
              availableFormats={draft.available_upstream_formats}
              toolProtocol={draft.tool_protocol}
              probeDisabled={busy === "probe" || !draft.base_url.trim()}
              testState={endpointTestState}
              onChange={(upstreamFormat) => setDraft({ ...draft, upstream_format: upstreamFormat })}
              onProbe={() => void runProbe()}
            />
          </div>
        </div>
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
        onTestModel={testModel}
        onToggle={(modelId, enabled) => updateModel(modelId, { enabled })}
        onUpdate={updateModel}
        onCancelNewModel={(modelId) =>
          setDraft((current) => ({
            ...current,
            models: renumberModels(current.models.filter((model) => model.id !== modelId)),
          }))
        }
        modelTestDisabled={!draft.base_url.trim()}
      />
      <div className="flex items-center justify-end border-t border-line px-5 py-3">
        <button
          type="button"
          className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md bg-action px-3 text-sm font-semibold text-white disabled:bg-slate-300"
          disabled={!dirty || busy === "save"}
          onClick={() =>
            onChange(
              draft,
              unsaved
                ? t("providers.providerAdded", { name: draft.name })
                : t("providers.providerSaved", { name: draft.name }),
            )
          }
        >
          <Save size={16} />
          {t("common.save")}
        </button>
      </div>
    </div>
  );
}


export function AddProviderPanel({
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
  form: AddProviderForm;
  onAdd: () => void;
  onDiscover: () => void;
  onFormChange: (form: AddProviderForm) => void;
  onProbe: () => Promise<UpstreamFormatProbeResult | null>;
  probeResult: UpstreamFormatProbeResult | null;
}) {
  const { t } = useTranslation();
  const { showToast, updateToast } = useToasts();
  const [endpointTestState, setEndpointTestState] = useState<InlineTestState>("idle");

  useEffect(() => {
    if (probeResult) {
      setEndpointTestState(probeSucceeded(probeResult) ? "success" : "error");
    }
  }, [probeResult]);

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

  async function runProbe() {
    setEndpointTestState("testing");
    const result = await onProbe();
    if (result) {
      onFormChange(applyAddProviderProbeResult(form, result));
    }
    setEndpointTestState(result && probeSucceeded(result) ? "success" : "error");
  }

  async function testModel(model: Model) {
    const label = displayModel(model);
    const upstreamFormat = normalizedEndpointFormat(form.upstream_format);
    const endpointLabel = upstreamFormatLabel(upstreamFormat, t as Translate);
    const toastId = showToast(t("providers.testingModel", { label, endpoint: endpointLabel }), "loading");
    try {
      const result = await api.testModelEndpoint(
        form.base_url,
        form.api_key,
        modelProbeId(model),
        upstreamFormat,
      );
      updateToast(toastId, {
        action: null,
        text: t("gateway.connectedHttp", { label, endpoint: endpointLabel, status: result.status }),
        tone: "success",
      });
      return true;
    } catch (err) {
      updateToast(toastId, {
        action: null,
        text: t("gateway.connectionFailed", { label, endpoint: endpointLabel, message: messageFromError(err) }),
        tone: "error",
      });
      return false;
    }
  }

  return (
    <div className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)_auto]">
      <div className="grid gap-2 border-b border-line p-4">
        <HeaderRow title={t("providers.addProvider")} />
        <div className="grid grid-cols-2 gap-2">
          <Field label={t("common.name")}>
            <input
              className="field field-compact"
              value={form.name}
              onChange={(event) => onFormChange({ ...form, name: event.target.value })}
            />
          </Field>
          <Field label={t("common.apiKey")}>
            <ApiKeyInput
              value={form.api_key}
              onChange={(apiKey) => onFormChange({ ...form, api_key: apiKey })}
            />
          </Field>
          <Field label={t("common.baseUrl")} className="col-span-2">
            <input
              className="field field-compact"
              value={form.base_url}
              onChange={(event) => onFormChange({ ...form, base_url: event.target.value })}
            />
          </Field>
          <div className="col-span-2">
            <EndpointSelectionPanel
              value={form.upstream_format}
              result={probeResult}
              availableFormats={form.available_upstream_formats}
              toolProtocol={form.tool_protocol}
              probeDisabled={busy === "probe" || !form.base_url.trim()}
              testState={endpointTestState}
              onChange={(upstreamFormat) => onFormChange({ ...form, upstream_format: upstreamFormat })}
              onProbe={() => void runProbe()}
            />
          </div>
        </div>
      </div>

      <ModelSection
        discoverDisabled={!form.base_url.trim()}
        discoverBusy={busy === "discover"}
        discoverError={discoverError}
        models={form.models}
        providerId={form.id.trim() || slugify(form.name)}
        onAdd={addModel}
        onDiscover={onDiscover}
        onReorder={(models) => onFormChange({ ...form, models: renumberModels(models) })}
        onRemove={(modelId) =>
          onFormChange({ ...form, models: form.models.filter((model) => model.id !== modelId) })
        }
        onTestModel={testModel}
        onToggle={(modelId, enabled) => updateModel(modelId, { enabled })}
        onUpdate={updateModel}
        onCancelNewModel={(modelId) =>
          onFormChange({
            ...form,
            models: renumberModels(form.models.filter((model) => model.id !== modelId)),
          })
        }
        modelTestDisabled={!form.base_url.trim()}
      />

      <div className="flex items-center justify-end border-t border-line px-5 py-3">
        <button
          type="button"
          className="focus-ring inline-flex h-9 items-center gap-2 rounded-md bg-action px-3 text-sm font-semibold text-white disabled:bg-slate-300"
          disabled={!canAdd || Boolean(busy)}
          onClick={onAdd}
        >
          <Plus size={16} />
          {t("providers.addProvider")}
        </button>
      </div>
    </div>
  );
}


function SubscriptionAuthChip({ signedIn }: { signedIn: boolean | null }) {
  const { t } = useTranslation();
  const tone = signedIn === true ? "ok" : signedIn === false ? "pending" : "muted";
  const label =
    signedIn === true
      ? t("providers.authorized")
      : signedIn === false
        ? t("providers.authMissing")
        : t("providers.authUnknown");
  return (
    <span
      className={cx(
        "inline-flex h-6 max-w-[112px] items-center rounded-full border px-2 text-[11px] font-semibold leading-none",
        tone === "ok" && "border-emerald-200 bg-emerald-50 text-emerald-700",
        tone === "pending" && "border-amber-200 bg-amber-50 text-amber-700",
        tone === "muted" && "border-slate-200 bg-white text-slate-500",
      )}
    >
      <span className="truncate whitespace-nowrap">{label}</span>
    </span>
  );
}

function EndpointSelectionPanel({
  availableFormats,
  onChange,
  onProbe,
  probeDisabled,
  result,
  testState,
  toolProtocol,
  value,
}: {
  availableFormats?: UpstreamFormat[] | null;
  onChange: (value: UpstreamFormat) => void;
  onProbe: () => void;
  probeDisabled: boolean;
  result?: UpstreamFormatProbeResult | null;
  testState: InlineTestState;
  toolProtocol?: ToolProtocol | null;
  value?: UpstreamFormat | null;
}) {
  const { t } = useTranslation();
  const selected = normalizedEndpointFormat(value);
  const mergedAvailableFormats = mergeEndpointFormats(availableFormats, probeAvailableFormats(result));

  return (
    <div className="grid min-w-0 gap-1 text-sm font-medium text-slate-700">
      <div className="flex min-w-0 items-center justify-between gap-2">
        <span>{t("common.endpointSelection")}</span>
        <span className="truncate text-xs font-medium text-slate-500">{toolProtocolLabel(toolProtocol)}</span>
      </div>
      <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2">
        <EndpointFormatSelect availableFormats={mergedAvailableFormats} value={selected} onChange={onChange} />
        <button
          type="button"
          className={cx(
            "mini-button inline-flex h-9 shrink-0 items-center justify-center gap-2 px-3 text-sm font-semibold disabled:bg-slate-100",
            testState === "success" && "status-pop border-emerald-200 bg-emerald-50 text-emerald-700",
            testState === "error" && "status-pop border-red-200 bg-red-50 text-danger",
          )}
          disabled={probeDisabled || testState === "testing"}
          onClick={onProbe}
        >
          <TestStateIcon state={testState} size={16} />
          {t("common.test")}
        </button>
      </div>
    </div>
  );
}

function EndpointFormatSelect({
  availableFormats,
  onChange,
  value,
}: {
  availableFormats: UpstreamFormat[];
  onChange: (value: UpstreamFormat) => void;
  value: UpstreamFormat;
}) {
  const [open, setOpen] = useState(false);
  const selected = endpointSelectionOptions.find((option) => option.value === value) ?? endpointSelectionOptions[0];
  const available = new Set(availableFormats);
  const selectedAvailable = available.has(selected.value);
  const { t } = useTranslation();
  const tr = t as Translate;

  return (
    <div
      className="relative min-w-0"
      onBlur={(event) => {
        const nextTarget = event.relatedTarget as Node | null;
        if (!nextTarget || !event.currentTarget.contains(nextTarget)) {
          setOpen(false);
        }
      }}
    >
      <button
        type="button"
        className="select-trigger h-9 w-full"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate">{upstreamFormatLabel(selected.value, tr)}</span>
          {selectedAvailable && <EndpointAvailableChip />}
        </span>
        <ChevronDown size={15} className="shrink-0 text-slate-500" />
      </button>
      {open && (
        <div className="select-popover absolute left-0 top-[calc(100%+6px)] z-30 w-full min-w-[240px]" role="listbox">
          {endpointSelectionOptions.map((option) => {
            const selectedOption = option.value === value;
            const optionAvailable = available.has(option.value);
            return (
              <button
                key={option.value}
                type="button"
                className="select-option"
                role="option"
                aria-selected={selectedOption}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  onChange(option.value);
                  setOpen(false);
                }}
              >
                <span className="truncate">{upstreamFormatLabel(option.value, tr)}</span>
                <span className="flex shrink-0 items-center gap-2">
                  {optionAvailable && <EndpointAvailableChip />}
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function EndpointAvailableChip() {
  const { t } = useTranslation();
  return (
    <span className="shrink-0 rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[11px] font-semibold leading-4 text-emerald-700">
      {t("common.available")}
    </span>
  );
}

function TestStateIcon({ size, state }: { size: number; state: InlineTestState }) {
  if (state === "testing") {
    return <RefreshCcw size={size} className="shrink-0 animate-spin" />;
  }
  if (state === "success") {
    return <Check size={size} className="shrink-0" />;
  }
  if (state === "error") {
    return <X size={size} className="shrink-0" />;
  }
  return <FlaskConical size={size} className="shrink-0" />;
}
