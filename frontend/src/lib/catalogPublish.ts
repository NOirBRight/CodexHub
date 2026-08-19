export type CatalogPublishRequest = {
  reason: string;
  persist: boolean;
  syncClients: boolean;
};

export type CatalogPublishAdapters<TSync> = {
  generate: () => Promise<unknown>;
  sync: () => Promise<TSync>;
};

export async function publishCatalog<TSync>(
  request: CatalogPublishRequest,
  adapters: CatalogPublishAdapters<TSync>,
): Promise<{ syncResult: TSync | null }> {
  if (request.persist) {
    await adapters.generate();
  }
  if (!request.syncClients) {
    return { syncResult: null };
  }
  return { syncResult: await adapters.sync() };
}
