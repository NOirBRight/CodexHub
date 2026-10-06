import type { Provider } from "./types";

export const SYSTEM_CONTEXT_CONSENT = "user-context-v1";

export function isCliSubscription(provider: Pick<Provider, "id">): boolean {
  return provider.id === "cursor-subscription" || provider.id === "claude-subscription";
}

export interface CliSubscriptionStatus {
  provider_id: string;
  state: string;
  cli_version: string | null;
  generation_qualified: false;
  models: { id: string; display_name: string }[];
}

export function canEnableCliSubscription(provider: Provider): boolean {
  return provider.id !== "claude-subscription" || provider.system_context_consent === SYSTEM_CONTEXT_CONSENT;
}
