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
