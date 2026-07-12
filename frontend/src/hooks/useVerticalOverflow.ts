import { useLayoutEffect, useRef, useState } from "react";

export function useVerticalOverflow<T extends HTMLElement>(dependencies: ReadonlyArray<unknown>) {
  const ref = useRef<T | null>(null);
  const [hasOverflow, setHasOverflow] = useState(false);

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) {
      setHasOverflow(false);
      return;
    }

    const update = () => {
      setHasOverflow(element.scrollHeight > element.clientHeight + 1);
    };

    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    Array.from(element.children).forEach((child) => observer.observe(child));
    window.addEventListener("resize", update);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", update);
    };
  }, dependencies);

  return [ref, hasOverflow] as const;
}
