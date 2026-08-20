import type {
  AppFlavorInfo,
  AppStatus,
  AppUpdateStatus,
  AppVersionInfo,
  GatewayClientInfo,
  GatewayEvent,
  GatewayStatus,
  GatewayUsageSnapshot,
  Model,
  Provider,
  Settings,
} from "./types";

export type RuntimeCache<T> = {
  data: T | null;
  loading: boolean;
  error: string | null;
  updatedAt: number | null;
  inflight?: Promise<T>;
};

export type RuntimeSnapshot = {
  status: RuntimeCache<AppStatus>;
  settings: RuntimeCache<Settings>;
  providers: RuntimeCache<Provider[]>;
  gatewayStatus: RuntimeCache<GatewayStatus>;
  gatewayUsageSnapshot: RuntimeCache<GatewayUsageSnapshot>;
  gatewayEvents: RuntimeCache<GatewayEvent[]>;
  gatewayClients: RuntimeCache<GatewayClientInfo[]>;
  catalogModels: RuntimeCache<Model[]>;
  modelMetadata: RuntimeCache<Model[]>;
  appFlavor: RuntimeCache<AppFlavorInfo>;
  appVersion: RuntimeCache<AppVersionInfo>;
  updateStatus: RuntimeCache<AppUpdateStatus>;
};

export type RuntimeCacheKey = keyof RuntimeSnapshot;
export type RuntimeData<K extends RuntimeCacheKey> = NonNullable<RuntimeSnapshot[K]["data"]>;

export function runtimeCache<T>(data: T | null = null): RuntimeCache<T> {
  return {
    data,
    loading: false,
    error: null,
    updatedAt: data === null ? null : Date.now(),
  };
}

export function createEmptyRuntimeSnapshot(): RuntimeSnapshot {
  return {
    status: runtimeCache<AppStatus>(),
    settings: runtimeCache<Settings>(),
    providers: runtimeCache<Provider[]>([]),
    gatewayStatus: runtimeCache<GatewayStatus>(),
    gatewayUsageSnapshot: runtimeCache<GatewayUsageSnapshot>(),
    gatewayEvents: runtimeCache<GatewayEvent[]>([]),
    gatewayClients: runtimeCache<GatewayClientInfo[]>([]),
    catalogModels: runtimeCache<Model[]>([]),
    modelMetadata: runtimeCache<Model[]>([]),
    appFlavor: runtimeCache<AppFlavorInfo>(),
    appVersion: runtimeCache<AppVersionInfo>(),
    updateStatus: runtimeCache<AppUpdateStatus>(),
  };
}

export function setCacheLoading(current: RuntimeSnapshot, key: RuntimeCacheKey): RuntimeSnapshot {
  const cache = current[key] as RuntimeCache<unknown>;
  return {
    ...current,
    [key]: {
      ...cache,
      loading: true,
      error: null,
    },
  } as RuntimeSnapshot;
}

export function setCacheData<K extends RuntimeCacheKey>(
  current: RuntimeSnapshot,
  key: K,
  data: RuntimeData<K>,
): RuntimeSnapshot {
  const cache = current[key] as RuntimeCache<RuntimeData<K>>;
  return {
    ...current,
    [key]: {
      ...cache,
      data,
      loading: false,
      error: null,
      updatedAt: Date.now(),
    },
  } as RuntimeSnapshot;
}

export function setCacheError(current: RuntimeSnapshot, key: RuntimeCacheKey, error: string): RuntimeSnapshot {
  const cache = current[key] as RuntimeCache<unknown>;
  return {
    ...current,
    [key]: {
      ...cache,
      data: key === "status" ? null : cache.data,
      loading: false,
      error,
    },
  } as RuntimeSnapshot;
}
