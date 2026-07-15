import { Activity, Flag, Pause, Play, RefreshCcw, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, messageFromError } from "../lib/tauri";
import type { DiagnosticsActionResult, DiagnosticsStatus } from "../lib/types";
import { useToasts } from "./PageToast";

interface DebugDiagnosticsOverlayProps {
  enabled: boolean;
  gatewayRunning: boolean;
  onClose: () => void;
  open: boolean;
}

export function DebugDiagnosticsOverlay({
  enabled,
  gatewayRunning,
  onClose,
  open,
}: DebugDiagnosticsOverlayProps) {
  const { t } = useTranslation();
  const { showToast, updateToast } = useToasts();
  const [status, setStatus] = useState<DiagnosticsStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  async function refresh(silent = false) {
    if (!enabled || !gatewayRunning || !open) {
      setStatus(null);
      return;
    }
    try {
      setStatus(await api.diagnosticsStatus());
      setError(null);
    } catch (err) {
      setError(messageFromError(err));
      if (!silent) {
        showToast(t("diagnostics.statusFailed"), "error");
      }
    }
  }

  useEffect(() => {
    if (!enabled || !gatewayRunning || !open) {
      setStatus(null);
      setError(null);
      return;
    }
    void refresh(true);
    const interval = window.setInterval(() => void refresh(true), 5_000);
    return () => window.clearInterval(interval);
  }, [enabled, gatewayRunning, open]);

  useEffect(() => {
    if (!enabled || !open) {
      return;
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [enabled, onClose, open]);

  async function runAction(
    name: "mark" | "pause" | "resume" | "delete",
    action: () => Promise<DiagnosticsActionResult>,
    incidentId?: string,
  ) {
    if (busy) {
      return;
    }
    setBusy(name === "delete" ? `${name}:${incidentId}` : name);
    const toastId = showToast(t(`diagnostics.${name}Loading`, { incident: incidentId ?? "" }), "loading");
    try {
      const result = await action();
      setStatus(result.status);
      setError(null);
      const text =
        name === "mark" && result.accepted === false
          ? t("diagnostics.markUnavailable")
          : name === "delete" && result.deleted === false
            ? t("diagnostics.deleteMissing", { incident: incidentId })
            : t(`diagnostics.${name}Done`, { incident: incidentId ?? result.incident_id ?? "" });
      updateToast(toastId, {
        action: null,
        text,
        tone: name === "mark" && result.accepted === false ? "error" : "success",
      });
    } catch (err) {
      setError(messageFromError(err));
      updateToast(toastId, {
        action: null,
        text: t("diagnostics.actionFailed"),
        tone: "error",
      });
    } finally {
      setBusy(null);
    }
  }

  if (!enabled || !open) {
    return null;
  }

  const active = Boolean(status?.active);
  const paused = Boolean(status?.paused);
  const controlsAvailable = gatewayRunning && Boolean(status) && !busy;
  const rollingHours = Math.floor((status?.rolling_window_seconds ?? 0) / 3600);

  return (
    <div className="fixed inset-0 z-[80] grid place-items-center bg-black/20 px-4 py-6">
      <section
        aria-labelledby="debug-diagnostics-overlay-title"
        aria-modal="true"
        className="grid max-h-full w-full max-w-[680px] grid-rows-[auto_minmax(0,1fr)] overflow-hidden rounded-overlay bg-surface shadow-overlay"
        role="dialog"
      >
        <div className="flex min-w-0 items-start justify-between gap-3 px-4 py-3 shadow-hairline">
          <div className="min-w-0">
            <h2 id="debug-diagnostics-overlay-title" className="flex min-w-0 items-center gap-2 text-base font-semibold text-ink">
              <Activity size={16} className="shrink-0 text-action" />
              <span className="truncate">{t("diagnostics.title")}</span>
            </h2>
            <p className="mt-0.5 truncate text-xs text-slate-500">
              {t("diagnostics.summary", {
                hours: rollingHours,
                bytes: status?.rolling_bytes ?? 0,
                count: status?.incident_count ?? 0,
              })}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <span
              className={`rounded-control px-2 py-1 text-[10px] font-semibold ${
                paused
                  ? "bg-amber-100 text-amber-800"
                  : active
                    ? "bg-emerald-100 text-emerald-800"
                    : "bg-slate-100 text-slate-600"
              }`}
            >
              {paused ? t("diagnostics.paused") : active ? t("diagnostics.active") : t("diagnostics.unavailable")}
            </span>
            <button
              type="button"
              className="focus-ring grid h-8 w-8 shrink-0 place-items-center rounded-control bg-panel text-slate-600 shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-white hover:shadow-raised active:scale-[0.96]"
              aria-label={t("common.close")}
              onClick={onClose}
              title={t("common.close")}
            >
              <X size={15} />
            </button>
          </div>
        </div>

        <div className="min-h-0 overflow-auto p-3">
          <div className="grid gap-2">
            <p className="text-xs text-slate-500">{t("diagnostics.subtitle")}</p>

            {!gatewayRunning ? (
              <p className="rounded-inner bg-panel px-2 py-1.5 text-xs text-slate-600">{t("diagnostics.gatewayRequired")}</p>
            ) : error ? (
              <p className="rounded-inner bg-amber-50 px-2 py-1.5 text-xs text-amber-800">{t("diagnostics.statusDelayed")}</p>
            ) : status ? (
              <div className="grid gap-1 rounded-inner bg-panel p-2 text-xs text-slate-600">
                <span>{t("diagnostics.rolling", { hours: rollingHours, bytes: status.rolling_bytes })}</span>
                <span>{t("diagnostics.incidents", { count: status.incident_count })}</span>
                <span>{t("diagnostics.noRestartRequired")}</span>
              </div>
            ) : (
              <p className="rounded-inner bg-panel px-2 py-1.5 text-xs text-slate-500">{t("diagnostics.loading")}</p>
            )}

            <div className="flex flex-wrap items-center gap-1.5">
              <button
                type="button"
                className="focus-ring inline-flex h-8 items-center gap-1 rounded-control bg-ink px-2 text-[11px] font-semibold text-white shadow-control transition hover:bg-slate-800 disabled:bg-slate-300"
                disabled={!controlsAvailable || paused}
                onClick={() => void runAction("mark", () => api.diagnosticsManualMark())}
              >
                <Flag size={13} />
                {t("diagnostics.mark")}
              </button>
              <button
                type="button"
                className="focus-ring inline-flex h-8 items-center gap-1 rounded-control bg-panel px-2 text-[11px] font-semibold text-slate-700 shadow-control transition hover:bg-white disabled:text-slate-300"
                disabled={!controlsAvailable}
                onClick={() =>
                  void runAction(paused ? "resume" : "pause", () =>
                    paused ? api.diagnosticsResume() : api.diagnosticsPause(),
                  )
                }
              >
                {paused ? <Play size={13} /> : <Pause size={13} />}
                {paused ? t("diagnostics.resume") : t("diagnostics.pause")}
              </button>
              <button
                type="button"
                className="focus-ring inline-flex h-8 w-8 items-center justify-center rounded-control bg-panel text-slate-700 shadow-control transition hover:bg-white disabled:text-slate-300"
                disabled={!gatewayRunning || Boolean(busy)}
                aria-label={t("diagnostics.refresh")}
                title={t("diagnostics.refresh")}
                onClick={() => void refresh()}
              >
                <RefreshCcw size={13} />
              </button>
            </div>

            {status?.incident_ids.length ? (
              <div className="grid gap-1 border-t border-line pt-2">
                {status.incident_ids.map((incidentId) => (
                  <div key={incidentId} className="flex items-center justify-between gap-2 rounded-inner bg-panel px-2 py-1 text-xs text-slate-600">
                    <span className="font-mono">{incidentId}</span>
                    <button
                      type="button"
                      className="focus-ring inline-flex h-6 items-center gap-1 rounded-control px-1.5 text-[11px] font-semibold text-danger hover:bg-red-50 disabled:text-slate-300"
                      disabled={!controlsAvailable}
                      onClick={() => void runAction("delete", () => api.diagnosticsDeleteIncident(incidentId), incidentId)}
                    >
                      <Trash2 size={12} />
                      {t("diagnostics.delete")}
                    </button>
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        </div>
      </section>
    </div>
  );
}
