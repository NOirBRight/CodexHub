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
thread_local! {
    static TEST_PRE_OPEN_EXISTING_HOOK: RefCell<Option<TestPreOpenHook>> = RefCell::new(None);
}

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

const LOCK_WAIT_TIMEOUT: Duration = Duration::from_secs(10);
const LOCK_RETRY_DELAY: Duration = Duration::from_millis(25);
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

pub(crate) fn write_text_atomic(path: &Path, text: &str) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            format!("failed to create file directory {}: {error}", parent.display())
        })?;
    }

    let lock = FileLock::acquire(path)?;
    lock.verify_namespace_identity()?;
    let temp_path = unique_temp_path(path);
    let mut temp_file = File::create(&temp_path)
        .map_err(|error| format!("failed to write temp file {}: {error}", temp_path.display()))?;
    temp_file
        .write_all(text.as_bytes())
        .and_then(|_| temp_file.sync_all())
        .map_err(|error| {
            let _ = fs::remove_file(&temp_path);
            format!("failed to write temp file {}: {error}", temp_path.display())
        })?;
    drop(temp_file);

    lock.verify_namespace_identity()
        .inspect_err(|_| {
            let _ = fs::remove_file(&temp_path);
        })?;
    fs::rename(&temp_path, path).map_err(|error| {
        let _ = fs::remove_file(&temp_path);
        format!("failed to move temp file {} to {}: {error}", temp_path.display(), path.display())
    })
}

fn unique_temp_path(path: &Path) -> PathBuf {
    path.with_file_name(format!(
        ".{}.{}.{}.tmp-codexhub",
        path.file_name().and_then(|name| name.to_str()).unwrap_or("file"),
        std::process::id(),
        timestamp_millis()
    ))
}

fn lock_path(path: &Path) -> PathBuf {
    path.with_file_name(format!(
        "{}.lock",
        path.file_name().and_then(|name| name.to_str()).unwrap_or("file")
    ))
}

fn timestamp_millis() -> u128 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|duration| duration.as_millis()).unwrap_or_default()
}

#[cfg(target_os = "linux")]
const LOCK_NOFOLLOW: i32 = 0x20000;
#[cfg(any(target_os = "macos", target_os = "ios", target_os = "freebsd", target_os = "openbsd", target_os = "netbsd"))]
const LOCK_NOFOLLOW: i32 = 0x100;
#[cfg(all(unix, not(any(target_os = "linux", target_os = "android", target_os = "macos", target_os = "ios", target_os = "freebsd", target_os = "openbsd", target_os = "netbsd"))))]
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
    primary.with_file_name(format!("{}.guard", primary.file_name().and_then(|name| name.to_str()).unwrap_or("file")))
}

