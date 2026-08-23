use super::clients::opencode::adopt_legacy_opencode_snapshot_files;
use super::clients::pi::adopt_legacy_pi_snapshot_files;
#[cfg(test)]
use super::write_text_replace;
use super::{runtime_home, runtime_proxy_dir, timestamp_millis, GatewayClientApplyResult};
use crate::app_flavor::RoutingOwner;
use crate::safe_file;
use serde::{Deserialize, Serialize};
use std::cell::RefCell;
use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

pub(in crate::gateway) fn client_backup_root(client_id: &str) -> PathBuf {
    client_backup_root_at(&runtime_home(), client_id)
}

pub(in crate::gateway) fn client_backup_root_at(owner_home: &Path, client_id: &str) -> PathBuf {
    runtime_proxy_dir(owner_home)
        .join("client-backups")
        .join(client_id)
}

pub(in crate::gateway) fn client_backup_root_for_owner(
    client_id: &str,
    owner: RoutingOwner,
) -> PathBuf {
    let current_owner = crate::app_flavor::current().routing_owner();
    let owner_home = if owner == current_owner {
        Some(runtime_home())
    } else {
        let flavor = match owner {
            RoutingOwner::Release => Some(crate::app_flavor::RuntimeFlavor::Stable),
            RoutingOwner::Beta => Some(crate::app_flavor::RuntimeFlavor::Beta),
            RoutingOwner::Official | RoutingOwner::UnknownExternal => None,
        };
        dirs::home_dir().and_then(|home| {
            flavor.map(|flavor| crate::runtime_paths::homes_for_flavor(&home, flavor).runtime)
        })
    }
    .unwrap_or_else(runtime_home);
    client_backup_root_at(&owner_home, client_id)
}

pub(in crate::gateway) fn owner_to_backup_channel(owner: RoutingOwner) -> BackupChannel {
    match owner {
        RoutingOwner::Beta => BackupChannel::Beta,
        _ => BackupChannel::Stable,
    }
}

pub(in crate::gateway) fn client_backup_roots_for_restore(
    client_id: &str,
    owner: RoutingOwner,
) -> Vec<(PathBuf, BackupChannel)> {
    let candidates = [
        (RoutingOwner::Release, BackupChannel::Stable),
        (RoutingOwner::Beta, BackupChannel::Beta),
    ];
    let mut roots: Vec<(PathBuf, BackupChannel)> = Vec::new();
    let primary_root = client_backup_root_for_owner(client_id, owner);
    let primary_channel = owner_to_backup_channel(owner);
    roots.push((primary_root, primary_channel));
    for (candidate_owner, channel) in candidates {
        let root = client_backup_root_for_owner(client_id, candidate_owner);
        if !roots.iter().any(|(existing, _)| existing == &root) {
            roots.push((root, channel));
        }
    }
    let legacy_root = client_backup_root(client_id);
    if !roots.iter().any(|(existing, _)| existing == &legacy_root) {
        roots.push((legacy_root, BackupChannel::Stable));
    }
    roots.sort_by_key(|(root, _)| !backup_root_has_entries(root));
    roots
}

/// Backup roots used during apply. The first root is the current channel's
/// backup directory (used for the pre-managed snapshot); the remainder are
/// the cross-channel Stable/Beta roots considered for legacy baseline adoption.
pub(in crate::gateway) fn client_backup_roots_for_apply(
    client_id: &str,
) -> Vec<(PathBuf, BackupChannel)> {
    let current_owner = crate::app_flavor::current().routing_owner();
    let mut roots: Vec<(PathBuf, BackupChannel)> = Vec::new();
    let primary_root = client_backup_root_for_owner(client_id, current_owner);
    let primary_channel = owner_to_backup_channel(current_owner);
    roots.push((primary_root, primary_channel));
    for (owner, channel) in [
        (RoutingOwner::Release, BackupChannel::Stable),
        (RoutingOwner::Beta, BackupChannel::Beta),
    ] {
        let root = client_backup_root_for_owner(client_id, owner);
        if !roots.iter().any(|(existing, _)| existing == &root) {
            roots.push((root, channel));
        }
    }
    roots
}

pub(in crate::gateway) fn backup_root_has_entries(root: &Path) -> bool {
    fs::read_dir(root)
        .ok()
        .is_some_and(|mut entries| entries.next().is_some())
}

// ---------------------------------------------------------------------------
// Version-independent rollback provenance for managed clients
// ---------------------------------------------------------------------------

