import { Grip } from "lucide-react";
import { DragEvent, ReactNode, useState } from "react";
import { cx } from "../lib/format";

interface SortableListProps<T> {
  items: T[];
  getId: (item: T) => string;
  renderItem: (item: T) => ReactNode;
  onReorder: (items: T[]) => void;
  className?: string;
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

  function startDrag(event: DragEvent<HTMLButtonElement>, id: string) {
    const row = event.currentTarget.closest("[data-sortable-row='true']");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", id);
    if (row instanceof HTMLElement) {
      event.dataTransfer.setDragImage(row, 18, 18);
    }
    setDraggedId(id);
  }

  function endDrag() {
    setDraggedId(null);
    setDropTargetId(null);
  }

  function move(targetId: string) {
    if (!draggedId || draggedId === targetId) {
      return;
    }
    const from = items.findIndex((item) => getId(item) === draggedId);
    const to = items.findIndex((item) => getId(item) === targetId);
    if (from < 0 || to < 0) {
      return;
    }
    const next = [...items];
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    onReorder(next);
  }

  return (
    <div className={cx("space-y-3", className)}>
      {items.map((item) => {
        const id = getId(item);
        return (
          <div
            key={id}
            data-sortable-row="true"
            className={cx(
              "group grid grid-cols-[30px_minmax(0,1fr)] items-stretch rounded-md border border-line bg-white shadow-subtle transition-[border-color,box-shadow,opacity,transform]",
              draggedId === id && "border-action bg-blue-50 opacity-70 shadow-md",
              dropTargetId === id && draggedId !== id && "border-action shadow-md",
            )}
            onDragEnter={() => setDropTargetId(id)}
            onDragOver={(event) => {
              event.preventDefault();
              event.dataTransfer.dropEffect = "move";
              setDropTargetId(id);
            }}
            onDragLeave={() => setDropTargetId((current) => (current === id ? null : current))}
            onDrop={() => {
              move(id);
              endDrag();
            }}
          >
            <button
              type="button"
              className="focus-ring flex cursor-grab items-center justify-center border-r border-transparent bg-transparent text-slate-300 transition-colors hover:text-slate-500 group-hover:border-line group-hover:text-slate-400 active:cursor-grabbing"
              draggable
              aria-label="Reorder"
              title="Reorder"
              onDragStart={(event) => startDrag(event, id)}
              onDragEnd={endDrag}
            >
              <Grip size={15} />
            </button>
            <div className="min-w-0">{renderItem(item)}</div>
          </div>
        );
      })}
    </div>
  );
}
