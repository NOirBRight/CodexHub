import { useDialogFocus } from "../../hooks/useDialogFocus";
import { useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { useTranslation } from "react-i18next";

/** Shared, bounded desktop dialog. The body owns scrolling; actions stay visible. */
export function WorkspaceDialog({
  open,
  title,
  children,
  actions,
  onClose,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  actions?: ReactNode;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const panel = useRef<HTMLElement>(null);
  useDialogFocus(open, panel, onClose);
  const host = document.querySelector(".workspace-root");
  if (!open || !host) return null;
  return createPortal(
    <div
      className="ws-dialog-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <section
        ref={panel}
        tabIndex={-1}
        className="ws-dialog"
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <header>
          <h2>{title}</h2>
          <button
            className="ws-icon"
            aria-label={t("common.close")}
            onClick={onClose}
          >
            <X size={16} />
          </button>
        </header>
        <div className="ws-dialog-body">{children}</div>
        {actions && <footer>{actions}</footer>}
      </section>
    </div>,
    host,
  );
}
