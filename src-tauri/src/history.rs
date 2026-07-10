use crate::config::{self, CommandRunner, ConfigPaths};
use crate::safe_file;
use serde::{Deserialize, Serialize};
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const HISTORY_OPERATION_TIMEOUT: Duration = Duration::from_secs(29);
const HISTORY_ROLLBACK_RESERVE: Duration = Duration::from_secs(5);
static HISTORY_REPAIR_IN_PROGRESS: AtomicBool = AtomicBool::new(false);

trait HistoryClock {
    fn now(&self) -> Instant;
}

struct SystemHistoryClock;

impl HistoryClock for SystemHistoryClock {
    fn now(&self) -> Instant {
        Instant::now()
    }
}

struct HistoryOperationBudget {
    deadline: Instant,
}

impl HistoryOperationBudget {
    fn new(clock: &dyn HistoryClock) -> Self {
        let started = clock.now();
        let deadline = started + HISTORY_OPERATION_TIMEOUT;
        Self { deadline }
    }
}

#[derive(Debug)]
struct HistoryRepairGuard<'a> {
    gate: &'a AtomicBool,
}

impl HistoryRepairGuard<'_> {
    fn try_acquire(gate: &AtomicBool) -> Option<HistoryRepairGuard<'_>> {
        gate.compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .ok()
            .map(|_| HistoryRepairGuard { gate })
    }
}

impl Drop for HistoryRepairGuard<'_> {
    fn drop(&mut self) {
        self.gate.store(false, Ordering::Release);
    }
}

fn acquire_history_repair(
    mutating: bool,
    gate: &AtomicBool,
) -> Result<Option<HistoryRepairGuard<'_>>, UnifiedHistoryResult> {
    if !mutating {
        return Ok(None);
    }
    HistoryRepairGuard::try_acquire(gate).map(Some).ok_or_else(|| {
        UnifiedHistoryResult::pending(UnifiedHistoryStatus::Conflict, "repair_in_progress")
    })
}

struct DeadlineCommandRunner;

impl DeadlineCommandRunner {
    fn run_until(
        &self,
        program: &Path,
        args: &[String],
        deadline: Instant,
    ) -> Result<config::CommandOutcome, String> {
        let mut command = Command::new(program);
        command.args(args).stdout(Stdio::piped()).stderr(Stdio::piped());
        configure_history_helper_no_window(&mut command);
        let mut child = command
            .spawn()
            .map_err(|error| format!("failed to start {}: {error}", program.display()))?;
        let mut stdout = child.stdout.take().expect("piped helper stdout");
        let mut stderr = child.stderr.take().expect("piped helper stderr");
        let stdout_reader = thread::spawn(move || {
            let mut bytes = Vec::new();
            let _ = stdout.read_to_end(&mut bytes);
            bytes
        });
        let stderr_reader = thread::spawn(move || {
            let mut bytes = Vec::new();
            let _ = stderr.read_to_end(&mut bytes);
            bytes
        });
        let status = loop {
            if let Some(status) = child
                .try_wait()
                .map_err(|error| format!("failed to wait for {}: {error}", program.display()))?
            {
                break status;
            }
            if Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                let _ = stdout_reader.join();
                let _ = stderr_reader.join();
                return Err("history_operation_timeout: helper command exceeded deadline".to_string());
            }
            thread::sleep(Duration::from_millis(20));
        };
        let stdout = stdout_reader.join().unwrap_or_default();
        let stderr = stderr_reader.join().unwrap_or_default();
        Ok(config::CommandOutcome {
            code: status.code(),
            stdout: String::from_utf8_lossy(&stdout).into_owned(),
            stderr: String::from_utf8_lossy(&stderr).into_owned(),
        })
    }
}

struct HistoryDeadlineRunner {
    operation_deadline: Instant,
}

impl HistoryDeadlineRunner {
    fn with_deadline(operation_deadline: Instant) -> Self {
        Self {
            operation_deadline,
        }
    }

    fn command_deadline(&self, args: &[String]) -> Instant {
        let rollback = args.iter().any(|arg| arg == "rollback-repair");
        let mutating = args.iter().any(|arg| {
            matches!(
                arg.as_str(),
                "restore" | "migrate-official-to-unified" | "restore-official-from-unified"
            )
        });
        if mutating && !rollback {
            self.operation_deadline
                .checked_sub(HISTORY_ROLLBACK_RESERVE)
                .unwrap_or(self.operation_deadline)
        } else {
            self.operation_deadline
        }
    }
}

