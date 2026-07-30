use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

#[cfg(test)]
use std::cell::RefCell;

#[cfg(test)]
type TestPreOpenHook = Box<dyn Fn(&Path)>;

#[cfg(test)]
type TestLockAcquireHook = Box<dyn Fn(&Path, &'static str) + Send + Sync>;

#[cfg(test)]
thread_local! {
    static TEST_PRE_OPEN_EXISTING_HOOK: RefCell<Option<TestPreOpenHook>> = RefCell::new(None);
    static TEST_PRE_PRIVATE_PUBLISH_HOOK: RefCell<Option<TestPreOpenHook>> = RefCell::new(None);
    static TEST_PRE_PRIVATE_QUARANTINE_HOOK: RefCell<Option<TestPreOpenHook>> = RefCell::new(None);
    #[cfg(target_os = "linux")]
    static TEST_PRE_PRIVATE_QUARANTINE_RENAME_HOOK: RefCell<Option<TestPreOpenHook>> =
        RefCell::new(None);
    #[cfg(all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    ))]
    static TEST_PRE_PRIVATE_EVIDENCE_ISOLATE_HOOK: RefCell<Option<TestPreOpenHook>> =
        RefCell::new(None);
    #[cfg(any(
        windows,
        all(
            target_os = "linux",
            any(target_arch = "x86_64", target_arch = "aarch64")
        )
    ))]
    static TEST_PRE_PRIVATE_TEMP_CLEANUP_HOOK: RefCell<Option<TestPreOpenHook>> =
        RefCell::new(None);
    #[cfg(target_os = "linux")]
    static TEST_PRIVATE_QUARANTINE_FAULT_PHASE: RefCell<Option<&'static str>> =
        const { RefCell::new(None) };
}

/// Test-only hook invoked from the lock-acquisition seam. The event argument is
/// one of "blocked" or "acquired" so callers can observe genuine contention.
#[cfg(test)]
pub(crate) static TEST_LOCK_ACQUIRE_HOOK: std::sync::Mutex<Option<TestLockAcquireHook>> =
    std::sync::Mutex::new(None);

#[cfg(test)]
fn install_test_pre_open_hook(hook: impl Fn(&Path) + 'static) {
    TEST_PRE_OPEN_EXISTING_HOOK.with(|slot| *slot.borrow_mut() = Some(Box::new(hook)));
}

#[cfg(test)]
fn clear_test_pre_open_hook() {
    TEST_PRE_OPEN_EXISTING_HOOK.with(|slot| *slot.borrow_mut() = None);
}

#[cfg(test)]
fn invoke_test_pre_open_hook(path: &Path) {
    TEST_PRE_OPEN_EXISTING_HOOK.with(|slot| {
        if let Some(hook) = slot.borrow().as_ref() {
            hook(path);
        }
    });
}

#[cfg(test)]
fn install_test_pre_private_publish_hook(hook: impl Fn(&Path) + 'static) {
    TEST_PRE_PRIVATE_PUBLISH_HOOK.with(|slot| *slot.borrow_mut() = Some(Box::new(hook)));
}

#[cfg(test)]
fn clear_test_pre_private_publish_hook() {
    TEST_PRE_PRIVATE_PUBLISH_HOOK.with(|slot| *slot.borrow_mut() = None);
}

#[cfg(test)]
fn invoke_test_pre_private_publish_hook(path: &Path) {
    TEST_PRE_PRIVATE_PUBLISH_HOOK.with(|slot| {
        if let Some(hook) = slot.borrow().as_ref() {
            hook(path);
        }
    });
}

#[cfg(test)]
fn install_test_pre_private_quarantine_hook(hook: impl Fn(&Path) + 'static) {
    TEST_PRE_PRIVATE_QUARANTINE_HOOK.with(|slot| *slot.borrow_mut() = Some(Box::new(hook)));
}

#[cfg(test)]
fn clear_test_pre_private_quarantine_hook() {
    TEST_PRE_PRIVATE_QUARANTINE_HOOK.with(|slot| *slot.borrow_mut() = None);
}

#[cfg(test)]
fn invoke_test_pre_private_quarantine_hook(path: &Path) {
    TEST_PRE_PRIVATE_QUARANTINE_HOOK.with(|slot| {
        if let Some(hook) = slot.borrow().as_ref() {
            hook(path);
        }
    });
}

#[cfg(all(test, target_os = "linux"))]
pub(crate) fn install_test_pre_private_quarantine_rename_hook(hook: impl Fn(&Path) + 'static) {
    TEST_PRE_PRIVATE_QUARANTINE_RENAME_HOOK
        .with(|slot| *slot.borrow_mut() = Some(Box::new(hook)));
}

#[cfg(all(test, target_os = "linux"))]
pub(crate) fn clear_test_pre_private_quarantine_rename_hook() {
    TEST_PRE_PRIVATE_QUARANTINE_RENAME_HOOK.with(|slot| *slot.borrow_mut() = None);
}

#[cfg(all(test, target_os = "linux"))]
fn invoke_test_pre_private_quarantine_rename_hook(path: &Path) {
    TEST_PRE_PRIVATE_QUARANTINE_RENAME_HOOK.with(|slot| {
        if let Some(hook) = slot.borrow().as_ref() {
            hook(path);
        }
    });
}

#[cfg(all(
    test,
    target_os = "linux",
    any(target_arch = "x86_64", target_arch = "aarch64")
))]
pub(crate) fn install_test_pre_private_evidence_isolate_hook(hook: impl Fn(&Path) + 'static) {
    TEST_PRE_PRIVATE_EVIDENCE_ISOLATE_HOOK
        .with(|slot| *slot.borrow_mut() = Some(Box::new(hook)));
}

#[cfg(all(
    test,
    target_os = "linux",
    any(target_arch = "x86_64", target_arch = "aarch64")
))]
pub(crate) fn clear_test_pre_private_evidence_isolate_hook() {
    TEST_PRE_PRIVATE_EVIDENCE_ISOLATE_HOOK.with(|slot| *slot.borrow_mut() = None);
}

#[cfg(all(
    test,
    target_os = "linux",
    any(target_arch = "x86_64", target_arch = "aarch64")
))]
fn invoke_test_pre_private_evidence_isolate_hook(path: &Path) {
    TEST_PRE_PRIVATE_EVIDENCE_ISOLATE_HOOK.with(|slot| {
        if let Some(hook) = slot.borrow().as_ref() {
            hook(path);
        }
    });
}

#[cfg(all(
    test,
    any(
        windows,
        all(
            target_os = "linux",
            any(target_arch = "x86_64", target_arch = "aarch64")
        )
    )
))]
pub(crate) fn install_test_pre_private_temp_cleanup_hook(hook: impl Fn(&Path) + 'static) {
    TEST_PRE_PRIVATE_TEMP_CLEANUP_HOOK
        .with(|slot| *slot.borrow_mut() = Some(Box::new(hook)));
}

#[cfg(all(
    test,
    any(
        windows,
        all(
            target_os = "linux",
            any(target_arch = "x86_64", target_arch = "aarch64")
        )
    )
))]
pub(crate) fn clear_test_pre_private_temp_cleanup_hook() {
    TEST_PRE_PRIVATE_TEMP_CLEANUP_HOOK.with(|slot| *slot.borrow_mut() = None);
}

#[cfg(all(
    test,
    any(
        windows,
        all(
            target_os = "linux",
            any(target_arch = "x86_64", target_arch = "aarch64")
        )
    )
))]
fn invoke_test_pre_private_temp_cleanup_hook(path: &Path) {
    TEST_PRE_PRIVATE_TEMP_CLEANUP_HOOK.with(|slot| {
        if let Some(hook) = slot.borrow().as_ref() {
            hook(path);
        }
    });
}

#[cfg(all(test, target_os = "linux"))]
pub(crate) fn install_test_private_quarantine_fault(phase: &'static str) {
    TEST_PRIVATE_QUARANTINE_FAULT_PHASE.with(|slot| *slot.borrow_mut() = Some(phase));
}

#[cfg(all(test, target_os = "linux"))]
pub(crate) fn clear_test_private_quarantine_fault() {
    TEST_PRIVATE_QUARANTINE_FAULT_PHASE.with(|slot| *slot.borrow_mut() = None);
}

#[cfg(all(
    target_os = "linux",
    any(target_arch = "x86_64", target_arch = "aarch64")
))]
fn private_quarantine_fault(phase: &'static str) -> Result<(), String> {
    #[cfg(test)]
    if TEST_PRIVATE_QUARANTINE_FAULT_PHASE
        .with(|slot| slot.borrow().as_ref() == Some(&phase))
    {
        return Err(format!("injected private quarantine fault at {phase}"));
    }
    let _ = phase;
    Ok(())
}

const LOCK_WAIT_TIMEOUT: Duration = Duration::from_secs(10);
const LOCK_RETRY_DELAY: Duration = Duration::from_millis(25);
static NEXT_TEMP_NONCE: std::sync::atomic::AtomicU64 =
    std::sync::atomic::AtomicU64::new(0);
// Versioned lock record. Anything else is fail-closed:
// - unknown/future versions -> never recovered (fail closed);
// - legacy pid/timestamp records -> recovered only when the PID is provably
//   dead, otherwise fail closed;
// - mixed-version caveat: binaries older than this protocol may still reclaim
//   or unlink a protocol lock file (they classify anything non-legacy as
//   stale); old binaries cannot be patched, so upgrades must drain running
//   old processes before relying on overlap protection.
// Crash-recovery bound: a holder death releases its OS byte lock, so a new
// owner enters within LOCK_WAIT_TIMEOUT.
const LOCK_PROTOCOL: &str = "codexhub-atomic-lock=1\n";

#[cfg(windows)]
mod win32 {
    pub(crate) const SHARE_READ_WRITE_DELETE: u32 = 0x00000007;
    pub(crate) const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x00200000;
    pub(crate) const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
    pub(crate) const LOCKFILE_FAIL_IMMEDIATELY: u32 = 0x1;
    pub(crate) const LOCKFILE_EXCLUSIVE_LOCK: u32 = 0x2;
    pub(crate) const ERROR_SHARING_VIOLATION: i32 = 32;
    pub(crate) const ERROR_LOCK_VIOLATION: i32 = 33;
    pub(crate) const STILL_ACTIVE: u32 = 259;
    pub(crate) const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;
}

#[cfg(unix)]
mod flock_op {
    pub(crate) const LOCK_EX: i32 = 2;
    pub(crate) const LOCK_NB: i32 = 4;
    pub(crate) const LOCK_UN: i32 = 8;
    pub(crate) const ESRCH: i32 = 3;
}

#[cfg(target_os = "linux")]
mod unix_private_io {
    pub(crate) const O_RDONLY: i32 = 0;
    pub(crate) const O_WRONLY: i32 = 0x0001;
    pub(crate) const O_CREAT: i32 = 0x0040;
    pub(crate) const O_EXCL: i32 = 0x0080;
    pub(crate) const O_DIRECTORY: i32 = 0x1_0000;
    pub(crate) const O_NOFOLLOW: i32 = 0x2_0000;
    pub(crate) const O_CLOEXEC: i32 = 0x8_0000;
    pub(crate) const RENAME_NOREPLACE: u32 = 1;
    pub(crate) const RENAME_EXCHANGE: u32 = 2;
    #[cfg(target_arch = "x86_64")]
    pub(crate) const SYS_RENAMEAT2: isize = 316;
    #[cfg(target_arch = "aarch64")]
    pub(crate) const SYS_RENAMEAT2: isize = 276;
}

#[cfg(all(unix, not(target_os = "linux")))]
mod unix_private_io {
    pub(crate) const O_DIRECTORY: i32 = 0x0010_0000;
    pub(crate) const O_NOFOLLOW: i32 = 0x0000_0100;
}

pub(crate) fn write_text_atomic(path: &Path, text: &str) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            format!(
                "failed to create file directory {}: {error}",
                parent.display()
            )
        })?;
    }

    let lock = FileLock::acquire(path)?;
    write_text_locked(path, text, &lock)
}

pub(crate) fn write_private_text_atomic(
    path: &Path,
    text: &str,
    boundary: &Path,
) -> Result<(), String> {
    validate_confined_path(path, boundary, true)?;
    let pinned_parent = PinnedPrivateParent::open(path, boundary)?;
    let lock = FileLock::acquire(path)?;
    write_text_locked_impl(path, text, &lock, Some(&pinned_parent))
}

/// Quarantine a private text file while retaining the original bytes under a
/// transaction-owned name. Supported Linux targets atomically replace the
/// live entry with the supplied exact sentinel; other targets complete the
/// sentinel publication in the guarded caller immediately afterward.
///
/// The retained parent handle/dirfd prevents parent-path substitution from
/// escaping the validated directory. Production callers additionally hold the
/// shared provider/catalog transaction guard, which serializes cooperative
/// CodexHub writers. On supported Linux targets, an atomic exchange plus inode
/// readback detects a one-shot source-entry substitution and preserves the
/// substituted bytes as evidence. This is not an atomic conditional rename
/// against an arbitrary same-principal process that can continuously rewrite
/// entries in the directory; that stronger broker/ownership boundary is
/// intentionally outside this contract. Non-Linux Unix keeps the legacy
/// quarantine path and is explicitly outside the 0.1.8 production gate under
/// follow-up #270; the 0.1.8 gate covers Windows plus Linux x86_64/aarch64.
pub(crate) fn quarantine_private_text(
    source: &Path,
    quarantine: &Path,
    boundary: &Path,
    replacement: &str,
    max_bytes: u64,
    label: &str,
) -> Result<String, String> {
    if source.parent() != quarantine.parent() {
        return Err("private quarantine paths must share one parent".to_string());
    }
    validate_confined_path(source, boundary, false)?;
    validate_confined_path(quarantine, boundary, true)?;
    if replacement.len() as u64 > max_bytes {
        return Err(format!(
            "{label} replacement exceeds the size limit of {max_bytes} bytes"
        ));
    }
    let parent = PinnedPrivateParent::open(source, boundary)?;
    #[cfg(test)]
    invoke_test_pre_private_quarantine_hook(source);
    let mut opened = parent.open_existing(source).map_err(|error| {
        format!("failed to open {label} through its pinned parent: {error}")
    })?;
    let text = read_opened_single_link_text(&mut opened, source, max_bytes, label)?;
    let opened_identity = lock_file_identity(&opened)
        .map_err(|_| format!("{label} is not a stable regular single-link file"))?;
    #[cfg(all(test, target_os = "linux"))]
    invoke_test_pre_private_quarantine_rename_hook(source);

    #[cfg(all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    ))]
    {
        let placeholder = match parent.open_existing(quarantine) {
            Ok(mut placeholder) => {
                let contents = read_opened_single_link_text(
                    &mut placeholder,
                    quarantine,
                    max_bytes,
                    "private quarantine replacement placeholder",
                )?;
                if contents != replacement {
                    return Err(format!(
                        "private quarantine destination already exists with bytes other than the exact replacement sentinel: {}",
                        quarantine.display()
                    ));
                }
                placeholder
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let placeholder_temp = unique_temp_path(quarantine);
                let mut placeholder = parent.create_temp(&placeholder_temp).map_err(|error| {
                    format!(
                        "failed to create private quarantine replacement placeholder temp {}: {error}",
                        placeholder_temp.display()
                    )
                })?;
                let mut cleanup =
                    TempPathCleanup::new(&parent, &placeholder, placeholder_temp.clone())?;
                placeholder
                    .write_all(replacement.as_bytes())
                    .and_then(|_| placeholder.sync_all())
                    .map_err(|error| {
                        format!(
                            "failed to persist private quarantine replacement placeholder temp {}: {error}",
                            placeholder_temp.display()
                        )
                    })?;
                let metadata = placeholder.metadata().map_err(|error| {
                    format!(
                        "failed to inspect private quarantine replacement placeholder temp {}: {error}",
                        placeholder_temp.display()
                    )
                })?;
                validate_regular_single_link(&metadata, &placeholder_temp)?;
                parent.publish_new(&placeholder_temp, quarantine)?;
                cleanup.disarm();
                placeholder
            }
            Err(error) => {
                return Err(format!(
                    "failed to inspect private quarantine destination {} through its pinned parent: {error}",
                    quarantine.display()
                ));
            }
        };
        let placeholder_identity = lock_file_identity(&placeholder).map_err(|_| {
            "private quarantine replacement placeholder is not a stable regular single-link file"
                .to_string()
        })?;
        private_quarantine_fault("after-placeholder")?;
        private_quarantine_fault("before-exchange")?;
        parent.exchange_existing(source, quarantine)?;
        private_quarantine_fault("after-exchange")?;

        let mut live = parent.open_existing(source).map_err(|error| {
            format!(
                "failed to read back private replacement sentinel through its pinned parent: {error}"
            )
        })?;
        let live_text = read_opened_single_link_text(
            &mut live,
            source,
            max_bytes,
            "private replacement sentinel",
        )?;
        let live_identity = lock_file_identity(&live).map_err(|_| {
            "private replacement sentinel is not a stable regular single-link file".to_string()
        })?;
        if live_text != replacement || live_identity != placeholder_identity {
            return Err(format!(
                "private replacement sentinel identity or bytes changed after atomic exchange; quarantine evidence was preserved at {}",
                quarantine.display()
            ));
        }

        let quarantined = parent.open_existing(quarantine).map_err(|error| {
            format!(
                "failed to read back quarantined {label} through its pinned parent: {error}"
            )
        })?;
        let quarantined_identity = lock_file_identity(&quarantined)
            .map_err(|_| format!("quarantined {label} is not a stable regular single-link file"))?;
        if quarantined_identity != opened_identity {
            return Err(format!(
                "quarantined {label} identity did not match the opened source; replacement evidence was preserved at {}",
                quarantine.display()
            ));
        }
        return Ok(text);
    }

    #[cfg(not(all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    )))]
    {
        let _ = replacement;
        if quarantine.exists() {
            return Err(format!(
                "private quarantine destination already exists: {}",
                quarantine.display()
            ));
        }
        parent.rename_existing(&mut opened, source, quarantine)?;
        let quarantined = parent.open_existing(quarantine).map_err(|error| {
            format!("failed to read back quarantined {label} through its pinned parent: {error}")
        })?;
        let quarantined_identity = lock_file_identity(&quarantined)
            .map_err(|_| format!("quarantined {label} is not a stable regular single-link file"))?;
        if quarantined_identity != opened_identity {
            return Err(format!(
                "quarantined {label} identity did not match the opened source"
            ));
        }
        Ok(text)
    }
}

/// Conditional rollback is a 0.1.8 release-gated primitive. Only Windows and
/// Linux x86_64/aarch64 provide the required conditional replace/isolate
/// semantics; every other target returns a fail-closed unsupported error and
/// remains tracked by #270.
pub(crate) struct PrivateTextReplacement<'a> {
    pub(crate) path: &'a Path,
    pub(crate) evidence: &'a Path,
    pub(crate) boundary: &'a Path,
    pub(crate) contents: &'a str,
    pub(crate) max_bytes: u64,
    pub(crate) label: &'a str,
}

