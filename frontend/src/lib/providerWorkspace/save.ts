/** One save owns persistence, publication, retry and the right to navigate. */
export function createWorkspaceSaveCoordinator() {
  let busy = false;
  let generation = 0;
  let retireRetry: (() => void) | undefined;
  return {
    get busy() { return busy; },
    async save<T>(operation: {
      persist: () => Promise<T>;
      committed: (value: T) => void;
      publish?: () => Promise<void>;
      sync?: () => Promise<void>;
      feedback: (result: { stage: "saving" | "publishing" | "syncing" | "complete" | "error"; saved: boolean; error?: unknown; retry?: () => Promise<void> }) => void;
      setBusy: (value: boolean) => void;
    }): Promise<{ kind: "ok"; value: T } | { kind: "blocked"; reason: string } | { kind: "error"; error: unknown }> {
      if (busy) return { kind: "blocked", reason: "save-in-progress" };
      busy = true;
      let current = generation;
      operation.setBusy(true);
      let saved = false;
      let stage: "saving" | "publishing" | "syncing" = "saving";
      const complete = async () => {
        try {
          if (stage === "publishing") {
            operation.feedback({ stage, saved });
            await operation.publish?.();
            stage = "syncing";
          }
          if (stage === "syncing") {
            operation.feedback({ stage, saved });
            await operation.sync?.();
          }
          operation.feedback({ stage: "complete", saved });
          retireRetry = undefined;
        } catch (error) {
          const retry = async () => {
            if (busy || current !== generation) return;
            busy = true;
            operation.setBusy(true);
            try { await complete(); }
            finally { busy = false; operation.setBusy(false); }
          };
          operation.feedback({ stage: "error", saved, error, retry });
          retireRetry = () => operation.feedback({ stage: "error", saved, error });
        }
      };
      try {
        operation.feedback({ stage, saved });
        const value = await operation.persist();
        saved = true;
        current = ++generation;
        retireRetry?.();
        retireRetry = undefined;
        operation.committed(value);
        stage = "publishing";
        await complete();
        return { kind: "ok", value };
      } catch (error) {
        operation.feedback({ stage: "error", saved, error });
        return { kind: "error", error };
      } finally {
        busy = false;
        operation.setBusy(false);
      }
    },
  };
}
