use std::path::{Path, PathBuf};

pub(crate) fn set_resource_root(path: impl AsRef<Path>) {
    let path = path.as_ref();
    if path.exists() {
        std::env::set_var("CODEXHUB_RESOURCE_ROOT", path);
    }
}

pub(crate) fn codex_home_dir() -> Result<PathBuf, String> {
    match std::env::var_os("CODEX_HOME").filter(|value| !value.is_empty()) {
        Some(value) => Ok(PathBuf::from(value)),
        None => dirs::home_dir()
            .ok_or_else(|| "failed to resolve user home directory".to_string())
            .map(|home| home.join(".codex")),
    }
}

pub(crate) fn resource_root() -> Result<PathBuf, String> {
    for candidate in resource_root_candidates() {
        if is_codexhub_resource_root(&candidate) {
            return Ok(candidate);
        }
    }

    Err("failed to locate CodexHub runtime resources".to_string())
}

fn resource_root_candidates() -> Vec<PathBuf> {
    let mut candidates = Vec::new();

    if let Some(value) =
        std::env::var_os("CODEXHUB_RESOURCE_ROOT").filter(|value| !value.is_empty())
    {
        candidates.push(PathBuf::from(value));
    }

    if let Ok(exe) = std::env::current_exe() {
        if let Some(exe_dir) = exe.parent() {
            candidates.push(exe_dir.join("resources"));
            candidates.push(exe_dir.to_path_buf());
        }
    }

    if let Some(repo_root) = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(Path::to_path_buf)
    {
        candidates.push(repo_root);
    }

    dedupe_paths(candidates)
}

fn is_codexhub_resource_root(path: &Path) -> bool {
    path.join("src-python").join("codex_proxy.py").exists()
        && path.join("config").join("providers.toml").exists()
}

fn dedupe_paths(paths: Vec<PathBuf>) -> Vec<PathBuf> {
    let mut result = Vec::new();
    for path in paths {
        if !result.iter().any(|existing: &PathBuf| existing == &path) {
            result.push(path);
        }
    }
    result
}