fn acquire_namespace_guard(path: &Path, started: &Instant, hook: Option<&dyn Fn(&'static str)>) -> Result<File, String> {
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
                return Ok(file);
            }
            Ok(false) => {
                if let Some(hook) = hook {
                    hook("blocked");
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
struct FileLock {
    namespace_path: PathBuf,
    namespace: File,
    file: File,
    locked: bool,
    namespace_locked: bool,
}

impl FileLock {
    fn acquire(target: &Path) -> Result<Self, String> {
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
    let file = open_lock_file(path, false).map_err(|_| "atomic write lock path changed".to_owned())?;
    lock_file_identity(&file)
}

#[cfg(windows)]
fn lock_file_identity(file: &File) -> Result<(u32, u64), String> {
    use std::os::windows::io::AsRawHandle;
    let mut information = ByHandleFileInformation::default();
    let result = unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut information) };
    if result == 0 || information.number_of_links != 1 || information.file_attributes & win32::FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err("atomic write lock is not a regular single-link file".to_owned());
    }
    let index = (u64::from(information.file_index_high) << 32) | u64::from(information.file_index_low);
    Ok((information.volume_serial_number, index))
}

#[cfg(windows)]
fn validate_lock_handle(file: &File) -> Result<(), String> {
    lock_file_identity(file).map(|_| ())
}

#[cfg(unix)]
fn verify_lock_identity(path: &Path, file: &File) -> Result<(), String> {
    use std::os::unix::fs::MetadataExt;
    let path_metadata = fs::symlink_metadata(path)
        .map_err(|_| "atomic write lock path changed".to_owned())?;
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
    let path_metadata = fs::symlink_metadata(path)
        .map_err(|_| "atomic write lock path changed".to_owned())?;
    validate_lock_metadata(&path_metadata)?;
    let path_file = open_lock_file(path, false)
        .map_err(|_| "atomic write lock path changed".to_owned())?;
    let path_identity = lock_file_identity(&path_file)
        .map_err(|_| "atomic write lock path changed".to_owned())?;
    let file_identity = lock_file_identity(file)
        .map_err(|_| "atomic write lock path changed".to_owned())?;
    if path_identity != file_identity {
        return Err("atomic write lock path changed".to_owned());
    }
    Ok(())
}

fn prepare_lock_metadata(file: &mut File, created: bool) -> Result<(), LockMetadataError> {
    let mut text = String::new();
    file.seek(SeekFrom::Start(0)).map_err(|_| LockMetadataError::Io)?;
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
    file.seek(SeekFrom::Start(0)).map_err(|_| LockMetadataError::Io)?;
    file.write_all(LOCK_PROTOCOL.as_bytes()).map_err(|_| LockMetadataError::Io)?;
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
    if matches!(text, "codexhub-atomic-lock=1\n" | "codexhub-atomic-lock=1\r\n") {
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
    let lines: Vec<&str> = if let Some(body) = text.strip_suffix("\r\n") {
        if body.split("\r\n").any(|line| line.contains('\r')) {
            return None;
        }
        body.split("\r\n").collect()
    } else if let Some(body) = text.strip_suffix('\n') {
        if body.contains('\r') {
            return None;
        }
        body.split('\n').collect()
    } else {
        return None;
    };
    if lines.len() != 2 {
        return None;
    }

    let mut pid = None;
    let mut timestamp = None;
    for line in lines {
        let (key, value) = line.split_once('=')?;
        match key {
            "pid" if pid.is_none() => pid = parse_legacy_pid_value(value),
            "acquired_at_millis" if timestamp.is_none() => {
                timestamp = parse_decimal_u128(value)
            }
            _ => return None,
        }
    }
    pid.zip(timestamp).map(|(pid, _)| pid)
}

fn parse_legacy_pid_value(value: &str) -> Option<i64> {
    let parsed = parse_decimal_u128(value)?;
    (1..=i32::MAX as u128).contains(&parsed).then_some(parsed as i64)
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
    } else if matches!(std::io::Error::last_os_error().kind(), std::io::ErrorKind::WouldBlock) {
        Ok(false)
    } else {
        Err(())
    }
}

#[cfg(unix)]
fn unlock(file: &File) -> std::io::Result<()> {
    use std::os::fd::AsRawFd;
    if unsafe { flock(file.as_raw_fd(), flock_op::LOCK_UN) } == 0 { Ok(()) } else { Err(std::io::Error::last_os_error()) }
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
    if result != 0 { Ok(true) }
    else if matches!(std::io::Error::last_os_error().raw_os_error(), Some(code) if code == win32::ERROR_SHARING_VIOLATION || code == win32::ERROR_LOCK_VIOLATION) { Ok(false) }
    else { Err(()) }
}

#[cfg(windows)]
fn unlock(file: &File) -> std::io::Result<()> {
    use std::os::windows::io::AsRawHandle;
    let mut overlapped = Overlapped::default();
    if unsafe { UnlockFileEx(file.as_raw_handle(), 0, 1, 0, &mut overlapped) } != 0 { Ok(()) }
    else { Err(std::io::Error::last_os_error()) }
}

#[cfg(unix)]
unsafe extern "C" {
    fn flock(fd: i32, operation: i32) -> i32;
    fn kill(pid: i32, signal: i32) -> i32;
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
#[link(name = "kernel32")]
unsafe extern "system" {
    fn GetFileInformationByHandle(handle: *mut std::ffi::c_void, information: *mut ByHandleFileInformation) -> i32;
    fn LockFileEx(handle: *mut std::ffi::c_void, flags: u32, reserved: u32, low: u32, high: u32, overlapped: *mut Overlapped) -> i32;
    fn UnlockFileEx(handle: *mut std::ffi::c_void, reserved: u32, low: u32, high: u32, overlapped: *mut Overlapped) -> i32;
    fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut std::ffi::c_void;
    fn GetExitCodeProcess(handle: *mut std::ffi::c_void, code: *mut u32) -> i32;
    fn CloseHandle(handle: *mut std::ffi::c_void) -> i32;
}

#[cfg(test)]
mod tests {
    use super::{
        install_test_pre_open_hook, lock_state, parse_legacy_pid, write_text_atomic, FileLock, LockState,
        LOCK_PROTOCOL,
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

    fn test_root(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!("codexhub-safe-file-{name}-{}", SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).unwrap().as_nanos()))
    }


    #[test]
    fn write_text_atomic_keeps_persistent_versioned_lock() {
        let root = test_root("lock-protocol");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("providers.toml");
        write_text_atomic(&target, "new").unwrap();
        assert_eq!(fs::read_to_string(root.join("providers.toml.lock")).unwrap(), "codexhub-atomic-lock=1\n");
    }

    #[test]
    fn legacy_recovery_is_never_based_on_age() {
        assert!(matches!(lock_state("pid=0\nacquired_at_millis=0\n"), LockState::Unknown));
        assert!(matches!(lock_state("acquired_at_millis=0\n"), LockState::Unknown));
        assert!(matches!(lock_state("not-a-lock\n"), LockState::Unknown));
    }

    #[test]
    fn parser_accepts_only_the_shared_protocol_and_legacy_shape() {
        assert!(matches!(lock_state("codexhub-atomic-lock=1\n"), LockState::Protocol));
        assert!(matches!(lock_state("codexhub-atomic-lock=1\r\n"), LockState::Protocol));
        assert_eq!(parse_legacy_pid("pid=1\r\nacquired_at_millis=0\r\n"), Some(1));
        assert_eq!(parse_legacy_pid("pid=1\nacquired_at_millis=340282366920938463463374607431768211456\n"), None);
        assert!(matches!(lock_state("codexhub-atomic-lock=1"), LockState::Unknown));
        assert!(matches!(lock_state("codexhub-atomic-lock=2\n"), LockState::Unknown));
        assert!(matches!(lock_state("codexhub-atomic-lock=1\nextra=value\n"), LockState::Unknown));
        assert!(matches!(lock_state("pid=1\npid=2\nacquired_at_millis=0\n"), LockState::Unknown));
        assert!(matches!(lock_state("pid=-1\nacquired_at_millis=0\n"), LockState::Unknown));
        assert!(matches!(lock_state("pid=999999999999999999999999\nacquired_at_millis=0\n"), LockState::Unknown));
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
        let mut child = Command::new("python").arg("-c").arg("pass").spawn().unwrap();
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
            if path == lock_for_hook.as_path() && !replaced_for_hook.swap(true, std::sync::atomic::Ordering::SeqCst) {
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
            if path == guard_for_hook.as_path() && !replaced_for_hook.swap(true, std::sync::atomic::Ordering::SeqCst) {
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
            .args(["/C", "mklink", "/J", &lock.to_string_lossy(), &victim.to_string_lossy()])
            .status()
            .unwrap();
        assert!(status.success(), "CI must provide a directory junction fixture");
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

        let result = lock.verify_namespace_identity();

        assert!(result.unwrap_err().contains("path changed"));
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
        PythonHolder { child, stdin, events: events_rx }
    }

    fn expect_handshake(events: &mpsc::Receiver<String>, expected: &str) {
        assert_eq!(events.recv_timeout(Duration::from_secs(10)).unwrap(), expected);
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
                    replacement_verified_rx.recv_timeout(Duration::from_secs(10)).unwrap();
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
        assert_eq!(fs::read_to_string(root.join("settings.json.lock")).unwrap(), LOCK_PROTOCOL);
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
        assert_eq!(fs::read_to_string(root.join("settings.json.lock")).unwrap(), LOCK_PROTOCOL);
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