pub(in crate::gateway) const ROLLBACK_BASELINE_VERSION: u32 = 1;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(in crate::gateway) struct RollbackBaseline {
    pub(in crate::gateway) version: u32,
    pub(in crate::gateway) recorded_at: u128,
    pub(in crate::gateway) files: HashMap<String, BaselineFile>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(in crate::gateway) enum BaselineFile {
    Snapshot { content: String },
    Absent,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(in crate::gateway) enum BackupChannel {
    Beta,
    Stable,
}

#[derive(Debug, Clone)]
pub(in crate::gateway) struct LegacySnapshotCandidate {
    pub(in crate::gateway) modified: SystemTime,
    pub(in crate::gateway) channel: BackupChannel,
    pub(in crate::gateway) name: String,
    pub(in crate::gateway) files: HashMap<String, BaselineFile>,
}

thread_local! {
    static ROLLBACK_PROVENANCE_DIR_OVERRIDE: RefCell<Option<PathBuf>> = const { RefCell::new(None) };
}

/// Test-only hook invoked after a thread has acquired the rollback baseline lock
/// and confirmed no baseline exists, but before it writes the first baseline.
/// Allows concurrency tests to force contenders to overlap at the lock seam.
#[cfg(test)]
pub(in crate::gateway) static TEST_BASELINE_WRITE_HOOK: std::sync::Mutex<
    Option<Box<dyn Fn() + Send + Sync>>,
> = std::sync::Mutex::new(None);

/// Override the canonical rollback provenance directory for the current thread.
/// Used by isolated apply seams so they never read or write production provenance.
pub(in crate::gateway) fn with_rollback_provenance_dir_override<T>(
    path: Option<PathBuf>,
    f: impl FnOnce() -> T,
) -> T {
    struct Guard;
    impl Drop for Guard {
        fn drop(&mut self) {
            ROLLBACK_PROVENANCE_DIR_OVERRIDE.with(|cell| *cell.borrow_mut() = None);
        }
    }
    ROLLBACK_PROVENANCE_DIR_OVERRIDE.with(|cell| *cell.borrow_mut() = path);
    let _guard = Guard;
    f()
}

pub(in crate::gateway) fn client_rollback_provenance_dir_resolved() -> PathBuf {
    ROLLBACK_PROVENANCE_DIR_OVERRIDE.with(|cell| {
        if let Some(path) = cell.borrow().as_ref() {
            return path.clone();
        }
        crate::runtime_paths::client_rollback_provenance_dir()
            .unwrap_or_else(|_| runtime_home().join("rollback-provenance"))
    })
}

pub(in crate::gateway) fn client_rollback_baseline_path(client_id: &str) -> PathBuf {
    client_rollback_provenance_dir_resolved()
        .join(client_id)
        .join("baseline.json")
}

pub(in crate::gateway) fn read_rollback_baseline(
    client_id: &str,
) -> Result<Option<RollbackBaseline>, String> {
    let path = client_rollback_baseline_path(client_id);
    let text = match fs::read_to_string(&path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err("rollback baseline is unreadable".to_string()),
    };
    let baseline = serde_json::from_str::<RollbackBaseline>(&text)
        .map_err(|_| "rollback baseline is corrupt".to_string())?;
    if baseline.version != ROLLBACK_BASELINE_VERSION {
        return Err("unsupported rollback baseline version".to_string());
    }
    Ok(Some(baseline))
}

#[cfg(test)]
pub(in crate::gateway) fn write_rollback_baseline_atomic(
    client_id: &str,
    baseline: &RollbackBaseline,
) -> Result<(), String> {
    if baseline.version != ROLLBACK_BASELINE_VERSION {
        return Err("unsupported rollback baseline version".to_string());
    }
    let path = client_rollback_baseline_path(client_id);
    let parent = path
        .parent()
        .ok_or_else(|| format!("failed to resolve rollback baseline parent for {client_id}"))?;
    fs::create_dir_all(parent).map_err(|error| {
        format!("failed to create rollback baseline directory for {client_id}: {error}")
    })?;
    let text = serde_json::to_string_pretty(baseline)
        .map_err(|error| format!("failed to serialize rollback baseline: {error}"))?;
    write_text_replace(&path, &text)
}

pub(in crate::gateway) fn create_baseline_dir(client_id: &str) -> Result<PathBuf, String> {
    let baseline_path = client_rollback_baseline_path(client_id);
    if let Some(parent) = baseline_path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            format!("failed to create rollback baseline directory for {client_id}: {error}")
        })?;
    }
    Ok(baseline_path)
}

pub(in crate::gateway) fn adopt_legacy_snapshot_files(
    client_id: &str,
    backup_root: &(PathBuf, BackupChannel),
) -> Result<Vec<LegacySnapshotCandidate>, String> {
    let (path, channel) = backup_root;
    match client_id {
        "opencode" => adopt_legacy_opencode_snapshot_files(path, *channel),
        "pi" => adopt_legacy_pi_snapshot_files(path, *channel),
        _ => Ok(Vec::new()),
    }
}