#[cfg(windows)]
pub(crate) fn replace_private_text_if_unchanged<Expected, BeforeCommit>(
    replacement: PrivateTextReplacement<'_>,
    expected: Expected,
    before_commit: BeforeCommit,
) -> Result<(), String>
where
    Expected: Fn(&str) -> bool,
    BeforeCommit: FnOnce(),
{
    let PrivateTextReplacement {
        path,
        evidence,
        boundary,
        contents: replacement,
        max_bytes,
        label,
    } = replacement;
    if path.parent() != evidence.parent() {
        return Err("conditional private replacement paths must share one parent".to_string());
    }
    validate_confined_path(path, boundary, true)?;
    validate_confined_path(evidence, boundary, true)?;
    let parent = PinnedPrivateParent::open(path, boundary)?;
    let (mut displaced, needs_isolate) = match parent.open_existing(path) {
        Ok(mut opened) => {
            let current = read_opened_single_link_text(&mut opened, path, max_bytes, label)?;
            if current == replacement {
                return finish_private_evidence_windows(
                    &parent,
                    evidence,
                    max_bytes,
                    label,
                    &expected,
                );
            }
            if !expected(&current) {
                return Err(format!(
                    "{label} changed before conditional restore; live bytes were preserved"
                ));
            }
            if evidence.try_exists().map_err(|error| {
                format!(
                    "failed to inspect conditional restore evidence {}: {error}",
                    evidence.display()
                )
            })? {
                return Err(format!(
                    "conditional restore evidence already exists while live {label} still requires replacement"
                ));
            }
            (opened, true)
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let mut displaced = parent.open_existing(evidence).map_err(|evidence_error| {
                format!(
                    "live {label} is absent and exact rollback evidence could not be opened: {evidence_error}"
                )
            })?;
            let displaced_text =
                read_opened_single_link_text(&mut displaced, evidence, max_bytes, label)?;
            if !expected(&displaced_text) {
                return Err(format!(
                    "live {label} is absent but rollback evidence is not the journaled owner"
                ));
            }
            (displaced, false)
        }
        Err(error) => {
            return Err(format!(
                "failed to open {label} before conditional restore: {error}"
            ));
        }
    };
    let displaced_identity = lock_file_identity(&displaced)
        .map_err(|_| format!("{label} is not a stable regular single-link file"))?;

    let temp_path = unique_temp_path(path);
    let mut temp = parent.create_temp(&temp_path).map_err(|error| {
        format!(
            "failed to create conditional restore temp {}: {error}",
            temp_path.display()
        )
    })?;
    let mut cleanup = TempPathCleanup::new(&parent, &temp, temp_path.clone())?;
    temp.write_all(replacement.as_bytes())
        .and_then(|_| temp.sync_all())
        .map_err(|error| {
            format!(
                "failed to persist conditional restore temp {}: {error}",
                temp_path.display()
            )
        })?;
    let replacement_identity = lock_file_identity(&temp)
        .map_err(|_| format!("conditional {label} replacement is not a stable file"))?;
    before_commit();

    if needs_isolate {
        parent
            .rename_existing(&mut displaced, path, evidence)
            .map_err(|error| {
                format!(
                    "failed to isolate {label} without replacing rollback evidence: {error}"
                )
            })?;
        match parent.open_existing(path) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Ok(_) => {
                return Err(format!(
                    "live {label} changed during conditional isolation; exact evidence remains at {}",
                    evidence.display()
                ));
            }
            Err(error) => {
                return Err(format!(
                    "failed to verify live {label} absence after conditional isolation: {error}"
                ));
            }
        }
    }

    parent
        .publish_new(&mut temp, path)
        .map_err(|error| format!("failed to publish restored {label} without replacement: {error}"))?;
    cleanup.disarm();

    let mut live = parent.open_existing(path).map_err(|error| {
        format!("failed to open restored {label} after conditional publication: {error}")
    })?;
    let live_text = read_opened_single_link_text(&mut live, path, max_bytes, label)?;
    let live_identity = lock_file_identity(&live)
        .map_err(|_| format!("restored {label} is not a stable single-link file"))?;
    if live_identity != replacement_identity || live_text != replacement {
        return Err(format!(
            "restored {label} mismatch after conditional publication; rollback evidence was preserved"
        ));
    }

    displaced
        .seek(SeekFrom::Start(0))
        .map_err(|error| format!("failed to rewind displaced {label} evidence: {error}"))?;
    let displaced_text =
        read_opened_single_link_text(&mut displaced, evidence, max_bytes, label)?;
    let current_evidence = parent.open_existing(evidence).map_err(|error| {
        format!("failed to re-open displaced {label} evidence after restore: {error}")
    })?;
    let current_evidence_identity = lock_file_identity(&current_evidence)
        .map_err(|_| format!("current {label} evidence is not a stable single-link file"))?;
    if current_evidence_identity != displaced_identity || !expected(&displaced_text) {
        return Err(format!(
            "displaced {label} mismatch after conditional restore; exact evidence remains at {}",
            evidence.display()
        ));
    }
    delete_opened_file_windows(&mut displaced, label)
}

#[cfg(all(
    target_os = "linux",
    any(target_arch = "x86_64", target_arch = "aarch64")
))]
pub(crate) fn replace_private_text_if_unchanged<Expected, BeforeCommit>(
    replacement: PrivateTextReplacement<'_>,
    expected: Expected,
    before_commit: BeforeCommit,
) -> Result<(), String>
where
    Expected: Fn(&str) -> bool,
    BeforeCommit: FnOnce(),
{
    let PrivateTextReplacement {
        path,
        evidence,
        boundary,
        contents: replacement,
        max_bytes,
        label,
    } = replacement;
    if path.parent() != evidence.parent() {
        return Err("conditional private replacement paths must share one parent".to_string());
    }
    validate_confined_path(path, boundary, false)?;
    validate_confined_path(evidence, boundary, true)?;
    let parent = PinnedPrivateParent::open(path, boundary)?;
    let mut opened = parent
        .open_existing(path)
        .map_err(|error| format!("failed to open {label} before conditional restore: {error}"))?;
    let current = read_opened_single_link_text(&mut opened, path, max_bytes, label)?;
    if current == replacement {
        return finish_private_evidence_linux(
            &parent,
            evidence,
            max_bytes,
            label,
            &expected,
        );
    }
    if !expected(&current) {
        return Err(format!(
            "{label} changed before conditional restore; live bytes were preserved"
        ));
    }
    let opened_identity = lock_file_identity(&opened)
        .map_err(|_| format!("{label} is not a stable regular single-link file"))?;

    let placeholder = match parent.open_existing(evidence) {
        Ok(mut placeholder) => {
            let contents = read_opened_single_link_text(
                &mut placeholder,
                evidence,
                max_bytes,
                "conditional restore placeholder",
            )?;
            if contents != replacement {
                return Err(format!(
                    "conditional restore evidence contains neither the exact placeholder nor a completed owner: {}",
                    evidence.display()
                ));
            }
            placeholder
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let temp_path = unique_temp_path(evidence);
            let mut placeholder = parent.create_temp(&temp_path).map_err(|error| {
                format!(
                    "failed to create conditional restore placeholder temp {}: {error}",
                    temp_path.display()
                )
            })?;
            let mut cleanup = TempPathCleanup::new(&parent, &placeholder, temp_path.clone())?;
            placeholder
                .write_all(replacement.as_bytes())
                .and_then(|_| placeholder.sync_all())
                .map_err(|error| {
                    format!(
                        "failed to persist conditional restore placeholder temp {}: {error}",
                        temp_path.display()
                    )
                })?;
            parent.publish_new(&temp_path, evidence)?;
            cleanup.disarm();
            placeholder
        }
        Err(error) => {
            return Err(format!(
                "failed to inspect conditional restore evidence {}: {error}",
                evidence.display()
            ));
        }
    };
    let placeholder_identity = lock_file_identity(&placeholder)
        .map_err(|_| "conditional restore placeholder is not a stable file".to_string())?;
    before_commit();
    parent.exchange_existing(path, evidence)?;

    let mut live = parent.open_existing(path).map_err(|error| {
        format!("failed to open restored {label} after conditional exchange: {error}")
    })?;
    let live_text = read_opened_single_link_text(&mut live, path, max_bytes, label)?;
    let live_identity = lock_file_identity(&live)
        .map_err(|_| format!("restored {label} is not a stable file"))?;
    if live_text != replacement || live_identity != placeholder_identity {
        return Err(format!(
            "restored {label} did not match the exact replacement after conditional exchange"
        ));
    }
    let mut displaced = parent.open_existing(evidence).map_err(|error| {
        format!("failed to open displaced {label} after conditional exchange: {error}")
    })?;
    let displaced_text =
        read_opened_single_link_text(&mut displaced, evidence, max_bytes, label)?;
    let displaced_identity = lock_file_identity(&displaced)
        .map_err(|_| format!("displaced {label} is not a stable file"))?;
    if displaced_identity != opened_identity || !expected(&displaced_text) {
        return Err(format!(
            "displaced {label} mismatch after conditional exchange; exact evidence remains at {}",
            evidence.display()
        ));
    }
    remove_opened_file_linux(&parent, &displaced, evidence, label)
}

#[cfg(not(any(
    windows,
    all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    )
)))]
pub(crate) fn replace_private_text_if_unchanged<Expected, BeforeCommit>(
    replacement: PrivateTextReplacement<'_>,
    expected: Expected,
    before_commit: BeforeCommit,
) -> Result<(), String>
where
    Expected: Fn(&str) -> bool,
    BeforeCommit: FnOnce(),
{
    let PrivateTextReplacement {
        path,
        evidence,
        boundary,
        contents: replacement,
        max_bytes,
        label,
    } = replacement;
    let _ = (
        path,
        evidence,
        boundary,
        replacement,
        max_bytes,
        expected,
        before_commit,
    );
    Err(format!(
        "conditional rollback replacement for {label} is supported only by the Windows and Linux x86_64/aarch64 0.1.8 release gates"
    ))
}

#[cfg(windows)]
pub(crate) fn remove_private_text_if_unchanged<Expected, BeforeCommit>(
    path: &Path,
    evidence: &Path,
    boundary: &Path,
    max_bytes: u64,
    label: &str,
    expected: Expected,
    before_commit: BeforeCommit,
) -> Result<(), String>
where
    Expected: Fn(&str) -> bool,
    BeforeCommit: FnOnce(),
{
    if path.parent() != evidence.parent() {
        return Err("conditional private removal paths must share one parent".to_string());
    }
    validate_confined_path(path, boundary, true)?;
    validate_confined_path(evidence, boundary, true)?;
    let parent = PinnedPrivateParent::open(path, boundary)?;
    let mut opened = match parent.open_existing(path) {
        Ok(opened) => opened,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return finish_private_evidence_windows(
                &parent,
                evidence,
                max_bytes,
                label,
                &expected,
            );
        }
        Err(error) => {
            return Err(format!(
                "failed to open {label} before conditional removal: {error}"
            ));
        }
    };
    let current = read_opened_single_link_text(&mut opened, path, max_bytes, label)?;
    if !expected(&current) {
        return Err(format!(
            "{label} changed before conditional removal; live bytes were preserved"
        ));
    }
    let opened_identity = lock_file_identity(&opened)
        .map_err(|_| format!("{label} is not a stable regular single-link file"))?;
    if evidence.try_exists().map_err(|error| {
        format!(
            "failed to inspect conditional removal evidence {}: {error}",
            evidence.display()
        )
    })? {
        return Err(format!(
            "conditional removal evidence already exists while live {label} remains"
        ));
    }
    before_commit();
    parent.rename_existing(&mut opened, path, evidence)?;

    match parent.open_existing(path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Ok(_) => {
            return Err(format!(
                "live {label} changed during conditional removal; displaced evidence remains at {}",
                evidence.display()
            ));
        }
        Err(error) => {
            return Err(format!(
                "failed to verify live {label} absence after conditional removal: {error}"
            ));
        }
    }
    let mut displaced = parent.open_existing(evidence).map_err(|error| {
        format!("failed to open displaced {label} after conditional removal: {error}")
    })?;
    let displaced_text =
        read_opened_single_link_text(&mut displaced, evidence, max_bytes, label)?;
    let displaced_identity = lock_file_identity(&displaced)
        .map_err(|_| format!("displaced {label} is not a stable single-link file"))?;
    if displaced_identity != opened_identity || !expected(&displaced_text) {
        return Err(format!(
            "displaced {label} mismatch after conditional removal; exact evidence remains at {}",
            evidence.display()
        ));
    }
    delete_opened_file_windows(&mut displaced, label)
}

#[cfg(all(
    target_os = "linux",
    any(target_arch = "x86_64", target_arch = "aarch64")
))]
pub(crate) fn remove_private_text_if_unchanged<Expected, BeforeCommit>(
    path: &Path,
    evidence: &Path,
    boundary: &Path,
    max_bytes: u64,
    label: &str,
    expected: Expected,
    before_commit: BeforeCommit,
) -> Result<(), String>
where
    Expected: Fn(&str) -> bool,
    BeforeCommit: FnOnce(),
{
    if path.parent() != evidence.parent() {
        return Err("conditional private removal paths must share one parent".to_string());
    }
    validate_confined_path(path, boundary, true)?;
    validate_confined_path(evidence, boundary, true)?;
    let parent = PinnedPrivateParent::open(path, boundary)?;
    let mut opened = match parent.open_existing(path) {
        Ok(opened) => opened,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return finish_private_evidence_linux(
                &parent,
                evidence,
                max_bytes,
                label,
                &expected,
            );
        }
        Err(error) => {
            return Err(format!(
                "failed to open {label} before conditional removal: {error}"
            ));
        }
    };
    let current = read_opened_single_link_text(&mut opened, path, max_bytes, label)?;
    if !expected(&current) {
        return Err(format!(
            "{label} changed before conditional removal; live bytes were preserved"
        ));
    }
    let opened_identity = lock_file_identity(&opened)
        .map_err(|_| format!("{label} is not a stable regular single-link file"))?;
    if evidence.try_exists().map_err(|error| {
        format!(
            "failed to inspect conditional removal evidence {}: {error}",
            evidence.display()
        )
    })? {
        return Err(format!(
            "conditional removal evidence already exists while live {label} remains"
        ));
    }
    before_commit();
    parent.rename_path_noreplace(path, evidence)?;

    match parent.open_existing(path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Ok(_) => {
            return Err(format!(
                "live {label} changed during conditional removal; displaced evidence remains at {}",
                evidence.display()
            ));
        }
        Err(error) => {
            return Err(format!(
                "failed to verify live {label} absence after conditional removal: {error}"
            ));
        }
    }
    let mut displaced = parent.open_existing(evidence).map_err(|error| {
        format!("failed to open displaced {label} after conditional removal: {error}")
    })?;
    let displaced_text =
        read_opened_single_link_text(&mut displaced, evidence, max_bytes, label)?;
    let displaced_identity = lock_file_identity(&displaced)
        .map_err(|_| format!("displaced {label} is not a stable file"))?;
    if displaced_identity != opened_identity || !expected(&displaced_text) {
        return Err(format!(
            "displaced {label} mismatch after conditional removal; exact evidence remains at {}",
            evidence.display()
        ));
    }
    remove_opened_file_linux(&parent, &displaced, evidence, label)
}

#[cfg(not(any(
    windows,
    all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    )
)))]
pub(crate) fn remove_private_text_if_unchanged<Expected, BeforeCommit>(
    path: &Path,
    evidence: &Path,
    boundary: &Path,
    max_bytes: u64,
    label: &str,
    expected: Expected,
    before_commit: BeforeCommit,
) -> Result<(), String>
where
    Expected: Fn(&str) -> bool,
    BeforeCommit: FnOnce(),
{
    let _ = (
        path,
        evidence,
        boundary,
        max_bytes,
        expected,
        before_commit,
    );
    Err(format!(
        "conditional rollback removal for {label} is supported only by the Windows and Linux x86_64/aarch64 0.1.8 release gates"
    ))
}

#[cfg_attr(test, allow(dead_code))]
pub(crate) fn read_private_text(
    path: &Path,
    boundary: &Path,
    max_bytes: u64,
    label: &str,
) -> Result<String, String> {
    validate_confined_path(path, boundary, false)?;
    let parent = PinnedPrivateParent::open(path, boundary)?;
    let mut opened = parent
        .open_existing(path)
        .map_err(|error| format!("failed to open {label} through its pinned parent: {error}"))?;
    read_opened_single_link_text(&mut opened, path, max_bytes, label)
}

fn read_opened_single_link_text(
    file: &mut File,
    path: &Path,
    max_bytes: u64,
    label: &str,
) -> Result<String, String> {
    let metadata = file
        .metadata()
        .map_err(|error| format!("failed to inspect {label}: {error}"))?;
    validate_regular_single_link(&metadata, path)?;
    if metadata.len() > max_bytes {
        return Err(format!(
            "{label} exceeds the size limit of {max_bytes} bytes"
        ));
    }
    let mut bytes = Vec::new();
    std::io::Read::by_ref(file)
        .take(max_bytes.saturating_add(1))
        .read_to_end(&mut bytes)
        .map_err(|error| format!("failed to read {label}: {error}"))?;
    if bytes.len() as u64 > max_bytes {
        return Err(format!(
            "{label} exceeds the size limit of {max_bytes} bytes"
        ));
    }
    String::from_utf8(bytes).map_err(|_| format!("{label} is not valid UTF-8"))
}

/// Write `text` to `path` using a temp file and atomic rename while already
/// holding an exclusive lock on `path`. Used for multi-step check-then-write
/// operations that must remain atomic across processes.
pub(crate) fn write_text_locked(path: &Path, text: &str, lock: &FileLock) -> Result<(), String> {
    write_text_locked_impl(path, text, lock, None)
}

fn write_text_locked_impl(
    path: &Path,
    text: &str,
    lock: &FileLock,
    private_parent: Option<&PinnedPrivateParent>,
) -> Result<(), String> {
    if lock.target_path() != path {
        return Err("atomic write lock does not match target path".to_owned());
    }
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            format!(
                "failed to create file directory {}: {error}",
                parent.display()
            )
        })?;
    }

    lock.verify_namespace_identity()?;
    let temp_path = unique_temp_path(path);
    let mut temp_file = match private_parent {
        Some(parent) => parent.create_temp(&temp_path),
        None => create_new_temp_file(&temp_path, false),
    }
        .map_err(|error| format!("failed to write temp file {}: {error}", temp_path.display()))?;
    temp_file
        .write_all(text.as_bytes())
        .and_then(|_| temp_file.sync_all())
        .map_err(|error| {
            let _ = fs::remove_file(&temp_path);
            format!("failed to write temp file {}: {error}", temp_path.display())
        })?;
    lock.verify_namespace_identity().inspect_err(|_| {
        let _ = fs::remove_file(&temp_path);
    })?;
    #[cfg(test)]
    if private_parent.is_some() {
        invoke_test_pre_private_publish_hook(path);
    }
    match private_parent {
        Some(parent) => parent.publish(&mut temp_file, &temp_path, path),
        None => {
            drop(temp_file);
            fs::rename(&temp_path, path).map_err(|error| {
                let _ = fs::remove_file(&temp_path);
                format!(
                    "failed to move temp file {} to {}: {error}",
                    temp_path.display(),
                    path.display()
                )
            })
        }
    }
}

