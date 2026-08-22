import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export interface ConfirmDialogRequest {
  cancelLabel: string;
  confirmLabel: string;
  message: string;
  title: string;
  tone?: "default" | "danger";
}

interface ActiveConfirmDialog extends ConfirmDialogRequest {
  resolve: (confirmed: boolean) => void;
}

export function useConfirmDialog() {
  const [active, setActive] = useState<ActiveConfirmDialog | null>(null);
  const activeRef = useRef<ActiveConfirmDialog | null>(null);

  const settle = useCallback((confirmed: boolean) => {
    const request = activeRef.current;
    activeRef.current = null;
    setActive(null);
    request?.resolve(confirmed);
  }, []);

  const confirm = useCallback((request: ConfirmDialogRequest) => {
    activeRef.current?.resolve(false);
    return new Promise<boolean>((resolve) => {
      const next = { ...request, resolve };
      activeRef.current = next;
      setActive(next);
    });
  }, []);

  useEffect(() => () => {
    activeRef.current?.resolve(false);
    activeRef.current = null;
  }, []);

  return {
    confirm,
    dialog: active ? <ConfirmDialog request={active} onClose={settle} /> : null,
  };
}

function ConfirmDialog({
  onClose,
  request,
}: {
  onClose: (confirmed: boolean) => void;
  request: ConfirmDialogRequest;
}) {
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    confirmRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const portalHost = document.getElementById("root");
  if (!portalHost) return null;

  return createPortal(
    <div
      className="fixed inset-0 z-[200] grid place-items-center bg-slate-950/35 p-4 backdrop-blur-[2px]"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) {
          onClose(false);
        }
      }}
    >
      <section
        aria-describedby="confirm-dialog-message"
        aria-labelledby="confirm-dialog-title"
        aria-modal="true"
        className="w-[min(420px,calc(100vw-2rem))] overflow-hidden rounded-overlay bg-surface shadow-overlay"
        role="dialog"
      >
        <div className="grid gap-2 px-5 py-4">
          <h2 id="confirm-dialog-title" className="text-base font-semibold text-ink">
            {request.title}
          </h2>
          <p id="confirm-dialog-message" className="text-sm leading-6 text-muted">
            {request.message}
          </p>
        </div>
        <div className="flex justify-end gap-2 bg-panel px-5 py-3 shadow-[inset_0_1px_0_rgba(15,23,42,0.08)]">
          <button
            type="button"
            className="focus-ring h-9 rounded-control bg-surface px-3 text-sm font-semibold text-ink shadow-control hover:bg-white"
            onClick={() => onClose(false)}
          >
            {request.cancelLabel}
          </button>
          <button
            ref={confirmRef}
            type="button"
            className={request.tone === "danger"
              ? "focus-ring h-9 rounded-control bg-rose-600 px-3 text-sm font-semibold text-white shadow-control hover:bg-rose-700"
              : "focus-ring h-9 rounded-control bg-ink px-3 text-sm font-semibold text-white shadow-control hover:bg-slate-800"}
            onClick={() => onClose(true)}
          >
            {request.confirmLabel}
          </button>
        </div>
      </section>
    </div>,
    portalHost,
  );
}
