use crate::config::{self, CommandRunner, ConfigPaths, ProcessCommandRunner};
use crate::safe_file;
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

const GRACEFUL_CLOSE_TIMEOUT_SECONDS: u64 = 10;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum UnifiedHistoryStatus {
    Clean,
    Repaired,
    RestartRequired,
    Conflict,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct UnifiedHistoryResult {
    pub status: UnifiedHistoryStatus,
    pub changed_rows: usize,
    pub changed_files: usize,
    pub backup_path: Option<String>,
    pub receipt_path: Option<String>,
    pub reason: Option<String>,
    pub error: Option<String>,
    pub codex_restarted: bool,
}

impl UnifiedHistoryResult {
    fn clean(reason: Option<&str>) -> Self {
        Self {
            status: UnifiedHistoryStatus::Clean,
            changed_rows: 0,
            changed_files: 0,
            backup_path: None,
            receipt_path: None,
            reason: reason.map(str::to_string),
            error: None,
            codex_restarted: false,
        }
    }

    fn pending(status: UnifiedHistoryStatus, reason: &str) -> Self {
        Self {
            status,
            changed_rows: 0,
            changed_files: 0,
            backup_path: None,
            receipt_path: None,
            reason: Some(reason.to_string()),
            error: None,
            codex_restarted: false,
        }
    }

    fn failed(error: String, backup_path: &Path) -> Self {
        Self {
            status: UnifiedHistoryStatus::Conflict,
            changed_rows: 0,
            changed_files: 0,
            backup_path: Some(backup_path.to_string_lossy().into_owned()),
            receipt_path: None,
            reason: Some("repair_failed".to_string()),
            error: Some(error),
            codex_restarted: false,
        }
    }
}

#[derive(Debug, Deserialize)]
struct ConfigInspection {
    status: InspectionStatus,
}

#[derive(Debug, Deserialize)]
struct BucketInspection {
    status: InspectionStatus,
    #[serde(default)]
    dirty_state_rows: usize,
    #[serde(default)]
    dirty_state_files: usize,
    #[serde(default)]
    dirty_jsonl_files: usize,
}

#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum InspectionStatus {
    Clean,
    NeedsRepair,
    Conflict,
    GatewayActive,
}

trait CodexAppController {
    fn is_running(&self) -> Result<bool, String>;
    fn close_gracefully(&self, timeout_seconds: u64) -> Result<bool, String>;
    fn launch(&self) -> Result<(), String>;
}

struct SystemCodexAppController;

#[derive(Debug, Clone, Copy)]
enum HistoryBucketTarget {
    Unified,
    Separated,
}

impl HistoryBucketTarget {
    fn from_unified(unified: bool) -> Self {
        if unified {
            Self::Unified
        } else {
            Self::Separated
        }
    }

    fn is_unified(self) -> bool {
        matches!(self, Self::Unified)
    }

    fn config_name(self) -> &'static str {
        match self {
            Self::Unified => "unified",
            Self::Separated => "separated",
        }
    }

    fn provider_name(self) -> &'static str {
        match self {
            Self::Unified => "custom",
            Self::Separated => "openai",
        }
    }
}

#[derive(Debug, Clone, Copy)]
enum PreflightTarget {
    Startup(HistoryBucketTarget),
    Explicit(HistoryBucketTarget),
}

impl PreflightTarget {
    fn bucket(self) -> HistoryBucketTarget {
        match self {
            Self::Startup(target) | Self::Explicit(target) => target,
        }
    }
}

#[derive(Debug, Clone, Copy)]
struct PreflightRequest {
    target: PreflightTarget,
    request_restart: bool,
}

struct HistoryRepairPlan<'a> {
    target: HistoryBucketTarget,
    repair_config: bool,
    repair_bucket: bool,
    backup_root: &'a Path,
    inspection: &'a BucketInspection,
}

struct AppliedRepairRollback<'a> {
    repair_config: bool,
    repair_bucket: bool,
    backup_root: &'a Path,
    config_snapshot: &'a Path,
    config_existed: bool,
}

pub fn preflight_unified_history(
    request_restart: bool,
    target_unified: Option<bool>,
) -> Result<UnifiedHistoryResult, String> {
    let settings = config::get_settings()?;
    let paths = ConfigPaths::runtime()?;
    let python = config::find_python();
    let runner = ProcessCommandRunner;
    let controller = SystemCodexAppController;
    preflight_unified_history_with_paths(
        PreflightRequest {
            target: match target_unified {
                Some(unified) => {
                    PreflightTarget::Explicit(HistoryBucketTarget::from_unified(unified))
                }
                None => PreflightTarget::Startup(HistoryBucketTarget::from_unified(
                    settings.unified_codex_history,
                )),
            },
            request_restart,
        },
        &paths,
        &python,
        &runner,
        &controller,
    )
}

