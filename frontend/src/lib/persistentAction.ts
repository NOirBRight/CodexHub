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
  disconnectedText?: string;
  formatRestart?: (target: RestartTarget) => string;
  isDisconnected?: (error: unknown) => boolean;
  onStartGateway?: (toastId?: string) => Promise<void> | void;
  startGatewayLabel?: string;
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
    success: (result: T) => {
      text: string;
      restart: RestartTarget;
      tone?: "info" | "success";
    };
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
    const restartText = (spec.formatRestart ?? formatRestartDisclosure)(completed.restart);
    spec.updateToast(toastId, {
      tone: completed.tone ?? "success",
      timeoutMs: 3000,
      text: (completed.text + " " + restartText).trim(),
      action: null,
    });
    return result;
  } catch (error) {
    const disconnected =
      spec.disconnected === "start-gateway" &&
      (spec.isDisconnected ?? isBackendDisconnected)(error);
    const errorText = error instanceof Error ? error.message : String(error);
    spec.updateToast(toastId, {
      tone: "error",
      timeoutMs: disconnected ? null : 5000,
      text: disconnected ? spec.disconnectedText ?? errorText : errorText,
      action:
        disconnected && spec.onStartGateway
          ? {
              label: spec.startGatewayLabel ?? "Start Gateway",
              onClick: () => {
                void spec.onStartGateway?.(toastId);
              },
            }
          : null,
    });
    throw error;
  }
}