/// Adopt the latest eligible legacy Stable/Beta snapshot across all supplied
/// backup roots into the canonical provenance store. Selection is fully
/// deterministic: candidates are gathered from every root regardless of caller
/// or `read_dir` order, then ranked by (mtime, channel, snapshot name). Stable
/// and Beta callers therefore choose the same winner for equal mtimes.
pub(in crate::gateway) fn adopt_latest_legacy_snapshot_files(
    client_id: &str,
    backup_roots: &[(PathBuf, BackupChannel)],
) -> Result<Option<HashMap<String, BaselineFile>>, String> {
    let mut candidates: Vec<LegacySnapshotCandidate> = Vec::new();
    for backup_root in backup_roots {
        candidates.extend(adopt_legacy_snapshot_files(client_id, backup_root)?);
    }
    candidates.sort_by(|a, b| {
        a.modified
            .cmp(&b.modified)
            .then_with(|| a.channel.cmp(&b.channel))
            .then_with(|| a.name.cmp(&b.name))
    });
    Ok(candidates.pop().map(|candidate| candidate.files))
}

/// Adopt an eligible legacy Stable/Beta snapshot into the canonical provenance
/// store under a cross-process lock. Returns the adopted baseline when one was
/// written; returns Ok(None) when a baseline already exists or no eligible
/// legacy snapshot is available.
pub(in crate::gateway) fn adopt_legacy_baseline_locked(
    client_id: &str,
    backup_roots: &[(PathBuf, BackupChannel)],
) -> Result<Option<RollbackBaseline>, String> {
    let baseline_path = create_baseline_dir(client_id)?;
    let lock = safe_file::FileLock::acquire(&baseline_path)?;
    if read_rollback_baseline(client_id)?.is_some() {
        return Ok(None);
    }
    #[cfg(test)]
    {
        if let Some(hook) = TEST_BASELINE_WRITE_HOOK.lock().unwrap().as_ref() {
            hook();
        }
    }
    let adopted_files = match adopt_latest_legacy_snapshot_files(client_id, backup_roots)? {
        Some(files) => files,
        None => return Ok(None),
    };
    let baseline = RollbackBaseline {
        version: ROLLBACK_BASELINE_VERSION,
        recorded_at: timestamp_millis(),
        files: adopted_files,
    };
    let text = serde_json::to_string_pretty(&baseline)
        .map_err(|error| format!("failed to serialize rollback baseline: {error}"))?;
    safe_file::write_text_locked(&baseline_path, &text, &lock)
        .map_err(|_| "failed to write adopted rollback baseline".to_string())?;
    Ok(Some(baseline))
}

/// Record a version-independent rollback baseline before the first managed write.
/// If a baseline already exists it is preserved unchanged so that reapplies and
/// cross-version takeovers never replace the original clean baseline with a
/// managed snapshot. When the current target is managed, an eligible clean
/// legacy Stable/Beta snapshot is adopted into the canonical baseline before
/// falling back to an Absent tombstone.
pub(in crate::gateway) fn ensure_rollback_baseline(
    client_id: &str,
    backup_roots: &[(PathBuf, BackupChannel)],
    files: &[(&str, &Path)],
    is_managed: impl Fn(&str, &str) -> bool,
) -> Result<(), String> {
    let baseline_path = create_baseline_dir(client_id)?;
    let lock = safe_file::FileLock::acquire(&baseline_path)?;
    if read_rollback_baseline(client_id)?.is_some() {
        return Ok(());
    }
    let mut baseline_files = HashMap::new();
    let mut any_managed = false;
    for (name, path) in files {
        let entry = if path.exists() {
            let text = fs::read_to_string(path).map_err(|error| {
                format!("failed to read {client_id} baseline source for {name}: {error}")
            })?;
            if is_managed(name, &text) {
                any_managed = true;
                BaselineFile::Absent
            } else {
                BaselineFile::Snapshot { content: text }
            }
        } else {
            BaselineFile::Absent
        };
        baseline_files.insert((*name).to_string(), entry);
    }
    if any_managed {
        if let Some(adopted) = adopt_latest_legacy_snapshot_files(client_id, backup_roots)? {
            baseline_files = adopted;
        }
    }
    if baseline_files.is_empty() {
        return Ok(());
    }
    let baseline = RollbackBaseline {
        version: ROLLBACK_BASELINE_VERSION,
        recorded_at: timestamp_millis(),
        files: baseline_files,
    };
    let text = serde_json::to_string_pretty(&baseline)
        .map_err(|error| format!("failed to serialize rollback baseline: {error}"))?;
    safe_file::write_text_locked(&baseline_path, &text, &lock)
        .map_err(|_| "failed to write rollback baseline".to_string())
}