fn preflight_unified_history_with_paths(
    request: PreflightRequest,
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
    controller: &dyn CodexAppController,
) -> Result<UnifiedHistoryResult, String> {
    let target = request.target.bucket();
    let startup_separated = matches!(
        request.target,
        PreflightTarget::Startup(HistoryBucketTarget::Separated)
    );

    let config_inspection: ConfigInspection = inspect_json(
        "unified history config inspection",
        python,
        paths.config_overlay_script(),
        vec![
            "inspect-unified".to_string(),
            "--config".to_string(),
            paths.codex_config_path().to_string_lossy().into_owned(),
            "--target".to_string(),
            target.config_name().to_string(),
        ],
        runner,
    )?;
    let bucket_inspection: BucketInspection = inspect_json(
        "unified history bucket inspection",
        python,
        paths.history_overlay_script(),
        {
            let mut args = vec![
                "inspect-unified".to_string(),
                "--codex-dir".to_string(),
                paths.codex_dir().to_string_lossy().into_owned(),
                "--target".to_string(),
                target.provider_name().to_string(),
            ];
            if !target.is_unified() {
                args.extend([
                    "--ledger-root".to_string(),
                    paths.proxy_dir().to_string_lossy().into_owned(),
                ]);
            }
            args
        },
        runner,
    )?;

    if config_inspection.status == InspectionStatus::Conflict {
        return Ok(UnifiedHistoryResult::pending(
            UnifiedHistoryStatus::Conflict,
            "unknown_custom_provider",
        ));
    }

    let repair_config = config_inspection.status == InspectionStatus::NeedsRepair;
    let repair_bucket = bucket_inspection.status == InspectionStatus::NeedsRepair;
    if !repair_config && !repair_bucket {
        return Ok(UnifiedHistoryResult::clean(
            startup_separated.then_some("unified_history_disabled"),
        ));
    }
    if startup_separated {
        return Ok(UnifiedHistoryResult::pending(
            UnifiedHistoryStatus::Conflict,
            "separated_history_drift",
        ));
    }

    let was_running = controller.is_running()?;
    if was_running && !request.request_restart {
        return Ok(UnifiedHistoryResult::pending(
            UnifiedHistoryStatus::RestartRequired,
            "codex_running",
        ));
    }
    if was_running && !controller.close_gracefully(GRACEFUL_CLOSE_TIMEOUT_SECONDS)? {
        return Ok(UnifiedHistoryResult::pending(
            UnifiedHistoryStatus::RestartRequired,
            "graceful_close_failed",
        ));
    }

    fs::create_dir_all(paths.proxy_dir()).map_err(|error| {
        format!(
            "failed to create unified history repair directory {}: {error}",
            paths.proxy_dir().display()
        )
    })?;
    let backup_root = history_backup_root(paths, "history-startup-repair");
    let repair_result = repair_unified_history(
        HistoryRepairPlan {
            target,
            repair_config,
            repair_bucket,
            backup_root: &backup_root,
            inspection: &bucket_inspection,
        },
        paths,
        python,
        runner,
    );

    let mut result = match repair_result {
        Ok(result) => result,
        Err(error) => {
            let mut result = UnifiedHistoryResult::failed(error, &backup_root);
            if was_running {
                match controller.launch() {
                    Ok(()) => result.codex_restarted = true,
                    Err(launch_error) => {
                        result.error = Some(format!(
                            "{}; failed to restart Codex App: {launch_error}",
                            result.error.as_deref().unwrap_or("repair failed")
                        ));
                    }
                }
            }
            return Ok(result);
        }
    };

    if was_running {
        controller.launch()?;
        result.codex_restarted = true;
    }
    Ok(result)
}

fn repair_unified_history(
    plan: HistoryRepairPlan<'_>,
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
) -> Result<UnifiedHistoryResult, String> {
    let HistoryRepairPlan {
        target,
        repair_config,
        repair_bucket,
        backup_root,
        inspection,
    } = plan;
    fs::create_dir_all(backup_root).map_err(|error| {
        format!(
            "failed to create unified history backup directory {}: {error}",
            backup_root.display()
        )
    })?;
    let config_existed = paths.codex_config_path().exists();
    let config_snapshot = backup_root.join("config.toml.before-repair");
    let mut changed_files = 0usize;
    if repair_config {
        if config_existed {
            fs::copy(paths.codex_config_path(), &config_snapshot).map_err(|error| {
                format!("failed to back up Codex config before repair: {error}")
            })?;
        }
        let mut args = vec![
            "restore".to_string(),
            "--config".to_string(),
            paths.codex_config_path().to_string_lossy().into_owned(),
            "--backup".to_string(),
            paths.config_backup_path().to_string_lossy().into_owned(),
        ];
        if target.is_unified() {
            args.push("--unified-history".to_string());
        }
        if let Err(error) = config::run_python_script(
            "unified history config repair",
            python,
            paths.config_overlay_script(),
            args,
            runner,
        ) {
            rollback_config_repair(paths, &config_snapshot, config_existed)?;
            return Err(error);
        }
        changed_files += 1;
    }

    let mut changed_rows = 0usize;
    if repair_bucket {
        let mut args = vec![
            if target.is_unified() {
                "migrate-official-to-unified".to_string()
            } else {
                "restore-official-from-unified".to_string()
            },
            "--codex-dir".to_string(),
            paths.codex_dir().to_string_lossy().into_owned(),
            "--backup-root".to_string(),
            backup_root.to_string_lossy().into_owned(),
        ];
        if !target.is_unified() {
            args.extend([
                "--ledger-root".to_string(),
                paths.proxy_dir().to_string_lossy().into_owned(),
            ]);
        }
        let outcome = config::run_python_script(
            "unified history bucket repair",
            python,
            paths.history_overlay_script(),
            args,
            runner,
        );
        let outcome = match outcome {
            Ok(outcome) => outcome,
            Err(error) => {
                if repair_config {
                    rollback_config_repair(paths, &config_snapshot, config_existed)?;
                }
                return Err(error);
            }
        };
        let values = parse_key_value_output(&outcome.stdout);
        changed_rows = values
            .get("state_rows")
            .copied()
            .unwrap_or(inspection.dirty_state_rows)
            + values.get("state_model_rows").copied().unwrap_or(0);
        changed_files += inspection.dirty_state_files;
        changed_files += values
            .get("jsonl_applied")
            .or_else(|| values.get("jsonl_restored"))
            .copied()
            .unwrap_or(inspection.dirty_jsonl_files);
    }

    let rollback = AppliedRepairRollback {
        repair_config,
        repair_bucket,
        backup_root,
        config_snapshot: &config_snapshot,
        config_existed,
    };
    let receipt_dir = paths.proxy_dir().join("migrations");
    let receipt_path = receipt_dir.join("unified-history-last.json");
    let result = UnifiedHistoryResult {
        status: UnifiedHistoryStatus::Repaired,
        changed_rows,
        changed_files,
        backup_path: Some(backup_root.to_string_lossy().into_owned()),
        receipt_path: Some(receipt_path.to_string_lossy().into_owned()),
        reason: None,
        error: None,
        codex_restarted: false,
    };
    let receipt_result = (|| {
        fs::create_dir_all(&receipt_dir).map_err(|error| {
            format!(
                "failed to create migration receipt directory {}: {error}",
                receipt_dir.display()
            )
        })?;
        let receipt = serde_json::to_string_pretty(&result)
            .map_err(|error| format!("failed to serialize unified history receipt: {error}"))?;
        safe_file::write_text_atomic(&receipt_path, &format!("{receipt}\n")).map_err(|error| {
            format!(
                "failed to write unified history receipt {}: {error}",
                receipt_path.display()
            )
        })
    })();
    if let Err(error) = receipt_result {
        let rollback_error = rollback_applied_repair(rollback, paths, python, runner).err();
        return Err(match rollback_error {
            Some(rollback_error) => {
                format!("{error}; repair rollback also failed: {rollback_error}")
            }
            None => error,
        });
    }
    Ok(result)
}

