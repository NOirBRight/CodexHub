// PROTOTYPE — throwaway entry. Hi-fi connection-metaphor Gateway page (issue #438).
import { useState } from "react";
import { createRoot } from "react-dom/client";
import "../../index.css";
import { initialClients, stateLabel, type ConnState, type StubClient } from "./data";
import { ConnectionPage } from "./page";

function PrototypeApp() {
  const [clients, setClients] = useState<StubClient[]>(initialClients);
  const [toast, setToast] = useState<string | null>(null);

  const onToggle = (id: string) => {
    const target = clients.find((c) => c.id === id);
    if (!target || target.state === "busy" || target.state === "unavailable") return;
    const next: ConnState = target.state === "connected" ? "disconnected" : "connected";
    const wasDrift = target.state === "drift";
    setClients((cs) => cs.map((c) => (c.id === id ? { ...c, state: "busy" as ConnState } : c)));
    setTimeout(() => {
      setClients((cs) =>
        cs.map((c) =>
          c.id === id && c.state === "busy"
            ? { ...c, state: next, note: wasDrift && next === "connected" ? undefined : c.note }
            : c,
        ),
      );
      setToast(
        next === "connected"
          ? target.name + " connected — injected block written"
          : target.name + " disconnected — injected block removed",
      );
      setTimeout(() => setToast(null), 2600);
    }, 700);
  };

  return (
    <div>
      <div className="bg-warn/10 px-4 py-1 text-center text-xs text-warn">
        PROTOTYPE — connection-metaphor Client routing (issue #438) · stub data
      </div>
      <ConnectionPage clients={clients} onToggle={onToggle} />
      {toast && (
        <div className="fixed bottom-5 right-5 z-50 rounded-inner bg-ink px-4 py-2.5 text-xs font-medium text-white shadow-floating">
          {toast}
        </div>
      )}
      <div className="mx-auto mb-8 mt-2 max-w-md rounded-panel bg-panel px-4 py-2 font-mono text-[11px] text-muted">
        {clients.map((c) => (
          <div key={c.id}>{c.id}: {stateLabel[c.state]}{c.note ? " (" + c.note + ")" : ""}</div>
        ))}
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<PrototypeApp />);

