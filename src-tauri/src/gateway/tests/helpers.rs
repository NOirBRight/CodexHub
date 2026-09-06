static TEST_ENV_LOCK: OnceLock<Mutex<()>> = OnceLock::new();

/// Keep Gateway client export tests independent from the developer's current
/// Codex subscription cache.  The production exporter intentionally treats
/// that cache as the Official membership authority, so a host with a newer or
/// older model list must not change these tests' expected gpt-5.4 assertions.
struct OfficialModelsTestHome {
    root: PathBuf,
    previous_codex_home: Option<std::ffi::OsString>,
    previous_runtime_home: Option<std::ffi::OsString>,
}

impl OfficialModelsTestHome {
    fn new() -> Self {
        let root = unique_temp_dir("codexhub-gateway-official-home");
        let catalogs = root.join("model-catalogs");
        fs::create_dir_all(&catalogs).unwrap();

        let official_models = ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark"];
        let subscription_models = official_models
            .iter()
            .map(|slug| {
                json!({
                    "slug": slug,
                    "display_name": slug,
                    "visibility": "list",
                })
            })
            .collect::<Vec<_>>();
        let published_models = official_models
            .iter()
            .map(|slug| {
                json!({
                    "slug": slug,
                    "codex_proxy_metadata": {
                        "provider": "openai",
                        "upstream_name": "official",
                        "official_context_budget": {
                            "source": "degraded_last_known_official",
                            "freshness": "stale",
                            "model_context_window": 272000,
                            "effective_context_window_percent": 95,
                            "effective_context_window": 258400,
                            "model_auto_compact_token_limit": 244800,
                        },
                    },
                })
            })
            .collect::<Vec<_>>();

        fs::write(
            catalogs.join("openai-plus-ollama-cloud.json"),
            serde_json::to_vec_pretty(&json!({
                "fetched_at": "2026-01-01T00:00:00Z",
                "models": subscription_models,
            }))
            .unwrap(),
        )
        .unwrap();
        fs::write(
            catalogs.join("codexhub-model-catalog.json"),
            serde_json::to_vec_pretty(&json!({ "models": published_models })).unwrap(),
        )
        .unwrap();

        let previous_codex_home = std::env::var_os("CODEX_HOME");
        let previous_runtime_home = std::env::var_os("CODEXHUB_RUNTIME_HOME");
        std::env::set_var("CODEX_HOME", &root);
        std::env::set_var("CODEXHUB_RUNTIME_HOME", &root);

        Self {
            root,
            previous_codex_home,
            previous_runtime_home,
        }
    }
}

impl Drop for OfficialModelsTestHome {
    fn drop(&mut self) {
        restore_env("CODEX_HOME", self.previous_codex_home.take());
        restore_env("CODEXHUB_RUNTIME_HOME", self.previous_runtime_home.take());
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn isolated_official_models_home() -> OfficialModelsTestHome {
    OfficialModelsTestHome::new()
}

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
            use std::os::windows::ffi::OsStrExt;
            use windows_sys::Win32::Storage::FileSystem::{
                GetFileAttributesW, SetFileAttributesW, FILE_ATTRIBUTE_READONLY,
                INVALID_FILE_ATTRIBUTES,
            };
            let path = self
                .path
                .as_os_str()
                .encode_wide()
                .chain(std::iter::once(0))
                .collect::<Vec<_>>();
            let attributes = unsafe { GetFileAttributesW(path.as_ptr()) };
            if attributes != INVALID_FILE_ATTRIBUTES {
                unsafe { SetFileAttributesW(path.as_ptr(), attributes & !FILE_ATTRIBUTE_READONLY) };
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