fn rollback_applied_repair(
    rollback: AppliedRepairRollback<'_>,
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
) -> Result<(), String> {
    let mut errors = Vec::new();
    if rollback.repair_bucket {
        if let Err(error) = config::run_python_script(
            "unified history repair rollback",
            python,
            paths.history_overlay_script(),
            vec![
                "rollback-repair".to_string(),
                "--codex-dir".to_string(),
                paths.codex_dir().to_string_lossy().into_owned(),
                "--backup-root".to_string(),
                rollback.backup_root.to_string_lossy().into_owned(),
            ],
            runner,
        ) {
            errors.push(error);
        }
    }
    if rollback.repair_config {
        if let Err(error) =
            rollback_config_repair(paths, rollback.config_snapshot, rollback.config_existed)
        {
            errors.push(error);
        }
    }
    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors.join("; "))
    }
}

fn rollback_config_repair(
    paths: &ConfigPaths,
    snapshot: &Path,
    config_existed: bool,
) -> Result<(), String> {
    if config_existed {
        fs::copy(snapshot, paths.codex_config_path())
            .map(|_| ())
            .map_err(|error| {
                format!("failed to roll back Codex config after repair error: {error}")
            })
    } else if paths.codex_config_path().exists() {
        fs::remove_file(paths.codex_config_path())
            .map_err(|error| format!("failed to remove repaired Codex config after error: {error}"))
    } else {
        Ok(())
    }
}

fn inspect_json<T: for<'de> Deserialize<'de>>(
    label: &str,
    python: &Path,
    script: PathBuf,
    args: Vec<String>,
    runner: &dyn CommandRunner,
) -> Result<T, String> {
    let outcome = config::run_python_script(label, python, script, args, runner)?;
    serde_json::from_str(outcome.stdout.trim())
        .map_err(|error| format!("{label} returned invalid JSON: {error}"))
}

fn parse_key_value_output(output: &str) -> std::collections::HashMap<String, usize> {
    output
        .lines()
        .filter_map(|line| line.split_once('='))
        .filter_map(|(key, value)| {
            value
                .trim()
                .parse()
                .ok()
                .map(|value| (key.to_string(), value))
        })
        .collect()
}

#[cfg(target_os = "windows")]
impl CodexAppController for SystemCodexAppController {
    fn is_running(&self) -> Result<bool, String> {
        let output = run_powershell(
            "if (Get-Process -Name Codex -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }",
        )?;
        Ok(output.code == Some(0))
    }

    fn close_gracefully(&self, timeout_seconds: u64) -> Result<bool, String> {
        let script = format!(
            "$ErrorActionPreference='Stop'; $p=@(Get-Process -Name Codex -ErrorAction SilentlyContinue); if (-not $p) {{ exit 0 }}; $p | ForEach-Object {{ [void]$_.CloseMainWindow() }}; $deadline=(Get-Date).AddSeconds({timeout_seconds}); do {{ Start-Sleep -Milliseconds 200; $p=@(Get-Process -Name Codex -ErrorAction SilentlyContinue) }} while ($p -and (Get-Date) -lt $deadline); if ($p) {{ exit 2 }}"
        );
        let output = run_powershell(&script)?;
        Ok(output.code == Some(0))
    }

    fn launch(&self) -> Result<(), String> {
        let script = "$ErrorActionPreference='Stop'; $app=Get-StartApps | Where-Object { $_.AppID -like 'OpenAI.Codex_*' -or $_.Name -eq 'Codex' } | Select-Object -First 1; if (-not $app) { throw 'Codex App is not installed.' }; Start-Process ('shell:AppsFolder\\' + $app.AppID)";
        let output = run_powershell(script)?;
        if output.code == Some(0) {
            Ok(())
        } else {
            Err(format!(
                "failed to restart Codex App: {}",
                output.stderr.trim()
            ))
        }
    }
}

#[cfg(target_os = "windows")]
fn run_powershell(script: &str) -> Result<config::CommandOutcome, String> {
    let runner = ProcessCommandRunner;
    runner.run(
        Path::new("powershell"),
        &[
            "-NoProfile".to_string(),
            "-ExecutionPolicy".to_string(),
            "Bypass".to_string(),
            "-Command".to_string(),
            script.to_string(),
        ],
    )
}

#[cfg(not(target_os = "windows"))]
impl CodexAppController for SystemCodexAppController {
    fn is_running(&self) -> Result<bool, String> {
        Ok(false)
    }
    fn close_gracefully(&self, _timeout_seconds: u64) -> Result<bool, String> {
        Ok(true)
    }
    fn launch(&self) -> Result<(), String> {
        Err("Codex App restart is supported on Windows only".to_string())
    }
}

