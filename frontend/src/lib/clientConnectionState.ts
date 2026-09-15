import type { GatewayClientInfo } from "./types";

export type ClientConnectionState =
  | "connected"
  | "disconnected"
  | "busy"
  | "drift"
  | "unavailable";

export function connectionStateFromInfo(
  info: GatewayClientInfo | undefined,
  busy?: boolean,
): ClientConnectionState {
  if (busy) {
    return "busy";
  }
  if (!info) {
    return "disconnected";
  }
  if (!info.installed) {
    return "unavailable";
  }
  if (info.route_mode === "stale") {
    return "drift";
  }
  if (
    info.route_mode === "other_channel" &&
    (info.route_owner === "release" || info.route_owner === "beta")
  ) {
    return "connected";
  }
  if (
    info.route_mode === "hub" ||
    info.route_mode === "release" ||
    info.route_mode === "beta"
  ) {
    return "connected";
  }
  return "disconnected";
}

export function switchCheckedFromState(state: ClientConnectionState): boolean {
  return state === "connected" || state === "busy";
}

export function clientIdFromBusyKey(busy: string): string {
  const id = busy.split(":")[0];
  return id || busy;
}

export function listReachedClientBusyTarget(
  busy: string,
  info: GatewayClientInfo | undefined,
): boolean {
  const state = connectionStateFromInfo(info);
  if (busy.endsWith(":disconnect") || busy.endsWith(":switch:official")) {
    return state === "disconnected" || state === "unavailable";
  }
  if (busy.includes(":switch:") || busy.endsWith(":connect")) {
    return state === "connected";
  }
  return false;
}
