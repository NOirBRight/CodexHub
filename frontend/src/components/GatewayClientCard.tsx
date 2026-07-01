import { TerminalSquare } from "lucide-react";
import { SegmentedSwitch } from "./SegmentedSwitch";
import { PendingPanel } from "./PendingPanel";
import type { GatewayClientContract } from "../lib/types";

interface GatewayClientCardProps {
  client: GatewayClientContract;
  pendingMessage: string;
}

export function GatewayClientCard({ client, pendingMessage }: GatewayClientCardProps) {
  return (
    <section className="grid gap-3 rounded-md border border-line bg-white p-4 shadow-subtle">
      <div className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-start gap-3">
        <div className="grid h-9 w-9 place-items-center rounded-md border border-line bg-panel text-slate-600">
          <TerminalSquare size={17} />
        </div>
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-ink">{client.name}</h3>
          <p className="mt-0.5 truncate text-xs text-slate-500">{client.kind}</p>
        </div>
        <span className="rounded-sm border border-line bg-panel px-1.5 py-0.5 text-[11px] font-semibold text-slate-500">
          manual
        </span>
      </div>

      <SegmentedSwitch
        ariaLabel={`${client.name} route mode`}
        className="grid-cols-2"
        disabled
        value="official"
        options={[
          { value: "official", label: "Official" },
          { value: "hub", label: "Hub" },
        ]}
      />

      <div className="rounded-md border border-line bg-panel p-3">
        <div className="text-[11px] font-semibold uppercase tracking-[0.04em] text-slate-500">
          CLI version
        </div>
        <div className="mt-1 flex flex-wrap items-center justify-between gap-2 text-xs text-slate-600">
          <span>Current unknown</span>
          <span>Latest unknown</span>
          <button
            type="button"
            className="focus-ring h-7 rounded-md border border-line bg-white px-2 font-semibold text-slate-500"
            disabled
          >
            Upgrade
          </button>
        </div>
      </div>

      <div className="grid gap-1 text-xs">
        <span className="font-semibold text-slate-500">Config target</span>
        <code className="truncate font-mono text-slate-600">{client.config_path}</code>
      </div>

      <PendingPanel
        compact
        title="Client manager"
        message={pendingMessage}
      />
    </section>
  );
}
