import { getCurrentWebview } from "@tauri-apps/api/webview";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { useLayoutEffect, useRef, useState, type ReactNode } from "react";

/** Compact desktop geometry; native Linux retains webview zoom for correct pointer hit-testing. */
export const FIT_STAGE_WIDTH = 1024;
export const FIT_STAGE_HEIGHT = 768;
export const FIT_STAGE_SCALE = 1.1;
export const NATIVE_SHADOW_INSET = 12;
const DEFAULT_FIT_STAGE_WIDTH = 1024;
const DEFAULT_FIT_STAGE_HEIGHT = 768;

function usesCssTransformScale() {
  if (typeof navigator === "undefined") {
    return true;
  }
  // WebKitGTK does not keep hit-testing aligned with CSS transforms, so clicks
  // can miss the painted UI and punch through. Linux uses webview zoom instead.
  return !isLinuxViewport() || /Android/i.test(navigator.userAgent);
}

export function FitStage({ children }: { children: ReactNode }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const linuxViewport = isLinuxViewport();
  const cssTransform = usesCssTransformScale();
  const [metrics, setMetrics] = useState(() => linuxViewport
    ? {
      scale: FIT_STAGE_SCALE,
      width: FIT_STAGE_WIDTH / FIT_STAGE_SCALE - 2 * NATIVE_SHADOW_INSET,
      height: FIT_STAGE_HEIGHT / FIT_STAGE_SCALE - 2 * NATIVE_SHADOW_INSET,
    }
    : {
      scale: FIT_STAGE_SCALE,
      width: DEFAULT_FIT_STAGE_WIDTH / FIT_STAGE_SCALE,
      height: DEFAULT_FIT_STAGE_HEIGHT / FIT_STAGE_SCALE,
    });

  useLayoutEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    if (!linuxViewport) {
      const measureDefaultViewport = () => {
        const viewportWidth = host.clientWidth;
        const viewportHeight = host.clientHeight;
        if (viewportWidth <= 0 || viewportHeight <= 0) return;
        // The workspace now reflows instead of shrinking desktop controls.
        const scale = FIT_STAGE_SCALE;
        setMetrics({
          scale,
          width: viewportWidth / scale,
          height: viewportHeight / scale,
        });
      };
      measureDefaultViewport();
      const observer = new ResizeObserver(measureDefaultViewport);
      observer.observe(host);
      return () => observer.disconnect();
    }

    let cancelled = false;
    let stopListening: (() => void) | undefined;

    const syncLinuxViewportMetrics = async () => {
      const viewport = await readLogicalViewportSize();
      if (cancelled || viewport.width <= 0 || viewport.height <= 0) {
        return;
      }
      const scale = FIT_STAGE_SCALE;
      const inset = viewport.expanded ? 0 : NATIVE_SHADOW_INSET;
      document.documentElement.dataset.windowExpanded = String(viewport.expanded);
      setMetrics({
        scale,
        width: viewport.width / scale - 2 * inset,
        height: viewport.height / scale - 2 * inset,
      });
      await setWebviewZoom(scale);
    };

    void syncLinuxViewportMetrics();
    void listenForViewportChanges(() => {
      void syncLinuxViewportMetrics();
    }).then((stop) => {
      if (cancelled) {
        stop();
        return;
      }
      stopListening = stop;
    });

    return () => {
      cancelled = true;
      stopListening?.();
      void setWebviewZoom(1);
    };
  }, [linuxViewport]);

  return (
    <div ref={hostRef} className="fit-stage-host relative h-full w-full overflow-hidden">
      <div
        className={cssTransform ? "relative origin-top-left" : "relative"}
        style={{
          width: metrics.width,
          height: metrics.height,
          ...(cssTransform ? { transform: "scale(" + metrics.scale + ")" } : {}),
        }}
      >
        {children}
      </div>
    </div>
  );
}

function isLinuxViewport() {
  return typeof navigator !== "undefined" && Boolean(window.__TAURI_INTERNALS__) && /Linux/i.test(navigator.userAgent);
}

async function readLogicalViewportSize() {
  try {
    const currentWindow = getCurrentWindow();
    const [inner, factor, maximized, fullscreen] = await Promise.all([
      currentWindow.innerSize(), currentWindow.scaleFactor(),
      currentWindow.isMaximized(), currentWindow.isFullscreen(),
    ]);
    return {
      width: inner.width / factor,
      height: inner.height / factor,
      expanded: maximized || fullscreen,
    };
  } catch {
    return {
      width: document.documentElement.clientWidth,
      height: document.documentElement.clientHeight,
      expanded: false,
    };
  }
}

async function listenForViewportChanges(onChange: () => void) {
  try {
    return await getCurrentWindow().onResized(() => {
      onChange();
    });
  } catch {
    window.addEventListener("resize", onChange);
    return () => window.removeEventListener("resize", onChange);
  }
}

async function setWebviewZoom(scale: number) {
  try {
    await getCurrentWebview().setZoom(scale);
  } catch {
    // Browser preview has no webview zoom control.
  }
}