#[cfg(unix)]
struct PinnedPrivateParent {
    directory: File,
    parent_path: PathBuf,
}

#[cfg(unix)]
impl PinnedPrivateParent {
    fn open(path: &Path, boundary: &Path) -> Result<Self, String> {
        use std::os::unix::fs::{MetadataExt, OpenOptionsExt};

        validate_confined_path(path, boundary, true)?;
        let parent_path = path
            .parent()
            .ok_or_else(|| "confined path has no parent".to_string())?
            .to_path_buf();
        let directory = OpenOptions::new()
            .read(true)
            .custom_flags(unix_private_io::O_DIRECTORY | unix_private_io::O_NOFOLLOW)
            .open(&parent_path)
            .map_err(|error| {
                format!(
                    "failed to pin private file parent {}: {error}",
                    parent_path.display()
                )
            })?;
        let opened = directory.metadata().map_err(|error| {
            format!(
                "failed to inspect pinned private file parent {}: {error}",
                parent_path.display()
            )
        })?;
        let current = fs::metadata(&parent_path).map_err(|error| {
            format!(
                "failed to recheck private file parent {}: {error}",
                parent_path.display()
            )
        })?;
        if !opened.is_dir()
            || opened.dev() != current.dev()
            || opened.ino() != current.ino()
        {
            return Err(format!(
                "private file parent identity changed before pinning {}",
                parent_path.display()
            ));
        }
        Ok(Self {
            directory,
            parent_path,
        })
    }

    fn create_temp(&self, temp_path: &Path) -> std::io::Result<File> {
        #[cfg(target_os = "linux")]
        {
            use std::ffi::CString;
            use std::os::fd::{AsRawFd, FromRawFd};
            use std::os::unix::ffi::OsStrExt;

            let name = temp_path
                .file_name()
                .ok_or_else(|| std::io::Error::from(std::io::ErrorKind::InvalidInput))?;
            let name = CString::new(name.as_bytes())
                .map_err(|_| std::io::Error::from(std::io::ErrorKind::InvalidInput))?;
            let fd = unsafe {
                openat(
                    self.directory.as_raw_fd(),
                    name.as_ptr(),
                    unix_private_io::O_WRONLY
                        | unix_private_io::O_CREAT
                        | unix_private_io::O_EXCL
                        | unix_private_io::O_NOFOLLOW
                        | unix_private_io::O_CLOEXEC,
                    0o600,
                )
            };
            if fd < 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(unsafe { File::from_raw_fd(fd) })
        }
        #[cfg(not(target_os = "linux"))]
        {
            let _ = &self.parent_path;
            create_new_temp_file(temp_path, true)
        }
    }

    fn open_existing(&self, path: &Path) -> std::io::Result<File> {
        use std::ffi::CString;
        use std::os::fd::{AsRawFd, FromRawFd};
        use std::os::unix::ffi::OsStrExt;

        let name = path
            .file_name()
            .ok_or_else(|| std::io::Error::from(std::io::ErrorKind::InvalidInput))?;
        let name = CString::new(name.as_bytes())
            .map_err(|_| std::io::Error::from(std::io::ErrorKind::InvalidInput))?;
        let fd = unsafe {
            openat(
                self.directory.as_raw_fd(),
                name.as_ptr(),
                unix_private_io::O_RDONLY | unix_private_io::O_NOFOLLOW,
                0,
            )
        };
        if fd < 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(unsafe { File::from_raw_fd(fd) })
    }

    #[cfg(not(all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    )))]
    fn rename_existing(
        &self,
        _opened: &mut File,
        source_path: &Path,
        target_path: &Path,
    ) -> Result<(), String> {
        use std::ffi::CString;
        use std::os::fd::AsRawFd;
        use std::os::unix::ffi::OsStrExt;

        let source_name = CString::new(
            source_path
                .file_name()
                .ok_or_else(|| "private quarantine source has no file name".to_string())?
                .as_bytes(),
        )
        .map_err(|_| "private quarantine source name contains a NUL byte".to_string())?;
        let target_name = CString::new(
            target_path
                .file_name()
                .ok_or_else(|| "private quarantine target has no file name".to_string())?
                .as_bytes(),
        )
        .map_err(|_| "private quarantine target name contains a NUL byte".to_string())?;
        let rename_result = unsafe {
            renameat(
                self.directory.as_raw_fd(),
                source_name.as_ptr(),
                self.directory.as_raw_fd(),
                target_name.as_ptr(),
            )
        };
        if rename_result != 0
        {
            return Err(format!(
                "failed to quarantine private file in pinned parent {}: {}",
                self.parent_path.display(),
                std::io::Error::last_os_error()
            ));
        }
        self.directory.sync_all().map_err(|error| {
            format!(
                "failed to flush private file parent {}: {error}",
                self.parent_path.display()
            )
        })
    }

    #[cfg(all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    ))]
    fn exchange_existing(&self, source_path: &Path, target_path: &Path) -> Result<(), String> {
        use std::ffi::CString;
        use std::os::fd::AsRawFd;
        use std::os::unix::ffi::OsStrExt;

        private_quarantine_fault("exchange")?;
        let source_name = CString::new(
            source_path
                .file_name()
                .ok_or_else(|| "private exchange source has no file name".to_string())?
                .as_bytes(),
        )
        .map_err(|_| "private exchange source name contains a NUL byte".to_string())?;
        let target_name = CString::new(
            target_path
                .file_name()
                .ok_or_else(|| "private exchange target has no file name".to_string())?
                .as_bytes(),
        )
        .map_err(|_| "private exchange target name contains a NUL byte".to_string())?;
        let exchange_result = unsafe {
            syscall(
                unix_private_io::SYS_RENAMEAT2,
                self.directory.as_raw_fd(),
                source_name.as_ptr(),
                self.directory.as_raw_fd(),
                target_name.as_ptr(),
                unix_private_io::RENAME_EXCHANGE,
            ) as i32
        };
        if exchange_result != 0 {
            return Err(format!(
                "failed to atomically exchange private file with its replacement in pinned parent {}: {}",
                self.parent_path.display(),
                std::io::Error::last_os_error()
            ));
        }
        self.directory.sync_all().map_err(|error| {
            format!(
                "failed to flush private exchange parent {}: {error}",
                self.parent_path.display()
            )
        })
    }

    #[cfg(all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    ))]
    fn publish_new(&self, source_path: &Path, target_path: &Path) -> Result<(), String> {
        use std::ffi::CString;
        use std::os::fd::AsRawFd;
        use std::os::unix::ffi::OsStrExt;

        private_quarantine_fault("placeholder-publish")?;
        let source_name = CString::new(
            source_path
                .file_name()
                .ok_or_else(|| "private placeholder temp has no file name".to_string())?
                .as_bytes(),
        )
        .map_err(|_| "private placeholder temp name contains a NUL byte".to_string())?;
        let target_name = CString::new(
            target_path
                .file_name()
                .ok_or_else(|| "private placeholder target has no file name".to_string())?
                .as_bytes(),
        )
        .map_err(|_| "private placeholder target name contains a NUL byte".to_string())?;
        let publish_result = unsafe {
            syscall(
                unix_private_io::SYS_RENAMEAT2,
                self.directory.as_raw_fd(),
                source_name.as_ptr(),
                self.directory.as_raw_fd(),
                target_name.as_ptr(),
                unix_private_io::RENAME_NOREPLACE,
            ) as i32
        };
        if publish_result != 0 {
            return Err(format!(
                "failed to publish private replacement placeholder without replacing evidence in pinned parent {}: {}",
                self.parent_path.display(),
                std::io::Error::last_os_error()
            ));
        }
        self.directory.sync_all().map_err(|error| {
            format!(
                "failed to flush private placeholder parent {}: {error}",
                self.parent_path.display()
            )
        })
    }

    #[cfg(all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    ))]
    fn rename_path_noreplace(
        &self,
        source_path: &Path,
        target_path: &Path,
    ) -> Result<(), String> {
        use std::ffi::CString;
        use std::os::fd::AsRawFd;
        use std::os::unix::ffi::OsStrExt;

        let source_name = CString::new(
            source_path
                .file_name()
                .ok_or_else(|| "private isolate source has no file name".to_string())?
                .as_bytes(),
        )
        .map_err(|_| "private isolate source name contains a NUL byte".to_string())?;
        let target_name = CString::new(
            target_path
                .file_name()
                .ok_or_else(|| "private isolate target has no file name".to_string())?
                .as_bytes(),
        )
        .map_err(|_| "private isolate target name contains a NUL byte".to_string())?;
        let isolate_result = unsafe {
            syscall(
                unix_private_io::SYS_RENAMEAT2,
                self.directory.as_raw_fd(),
                source_name.as_ptr(),
                self.directory.as_raw_fd(),
                target_name.as_ptr(),
                unix_private_io::RENAME_NOREPLACE,
            ) as i32
        };
        if isolate_result != 0 {
            return Err(format!(
                "failed to isolate private file without replacing evidence in pinned parent {}: {}",
                self.parent_path.display(),
                std::io::Error::last_os_error()
            ));
        }
        self.directory.sync_all().map_err(|error| {
            format!(
                "failed to flush private isolate parent {}: {error}",
                self.parent_path.display()
            )
        })
    }

    fn publish(
        &self,
        _temp_file: &mut File,
        temp_path: &Path,
        target_path: &Path,
    ) -> Result<(), String> {
        #[cfg(target_os = "linux")]
        {
            use std::ffi::CString;
            use std::os::fd::AsRawFd;
            use std::os::unix::ffi::OsStrExt;

            let temp_name = CString::new(
                temp_path
                    .file_name()
                    .ok_or_else(|| "private temp path has no file name".to_string())?
                    .as_bytes(),
            )
            .map_err(|_| "private temp file name contains a NUL byte".to_string())?;
            let target_name = CString::new(
                target_path
                    .file_name()
                    .ok_or_else(|| "private target path has no file name".to_string())?
                    .as_bytes(),
            )
            .map_err(|_| "private target file name contains a NUL byte".to_string())?;
            if unsafe {
                renameat(
                    self.directory.as_raw_fd(),
                    temp_name.as_ptr(),
                    self.directory.as_raw_fd(),
                    target_name.as_ptr(),
                )
            } != 0
            {
                return Err(format!(
                    "failed to publish private temp file in pinned parent {}: {}",
                    self.parent_path.display(),
                    std::io::Error::last_os_error()
                ));
            }
            self.directory.sync_all().map_err(|error| {
                format!(
                    "failed to flush private file parent {}: {error}",
                    self.parent_path.display()
                )
            })?;
            Ok(())
        }
        #[cfg(not(target_os = "linux"))]
        {
            fs::rename(temp_path, target_path).map_err(|error| {
                format!(
                    "failed to move temp file {} to {}: {error}",
                    temp_path.display(),
                    target_path.display()
                )
            })
        }
    }
}

#[cfg(windows)]
struct PinnedPrivateParent {
    directory: File,
    parent_path: PathBuf,
}

#[cfg(windows)]
impl PinnedPrivateParent {
    fn open(path: &Path, boundary: &Path) -> Result<Self, String> {
        validate_confined_path(path, boundary, true)?;
        let parent_path = path
            .parent()
            .ok_or_else(|| "confined path has no parent".to_string())?
            .to_path_buf();
        let (directory, identity) = open_windows_directory(&parent_path)?;
        let (current, current_identity) = open_windows_directory(&parent_path)?;
        drop(current);
        if identity != current_identity {
            return Err(format!(
                "private file parent identity changed before pinning {}",
                parent_path.display()
            ));
        }
        Ok(Self {
            directory,
            parent_path,
        })
    }

    fn create_temp(&self, temp_path: &Path) -> std::io::Result<File> {
        let _ = &self.parent_path;
        create_private_temp_file_windows(temp_path)
    }

    fn open_existing(&self, path: &Path) -> std::io::Result<File> {
        open_existing_private_file_windows(path)
    }

    fn rename_existing(
        &self,
        opened: &mut File,
        _source_path: &Path,
        target_path: &Path,
    ) -> Result<(), String> {
        self.rename_opened_file(opened, target_path, false)
    }

    fn publish(
        &self,
        temp_file: &mut File,
        _temp_path: &Path,
        target_path: &Path,
    ) -> Result<(), String> {
        self.rename_opened_file(temp_file, target_path, true)
    }

    fn publish_new(&self, temp_file: &mut File, target_path: &Path) -> Result<(), String> {
        self.rename_opened_file(temp_file, target_path, false)
    }

    fn rename_opened_file(
        &self,
        file: &mut File,
        target_path: &Path,
        replace_existing: bool,
    ) -> Result<(), String> {
        use std::os::windows::ffi::OsStrExt;
        use std::os::windows::io::AsRawHandle;

        let target_name = target_path
            .file_name()
            .ok_or_else(|| "private target path has no file name".to_string())?
            .encode_wide()
            .collect::<Vec<_>>();
        let total_bytes = std::mem::size_of::<FileRenameInformation>()
            + target_name.len() * std::mem::size_of::<u16>();
        let words = total_bytes.div_ceil(std::mem::size_of::<usize>());
        let mut storage = vec![0_usize; words];
        let information = storage.as_mut_ptr().cast::<FileRenameInformation>();
        unsafe {
            (*information).flags = u32::from(replace_existing);
            (*information).root_directory = self.directory.as_raw_handle();
            (*information).file_name_length =
                u32::try_from(target_name.len() * std::mem::size_of::<u16>())
                    .map_err(|_| "private target file name is too long".to_string())?;
            std::ptr::copy_nonoverlapping(
                target_name.as_ptr(),
                std::ptr::addr_of_mut!((*information).file_name).cast::<u16>(),
                target_name.len(),
            );
        }
        let mut io_status = IoStatusBlock::default();
        let status = unsafe {
            NtSetInformationFile(
                file.as_raw_handle(),
                &mut io_status,
                information.cast(),
                u32::try_from(total_bytes)
                    .map_err(|_| "private rename information is too large".to_string())?,
                10,
            )
        };
        if status < 0 {
            let code = unsafe { RtlNtStatusToDosError(status) };
            return Err(format!(
                "failed to publish private temp file in pinned parent {}: {}",
                self.parent_path.display(),
                std::io::Error::from_raw_os_error(i32::try_from(code).unwrap_or(i32::MAX))
            ));
        }
        Ok(())
    }
}

#[cfg(unix)]
fn create_new_temp_file(path: &Path, private: bool) -> std::io::Result<File> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    if private {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options.open(path)
}

#[cfg(windows)]
fn create_new_temp_file(path: &Path, private: bool) -> std::io::Result<File> {
    if !private {
        return OpenOptions::new().write(true).create_new(true).open(path);
    }
    create_private_temp_file_windows(path)
}

#[cfg(not(any(unix, windows)))]
fn create_new_temp_file(path: &Path, _private: bool) -> std::io::Result<File> {
    OpenOptions::new().write(true).create_new(true).open(path)
}

#[cfg(windows)]
fn create_private_temp_file_windows(path: &Path) -> std::io::Result<File> {
    use std::os::windows::io::FromRawHandle;

    const SDDL_REVISION_1: u32 = 1;
    const GENERIC_WRITE: u32 = 0x4000_0000;
    const DELETE: u32 = 0x0001_0000;
    const CREATE_NEW: u32 = 1;
    const FILE_ATTRIBUTE_NORMAL: u32 = 0x0000_0080;
    const PRIVATE_SDDL: &str = "D:P(A;;FA;;;OW)(A;;FA;;;SY)";

    let wide_path = windows_wide_path(path)?;
    let wide_sddl = PRIVATE_SDDL
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect::<Vec<_>>();
    let mut descriptor = std::ptr::null_mut();
    let converted = unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            wide_sddl.as_ptr(),
            SDDL_REVISION_1,
            &mut descriptor,
            std::ptr::null_mut(),
        )
    };
    if converted == 0 || descriptor.is_null() {
        return Err(std::io::Error::last_os_error());
    }
    let mut attributes = SecurityAttributes {
        length: u32::try_from(std::mem::size_of::<SecurityAttributes>()).unwrap_or(u32::MAX),
        security_descriptor: descriptor,
        inherit_handle: 0,
    };
    let handle = unsafe {
        CreateFileW(
            wide_path.as_ptr(),
            GENERIC_WRITE | DELETE,
            win32::SHARE_READ_WRITE_DELETE,
            &mut attributes,
            CREATE_NEW,
            FILE_ATTRIBUTE_NORMAL,
            std::ptr::null_mut(),
        )
    };
    let creation_error = (handle == INVALID_HANDLE_VALUE).then(std::io::Error::last_os_error);
    unsafe {
        LocalFree(descriptor);
    }
    if let Some(error) = creation_error {
        return Err(error);
    }
    Ok(unsafe { File::from_raw_handle(handle) })
}

#[cfg(windows)]
fn open_existing_private_file_windows(path: &Path) -> std::io::Result<File> {
    use std::os::windows::io::FromRawHandle;

    const GENERIC_READ: u32 = 0x8000_0000;
    const DELETE: u32 = 0x0001_0000;
    const OPEN_EXISTING: u32 = 3;
    const FILE_ATTRIBUTE_NORMAL: u32 = 0x0000_0080;

    let wide_path = windows_wide_path(path)?;
    let handle = unsafe {
        CreateFileW(
            wide_path.as_ptr(),
            GENERIC_READ | DELETE,
            win32::SHARE_READ_WRITE_DELETE,
            std::ptr::null_mut(),
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL | win32::FILE_FLAG_OPEN_REPARSE_POINT,
            std::ptr::null_mut(),
        )
    };
    if handle == INVALID_HANDLE_VALUE {
        return Err(std::io::Error::last_os_error());
    }
    Ok(unsafe { File::from_raw_handle(handle) })
}

#[cfg(windows)]
fn open_windows_directory(path: &Path) -> Result<(File, (u32, u64)), String> {
    use std::os::windows::io::FromRawHandle;

    const FILE_READ_ATTRIBUTES: u32 = 0x0000_0080;
    const GENERIC_READ: u32 = 0x8000_0000;
    const OPEN_EXISTING: u32 = 3;
    const FILE_FLAG_BACKUP_SEMANTICS: u32 = 0x0200_0000;
    const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
    const FILE_ATTRIBUTE_DIRECTORY: u32 = 0x0000_0010;

    let wide = windows_wide_path(path).map_err(|error| {
        format!(
            "failed to make private file parent path absolute {}: {error}",
            path.display()
        )
    })?;
    let handle = unsafe {
        CreateFileW(
            wide.as_ptr(),
            GENERIC_READ | FILE_READ_ATTRIBUTES,
            0x0000_0001 | 0x0000_0002,
            std::ptr::null_mut(),
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            std::ptr::null_mut(),
        )
    };
    if handle == INVALID_HANDLE_VALUE {
        return Err(format!(
            "failed to pin private file parent {}: {}",
            path.display(),
            std::io::Error::last_os_error()
        ));
    }
    let file = unsafe { File::from_raw_handle(handle) };
    let mut information = ByHandleFileInformation::default();
    let result = unsafe { GetFileInformationByHandle(handle, &mut information) };
    if result == 0
        || information.file_attributes & FILE_ATTRIBUTE_DIRECTORY == 0
        || information.file_attributes & win32::FILE_ATTRIBUTE_REPARSE_POINT != 0
    {
        return Err(format!(
            "private file parent {} is not a stable non-reparse directory",
            path.display()
        ));
    }
    let index =
        (u64::from(information.file_index_high) << 32) | u64::from(information.file_index_low);
    Ok((file, (information.volume_serial_number, index)))
}