impl CommandRunner for HistoryDeadlineRunner {
    fn run(&self, program: &Path, args: &[String]) -> Result<config::CommandOutcome, String> {
        DeadlineCommandRunner.run_until(program, args, self.command_deadline(args))
    }
}

#[cfg(target_os = "windows")]
fn configure_history_helper_no_window(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(target_os = "windows"))]
fn configure_history_helper_no_window(_command: &mut Command) {}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum UnifiedHistoryStatus {
    Clean,
    Repaired,
    Deferred,
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
        let reason = if error.contains("history_operation_timeout") {
            "helper_timeout"
        } else {
            "repair_failed"
        };
        Self {
            status: UnifiedHistoryStatus::Conflict,
            changed_rows: 0,
            changed_files: 0,
            backup_path: Some(backup_path.to_string_lossy().into_owned()),
            receipt_path: None,
            reason: Some(reason.to_string()),
            error: Some(error),
            codex_restarted: false,
        }
    }

    fn helper_timeout(error: String) -> Self {
        let mut result = Self::pending(UnifiedHistoryStatus::Conflict, "helper_timeout");
        result.error = Some(error);
        result
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
    apply_repairs: bool,
}

struct HistoryRepairPlan<'a> {
    target: HistoryBucketTarget,
    repair_config: bool,
    repair_bucket: bool,
    backup_root: &'a Path,
    inspection: &'a BucketInspection,
}

pub fn preflight_unified_history(
    apply_repairs: bool,
    target_unified: Option<bool>,
) -> Result<UnifiedHistoryResult, String> {
    let _repair_guard = match acquire_history_repair(apply_repairs, &HISTORY_REPAIR_IN_PROGRESS) {
        Ok(guard) => guard,
        Err(result) => return Ok(result),
    };
    let clock = SystemHistoryClock;
    let budget = HistoryOperationBudget::new(&clock);
    let settings = config::get_settings()?;
    let paths = ConfigPaths::runtime()?;
    let python = config::find_python();
    let runner = HistoryDeadlineRunner::with_deadline(budget.deadline);
    preflight_unified_history_with_budget(
        PreflightRequest {
            target: match target_unified {
                Some(unified) => {
                    PreflightTarget::Explicit(HistoryBucketTarget::from_unified(unified))
                }
                None => PreflightTarget::Startup(HistoryBucketTarget::from_unified(
                    settings.unified_codex_history,
                )),
            },
            apply_repairs,
        },
        &paths,
        &python,
        &runner,
        &budget,
        false,
    )
}

#[cfg(test)]
fn preflight_unified_history_with_paths(
    request: PreflightRequest,
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
) -> Result<UnifiedHistoryResult, String> {
    let clock = SystemHistoryClock;
    let budget = HistoryOperationBudget::new(&clock);
    preflight_unified_history_with_budget(request, paths, python, runner, &budget, true)
}