pub fn sync_history(target_provider: Option<&str>) -> Result<String, String> {
    let target_unified = target_unified_from_provider(target_provider)?;
    legacy_reconcile_message(
        preflight_unified_history(true, Some(target_unified))?,
        if target_unified { "custom" } else { "openai" },
    )
}

pub fn reconcile_after_route_switch(
    target_provider: Option<&str>,
) -> Result<UnifiedHistoryResult, String> {
    let target_unified = target_unified_from_provider(target_provider)?;
    let paths = ConfigPaths::runtime()?;
    let python = config::find_python();
    let runner = ProcessCommandRunner;
    let controller = SystemCodexAppController;
    reconcile_after_route_switch_with_paths(
        HistoryBucketTarget::from_unified(target_unified),
        &paths,
        &python,
        &runner,
        &controller,
    )
}

fn target_unified_from_provider(target_provider: Option<&str>) -> Result<bool, String> {
    match target_provider {
        None | Some("custom") => Ok(true),
        Some("openai") => Ok(false),
        Some(value) => Err(format!(
            "unsupported history repair target: {value}; expected custom or openai"
        )),
    }
}

fn reconcile_after_route_switch_with_paths(
    target: HistoryBucketTarget,
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
    controller: &dyn CodexAppController,
) -> Result<UnifiedHistoryResult, String> {
    let mut result = preflight_unified_history_with_paths(
        PreflightRequest {
            target: PreflightTarget::Explicit(target),
            request_restart: true,
        },
        paths,
        python,
        runner,
        controller,
    )?;
    if result.status != UnifiedHistoryStatus::Clean || !controller.is_running()? {
        return Ok(result);
    }
    if !controller.close_gracefully(GRACEFUL_CLOSE_TIMEOUT_SECONDS)? {
        return Ok(UnifiedHistoryResult::pending(
            UnifiedHistoryStatus::RestartRequired,
            "graceful_close_failed",
        ));
    }
    controller.launch()?;
    result.codex_restarted = true;
    Ok(result)
}

fn legacy_reconcile_message(
    result: UnifiedHistoryResult,
    target_provider: &str,
) -> Result<String, String> {
    match result.status {
        UnifiedHistoryStatus::Clean => Ok(if result.codex_restarted {
            format!("History bucket is already clean for {target_provider}; Codex App restarted to load the new route")
        } else {
            format!("History bucket is already clean for {target_provider}")
        }),
        UnifiedHistoryStatus::Repaired => Ok(format!(
            "History bucket repair completed for {target_provider}; changed rows: {}; changed files: {}; backup root: {}",
            result.changed_rows,
            result.changed_files,
            result.backup_path.as_deref().unwrap_or("not required")
        )),
        UnifiedHistoryStatus::RestartRequired | UnifiedHistoryStatus::Conflict => Err(result
            .error
            .or(result.reason)
            .unwrap_or_else(|| "history reconciliation did not complete".to_string())),
    }
}

#[cfg(test)]
fn sync_history_with_paths(
    target_provider: Option<&str>,
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
) -> Result<String, String> {
    let target_provider = match target_provider {
        None | Some("custom") => "custom",
        Some("openai") => "openai",
        Some(value) => {
            return Err(format!(
                "unsupported history repair target: {value}; expected custom or openai"
            ))
        }
    };
    fs::create_dir_all(paths.proxy_dir()).map_err(|error| {
        format!(
            "failed to create history backup parent {}: {error}",
            paths.proxy_dir().display()
        )
    })?;

    let backup_root = history_manual_backup_root(paths);
    let outcome = config::run_python_script(
        "history bucket repair",
        python,
        paths.history_overlay_script(),
        vec![
            "repair-history".to_string(),
            "--codex-dir".to_string(),
            paths.codex_dir().to_string_lossy().into_owned(),
            "--backup-root".to_string(),
            backup_root.to_string_lossy().into_owned(),
            "--target".to_string(),
            target_provider.to_string(),
            "--ledger-root".to_string(),
            paths.proxy_dir().to_string_lossy().into_owned(),
        ],
        runner,
    )?;

    let stdout = outcome.stdout.trim();
    let mut message = format!(
        "History bucket repair completed for {target_provider}; backup root: {}",
        backup_root.display()
    );
    if !stdout.is_empty() {
        message.push('\n');
        message.push_str(stdout);
    }

    Ok(message)
}

pub fn migrate_official_history_to_unified() -> Result<String, String> {
    sync_history(Some("custom"))
}

#[cfg(test)]
fn migrate_official_history_to_unified_with_paths(
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
) -> Result<String, String> {
    fs::create_dir_all(paths.proxy_dir()).map_err(|error| {
        format!(
            "failed to create history backup parent {}: {error}",
            paths.proxy_dir().display()
        )
    })?;
    let backup_root = history_backup_root(paths, "history-official-to-unified");
    let outcome = config::run_python_script(
        "official history migration",
        python,
        paths.history_overlay_script(),
        vec![
            "migrate-official-to-unified".to_string(),
            "--codex-dir".to_string(),
            paths.codex_dir().to_string_lossy().into_owned(),
            "--backup-root".to_string(),
            backup_root.to_string_lossy().into_owned(),
        ],
        runner,
    )?;
    let stdout = outcome.stdout.trim();
    let mut message = format!(
        "Official history migration completed; backup root: {}",
        backup_root.display()
    );
    if !stdout.is_empty() {
        message.push('\n');
        message.push_str(stdout);
    }
    Ok(message)
}

pub fn restore_official_history_from_unified() -> Result<String, String> {
    sync_history(Some("openai"))
}

