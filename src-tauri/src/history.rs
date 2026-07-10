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

const GRACEFUL_CLOSE_TIMEOUT_SECONDS: u64 = 10;
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

struct HistoryOperationBudget<'a> {
    clock: &'a dyn HistoryClock,
    deadline: Instant,
    work_deadline: Instant,
}

impl<'a> HistoryOperationBudget<'a> {
    fn new(clock: &'a dyn HistoryClock) -> Self {
        let started = clock.now();
        let deadline = started + HISTORY_OPERATION_TIMEOUT;
        Self {
            clock,
            deadline,
            work_deadline: deadline
                .checked_sub(HISTORY_ROLLBACK_RESERVE)
                .unwrap_or(deadline),
        }
    }

    fn close_timeout(&self) -> Duration {
        self.work_deadline
            .saturating_duration_since(self.clock.now())
            .min(Duration::from_secs(GRACEFUL_CLOSE_TIMEOUT_SECONDS))
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

    fn process_timeout(error: String) -> Self {
        let mut result = Self::pending(UnifiedHistoryStatus::Conflict, "process_timeout");
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

trait CodexAppController {
    fn is_running(&self, deadline: Instant) -> Result<bool, String>;
    fn close_gracefully(
        &self,
        codex_dir: &Path,
        timeout: Duration,
        deadline: Instant,
    ) -> Result<CloseOutcome, String>;
    fn launch(&self, deadline: Instant) -> Result<(), String>;
}

struct SystemCodexAppController;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CloseOutcome {
    Released,
    CloseTimedOut,
    BackgroundProcessesRemain,
    LockedFilesRemain,
}

impl CloseOutcome {
    fn restart_reason(self) -> Option<&'static str> {
        match self {
            Self::Released => None,
            Self::CloseTimedOut => Some("graceful_close_failed"),
            Self::BackgroundProcessesRemain => Some("background_processes_remain"),
            Self::LockedFilesRemain => Some("codex_files_locked"),
        }
    }
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
struct CodexProcessSnapshot {
    process_id: u32,
    process_name: String,
    executable_path: PathBuf,
    main_window_handle: u64,
}

#[cfg(target_os = "windows")]
#[derive(Debug, Deserialize)]
struct WindowsCodexDiscovery {
    package_install_path: Option<PathBuf>,
    #[serde(default)]
    processes: Vec<CodexProcessSnapshot>,
}

impl CodexProcessSnapshot {
    #[cfg(test)]
    fn new(
        process_id: u32,
        process_name: impl Into<String>,
        executable_path: PathBuf,
        main_window_handle: u64,
    ) -> Self {
        Self {
            process_id,
            process_name: process_name.into(),
            executable_path,
            main_window_handle,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct CodexProcessState {
    visible_ui_process_ids: Vec<u32>,
    relevant_process_ids: Vec<u32>,
}

fn classify_codex_processes(
    package_install_path: &Path,
    snapshots: &[CodexProcessSnapshot],
) -> CodexProcessState {
    let package_prefix = package_install_path
        .to_string_lossy()
        .trim_end_matches(['\\', '/'])
        .to_lowercase();
    let belongs_to_package = |snapshot: &&CodexProcessSnapshot| {
        let executable = snapshot.executable_path.to_string_lossy().to_lowercase();
        executable == package_prefix
            || executable
                .strip_prefix(&package_prefix)
                .is_some_and(|suffix| suffix.starts_with('\\') || suffix.starts_with('/'))
    };
    let relevant: Vec<&CodexProcessSnapshot> =
        snapshots.iter().filter(belongs_to_package).collect();
    CodexProcessState {
        visible_ui_process_ids: relevant
            .iter()
            .filter(|snapshot| snapshot.main_window_handle != 0)
            .map(|snapshot| snapshot.process_id)
            .collect(),
        relevant_process_ids: relevant
            .iter()
            .map(|snapshot| snapshot.process_id)
            .collect(),
    }
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
    let _repair_guard = match acquire_history_repair(request_restart, &HISTORY_REPAIR_IN_PROGRESS) {
        Ok(guard) => guard,
        Err(result) => return Ok(result),
    };
    let clock = SystemHistoryClock;
    let budget = HistoryOperationBudget::new(&clock);
    let settings = config::get_settings()?;
    let paths = ConfigPaths::runtime()?;
    let python = config::find_python();
    let runner = HistoryDeadlineRunner::with_deadline(budget.deadline);
    let controller = SystemCodexAppController;
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
            request_restart,
        },
        &paths,
        &python,
        &runner,
        &controller,
        &budget,
    )
}

#[cfg(test)]
fn preflight_unified_history_with_paths(
    request: PreflightRequest,
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
    controller: &dyn CodexAppController,
) -> Result<UnifiedHistoryResult, String> {
    let clock = SystemHistoryClock;
    let budget = HistoryOperationBudget::new(&clock);
    preflight_unified_history_with_budget(
        request,
        paths,
        python,
        runner,
        controller,
        &budget,
    )
}

fn preflight_unified_history_with_budget(
    request: PreflightRequest,
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
    controller: &dyn CodexAppController,
    budget: &HistoryOperationBudget<'_>,
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

    let was_running = match controller.is_running(budget.work_deadline) {
        Ok(running) => running,
        Err(error) if error.contains("history_operation_timeout") => {
            return Ok(UnifiedHistoryResult::process_timeout(error));
        }
        Err(error) => return Err(error),
    };
    if !request.request_restart {
        return Ok(UnifiedHistoryResult::pending(
            UnifiedHistoryStatus::RestartRequired,
            if was_running { "codex_running" } else { "repair_required" },
        ));
    }
    let close_outcome = match controller.close_gracefully(
        paths.codex_dir(),
        budget.close_timeout(),
        budget.work_deadline,
    ) {
        Ok(outcome) => outcome,
        Err(error) if error.contains("history_operation_timeout") => {
            return Ok(UnifiedHistoryResult::process_timeout(error));
        }
        Err(error) => return Err(error),
    };
    if let Some(reason) = close_outcome.restart_reason() {
        return Ok(UnifiedHistoryResult::pending(
            UnifiedHistoryStatus::RestartRequired,
            reason,
        ));
    }

    fs::create_dir_all(paths.proxy_dir()).map_err(|error| {
        format!(
            "failed to create unified history repair directory {}: {error}",
            paths.proxy_dir().display()
        )
    })?;
    let backup_root = history_backup_root(paths, "history-startup-repair");
    let config_existed_before_repair = paths.codex_config_path().exists();
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
                match controller.launch(budget.work_deadline) {
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
        if let Err(launch_error) = controller.launch(budget.work_deadline) {
            let receipt_path = result.receipt_path.as_ref().map(PathBuf::from);
            let rollback_error = rollback_applied_repair(
                AppliedRepairRollback {
                    repair_config,
                    repair_bucket,
                    backup_root: &backup_root,
                    config_snapshot: &backup_root.join("config.toml.before-repair"),
                    config_existed: config_existed_before_repair,
                },
                paths,
                python,
                runner,
            )
            .err();
            let receipt_delete_error = receipt_path.as_ref().and_then(|receipt_path| {
                match fs::remove_file(receipt_path) {
                    Ok(()) => None,
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
                    Err(error) => Some(format!(
                        "failed to delete rolled-back repair receipt: {error}"
                    )),
                }
            });
            finalize_relaunch_failure(
                &mut result,
                &launch_error,
                rollback_error,
                receipt_delete_error,
            );
            return Ok(result);
        }
        result.codex_restarted = true;
    }
    Ok(result)
}

fn finalize_relaunch_failure(
    result: &mut UnifiedHistoryResult,
    launch_error: &str,
    rollback_error: Option<String>,
    receipt_delete_error: Option<String>,
) {
    result.status = UnifiedHistoryStatus::Conflict;
    result.reason = Some("relaunch_failed".to_string());
    let mut errors = vec![format!("failed to relaunch Codex App: {launch_error}")];
    if let Some(rollback_error) = rollback_error {
        errors.push(format!("repair rollback also failed: {rollback_error}"));
    }
    if let Some(receipt_delete_error) = receipt_delete_error {
        errors.push(receipt_delete_error);
    } else {
        result.receipt_path = None;
    }
    result.error = Some(errors.join("; "));
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
                let rollback_error = rollback_applied_repair(
                    AppliedRepairRollback {
                        repair_config,
                        repair_bucket: true,
                        backup_root,
                        config_snapshot: &config_snapshot,
                        config_existed,
                    },
                    paths,
                    python,
                    runner,
                )
                .err();
                return Err(match rollback_error {
                    Some(rollback_error) => {
                        format!("{error}; repair rollback also failed: {rollback_error}")
                    }
                    None => error,
                });
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
    fn is_running(&self, deadline: Instant) -> Result<bool, String> {
        let output = run_powershell_until(&windows_codex_discovery_script(), deadline)?;
        if output.code != Some(0) {
            return Err(format!(
                "failed to discover Codex App processes: {}",
                output.stderr.trim()
            ));
        }
        let discovery: WindowsCodexDiscovery = serde_json::from_str(output.stdout.trim())
            .map_err(|error| format!("Codex App process discovery returned invalid JSON: {error}"))?;
        let Some(package_install_path) = discovery.package_install_path else {
            return Ok(false);
        };
        Ok(!classify_codex_processes(&package_install_path, &discovery.processes)
            .relevant_process_ids
            .is_empty())
    }

    fn close_gracefully(
        &self,
        codex_dir: &Path,
        timeout: Duration,
        deadline: Instant,
    ) -> Result<CloseOutcome, String> {
        let script = windows_codex_close_script(timeout, codex_dir);
        let output = run_powershell_until(&script, deadline)?;
        match output.code {
            Some(0) => Ok(CloseOutcome::Released),
            Some(2) => Ok(CloseOutcome::CloseTimedOut),
            Some(3) => Ok(CloseOutcome::BackgroundProcessesRemain),
            Some(4) => Ok(CloseOutcome::LockedFilesRemain),
            _ => Err(format!("failed to close Codex App gracefully: {}", output.stderr.trim())),
        }
    }

    fn launch(&self, deadline: Instant) -> Result<(), String> {
        let output = run_powershell_until(&windows_codex_launch_script(), deadline)?;
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
fn windows_codex_launch_script() -> String {
    "$ErrorActionPreference='Stop'; $package=Get-AppxPackage -Name OpenAI.Codex | Select-Object -First 1; if (-not $package) { throw 'Codex App is not installed.' }; $manifest=Get-AppxPackageManifest -Package $package; $applications=@($manifest.Package.Applications.Application); $application=$applications | Where-Object { $_.Executable -and ([string]$_.Executable).EndsWith('ChatGPT.exe',[System.StringComparison]::OrdinalIgnoreCase) } | Select-Object -First 1; if (-not $application) { $application=$applications | Select-Object -First 1 }; if (-not $application) { throw 'Codex App manifest has no launchable application.' }; $aumid=$package.PackageFamilyName + '!' + [string]$application.Id; Start-Process -FilePath 'explorer.exe' -ArgumentList ('shell:AppsFolder\\' + $aumid)".to_string()
}

#[cfg(target_os = "windows")]
fn windows_codex_discovery_script() -> String {
    "$ErrorActionPreference='Stop'; $package=Get-AppxPackage -Name OpenAI.Codex | Select-Object -First 1; if (-not $package) { @{ package_install_path=$null; processes=@() } | ConvertTo-Json -Compress -Depth 3; exit 0 }; $root=$package.InstallLocation.TrimEnd('\\'); $processes=@(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and ($_.ExecutablePath -eq $root -or $_.ExecutablePath.StartsWith($root + '\\',[System.StringComparison]::OrdinalIgnoreCase)) } | ForEach-Object { $process=Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; @{ process_id=[uint32]$_.ProcessId; process_name=[string]$_.Name; executable_path=[string]$_.ExecutablePath; main_window_handle=if ($process) { [uint64]$process.MainWindowHandle.ToInt64() } else { [uint64]0 } } }); @{ package_install_path=$root; processes=$processes } | ConvertTo-Json -Compress -Depth 3".to_string()
}

#[cfg(target_os = "windows")]
fn powershell_single_quoted(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

#[cfg(target_os = "windows")]
fn windows_codex_close_script(timeout: Duration, codex_dir: &Path) -> String {
    windows_codex_close_script_with_lock_probe(timeout, codex_dir, None)
}

#[cfg(target_os = "windows")]
fn windows_codex_close_script_with_lock_probe(
    timeout: Duration,
    codex_dir: &Path,
    lock_probe: Option<&str>,
) -> String {
    let timeout_millis = timeout.as_millis().min(u64::MAX as u128) as u64;
    let codex_dir = powershell_single_quoted(&codex_dir.to_string_lossy());
    let lock_probe = match lock_probe {
        Some(probe) => format!("$lockProbe={probe};"),
        None => "$lockProbe=$null;".to_string(),
    };
    format!(
        "$ErrorActionPreference='Stop'; {lock_probe} $package=Get-AppxPackage -Name OpenAI.Codex | Select-Object -First 1; $root=if ($package) {{ $package.InstallLocation.TrimEnd('\\') }} else {{ $null }}; $codex={codex_dir}; function Relevant {{ if (-not $root) {{ return @() }}; @(Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -and ($_.ExecutablePath -eq $root -or $_.ExecutablePath.StartsWith($root + '\\',[System.StringComparison]::OrdinalIgnoreCase)) }}) }}; function Locked {{ if (-not (Test-Path -LiteralPath $codex)) {{ return $false }}; $files=@(Get-ChildItem -LiteralPath $codex -File -Recurse -ErrorAction SilentlyContinue | Where-Object {{ $_.Name -eq 'config.toml' -or $_.Name -like 'state*.sqlite*' -or $_.Extension -eq '.jsonl' }}); foreach ($file in $files) {{ if ($lockProbe) {{ if (& $lockProbe $file.FullName) {{ return $true }} }} else {{ try {{ $stream=[System.IO.File]::Open($file.FullName,'Open','Read','None'); $stream.Dispose() }} catch {{ return $true }} }} }}; return $false }}; $relevant=@(Relevant); if ($relevant) {{ $visible=@($relevant | ForEach-Object {{ Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }} | Where-Object {{ $_.MainWindowHandle -ne 0 }}); if (-not $visible) {{ exit 3 }}; $visible | ForEach-Object {{ [void]$_.CloseMainWindow() }} }}; $deadline=(Get-Date).AddMilliseconds({timeout_millis}); do {{ $relevant=@(Relevant); $locked=Locked; if (-not $relevant -and -not $locked) {{ exit 0 }}; Start-Sleep -Milliseconds 200 }} while ((Get-Date) -lt $deadline); $visible=@($relevant | ForEach-Object {{ Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }} | Where-Object {{ $_.MainWindowHandle -ne 0 }}); if ($visible) {{ exit 2 }} elseif ($relevant) {{ exit 3 }} else {{ exit 4 }}"
    )
}

#[cfg(target_os = "windows")]
fn run_powershell_until(
    script: &str,
    deadline: Instant,
) -> Result<config::CommandOutcome, String> {
    DeadlineCommandRunner.run_until(
        Path::new("powershell"),
        &[
            "-NoProfile".to_string(),
            "-ExecutionPolicy".to_string(),
            "Bypass".to_string(),
            "-Command".to_string(),
            script.to_string(),
        ],
        deadline,
    )
}

#[cfg(not(target_os = "windows"))]
impl CodexAppController for SystemCodexAppController {
    fn is_running(&self, _deadline: Instant) -> Result<bool, String> {
        Ok(false)
    }
    fn close_gracefully(
        &self,
        _codex_dir: &Path,
        _timeout: Duration,
        _deadline: Instant,
    ) -> Result<CloseOutcome, String> {
        Ok(CloseOutcome::Released)
    }
    fn launch(&self, _deadline: Instant) -> Result<(), String> {
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
    let _repair_guard = match acquire_history_repair(true, &HISTORY_REPAIR_IN_PROGRESS) {
        Ok(guard) => guard,
        Err(result) => return Ok(result),
    };
    let clock = SystemHistoryClock;
    let budget = HistoryOperationBudget::new(&clock);
    let target_unified = target_unified_from_provider(target_provider)?;
    let paths = ConfigPaths::runtime()?;
    let python = config::find_python();
    let runner = HistoryDeadlineRunner::with_deadline(budget.deadline);
    let controller = SystemCodexAppController;
    reconcile_after_route_switch_with_budget(
        HistoryBucketTarget::from_unified(target_unified),
        &paths,
        &python,
        &runner,
        &controller,
        &budget,
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

#[cfg(test)]
fn reconcile_after_route_switch_with_paths(
    target: HistoryBucketTarget,
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
    controller: &dyn CodexAppController,
) -> Result<UnifiedHistoryResult, String> {
    let clock = SystemHistoryClock;
    let budget = HistoryOperationBudget::new(&clock);
    reconcile_after_route_switch_with_budget(target, paths, python, runner, controller, &budget)
}

fn reconcile_after_route_switch_with_budget(
    target: HistoryBucketTarget,
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
    controller: &dyn CodexAppController,
    budget: &HistoryOperationBudget<'_>,
) -> Result<UnifiedHistoryResult, String> {
    let mut result = preflight_unified_history_with_budget(
        PreflightRequest {
            target: PreflightTarget::Explicit(target),
            request_restart: true,
        },
        paths,
        python,
        runner,
        controller,
        budget,
    )?;
    if result.status != UnifiedHistoryStatus::Clean {
        return Ok(result);
    }
    let running = match controller.is_running(budget.work_deadline) {
        Ok(running) => running,
        Err(error) if error.contains("history_operation_timeout") => {
            return Ok(UnifiedHistoryResult::process_timeout(error));
        }
        Err(error) => return Err(error),
    };
    if !running {
        return Ok(result);
    }
    let close_outcome = match controller.close_gracefully(
        paths.codex_dir(),
        budget.close_timeout(),
        budget.work_deadline,
    ) {
        Ok(outcome) => outcome,
        Err(error) if error.contains("history_operation_timeout") => {
            return Ok(UnifiedHistoryResult::process_timeout(error));
        }
        Err(error) => return Err(error),
    };
    if let Some(reason) = close_outcome.restart_reason() {
        return Ok(UnifiedHistoryResult::pending(
            UnifiedHistoryStatus::RestartRequired,
            reason,
        ));
    }
    if let Err(error) = controller.launch(budget.work_deadline) {
        if error.contains("history_operation_timeout") {
            return Ok(UnifiedHistoryResult::process_timeout(error));
        }
        return Err(error);
    }
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
    #[cfg(target_os = "windows")]
    use super::{
        windows_codex_close_script, windows_codex_close_script_with_lock_probe,
        windows_codex_discovery_script, windows_codex_launch_script,
    };
    use super::{
        classify_codex_processes, CloseOutcome, CodexProcessSnapshot,
        acquire_history_repair, finalize_relaunch_failure, DeadlineCommandRunner,
        preflight_unified_history_with_budget, HistoryClock, HistoryOperationBudget,
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
    use std::sync::atomic::AtomicBool;
    use std::time::{Duration, Instant};

    struct ManualClock {
        now: Cell<Instant>,
    }

    impl ManualClock {
        fn new(now: Instant) -> Self {
            Self { now: Cell::new(now) }
        }

        fn advance(&self, duration: Duration) {
            self.now.set(self.now.get() + duration);
        }
    }

    impl HistoryClock for ManualClock {
        fn now(&self) -> Instant {
            self.now.get()
        }
    }

    struct AdvancingRunner<'a> {
        clock: &'a ManualClock,
        commands: RefCell<Vec<RecordedCommand>>,
        outcomes: RefCell<VecDeque<(Duration, CommandOutcome)>>,
    }

    impl CommandRunner for AdvancingRunner<'_> {
        fn run(&self, program: &Path, args: &[String]) -> Result<CommandOutcome, String> {
            self.commands.borrow_mut().push(RecordedCommand {
                program: program.to_path_buf(),
                args: args.to_vec(),
            });
            let (duration, outcome) = self
                .outcomes
                .borrow_mut()
                .pop_front()
                .ok_or_else(|| "unexpected command".to_string())?;
            self.clock.advance(duration);
            Ok(outcome)
        }
    }

    struct BudgetRecordingController {
        running: bool,
        close_outcome: CloseOutcome,
        launch_error: Option<String>,
        close_budget: Cell<Option<Duration>>,
        close_codex_dir: RefCell<Option<PathBuf>>,
        launch_deadline: Cell<Option<Instant>>,
    }

    impl CodexAppController for BudgetRecordingController {
        fn is_running(&self, _deadline: Instant) -> Result<bool, String> {
            Ok(self.running)
        }

        fn close_gracefully(
            &self,
            codex_dir: &Path,
            timeout: Duration,
            _deadline: Instant,
        ) -> Result<CloseOutcome, String> {
            self.close_codex_dir.replace(Some(codex_dir.to_path_buf()));
            self.close_budget.set(Some(timeout));
            Ok(self.close_outcome)
        }

        fn launch(&self, deadline: Instant) -> Result<(), String> {
            self.launch_deadline.set(Some(deadline));
            match &self.launch_error {
                Some(error) => Err(error.clone()),
                None => Ok(()),
            }
        }
    }

    #[test]
    fn inspections_reduce_the_graceful_close_budget() {
        let started = Instant::now();
        let clock = ManualClock::new(started);
        let budget = HistoryOperationBudget::new(&clock);
        let root = temp_root("inspection-close-budget");
        let paths = test_paths(&root);
        let runner = AdvancingRunner {
            clock: &clock,
            commands: RefCell::new(Vec::new()),
            outcomes: RefCell::new(VecDeque::from([
                (Duration::from_secs(10), successful_outcome(r#"{"status":"needs_repair"}"#)),
                (
                    Duration::from_secs(10),
                    successful_outcome(r#"{"status":"needs_repair","dirty_state_rows":1}"#),
                ),
            ])),
        };
        let controller = BudgetRecordingController {
            running: true,
            close_outcome: CloseOutcome::CloseTimedOut,
            launch_error: None,
            close_budget: Cell::new(None),
            close_codex_dir: RefCell::new(None),
            launch_deadline: Cell::new(None),
        };

        let result = preflight_unified_history_with_budget(
            PreflightRequest {
                target: PreflightTarget::Explicit(HistoryBucketTarget::Unified),
                request_restart: true,
            },
            &paths,
            Path::new("python-test"),
            &runner,
            &controller,
            &budget,
        )
        .expect("bounded close result");

        assert_eq!(result.reason.as_deref(), Some("graceful_close_failed"));
        assert_eq!(controller.close_budget.get(), Some(Duration::from_secs(4)));
        assert_eq!(controller.close_codex_dir.borrow().as_deref(), Some(paths.codex_dir()));
    }

    #[test]
    fn launch_uses_work_deadline_and_rollback_uses_reserved_budget() {
        let started = Instant::now();
        let clock = ManualClock::new(started);
        let budget = HistoryOperationBudget::new(&clock);
        let root = temp_root("launch-budget");
        let paths = test_paths(&root);
        let runner = AdvancingRunner {
            clock: &clock,
            commands: RefCell::new(Vec::new()),
            outcomes: RefCell::new(VecDeque::from([
                (Duration::ZERO, successful_outcome(r#"{"status":"clean"}"#)),
                (
                    Duration::ZERO,
                    successful_outcome(r#"{"status":"needs_repair","dirty_state_rows":1,"dirty_state_files":1}"#),
                ),
                (
                    Duration::from_secs(23),
                    successful_outcome("status=completed\nstate_rows=1\n"),
                ),
                (
                    Duration::from_secs(1),
                    successful_outcome("restored_state_backups=1\n"),
                ),
            ])),
        };
        let controller = BudgetRecordingController {
            running: false,
            close_outcome: CloseOutcome::Released,
            launch_error: Some("history_operation_timeout: launch".to_string()),
            close_budget: Cell::new(None),
            close_codex_dir: RefCell::new(None),
            launch_deadline: Cell::new(None),
        };

        let result = preflight_unified_history_with_budget(
            PreflightRequest {
                target: PreflightTarget::Explicit(HistoryBucketTarget::Unified),
                request_restart: true,
            },
            &paths,
            Path::new("python-test"),
            &runner,
            &controller,
            &budget,
        )
        .expect("bounded launch result");

        assert_eq!(result.reason.as_deref(), Some("relaunch_failed"));
        assert_eq!(controller.launch_deadline.get(), Some(started + Duration::from_secs(24)));
        assert!(clock.now() <= started + Duration::from_secs(29));
        assert_contains_sequence(&runner.commands.borrow()[3].args, &["rollback-repair"]);
    }

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
    fn windows_close_script_targets_supplied_codex_home_without_force_kill() {
        let target = Path::new(r"C:\Users\O'Brien\custom codex");

        let script = windows_codex_close_script(Duration::from_millis(25), target);

        assert!(script.contains(r"$codex='C:\Users\O''Brien\custom codex'"));
        assert!(!script.contains("USERPROFILE"));
        assert!(!script.contains("Stop-Process"));
        assert!(!script.contains("taskkill"));
        assert!(script.contains("Test-Path -LiteralPath $codex"));
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn windows_close_script_reports_injected_lock_for_custom_codex_home() {
        let target = Path::new(r"D:\Codex Homes\target");
        let script = format!(
            "function Get-AppxPackage {{ param($Name) @() }}; function Test-Path {{ param($LiteralPath) $LiteralPath -eq 'D:\\Codex Homes\\target' }}; function Get-ChildItem {{ param($LiteralPath,[switch]$File,[switch]$Recurse,$ErrorAction) [pscustomobject]@{{ Name='config.toml'; Extension='.toml'; FullName=($LiteralPath + '\\config.toml') }} }}; {}",
            windows_codex_close_script_with_lock_probe(
                Duration::ZERO,
                target,
                Some("{ param($path) $path -eq 'D:\\Codex Homes\\target\\config.toml' }"),
            )
        );

        let output = std::process::Command::new("powershell")
            .args(["-NoProfile", "-Command", &script])
            .output()
            .expect("injected close script runs");

        assert_eq!(output.status.code(), Some(4), "{}", String::from_utf8_lossy(&output.stderr));
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn windows_discovery_serializes_an_intptr_window_handle() {
        let script = format!(
            "function Get-AppxPackage {{ param($Name) [pscustomobject]@{{ InstallLocation='C:\\Program Files\\WindowsApps\\OpenAI.Codex_test' }} }}; function Get-CimInstance {{ param($ClassName) [pscustomobject]@{{ ExecutablePath='C:\\Program Files\\WindowsApps\\OpenAI.Codex_test\\ChatGPT.exe'; ProcessId=41; Name='ChatGPT.exe' }} }}; function Get-Process {{ param($Id,$ErrorAction) [pscustomobject]@{{ MainWindowHandle=[IntPtr]::new(9001) }} }}; {}",
            windows_codex_discovery_script()
        );

        let output = std::process::Command::new("powershell")
            .args(["-NoProfile", "-Command", &script])
            .output()
            .expect("injected discovery script runs");

        assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
        let discovery: super::WindowsCodexDiscovery =
            serde_json::from_slice(&output.stdout).expect("valid discovery JSON");
        assert_eq!(discovery.processes.len(), 1);
        assert_eq!(discovery.processes[0].main_window_handle, 9001);
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn windows_launch_uses_the_codex_manifest_application_via_shell_activation() {
        let script = format!(
            "function Get-AppxPackage {{ param($Name) [pscustomobject]@{{ PackageFamilyName='OpenAI.Codex_test' }} }}; function Get-AppxPackageManifest {{ param($Package) [pscustomobject]@{{ Package=[pscustomobject]@{{ Applications=[pscustomobject]@{{ Application=@([pscustomobject]@{{ Id='Main'; Executable='ChatGPT.exe' }}) }} }} }} }}; function Start-Process {{ param($FilePath,$ArgumentList) Write-Output ($FilePath + '|' + $ArgumentList) }}; {}",
            windows_codex_launch_script()
        );

        let output = std::process::Command::new("powershell")
            .args(["-NoProfile", "-Command", &script])
            .output()
            .expect("injected launch script runs");

        assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
        assert_eq!(
            String::from_utf8_lossy(&output.stdout).trim(),
            r"explorer.exe|shell:AppsFolder\OpenAI.Codex_test!Main"
        );
        assert!(!windows_codex_launch_script().contains("Get-StartApps"));
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
    fn windows_process_discovery_identifies_visible_chatgpt_from_codex_package() {
        let package = Path::new(r"C:\Program Files\WindowsApps\OpenAI.Codex_1.2.3_x64__abc");
        let state = classify_codex_processes(
            package,
            &[
                CodexProcessSnapshot::new(
                    41,
                    "ChatGPT.exe",
                    package.join("ChatGPT.exe"),
                    9001,
                ),
                CodexProcessSnapshot::new(
                    42,
                    "codex.exe",
                    package.join(r"app\resources\codex.exe"),
                    0,
                ),
            ],
        );

        assert_eq!(state.visible_ui_process_ids, vec![41]);
        assert_eq!(state.relevant_process_ids, vec![41, 42]);
    }

    #[test]
    fn windows_process_discovery_does_not_treat_headless_codex_as_desktop_ui() {
        let package = Path::new(r"C:\Program Files\WindowsApps\OpenAI.Codex_1.2.3_x64__abc");
        let state = classify_codex_processes(
            package,
            &[CodexProcessSnapshot::new(
                42,
                "codex.exe",
                package.join(r"app\resources\codex.exe"),
                0,
            )],
        );

        assert!(state.visible_ui_process_ids.is_empty());
        assert_eq!(state.relevant_process_ids, vec![42]);
    }

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
    fn startup_preflight_is_read_only_while_codex_is_stopped() {
        let root = temp_root("preflight-stopped");
        let paths = test_paths(&root);
        let runner = SequenceRunner::successful([
            r#"{"status":"needs_repair"}"#,
            r#"{"status":"needs_repair","dirty_state_rows":2,"dirty_state_files":1,"dirty_jsonl_files":1}"#,
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

        assert_eq!(result.status, UnifiedHistoryStatus::RestartRequired);
        assert_eq!(result.reason.as_deref(), Some("repair_required"));
        assert_eq!(runner.commands.borrow().len(), 2);
        assert!(!paths.proxy_dir().exists());
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
    fn route_switch_process_timeout_returns_typed_result() {
        let root = temp_root("route-switch-process-timeout");
        let paths = test_paths(&root);
        let runner = SequenceRunner::successful([
            r#"{"status":"clean"}"#,
            r#"{"status":"clean","dirty_state_rows":0,"dirty_jsonl_files":0}"#,
        ]);
        let controller = ProcessTimeoutController;

        let result = reconcile_after_route_switch_with_paths(
            HistoryBucketTarget::Unified,
            &paths,
            Path::new("python-test"),
            &runner,
            &controller,
        )
        .expect("typed process timeout");

        assert_eq!(result.status, UnifiedHistoryStatus::Conflict);
        assert_eq!(result.reason.as_deref(), Some("process_timeout"));
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
    fn requested_repair_returns_background_process_reason_without_writing() {
        let root = temp_root("preflight-background-process");
        let paths = test_paths(&root);
        let runner = SequenceRunner::successful([
            r#"{"status":"clean"}"#,
            r#"{"status":"needs_repair","dirty_state_rows":1,"dirty_jsonl_files":0}"#,
        ]);
        let controller = RecordingCodexController::running_with_close_outcome(
            CloseOutcome::BackgroundProcessesRemain,
        );

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
        assert_eq!(result.reason.as_deref(), Some("background_processes_remain"));
        assert_eq!(runner.commands.borrow().len(), 2);
        assert!(!paths.proxy_dir().exists());
    }

    #[test]
    fn requested_repair_returns_locked_files_reason_without_writing() {
        let root = temp_root("preflight-locked-files");
        let paths = test_paths(&root);
        let runner = SequenceRunner::successful([
            r#"{"status":"clean"}"#,
            r#"{"status":"needs_repair","dirty_state_rows":1,"dirty_jsonl_files":0}"#,
        ]);
        let controller = RecordingCodexController::running_with_close_outcome(
            CloseOutcome::LockedFilesRemain,
        );

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
        assert_eq!(result.reason.as_deref(), Some("codex_files_locked"));
        assert_eq!(runner.commands.borrow().len(), 2);
        assert!(!paths.proxy_dir().exists());
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
        .expect("preflight result");

        assert_eq!(result.status, UnifiedHistoryStatus::Repaired);
        assert!(!result.codex_restarted);
        assert_eq!(controller.launch_calls.get(), 0);
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
    fn helper_timeout_returns_typed_error_and_rolls_config_back() {
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
        .expect("typed timeout result");

        assert_eq!(result.status, UnifiedHistoryStatus::Conflict);
        assert_eq!(result.reason.as_deref(), Some("helper_timeout"));
        assert!(result.error.as_deref().is_some_and(|error| error.contains("history_operation_timeout")));
        assert_eq!(
            fs::read_to_string(paths.codex_config_path()).unwrap(),
            "model_provider = \"openai\"\n"
        );
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
        .expect("typed inspection timeout");

        assert_eq!(result.status, UnifiedHistoryStatus::Conflict);
        assert_eq!(result.reason.as_deref(), Some("helper_timeout"));
        assert!(!paths.proxy_dir().exists());
        assert_eq!(runner.commands.borrow().len(), 1);
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
    fn relaunch_failure_returns_typed_error_and_rolls_repair_back() {
        let root = temp_root("preflight-relaunch-failure");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.codex_dir()).unwrap();
        fs::write(paths.codex_config_path(), "model_provider = \"openai\"\n").unwrap();
        let runner = SequenceRunner::successful([
            r#"{"status":"needs_repair"}"#,
            r#"{"status":"needs_repair","dirty_state_rows":1,"dirty_state_files":1,"dirty_jsonl_files":1}"#,
            "unified_history=injected\n",
            "status=completed\nstate_rows=1\njsonl_applied=1\n",
            "restored_state_backups=1\nrestored_jsonl_backups=1\n",
        ]);
        let controller = RecordingCodexController::running_with_launch_error("launch failed");

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
        .expect("typed relaunch failure");

        assert_eq!(result.status, UnifiedHistoryStatus::Conflict);
        assert_eq!(result.reason.as_deref(), Some("relaunch_failed"));
        assert!(result.error.as_deref().is_some_and(|error| error.contains("launch failed")));
        assert_eq!(
            fs::read_to_string(paths.codex_config_path()).unwrap(),
            "model_provider = \"openai\"\n"
        );
        assert!(!paths.proxy_dir().join("migrations").join("unified-history-last.json").exists());
        assert_contains_sequence(&runner.commands.borrow()[4].args, &["rollback-repair"]);
    }

    #[test]
    fn receipt_delete_failure_preserves_actual_path_and_reports_error() {
        let mut result = super::UnifiedHistoryResult {
            status: UnifiedHistoryStatus::Repaired,
            changed_rows: 1,
            changed_files: 2,
            backup_path: Some("backup".to_string()),
            receipt_path: Some("receipt.json".to_string()),
            reason: None,
            error: None,
            codex_restarted: false,
        };

        finalize_relaunch_failure(
            &mut result,
            "launch failed",
            None,
            Some("receipt delete denied".to_string()),
        );

        assert_eq!(result.reason.as_deref(), Some("relaunch_failed"));
        assert_eq!(result.receipt_path.as_deref(), Some("receipt.json"));
        assert!(result.error.as_deref().is_some_and(|error| error.contains("receipt delete denied")));
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
        close_outcome: CloseOutcome,
        close_calls: Cell<usize>,
        launch_calls: Cell<usize>,
        launch_error: Option<String>,
    }

    struct ProcessTimeoutController;

    impl CodexAppController for ProcessTimeoutController {
        fn is_running(&self, _deadline: Instant) -> Result<bool, String> {
            Err("history_operation_timeout: process discovery".to_string())
        }

        fn close_gracefully(
            &self,
            _codex_dir: &Path,
            _timeout: Duration,
            _deadline: Instant,
        ) -> Result<CloseOutcome, String> {
            unreachable!()
        }

        fn launch(&self, _deadline: Instant) -> Result<(), String> {
            unreachable!()
        }
    }

    impl RecordingCodexController {
        fn running() -> Self {
            Self::running_with_close_result(true)
        }

        fn running_with_close_result(close_result: bool) -> Self {
            Self::running_with_close_outcome(if close_result {
                CloseOutcome::Released
            } else {
                CloseOutcome::CloseTimedOut
            })
        }

        fn running_with_close_outcome(close_outcome: CloseOutcome) -> Self {
            Self {
                running: true,
                close_outcome,
                close_calls: Cell::new(0),
                launch_calls: Cell::new(0),
                launch_error: None,
            }
        }

        fn stopped() -> Self {
            Self {
                running: false,
                close_outcome: CloseOutcome::Released,
                close_calls: Cell::new(0),
                launch_calls: Cell::new(0),
                launch_error: None,
            }
        }

        fn running_with_launch_error(error: &str) -> Self {
            let mut controller = Self::running();
            controller.launch_error = Some(error.to_string());
            controller
        }
    }

    impl CodexAppController for RecordingCodexController {
        fn is_running(&self, _deadline: Instant) -> Result<bool, String> {
            Ok(self.running)
        }

        fn close_gracefully(
            &self,
            _codex_dir: &Path,
            timeout: Duration,
            _deadline: Instant,
        ) -> Result<CloseOutcome, String> {
            assert!(timeout <= Duration::from_secs(GRACEFUL_CLOSE_TIMEOUT_SECONDS));
            self.close_calls.set(self.close_calls.get() + 1);
            Ok(self.close_outcome)
        }

        fn launch(&self, _deadline: Instant) -> Result<(), String> {
            self.launch_calls.set(self.launch_calls.get() + 1);
            match &self.launch_error {
                Some(error) => Err(error.clone()),
                None => Ok(()),
            }
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