#[cfg(windows)]
fn windows_wide_path(path: &Path) -> std::io::Result<Vec<u16>> {
    use std::os::windows::ffi::OsStrExt;

    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()?.join(path)
    };
    let mut raw = absolute
        .as_os_str()
        .encode_wide()
        .map(|unit| if unit == u16::from(b'/') { u16::from(b'\\') } else { unit })
        .collect::<Vec<_>>();
    const VERBATIM_PREFIX: [u16; 4] = [
        u16::from_le_bytes(*b"\\\0"),
        u16::from_le_bytes(*b"\\\0"),
        u16::from_le_bytes(*b"?\0"),
        u16::from_le_bytes(*b"\\\0"),
    ];
    const DEVICE_PREFIX: [u16; 4] = [
        u16::from_le_bytes(*b"\\\0"),
        u16::from_le_bytes(*b"\\\0"),
        u16::from_le_bytes(*b".\0"),
        u16::from_le_bytes(*b"\\\0"),
    ];
    const UNC_PREFIX: [u16; 2] = [
        u16::from_le_bytes(*b"\\\0"),
        u16::from_le_bytes(*b"\\\0"),
    ];

    let mut wide = if raw.starts_with(&VERBATIM_PREFIX) || raw.starts_with(&DEVICE_PREFIX) {
        raw
    } else if raw.starts_with(&UNC_PREFIX) {
        let mut wide = "\\\\?\\UNC\\".encode_utf16().collect::<Vec<_>>();
        wide.extend_from_slice(&raw[UNC_PREFIX.len()..]);
        wide
    } else {
        let mut wide = VERBATIM_PREFIX.to_vec();
        wide.append(&mut raw);
        wide
    };
    wide.push(0);
    Ok(wide)
}

pub(crate) fn validate_confined_path(
    path: &Path,
    boundary: &Path,
    allow_missing: bool,
) -> Result<(), String> {
    let boundary = fs::canonicalize(boundary).map_err(|error| {
        format!(
            "failed to resolve trusted path boundary {}: {error}",
            boundary.display()
        )
    })?;
    let parent = path
        .parent()
        .ok_or_else(|| "confined path has no parent".to_string())?;
    let parent = fs::canonicalize(parent).map_err(|error| {
        format!(
            "failed to resolve confined path parent {}: {error}",
            parent.display()
        )
    })?;
    if !parent.starts_with(&boundary) {
        return Err(format!(
            "confined path parent {} escapes trusted boundary {}",
            parent.display(),
            boundary.display()
        ));
    }
    validate_no_reparse_components(&parent, &boundary)?;
    match fs::symlink_metadata(path) {
        Ok(metadata) => validate_regular_single_link(&metadata, path),
        Err(error) if allow_missing && error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!(
            "failed to inspect confined path {}: {error}",
            path.display()
        )),
    }
}

pub(crate) fn read_single_link_text(
    path: &Path,
    max_bytes: u64,
    label: &str,
) -> Result<String, String> {
    let mut file = open_read_no_follow(path)
        .map_err(|error| format!("failed to open {label} without following links: {error}"))?;
    let metadata = file
        .metadata()
        .map_err(|error| format!("failed to inspect {label}: {error}"))?;
    validate_regular_single_link(&metadata, path)?;
    let opened_identity = lock_file_identity(&file)
        .map_err(|_| format!("{label} is not a stable regular single-link file"))?;
    if metadata.len() > max_bytes {
        return Err(format!(
            "{label} exceeds the size limit of {max_bytes} bytes"
        ));
    }
    let mut bytes = Vec::new();
    std::io::Read::by_ref(&mut file)
        .take(max_bytes.saturating_add(1))
        .read_to_end(&mut bytes)
        .map_err(|error| format!("failed to read {label}: {error}"))?;
    if bytes.len() as u64 > max_bytes {
        return Err(format!(
            "{label} exceeds the size limit of {max_bytes} bytes"
        ));
    }
    let reopened = open_read_no_follow(path)
        .map_err(|_| format!("{label} path identity changed while it was read"))?;
    let current_identity = lock_file_identity(&reopened)
        .map_err(|_| format!("{label} path identity changed while it was read"))?;
    if opened_identity != current_identity {
        return Err(format!("{label} path identity changed while it was read"));
    }
    String::from_utf8(bytes).map_err(|_| format!("{label} is not valid UTF-8"))
}

#[cfg(unix)]
fn open_read_no_follow(path: &Path) -> std::io::Result<File> {
    use std::os::unix::fs::OpenOptionsExt;
    #[cfg(test)]
    invoke_test_pre_open_hook(path);
    let mut options = OpenOptions::new();
    options.read(true).custom_flags(LOCK_NOFOLLOW);
    options.open(path)
}

#[cfg(windows)]
fn open_read_no_follow(path: &Path) -> std::io::Result<File> {
    use std::os::windows::fs::OpenOptionsExt;
    #[cfg(test)]
    invoke_test_pre_open_hook(path);
    let mut options = OpenOptions::new();
    options
        .read(true)
        .share_mode(win32::SHARE_READ_WRITE_DELETE)
        .custom_flags(win32::FILE_FLAG_OPEN_REPARSE_POINT);
    options.open(path)
}

fn validate_no_reparse_components(parent: &Path, boundary: &Path) -> Result<(), String> {
    let relative = parent.strip_prefix(boundary).map_err(|_| {
        format!(
            "confined parent {} is outside {}",
            parent.display(),
            boundary.display()
        )
    })?;
    let mut current = boundary.to_path_buf();
    validate_not_reparse(
        &fs::symlink_metadata(&current).map_err(|error| {
            format!(
                "failed to inspect path component {}: {error}",
                current.display()
            )
        })?,
        &current,
    )?;
    for component in relative.components() {
        current.push(component);
        let metadata = fs::symlink_metadata(&current).map_err(|error| {
            format!(
                "failed to inspect path component {}: {error}",
                current.display()
            )
        })?;
        validate_not_reparse(&metadata, &current)?;
    }
    Ok(())
}

fn validate_regular_single_link(metadata: &fs::Metadata, path: &Path) -> Result<(), String> {
    validate_not_reparse(metadata, path)?;
    if !metadata.is_file() || metadata_link_count(metadata, path) != 1 {
        return Err(format!(
            "confined path {} is not a regular single-link file",
            path.display()
        ));
    }
    Ok(())
}

fn validate_not_reparse(metadata: &fs::Metadata, path: &Path) -> Result<(), String> {
    if metadata.file_type().is_symlink() || metadata_is_reparse(metadata) {
        return Err(format!(
            "confined path {} is a symlink or reparse point",
            path.display()
        ));
    }
    Ok(())
}

#[cfg(unix)]
fn metadata_link_count(metadata: &fs::Metadata, _path: &Path) -> u64 {
    use std::os::unix::fs::MetadataExt;
    metadata.nlink()
}

#[cfg(windows)]
fn metadata_link_count(_metadata: &fs::Metadata, path: &Path) -> u64 {
    use std::os::windows::fs::OpenOptionsExt;
    use std::os::windows::io::AsRawHandle;
    let mut options = OpenOptions::new();
    options
        .read(true)
        .share_mode(win32::SHARE_READ_WRITE_DELETE)
        .custom_flags(win32::FILE_FLAG_OPEN_REPARSE_POINT);
    let Ok(file) = options.open(path) else {
        return 0;
    };
    let mut information = ByHandleFileInformation::default();
    let result = unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut information) };
    if result == 0 || information.file_attributes & win32::FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return 0;
    }
    u64::from(information.number_of_links)
}

#[cfg(unix)]
fn metadata_is_reparse(_metadata: &fs::Metadata) -> bool {
    false
}

#[cfg(windows)]
fn metadata_is_reparse(metadata: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;
    metadata.file_attributes() & win32::FILE_ATTRIBUTE_REPARSE_POINT != 0
}

#[cfg(all(test, windows))]
pub(crate) fn security_descriptor_sddl(path: &Path) -> Result<String, String> {
    use std::os::windows::ffi::OsStrExt;
    let wide = path
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect::<Vec<_>>();
    let mut descriptor = std::ptr::null_mut();
    let status = unsafe {
        GetNamedSecurityInfoW(
            wide.as_ptr(),
            1,
            0x0000_0005,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut descriptor,
        )
    };
    if status != 0 || descriptor.is_null() {
        return Err(format!(
            "failed to read Windows security descriptor: {status}"
        ));
    }
    let mut text = std::ptr::null_mut();
    let mut length = 0_u32;
    let converted = unsafe {
        ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor,
            1,
            0x0000_0005,
            &mut text,
            &mut length,
        )
    };
    if converted == 0 || text.is_null() {
        unsafe {
            LocalFree(descriptor);
        }
        return Err("failed to convert Windows security descriptor to SDDL".to_string());
    }
    let sddl = String::from_utf16_lossy(unsafe {
        std::slice::from_raw_parts(text, usize::try_from(length).unwrap_or_default())
    });
    unsafe {
        LocalFree(text.cast());
        LocalFree(descriptor);
    }
    Ok(sddl)
}

fn unique_temp_path(path: &Path) -> PathBuf {
    path.with_file_name(format!(
        ".{}.{}.{}.{}.tmp-codexhub",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("file"),
        std::process::id(),
        timestamp_millis(),
        NEXT_TEMP_NONCE.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    ))
}

#[cfg(all(
    target_os = "linux",
    any(target_arch = "x86_64", target_arch = "aarch64")
))]
fn retained_evidence_path(path: &Path) -> PathBuf {
    path.with_file_name(format!(
        ".{}.retained-codexhub-evidence",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("rollback-evidence")
    ))
}

#[cfg(windows)]
fn finish_private_evidence_windows<Expected>(
    parent: &PinnedPrivateParent,
    evidence: &Path,
    max_bytes: u64,
    label: &str,
    expected: &Expected,
) -> Result<(), String>
where
    Expected: Fn(&str) -> bool,
{
    if !evidence.try_exists().map_err(|error| {
        format!(
            "failed to inspect completed {label} rollback evidence {}: {error}",
            evidence.display()
        )
    })? {
        return Ok(());
    }
    let mut displaced = parent.open_existing(evidence).map_err(|error| {
        format!("failed to open completed {label} rollback evidence: {error}")
    })?;
    let displaced_text =
        read_opened_single_link_text(&mut displaced, evidence, max_bytes, label)?;
    if !expected(&displaced_text) {
        return Err(format!(
            "completed {label} rollback has mismatched evidence at {}; recovery remains fail-closed",
            evidence.display()
        ));
    }
    delete_opened_file_windows(&mut displaced, label)
}

#[cfg(all(
    target_os = "linux",
    any(target_arch = "x86_64", target_arch = "aarch64")
))]
fn finish_private_evidence_linux<Expected>(
    parent: &PinnedPrivateParent,
    evidence: &Path,
    max_bytes: u64,
    label: &str,
    expected: &Expected,
) -> Result<(), String>
where
    Expected: Fn(&str) -> bool,
{
    let mut displaced = match parent.open_existing(evidence) {
        Ok(displaced) => displaced,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let retained = retained_evidence_path(evidence);
            let mut retained_file = match parent.open_existing(&retained) {
                Ok(retained_file) => retained_file,
                Err(retained_error)
                    if retained_error.kind() == std::io::ErrorKind::NotFound =>
                {
                    return Ok(());
                }
                Err(retained_error) => {
                    return Err(format!(
                        "failed to open retained {label} rollback evidence: {retained_error}"
                    ));
                }
            };
            let retained_text =
                read_opened_single_link_text(&mut retained_file, &retained, max_bytes, label)?;
            if !expected(&retained_text) {
                return Err(format!(
                    "retained {label} rollback evidence mismatched at {}; recovery remains fail-closed",
                    retained.display()
                ));
            }
            return Ok(());
        }
        Err(error) => {
            return Err(format!(
                "failed to open completed {label} rollback evidence: {error}"
            ));
        }
    };
    let displaced_text =
        read_opened_single_link_text(&mut displaced, evidence, max_bytes, label)?;
    if !expected(&displaced_text) {
        return Err(format!(
            "completed {label} rollback has mismatched evidence at {}; recovery remains fail-closed",
            evidence.display()
        ));
    }
    remove_opened_file_linux(parent, &displaced, evidence, label)
}

#[cfg(all(
    target_os = "linux",
    any(target_arch = "x86_64", target_arch = "aarch64")
))]
fn remove_opened_file_linux(
    parent: &PinnedPrivateParent,
    opened: &File,
    path: &Path,
    label: &str,
) -> Result<(), String> {
    let opened_identity = lock_file_identity(opened)
        .map_err(|_| format!("verified {label} evidence is not a stable file"))?;
    let current = parent.open_existing(path).map_err(|error| {
        format!(
            "failed to re-open verified {label} evidence before cleanup: {error}"
        )
    })?;
    let current_identity = lock_file_identity(&current)
        .map_err(|_| format!("current {label} evidence is not a stable file"))?;
    if current_identity != opened_identity {
        return Err(format!(
            "{label} evidence changed before cleanup; replacement evidence was preserved"
        ));
    }
    drop(current);
    #[cfg(test)]
    invoke_test_pre_private_evidence_isolate_hook(path);
    let retained = retained_evidence_path(path);
    parent
        .rename_path_noreplace(path, &retained)
        .map_err(|error| {
            format!(
                "failed to isolate verified {label} evidence without deleting pathname bytes: {error}"
            )
        })?;
    match parent.open_existing(path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Ok(_) => {
            return Err(format!(
                "{label} evidence was replaced during isolation; replacement bytes remain at {}",
                path.display()
            ));
        }
        Err(error) => {
            return Err(format!(
                "failed to verify {label} evidence absence after isolation: {error}"
            ));
        }
    }
    let isolated = parent.open_existing(&retained).map_err(|error| {
        format!(
            "failed to open isolated {label} evidence at {}: {error}",
            retained.display()
        )
    })?;
    let isolated_identity = lock_file_identity(&isolated)
        .map_err(|_| format!("isolated {label} evidence is not a stable file"))?;
    if isolated_identity != opened_identity {
        return Err(format!(
            "{label} evidence changed before atomic isolation; replacement evidence was preserved at {}",
            retained.display()
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn delete_opened_file_windows(file: &mut File, label: &str) -> Result<(), String> {
    use std::os::windows::io::AsRawHandle;

    let disposition = FileDispositionInformation { delete_file: 1 };
    let mut io_status = IoStatusBlock::default();
    let status = unsafe {
        NtSetInformationFile(
            file.as_raw_handle(),
            &mut io_status,
            std::ptr::addr_of!(disposition).cast(),
            u32::try_from(std::mem::size_of::<FileDispositionInformation>())
                .map_err(|_| "private delete disposition is too large".to_string())?,
            13,
        )
    };
    if status < 0 {
        let code = unsafe { RtlNtStatusToDosError(status) };
        return Err(format!(
            "failed to remove verified {label} recovery evidence by handle: {}",
            std::io::Error::from_raw_os_error(i32::try_from(code).unwrap_or(i32::MAX))
        ));
    }
    Ok(())
}

#[cfg(any(
    windows,
    all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    )
))]
struct TempPathCleanup<'a> {
    #[cfg(all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    ))]
    parent: &'a PinnedPrivateParent,
    #[cfg(windows)]
    _parent: std::marker::PhantomData<&'a PinnedPrivateParent>,
    opened: File,
    #[cfg(any(
        test,
        all(
            target_os = "linux",
            any(target_arch = "x86_64", target_arch = "aarch64")
        )
    ))]
    path: PathBuf,
    armed: bool,
}

#[cfg(any(
    windows,
    all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    )
))]
impl<'a> TempPathCleanup<'a> {
    fn new(
        _parent: &'a PinnedPrivateParent,
        opened: &File,
        path: PathBuf,
    ) -> Result<Self, String> {
        let opened = opened.try_clone().map_err(|error| {
            format!(
                "failed to retain the private temp handle for safe cleanup {}: {error}",
                path.display()
            )
        })?;
        Ok(Self {
            #[cfg(all(
                target_os = "linux",
                any(target_arch = "x86_64", target_arch = "aarch64")
            ))]
            parent: _parent,
            #[cfg(windows)]
            _parent: std::marker::PhantomData,
            opened,
            #[cfg(any(
                test,
                all(
                    target_os = "linux",
                    any(target_arch = "x86_64", target_arch = "aarch64")
                )
            ))]
            path,
            armed: true,
        })
    }

    fn disarm(&mut self) {
        self.armed = false;
    }
}

#[cfg(any(
    windows,
    all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    )
))]
impl Drop for TempPathCleanup<'_> {
    fn drop(&mut self) {
        if !self.armed {
            return;
        }
        #[cfg(test)]
        invoke_test_pre_private_temp_cleanup_hook(&self.path);
        #[cfg(windows)]
        {
            let _ = delete_opened_file_windows(&mut self.opened, "private temporary file");
        }
        #[cfg(all(
            target_os = "linux",
            any(target_arch = "x86_64", target_arch = "aarch64")
        ))]
        {
            let retained = retained_evidence_path(&self.path);
            if self
                .parent
                .rename_path_noreplace(&self.path, &retained)
                .is_err()
            {
                return;
            }
            let Ok(current) = self.parent.open_existing(&retained) else {
                return;
            };
            let Ok(opened_identity) = lock_file_identity(&self.opened) else {
                return;
            };
            let Ok(current_identity) = lock_file_identity(&current) else {
                return;
            };
            if current_identity == opened_identity {
                self.armed = false;
            }
            // Linux has no handle-bound unlink. Retain whichever inode was
            // atomically isolated under a unique name instead of risking
            // deletion of bytes that replaced the original temp pathname.
        }
    }
}

fn lock_path(path: &Path) -> PathBuf {
    path.with_file_name(format!(
        "{}.lock",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("file")
    ))
}

fn timestamp_millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or_default()
}

#[cfg(target_os = "linux")]
const LOCK_NOFOLLOW: i32 = 0x20000;
#[cfg(any(
    target_os = "macos",
    target_os = "ios",
    target_os = "freebsd",
    target_os = "openbsd",
    target_os = "netbsd"
))]
const LOCK_NOFOLLOW: i32 = 0x100;
#[cfg(all(
    unix,
    not(any(
        target_os = "linux",
        target_os = "android",
        target_os = "macos",
        target_os = "ios",
        target_os = "freebsd",
        target_os = "openbsd",
        target_os = "netbsd"
    ))
))]
const LOCK_NOFOLLOW: i32 = 0x100;

fn open_lock_file(path: &Path, create_new: bool) -> std::io::Result<File> {
    let mut options = OpenOptions::new();
    options.read(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(LOCK_NOFOLLOW);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options
            .share_mode(win32::SHARE_READ_WRITE_DELETE)
            .custom_flags(win32::FILE_FLAG_OPEN_REPARSE_POINT);
    }
    if create_new {
        options.create_new(true).open(path)
    } else {
        options.open(path)
    }
}

