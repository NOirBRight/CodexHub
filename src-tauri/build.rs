use std::path::PathBuf;
use std::process::Command;

fn main() {
    println!("cargo:rerun-if-env-changed=CODEXHUB_BUILD_FLAVOR");
    println!("cargo:rerun-if-env-changed=CARGO_FEATURE_DEBUG_DIAGNOSTICS");
    println!("cargo:rerun-if-env-changed=GITHUB_SHA");
    emit_git_revision_rerun_paths();

    let flavor = std::env::var("CODEXHUB_BUILD_FLAVOR")
        .unwrap_or_else(|_| "normal".to_string())
        .trim()
        .to_ascii_lowercase();
    if !matches!(flavor.as_str(), "normal" | "debug") {
        panic!("CODEXHUB_BUILD_FLAVOR must be normal or debug, got {flavor:?}");
    }

    let diagnostics_enabled = std::env::var_os("CARGO_FEATURE_DEBUG_DIAGNOSTICS").is_some();
    if (flavor == "debug") != diagnostics_enabled {
        panic!(
            "CODEXHUB_BUILD_FLAVOR={flavor} must {} the debug-diagnostics Cargo feature",
            if flavor == "debug" {
                "enable"
            } else {
                "not enable"
            }
        );
    }

    println!("cargo:rustc-env=CODEXHUB_BUILD_FLAVOR={flavor}");
    println!(
        "cargo:rustc-env=CODEXHUB_SOURCE_REVISION={}",
        source_revision()
    );
    tauri_build::build()
}

fn emit_git_revision_rerun_paths() {
    if let Some(path) = git_path("HEAD") {
        println!("cargo:rerun-if-changed={}", path.display());
    }

    let reference = Command::new("git")
        .args(["symbolic-ref", "-q", "HEAD"])
        .current_dir("..")
        .output()
        .ok()
        .filter(|output| output.status.success())
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty());
    if let Some(reference) = reference.as_deref() {
        if let Some(path) = git_path(reference) {
            println!("cargo:rerun-if-changed={}", path.display());
        }
    }
}

fn git_path(reference: &str) -> Option<PathBuf> {
    let value = Command::new("git")
        .args(["rev-parse", "--git-path", reference])
        .current_dir("..")
        .output()
        .ok()
        .filter(|output| output.status.success())
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())?;
    let path = PathBuf::from(value);
    Some(if path.is_absolute() {
        path
    } else {
        PathBuf::from("..").join(path)
    })
}

fn source_revision() -> String {
    if let Some(revision) = std::env::var("GITHUB_SHA")
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
    {
        return revision;
    }

    Command::new("git")
        .args(["rev-parse", "HEAD"])
        .current_dir("..")
        .output()
        .ok()
        .filter(|output| output.status.success())
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "unknown".to_string())
}
