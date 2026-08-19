export type RestartTarget =
  | { kind: "none" }
  | { kind: "runtime"; name: "Gateway" | "CodexHub" }
  | { kind: "client"; name: string }
  | { kind: "codex-app" }
  | { kind: "clients"; names: string[] };

export type ToastPatch = {
  action?: { label: string; onClick: () => void } | null;
  text?: string;
  timeoutMs?: number | null;
  tone?: "message" | "info" | "success" | "error" | "loading";
};

export type PersistentActionDeps = {
  showToast: (input: {
    text: string;
    tone: "loading";
    timeoutMs: null;
    dedupeKey?: string;
  }) => string;
  updateToast: (id: string, patch: ToastPatch) => void;
  disconnected?: "start-gateway";
  onStartGateway?: () => Promise<void> | void;
};

export function formatRestartDisclosure(target: RestartTarget): string {
  switch (target.kind) {
    case "none":
      return "No restart required.";
    case "runtime":
      return `Restart ${target.name}.`;
    case "client":
      return `Restart ${target.name}.`;
    case "codex-app":
      return "Restart Codex App.";
    case "clients":
      return `Restart ${target.names.join(", ")}.`;
  }
}

function isBackendDisconnected(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error ?? "");
  return /Failed to connect to the CodexHub backend/i.test(message);
}

export async function runPersistentAction<T>(
  spec: PersistentActionDeps & {
    dedupeKey?: string;
    loading: string;
    work: () => Promise<T>;
    success: (result: T) => { text: string; restart: RestartTarget };
  },
): Promise<T> {
  const toastId = spec.showToast({
    text: spec.loading,
    tone: "loading",
    timeoutMs: null,
    dedupeKey: spec.dedupeKey,
  });
  try {
    const result = await spec.work();
    const completed = spec.success(result);
    spec.updateToast(toastId, {
      tone: "success",
      text: `${completed.text} ${formatRestartDisclosure(completed.restart)}`.trim(),
      action: null,
    });
    return result;
  } catch (error) {
    const disconnected = spec.disconnected === "start-gateway" && isBackendDisconnected(error);
    spec.updateToast(toastId, {
      tone: "error",
      text: error instanceof Error ? error.message : String(error),
      action:
        disconnected && spec.onStartGateway
          ? {
              label: "Start Gateway",
              onClick: () => {
                void spec.onStartGateway?.();
              },
            }
          : null,
    });
    throw error;
  }
}