fn namespace_lock_path(primary: &Path) -> PathBuf {
    primary.with_file_name(format!(
        "{}.guard",
        primary
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("file")
    ))
}

fn acquire_namespace_guard(
    path: &Path,
    started: &Instant,
    hook: Option<&dyn Fn(&'static str)>,
) -> Result<File, String> {
    loop {
        let file = match open_lock_file(path, true) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                let metadata = match fs::symlink_metadata(path) {
                    Ok(metadata) => metadata,
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                        retry_lock(started)?;
                        continue;
                    }
                    Err(_) => return Err("failed to open atomic write lock".to_owned()),
                };
                validate_lock_metadata(&metadata)?;
                let pre_open_identity = lock_path_identity(path, &metadata)?;
                #[cfg(test)]
                invoke_test_pre_open_hook(path);
                match open_lock_file(path, false) {
                    Ok(file) => {
                        let opened_identity = lock_file_identity(&file)?;
                        if opened_identity != pre_open_identity {
                            return Err("atomic write lock path changed".to_owned());
                        }
                        file
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                        retry_lock(started)?;
                        continue;
                    }
                    Err(_) => return Err("failed to open atomic write lock".to_owned()),
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                retry_lock(started)?;
                continue;
            }
            Err(_) => return Err("failed to open atomic write lock".to_owned()),
        };
        let metadata = file
            .metadata()
            .map_err(|_| "failed to open atomic write lock".to_owned())?;
        validate_lock_metadata(&metadata)?;
        validate_lock_handle(&file)?;
        if let Some(hook) = hook {
            hook("attempt");
        }
        match try_lock_exclusive(&file) {
            Ok(true) => {
                if let Err(error) = verify_lock_identity(path, &file) {
                    let _ = unlock(&file);
                    return Err(error);
                }
                if let Some(hook) = hook {
                    hook("acquired");
                }
                #[cfg(test)]
                {
                    if let Some(hook) = TEST_LOCK_ACQUIRE_HOOK.lock().unwrap().as_ref() {
                        hook(path, "acquired");
                    }
                }
                return Ok(file);
            }
            Ok(false) => {
                if let Some(hook) = hook {
                    hook("blocked");
                }
                #[cfg(test)]
                {
                    if let Some(hook) = TEST_LOCK_ACQUIRE_HOOK.lock().unwrap().as_ref() {
                        hook(path, "blocked");
                    }
                }
            }
            Err(()) => return Err("failed to acquire atomic write lock".to_owned()),
        }
        drop(file);
        retry_lock(started)?;
    }
}

///
/// Python uses fcntl.flock/LockFileEx for exactly the same one-byte lock.  We
/// never delete this file: process death releases the held lock, and the next
/// owner overwrites the versioned metadata while holding it.
pub(crate) struct FileLock {
    target_path: PathBuf,
    namespace_path: PathBuf,
    namespace: File,
    file: File,
    locked: bool,
    namespace_locked: bool,
}

impl FileLock {
    pub(crate) fn acquire(target: &Path) -> Result<Self, String> {
        Self::acquire_inner(target, None)
    }

    #[cfg(test)]
    fn acquire_with_hook(target: &Path, hook: &dyn Fn(&'static str)) -> Result<Self, String> {
        Self::acquire_inner(target, Some(hook))
    }

    fn acquire_inner(target: &Path, hook: Option<&dyn Fn(&'static str)>) -> Result<Self, String> {
        let path = lock_path(target);
        let namespace_path = namespace_lock_path(&path);
        let started = Instant::now();
        let namespace = acquire_namespace_guard(&namespace_path, &started, hook)?;
        loop {
            let (mut file, created) = match open_lock_file(&path, true) {
                Ok(file) => {
                    let metadata = file
                        .metadata()
                        .map_err(|_| "failed to open atomic write lock".to_owned())?;
                    validate_lock_metadata(&metadata)?;
                    validate_lock_handle(&file)?;
                    (file, true)
                }
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                    let metadata = match fs::symlink_metadata(&path) {
                        Ok(metadata) => metadata,
                        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                            retry_lock(&started)?;
                            continue;
                        }
                        Err(_) => return Err("failed to open atomic write lock".to_owned()),
                    };
                    validate_lock_metadata(&metadata)?;
                    let pre_open_identity = lock_path_identity(&path, &metadata)?;
                    #[cfg(test)]
                    invoke_test_pre_open_hook(&path);
                    let file = match open_lock_file(&path, false) {
                        Ok(file) => file,
                        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                            retry_lock(&started)?;
                            continue;
                        }
                        Err(_) => return Err("failed to open atomic write lock".to_owned()),
                    };
                    let opened_metadata = file
                        .metadata()
                        .map_err(|_| "failed to open atomic write lock".to_owned())?;
                    validate_lock_metadata(&opened_metadata)?;
                    validate_lock_handle(&file)?;
                    if lock_file_identity(&file)? != pre_open_identity {
                        return Err("atomic write lock path changed".to_owned());
                    }
                    (file, false)
                }
                Err(_) => return Err("failed to open atomic write lock".to_owned()),
            };

            match try_lock_exclusive(&file) {
                Ok(true) => {
                    if let Err(error) = verify_lock_identity(&path, &file) {
                        let _ = unlock(&file);
                        return Err(error);
                    }
                    match prepare_lock_metadata(&mut file, created) {
                        Ok(()) => {
                            if let Err(error) = verify_lock_identity(&path, &file) {
                                let _ = unlock(&file);
                                return Err(error);
                            }
                            return Ok(Self {
                                target_path: target.to_path_buf(),
                                namespace_path: namespace_path.clone(),
                                namespace,
                                file,
                                locked: true,
                                namespace_locked: true,
                            });
                        }
                        Err(LockMetadataError::Transient) => {
                            let _ = unlock(&file);
                        }
                        Err(LockMetadataError::Unrecoverable) => {
                            let _ = unlock(&file);
                            return Err("atomic write lock is unavailable".to_owned());
                        }
                        Err(LockMetadataError::Io) => {
                            let _ = unlock(&file);
                            return Err("failed to prepare atomic write lock".to_owned());
                        }
                    }
                }
                Ok(false) => {}
                Err(()) => return Err("failed to acquire atomic write lock".to_owned()),
            }

            retry_lock(&started)?;
        }
    }

    fn target_path(&self) -> &Path {
        &self.target_path
    }

    fn verify_namespace_identity(&self) -> Result<(), String> {
        verify_lock_identity(&self.namespace_path, &self.namespace)
    }

    fn release(&mut self) -> Result<(), String> {
        let mut first_error = None;
        if self.locked {
            if unlock(&self.file).is_err() {
                first_error = Some("failed to release atomic write lock".to_owned());
            } else {
                self.locked = false;
            }
        }
        if self.namespace_locked {
            if unlock(&self.namespace).is_err() && first_error.is_none() {
                first_error = Some("failed to release atomic write namespace".to_owned());
            }
            self.namespace_locked = false;
        }
        first_error.map_or(Ok(()), Err)
    }
}

fn retry_lock(started: &Instant) -> Result<(), String> {
    let elapsed = started.elapsed();
    if elapsed >= LOCK_WAIT_TIMEOUT {
        return Err("timed out waiting for atomic write lock".to_owned());
    }
    thread::sleep(LOCK_RETRY_DELAY.min(LOCK_WAIT_TIMEOUT - elapsed));
    Ok(())
}

impl Drop for FileLock {
    fn drop(&mut self) {
        let _ = self.release();
    }
}

enum LockMetadataError {
    Transient,
    Unrecoverable,
    Io,
}

fn validate_lock_metadata(metadata: &fs::Metadata) -> Result<(), String> {
    if !metadata.file_type().is_file() {
        return Err("atomic write lock is not a regular single-link file".to_owned());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if metadata.nlink() != 1 {
            return Err("atomic write lock is not a regular single-link file".to_owned());
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if metadata.file_attributes() & win32::FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err("atomic write lock is not a regular single-link file".to_owned());
        }
    }
    Ok(())
}

#[cfg(not(windows))]
fn validate_lock_handle(_file: &File) -> Result<(), String> {
    Ok(())
}

#[cfg(unix)]
fn lock_file_identity(file: &File) -> Result<(u64, u64), String> {
    use std::os::unix::fs::MetadataExt;
    let metadata = file
        .metadata()
        .map_err(|_| "atomic write lock path changed".to_owned())?;
    validate_lock_metadata(&metadata)?;
    Ok((metadata.dev(), metadata.ino()))
}

#[cfg(unix)]
fn lock_path_identity(_path: &Path, metadata: &fs::Metadata) -> Result<(u64, u64), String> {
    use std::os::unix::fs::MetadataExt;
    Ok((metadata.dev(), metadata.ino()))
}

#[cfg(windows)]
fn lock_path_identity(path: &Path, _metadata: &fs::Metadata) -> Result<(u32, u64), String> {
    let file =
        open_lock_file(path, false).map_err(|_| "atomic write lock path changed".to_owned())?;
    lock_file_identity(&file)
}

#[cfg(windows)]
fn lock_file_identity(file: &File) -> Result<(u32, u64), String> {
    use std::os::windows::io::AsRawHandle;
    let mut information = ByHandleFileInformation::default();
    let result = unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut information) };
    if result == 0
        || information.number_of_links != 1
        || information.file_attributes & win32::FILE_ATTRIBUTE_REPARSE_POINT != 0
    {
        return Err("atomic write lock is not a regular single-link file".to_owned());
    }
    let index =
        (u64::from(information.file_index_high) << 32) | u64::from(information.file_index_low);
    Ok((information.volume_serial_number, index))
}

#[cfg(windows)]
fn validate_lock_handle(file: &File) -> Result<(), String> {
    lock_file_identity(file).map(|_| ())
}

#[cfg(unix)]
fn verify_lock_identity(path: &Path, file: &File) -> Result<(), String> {
    use std::os::unix::fs::MetadataExt;
    let path_metadata =
        fs::symlink_metadata(path).map_err(|_| "atomic write lock path changed".to_owned())?;
    validate_lock_metadata(&path_metadata)?;
    let file_metadata = file
        .metadata()
        .map_err(|_| "atomic write lock path changed".to_owned())?;
    validate_lock_metadata(&file_metadata)?;
    if path_metadata.dev() != file_metadata.dev() || path_metadata.ino() != file_metadata.ino() {
        return Err("atomic write lock path changed".to_owned());
    }
    Ok(())
}

#[cfg(windows)]
fn verify_lock_identity(path: &Path, file: &File) -> Result<(), String> {
    let path_metadata =
        fs::symlink_metadata(path).map_err(|_| "atomic write lock path changed".to_owned())?;
    validate_lock_metadata(&path_metadata)?;
    let path_file =
        open_lock_file(path, false).map_err(|_| "atomic write lock path changed".to_owned())?;
    let path_identity =
        lock_file_identity(&path_file).map_err(|_| "atomic write lock path changed".to_owned())?;
    let file_identity =
        lock_file_identity(file).map_err(|_| "atomic write lock path changed".to_owned())?;
    if path_identity != file_identity {
        return Err("atomic write lock path changed".to_owned());
    }
    Ok(())
}

fn prepare_lock_metadata(file: &mut File, created: bool) -> Result<(), LockMetadataError> {
    let mut text = String::new();
    file.seek(SeekFrom::Start(0))
        .map_err(|_| LockMetadataError::Io)?;
    match file.read_to_string(&mut text) {
        Ok(_) => {}
        Err(error) if error.kind() == std::io::ErrorKind::InvalidData => {
            return Err(LockMetadataError::Unrecoverable)
        }
        Err(_) => return Err(LockMetadataError::Io),
    }
    let recoverable = match lock_state(&text) {
        LockState::Protocol | LockState::DeadLegacy => true,
        LockState::Empty if created => true,
        LockState::Empty => return Err(LockMetadataError::Transient),
        LockState::LiveLegacy | LockState::Unknown => false,
    };
    if !recoverable {
        return Err(LockMetadataError::Unrecoverable);
    }
    file.set_len(0).map_err(|_| LockMetadataError::Io)?;
    file.seek(SeekFrom::Start(0))
        .map_err(|_| LockMetadataError::Io)?;
    file.write_all(LOCK_PROTOCOL.as_bytes())
        .map_err(|_| LockMetadataError::Io)?;
    file.sync_all().map_err(|_| LockMetadataError::Io)
}

enum LockState {
    Empty,
    Protocol,
    DeadLegacy,
    LiveLegacy,
    Unknown,
}

fn lock_state(text: &str) -> LockState {
    if text.is_empty() {
        return LockState::Empty;
    }
    if matches!(
        text,
        "codexhub-atomic-lock=1\n" | "codexhub-atomic-lock=1\r\n"
    ) {
        return LockState::Protocol;
    }
    match parse_legacy_pid(text) {
        Some(pid) if pid_is_definitely_dead(pid) => LockState::DeadLegacy,
        Some(_) => LockState::LiveLegacy,
        None => LockState::Unknown,
    }
}

/// Parse only the exact legacy record. Timestamp-only metadata is unsafe: its
/// wall-clock age cannot prove that a former owner has stopped writing.
fn parse_legacy_pid(text: &str) -> Option<i64> {
    let crlf_body = text.strip_suffix("\r\n");
    let (body, separator) = match crlf_body {
        Some(body) => (body, "\r\n"),
        None => (text.strip_suffix('\n')?, "\n"),
    };
    let lines: Vec<&str> = body.split(separator).collect();
    if lines.iter().any(|line| line.contains('\r')) || lines.len() != 2 {
        return None;
    }

    let mut pid = None;
    let mut timestamp = None;
    for line in lines {
        let (key, value) = line.split_once('=')?;
        match key {
            "pid" if pid.is_none() => pid = parse_legacy_pid_value(value),
            "acquired_at_millis" if timestamp.is_none() => timestamp = parse_decimal_u128(value),
            _ => return None,
        }
    }
    pid.zip(timestamp).map(|(pid, _)| pid)
}

fn parse_legacy_pid_value(value: &str) -> Option<i64> {
    let parsed = parse_decimal_u128(value)?;
    (1..=i32::MAX as u128)
        .contains(&parsed)
        .then_some(parsed as i64)
}

fn parse_decimal_u128(value: &str) -> Option<u128> {
    if value.is_empty() || !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    value.parse::<u128>().ok()
}

#[cfg(unix)]
fn pid_is_definitely_dead(pid: i64) -> bool {
    if pid <= 0 {
        return true;
    }
    // kill(pid, 0) cannot signal the process. EPERM means it exists but is not
    // ours, so it is deliberately not reclaimed.
    let result = unsafe { kill(pid as i32, 0) };
    result != 0 && std::io::Error::last_os_error().raw_os_error() == Some(flock_op::ESRCH)
}

#[cfg(windows)]
fn pid_is_definitely_dead(pid: i64) -> bool {
    if pid <= 0 || pid > u32::MAX as i64 {
        return true;
    }
    unsafe {
        let handle = OpenProcess(win32::PROCESS_QUERY_LIMITED_INFORMATION, 0, pid as u32);
        if handle.is_null() {
            return false; // Access-denied and unknown are fail-safe.
        }
        let mut code = 0;
        let result = GetExitCodeProcess(handle, &mut code);
        CloseHandle(handle);
        result != 0 && code != win32::STILL_ACTIVE
    }
}

#[cfg(unix)]
fn try_lock_exclusive(file: &File) -> Result<bool, ()> {
    use std::os::fd::AsRawFd;
    let result = unsafe { flock(file.as_raw_fd(), flock_op::LOCK_EX | flock_op::LOCK_NB) };
    if result == 0 {
        Ok(true)
    } else if matches!(
        std::io::Error::last_os_error().kind(),
        std::io::ErrorKind::WouldBlock
    ) {
        Ok(false)
    } else {
        Err(())
    }
}

#[cfg(unix)]
fn unlock(file: &File) -> std::io::Result<()> {
    use std::os::fd::AsRawFd;
    if unsafe { flock(file.as_raw_fd(), flock_op::LOCK_UN) } == 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

#[cfg(windows)]
fn try_lock_exclusive(file: &File) -> Result<bool, ()> {
    use std::os::windows::io::AsRawHandle;
    let mut overlapped = Overlapped::default();
    let result = unsafe {
        LockFileEx(
            file.as_raw_handle(),
            win32::LOCKFILE_EXCLUSIVE_LOCK | win32::LOCKFILE_FAIL_IMMEDIATELY,
            0,
            1,
            0,
            &mut overlapped,
        )
    };
    if result != 0 {
        Ok(true)
    } else if matches!(std::io::Error::last_os_error().raw_os_error(), Some(code) if code == win32::ERROR_SHARING_VIOLATION || code == win32::ERROR_LOCK_VIOLATION)
    {
        Ok(false)
    } else {
        Err(())
    }
}

#[cfg(windows)]
fn unlock(file: &File) -> std::io::Result<()> {
    use std::os::windows::io::AsRawHandle;
    let mut overlapped = Overlapped::default();
    if unsafe { UnlockFileEx(file.as_raw_handle(), 0, 1, 0, &mut overlapped) } != 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

#[cfg(unix)]
unsafe extern "C" {
    fn flock(fd: i32, operation: i32) -> i32;
    fn kill(pid: i32, signal: i32) -> i32;
}

#[cfg(unix)]
unsafe extern "C" {
    fn openat(directory_fd: i32, path: *const std::ffi::c_char, flags: i32, mode: u32) -> i32;
    fn renameat(
        old_directory_fd: i32,
        old_path: *const std::ffi::c_char,
        new_directory_fd: i32,
        new_path: *const std::ffi::c_char,
    ) -> i32;
}

#[cfg(all(
    target_os = "linux",
    any(target_arch = "x86_64", target_arch = "aarch64")
))]
unsafe extern "C" {
    fn syscall(number: isize, ...) -> isize;
}

#[cfg(windows)]
#[repr(C)]
#[derive(Default)]
struct ByHandleFileInformation {
    file_attributes: u32,
    creation_time_low: u32,
    creation_time_high: u32,
    last_access_time_low: u32,
    last_access_time_high: u32,
    last_write_time_low: u32,
    last_write_time_high: u32,
    volume_serial_number: u32,
    file_size_high: u32,
    file_size_low: u32,
    number_of_links: u32,
    file_index_high: u32,
    file_index_low: u32,
}
#[cfg(windows)]
#[repr(C)]
#[derive(Default)]
struct Overlapped {
    internal: usize,
    internal_high: usize,
    offset: u32,
    offset_high: u32,
    event: *mut std::ffi::c_void,
}

#[cfg(windows)]
#[repr(C)]
struct SecurityAttributes {
    length: u32,
    security_descriptor: *mut std::ffi::c_void,
    inherit_handle: i32,
}

#[cfg(windows)]
#[repr(C)]
struct FileRenameInformation {
    flags: u32,
    root_directory: *mut std::ffi::c_void,
    file_name_length: u32,
    file_name: [u16; 1],
}

#[cfg(windows)]
#[repr(C)]
struct FileDispositionInformation {
    delete_file: u8,
}

