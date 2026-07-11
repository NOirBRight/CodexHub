import type { UnifiedHistoryResult } from "./types";

export type HistoryIssueKey =
  | "settings.historyManualExitRequired"
  | "settings.historyOperationTimedOut"
  | "settings.historyProviderConflict"
  | "settings.historyRepairInProgress"
  | "settings.historyRelaunchFailed"
  | "settings.historySeparatedDrift"
  | "settings.historyTakeoverRequired"
  | "settings.historyUnexpectedFailure";

export function historyIssueKey(result: UnifiedHistoryResult): HistoryIssueKey {
  switch (result.reason) {
    case "graceful_close_failed":
    case "background_processes_remain":
    case "codex_files_locked":
    case "codex_running":
      return "settings.historyManualExitRequired";
    case "helper_timeout":
    case "process_timeout":
      return "settings.historyOperationTimedOut";
    case "repair_in_progress":
      return "settings.historyRepairInProgress";
    case "relaunch_failed":
      return "settings.historyRelaunchFailed";
    case "unknown_custom_provider":
      return "settings.historyProviderConflict";
    case "separated_history_drift":
      return "settings.historySeparatedDrift";
    case "route_takeover_required":
      return "settings.historyTakeoverRequired";
    default:
      return "settings.historyUnexpectedFailure";
  }
}
