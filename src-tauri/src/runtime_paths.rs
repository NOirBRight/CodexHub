use std::path::{Path, PathBuf};
use std::process::Command;

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

/// Candidate names shared by Codex CLI discovery call sites, in precedence order.
pub(crate) fn codex_executable_candidates() -> &'static [&'static str] {
    &["codex.cmd", "codex", "codex.exe"]
}

/// Remove host-environment selectors before starting a Python child.
///
/// An already selected Python executable can still be redirected to another
/// installation by `PYTHONHOME` (or by an activated environment's selectors).
/// Keep this at the process boundary so Gateway, catalog, config, history, and
/// model-probe children all receive the same runtime contract.
pub(crate) fn configure_python_command(command: &mut Command) {
    for name in [
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "CONDA_DEFAULT_ENV",
        "CONDA_PROMPT_MODIFIER",
        "PIPENV_ACTIVE",
    ] {
        command.env_remove(name);
    }
    configure_no_window(command);
}

pub(crate) fn configure_no_window(command: &mut Command) {
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    #[cfg(not(target_os = "windows"))]
    {
        let _ = command;
    }
}

/// Construct a Python child command with the repository runtime boundary
/// applied before callers add arguments or test-specific environment.
pub(crate) fn configured_python_command(python: &Path) -> Command {
    let mut command = Command::new(python);
    configure_python_command(&mut command);
    command
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

/// Version-independent directory for managed-client rollback provenance.
///
/// This store is intentionally outside per-channel runtime homes so that a
/// baseline recorded by one app version/flavor can be restored by another.
/// `CODEXHUB_ROLLBACK_PROVENANCE_DIR` overrides the location for tests.
pub(crate) fn client_rollback_provenance_dir() -> Result<PathBuf, String> {
    match std::env::var_os("CODEXHUB_ROLLBACK_PROVENANCE_DIR").filter(|value| !value.is_empty()) {
        Some(value) => Ok(PathBuf::from(value)),
        None => dirs::home_dir()
            .ok_or_else(|| "failed to resolve user home directory".to_string())
            .map(|home| home.join(".codexhub-rollback-provenance")),
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

/// Resolve the one Python interpreter that CodexHub is allowed to execute.
///
/// The repository uses newer Python syntax and the product runtime contract is
/// Python 3.13+, so accepting the first executable named `python` is not safe.
/// Explicit environment overrides are a hard
/// choice rather than a hint: if one is present but incompatible, return an
/// error instead of silently falling back to another interpreter.  This is
/// what keeps a stale 3.11 virtualenv from being selected by one child
/// process while the parent is running under 3.13.
pub(crate) fn find_python(resource_root: Option<&Path>) -> Result<PathBuf, String> {
    if let Some(explicit) = python_env_candidates().into_iter().next() {
        return compatible_python_path(&explicit).ok_or_else(|| {
            format!(
                "configured CodexHub Python interpreter is missing or incompatible (requires Python 3.13+): {}",
                explicit.display()
            )
        });
    }

    for candidate in python_candidates(resource_root) {
        if let Some(path) = compatible_python_path(&candidate) {
            return Ok(path);
        }
    }

    // Only probe host launchers after packaged and repository runtimes have
    // failed.  Resolving `py.exe` starts another Python process and used to
    // happen eagerly on every Gateway/configuration operation.
    for candidate in host_python_candidates() {
        if let Some(path) = compatible_python_path(&candidate) {
            return Ok(path);
        }
    }

    let ambient = if cfg!(windows) {
        "python.exe"
    } else {
        "python"
    };
    Err(format!(
        "CodexHub requires Python 3.13 or newer, but no compatible interpreter was found. Install Python 3.13+ or set CODEXHUB_PYTHON to its executable (ambient command: {ambient})"
    ))
}

/// Return host interpreters that satisfy the repository's Python 3.13+
/// runtime contract. This is deliberately separate from the bundled-runtime search:
/// source tests often use a temporary resource root that contains no runtime,
/// and must not fall back to whichever `python` happens to be first on PATH.
pub(crate) fn host_python_candidates() -> Vec<PathBuf> {
    discover_host_python_candidates()
}

fn discover_host_python_candidates() -> Vec<PathBuf> {
    let mut candidates = Vec::new();

    #[cfg(windows)]
    {
        for launcher in ["py.exe", "py"] {
            if let Ok(launcher_path) = which::which(launcher) {
                if let Some(path) = resolve_python_launcher(&launcher_path) {
                    candidates.push(path);
                    break;
                }
            }
        }
    }

    if candidates.is_empty() {
        for command_name in ["python3.13", "python3.13.exe", "python3", "python"] {
            if let Ok(path) = which::which(command_name) {
                if supports_python_313(&path) {
                    candidates.push(path);
                }
            }
        }
    }

    dedupe_paths(candidates)
}

/// Resolve the interpreter selected by the Windows Python launcher to its
/// concrete executable so callers can spawn it without carrying `-3.13` as a
/// hidden extra argument.
#[cfg(windows)]
fn resolve_python_launcher(launcher: &Path) -> Option<PathBuf> {
    let mut command = Command::new(launcher);
    configure_python_command(&mut command);
    let output = command
        .args([
            "-3.13",
            "-c",
            "import sys; print(sys.executable); raise SystemExit(0 if sys.version_info >= (3, 13) else 1)",
        ])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let stdout = String::from_utf8(output.stdout).ok()?;
    let path = stdout.lines().last()?.trim();
    let path = PathBuf::from(path);
    path.is_file().then_some(path)
}

fn supports_python_313(path: &Path) -> bool {
    let mut command = Command::new(path);
    configure_python_command(&mut command);
    command
        .args([
            "-c",
            "import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)",
        ])
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

/// Resolve and validate one configured or discovered interpreter.
///
/// A bare `python` from PATH is intentionally probed instead of trusted. This
/// is the boundary that prevents an unrelated 3.11 virtualenv from becoming
/// the Gateway interpreter when the caller did not use the PowerShell entry
/// point.
pub(crate) fn compatible_python_path(candidate: &Path) -> Option<PathBuf> {
    let path = if candidate.is_file() {
        if candidate.is_absolute() {
            candidate.to_path_buf()
        } else {
            std::env::current_dir().ok()?.join(candidate)
        }
    } else if candidate.is_absolute() || candidate.components().count() > 1 {
        // Absolute and path-like candidates are already fully qualified. Do
        // not ask `which` to search PATH for a missing long path; that makes
        // every start attempt pay an unnecessary process/filesystem penalty.
        return None;
    } else {
        which::which(candidate).ok()?
    };
    supports_python_313(&path).then_some(path)
}

/// Resolve the interpreter used by Rust/Python process fixtures.
///
/// This keeps test-only subprocesses on the same repository contract even
/// when the current shell puts an incompatible Python virtualenv first.
#[cfg(test)]
pub(crate) fn find_test_python() -> PathBuf {
    if let Some(explicit) = python_env_candidates().into_iter().next() {
        return compatible_python_path(&explicit)
            .unwrap_or_else(|| PathBuf::from("__codexhub_python_resolution_failed__"));
    }
    let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(Path::to_path_buf);
    if let Some(root) = repo_root.as_deref() {
        if let Ok(path) = find_python(Some(root)) {
            return path;
        }
    }
    // Keep the test helper's historical PathBuf API, but make an unavailable
    // interpreter fail at spawn rather than ever falling back to ambient
    // `python` (which is commonly Python 3.11 on developer machines).
    PathBuf::from("__codexhub_python_resolution_failed__")
}

pub(crate) fn python_env_candidates() -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    // E2E launchers bind this variable for every isolated child. Keep it
    // ahead of the generic overrides so a nested Rust -> Python boundary
    // cannot drift back to a different host interpreter.
    for name in [
        "CODEXHUB_E2E_PYTHON",
        "CODEXHUB_PYTHON",
        "CODEXHUB_PROXY_PYTHON",
    ] {
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
    candidates.extend(repository_python_candidates());
    dedupe_paths(candidates)
}

fn repository_python_candidates() -> Vec<PathBuf> {
    let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(Path::to_path_buf);
    let Some(repo_root) = repo_root else {
        return Vec::new();
    };

    #[cfg(windows)]
    let relative_candidates = [
        "src-tauri\\resources\\python\\python.exe",
        ".venv-ci\\Scripts\\python.exe",
        ".venv\\Scripts\\python.exe",
    ];
    #[cfg(not(windows))]
    let relative_candidates = [
        "src-tauri/resources/python/bin/python",
        ".venv-ci/bin/python",
        ".venv/bin/python",
    ];

    relative_candidates
        .iter()
        .map(|relative| repo_root.join(relative))
        .collect()
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
    use super::{
        bundled_python_candidates, configure_python_command, configured_python_command,
        find_test_python, homes_for_flavor,
    };
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

        assert_eq!(super::python_candidates(Some(&root)).first(), Some(&python));
    }

    #[test]
    fn test_python_resolver_selects_a_compatible_interpreter() {
        let python = find_test_python();
        assert!(
            python.is_absolute(),
            "test Python resolver must return an absolute path: {}",
            python.display()
        );
        let status = configured_python_command(&python)
            .args([
                "-c",
                "import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)",
            ])
            .status()
            .expect("repository Python interpreter should start");
        assert!(
            status.success(),
            "test Python must be 3.13 or newer: {}",
            python.display()
        );
    }

    #[test]
    fn configure_python_command_removes_host_runtime_selectors() {
        let python = find_test_python();
        let mut command = configured_python_command(&python);
        command
            .env("PYTHONHOME", r"C:\hermes-3.11")
            .env("PYTHONSTARTUP", r"C:\hermes-3.11\startup.py")
            .env("PYTHONUSERBASE", r"C:\hermes-3.11\user")
            .env("VIRTUAL_ENV", r"C:\hermes-3.11")
            .env("CONDA_PREFIX", r"C:\hermes-3.11\conda")
            .env("CONDA_DEFAULT_ENV", "hermes")
            .env("CONDA_PROMPT_MODIFIER", "(hermes)")
            .env("PIPENV_ACTIVE", "1")
            .arg("-c")
            .arg(
            "import os, sys; names = ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP', 'PYTHONUSERBASE', 'VIRTUAL_ENV', 'CONDA_PREFIX', 'CONDA_DEFAULT_ENV', 'CONDA_PROMPT_MODIFIER', 'PIPENV_ACTIVE'); print(sys.version_info[:2]); print([os.environ.get(name) for name in names])",
            );
        configure_python_command(&mut command);

        let output = command.output().expect("configured Python should start");
        assert!(
            output.status.success(),
            "configured Python failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        let stdout = String::from_utf8_lossy(&output.stdout);
        assert!(stdout.contains("(3, 13)") || stdout.contains("(3, 14)"));
        assert!(stdout.contains("[None, None, None, None, None, None, None, None, None]"));
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