#[cfg(windows)]
#[repr(C)]
#[derive(Default)]
struct IoStatusBlock {
    status_or_pointer: usize,
    information: usize,
}

#[cfg(windows)]
const INVALID_HANDLE_VALUE: *mut std::ffi::c_void = -1_isize as *mut std::ffi::c_void;

#[cfg(windows)]
#[link(name = "kernel32")]
unsafe extern "system" {
    fn CreateFileW(
        file_name: *const u16,
        desired_access: u32,
        share_mode: u32,
        security_attributes: *mut SecurityAttributes,
        creation_disposition: u32,
        flags_and_attributes: u32,
        template_file: *mut std::ffi::c_void,
    ) -> *mut std::ffi::c_void;
    fn GetFileInformationByHandle(
        handle: *mut std::ffi::c_void,
        information: *mut ByHandleFileInformation,
    ) -> i32;
    fn LockFileEx(
        handle: *mut std::ffi::c_void,
        flags: u32,
        reserved: u32,
        low: u32,
        high: u32,
        overlapped: *mut Overlapped,
    ) -> i32;
    fn UnlockFileEx(
        handle: *mut std::ffi::c_void,
        reserved: u32,
        low: u32,
        high: u32,
        overlapped: *mut Overlapped,
    ) -> i32;
    fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut std::ffi::c_void;
    fn GetExitCodeProcess(handle: *mut std::ffi::c_void, code: *mut u32) -> i32;
    fn CloseHandle(handle: *mut std::ffi::c_void) -> i32;
    fn LocalFree(memory: *mut std::ffi::c_void) -> *mut std::ffi::c_void;
}

#[cfg(windows)]
#[link(name = "ntdll")]
unsafe extern "system" {
    fn NtSetInformationFile(
        file_handle: *mut std::ffi::c_void,
        io_status_block: *mut IoStatusBlock,
        file_information: *const std::ffi::c_void,
        length: u32,
        file_information_class: i32,
    ) -> i32;
    fn RtlNtStatusToDosError(status: i32) -> u32;
}

#[cfg(windows)]
#[link(name = "advapi32")]
unsafe extern "system" {
    fn ConvertStringSecurityDescriptorToSecurityDescriptorW(
        string_security_descriptor: *const u16,
        string_sd_revision: u32,
        security_descriptor: *mut *mut std::ffi::c_void,
        security_descriptor_size: *mut u32,
    ) -> i32;
}

#[cfg(all(test, windows))]
#[link(name = "advapi32")]
unsafe extern "system" {
    fn GetNamedSecurityInfoW(
        object_name: *const u16,
        object_type: u32,
        security_information: u32,
        owner: *mut *mut std::ffi::c_void,
        group: *mut *mut std::ffi::c_void,
        dacl: *mut *mut std::ffi::c_void,
        sacl: *mut *mut std::ffi::c_void,
        security_descriptor: *mut *mut std::ffi::c_void,
    ) -> u32;
    fn ConvertSecurityDescriptorToStringSecurityDescriptorW(
        security_descriptor: *mut std::ffi::c_void,
        revision: u32,
        security_information: u32,
        string_security_descriptor: *mut *mut u16,
        string_security_descriptor_length: *mut u32,
    ) -> i32;
}