pub(in crate::gateway) fn restore_with_backup_roots<F>(
    backup_roots: &[(PathBuf, BackupChannel)],
    mut restore: F,
) -> Result<GatewayClientApplyResult, String>
where
    F: FnMut(&Path) -> Result<GatewayClientApplyResult, String>,
{
    let mut errors = Vec::new();
    for (backup_root, _channel) in backup_roots {
        match restore(backup_root) {
            Ok(result) => return Ok(result),
            Err(error) => errors.push(error),
        }
    }
    Err(if errors.is_empty() {
        "no channel backup roots are available".to_string()
    } else {
        errors.join("; ")
    })
}

pub(in crate::gateway) fn create_snapshot_backup(
    client_id: &str,
    backup_root: &Path,
    files: &[(&str, &Path)],
    current_is_managed: bool,
) -> Result<Option<PathBuf>, String> {
    if current_is_managed {
        return Ok(None);
    }
    let existing_files = files
        .iter()
        .filter(|(_, path)| path.exists())
        .collect::<Vec<_>>();
    if existing_files.is_empty() {
        return Ok(None);
    }
    fs::create_dir_all(backup_root).map_err(|error| {
        format!(
            "failed to create {client_id} backup directory {}: {error}",
            backup_root.display()
        )
    })?;
    let backup_path = backup_root.join(format!("{client_id}-{}", timestamp_millis()));
    fs::create_dir_all(&backup_path).map_err(|error| {
        format!(
            "failed to create {client_id} backup snapshot {}: {error}",
            backup_path.display()
        )
    })?;
    for (name, source) in existing_files {
        let target = backup_path.join(name);
        fs::copy(source, &target).map_err(|error| {
            format!(
                "failed to back up {client_id} config {} to {}: {error}",
                source.display(),
                target.display()
            )
        })?;
    }
    Ok(Some(backup_path))
}

pub(in crate::gateway) fn latest_clean_snapshot_backup<F>(
    client_id: &str,
    backup_root: &Path,
    is_managed_snapshot: F,
) -> Result<PathBuf, String>
where
    F: Fn(&Path) -> bool,
{
    fs::read_dir(backup_root)
        .map_err(|error| {
            format!(
                "failed to read backup directory {}: {error}",
                backup_root.display()
            )
        })?
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let metadata = entry.metadata().ok()?;
            if !metadata.is_dir() {
                return None;
            }
            let modified = metadata.modified().ok()?;
            let path = entry.path();
            (!is_managed_snapshot(&path)).then_some((modified, path))
        })
        .max_by_key(|(modified, _)| *modified)
        .map(|(_, path)| path)
        .ok_or_else(|| format!("no clean official backup is available for {client_id}"))
}

pub(in crate::gateway) fn restore_latest_snapshot_backup<F>(
    client_id: &str,
    backup_root: &Path,
    targets: &[(&str, &Path)],
    is_managed_snapshot: F,
) -> Result<PathBuf, String>
where
    F: Fn(&Path) -> bool,
{
    let latest = latest_clean_snapshot_backup(client_id, backup_root, is_managed_snapshot)?;
    restore_snapshot_files(&latest, targets)?;
    Ok(latest)
}

pub(in crate::gateway) fn restore_snapshot_files(
    snapshot_path: &Path,
    targets: &[(&str, &Path)],
) -> Result<(), String> {
    for (name, target) in targets {
        let source = snapshot_path.join(name);
        if source.exists() {
            if let Some(parent) = target.parent() {
                fs::create_dir_all(parent).map_err(|error| {
                    format!(
                        "failed to create config directory {}: {error}",
                        parent.display()
                    )
                })?;
            }
            fs::copy(&source, target).map_err(|error| {
                format!(
                    "failed to restore config {} to {}: {error}",
                    source.display(),
                    target.display()
                )
            })?;
        } else if target.exists() {
            fs::remove_file(target).map_err(|error| {
                format!(
                    "failed to remove restored-absent config {}: {error}",
                    target.display()
                )
            })?;
        }
    }
    Ok(())
}

pub(in crate::gateway) fn combined_current_preview(files: &[(&str, &Path)]) -> Option<String> {
    let sections = files
        .iter()
        .filter_map(|(name, path)| {
            fs::read_to_string(path)
                .ok()
                .map(|text| format!("{name}:\n{text}"))
        })
        .collect::<Vec<_>>();
    (!sections.is_empty()).then(|| sections.join("\n"))
}

pub(in crate::gateway) fn combined_named_text(files: &[(&str, &str)]) -> String {
    files
        .iter()
        .map(|(name, text)| format!("{name}:\n{text}"))
        .collect::<Vec<_>>()
        .join("\n")
}
