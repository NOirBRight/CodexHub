import { getCurrentWebview } from "@tauri-apps/api/webview";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { useLayoutEffect, useRef, useState, type ReactNode } from "react";

/** Linux uses the compact 1024x768 shell; other platforms retain the beta3 desktop geometry. */
export const FIT_STAGE_WIDTH = 1024;
export const FIT_STAGE_HEIGHT = 768;
export const FIT_STAGE_SCALE = 0.93;
const DEFAULT_FIT_STAGE_WIDTH = 1280;
const DEFAULT_FIT_STAGE_HEIGHT = 960;

export function FitStage({ children }: { children: ReactNode }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const linuxViewport = isLinuxViewport();
  const [metrics, setMetrics] = useState(() => linuxViewport
    ? {
      scale: FIT_STAGE_SCALE,
      width: FIT_STAGE_WIDTH / FIT_STAGE_SCALE,
      height: FIT_STAGE_HEIGHT / FIT_STAGE_SCALE,
    }
    : {
      scale: 1,
      width: DEFAULT_FIT_STAGE_WIDTH,
      height: DEFAULT_FIT_STAGE_HEIGHT,
    });

  useLayoutEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    if (!linuxViewport) {
      const measureDefaultViewport = () => {
        const viewportWidth = host.clientWidth;
        const viewportHeight = host.clientHeight;
        if (viewportWidth <= 0 || viewportHeight <= 0) return;
        const scale = Math.min(
          1,
          viewportWidth / DEFAULT_FIT_STAGE_WIDTH,
          viewportHeight / DEFAULT_FIT_STAGE_HEIGHT,
        );
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
      setMetrics({
        scale,
        width: viewport.width / scale,
        height: viewport.height / scale,
      });
      // WebKitGTK setZoom != 1 letterboxes a frame around the UI.
      await setWebviewZoom(1);
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
    <div ref={hostRef} className="relative h-full w-full overflow-hidden bg-canvas">
      <div
        className="relative origin-top-left"
        style={{
          width: metrics.width,
          height: metrics.height,
          transform: "scale(" + metrics.scale + ")",
        }}
      >
        {children}
      </div>
    </div>
  );
}

function isLinuxViewport() {
  return typeof navigator !== "undefined" && /Linux/i.test(navigator.userAgent);
}

async function readLogicalViewportSize() {
  try {
    const currentWindow = getCurrentWindow();
    const [inner, factor] = await Promise.all([currentWindow.innerSize(), currentWindow.scaleFactor()]);
    return {
      width: inner.width / factor,
      height: inner.height / factor,
    };
  } catch {
    return {
      width: document.documentElement.clientWidth,
      height: document.documentElement.clientHeight,
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
