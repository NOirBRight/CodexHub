import { RefreshCcw, X } from "lucide-react";
import { cx } from "../lib/format";

export type PageToastTone = "info" | "success" | "error" | "loading";

export type PageToastState = {
  action?: {
    label: string;
    onClick: () => void;
  };
  tone: PageToastTone;
  text: string;
};

interface PageToastProps {
  toast: PageToastState;
  onDismiss: () => void;
}

export function PageToast({ toast, onDismiss }: PageToastProps) {
  return (
    <div
      className={cx(
        "absolute bottom-3 left-3 z-50 grid max-w-[460px] grid-cols-[auto_minmax(0,1fr)_auto_auto] items-center gap-2 rounded-panel px-3 py-2 text-sm shadow-floating",
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
      ) : (
        <span className="h-2 w-2 rounded-full bg-action" />
      )}
      <span
        className={cx(
          "min-w-0",
          toast.action ? "truncate" : toast.tone === "error" ? "max-h-32 overflow-auto whitespace-pre-wrap break-words" : "truncate",
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
      <button
        type="button"
        className="focus-ring grid h-6 w-6 place-items-center rounded-control text-slate-500 transition-colors hover:bg-panel hover:text-ink"
        aria-label="Dismiss notification"
        onClick={onDismiss}
      >
        <X size={14} />
      </button>
    </div>
  );
}
