import { getCurrentWebview } from "@tauri-apps/api/webview";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { useLayoutEffect, useState, type ReactNode } from "react";

/** Default window is 1024x768. Layout is slightly smaller than 1:1 so type stays readable without clipping. */
export const FIT_STAGE_WIDTH = 1024;
export const FIT_STAGE_HEIGHT = 768;
export const FIT_STAGE_SCALE = 0.93;

function usesCssTransformScale() {
  if (typeof navigator === "undefined") {
    return true;
  }
  // WebKitGTK does not keep hit-testing aligned with CSS transform, so clicks
  // miss the painted UI and punch through. Linux uses webview zoom instead.
  return !/Linux/i.test(navigator.userAgent) || /Android/i.test(navigator.userAgent);
}

export function FitStage({ children }: { children: ReactNode }) {
  const cssTransform = usesCssTransformScale();
  const [metrics, setMetrics] = useState({
    scale: FIT_STAGE_SCALE,
    width: FIT_STAGE_WIDTH / FIT_STAGE_SCALE,
    height: FIT_STAGE_HEIGHT / FIT_STAGE_SCALE,
  });

  useLayoutEffect(() => {
    let cancelled = false;
    let stopListening: (() => void) | undefined;

    const apply = async () => {
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
      if (!cssTransform) {
        await setWebviewZoom(scale);
      } else {
        await setWebviewZoom(1);
      }
    };

    void apply();
    void listenForViewportChanges(() => {
      void apply();
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
  }, [cssTransform]);

  return (
    <div className="relative h-full w-full overflow-hidden bg-canvas">
      <div className="pointer-events-none absolute inset-0 bg-canvas" aria-hidden="true" />
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