#[cfg(test)]
mod tests {
    use super::{
        clear_test_pre_open_hook, clear_test_pre_private_publish_hook,
        clear_test_pre_private_quarantine_hook, install_test_pre_open_hook,
        install_test_pre_private_publish_hook, install_test_pre_private_quarantine_hook,
        lock_state, parse_legacy_pid, quarantine_private_text, read_single_link_text,
        write_private_text_atomic, write_text_atomic, write_text_locked, FileLock, LockState,
        LOCK_PROTOCOL,
    };
    #[cfg(target_os = "linux")]
    use super::{
        clear_test_pre_private_quarantine_rename_hook, clear_test_private_quarantine_fault,
        install_test_pre_private_quarantine_rename_hook, install_test_private_quarantine_fault,
    };
    #[cfg(all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    ))]
    use super::{
        clear_test_pre_private_evidence_isolate_hook,
        install_test_pre_private_evidence_isolate_hook,
    };
    #[cfg(any(
        windows,
        all(
            target_os = "linux",
            any(target_arch = "x86_64", target_arch = "aarch64")
        )
    ))]
    use super::{
        clear_test_pre_private_temp_cleanup_hook, install_test_pre_private_temp_cleanup_hook,
        remove_private_text_if_unchanged, replace_private_text_if_unchanged,
        PrivateTextReplacement,
    };
    use std::{
        fs,
        io::{BufRead, Read, Write},
        path::{Path, PathBuf},
        process::{Command, Stdio},
        sync::mpsc,
        thread,
        time::{Duration, SystemTime},
    };

    #[cfg(any(
        windows,
        all(
            target_os = "linux",
            any(target_arch = "x86_64", target_arch = "aarch64")
        )
    ))]
    fn assert_conditional_private_rollback_primitives() {
        let root = test_root("conditional-private-rollback-direct");
        fs::create_dir_all(&root).unwrap();
        let existing = root.join("existing.json");
        let existing_evidence = root.join("existing.rollback-evidence");
        fs::write(&existing, "journaled-candidate").unwrap();

        replace_private_text_if_unchanged(
            PrivateTextReplacement {
                path: &existing,
                evidence: &existing_evidence,
                boundary: &root,
                contents: "base-snapshot",
                max_bytes: 1024,
                label: "direct existing rollback",
            },
            |current| current == "journaled-candidate",
            || {},
        )
        .unwrap();

        assert_eq!(fs::read_to_string(&existing).unwrap(), "base-snapshot");
        assert!(!existing_evidence.exists());

        let absent = root.join("absent.json");
        let absent_evidence = root.join("absent.rollback-evidence");
        fs::write(&absent, "journaled-candidate").unwrap();
        remove_private_text_if_unchanged(
            &absent,
            &absent_evidence,
            &root,
            1024,
            "direct absent rollback",
            |current| current == "journaled-candidate",
            || {},
        )
        .unwrap();

        assert!(!absent.exists());
        assert!(!absent_evidence.exists());
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn windows_conditional_private_rollback_primitives_restore_and_remove_exact_owner() {
        assert_conditional_private_rollback_primitives();
    }

    #[cfg(windows)]
    #[test]
    fn windows_conditional_replace_never_overwrites_evidence_created_at_commit_boundary() {
        let root = test_root("conditional-replace-evidence-race");
        fs::create_dir_all(&root).unwrap();
        let live = root.join("providers.toml");
        let evidence = root.join("providers.rollback-evidence");
        fs::write(&live, "journaled-candidate").unwrap();
        let evidence_for_hook = evidence.clone();

        let error = replace_private_text_if_unchanged(
            PrivateTextReplacement {
                path: &live,
                evidence: &evidence,
                boundary: &root,
                contents: "base-snapshot",
                max_bytes: 1024,
                label: "evidence creation race",
            },
            |current| current == "journaled-candidate",
            move || fs::write(&evidence_for_hook, "unowned-evidence").unwrap(),
        )
        .expect_err("conditional replace must never overwrite newly created evidence");

        assert!(error.contains("evidence") || error.contains("replace"));
        assert_eq!(fs::read_to_string(&live).unwrap(), "journaled-candidate");
        assert_eq!(fs::read_to_string(&evidence).unwrap(), "unowned-evidence");
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn windows_conditional_replace_resumes_after_candidate_isolated_before_publish() {
        let root = test_root("conditional-replace-isolated-prefix");
        fs::create_dir_all(&root).unwrap();
        let live = root.join("providers.toml");
        let evidence = root.join("providers.rollback-evidence");
        fs::write(&evidence, "journaled-candidate").unwrap();

        replace_private_text_if_unchanged(
            PrivateTextReplacement {
                path: &live,
                evidence: &evidence,
                boundary: &root,
                contents: "base-snapshot",
                max_bytes: 1024,
                label: "isolated candidate prefix",
            },
            |current| current == "journaled-candidate",
            || {},
        )
        .unwrap();

        assert_eq!(fs::read_to_string(&live).unwrap(), "base-snapshot");
        assert!(!evidence.exists());
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn windows_temp_cleanup_deletes_only_its_opened_inode_after_entry_replacement() {
        let root = test_root("conditional-temp-cleanup-race");
        let parent = root.join("owned");
        let moved_parent = root.join("owned-moved");
        fs::create_dir_all(&parent).unwrap();
        let live = parent.join("providers.toml");
        let evidence = parent.join("providers.rollback-evidence");
        let owned_temp = root.join("owned-temp-retained");
        fs::write(&live, "journaled-candidate").unwrap();
        let observed_temp = std::rc::Rc::new(std::cell::RefCell::new(None::<PathBuf>));
        let observed_temp_for_hook = observed_temp.clone();
        let parent_for_hook = parent.clone();
        let moved_parent_for_hook = moved_parent.clone();
        install_test_pre_private_temp_cleanup_hook(move |temp_path| {
            assert!(
                fs::rename(&parent_for_hook, &moved_parent_for_hook).is_err(),
                "the pinned Windows parent must deny a parent-path swap"
            );
            fs::rename(temp_path, &owned_temp).unwrap();
            fs::write(temp_path, "unowned-temp-replacement").unwrap();
            *observed_temp_for_hook.borrow_mut() = Some(temp_path.to_path_buf());
        });
        let evidence_for_hook = evidence.clone();

        let error = replace_private_text_if_unchanged(
            PrivateTextReplacement {
                path: &live,
                evidence: &evidence,
                boundary: &root,
                contents: "base-snapshot",
                max_bytes: 1024,
                label: "temp cleanup race",
            },
            |current| current == "journaled-candidate",
            move || fs::write(&evidence_for_hook, "unowned-evidence").unwrap(),
        )
        .expect_err("the evidence collision must leave the temp cleanup armed");
        clear_test_pre_private_temp_cleanup_hook();

        assert!(error.contains("evidence"));
        let replacement_temp = observed_temp
            .borrow()
            .clone()
            .expect("cleanup hook must observe the private temp path");
        assert_eq!(
            fs::read_to_string(replacement_temp).unwrap(),
            "unowned-temp-replacement",
            "cleanup must never delete a replacement occupying the temp pathname"
        );
        assert_eq!(fs::read_to_string(&live).unwrap(), "journaled-candidate");
        assert_eq!(fs::read_to_string(&evidence).unwrap(), "unowned-evidence");
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    ))]
    #[test]
    fn linux_conditional_private_rollback_primitives_restore_and_remove_exact_owner() {
        assert_conditional_private_rollback_primitives();
    }

    #[cfg(all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    ))]
    #[test]
    fn linux_conditional_cleanup_never_unlinks_evidence_replaced_after_identity_check() {
        let root = test_root("conditional-cleanup-evidence-race");
        fs::create_dir_all(&root).unwrap();
        let live = root.join("providers.toml");
        let evidence = root.join("providers.rollback-evidence");
        let owned_copy = root.join("journaled-candidate.preserved");
        fs::write(&live, "journaled-candidate").unwrap();
        let owned_copy_for_hook = owned_copy.clone();
        install_test_pre_private_evidence_isolate_hook(move |path| {
            fs::rename(path, &owned_copy_for_hook).unwrap();
            fs::write(path, "unowned-evidence").unwrap();
        });

        let error = remove_private_text_if_unchanged(
            &live,
            &evidence,
            &root,
            1024,
            "cleanup evidence race",
            |current| current == "journaled-candidate",
            || {},
        )
        .expect_err("cleanup must fail closed when evidence identity changes");
        clear_test_pre_private_evidence_isolate_hook();
        let retry_error = remove_private_text_if_unchanged(
            &live,
            &evidence,
            &root,
            1024,
            "cleanup evidence race retry",
            |current| current == "journaled-candidate",
            || {},
        )
        .expect_err("restart must retain the mismatch and remain fail closed");

        assert!(error.contains("changed") || error.contains("mismatch"));
        assert!(retry_error.contains("mismatch"));
        assert_eq!(
            fs::read_to_string(&owned_copy).unwrap(),
            "journaled-candidate"
        );
        assert!(
            fs::read_dir(&root)
                .unwrap()
                .filter_map(Result::ok)
                .any(|entry| fs::read_to_string(entry.path())
                    .is_ok_and(|text| text == "unowned-evidence")),
            "the replacement evidence must remain byte-for-byte in the transaction directory"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    ))]
    #[test]
    fn linux_temp_cleanup_stays_in_pinned_parent_and_preserves_replacement_entry() {
        let root = test_root("private-temp-parent-and-entry-race");
        let parent = root.join("owned");
        let moved_parent = root.join("owned-moved");
        let source = parent.join("catalog.json");
        let quarantine = parent.join("catalog.quarantine");
        fs::create_dir_all(&parent).unwrap();
        fs::write(&source, "transaction-owned-catalog").unwrap();
        let parent_for_hook = parent.clone();
        let moved_parent_for_hook = moved_parent.clone();
        let observed_replacement =
            std::rc::Rc::new(std::cell::RefCell::new(None::<PathBuf>));
        let observed_replacement_for_hook = observed_replacement.clone();
        install_test_pre_private_temp_cleanup_hook(move |temp_path| {
            let file_name = temp_path.file_name().unwrap().to_owned();
            fs::rename(&parent_for_hook, &moved_parent_for_hook).unwrap();
            fs::create_dir_all(&parent_for_hook).unwrap();
            let replacement = parent_for_hook.join(file_name);
            fs::write(&replacement, "unowned-temp-replacement").unwrap();
            *observed_replacement_for_hook.borrow_mut() = Some(replacement);
        });
        install_test_private_quarantine_fault("placeholder-publish");

        let error = quarantine_private_text(
            &source,
            &quarantine,
            &root,
            "disabled-sentinel",
            1024,
            "temp cleanup parent race",
        )
        .expect_err("the injected placeholder publication fault must arm cleanup");
        clear_test_private_quarantine_fault();
        clear_test_pre_private_temp_cleanup_hook();

        assert!(error.contains("placeholder-publish"));
        let replacement = observed_replacement
            .borrow()
            .clone()
            .expect("cleanup hook must create a replacement temp entry");
        assert_eq!(
            fs::read_to_string(replacement).unwrap(),
            "unowned-temp-replacement",
            "cleanup must not escape the pinned parent or delete replacement bytes"
        );
        assert_eq!(
            fs::read_to_string(moved_parent.join("catalog.json")).unwrap(),
            "transaction-owned-catalog"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn private_atomic_write_rejects_existing_hardlink_without_modifying_its_peer() {
        let root = test_root("private-hardlink");
        fs::create_dir_all(&root).unwrap();
        let peer = root.join("peer");
        let target = root.join("secret.backup");
        fs::write(&peer, "peer-content").unwrap();
        fs::hard_link(&peer, &target).unwrap();

        let error = write_private_text_atomic(&target, "new-secret", &root)
            .expect_err("hard-linked destination must fail closed");

        assert!(error.contains("single-link"));
        assert_eq!(fs::read_to_string(peer).unwrap(), "peer-content");
    }

    #[test]
    fn private_atomic_write_publishes_only_through_the_pinned_parent_during_a_real_rename_race() {
        let root = test_root("private-parent-race");
        let parent = root.join("owned");
        let moved_parent = root.join("owned-moved");
        let target = parent.join("secret.backup");
        fs::create_dir_all(&parent).unwrap();
        let (start_tx, start_rx) = mpsc::channel();
        let (done_tx, done_rx) = mpsc::channel();
        let parent_for_attacker = parent.clone();
        let moved_for_attacker = moved_parent.clone();
        let attacker = thread::spawn(move || {
            start_rx.recv_timeout(Duration::from_secs(10)).unwrap();
            let rename_result = fs::rename(&parent_for_attacker, &moved_for_attacker);
            #[cfg(unix)]
            if rename_result.is_ok() {
                fs::create_dir_all(&parent_for_attacker).unwrap();
                fs::write(parent_for_attacker.join("sentinel"), "replacement").unwrap();
            }
            done_tx.send(rename_result).unwrap();
        });
        install_test_pre_private_publish_hook(move |_| {
            start_tx.send(()).unwrap();
            let result = done_rx.recv_timeout(Duration::from_secs(10)).unwrap();
            #[cfg(windows)]
            assert!(
                result.is_err(),
                "the pinned Windows directory handle must deny parent rename/delete"
            );
            #[cfg(unix)]
            assert!(
                result.is_ok(),
                "the Unix race fixture must actually rename the pathname"
            );
        });

        let result = write_private_text_atomic(&target, "sensitive", &root);
        clear_test_pre_private_publish_hook();
        result.unwrap();
        attacker.join().unwrap();

        #[cfg(windows)]
        assert_eq!(fs::read_to_string(&target).unwrap(), "sensitive");
        #[cfg(unix)]
        {
            assert_eq!(
                fs::read_to_string(moved_parent.join("secret.backup")).unwrap(),
                "sensitive"
            );
            assert_eq!(
                fs::read_to_string(parent.join("sentinel")).unwrap(),
                "replacement"
            );
            assert!(!parent.join("secret.backup").exists());
        }
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn private_quarantine_renames_and_reads_back_only_through_the_pinned_parent() {
        let root = test_root("private-quarantine-parent-race");
        let parent = root.join("owned");
        let moved_parent = root.join("owned-moved");
        let victim = root.join("victim");
        let source = parent.join("catalog.json");
        let quarantine = parent.join("catalog.json.recovery.quarantine");
        fs::create_dir_all(&parent).unwrap();
        fs::create_dir_all(&victim).unwrap();
        fs::write(&source, "transaction-owned-catalog").unwrap();
        fs::write(victim.join("catalog.json"), "victim-catalog").unwrap();
        let (start_tx, start_rx) = mpsc::channel();
        let (done_tx, done_rx) = mpsc::channel();
        let parent_for_attacker = parent.clone();
        let moved_for_attacker = moved_parent.clone();
        let victim_for_attacker = victim.clone();
        let attacker = thread::spawn(move || {
            start_rx.recv_timeout(Duration::from_secs(10)).unwrap();
            let rename_result = fs::rename(&parent_for_attacker, &moved_for_attacker);
            if rename_result.is_ok() {
                #[cfg(unix)]
                std::os::unix::fs::symlink(&victim_for_attacker, &parent_for_attacker).unwrap();
                #[cfg(windows)]
                {
                    let status = Command::new("cmd")
                        .args([
                            "/C",
                            "mklink",
                            "/J",
                            &parent_for_attacker.to_string_lossy(),
                            &victim_for_attacker.to_string_lossy(),
                        ])
                        .status()
                        .unwrap();
                    assert!(status.success(), "junction swap fixture must be created");
                }
            }
            done_tx.send(rename_result).unwrap();
        });
        install_test_pre_private_quarantine_hook(move |_| {
            start_tx.send(()).unwrap();
            let result = done_rx.recv_timeout(Duration::from_secs(10)).unwrap();
            #[cfg(windows)]
            assert!(
                result.is_err(),
                "the retained Windows parent handle must deny a junction swap"
            );
            #[cfg(unix)]
            assert!(
                result.is_ok(),
                "the Unix fixture must replace the pathname after validation"
            );
        });

        let readback = quarantine_private_text(
            &source,
            &quarantine,
            &root,
            "disabled-sentinel",
            1024,
            "generated catalog quarantine",
        );
        clear_test_pre_private_quarantine_hook();
        let readback = readback.unwrap();
        attacker.join().unwrap();

        assert_eq!(readback, "transaction-owned-catalog");
        assert_eq!(
            fs::read_to_string(victim.join("catalog.json")).unwrap(),
            "victim-catalog"
        );
        assert!(!victim.join("catalog.json.recovery.quarantine").exists());
        #[cfg(windows)]
        assert_eq!(
            fs::read_to_string(&quarantine).unwrap(),
            "transaction-owned-catalog"
        );
        #[cfg(unix)]
        assert_eq!(
            fs::read_to_string(moved_parent.join("catalog.json.recovery.quarantine")).unwrap(),
            "transaction-owned-catalog"
        );
        #[cfg(target_os = "linux")]
        assert_eq!(
            fs::read_to_string(moved_parent.join("catalog.json")).unwrap(),
            "disabled-sentinel"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    ))]
    #[test]
    fn private_quarantine_installs_sentinel_and_preserves_replacement_evidence_on_inode_mismatch() {
        let root = test_root("private-quarantine-source-inode-race");
        let parent = root.join("owned");
        let source = parent.join("catalog.json");
        let displaced = parent.join("catalog.original");
        let replacement = parent.join("catalog.replacement");
        let quarantine = parent.join("catalog.quarantine");
        fs::create_dir_all(&parent).unwrap();
        fs::write(&source, "transaction-owned-catalog").unwrap();
        fs::write(&replacement, "attacker-replacement").unwrap();
        let source_for_hook = source.clone();
        let displaced_for_hook = displaced.clone();
        let replacement_for_hook = replacement.clone();
        install_test_pre_private_quarantine_rename_hook(move |_| {
            fs::rename(&source_for_hook, &displaced_for_hook).unwrap();
            fs::rename(&replacement_for_hook, &source_for_hook).unwrap();
        });

        let error = quarantine_private_text(
            &source,
            &quarantine,
            &root,
            "disabled-sentinel",
            1024,
            "source inode race",
        )
        .expect_err("a replacement source inode must never be quarantined");
        clear_test_pre_private_quarantine_rename_hook();

        assert!(error.contains("identity"));
        assert_eq!(fs::read_to_string(&source).unwrap(), "disabled-sentinel");
        assert_eq!(
            fs::read_to_string(&quarantine).unwrap(),
            "attacker-replacement",
            "the losing source entry must remain byte-for-byte as transaction evidence"
        );
        assert_eq!(
            fs::read_to_string(&displaced).unwrap(),
            "transaction-owned-catalog"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    ))]
    #[test]
    fn private_quarantine_placeholder_publish_failure_keeps_the_initial_prefix_retryable() {
        let root = test_root("private-quarantine-placeholder-publish");
        let source = root.join("catalog.json");
        let quarantine = root.join("catalog.quarantine");
        fs::create_dir_all(&root).unwrap();
        fs::write(&source, "transaction-owned-catalog").unwrap();

        install_test_private_quarantine_fault("placeholder-publish");
        let error = quarantine_private_text(
            &source,
            &quarantine,
            &root,
            "disabled-sentinel",
            1024,
            "placeholder publication failure",
        )
        .expect_err("the injected no-replace publication fault must interrupt invalidation");
        clear_test_private_quarantine_fault();
        assert!(error.contains("placeholder-publish"));
        assert_eq!(
            fs::read_to_string(&source).unwrap(),
            "transaction-owned-catalog"
        );
        assert!(!quarantine.exists());
        assert!(
            !fs::read_dir(&root).unwrap().filter_map(Result::ok).any(|entry| {
                entry
                    .file_name()
                    .to_str()
                    .is_some_and(|name| name.ends_with(".tmp-codexhub"))
            }),
            "a failed placeholder publication must remove its private temp"
        );

        let readback = quarantine_private_text(
            &source,
            &quarantine,
            &root,
            "disabled-sentinel",
            1024,
            "placeholder publication failure",
        )
        .expect("the untouched initial prefix must retry");
        assert_eq!(readback, "transaction-owned-catalog");
        assert_eq!(fs::read_to_string(&source).unwrap(), "disabled-sentinel");
        assert_eq!(
            fs::read_to_string(&quarantine).unwrap(),
            "transaction-owned-catalog"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    ))]
    #[test]
    fn private_quarantine_resumes_exact_pre_exchange_placeholder_prefixes() {
        for phase in ["after-placeholder", "before-exchange", "exchange"] {
            let root = test_root(&format!("private-quarantine-{phase}"));
            let source = root.join("catalog.json");
            let quarantine = root.join("catalog.quarantine");
            fs::create_dir_all(&root).unwrap();
            fs::write(&source, "transaction-owned-catalog").unwrap();

            install_test_private_quarantine_fault(phase);
            let error = quarantine_private_text(
                &source,
                &quarantine,
                &root,
                "disabled-sentinel",
                1024,
                "pre-exchange crash prefix",
            )
            .expect_err("the injected pre-exchange fault must interrupt publication");
            clear_test_private_quarantine_fault();
            assert!(error.contains(phase));
            assert_eq!(
                fs::read_to_string(&source).unwrap(),
                "transaction-owned-catalog"
            );
            assert_eq!(
                fs::read_to_string(&quarantine).unwrap(),
                "disabled-sentinel"
            );

            let readback = quarantine_private_text(
                &source,
                &quarantine,
                &root,
                "disabled-sentinel",
                1024,
                "pre-exchange crash prefix",
            )
            .expect("an exact placeholder prefix must resume");
            assert_eq!(readback, "transaction-owned-catalog");
            assert_eq!(fs::read_to_string(&source).unwrap(), "disabled-sentinel");
            assert_eq!(
                fs::read_to_string(&quarantine).unwrap(),
                "transaction-owned-catalog"
            );
            let _ = fs::remove_dir_all(root);
        }
    }

    #[cfg(all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    ))]
    #[test]
    fn private_quarantine_post_exchange_fault_preserves_the_completed_prefix() {
        let root = test_root("private-quarantine-after-exchange");
        let source = root.join("catalog.json");
        let quarantine = root.join("catalog.quarantine");
        fs::create_dir_all(&root).unwrap();
        fs::write(&source, "transaction-owned-catalog").unwrap();

        install_test_private_quarantine_fault("after-exchange");
        let error = quarantine_private_text(
            &source,
            &quarantine,
            &root,
            "disabled-sentinel",
            1024,
            "post-exchange crash prefix",
        )
        .expect_err("the injected post-exchange fault must interrupt readback");
        clear_test_private_quarantine_fault();

        assert!(error.contains("after-exchange"));
        assert_eq!(fs::read_to_string(&source).unwrap(), "disabled-sentinel");
        assert_eq!(
            fs::read_to_string(&quarantine).unwrap(),
            "transaction-owned-catalog"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn private_quarantine_rejects_collision_oversize_and_link_sources_without_mutation() {
        let collision_root = test_root("private-quarantine-collision");
        fs::create_dir_all(&collision_root).unwrap();
        let collision_source = collision_root.join("catalog.json");
        let collision_target = collision_root.join("catalog.json.quarantine");
        fs::write(&collision_source, "source").unwrap();
        fs::write(&collision_target, "existing-evidence").unwrap();

        let collision = quarantine_private_text(
            &collision_source,
            &collision_target,
            &collision_root,
            "disabled-sentinel",
            1024,
            "collision source",
        )
        .expect_err("an existing quarantine must never be replaced");

        assert!(collision.contains("already exists"));
        assert_eq!(fs::read_to_string(&collision_source).unwrap(), "source");
        assert_eq!(
            fs::read_to_string(&collision_target).unwrap(),
            "existing-evidence"
        );

        let collision_race_root = test_root("private-quarantine-collision-race");
        fs::create_dir_all(&collision_race_root).unwrap();
        let collision_race_source = collision_race_root.join("catalog.json");
        let collision_race_target = collision_race_root.join("catalog.json.quarantine");
        fs::write(&collision_race_source, "race-source").unwrap();
        let collision_race_target_for_hook = collision_race_target.clone();
        install_test_pre_private_quarantine_hook(move |_| {
            fs::write(&collision_race_target_for_hook, "racing-evidence").unwrap();
        });

        let collision_race = quarantine_private_text(
            &collision_race_source,
            &collision_race_target,
            &collision_race_root,
            "disabled-sentinel",
            1024,
            "collision race source",
        )
        .expect_err("a quarantine created after validation must never be replaced");
        clear_test_pre_private_quarantine_hook();

        assert!(collision_race.contains("quarantine"));
        assert_eq!(
            fs::read_to_string(&collision_race_source).unwrap(),
            "race-source"
        );
        assert_eq!(
            fs::read_to_string(&collision_race_target).unwrap(),
            "racing-evidence"
        );

        let oversize_root = test_root("private-quarantine-oversize");
        let oversize_parent = oversize_root.join("owned");
        let oversize_moved = oversize_root.join("owned-after-error");
        fs::create_dir_all(&oversize_parent).unwrap();
        let oversize_source = oversize_parent.join("catalog.json");
        fs::write(&oversize_source, "too-large").unwrap();
        let oversize = quarantine_private_text(
            &oversize_source,
            &oversize_parent.join("catalog.json.quarantine"),
            &oversize_root,
            "disabled-sentinel",
            3,
            "oversize source",
        )
        .expect_err("oversized source must remain in place");

        assert!(oversize.contains("size limit"));
        assert_eq!(fs::read_to_string(&oversize_source).unwrap(), "too-large");
        fs::rename(&oversize_parent, &oversize_moved)
            .expect("the retained parent handle must be released on an error path");

        let link_root = test_root("private-quarantine-link-source");
        fs::create_dir_all(&link_root).unwrap();
        let victim = link_root.join("victim");
        fs::create_dir_all(&victim).unwrap();
        fs::write(victim.join("catalog.json"), "victim").unwrap();
        let link_source = link_root.join("catalog-link");
        #[cfg(unix)]
        std::os::unix::fs::symlink(victim.join("catalog.json"), &link_source).unwrap();
        #[cfg(windows)]
        {
            let status = Command::new("cmd")
                .args([
                    "/C",
                    "mklink",
                    "/J",
                    &link_source.to_string_lossy(),
                    &victim.to_string_lossy(),
                ])
                .status()
                .unwrap();
            assert!(status.success(), "junction source fixture must be created");
        }

        let link_error = quarantine_private_text(
            &link_source,
            &link_root.join("catalog-link.quarantine"),
            &link_root,
            "disabled-sentinel",
            1024,
            "linked source",
        )
        .expect_err("symlink or reparse source must fail closed");

        assert!(link_error.contains("symlink") || link_error.contains("reparse"));
        assert_eq!(
            fs::read_to_string(victim.join("catalog.json")).unwrap(),
            "victim"
        );
        assert!(!link_root.join("catalog-link.quarantine").exists());
        let _ = fs::remove_dir_all(collision_root);
        let _ = fs::remove_dir_all(collision_race_root);
        let _ = fs::remove_dir_all(oversize_root);
        let _ = fs::remove_dir_all(link_root);
    }

    #[cfg(windows)]
    #[test]
    fn private_atomic_write_supports_long_nested_windows_paths() {
        let root = test_root("private-long-path");
        let parent = root
            .join(format!("nested-a-{}", "a".repeat(72)))
            .join(format!("nested-b-{}", "b".repeat(72)))
            .join(format!("nested-c-{}", "c".repeat(72)));
        fs::create_dir_all(&parent).unwrap();
        let target = parent.join(".request-token");
        assert!(
            target.as_os_str().to_string_lossy().encode_utf16().count() > 260,
            "fixture must cross the legacy Win32 MAX_PATH boundary"
        );

        write_private_text_atomic(&target, "one-shot-token\n", &root).unwrap();

        assert_eq!(fs::read_to_string(&target).unwrap(), "one-shot-token\n");
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn private_temp_file_never_exposes_an_inherited_dacl_to_parent_monitoring() {
        use std::sync::{
            atomic::{AtomicBool, Ordering},
            Arc,
        };

        let root = test_root("private-temp-creation-dacl");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("secret.backup");
        let monitor_ready = Arc::new(AtomicBool::new(false));
        let stop = Arc::new(AtomicBool::new(false));
        let exposed = Arc::new(AtomicBool::new(false));
        let monitor_root = root.clone();
        let monitor_ready_clone = monitor_ready.clone();
        let stop_clone = stop.clone();
        let exposed_clone = exposed.clone();
        let monitor = thread::spawn(move || {
            monitor_ready_clone.store(true, Ordering::Release);
            while !stop_clone.load(Ordering::Acquire) {
                let Ok(entries) = fs::read_dir(&monitor_root) else {
                    continue;
                };
                for entry in entries.flatten() {
                    let name = entry.file_name().to_string_lossy().into_owned();
                    if !name.ends_with(".tmp-codexhub") {
                        continue;
                    }
                    if let Ok(sddl) = super::security_descriptor_sddl(&entry.path()) {
                        if !sddl.contains("D:P") {
                            exposed_clone.store(true, Ordering::Release);
                        }
                    }
                }
            }
        });
        while !monitor_ready.load(Ordering::Acquire) {
            thread::yield_now();
        }

        write_private_text_atomic(&target, "sensitive", &root).unwrap();
        stop.store(true, Ordering::Release);
        monitor.join().unwrap();

        assert!(
            !exposed.load(Ordering::Acquire),
            "private temp file was visible before its protected DACL was installed"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn secure_read_rejects_path_identity_replacement() {
        let root = test_root("secure-read-replacement");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("source");
        let replacement = root.join("replacement");
        fs::write(&target, "original").unwrap();
        fs::write(&replacement, "replacement").unwrap();
        let opens = std::rc::Rc::new(std::cell::Cell::new(0));
        let opens_for_hook = opens.clone();
        let target_for_hook = target.clone();
        let replacement_for_hook = replacement.clone();
        install_test_pre_open_hook(move |path| {
            if path == target_for_hook && opens_for_hook.get() == 1 {
                fs::remove_file(&target_for_hook).unwrap();
                fs::rename(&replacement_for_hook, &target_for_hook).unwrap();
            }
            opens_for_hook.set(opens_for_hook.get() + 1);
        });

        let error = read_single_link_text(&target, 1024, "secure source")
            .expect_err("path replacement must fail closed");
        clear_test_pre_open_hook();

        assert!(error.contains("identity changed"));
    }

    #[cfg(unix)]
    #[test]
    fn private_atomic_write_publishes_owner_only_mode() {
        use std::os::unix::fs::PermissionsExt;
        let root = test_root("private-mode");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("secret.backup");

        write_private_text_atomic(&target, "sensitive", &root).unwrap();

        assert_eq!(
            fs::metadata(target).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }

    fn test_root(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "codexhub-safe-file-{name}-{}",
            SystemTime::now()
                .duration_since(SystemTime::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ))
    }

    #[test]
    fn write_text_atomic_keeps_persistent_versioned_lock() {
        let root = test_root("lock-protocol");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("providers.toml");
        write_text_atomic(&target, "new").unwrap();
        assert_eq!(
            fs::read_to_string(root.join("providers.toml.lock")).unwrap(),
            "codexhub-atomic-lock=1\n"
        );
    }

    #[test]
    fn legacy_recovery_is_never_based_on_age() {
        assert!(matches!(
            lock_state("pid=0\nacquired_at_millis=0\n"),
            LockState::Unknown
        ));
        assert!(matches!(
            lock_state("acquired_at_millis=0\n"),
            LockState::Unknown
        ));
        assert!(matches!(lock_state("not-a-lock\n"), LockState::Unknown));
    }

    #[test]
    fn parser_accepts_only_the_shared_protocol_and_legacy_shape() {
        assert!(matches!(
            lock_state("codexhub-atomic-lock=1\n"),
            LockState::Protocol
        ));
        assert!(matches!(
            lock_state("codexhub-atomic-lock=1\r\n"),
            LockState::Protocol
        ));
        assert_eq!(
            parse_legacy_pid("pid=1\r\nacquired_at_millis=0\r\n"),
            Some(1)
        );
        assert_eq!(
            parse_legacy_pid("pid=1\nacquired_at_millis=340282366920938463463374607431768211456\n"),
            None
        );
        assert!(matches!(
            lock_state("codexhub-atomic-lock=1"),
            LockState::Unknown
        ));
        assert!(matches!(
            lock_state("codexhub-atomic-lock=2\n"),
            LockState::Unknown
        ));
        assert!(matches!(
            lock_state("codexhub-atomic-lock=1\nextra=value\n"),
            LockState::Unknown
        ));
        assert!(matches!(
            lock_state("pid=1\npid=2\nacquired_at_millis=0\n"),
            LockState::Unknown
        ));
        assert!(matches!(
            lock_state("pid=-1\nacquired_at_millis=0\n"),
            LockState::Unknown
        ));
        assert!(matches!(
            lock_state("pid=999999999999999999999999\nacquired_at_millis=0\n"),
            LockState::Unknown
        ));
    }

    #[test]
    fn existing_empty_lock_fails_closed() {
        let root = test_root("empty-lock");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("settings.json");
        let lock = root.join("settings.json.lock");
        fs::write(&lock, b"").unwrap();

        let error = write_text_atomic(&target, "new").unwrap_err();

        assert!(error.contains("timed out") || error.contains("unavailable"));
        assert_eq!(fs::read(&lock).unwrap(), b"");
    }

    #[test]
    fn dead_legacy_lock_is_recovered_without_unlinking_its_inode() {
        let root = test_root("dead-legacy");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("settings.json");
        let lock = root.join("settings.json.lock");
        let mut child = Command::new("python")
            .arg("-c")
            .arg("pass")
            .spawn()
            .unwrap();
        let dead_pid = child.id();
        assert!(child.wait().unwrap().success());
        fs::write(&lock, format!("pid={dead_pid}\nacquired_at_millis=0\n")).unwrap();
        let mut original = fs::File::open(&lock).unwrap();

        write_text_atomic(&target, "new").unwrap();

        assert_eq!(fs::read_to_string(&target).unwrap(), "new");
        let mut original_text = String::new();
        original.read_to_string(&mut original_text).unwrap();
        assert_eq!(original_text, LOCK_PROTOCOL);
        assert_eq!(fs::read_to_string(&lock).unwrap(), LOCK_PROTOCOL);
    }

    #[test]
    fn unknown_legacy_and_future_locks_fail_closed() {
        let root = test_root("unknown-lock");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("settings.json");
        let lock = root.join("settings.json.lock");
        for metadata in [
            "acquired_at_millis=0\n",
            "not-a-lock\n",
            "codexhub-atomic-lock=2\n",
            "codexhub-atomic-lock=1\nextra=value\n",
            "pid=0\nacquired_at_millis=0\n",
        ] {
            fs::write(&lock, metadata).unwrap();
            let error = write_text_atomic(&target, "new").unwrap_err();
            assert!(error.contains("unavailable"));
        }
    }

    #[test]
    fn hard_link_lock_is_rejected_by_production_entrypoint() {
        let root = test_root("hard-link-lock");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("settings.json");
        let victim = root.join("victim");
        let lock = root.join("settings.json.lock");
        fs::write(&victim, "do not modify").unwrap();
        if fs::hard_link(&victim, &lock).is_err() {
            return;
        }

        let error = write_text_atomic(&target, "new").unwrap_err();

        assert!(error.contains("atomic write lock"));
        assert_eq!(fs::read_to_string(&victim).unwrap(), "do not modify");
        assert!(!target.exists());
    }

    #[test]
    fn invalid_utf8_lock_is_rejected_by_production_entrypoint() {
        let root = test_root("invalid-utf8-lock");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("settings.json");
        let lock = root.join("settings.json.lock");
        fs::write(&lock, b"pid=1\nacquired_at_millis=0\n\xff").unwrap();

        let error = write_text_atomic(&target, "new").unwrap_err();

        assert_eq!(error, "atomic write lock is unavailable");
        assert!(!target.exists());
        assert!(fs::read(&lock).unwrap().ends_with(b"\xff"));
    }

    #[test]
    fn existing_primary_replacement_between_metadata_and_open_is_rejected() {
        let root = test_root("primary-pre-open-replacement");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("settings.json");
        let lock = root.join("settings.json.lock");
        let guard = root.join("settings.json.lock.guard");
        let replacement = root.join("replacement.lock");
        fs::write(&guard, LOCK_PROTOCOL).unwrap();
        fs::write(&lock, LOCK_PROTOCOL).unwrap();
        fs::write(&replacement, LOCK_PROTOCOL).unwrap();
        let replaced = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let replaced_for_hook = replaced.clone();
        let lock_for_hook = lock.clone();
        let replacement_for_hook = replacement.clone();
        install_test_pre_open_hook(move |path| {
            if path == lock_for_hook.as_path()
                && !replaced_for_hook.swap(true, std::sync::atomic::Ordering::SeqCst)
            {
                fs::remove_file(&lock_for_hook).unwrap();
                fs::rename(&replacement_for_hook, &lock_for_hook).unwrap();
            }
        });

        let result = FileLock::acquire(&target);
        super::clear_test_pre_open_hook();

        assert!(replaced.load(std::sync::atomic::Ordering::SeqCst));
        match result {
            Err(error) => assert!(error.contains("path changed")),
            Ok(_) => panic!("replacement was accepted"),
        }
        assert!(!target.exists());
        assert_eq!(fs::read_to_string(&lock).unwrap(), LOCK_PROTOCOL);
    }

    #[test]
    fn existing_guard_replacement_between_metadata_and_open_is_rejected() {
        let root = test_root("guard-pre-open-replacement");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("settings.json");
        let lock = root.join("settings.json.lock");
        let guard = root.join("settings.json.lock.guard");
        let replacement = root.join("replacement.guard");
        fs::write(&guard, LOCK_PROTOCOL).unwrap();
        fs::write(&lock, LOCK_PROTOCOL).unwrap();
        fs::write(&replacement, LOCK_PROTOCOL).unwrap();
        let replaced = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let replaced_for_hook = replaced.clone();
        let guard_for_hook = guard.clone();
        let replacement_for_hook = replacement.clone();
        install_test_pre_open_hook(move |path| {
            if path == guard_for_hook.as_path()
                && !replaced_for_hook.swap(true, std::sync::atomic::Ordering::SeqCst)
            {
                fs::remove_file(&guard_for_hook).unwrap();
                fs::rename(&replacement_for_hook, &guard_for_hook).unwrap();
            }
        });

        let result = FileLock::acquire(&target);
        super::clear_test_pre_open_hook();

        assert!(replaced.load(std::sync::atomic::Ordering::SeqCst));
        match result {
            Err(error) => assert!(error.contains("path changed")),
            Ok(_) => panic!("replacement was accepted"),
        }
        assert!(!target.exists());
        assert_eq!(fs::read_to_string(&guard).unwrap(), LOCK_PROTOCOL);
    }

    #[cfg(windows)]
    #[test]
    fn write_text_atomic_rejects_directory_junction_lock_without_changes() {
        use std::os::windows::fs::MetadataExt;

        let root = test_root("directory-junction-lock");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("settings.json");
        let victim = root.join("victim");
        let lock = root.join("settings.json.lock");
        let guard = root.join("settings.json.lock.guard");
        fs::create_dir_all(&victim).unwrap();
        let victim_file = victim.join("sentinel");
        fs::write(&victim_file, "do not modify").unwrap();
        fs::write(&target, "old").unwrap();
        let status = std::process::Command::new("cmd")
            .args([
                "/C",
                "mklink",
                "/J",
                &lock.to_string_lossy(),
                &victim.to_string_lossy(),
            ])
            .status()
            .unwrap();
        assert!(
            status.success(),
            "CI must provide a directory junction fixture"
        );
        let before = fs::symlink_metadata(&lock).unwrap();

        let error = write_text_atomic(&target, "new").unwrap_err();

        let after = fs::symlink_metadata(&lock).unwrap();
        assert!(error.contains("atomic write lock"));
        assert_eq!(fs::read_to_string(&target).unwrap(), "old");
        assert_eq!(fs::read_to_string(&victim_file).unwrap(), "do not modify");
        assert_eq!(before.file_attributes(), after.file_attributes());
        assert!(!target.with_file_name(".settings.json").exists());
        let _ = fs::remove_file(&guard);
    }

    #[test]
    fn guard_replacement_after_acquire_is_rejected_before_writer_operation() {
        let root = test_root("guard-replacement-after-acquire");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("settings.json");
        let guard = root.join("settings.json.lock.guard");
        let lock = FileLock::acquire(&target).unwrap();
        fs::remove_file(&guard).unwrap();
        fs::write(&guard, LOCK_PROTOCOL).unwrap();

        // Rejection must fail closed; the message differs by platform:
        // Windows still resolves the delete-pending handle (identity mismatch),
        // while Linux reports the unlinked handle's zero link count first.
        let result = write_text_locked(&target, "probe", &lock);
        let error = result.unwrap_err();
        assert!(
            error.contains("path changed") || error.contains("not a regular single-link file"),
            "unexpected error: {error}"
        );
        drop(lock);
    }

    #[test]
    fn write_text_locked_rejects_mismatched_target_path() {
        let root = test_root("mismatched-target");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("settings.json");
        let other = root.join("other.json");
        let lock = FileLock::acquire(&target).unwrap();

        let error = write_text_locked(&other, "probe", &lock).unwrap_err();
        assert!(
            error.contains("does not match target path"),
            "unexpected error: {error}"
        );
        drop(lock);
    }

    struct PythonHolder {
        child: std::process::Child,
        stdin: std::process::ChildStdin,
        events: mpsc::Receiver<String>,
    }

    fn python_holder(target: &Path) -> PythonHolder {
        let source = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../src-python");
        let script = "import pathlib, sys; from atomic_io import file_lock_for; target = pathlib.Path(sys.argv[1]);\nwith file_lock_for(target):\n    print('ready', flush=True);\n    if sys.stdin.readline().strip() != 'release': raise SystemExit(2);\n    print('released', flush=True)";
        let mut child = Command::new("python")
            .env("PYTHONPATH", source)
            .arg("-c")
            .arg(script)
            .arg(target)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .expect("python is required for the cross-language lock tests");
        let stdin = child.stdin.take().unwrap();
        let stdout = child.stdout.take().unwrap();
        let (events_tx, events_rx) = mpsc::channel();
        thread::spawn(move || {
            for line in std::io::BufReader::new(stdout).lines() {
                let Ok(line) = line else { break };
                if events_tx.send(line).is_err() {
                    break;
                }
            }
        });
        PythonHolder {
            child,
            stdin,
            events: events_rx,
        }
    }

    fn expect_handshake(events: &mpsc::Receiver<String>, expected: &str) {
        assert_eq!(
            events.recv_timeout(Duration::from_secs(10)).unwrap(),
            expected
        );
    }

    #[test]
    fn python_holder_blocks_rust_contender_until_handshake_release() {
        let root = test_root("python-holder-rust-contender");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("shared.json");
        let lock_path = target.with_file_name("shared.json.lock");
        let mut holder = python_holder(&target);
        expect_handshake(&holder.events, "ready");

        let (events_tx, events_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let (replacement_verified_tx, replacement_verified_rx) = mpsc::channel();
        let (done_tx, done_rx) = mpsc::channel();
        let contender = target.clone();
        thread::spawn(move || {
            let hook = |event: &'static str| {
                events_tx.send(event.to_owned()).unwrap();
                if event == "blocked" {
                    replacement_verified_rx
                        .recv_timeout(Duration::from_secs(10))
                        .unwrap();
                    events_tx.send("replacement-verified".to_owned()).unwrap();
                }
            };
            let lock = FileLock::acquire_with_hook(&contender, &hook).unwrap();
            done_tx.send(()).unwrap();
            release_rx.recv_timeout(Duration::from_secs(10)).unwrap();
            drop(lock);
        });
        expect_handshake(&events_rx, "attempt");
        expect_handshake(&events_rx, "blocked");
        fs::remove_file(&lock_path).unwrap();
        fs::write(&lock_path, LOCK_PROTOCOL).unwrap();
        replacement_verified_tx.send(()).unwrap();
        expect_handshake(&events_rx, "replacement-verified");
        holder.stdin.write_all(b"release\n").unwrap();
        holder.stdin.flush().unwrap();
        expect_handshake(&holder.events, "released");
        assert!(holder.child.wait().unwrap().success());
        expect_handshake(&events_rx, "attempt");
        expect_handshake(&events_rx, "acquired");
        release_tx.send(()).unwrap();
        done_rx.recv_timeout(Duration::from_secs(10)).unwrap();
        assert!(!target.exists());
    }

    #[test]
    fn rust_holder_blocks_python_contender_until_handshake_release() {
        let root = test_root("rust-holder-python-contender");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("shared.json");
        let source = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../src-python");
        let script = "import pathlib, sys; from atomic_io import _set_test_lock_hook, atomic_write_text; target = pathlib.Path(sys.argv[1]); _set_test_lock_hook(lambda event: print(event, flush=True)); atomic_write_text(target, 'python'); print('entered', flush=True)";
        let lock = FileLock::acquire(&target).unwrap();
        let mut child = Command::new("python")
            .env("PYTHONPATH", source)
            .arg("-c")
            .arg(script)
            .arg(&target)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap();
        let stdout = child.stdout.take().unwrap();
        let (events_tx, events_rx) = mpsc::channel();
        thread::spawn(move || {
            for line in std::io::BufReader::new(stdout).lines() {
                let Ok(line) = line else { break };
                if events_tx.send(line).is_err() {
                    break;
                }
            }
        });
        expect_handshake(&events_rx, "attempt");
        expect_handshake(&events_rx, "blocked");
        drop(lock);
        expect_handshake(&events_rx, "attempt");
        expect_handshake(&events_rx, "acquired");
        expect_handshake(&events_rx, "entered");
        assert!(child.wait().unwrap().success());
        assert_eq!(fs::read_to_string(target).unwrap(), "python");
    }

    #[test]
    fn killed_python_holder_releases_protocol_lock_for_rust_recovery() {
        let root = test_root("killed-python-holder");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("shared.json");
        let mut holder = python_holder(&target);
        expect_handshake(&holder.events, "ready");
        holder.child.kill().unwrap();
        holder.child.wait().unwrap();
        write_text_atomic(&target, "recovered").unwrap();
        assert_eq!(fs::read_to_string(target).unwrap(), "recovered");
    }

    fn recv_until(events: &mpsc::Receiver<String>, expected: &str) {
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        loop {
            let remaining = deadline.saturating_duration_since(std::time::Instant::now());
            assert!(!remaining.is_zero(), "timed out waiting for {expected}");
            let event = events.recv_timeout(remaining).unwrap();
            if event == expected {
                return;
            }
        }
    }

    #[test]
    fn live_legacy_lock_is_never_reclaimed() {
        let root = test_root("live-legacy");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("settings.json");
        let lock = root.join("settings.json.lock");
        let metadata = format!("pid={}\nacquired_at_millis=0\n", std::process::id());
        fs::write(&lock, &metadata).unwrap();

        let error = write_text_atomic(&target, "new").unwrap_err();

        assert!(error.contains("unavailable"));
        assert_eq!(fs::read_to_string(&lock).unwrap(), metadata);
        assert!(!target.exists());
    }

    #[test]
    fn release_is_idempotent_and_protocol_instance_persists() {
        let root = test_root("release-idempotent");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("settings.json");
        let mut lock = FileLock::acquire(&target).unwrap();
        lock.release().unwrap();
        lock.release().unwrap();
        assert_eq!(
            fs::read_to_string(root.join("settings.json.lock")).unwrap(),
            LOCK_PROTOCOL
        );
    }

    #[test]
    fn release_after_external_replacement_keeps_replacement_instance() {
        let root = test_root("release-external-replacement");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("settings.json");
        let lock_path = root.join("settings.json.lock");
        let lock = FileLock::acquire(&target).unwrap();
        fs::remove_file(&lock_path).unwrap();
        fs::write(&lock_path, LOCK_PROTOCOL).unwrap();
        // The owner's release operates on its own handle and never unlinks, so
        // the external replacement instance survives untouched.
        drop(lock);
        assert_eq!(fs::read_to_string(&lock_path).unwrap(), LOCK_PROTOCOL);
    }

    #[test]
    fn protocol_lock_carries_no_age_metadata_and_contender_stays_blocked() {
        let root = test_root("no-age-metadata");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("settings.json");
        // The persisted record carries no timestamp: age alone can never
        // authorize a second writer, however long the first one holds.
        write_text_atomic(&target, "seed").unwrap();
        assert_eq!(
            fs::read_to_string(root.join("settings.json.lock")).unwrap(),
            LOCK_PROTOCOL
        );
        fs::remove_file(&target).unwrap();

        let lock = FileLock::acquire(&target).unwrap();
        let (events_tx, events_rx) = mpsc::channel();
        let contender_target = target.clone();
        let contender = thread::spawn(move || {
            let hook = |event: &'static str| events_tx.send(event.to_owned()).unwrap();
            FileLock::acquire_with_hook(&contender_target, &hook).unwrap()
        });
        expect_handshake(&events_rx, "attempt");
        expect_handshake(&events_rx, "blocked");
        drop(lock);
        recv_until(&events_rx, "acquired");
        contender.join().unwrap();
    }

    #[test]
    fn same_language_ab_c_choreography_keeps_single_owner() {
        let root = test_root("ab-c-choreography");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("settings.json");
        let lock_path = root.join("settings.json.lock");

        // A holds first.
        let a_lock = FileLock::acquire(&target).unwrap();

        // B waits and is provably blocked while A holds the namespace guard.
        let (b_events_tx, b_events_rx) = mpsc::channel();
        let (b_release_tx, b_release_rx) = mpsc::channel();
        let (b_done_tx, b_done_rx) = mpsc::channel();
        let b_target = target.clone();
        thread::spawn(move || {
            let hook = |event: &'static str| b_events_tx.send(event.to_owned()).unwrap();
            let lock = FileLock::acquire_with_hook(&b_target, &hook).unwrap();
            b_events_tx.send("b-inside".to_owned()).unwrap();
            b_release_rx.recv_timeout(Duration::from_secs(10)).unwrap();
            drop(lock);
            b_done_tx.send(()).unwrap();
        });
        expect_handshake(&b_events_rx, "attempt");
        expect_handshake(&b_events_rx, "blocked");

        drop(a_lock);
        // A's release operates on its own handle: the instance B is about to
        // own survives, so B legitimately follows A.
        assert_eq!(fs::read_to_string(&lock_path).unwrap(), LOCK_PROTOCOL);
        recv_until(&b_events_rx, "acquired");
        recv_until(&b_events_rx, "b-inside");

        // C cannot overlap B's critical section; it waits for B's release.
        let (c_events_tx, c_events_rx) = mpsc::channel();
        let (c_done_tx, c_done_rx) = mpsc::channel();
        let c_target = target.clone();
        let c_handle = thread::spawn(move || {
            let hook = |event: &'static str| c_events_tx.send(event.to_owned()).unwrap();
            let lock = FileLock::acquire_with_hook(&c_target, &hook).unwrap();
            drop(lock);
            c_done_tx.send(()).unwrap();
        });
        expect_handshake(&c_events_rx, "attempt");
        expect_handshake(&c_events_rx, "blocked");
        assert!(c_done_rx.recv_timeout(Duration::from_millis(200)).is_err());

        b_release_tx.send(()).unwrap();
        b_done_rx.recv_timeout(Duration::from_secs(10)).unwrap();
        recv_until(&c_events_rx, "acquired");
        c_done_rx.recv_timeout(Duration::from_secs(10)).unwrap();
        c_handle.join().unwrap();
        assert_eq!(fs::read_to_string(&lock_path).unwrap(), LOCK_PROTOCOL);
    }

    #[test]
    fn simultaneous_rust_python_acquisition_produces_exactly_one_owner() {
        let root = test_root("simultaneous-rust-python");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("shared.json");
        let log = root.join("order.log");
        fs::write(&log, "").unwrap();
        let source = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../src-python");
        let rounds = 5;
        let script = "import pathlib, sys, time; from atomic_io import file_lock_for; target = pathlib.Path(sys.argv[1]); log = pathlib.Path(sys.argv[2]);\nfor _ in range(5):\n    with file_lock_for(target):\n        with log.open('a', encoding='ascii') as stream:\n            stream.write('START-P\\n'); stream.flush(); time.sleep(0.05); stream.write('END-P\\n')\nprint('done', flush=True)";
        let mut child = Command::new("python")
            .env("PYTHONPATH", &source)
            .arg("-c")
            .arg(script)
            .arg(&target)
            .arg(&log)
            .stdout(Stdio::null())
            .stderr(Stdio::inherit())
            .spawn()
            .expect("python is required for the cross-language lock tests");

        let rust_log = log.clone();
        let rust_target = target.clone();
        let worker = thread::spawn(move || {
            for _ in 0..rounds {
                let lock = FileLock::acquire(&rust_target).unwrap();
                {
                    let mut stream = fs::OpenOptions::new().append(true).open(&rust_log).unwrap();
                    stream.write_all(b"START-R\n").unwrap();
                    stream.flush().unwrap();
                    thread::sleep(Duration::from_millis(50));
                    stream.write_all(b"END-R\n").unwrap();
                }
                drop(lock);
            }
        });
        worker.join().unwrap();
        assert!(child.wait().unwrap().success());

        let content = fs::read_to_string(&log).unwrap();
        let mut open: Option<&str> = None;
        let mut python_rounds = 0;
        let mut rust_rounds = 0;
        for line in content.lines() {
            if let Some(owner) = line.strip_prefix("START-") {
                assert!(open.is_none(), "overlapping critical sections at {line}");
                open = Some(owner);
            } else if let Some(owner) = line.strip_prefix("END-") {
                assert_eq!(open.take(), Some(owner), "mismatched END marker");
                match owner {
                    "P" => python_rounds += 1,
                    "R" => rust_rounds += 1,
                    _ => panic!("unexpected owner {owner}"),
                }
            } else {
                panic!("unexpected log line {line}");
            }
        }
        assert!(open.is_none());
        assert_eq!(python_rounds, 5);
        assert_eq!(rust_rounds, rounds);
    }
}
