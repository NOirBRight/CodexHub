import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import type { Provider } from "../../lib/types";

export function ProviderCatalogPicker({
  existingIds,
  loading,
  onClose,
  onSelectCustom,
  onSelectPreset,
  presets,
}: {
  existingIds: Set<string>;
  loading: boolean;
  onClose: () => void;
  onSelectCustom: () => void;
  onSelectPreset: (provider: Provider) => void;
  presets: Provider[];
}) {
  const { t } = useTranslation();
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const portalHost = document.getElementById("root");
  if (!portalHost) return null;

  return createPortal(
    <div
      className="absolute inset-0 z-[200] grid place-items-center bg-slate-950/35 p-4 backdrop-blur-[2px]"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) {
          onClose();
        }
      }}
    >
      <section
        aria-describedby="provider-catalog-picker-body"
        aria-labelledby="provider-catalog-picker-title"
        aria-modal="true"
        className="grid w-[min(480px,calc(100vw-2rem))] max-h-[min(640px,calc(100vh-2rem))] grid-rows-[auto_minmax(0,1fr)_auto] overflow-hidden rounded-overlay bg-surface shadow-overlay"
        role="dialog"
      >
        <div className="grid gap-2 px-5 py-4">
          <h2 id="provider-catalog-picker-title" className="text-base font-semibold text-ink">
            {t("providers.chooseCatalogProviderTitle")}
          </h2>
          <p id="provider-catalog-picker-body" className="text-sm leading-6 text-muted">
            {t("providers.chooseCatalogProviderBody")}
          </p>
        </div>
        <div className="min-h-0 overflow-y-auto px-5 pb-2">
          {loading ? (
            <p className="py-6 text-sm text-slate-500">{t("providers.catalogPickerLoading")}</p>
          ) : (
            <div className="grid gap-2">
              {presets.map((provider) => {
                const alreadyAdded = existingIds.has(provider.id);
                return (
                  <button
                    key={provider.id}
                    type="button"
                    className="focus-ring grid w-full gap-1 rounded-control bg-panel px-3 py-3 text-left shadow-control hover:bg-white"
                    onClick={() => onSelectPreset(provider)}
                  >
                    <span className="flex items-center justify-between gap-2">
                      <span className="truncate text-sm font-semibold text-ink">{provider.name}</span>
                      {alreadyAdded ? (
                        <span className="shrink-0 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                          {t("providers.catalogProviderAlreadyAdded")}
                        </span>
                      ) : null}
                    </span>
                    <span className="truncate text-xs text-slate-500">{provider.base_url}</span>
                    {provider.id === "xai" ? (
                      <span className="text-xs text-slate-600">{t("providers.catalogProviderXaiHint")}</span>
                    ) : null}
                  </button>
                );
              })}
              <button
                type="button"
                className="focus-ring grid w-full gap-1 rounded-control border border-dashed border-line bg-surface px-3 py-3 text-left hover:bg-white"
                onClick={onSelectCustom}
              >
                <span className="truncate text-sm font-semibold text-ink">
                  {t("providers.chooseCatalogProviderCustom")}
                </span>
                <span className="text-xs text-slate-500">{t("providers.chooseCatalogProviderCustomHint")}</span>
              </button>
            </div>
          )}
        </div>
        <div className="flex justify-end bg-panel px-5 py-3 shadow-[inset_0_1px_0_rgba(15,23,42,0.08)]">
          <button
            ref={closeRef}
            type="button"
            className="focus-ring h-9 rounded-control bg-surface px-3 text-sm font-semibold text-ink shadow-control hover:bg-white"
            onClick={onClose}
          >
            {t("common.cancel")}
          </button>
        </div>
      </section>
    </div>,
    portalHost,
  );
}
