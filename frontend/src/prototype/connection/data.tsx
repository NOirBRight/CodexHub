// PROTOTYPE — throwaway stub data + shared controls for the connection-metaphor
// Gateway page (issue #438). Left column is a faithful static replica of the
// current page; only the Client routing column carries the new interaction.
import { AlertTriangle, Loader2 } from "lucide-react";
import codexLogo from "../../assets/codex-logo.svg";
import ompIcon from "../../assets/omp-icon.png";
import opencodeIcon from "../../assets/opencode-icon.png";
import piIcon from "../../assets/pi-icon.png";
import zcodeIcon from "../../assets/zcode-icon.png";

export type ConnState = "connected" | "disconnected" | "busy" | "drift" | "unavailable";

export interface StubClient {
  id: string;
  name: string;
  kind: string;
  version: string | null;
  configPath: string;
  qualified: boolean;
  state: ConnState;
  note?: string;
}

export function DshIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      <path d="M12 2.5 20 12l-8 9.5L4 12z" fill="#4D6BFE" />
      <path d="M12 7 16 12l-4 5-4-5z" fill="#fff" opacity="0.85" />
    </svg>
  );
}

export const clientIcons: Record<string, string> = {
  codex: codexLogo,
  opencode: opencodeIcon,
  zcode: zcodeIcon,
  pi: piIcon,
  omp: ompIcon,
};

export const initialClients: StubClient[] = [
  { id: "opencode", name: "OpenCode", kind: "Terminal client", version: "1.18.3", configPath: "C:\\Users\\noirb\\.config\\opencode\\opencode.json", qualified: true, state: "connected" },
  { id: "dsh", name: "DeepSeek Harness", kind: "Agent runtime", version: "0.1.0-rc.6", configPath: "C:\\Users\\noirb\\.dsh\\settings.yaml", qualified: true, state: "connected" },
  { id: "zcode", name: "ZCode", kind: "IDE extension", version: "2.4.1", configPath: "D:\\zcode\\.zcode\\v2\\config.json", qualified: true, state: "drift", note: "Injected block edited externally" },
  { id: "pi", name: "Pi", kind: "Compact CLI", version: null, configPath: "C:\\Users\\noirb\\.pi\\agent\\settings.json", qualified: false, state: "unavailable", note: "Not installed" },
  { id: "omp", name: "OMP", kind: "Prompt runtime", version: "0.9.2", configPath: "C:\\Users\\noirb\\.omp\\agent\\config.yml", qualified: true, state: "disconnected" },
];

export const stateLabel: Record<ConnState, string> = {
  connected: "Connected",
  disconnected: "Not connected",
  busy: "Working…",
  drift: "Config drift",
  unavailable: "Not installed",
};

/** Line segment; connected lines carry an animated flow overlay (CSS in main.tsx). */
export function Seg({ state }: { state: ConnState }) {
  if (state === "connected") return <div className="proto-flow h-0.5 flex-1 rounded-full bg-action" />;
  if (state === "busy") return <div className="h-0.5 flex-1 animate-pulse rounded-full bg-action/50" />;
  if (state === "drift") return <div className="h-0.5 flex-1 rounded-full bg-warn" />;
  return <div className="h-0 flex-1 border-t-2 border-dashed border-line" />;
}

export function ConnSwitch({ state, onToggle }: { state: ConnState; onToggle: () => void }) {
  const on = state === "connected" || state === "busy";
  const disabled = state === "unavailable" || state === "busy";
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      disabled={disabled}
      onClick={onToggle}
      className={[
        "relative z-10 inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-[background-color,box-shadow,opacity] duration-300",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action/40",
        disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer",
        on ? "bg-action shadow-control" : state === "drift" ? "bg-warn" : "bg-line",
      ].join(" ")}
    >
      {state === "busy" ? (
        <Loader2 className="absolute left-0.5 h-4 w-4 animate-spin text-white" />
      ) : (
        <span
          className={[
            "absolute left-0.5 top-0.5 h-4 w-4 transform rounded-full bg-white shadow-control transition-transform duration-300",
            on ? "translate-x-4" : "translate-x-0",
          ].join(" ")}
        />
      )}
    </button>
  );
}

export function ConnDot({ state }: { state: ConnState }) {
  const cls =
    state === "connected" ? "bg-ok" : state === "busy" ? "bg-action" : state === "drift" ? "bg-warn" : "bg-line";
  return <span className={"inline-block h-1.5 w-1.5 rounded-full " + cls} />;
}

export { AlertTriangle };
