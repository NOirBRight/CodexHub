import { useLayoutEffect, useRef, useState, type ReactNode } from "react";

/** Composed desktop shell that keeps title, tabs, Gateway, Recovery, a full usage chart, and five client cards on-screen. Smaller viewports scale down uniformly instead of clipping the bottom. */
export const FIT_STAGE_WIDTH = 1280;
export const FIT_STAGE_HEIGHT = 960;

export function FitStage({ children }: { children: ReactNode }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [metrics, setMetrics] = useState({
    scale: 1,
    width: FIT_STAGE_WIDTH,
    height: FIT_STAGE_HEIGHT,
  });

  useLayoutEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const measure = () => {
      const viewportWidth = host.clientWidth;
      const viewportHeight = host.clientHeight;
      if (viewportWidth <= 0 || viewportHeight <= 0) return;
      const scale = Math.min(1, viewportWidth / FIT_STAGE_WIDTH, viewportHeight / FIT_STAGE_HEIGHT);
      setMetrics({
        scale,
        width: viewportWidth / scale,
        height: viewportHeight / scale,
      });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  return (
    <div ref={hostRef} className="h-screen w-screen overflow-hidden bg-canvas">
      <div
        className="origin-top-left"
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
