import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { AlertCircle, CheckCircle2, Info, RefreshCcw, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { cx } from "../lib/format";

export type PageToastTone = "message" | "info" | "success" | "error" | "loading";

export type PageToastAction = {
  label: string;
  onClick: () => void;
};

export type PageToastState = {
  action?: PageToastAction;
  id: string;
  timeoutMs?: number | null;
  tone: PageToastTone;
  text: string;
};

export type ToastPatch = Partial<Omit<PageToastState, "action" | "id">> & {
  action?: PageToastAction | null;
};

type ToastInput = {
  action?: PageToastAction;
  text: string;
  timeoutMs?: number | null;
  tone?: PageToastTone;
};

export type ToastContextValue = {
  dismissToast: (id: string) => void;
  showToast: {
    (text: string, tone?: PageToastTone, action?: PageToastAction): string;
    (toast: ToastInput): string;
  };
  updateToast: (id: string, patch: ToastPatch) => void;
};

interface PageToastProps {
  toast: PageToastState;
  onDismiss: () => void;
}

interface ToastItemProps {
  dismissToast: (id: string) => void;
  toast: PageToastState;
}

const ToastContext = createContext<ToastContextValue | null>(null);

let toastSequence = 0;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<PageToastState[]>([]);

  const dismissToast = useCallback((id: string) => {
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  const showToast = useCallback<ToastContextValue["showToast"]>(
    (input: string | ToastInput, tone: PageToastTone = "message", action?: PageToastAction) => {
      const toastInput = typeof input === "string" ? { action, text: input, tone } : input;
      const id = `toast-${Date.now()}-${toastSequence++}`;
      const toast: PageToastState = {
        id,
        action: toastInput.action,
        text: toastInput.text,
        timeoutMs: toastInput.timeoutMs,
        tone: toastInput.tone ?? "message",
      };
      setToasts((current) => [...current, toast]);
      return id;
    },
    [],
  );

  const updateToast = useCallback((id: string, patch: ToastPatch) => {
    setToasts((current) =>
      current.map((toast) => {
        if (toast.id !== id) {
          return toast;
        }
        const { action, ...rest } = patch;
        const next: PageToastState = { ...toast, ...rest };
        if ("action" in patch) {
          next.action = action ?? undefined;
        }
        return next;
      }),
    );
  }, []);

  const value = useMemo(
    () => ({ dismissToast, showToast, updateToast }),
    [dismissToast, showToast, updateToast],
  );

  return (
    <ToastContext.Provider value={value}>
      {children}
      <ToastViewport dismissToast={dismissToast} toasts={toasts} />
    </ToastContext.Provider>
  );
}

export function useToasts() {
  const value = useContext(ToastContext);
  if (!value) {
    throw new Error("useToasts must be used within ToastProvider");
  }
  return value;
}

function ToastViewport({
  dismissToast,
  toasts,
}: {
  dismissToast: (id: string) => void;
  toasts: PageToastState[];
}) {
  if (!toasts.length) {
    return null;
  }

  return (
    <div className="fixed bottom-4 left-4 z-[70] flex max-w-[min(calc(100vw-2rem),460px)] flex-col gap-2">
      {toasts.map((toast) => (
        <ToastItem
          dismissToast={dismissToast}
          key={toast.id}
          toast={toast}
        />
      ))}
    </div>
  );
}

function ToastItem({ dismissToast, toast }: ToastItemProps) {
  const dismissCurrentToast = useCallback(() => dismissToast(toast.id), [dismissToast, toast.id]);
  const hasAction = Boolean(toast.action);

  useEffect(() => {
    const timeoutMs = toastTimeoutMs(toast);
    if (timeoutMs == null) {
      return;
    }
    const timer = window.setTimeout(() => dismissToast(toast.id), timeoutMs);
    return () => window.clearTimeout(timer);
  }, [dismissToast, hasAction, toast.id, toast.timeoutMs, toast.tone]);

  return <PageToast toast={toast} onDismiss={dismissCurrentToast} />;
}

export function PageToast({ toast, onDismiss }: PageToastProps) {
  const { t } = useTranslation();
  const dismissible = toast.tone !== "loading";
  return (
    <div
      className={cx(
        "grid min-h-10 w-full grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2 rounded-panel px-3 py-2 text-sm shadow-floating transition-[opacity,transform] duration-150 ease-out",
        toast.tone === "error"
          ? "bg-red-50 text-danger"
          : toast.tone === "success"
            ? "bg-emerald-50 text-emerald-700"
            : "bg-surface text-slate-700",
      )}
      role={toast.tone === "error" ? "alert" : "status"}
    >
      {toast.tone === "loading" ? (
        <RefreshCcw size={14} className="animate-spin text-action" />
      ) : toast.tone === "success" ? (
        <CheckCircle2 size={14} className="text-emerald-600" />
      ) : toast.tone === "error" ? (
        <AlertCircle size={14} className="text-danger" />
      ) : (
        <Info size={14} className="text-action" />
      )}
      <span
        className={cx(
          "min-w-0",
          toast.action
            ? "truncate"
            : toast.tone === "error"
              ? "max-h-32 overflow-auto whitespace-pre-wrap break-words"
              : "truncate",
        )}
      >
        {toast.text}
      </span>
      {toast.action && (
        <button
          type="button"
          className="focus-ring inline-flex h-7 items-center justify-center rounded-control bg-ink px-3 text-xs font-semibold text-white shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-slate-800 hover:shadow-raised active:scale-[0.96]"
          onClick={toast.action.onClick}
        >
          {toast.action.label}
        </button>
      )}
      {dismissible && (
        <button
          type="button"
          className="focus-ring grid h-6 w-6 place-items-center rounded-control text-slate-500 transition-colors hover:bg-panel hover:text-ink"
          aria-label={t("common.dismissNotification")}
          onClick={onDismiss}
        >
          <X size={14} />
        </button>
      )}
    </div>
  );
}

function toastTimeoutMs(toast: PageToastState) {
  if (toast.timeoutMs !== undefined) {
    return toast.timeoutMs;
  }
  if (toast.action || toast.tone === "loading" || toast.tone === "error") {
    return null;
  }
  return 3000;
}
