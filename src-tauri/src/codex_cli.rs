//! Shared CLI discovery for desktop processes that do not inherit a login shell.
use std::path::{Path, PathBuf};
use std::process::Command;

pub(crate) fn command() -> Result<Command, String> {
    let explicit = std::env::var_os("CODEXHUB_CODEX_PATH").filter(|value| !value.is_empty());
    let executable = if let Some(path) = explicit {
        PathBuf::from(path)
    } else {
        find_executable().ok_or_else(|| {
            "Codex CLI was not found on PATH or in common installation directories. Install Codex CLI or set CODEXHUB_CODEX_PATH to its executable.".to_string()
        })?
    };
    command_for(&executable)
}

fn find_executable() -> Option<PathBuf> {
    #[cfg(windows)]
    {
        if let Some(path) =
            std::env::var_os("LOCALAPPDATA").and_then(|root| desktop_executable(Path::new(&root)))
        {
            return Some(path);
        }
        if let Some(root) = std::env::var_os("APPDATA") {
            let vendor = PathBuf::from(root).join("npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc");
            for subdir in ["bin", "codex"] {
                let path = vendor.join(subdir).join("codex.exe");
                if path.is_file() {
                    return Some(path);
                }
            }
        }
    }
    for candidate in crate::runtime_paths::codex_executable_candidates() {
        if let Ok(path) = which::which(candidate) {
            return Some(path);
        }
    }
    #[cfg(unix)]
    {
        let home = dirs::home_dir()?;
        let nvm = std::env::var_os("NVM_DIR")
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join(".nvm"));
        unix_fallback(&home, &nvm)
    }
    #[cfg(not(unix))]
    None
}

fn command_for(executable: &Path) -> Result<Command, String> {
    let cwd = std::env::current_dir()
        .map_err(|error| format!("Cannot resolve Codex CLI directory: {error}"))?;
    command_for_environment(executable, std::env::var_os("PATH"), &cwd)
}

fn command_for_environment(
    executable: &Path,
    search_path: Option<std::ffi::OsString>,
    cwd: &Path,
) -> Result<Command, String> {
    // Resolve bare names against the original PATH before adding a Node bin.
    // Never interpret an unresolved name as a path inside the working project.
    let executable = which::which_in(executable, search_path.as_ref(), cwd)
        .map_err(|error| format!("Cannot resolve Codex CLI executable: {error}"))?;
    let command = Command::new(&executable);
    #[cfg(unix)]
    {
        let mut command = command;
        // npm's Codex launcher uses /usr/bin/env node. A GUI can miss both
        // Codex and the matching Node runtime, even when the launcher exists.
        // Resolve symlinks too (e.g. ~/.local/bin/codex -> an NVM installation).
        let resolved = std::fs::canonicalize(&executable)
            .map_err(|error| format!("Cannot resolve Codex CLI installation: {error}"))?;
        let node_bin = resolved.ancestors().find_map(|root| {
            let bin = root.join("bin");
            executable_file(&bin.join("node")).then_some(bin)
        });
        if let Some(bin) = node_bin {
            let mut paths = vec![bin];
            if let Some(path) = search_path {
                paths.extend(std::env::split_paths(&path));
            }
            if let Ok(path) = std::env::join_paths(paths) {
                command.env("PATH", path);
            }
        }
        Ok(command)
    }
    #[cfg(not(unix))]
    Ok(command)
}

#[cfg(unix)]
fn executable_file(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path)
        .is_ok_and(|metadata| metadata.is_file() && metadata.permissions().mode() & 0o111 != 0)
}

#[cfg(unix)]
fn unix_fallback(home: &Path, nvm: &Path) -> Option<PathBuf> {
    for bin in [
        ".local/bin",
        ".npm-global/bin",
        ".volta/bin",
        ".bun/bin",
        "bin",
    ] {
        let path = home.join(bin).join("codex");
        if executable_file(&path) {
            return Some(path);
        }
    }
    let mut versions = std::fs::read_dir(nvm.join("versions/node"))
        .ok()
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let name = entry.file_name();
            let version = name.to_str()?.strip_prefix('v')?;
            let parts = version
                .split('.')
                .map(str::parse::<u64>)
                .collect::<Result<Vec<_>, _>>()
                .ok()?;
            (parts.len() == 3).then_some((parts, entry.path().join("bin/codex")))
        })
        .collect::<Vec<_>>();
    // PATH remains authoritative. If the desktop has no selected version,
    // choose the newest installed numeric Node version that contains Codex.
    versions.sort_by(|left, right| right.0.cmp(&left.0));
    versions
        .into_iter()
        .map(|(_, path)| path)
        .find(|path| executable_file(path))
}

