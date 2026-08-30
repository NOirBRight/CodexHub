use crate::safe_file;
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub(crate) struct PreparedTextFile {
    pub(crate) path: PathBuf,
    pub(crate) text: String,
    pub(crate) unix_mode: Option<u32>,
    pub(crate) namespace: PreparedFileNamespace,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum PreparedFileNamespace {
    Absolute,
    Runtime,
    CodexTarget,
}

pub(crate) const ROLLBACK_FAILED_ERROR: &str = "file_transaction_rollback_failed";

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum FileTransactionError {
    SnapshotFailed(String),
    RolledBack(String),
    RollbackFailed {
        operation: String,
        rollback: Vec<String>,
    },
}

impl FileTransactionError {
    pub(crate) fn rollback_failed(&self) -> bool {
        matches!(self, Self::RollbackFailed { .. })
    }
}

impl fmt::Display for FileTransactionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::SnapshotFailed(error) => {
                write!(formatter, "file_transaction_snapshot_failed: {error}")
            }
            Self::RolledBack(error) => write!(formatter, "file_transaction_rolled_back: {error}"),
            Self::RollbackFailed {
                operation,
                rollback,
            } => write!(
                formatter,
                "{ROLLBACK_FAILED_ERROR}: operation failed ({operation}); rollback failed ({})",
                rollback.join("; ")
            ),
        }
    }
}

impl PreparedTextFile {
    pub(crate) fn new(path: PathBuf, text: String) -> Self {
        Self {
            path,
            text,
            unix_mode: None,
            namespace: PreparedFileNamespace::Absolute,
        }
    }

    pub(crate) fn owner_only(path: PathBuf, text: String) -> Self {
        Self {
            path,
            text,
            unix_mode: Some(0o600),
            namespace: PreparedFileNamespace::Absolute,
        }
    }

    pub(crate) fn runtime(path: PathBuf, text: String) -> Self {
        Self {
            path,
            text,
            unix_mode: None,
            namespace: PreparedFileNamespace::Runtime,
        }
    }

    pub(crate) fn codex_target_owner_only(path: PathBuf, text: String) -> Self {
        Self {
            path,
            text,
            unix_mode: Some(0o600),
            namespace: PreparedFileNamespace::CodexTarget,
        }
    }
}

#[derive(Debug)]
struct FileSnapshot {
    path: PathBuf,
    text: Option<String>,
}

pub(crate) fn with_text_file_rollback<T>(
    paths: &[PathBuf],
    operation: impl FnOnce() -> Result<T, String>,
) -> Result<T, FileTransactionError> {
    with_text_file_rollback_using(paths, operation, restore_snapshot)
}

fn with_text_file_rollback_using<T, Restore>(
    paths: &[PathBuf],
    operation: impl FnOnce() -> Result<T, String>,
    mut restore: Restore,
) -> Result<T, FileTransactionError>
where
    Restore: FnMut(&FileSnapshot) -> Result<(), String>,
{
    let snapshots = snapshot_files(paths).map_err(FileTransactionError::SnapshotFailed)?;
    match operation() {
        Ok(value) => Ok(value),
        Err(error) => {
            let rollback_errors = snapshots
                .iter()
                .rev()
                .filter_map(|snapshot| restore(snapshot).err())
                .collect::<Vec<_>>();
            if rollback_errors.is_empty() {
                Err(FileTransactionError::RolledBack(error))
            } else {
                Err(FileTransactionError::RollbackFailed {
                    operation: error,
                    rollback: rollback_errors,
                })
            }
        }
    }
}

pub(crate) fn publish_prepared_text_files(files: &[PreparedTextFile]) -> Result<(), String> {
    for file in files {
        safe_file::write_text_atomic_with_mode(&file.path, &file.text, file.unix_mode)
            .map_err(|error| format!("failed to publish prepared file: {error}"))?;
    }
    Ok(())
}

fn snapshot_files(paths: &[PathBuf]) -> Result<Vec<FileSnapshot>, String> {
    let mut unique = Vec::<PathBuf>::new();
    for path in paths {
        if !unique.contains(path) {
            unique.push(path.clone());
        }
    }
    unique
        .into_iter()
        .map(|path| {
            let text = match fs::read_to_string(&path) {
                Ok(text) => Some(text),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
                Err(error) => {
                    return Err(format!("failed to snapshot transaction file: {error}"));
                }
            };
            Ok(FileSnapshot { path, text })
        })
        .collect()
}

fn restore_snapshot(snapshot: &FileSnapshot) -> Result<(), String> {
    match &snapshot.text {
        Some(text) => safe_file::write_text_atomic(&snapshot.path, text)
            .map_err(|error| format!("failed to restore transaction file: {error}")),
        None => remove_created_file(&snapshot.path),
    }
}

fn remove_created_file(path: &Path) -> Result<(), String> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!("failed to remove transaction-created file: {error}")),
    }
}

#[cfg(test)]
mod tests {
    use super::{with_text_file_rollback_using, FileSnapshot, FileTransactionError};
    use std::cell::Cell;
    use std::fs;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn every_failed_publish_point_restores_original_bytes_and_absence() {
        for fail_after in 0..=3 {
            let root = temp_root("failure-injection");
            fs::create_dir_all(&root).unwrap();
            let first = root.join("first.json");
            let second = root.join("second.json");
            let third = root.join("third.json");
            fs::write(&first, "old-first").unwrap();
            fs::write(&second, "old-second").unwrap();
            let paths = vec![first.clone(), second.clone(), third.clone()];
            let writes = Cell::new(0usize);

            let result = with_text_file_rollback_using(
                &paths,
                || {
                    for (path, text) in [
                        (&first, "new-first"),
                        (&second, "new-second"),
                        (&third, "new-third"),
                    ] {
                        if writes.get() == fail_after {
                            return Err(format!("injected failure after {fail_after} writes"));
                        }
                        fs::write(path, text).unwrap();
                        writes.set(writes.get() + 1);
                    }
                    if writes.get() == fail_after {
                        return Err(format!("injected failure after {fail_after} writes"));
                    }
                    Ok(())
                },
                restore_for_test,
            );

            assert!(result.is_err());
            assert_eq!(fs::read_to_string(&first).unwrap(), "old-first");
            assert_eq!(fs::read_to_string(&second).unwrap(), "old-second");
            assert!(!third.exists());
            let _ = fs::remove_dir_all(root);
        }
    }

    #[test]
    fn restore_failure_has_structured_uncertain_state_semantics() {
        let root = temp_root("restore-failure");
        fs::create_dir_all(&root).unwrap();
        let path = root.join("state.json");
        fs::write(&path, "old").unwrap();

        let error = with_text_file_rollback_using(
            std::slice::from_ref(&path),
            || {
                fs::write(&path, "new").unwrap();
                Err::<(), _>("publish failed".to_string())
            },
            |_| Err("restore denied".to_string()),
        )
        .expect_err("rollback failure");

        assert!(matches!(
            error,
            FileTransactionError::RollbackFailed { .. }
        ));
        assert!(error.rollback_failed());
        let _ = fs::remove_dir_all(root);
    }

    fn restore_for_test(snapshot: &FileSnapshot) -> Result<(), String> {
        match &snapshot.text {
            Some(text) => fs::write(&snapshot.path, text).map_err(|error| error.to_string()),
            None => match fs::remove_file(&snapshot.path) {
                Ok(()) => Ok(()),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
                Err(error) => Err(error.to_string()),
            },
        }
    }

    fn temp_root(name: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!("codexhub-file-transaction-{name}-{nonce}"))
    }
}
