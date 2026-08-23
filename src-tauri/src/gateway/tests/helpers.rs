static TEST_ENV_LOCK: OnceLock<Mutex<()>> = OnceLock::new();

fn published_context_windows(entries: &[(&str, u32)]) -> BTreeMap<String, u32> {
    entries
        .iter()
        .map(|(id, context_window)| ((*id).to_string(), *context_window))
        .collect()
}

fn stable_root(path: PathBuf) -> (PathBuf, super::BackupChannel) {
    (path, super::BackupChannel::Stable)
}

fn beta_root(path: PathBuf) -> (PathBuf, super::BackupChannel) {
    (path, super::BackupChannel::Beta)
}

/// Make a file unreplaceable so write-failure tests work on Linux too.
/// `set_readonly` on a file does not stop atomic replace (unlink + create).
struct ReplacementLock {
    path: PathBuf,
    #[cfg(unix)]
    previous_mode: u32,
}

fn lock_path_against_replacement(path: &Path) -> ReplacementLock {
    #[cfg(windows)]
    {
        let mut permissions = fs::metadata(path).unwrap().permissions();
        permissions.set_readonly(true);
        fs::set_permissions(path, permissions).unwrap();
        ReplacementLock {
            path: path.to_path_buf(),
        }
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let parent = path.parent().expect("file has a parent");
        let metadata = fs::metadata(parent).unwrap();
        let previous_mode = metadata.permissions().mode();
        let mut permissions = metadata.permissions();
        permissions.set_mode(0o555);
        fs::set_permissions(parent, permissions).unwrap();
        ReplacementLock {
            path: parent.to_path_buf(),
            previous_mode,
        }
    }
}

impl Drop for ReplacementLock {
    fn drop(&mut self) {
        #[cfg(windows)]
        {
            if let Ok(metadata) = fs::metadata(&self.path) {
                let mut permissions = metadata.permissions();
                permissions.set_readonly(false);
                let _ = fs::set_permissions(&self.path, permissions);
            }
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if let Ok(metadata) = fs::metadata(&self.path) {
                let mut permissions = metadata.permissions();
                permissions.set_mode(self.previous_mode);
                let _ = fs::set_permissions(&self.path, permissions);
            }
        }
    }
}
