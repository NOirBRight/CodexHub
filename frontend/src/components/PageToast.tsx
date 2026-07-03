import { RefreshCcw, X } from "lucide-react";
import { cx } from "../lib/format";

export type PageToastTone = "info" | "error" | "loading";

export type PageToastState = {
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
        "absolute bottom-3 left-3 z-50 grid max-w-[420px] grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2 rounded-md border px-3 py-2 text-sm shadow-lg",
        toast.tone === "error"
          ? "border-red-200 bg-red-50 text-danger"
          : "border-line bg-white text-slate-700",
      )}
      role={toast.tone === "error" ? "alert" : "status"}
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
        onClick={onDismiss}
      >
        <X size={14} />
      </button>
    </div>
  );
}