#[cfg(test)]
fn restore_official_history_from_unified_with_paths(
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
) -> Result<String, String> {
    fs::create_dir_all(paths.proxy_dir()).map_err(|error| {
        format!(
            "failed to create history backup parent {}: {error}",
            paths.proxy_dir().display()
        )
    })?;
    let backup_root = history_backup_root(paths, "history-unified-to-official");
    let outcome = config::run_python_script(
        "official history restore",
        python,
        paths.history_overlay_script(),
        vec![
            "restore-official-from-unified".to_string(),
            "--codex-dir".to_string(),
            paths.codex_dir().to_string_lossy().into_owned(),
            "--backup-root".to_string(),
            backup_root.to_string_lossy().into_owned(),
            "--ledger-root".to_string(),
            paths.proxy_dir().to_string_lossy().into_owned(),
        ],
        runner,
    )?;
    let stdout = outcome.stdout.trim();
    let mut message = format!(
        "Official history restore completed; backup root: {}",
        backup_root.display()
    );
    if !stdout.is_empty() {
        message.push('\n');
        message.push_str(stdout);
    }
    Ok(message)
}

#[cfg(test)]
fn history_manual_backup_root(paths: &ConfigPaths) -> PathBuf {
    history_backup_root(paths, "history-bucket-repair")
}

fn history_backup_root(paths: &ConfigPaths, prefix: &str) -> PathBuf {
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or_default();

    paths.proxy_dir().join(format!("{prefix}-{stamp}"))
}