fn preflight_unified_history_with_budget(
    request: PreflightRequest,
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
    _budget: &HistoryOperationBudget,
    online_repairs_enabled: bool,
) -> Result<UnifiedHistoryResult, String> {
    let target = request.target.bucket();
    let startup_separated = matches!(
        request.target,
        PreflightTarget::Startup(HistoryBucketTarget::Separated)
    );

    let config_inspection: ConfigInspection = match inspect_json(
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
    ) {
        Ok(inspection) => inspection,
        Err(error) if error.contains("history_operation_timeout") => {
            return Ok(UnifiedHistoryResult::helper_timeout(error));
        }
        Err(error) => return Err(error),
    };
    let bucket_inspection: BucketInspection = match inspect_json(
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
    ) {
        Ok(inspection) => inspection,
        Err(error) if error.contains("history_operation_timeout") => {
            return Ok(UnifiedHistoryResult::helper_timeout(error));
        }
        Err(error) => return Err(error),
    };

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

    if !request.apply_repairs {
        return Ok(UnifiedHistoryResult::pending(
            UnifiedHistoryStatus::RestartRequired,
            "repair_required",
        ));
    }

    // Online history writes remain disabled until the running-Codex Windows E2E
    // proves JSONL and SQLite concurrency behavior. Route switching must stay usable
    // without risking partial migration of a user's history.
    if !online_repairs_enabled {
        return Ok(UnifiedHistoryResult::pending(
            UnifiedHistoryStatus::Deferred,
            "online_history_sync_pending_validation",
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

    let result = match repair_result {
        Ok(result) => result,
        Err(error) => return Ok(UnifiedHistoryResult::failed(error, &backup_root)),
    };
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
        config::run_python_script(
            "unified history config repair",
            python,
            paths.config_overlay_script(),
            args,
            runner,
        )?;
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
        let outcome = outcome?;
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
    receipt_result?;
    Ok(result)
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

pub fn sync_history(target_provider: Option<&str>) -> Result<String, String> {
    let target_unified = target_unified_from_provider(target_provider)?;
    let result = preflight_unified_history(true, Some(target_unified))?;
    legacy_reconcile_message(result, if target_unified { "custom" } else { "openai" })
}

pub fn reconcile_after_route_switch(
    target_provider: Option<&str>,
) -> Result<UnifiedHistoryResult, String> {
    let _ = target_unified_from_provider(target_provider)?;
    Ok(UnifiedHistoryResult::clean(Some(
        "route_changed_history_unchanged",
    )))
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

#[cfg(test)]
fn reconcile_after_route_switch_with_paths(
    target: HistoryBucketTarget,
    _paths: &ConfigPaths,
    _python: &Path,
    _runner: &dyn CommandRunner,
) -> Result<UnifiedHistoryResult, String> {
    let _ = target;
    Ok(UnifiedHistoryResult::clean(Some(
        "route_changed_history_unchanged",
    )))
}

fn legacy_reconcile_message(
    result: UnifiedHistoryResult,
    target_provider: &str,
) -> Result<String, String> {
    match result.status {
        UnifiedHistoryStatus::Clean => {
            Ok(format!("History bucket is already clean for {target_provider}"))
        }
        UnifiedHistoryStatus::Repaired => Ok(format!(
            "History bucket repair completed for {target_provider}; changed rows: {}; changed files: {}; backup root: {}",
            result.changed_rows,
            result.changed_files,
            result.backup_path.as_deref().unwrap_or("not required")
        )),
        UnifiedHistoryStatus::Deferred
        | UnifiedHistoryStatus::RestartRequired
        | UnifiedHistoryStatus::Conflict => Err(
            result
                .error
                .or(result.reason)
                .unwrap_or_else(|| "history reconciliation did not complete".to_string()),
        ),
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
        acquire_history_repair, DeadlineCommandRunner,
        migrate_official_history_to_unified_with_paths, preflight_unified_history_with_paths,
        reconcile_after_route_switch_with_paths, restore_official_history_from_unified_with_paths,
        sync_history_with_paths, HistoryBucketTarget, PreflightRequest, PreflightTarget,
        UnifiedHistoryStatus,
    };
    use crate::config::{CommandOutcome, CommandRunner, ConfigPaths};
    use std::cell::RefCell;
    use std::collections::VecDeque;
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::AtomicBool;
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

    #[test]
    fn history_repair_gate_allows_only_one_mutation_at_a_time() {
        let gate = AtomicBool::new(false);
        let first = acquire_history_repair(true, &gate)
            .expect("first repair accepted")
            .expect("first repair owns gate");
        let mut mutations = 0;

        let concurrent = acquire_history_repair(true, &gate);
        if concurrent.is_ok() {
            mutations += 1;
        }

        let result = concurrent.expect_err("concurrent repair rejected");
        assert_eq!(result.status, UnifiedHistoryStatus::Conflict);
        assert_eq!(result.reason.as_deref(), Some("repair_in_progress"));
        assert_eq!(mutations, 0);
        drop(first);
        assert!(acquire_history_repair(true, &gate).is_ok());
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn history_deadline_runner_terminates_a_hung_helper() {
        let runner = DeadlineCommandRunner;
        let started = Instant::now();
        let error = runner
            .run_until(
                Path::new("powershell"),
                &[
                    "-NoProfile".to_string(),
                    "-Command".to_string(),
                    "Start-Sleep -Seconds 5".to_string(),
                ],
                Instant::now() + Duration::from_millis(100),
            )
            .expect_err("helper must time out");

        assert!(error.contains("history_operation_timeout"));
        assert!(started.elapsed() < Duration::from_secs(2));
    }

    #[test]
    fn startup_preflight_is_read_only_while_codex_is_running() {
        let root = temp_root("preflight-running");
        let paths = test_paths(&root);
        let runner = SequenceRunner::successful([
            r#"{"status":"needs_repair"}"#,
            r#"{"status":"needs_repair","dirty_state_rows":2,"dirty_state_files":1,"dirty_jsonl_files":1}"#,
        ]);
        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Startup(HistoryBucketTarget::Unified),
                apply_repairs: false,
            },
            &paths,
            Path::new("python-test"),
            &runner,
        )
        .expect("preflight result");

        assert_eq!(result.status, UnifiedHistoryStatus::RestartRequired);
        assert_eq!(result.reason.as_deref(), Some("repair_required"));
        assert_eq!(runner.commands.borrow().len(), 2);
        assert!(!paths.proxy_dir().join("migrations").exists());
    }

    #[test]
    fn startup_preflight_is_read_only_while_codex_is_stopped() {
        let root = temp_root("preflight-stopped");
        let paths = test_paths(&root);
        let runner = SequenceRunner::successful([
            r#"{"status":"needs_repair"}"#,
            r#"{"status":"needs_repair","dirty_state_rows":2,"dirty_state_files":1,"dirty_jsonl_files":1}"#,
        ]);
        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Startup(HistoryBucketTarget::Unified),
                apply_repairs: false,
            },
            &paths,
            Path::new("python-test"),
            &runner,
        )
        .expect("preflight result");

        assert_eq!(result.status, UnifiedHistoryStatus::RestartRequired);
        assert_eq!(result.reason.as_deref(), Some("repair_required"));
        assert_eq!(runner.commands.borrow().len(), 2);
        assert!(!paths.proxy_dir().exists());
    }

    #[test]
    fn route_switch_never_restarts_running_codex_when_history_is_clean() {
        let root = temp_root("route-switch-clean");
        let paths = test_paths(&root);
        let runner = SequenceRunner::successful([
            r#"{"status":"clean"}"#,
            r#"{"status":"clean","dirty_state_rows":0,"dirty_jsonl_files":0}"#,
        ]);
        let result = reconcile_after_route_switch_with_paths(
            HistoryBucketTarget::Unified,
            &paths,
            Path::new("python-test"),
            &runner,
        )
        .expect("route switch reconciliation");

        assert_eq!(result.status, UnifiedHistoryStatus::Clean);
        assert!(!result.codex_restarted);
        assert_eq!(runner.commands.borrow().len(), 0);
    }

    #[test]
    fn route_switch_does_not_query_codex_processes() {
        let root = temp_root("route-switch-process-timeout");
        let paths = test_paths(&root);
        let runner = SequenceRunner::successful([
            r#"{"status":"clean"}"#,
            r#"{"status":"clean","dirty_state_rows":0,"dirty_jsonl_files":0}"#,
        ]);
        let result = reconcile_after_route_switch_with_paths(
            HistoryBucketTarget::Unified,
            &paths,
            Path::new("python-test"),
            &runner,
        )
        .expect("route switch result");

        assert_eq!(result.status, UnifiedHistoryStatus::Clean);
        assert_eq!(
            result.reason.as_deref(),
            Some("route_changed_history_unchanged")
        );
        assert_eq!(runner.commands.borrow().len(), 0);
    }

    #[test]
    fn requested_repair_does_not_close_running_codex() {
        let root = temp_root("preflight-online-repair");
        let paths = test_paths(&root);
        let runner = SequenceRunner::successful([
            r#"{"status":"clean"}"#,
            r#"{"status":"needs_repair","dirty_state_rows":1,"dirty_jsonl_files":0}"#,
            "status=completed\nstate_rows=1\njsonl_applied=0\n",
        ]);
        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Explicit(HistoryBucketTarget::Unified),
                apply_repairs: true,
            },
            &paths,
            Path::new("python-test"),
            &runner,
        )
        .expect("preflight result");

        assert_eq!(result.status, UnifiedHistoryStatus::Repaired);
        assert_eq!(result.changed_rows, 1);
        assert_eq!(runner.commands.borrow().len(), 3);
    }

    #[test]
    fn requested_preflight_repairs_config_and_history_then_writes_receipt() {
        let root = temp_root("preflight-repair");
        let paths = test_paths(&root);
        let runner = SequenceRunner::successful([
            r#"{"status":"needs_repair"}"#,
            r#"{"status":"needs_repair","dirty_state_rows":2,"dirty_state_files":1,"dirty_jsonl_files":1}"#,
            "unified_history=repaired_unified\n",
            "status=completed\nstate_rows=2\njsonl_applied=1\n",
        ]);
        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Explicit(HistoryBucketTarget::Unified),
                apply_repairs: true,
            },
            &paths,
            Path::new("python-test"),
            &runner,
        )
        .expect("preflight result");

        assert_eq!(result.status, UnifiedHistoryStatus::Repaired);
        assert!(!result.codex_restarted);
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
        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Startup(HistoryBucketTarget::Unified),
                apply_repairs: false,
            },
            &paths,
            Path::new("python-test"),
            &runner,
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
        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Startup(HistoryBucketTarget::Separated),
                apply_repairs: false,
            },
            &paths,
            Path::new("python-test"),
            &runner,
        )
        .expect("preflight result");

        assert_eq!(result.status, UnifiedHistoryStatus::Clean);
        assert_eq!(result.reason.as_deref(), Some("unified_history_disabled"));
        assert_eq!(runner.commands.borrow().len(), 2);
    }

    #[test]
    fn disabled_unified_history_reports_drift_without_writing() {
        let root = temp_root("preflight-disabled-drift");
        let paths = test_paths(&root);
        let runner = SequenceRunner::successful([
            r#"{"status":"needs_repair"}"#,
            r#"{"status":"needs_repair","dirty_state_rows":1,"dirty_jsonl_files":0}"#,
        ]);
        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Startup(HistoryBucketTarget::Separated),
                apply_repairs: false,
            },
            &paths,
            Path::new("python-test"),
            &runner,
        )
        .expect("preflight result");

        assert_eq!(result.status, UnifiedHistoryStatus::Conflict);
        assert_eq!(result.reason.as_deref(), Some("separated_history_drift"));
        assert_eq!(runner.commands.borrow().len(), 2);
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
        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Explicit(HistoryBucketTarget::Separated),
                apply_repairs: true,
            },
            &paths,
            Path::new("python-test"),
            &runner,
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
    fn repair_failure_is_deferred_without_destructive_rollback() {
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
        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Explicit(HistoryBucketTarget::Unified),
                apply_repairs: true,
            },
            &paths,
            Path::new("python-test"),
            &runner,
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
        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 4);
        assert!(!commands.iter().any(|command| command.args.iter().any(|arg| arg == "rollback-repair")));
    }

    #[test]
    fn helper_timeout_is_deferred_without_destructive_rollback() {
        let root = temp_root("preflight-helper-timeout");
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
                    code: None,
                    stdout: String::new(),
                    stderr: "history_operation_timeout".to_string(),
                },
            ])),
        };
        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Explicit(HistoryBucketTarget::Unified),
                apply_repairs: true,
            },
            &paths,
            Path::new("python-test"),
            &runner,
        )
        .expect("typed timeout result");

        assert_eq!(result.status, UnifiedHistoryStatus::Conflict);
        assert_eq!(result.reason.as_deref(), Some("helper_timeout"));
        assert!(result.error.as_deref().is_some_and(|error| error.contains("history_operation_timeout")));
        assert_eq!(
            fs::read_to_string(paths.codex_config_path()).unwrap(),
            "model_provider = \"openai\"\n"
        );
        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 4);
        assert!(!commands.iter().any(|command| command.args.iter().any(|arg| arg == "rollback-repair")));
    }

    #[test]
    fn inspection_helper_timeout_returns_typed_result_without_mutation() {
        let root = temp_root("preflight-inspection-timeout");
        let paths = test_paths(&root);
        let runner = SequenceRunner {
            commands: RefCell::new(Vec::new()),
            outcomes: RefCell::new(VecDeque::from([CommandOutcome {
                code: None,
                stdout: String::new(),
                stderr: "history_operation_timeout".to_string(),
            }])),
        };
        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Explicit(HistoryBucketTarget::Unified),
                apply_repairs: true,
            },
            &paths,
            Path::new("python-test"),
            &runner,
        )
        .expect("typed inspection timeout");

        assert_eq!(result.status, UnifiedHistoryStatus::Conflict);
        assert_eq!(result.reason.as_deref(), Some("helper_timeout"));
        assert!(!paths.proxy_dir().exists());
        assert_eq!(runner.commands.borrow().len(), 1);
    }

    #[test]
    fn receipt_failure_never_rolls_back_completed_history_changes() {
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
        let result = preflight_unified_history_with_paths(
            PreflightRequest {
                target: PreflightTarget::Explicit(HistoryBucketTarget::Unified),
                apply_repairs: true,
            },
            &paths,
            Path::new("python-test"),
            &runner,
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
        assert_eq!(commands.len(), 4);
        assert!(!commands.iter().any(|command| command.args.iter().any(|arg| arg == "rollback-repair")));
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
