import { Grip } from "lucide-react";
import { PointerEvent as ReactPointerEvent, ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { cx } from "../lib/format";

interface SortableListProps<T> {
  items: T[];
  getId: (item: T) => string;
  renderItem: (item: T) => ReactNode;
  onReorder: (items: T[]) => void;
  className?: string;
}

interface DragGhost {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
  offsetX: number;
  offsetY: number;
}

export function SortableList<T>({
  items,
  getId,
  renderItem,
  onReorder,
  className,
}: SortableListProps<T>) {
  const [draggedId, setDraggedId] = useState<string | null>(null);
  const [dropTargetId, setDropTargetId] = useState<string | null>(null);
  const [previewItems, setPreviewItems] = useState<T[] | null>(null);
  const [dragGhost, setDragGhost] = useState<DragGhost | null>(null);
  const rowRefs = useRef(new Map<string, HTMLDivElement>());
  const previousRects = useRef<Map<string, DOMRect> | null>(null);
  const previewRef = useRef<T[] | null>(null);
  const dragGhostRef = useRef<DragGhost | null>(null);

  const renderedItems = previewItems ?? items;
  const draggedItem = draggedId
    ? (renderedItems.find((item) => getId(item) === draggedId) ?? items.find((item) => getId(item) === draggedId))
    : null;

  useEffect(() => {
    if (!draggedId) {
      return;
    }
    const style = document.createElement("style");
    style.textContent = `
      html.sortable-list-dragging,
      html.sortable-list-dragging * {
        cursor: grabbing !important;
        user-select: none !important;
      }
    `;
    const previousRootCursor = document.documentElement.style.cursor;
    const previousCursor = document.body.style.cursor;
    document.head.appendChild(style);
    document.documentElement.classList.add("sortable-list-dragging");
    document.documentElement.style.cursor = "grabbing";
    document.body.style.cursor = "grabbing";
    return () => {
      document.documentElement.classList.remove("sortable-list-dragging");
      style.remove();
      document.documentElement.style.cursor = previousRootCursor;
      document.body.style.cursor = previousCursor;
    };
  }, [draggedId]);

  useEffect(() => {
    if (!draggedId) {
      return;
    }

    function handlePointerMove(event: PointerEvent) {
      const currentGhost = dragGhostRef.current;
      if (!currentGhost) {
        return;
      }
      event.preventDefault();
      const nextGhost = {
        ...currentGhost,
        x: event.clientX - currentGhost.offsetX,
        y: event.clientY - currentGhost.offsetY,
      };
      dragGhostRef.current = nextGhost;
      setDragGhost(nextGhost);
      movePreviewFromPointer(nextGhost.y + nextGhost.height / 2);
    }

    function handlePointerUp(event: PointerEvent) {
      event.preventDefault();
      finishDrag(true);
    }

    function handlePointerCancel(event: PointerEvent) {
      event.preventDefault();
      finishDrag(false);
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        finishDrag(false);
      }
    }

    window.addEventListener("pointermove", handlePointerMove, { passive: false });
    window.addEventListener("pointerup", handlePointerUp, { passive: false });
    window.addEventListener("pointercancel", handlePointerCancel, { passive: false });
    window.addEventListener("keydown", handleKeyDown);

    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
      window.removeEventListener("pointercancel", handlePointerCancel);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [draggedId]);

  useLayoutEffect(() => {
    const rects = previousRects.current;
    if (!rects) {
      return;
    }
    previousRects.current = null;

    rowRefs.current.forEach((node, id) => {
      const previous = rects.get(id);
      if (!previous) {
        return;
      }
      const next = node.getBoundingClientRect();
      const deltaX = previous.left - next.left;
      const deltaY = previous.top - next.top;
      if (!deltaX && !deltaY) {
        return;
      }
      node.animate(
        [
          { transform: `translate(${deltaX}px, ${deltaY}px)` },
          { transform: "translate(0, 0)" },
        ],
        {
          duration: 160,
          easing: "cubic-bezier(0.2, 0, 0, 1)",
        },
      );
    });
  }, [renderedItems]);

  function startDrag(event: ReactPointerEvent<HTMLButtonElement>, id: string) {
    if (event.button !== 0) {
      return;
    }
    const row = event.currentTarget.closest("[data-sortable-row='true']");
    if (!(row instanceof HTMLElement)) {
      return;
    }
    event.preventDefault();
    event.currentTarget.focus();
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Some embedded webviews do not keep capture once the pointer leaves the handle.
    }
    const rect = row.getBoundingClientRect();
    const nextGhost = {
      id,
      x: rect.left,
      y: rect.top,
      width: rect.width,
      height: rect.height,
      offsetX: event.clientX - rect.left,
      offsetY: event.clientY - rect.top,
    };
    dragGhostRef.current = nextGhost;
    previewRef.current = items;
    setPreviewItems(items);
    setDragGhost(nextGhost);
    setDraggedId(id);
  }

  function finishDrag(commit: boolean) {
    const next = previewRef.current;
    if (commit && next && !sameOrder(next, items, getId)) {
      onReorder(next);
    }
    previewRef.current = null;
    dragGhostRef.current = null;
    setPreviewItems(null);
    setDragGhost(null);
    setDraggedId(null);
    setDropTargetId(null);
  }

  function captureRects() {
    previousRects.current = new Map(
      Array.from(rowRefs.current.entries()).map(([id, node]) => [id, node.getBoundingClientRect()]),
    );
  }

  function setRowRef(id: string, node: HTMLDivElement | null) {
    if (node) {
      rowRefs.current.set(id, node);
    } else {
      rowRefs.current.delete(id);
    }
  }

  function movePreviewFromPointer(pointerY: number) {
    if (!draggedId) {
      return;
    }
    const current = previewRef.current ?? items;
    const from = current.findIndex((item) => getId(item) === draggedId);
    if (from < 0) {
      return;
    }
    const draggedItem = current[from];
    const remaining = current.filter((item) => getId(item) !== draggedId);
    let insertionIndex = remaining.length;
    let nextDropTargetId: string | null = remaining.length ? getId(remaining[remaining.length - 1]) : null;

    for (let index = 0; index < remaining.length; index += 1) {
      const id = getId(remaining[index]);
      const node = rowRefs.current.get(id);
      if (!node) {
        continue;
      }
      const rect = node.getBoundingClientRect();
      if (pointerY < rect.top + rect.height / 2) {
        insertionIndex = index;
        nextDropTargetId = id;
        break;
      }
    }

    const next = [
      ...remaining.slice(0, insertionIndex),
      draggedItem,
      ...remaining.slice(insertionIndex),
    ];

    setDropTargetId(nextDropTargetId);
    if (sameOrder(next, current, getId)) {
      return;
    }
    captureRects();
    previewRef.current = next;
    setPreviewItems(next);
  }

  return (
    <div className={cx("space-y-3 px-px py-px", draggedId && "cursor-grabbing", className)}>
      {renderedItems.map((item) => {
        const id = getId(item);
        return (
          <div
            key={id}
            ref={(node) => setRowRef(id, node)}
            data-sortable-row="true"
            className={cx(
              "group grid grid-cols-[30px_minmax(0,1fr)] items-stretch rounded-inner bg-surface shadow-card transition-[box-shadow,opacity,transform] duration-150 ease-out hover:shadow-raised",
              draggedId === id && "invisible pointer-events-none border-transparent bg-transparent shadow-none",
              dropTargetId === id && draggedId !== id && "shadow-raised ring-2 ring-action/20",
            )}
          >
            <button
              type="button"
              data-sortable-handle="true"
              className={cx(
                "focus-ring flex touch-none select-none items-center justify-center rounded-l-inner border-r border-transparent bg-transparent text-slate-300 transition-colors hover:text-slate-500 group-hover:border-line group-hover:text-slate-400 [&_*]:pointer-events-none",
                draggedId ? "cursor-grabbing" : "cursor-grab",
              )}
              aria-label="Reorder"
              aria-grabbed={draggedId === id}
              title="Reorder"
              style={{ cursor: draggedId ? "grabbing" : "grab" }}
              onDragStart={(event) => event.preventDefault()}
              onPointerDown={(event) => startDrag(event, id)}
            >
              <Grip className="pointer-events-none" size={15} />
            </button>
            <div className="min-w-0">{renderItem(item)}</div>
          </div>
        );
      })}
      {dragGhost && draggedItem
        ? createPortal(
            <div
              className="pointer-events-none fixed z-50 grid grid-cols-[30px_minmax(0,1fr)] items-stretch rounded-inner bg-surface opacity-95 shadow-floating ring-2 ring-action/20"
              style={{
                left: dragGhost.x,
                top: dragGhost.y,
                width: dragGhost.width,
                minHeight: dragGhost.height,
              }}
            >
              <div className="flex cursor-grabbing items-center justify-center border-r border-line text-slate-400">
                <Grip size={15} />
              </div>
              <div className="min-w-0">{renderItem(draggedItem)}</div>
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}

function sameOrder<T>(left: T[], right: T[], getId: (item: T) => string) {
  return left.length === right.length && left.every((item, index) => getId(item) === getId(right[index]));
}
