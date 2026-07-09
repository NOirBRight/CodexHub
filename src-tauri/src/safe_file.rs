use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

const LOCK_WAIT_TIMEOUT: Duration = Duration::from_secs(10);
const LOCK_RETRY_DELAY: Duration = Duration::from_millis(25);

pub(crate) fn write_text_atomic(path: &Path, text: &str) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            format!(
                "failed to create config directory {}: {error}",
                parent.display()
            )
        })?;
    }

    let _lock = FileLock::acquire(path)?;
    let temp_path = unique_temp_path(path);
    let mut temp_file = File::create(&temp_path).map_err(|error| {
        format!(
            "failed to write temp config {}: {error}",
            temp_path.display()
        )
    })?;
    temp_file
        .write_all(text.as_bytes())
        .and_then(|_| temp_file.sync_all())
        .map_err(|error| {
            let _ = fs::remove_file(&temp_path);
            format!(
                "failed to write temp config {}: {error}",
                temp_path.display()
            )
        })?;
    drop(temp_file);

    fs::rename(&temp_path, path).map_err(|error| {
        let _ = fs::remove_file(&temp_path);
        format!(
            "failed to move temp config {} to {}: {error}",
            temp_path.display(),
            path.display()
        )
    })
}

fn unique_temp_path(path: &Path) -> PathBuf {
    path.with_file_name(format!(
        ".{}.{}.{}.tmp-codexhub",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("config"),
        std::process::id(),
        timestamp_millis()
    ))
}

fn lock_path(path: &Path) -> PathBuf {
    path.with_file_name(format!(
        "{}.lock",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("config")
    ))
}

fn timestamp_millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or_default()
}

struct FileLock {
    path: PathBuf,
}

impl FileLock {
    fn acquire(target: &Path) -> Result<Self, String> {
        let path = lock_path(target);
        let started = Instant::now();
        loop {
            match OpenOptions::new().write(true).create_new(true).open(&path) {
                Ok(mut file) => {
                    let _ = writeln!(file, "pid={}", std::process::id());
                    return Ok(Self { path });
                }
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                    if started.elapsed() >= LOCK_WAIT_TIMEOUT {
                        return Err(format!(
                            "timed out waiting for config lock {}",
                            path.display()
                        ));
                    }
                    thread::sleep(LOCK_RETRY_DELAY);
                }
                Err(error) => {
                    return Err(format!("failed to create config lock {}: {error}", path.display()))
                }
            }
        }
    }
}

impl Drop for FileLock {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

#[cfg(test)]
mod tests {
    use super::write_text_atomic;
    use std::{fs, path::PathBuf, time::SystemTime};

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
    fn write_text_atomic_does_not_clobber_existing_stale_temp_file() {
        let root = test_root("stale-temp");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("config.toml");
        let stale_temp = target.with_extension("tmp-codexhub");
        fs::write(&target, "old").unwrap();
        fs::write(&stale_temp, "stale-temp").unwrap();

        write_text_atomic(&target, "new").unwrap();

        assert_eq!(fs::read_to_string(&target).unwrap(), "new");
        assert_eq!(fs::read_to_string(&stale_temp).unwrap(), "stale-temp");
    }
}