#[cfg(any(windows, test))]
fn desktop_executable(local_appdata: &Path) -> Option<PathBuf> {
    let mut candidates = std::fs::read_dir(local_appdata.join("OpenAI/Codex/bin"))
        .ok()?
        .filter_map(Result::ok)
        .map(|entry| entry.path().join("codex.exe"))
        .filter(|path| path.is_file())
        .collect::<Vec<_>>();
    candidates.sort_by_key(|path| {
        std::fs::metadata(path)
            .and_then(|metadata| metadata.modified())
            .unwrap_or(std::time::UNIX_EPOCH)
    });
    candidates.pop()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_root() -> PathBuf {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!("codexhub-cli-discovery-{nonce}"))
    }

    #[test]
    fn desktop_runtime_is_found() {
        let root = temp_root();
        let path = root.join("OpenAI/Codex/bin/runtime-hash/codex.exe");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, b"codex").unwrap();
        assert_eq!(desktop_executable(&root), Some(path));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    fn executable(path: &Path, content: &str) {
        use std::os::unix::fs::PermissionsExt;
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, content).unwrap();
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700)).unwrap();
    }

    #[test]
    #[cfg(unix)]
    fn gui_fallback_finds_nvm_and_runs_with_its_node_without_global_path_changes() {
        let root = temp_root();
        let nvm = root.join("custom-nvm");
        let install = nvm.join("versions/node/v22.10.0");
        let launcher = install.join("lib/node_modules/@openai/codex/bin/codex.js");
        executable(&launcher, "#!/usr/bin/env node\n");
        executable(
            &install.join("bin/node"),
            "#!/bin/sh\nprintf 'matching-node'\n",
        );
        std::os::unix::fs::symlink(&launcher, install.join("bin/codex")).unwrap();
        executable(
            &nvm.join("versions/node/v22.9.0/bin/codex"),
            "#!/bin/sh\nexit 1\n",
        );
        std::fs::create_dir_all(nvm.join("versions/node/v24.0.0/bin")).unwrap();
        let before = std::env::var_os("PATH");
        let found = unix_fallback(&root, &nvm).expect("NVM installation missing from GUI PATH");
        assert_eq!(found, install.join("bin/codex"));
        let output = command_for(&found).unwrap().output().unwrap();
        assert!(output.status.success());
        assert_eq!(output.stdout, b"matching-node");
        assert_eq!(std::env::var_os("PATH"), before);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    #[cfg(unix)]
    fn bare_override_uses_original_path_not_project_node() {
        let root = temp_root();
        let trusted = root.join("trusted/bin");
        let project = root.join("project");
        executable(&trusted.join("codex"), "#!/usr/bin/env node\n");
        executable(&trusted.join("node"), "#!/bin/sh\nprintf trusted-node\n");
        executable(
            &project.join("bin/node"),
            "#!/bin/sh\nprintf project-node\n",
        );
        let path = Some(trusted.as_os_str().to_os_string());
        let mut command = command_for_environment(Path::new("codex"), path, &project).unwrap();
        assert_eq!(command.get_program(), trusted.join("codex"));
        let output = command.current_dir(&project).output().unwrap();
        assert!(output.status.success());
        assert_eq!(output.stdout, b"trusted-node");
        assert!(command_for_environment(Path::new("missing-codex"), None, &project).is_err());
        let mut relative =
            command_for_environment(Path::new("../trusted/bin/codex"), None, &project).unwrap();
        assert_eq!(
            relative.current_dir(&project).output().unwrap().stdout,
            b"trusted-node"
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    #[cfg(unix)]
    fn fallback_skips_broken_and_non_executable_candidates() {
        let root = temp_root();
        let nvm = root.join(".nvm");
        let local = root.join(".local/bin/codex");
        std::fs::create_dir_all(local.parent().unwrap()).unwrap();
        std::os::unix::fs::symlink(root.join("missing"), &local).unwrap();
        let path = nvm.join("versions/node/v22.0.0/bin/codex");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, b"not executable").unwrap();
        assert_eq!(unix_fallback(&root, &nvm), None);
        executable(&root.join(".npm-global/bin/codex"), "#!/bin/sh\n");
        assert_eq!(
            unix_fallback(&root, &nvm),
            Some(root.join(".npm-global/bin/codex"))
        );
        std::fs::remove_dir_all(root).unwrap();
    }
}
