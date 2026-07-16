import { useCallback, useRef, useState } from "react";

export type ProviderDraftState<TDraft> = {
  providerId: string;
  draft: TDraft;
  dirty: boolean;
};

export type PendingProviderNavigation<TDraft, TAddForm> =
  | {
      kind: "existing";
      targetId: string;
      draft: TDraft;
    }
  | {
      kind: "add";
      targetId: string;
      form: TAddForm;
    };

type ProviderNavigationGuardOptions<TDraft, TAddForm> = {
  addId: string;
  form: TAddForm;
  initialSelectedId: string;
  isAddFormDirty: (form: TAddForm) => boolean;
  resetAddForm: () => void;
  saveAddForm: (form: TAddForm, targetId: string) => Promise<boolean>;
  saveExistingDraft: (draft: TDraft) => Promise<void>;
};

export function useProviderNavigationGuard<TDraft extends { id: string }, TAddForm>({
  addId,
  form,
  initialSelectedId,
  isAddFormDirty,
  resetAddForm,
  saveAddForm,
  saveExistingDraft,
}: ProviderNavigationGuardOptions<TDraft, TAddForm>) {
  const [selectedId, setSelectedId] = useState<string>(initialSelectedId);
  const dirtyProviderDraftRef = useRef<ProviderDraftState<TDraft> | null>(null);
  const [pendingProviderNavigation, setPendingProviderNavigation] =
    useState<PendingProviderNavigation<TDraft, TAddForm> | null>(null);

  const trackProviderDraft = useCallback((state: ProviderDraftState<TDraft>) => {
    if (!state.dirty) {
      if (dirtyProviderDraftRef.current?.providerId === state.providerId) {
        dirtyProviderDraftRef.current = null;
      }
      return;
    }
    dirtyProviderDraftRef.current = state;
  }, []);

  const selectProvider = useCallback(
    (id: string) => {
      if (id === selectedId) {
        return;
      }
      if (selectedId === addId) {
        if (isAddFormDirty(form)) {
          setPendingProviderNavigation({ kind: "add", targetId: id, form });
          return;
        }
        resetAddForm();
        setSelectedId(id);
        return;
      }
      const dirtyDraft = dirtyProviderDraftRef.current;
      if (dirtyDraft?.dirty && dirtyDraft.providerId === selectedId) {
        setPendingProviderNavigation({ kind: "existing", targetId: id, draft: dirtyDraft.draft });
        return;
      }
      setSelectedId(id);
    },
    [addId, form, isAddFormDirty, resetAddForm, selectedId],
  );

  const savePendingProviderNavigation = useCallback(async () => {
    const pending = pendingProviderNavigation;
    if (!pending) {
      return;
    }
    try {
      if (pending.kind === "existing") {
        await saveExistingDraft(pending.draft);
        dirtyProviderDraftRef.current = null;
      } else if (pending.kind === "add") {
        const added = await saveAddForm(pending.form, pending.targetId);
        if (!added) {
          return;
        }
      }
      setPendingProviderNavigation(null);
      setSelectedId(pending.targetId);
    } catch {
      // The caller's save path already surfaces the failure.
    }
  }, [pendingProviderNavigation, saveAddForm, saveExistingDraft]);

  const discardPendingProviderNavigation = useCallback(() => {
    const pending = pendingProviderNavigation;
    if (!pending) {
      return;
    }
    dirtyProviderDraftRef.current = null;
    if (pending.kind === "add") {
      resetAddForm();
    }
    setPendingProviderNavigation(null);
    setSelectedId(pending.targetId);
  }, [pendingProviderNavigation, resetAddForm]);

  const cancelPendingProviderNavigation = useCallback(() => {
    setPendingProviderNavigation(null);
  }, []);

  return {
    cancelPendingProviderNavigation,
    discardPendingProviderNavigation,
    pendingProviderNavigation,
    savePendingProviderNavigation,
    selectedId,
    selectProvider,
    setSelectedId,
    trackProviderDraft,
  };
}