#[cfg(test)]
mod tests {
    use super::{
        migrate_official_history_to_unified_with_paths, preflight_unified_history_with_paths,
        reconcile_after_route_switch_with_paths, restore_official_history_from_unified_with_paths,
        sync_history_with_paths, CodexAppController, HistoryBucketTarget, PreflightRequest,
        PreflightTarget, UnifiedHistoryStatus, GRACEFUL_CLOSE_TIMEOUT_SECONDS,
    };
    use crate::config::{CommandOutcome, CommandRunner, ConfigPaths};
    use std::cell::{Cell, RefCell};
    use std::collections::VecDeque;
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn startup_preflight_is_read_only_while_codex_is_running() {
        let root = temp_root("preflight-running");
        let paths = test_paths(&root);
        let runner = SequenceRunner::successful([
            r#"{"status":"needs_repair"}"#,
            r#"{"status":"needs_repair","dirty_state_rows":2,"dirty_state_files":1,"dirty_jsonl_files":1}"#,
        ]);
        let controller = RecordingCodexController::running();

        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Startup(HistoryBucketTarget::Unified),
                request_restart: false,
            },
            &paths,
            Path::new("python-test"),
            &runner,
            &controller,
        )
        .expect("preflight result");

        assert_eq!(result.status, UnifiedHistoryStatus::RestartRequired);
        assert_eq!(result.reason.as_deref(), Some("codex_running"));
        assert_eq!(runner.commands.borrow().len(), 2);
        assert_eq!(controller.close_calls.get(), 0);
        assert!(!paths.proxy_dir().join("migrations").exists());
    }

    #[test]
    fn route_switch_restarts_running_codex_even_when_history_is_clean() {
        let root = temp_root("route-switch-clean");
        let paths = test_paths(&root);
        let runner = SequenceRunner::successful([
            r#"{"status":"clean"}"#,
            r#"{"status":"clean","dirty_state_rows":0,"dirty_jsonl_files":0}"#,
        ]);
        let controller = RecordingCodexController::running();

        let result = reconcile_after_route_switch_with_paths(
            HistoryBucketTarget::Unified,
            &paths,
            Path::new("python-test"),
            &runner,
            &controller,
        )
        .expect("route switch reconciliation");

        assert_eq!(result.status, UnifiedHistoryStatus::Clean);
        assert!(result.codex_restarted);
        assert_eq!(controller.close_calls.get(), 1);
        assert_eq!(controller.launch_calls.get(), 1);
        assert_eq!(runner.commands.borrow().len(), 2);
    }

    #[test]
    fn requested_repair_never_mutates_when_graceful_close_fails() {
        let root = temp_root("preflight-close-failed");
        let paths = test_paths(&root);
        let runner = SequenceRunner::successful([
            r#"{"status":"clean"}"#,
            r#"{"status":"needs_repair","dirty_state_rows":1,"dirty_jsonl_files":0}"#,
        ]);
        let controller = RecordingCodexController::running_with_close_result(false);

        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Explicit(HistoryBucketTarget::Unified),
                request_restart: true,
            },
            &paths,
            Path::new("python-test"),
            &runner,
            &controller,
        )
        .expect("preflight result");

        assert_eq!(result.status, UnifiedHistoryStatus::RestartRequired);
        assert_eq!(result.reason.as_deref(), Some("graceful_close_failed"));
        assert_eq!(runner.commands.borrow().len(), 2);
        assert_eq!(controller.close_calls.get(), 1);
        assert_eq!(controller.launch_calls.get(), 0);
    }

    #[test]
    fn preflight_repairs_config_and_history_then_writes_receipt() {
        let root = temp_root("preflight-repair");
        let paths = test_paths(&root);
        let runner = SequenceRunner::successful([
            r#"{"status":"needs_repair"}"#,
            r#"{"status":"needs_repair","dirty_state_rows":2,"dirty_state_files":1,"dirty_jsonl_files":1}"#,
            "unified_history=repaired_unified\n",
            "status=completed\nstate_rows=2\njsonl_applied=1\n",
        ]);
        let controller = RecordingCodexController::stopped();

        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Startup(HistoryBucketTarget::Unified),
                request_restart: false,
            },
            &paths,
            Path::new("python-test"),
            &runner,
            &controller,
        )
        .expect("preflight result");

        assert_eq!(result.status, UnifiedHistoryStatus::Repaired);
        assert_eq!(result.changed_rows, 2);
        assert_eq!(result.changed_files, 3);
        assert!(result
            .backup_path
            .as_deref()
            .is_some_and(|path| path.contains("history-startup-repair-")));
        assert!(paths
            .proxy_dir()
            .join("migrations")
            .join("unified-history-last.json")
            .exists());
        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 4);
        assert_contains_sequence(&commands[2].args, &["restore", "--unified-history"]);
        assert_contains_sequence(&commands[3].args, &["migrate-official-to-unified"]);
    }

    #[test]
    fn preflight_reports_unknown_custom_provider_as_conflict() {
        let root = temp_root("preflight-conflict");
        let paths = test_paths(&root);
        let runner = SequenceRunner::successful([
            r#"{"status":"conflict"}"#,
            r#"{"status":"clean","dirty_state_rows":0,"dirty_jsonl_files":0}"#,
        ]);
        let controller = RecordingCodexController::stopped();

        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Startup(HistoryBucketTarget::Unified),
                request_restart: false,
            },
            &paths,
            Path::new("python-test"),
            &runner,
            &controller,
        )
        .expect("preflight result");

        assert_eq!(result.status, UnifiedHistoryStatus::Conflict);
        assert_eq!(runner.commands.borrow().len(), 2);
    }

    #[test]
    fn disabled_unified_history_runs_read_only_drift_inspection() {
        let root = temp_root("preflight-disabled");
        let paths = test_paths(&root);
        let runner = SequenceRunner::successful([
            r#"{"status":"clean"}"#,
            r#"{"status":"clean","dirty_state_rows":0,"dirty_jsonl_files":0}"#,
        ]);
        let controller = RecordingCodexController::running();

        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Startup(HistoryBucketTarget::Separated),
                request_restart: false,
            },
            &paths,
            Path::new("python-test"),
            &runner,
            &controller,
        )
        .expect("preflight result");

        assert_eq!(result.status, UnifiedHistoryStatus::Clean);
        assert_eq!(result.reason.as_deref(), Some("unified_history_disabled"));
        assert_eq!(runner.commands.borrow().len(), 2);
        assert_eq!(controller.close_calls.get(), 0);
    }

    #[test]
    fn disabled_unified_history_reports_drift_without_writing() {
        let root = temp_root("preflight-disabled-drift");
        let paths = test_paths(&root);
        let runner = SequenceRunner::successful([
            r#"{"status":"needs_repair"}"#,
            r#"{"status":"needs_repair","dirty_state_rows":1,"dirty_jsonl_files":0}"#,
        ]);
        let controller = RecordingCodexController::stopped();

        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Startup(HistoryBucketTarget::Separated),
                request_restart: false,
            },
            &paths,
            Path::new("python-test"),
            &runner,
            &controller,
        )
        .expect("preflight result");

        assert_eq!(result.status, UnifiedHistoryStatus::Conflict);
        assert_eq!(result.reason.as_deref(), Some("separated_history_drift"));
        assert_eq!(runner.commands.borrow().len(), 2);
        assert_eq!(controller.close_calls.get(), 0);
    }

    #[test]
    fn explicit_disable_restores_ledger_bucket_while_gateway_config_stays_active() {
        let root = temp_root("preflight-disable-gateway");
        let paths = test_paths(&root);
        let runner = SequenceRunner::successful([
            r#"{"status":"gateway_active"}"#,
            r#"{"status":"needs_repair","dirty_state_rows":1,"dirty_state_files":1,"dirty_jsonl_files":1}"#,
            "status=completed\nstate_rows=1\njsonl_restored=1\n",
        ]);
        let controller = RecordingCodexController::stopped();

        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Explicit(HistoryBucketTarget::Separated),
                request_restart: true,
            },
            &paths,
            Path::new("python-test"),
            &runner,
            &controller,
        )
        .expect("preflight result");

        assert_eq!(result.status, UnifiedHistoryStatus::Repaired);
        assert_eq!(result.changed_rows, 1);
        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 3);
        assert_contains_sequence(&commands[2].args, &["restore-official-from-unified"]);
        assert_contains_sequence(&commands[2].args, &["--ledger-root"]);
    }

    #[test]
    fn repair_failure_returns_typed_error_and_rolls_config_back() {
        let root = temp_root("preflight-repair-failure");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.codex_dir()).unwrap();
        fs::write(paths.codex_config_path(), "model_provider = \"openai\"\n").unwrap();
        let runner = SequenceRunner {
            commands: RefCell::new(Vec::new()),
            outcomes: RefCell::new(VecDeque::from([
                successful_outcome(r#"{"status":"needs_repair"}"#),
                successful_outcome(
                    r#"{"status":"needs_repair","dirty_state_rows":1,"dirty_state_files":1,"dirty_jsonl_files":0}"#,
                ),
                successful_outcome("unified_history=injected\n"),
                CommandOutcome {
                    code: Some(1),
                    stdout: String::new(),
                    stderr: "bucket failed".to_string(),
                },
            ])),
        };
        let controller = RecordingCodexController::stopped();

        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Explicit(HistoryBucketTarget::Unified),
                request_restart: true,
            },
            &paths,
            Path::new("python-test"),
            &runner,
            &controller,
        )
        .expect("typed failure result");

        assert_eq!(result.status, UnifiedHistoryStatus::Conflict);
        assert_eq!(result.reason.as_deref(), Some("repair_failed"));
        assert!(result
            .error
            .as_deref()
            .is_some_and(|error| error.contains("bucket failed")));
        assert_eq!(
            fs::read_to_string(paths.codex_config_path()).unwrap(),
            "model_provider = \"openai\"\n"
        );
    }

    #[test]
    fn receipt_failure_rolls_back_completed_config_and_bucket_repair() {
        let root = temp_root("preflight-receipt-failure");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.codex_dir()).unwrap();
        fs::write(paths.codex_config_path(), "model_provider = \"openai\"\n").unwrap();
        fs::create_dir_all(paths.proxy_dir()).unwrap();
        fs::write(
            paths.proxy_dir().join("migrations"),
            "blocks receipt directory",
        )
        .unwrap();
        let runner = SequenceRunner::successful([
            r#"{"status":"needs_repair"}"#,
            r#"{"status":"needs_repair","dirty_state_rows":1,"dirty_state_files":1,"dirty_jsonl_files":1}"#,
            "unified_history=injected\n",
            "status=completed\nstate_rows=1\njsonl_applied=1\n",
            "restored_state_backups=1\nrestored_jsonl_backups=1\n",
        ]);
        let controller = RecordingCodexController::stopped();

        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Explicit(HistoryBucketTarget::Unified),
                request_restart: true,
            },
            &paths,
            Path::new("python-test"),
            &runner,
            &controller,
        )
        .expect("typed receipt failure");

        assert_eq!(result.status, UnifiedHistoryStatus::Conflict);
        assert!(result.error.as_deref().is_some_and(|error| {
            error.contains("failed to create migration receipt directory")
        }));
        assert_eq!(
            fs::read_to_string(paths.codex_config_path()).unwrap(),
            "model_provider = \"openai\"\n"
        );
        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 5);
        assert_contains_sequence(&commands[4].args, &["rollback-repair"]);
    }

    #[test]
    fn sync_history_runs_unified_repair_and_returns_stdout_context() {
        let root = temp_root("history-default-custom");
        let codex_home = root.join("codex-home");
        let repo_root = root.join("repo-root");
        let paths = test_paths(&root);
        write_fake_history_script(&repo_root);
        let runner =
            RecordingRunner::successful("status=completed\nstate_rows=2\njsonl_applied=3\n");

        let result = sync_history_with_paths(None, &paths, Path::new("python-test"), &runner)
            .expect("history sync");

        assert!(result.contains("History bucket repair completed for custom"));
        assert!(result.contains("state_rows=2"));
        assert!(result.contains("jsonl_applied=3"));

        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 1);
        assert_eq!(commands[0].program, PathBuf::from("python-test"));
        assert_eq!(
            commands[0].args[0],
            repo_root
                .join("src-python")
                .join("history_overlay.py")
                .to_string_lossy()
        );
        assert_contains_sequence(&commands[0].args, &["repair-history"]);
        assert_arg_value(&commands[0].args, "--codex-dir", &codex_home);
        assert_arg_literal(&commands[0].args, "--target", "custom");
        assert_arg_value(
            &commands[0].args,
            "--ledger-root",
            &codex_home.join("proxy"),
        );
        let backup_root = PathBuf::from(arg_value(&commands[0].args, "--backup-root"));
        assert!(backup_root.starts_with(codex_home.join("proxy")));
        assert!(backup_root
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.starts_with("history-bucket-repair-")));
    }

    #[test]
    fn sync_history_repairs_openai_target_when_requested() {
        let root = temp_root("history-openai-target");
        let codex_home = root.join("codex-home");
        let repo_root = root.join("repo-root");
        let paths = test_paths(&root);
        write_fake_history_script(&repo_root);
        let runner = RecordingRunner::successful("state_rows=1\n");

        let result =
            sync_history_with_paths(Some("openai"), &paths, Path::new("python-test"), &runner)
                .expect("history sync");

        assert!(result.contains("History bucket repair completed for openai"));
        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 1);
        assert_contains_sequence(&commands[0].args, &["repair-history"]);
        assert_arg_value(&commands[0].args, "--codex-dir", &codex_home);
        assert_arg_literal(&commands[0].args, "--target", "openai");
        assert_arg_value(
            &commands[0].args,
            "--ledger-root",
            &codex_home.join("proxy"),
        );
    }

    #[test]
    fn sync_history_rejects_invalid_target_provider() {
        let root = temp_root("history-invalid-target");
        let repo_root = root.join("repo-root");
        let paths = test_paths(&root);
        write_fake_history_script(&repo_root);
        let runner = RecordingRunner::successful("status=already-unified\n");

        let error =
            sync_history_with_paths(Some("official"), &paths, Path::new("python-test"), &runner)
                .expect_err("invalid target should fail");

        assert!(error.contains("unsupported history repair target"));
        assert_eq!(runner.commands.borrow().len(), 0);
    }

    #[test]
    fn migrate_official_history_command_uses_dedicated_subcommand() {
        let root = temp_root("history-migrate-official");
        let codex_home = root.join("codex-home");
        let repo_root = root.join("repo-root");
        let paths = test_paths(&root);
        write_fake_history_script(&repo_root);
        let runner = RecordingRunner::successful("status=completed\nstate_rows=2\n");

        let result = migrate_official_history_to_unified_with_paths(
            &paths,
            Path::new("python-test"),
            &runner,
        )
        .expect("migrate official");

        assert!(result.contains("Official history migration completed"));
        assert!(result.contains("state_rows=2"));
        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 1);
        assert_contains_sequence(&commands[0].args, &["migrate-official-to-unified"]);
        assert_arg_value(&commands[0].args, "--codex-dir", &codex_home);
        let backup_root = PathBuf::from(arg_value(&commands[0].args, "--backup-root"));
        assert!(backup_root
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.starts_with("history-official-to-unified-")));
    }

    #[test]
    fn restore_official_history_command_uses_ledger_root() {
        let root = temp_root("history-restore-official");
        let codex_home = root.join("codex-home");
        let repo_root = root.join("repo-root");
        let paths = test_paths(&root);
        write_fake_history_script(&repo_root);
        let runner = RecordingRunner::successful("status=completed\njsonl_restored=1\n");

        let result = restore_official_history_from_unified_with_paths(
            &paths,
            Path::new("python-test"),
            &runner,
        )
        .expect("restore official");

        assert!(result.contains("Official history restore completed"));
        assert!(result.contains("jsonl_restored=1"));
        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 1);
        assert_contains_sequence(&commands[0].args, &["restore-official-from-unified"]);
        assert_arg_value(&commands[0].args, "--codex-dir", &codex_home);
        assert_arg_value(
            &commands[0].args,
            "--ledger-root",
            &codex_home.join("proxy"),
        );
    }

    #[test]
    fn sync_history_failure_includes_command_stdout_and_stderr() {
        let root = temp_root("history-failure");
        let repo_root = root.join("repo-root");
        let paths = test_paths(&root);
        write_fake_history_script(&repo_root);
        let runner = RecordingRunner::failed(42, "printed stdout", "printed stderr");

        let error = sync_history_with_paths(None, &paths, Path::new("python-test"), &runner)
            .expect_err("history sync should fail");

        assert!(error.contains("history bucket repair failed"));
        assert!(error.contains("exit code 42"));
        assert!(error.contains("command: python-test"));
        assert!(error.contains("history_overlay.py"));
        assert!(error.contains("repair-history"));
        assert!(error.contains("printed stdout"));
        assert!(error.contains("printed stderr"));
    }

    #[derive(Debug, Clone)]
    struct RecordedCommand {
        program: PathBuf,
        args: Vec<String>,
    }

    struct RecordingRunner {
        commands: RefCell<Vec<RecordedCommand>>,
        outcome: CommandOutcome,
    }

    struct SequenceRunner {
        commands: RefCell<Vec<RecordedCommand>>,
        outcomes: RefCell<VecDeque<CommandOutcome>>,
    }

    fn successful_outcome(stdout: &str) -> CommandOutcome {
        CommandOutcome {
            code: Some(0),
            stdout: stdout.to_string(),
            stderr: String::new(),
        }
    }

    impl SequenceRunner {
        fn successful<const N: usize>(outputs: [&str; N]) -> Self {
            Self {
                commands: RefCell::new(Vec::new()),
                outcomes: RefCell::new(outputs.into_iter().map(successful_outcome).collect()),
            }
        }
    }

    impl CommandRunner for SequenceRunner {
        fn run(&self, program: &Path, args: &[String]) -> Result<CommandOutcome, String> {
            self.commands.borrow_mut().push(RecordedCommand {
                program: program.to_path_buf(),
                args: args.to_vec(),
            });
            self.outcomes
                .borrow_mut()
                .pop_front()
                .ok_or_else(|| "unexpected command".to_string())
        }
    }

    struct RecordingCodexController {
        running: bool,
        close_result: bool,
        close_calls: Cell<usize>,
        launch_calls: Cell<usize>,
    }

    impl RecordingCodexController {
        fn running() -> Self {
            Self::running_with_close_result(true)
        }

        fn running_with_close_result(close_result: bool) -> Self {
            Self {
                running: true,
                close_result,
                close_calls: Cell::new(0),
                launch_calls: Cell::new(0),
            }
        }

        fn stopped() -> Self {
            Self {
                running: false,
                close_result: true,
                close_calls: Cell::new(0),
                launch_calls: Cell::new(0),
            }
        }
    }

    impl CodexAppController for RecordingCodexController {
        fn is_running(&self) -> Result<bool, String> {
            Ok(self.running)
        }

        fn close_gracefully(&self, timeout_seconds: u64) -> Result<bool, String> {
            assert_eq!(timeout_seconds, GRACEFUL_CLOSE_TIMEOUT_SECONDS);
            self.close_calls.set(self.close_calls.get() + 1);
            Ok(self.close_result)
        }

        fn launch(&self) -> Result<(), String> {
            self.launch_calls.set(self.launch_calls.get() + 1);
            Ok(())
        }
    }

    impl RecordingRunner {
        fn successful(stdout: &str) -> Self {
            Self {
                commands: RefCell::new(Vec::new()),
                outcome: CommandOutcome {
                    code: Some(0),
                    stdout: stdout.to_string(),
                    stderr: String::new(),
                },
            }
        }

        fn failed(code: i32, stdout: &str, stderr: &str) -> Self {
            Self {
                commands: RefCell::new(Vec::new()),
                outcome: CommandOutcome {
                    code: Some(code),
                    stdout: stdout.to_string(),
                    stderr: stderr.to_string(),
                },
            }
        }
    }

    impl CommandRunner for RecordingRunner {
        fn run(&self, program: &Path, args: &[String]) -> Result<CommandOutcome, String> {
            self.commands.borrow_mut().push(RecordedCommand {
                program: program.to_path_buf(),
                args: args.to_vec(),
            });
            Ok(self.outcome.clone())
        }
    }

    fn test_paths(root: &Path) -> ConfigPaths {
        ConfigPaths::new(root.join("codex-home"), root.join("repo-root"))
    }

    fn write_fake_history_script(repo_root: &Path) {
        let script = repo_root.join("src-python").join("history_overlay.py");
        fs::create_dir_all(script.parent().unwrap()).unwrap();
        fs::write(script, "# fake history overlay").unwrap();
    }

    fn temp_root(name: &str) -> PathBuf {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "codexhub-history-{name}-{}-{suffix}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&path);
        fs::create_dir_all(&path).unwrap();
        path
    }

    fn assert_contains_sequence(args: &[String], values: &[&str]) {
        let mut position = 0;
        for value in values {
            position = args[position..]
                .iter()
                .position(|arg| arg == value)
                .map(|offset| position + offset + 1)
                .unwrap_or_else(|| panic!("missing argument {value:?} in {args:?}"));
        }
    }

    fn assert_arg_value(args: &[String], name: &str, expected: &Path) {
        assert_arg_literal(args, name, &expected.to_string_lossy());
    }

    fn assert_arg_literal(args: &[String], name: &str, expected: &str) {
        assert_eq!(arg_value(args, name), expected);
    }

    fn arg_value<'a>(args: &'a [String], name: &str) -> &'a str {
        let index = args
            .iter()
            .position(|arg| arg == name)
            .unwrap_or_else(|| panic!("missing argument {name:?} in {args:?}"));
        args.get(index + 1)
            .unwrap_or_else(|| panic!("missing value for {name:?} in {args:?}"))
    }
}
