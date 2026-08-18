import { getCurrentWindow } from "@tauri-apps/api/window";
import type { MouseEvent } from "react";
import { useTranslation } from "react-i18next";
import { cx } from "../lib/format";

type ResizeDirection =
  | "East"
  | "North"
  | "NorthEast"
  | "NorthWest"
  | "South"
  | "SouthEast"
  | "SouthWest"
  | "West";

const HANDLES: Array<{ direction: ResizeDirection; className: string }> = [
  { direction: "North", className: "top-0 right-2 left-2 h-1.5 cursor-n-resize" },
  { direction: "South", className: "right-2 bottom-0 left-2 h-1.5 cursor-s-resize" },
  { direction: "West", className: "top-2 bottom-2 left-0 w-1.5 cursor-w-resize" },
  { direction: "East", className: "top-2 right-0 bottom-2 w-1.5 cursor-e-resize" },
  { direction: "NorthWest", className: "top-0 left-0 h-3 w-3 cursor-nw-resize" },
  { direction: "NorthEast", className: "top-0 right-0 h-3 w-3 cursor-ne-resize" },
  { direction: "SouthWest", className: "bottom-0 left-0 h-3 w-3 cursor-sw-resize" },
  { direction: "SouthEast", className: "right-0 bottom-0 h-3 w-3 cursor-se-resize" },
];

export function WindowResizeHandles() {
  const { t } = useTranslation();
  return (
    <>
      {HANDLES.map((handle) => (
        <div
          key={handle.direction}
          aria-label={t("runtime.resizeWindow")}
          className={cx("fixed z-[100] select-none bg-canvas", handle.className)}
          data-window-control
          onMouseDown={(event) => startWindowResize(event, handle.direction)}
        />
      ))}
    </>
  );
}

function startWindowResize(event: MouseEvent<HTMLElement>, direction: ResizeDirection) {
  if (event.button !== 0) {
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  try {
    void getCurrentWindow().startResizeDragging(direction).catch(() => undefined);
  } catch {
    // Browser preview has no Tauri window to resize.
  }
}
