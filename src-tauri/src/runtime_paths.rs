use std::path::{Path, PathBuf};

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct RuntimeHomes {
    pub(crate) runtime: PathBuf,
    pub(crate) codex_target: PathBuf,
}

pub(crate) fn set_resource_root(path: impl AsRef<Path>) {
    let path = path.as_ref();
    if path.exists() {
        std::env::set_var("CODEXHUB_RESOURCE_ROOT", path);
    }
}

pub(crate) fn codex_home_dir() -> Result<PathBuf, String> {
    runtime_home_dir()
}

pub(crate) fn runtime_home_dir() -> Result<PathBuf, String> {
    match std::env::var_os("CODEXHUB_RUNTIME_HOME").filter(|value| !value.is_empty()) {
        Some(value) => Ok(PathBuf::from(value)),
        None if crate::app_flavor::current() == crate::app_flavor::RuntimeFlavor::Stable => {
            match std::env::var_os("CODEX_HOME").filter(|value| !value.is_empty()) {
                Some(value) => Ok(PathBuf::from(value)),
                None => dirs::home_dir()
                    .ok_or_else(|| "failed to resolve user home directory".to_string())
                    .map(|home| homes_for_flavor(&home, crate::app_flavor::current()).runtime),
            }
        }
        None => dirs::home_dir()
            .ok_or_else(|| "failed to resolve user home directory".to_string())
            .map(|home| homes_for_flavor(&home, crate::app_flavor::current()).runtime),
    }
}

pub(crate) fn codex_target_home_dir() -> Result<PathBuf, String> {
    match std::env::var_os("CODEX_HOME").filter(|value| !value.is_empty()) {
        Some(value) => Ok(PathBuf::from(value)),
        None => dirs::home_dir()
            .ok_or_else(|| "failed to resolve user home directory".to_string())
            .map(|home| homes_for_flavor(&home, crate::app_flavor::current()).codex_target),
    }
}

pub(crate) fn homes_for_flavor(
    user_home: &Path,
    flavor: crate::app_flavor::RuntimeFlavor,
) -> RuntimeHomes {
    RuntimeHomes {
        runtime: user_home.join(flavor.runtime_home_suffix()),
        codex_target: user_home.join(flavor.codex_target_home_suffix()),
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

pub(crate) fn find_python(resource_root: Option<&Path>) -> PathBuf {
    for candidate in python_candidates(resource_root) {
        if candidate.exists() {
            return candidate;
        }
    }

    which::which("python")
        .or_else(|_| which::which("python3"))
        .unwrap_or_else(|_| PathBuf::from("python"))
}

pub(crate) fn python_env_candidates() -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    for name in ["CODEXHUB_PYTHON", "CODEXHUB_PROXY_PYTHON"] {
        if let Some(value) = std::env::var_os(name).filter(|value| !value.is_empty()) {
            candidates.push(PathBuf::from(value));
        }
    }
    candidates
}

pub(crate) fn bundled_python_candidates(resource_root: &Path) -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    #[cfg(windows)]
    {
        candidates.push(resource_root.join("python").join("python.exe"));
    }
    #[cfg(not(windows))]
    {
        candidates.push(resource_root.join("python").join("bin").join("python"));
        candidates.push(resource_root.join("python").join("python"));
    }
    candidates
}

pub(crate) fn current_exe_python_candidates() -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            #[cfg(windows)]
            {
                candidates.push(dir.join("python").join("python.exe"));
                candidates.push(dir.join("python.exe"));
                candidates.push(dir.join("python3.exe"));
                candidates.push(dir.join("codexhub-python.exe"));
            }
            #[cfg(not(windows))]
            {
                candidates.push(dir.join("python").join("bin").join("python"));
                candidates.push(dir.join("python"));
                candidates.push(dir.join("python3"));
                candidates.push(dir.join("codexhub-python"));
            }
        }
    }
    candidates
}

fn python_candidates(resource_root: Option<&Path>) -> Vec<PathBuf> {
    let mut candidates = python_env_candidates();
    if let Some(root) = resource_root {
        candidates.extend(bundled_python_candidates(root));
    }
    candidates.extend(current_exe_python_candidates());
    dedupe_paths(candidates)
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

#[cfg(test)]
mod tests {
    use super::{bundled_python_candidates, find_python, homes_for_flavor};
    use crate::app_flavor::RuntimeFlavor;
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn bundled_python_candidates_include_resource_root_runtime() {
        let root = PathBuf::from("C:\\CodexHub");
        let candidates = bundled_python_candidates(&root);

        #[cfg(windows)]
        assert!(candidates.contains(&root.join("python").join("python.exe")));
        #[cfg(not(windows))]
        assert!(candidates.contains(&root.join("python").join("bin").join("python")));
    }

    #[test]
    fn find_python_prefers_bundled_runtime_when_present() {
        let root = temp_root("bundled-python");
        let python = bundled_python_path(&root);
        fs::create_dir_all(python.parent().unwrap()).unwrap();
        fs::write(&python, "").unwrap();

        assert_eq!(find_python(Some(&root)), python);
    }

    #[test]
    fn beta_homes_keep_runtime_data_away_from_real_codex_target() {
        let user_home = PathBuf::from("C:\\Users\\tester");

        let homes = homes_for_flavor(&user_home, RuntimeFlavor::Beta);

        assert_eq!(homes.runtime, user_home.join(".codexhub-beta"));
        assert_eq!(homes.codex_target, user_home.join(".codex"));
    }

    fn bundled_python_path(root: &Path) -> PathBuf {
        #[cfg(windows)]
        {
            root.join("python").join("python.exe")
        }
        #[cfg(not(windows))]
        {
            root.join("python").join("bin").join("python")
        }
    }

    fn temp_root(name: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!("codexhub-runtime-paths-{name}-{nonce}"))
    }
}
