import { Copy } from "lucide-react";

interface EndpointRowProps {
  label: string;
  meta: string;
  value: string;
  onCopy: () => void;
}

export function EndpointRow({ label, meta, onCopy, value }: EndpointRowProps) {
  return (
    <div className="grid gap-2 rounded-md border border-line bg-white px-3 py-2 text-sm lg:grid-cols-[130px_minmax(0,1fr)_auto] lg:items-center">
      <div className="min-w-0">
        <div className="truncate font-semibold text-ink">{label}</div>
        <div className="truncate text-[11px] text-slate-500">{meta}</div>
      </div>
      <code className="truncate font-mono text-xs text-slate-600">{value}</code>
      <button
        type="button"
        className="focus-ring inline-flex h-8 items-center justify-center gap-1 rounded-md border border-line bg-panel px-2 text-xs font-semibold text-slate-700 hover:bg-slate-100"
        onClick={onCopy}
      >
        <Copy size={13} />
        Copy
      </button>
    </div>
  );
}
