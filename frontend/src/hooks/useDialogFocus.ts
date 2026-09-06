import { useEffect, useRef, type RefObject } from "react";

/** Keep keyboard navigation inside the active dialog and restore its trigger. */
export function useDialogFocus(
  open: boolean,
  panel: RefObject<HTMLElement>,
  onClose: () => void,
) {
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    function keydown(event: KeyboardEvent) {
      if (!panel.current?.contains(document.activeElement)) return;
      if (event.key === "Escape" && !event.defaultPrevented) {
        event.preventDefault();
        event.stopPropagation();
        close.current();
      }
      if (event.key === "Tab") {
        const focusable = Array.from(
          panel.current.querySelectorAll<HTMLElement>(
            'button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],[tabindex="0"]',
          ),
        ).filter((element) => element.getClientRects().length > 0);
        const first = focusable[0],
          last = focusable[focusable.length - 1];
        if (
          event.shiftKey &&
          (document.activeElement === first ||
            document.activeElement === panel.current)
        ) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    }
    document.addEventListener("keydown", keydown);
    return () => {
      document.removeEventListener("keydown", keydown);
      previous?.focus();
    };
  }, [open, panel]);
}
