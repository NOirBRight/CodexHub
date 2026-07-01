import { AlertTriangle, CheckCircle2, CircleDashed } from "lucide-react";
import { cx } from "../lib/format";
import type { GatewayEvent, SubagentMatrixStatus } from "../lib/types";

export function SubagentDiagnosticsPanel({
  status,
}: {
  status: SubagentMatrixStatus | null;
}) {
  if (!status) {
    return (
      <section className="grid gap-3 border-t border-line p-5">
        <SectionTitle title="Subagent Diagnostics" subtitle="tool_search -> spawn_agent -> wait_agent -> close_agent" />
        <div className="rounded-md border border-line bg-panel p-4 text-sm text-slate-500">
          Diagnostics are not loaded.
        </div>
      </section>
    );
  }

  return (
    <section className="grid gap-4 border-t border-line p-5">
      <SectionTitle title="Subagent Diagnostics" subtitle="tool_search -> spawn_agent -> wait_agent -> close_agent" />

      <div className="grid gap-2 lg:grid-cols-4">
        {status.readiness.map((item) => (
          <div
            key={item.step}
            className="grid min-h-[86px] gap-2 rounded-md border border-line bg-panel p-3"
          >
            <div className="flex items-center gap-2 text-sm font-semibold">
              {item.ready ? (
                <CheckCircle2 size={16} className="text-ok" />
              ) : (
                <AlertTriangle size={16} className="text-warn" />
              )}
              <span>{item.step}</span>
            </div>
            <div className="text-xs leading-5 text-slate-500">{item.feature}</div>
          </div>
        ))}
      </div>

      <div className="overflow-hidden rounded-md border border-line">
        <div className="grid grid-cols-[1.2fr_0.8fr_1fr_1fr_0.8fr_0.8fr_0.8fr] bg-panel px-3 py-2 text-xs font-semibold text-slate-600">
          <span>Model</span>
          <span>Provider</span>
          <span>Thread</span>
          <span>Child agent</span>
          <span>Wait</span>
          <span>Close</span>
          <span>Output</span>
        </div>
        {status.rows.map((row) => (
          <div
            key={row.model}
            className="grid grid-cols-[1.2fr_0.8fr_1fr_1fr_0.8fr_0.8fr_0.8fr] items-center border-t border-line px-3 py-2 text-xs"
          >
            <span className="truncate font-medium">{row.model}</span>
            <span className="truncate text-slate-600">{row.provider}</span>
            <Value value={row.thread_id} />
            <Value value={row.child_agent_id} />
            <BooleanValue value={row.wait_timed_out === null ? null : !row.wait_timed_out} />
            <BooleanValue value={row.close_succeeded} />
            <BooleanValue value={row.child_output_ok} />
          </div>
        ))}
      </div>

      <p className="text-xs leading-5 text-slate-500">{status.message}</p>

      <EventList events={status.recent_events} title="Recent subagent proxy events" />
    </section>
  );
}

export function EventList({ events, title }: { events: GatewayEvent[]; title: string }) {
  return (
    <div className="grid gap-2">
      <h3 className="text-sm font-semibold">{title}</h3>
      {events.length === 0 ? (
        <div className="rounded-md border border-line bg-panel p-4 text-sm text-slate-500">
          No recent events found.
        </div>
      ) : (
        <div className="max-h-72 overflow-auto rounded-md border border-line">
          {events.map((event, index) => (
            <div
              key={`${event.ts ?? "event"}-${event.request_id ?? index}-${index}`}
              className="grid gap-1 border-b border-line px-3 py-2 text-xs last:border-b-0"
            >
              <div className="flex flex-wrap items-center gap-2">
                <CategoryPill category={event.category} />
                <span className="font-semibold">{event.event ?? "event"}</span>
                <span className="text-slate-500">{event.ts ?? ""}</span>
                {event.status !== null && event.status !== undefined && (
                  <span className="tabular-nums text-slate-600">HTTP {event.status}</span>
                )}
                {event.duration_ms !== null && event.duration_ms !== undefined && (
                  <span className="tabular-nums text-slate-600">{event.duration_ms} ms</span>
                )}
              </div>
              <div className="truncate text-slate-600">
                {[event.path, event.model, event.upstream, event.upstream_format]
                  .filter(Boolean)
                  .join(" | ")}
              </div>
              {(event.error || event.detail) && (
                <div className="truncate text-danger">
                  {[event.error, event.detail].filter(Boolean).join(": ")}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function SectionTitle({ subtitle, title }: { subtitle: string; title: string }) {
  return (
    <div>
      <h2 className="text-base font-semibold">{title}</h2>
      <p className="mt-1 text-sm text-slate-500">{subtitle}</p>
    </div>
  );
}

function Value({ value }: { value?: string | null }) {
  return (
    <span className={cx("truncate", value ? "text-slate-700" : "text-slate-400")}>
      {value || "-"}
    </span>
  );
}

function BooleanValue({ value }: { value?: boolean | null }) {
  if (value === null || value === undefined) {
    return (
      <span className="inline-flex items-center gap-1 text-slate-400">
        <CircleDashed size={13} />
        n/a
      </span>
    );
  }
  return value ? (
    <span className="inline-flex items-center gap-1 text-ok">
      <CheckCircle2 size={13} />
      OK
    </span>
  ) : (
    <span className="inline-flex items-center gap-1 text-danger">
      <AlertTriangle size={13} />
      Fail
    </span>
  );
}

function CategoryPill({ category }: { category: string }) {
  const tone =
    category === "codex_auth"
      ? "bg-amber-50 text-amber-700"
      : category === "external_upstream"
        ? "bg-red-50 text-danger"
        : category === "streaming"
          ? "bg-blue-50 text-action"
          : category === "tool_call_subagent"
            ? "bg-emerald-50 text-ok"
            : "bg-slate-100 text-slate-600";
  return (
    <span className={cx("rounded-md px-2 py-0.5 text-[11px] font-semibold", tone)}>
      {category}
    </span>
  );
}
