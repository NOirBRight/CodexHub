use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum BuildFlavor {
    Normal,
    Debug,
}

impl BuildFlavor {
    pub fn from_name(value: &str) -> Option<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "normal" => Some(Self::Normal),
            "debug" => Some(Self::Debug),
            _ => None,
        }
    }

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Normal => "normal",
            Self::Debug => "debug",
        }
    }

    pub const fn diagnostics_enabled(self) -> bool {
        matches!(self, Self::Debug)
    }

    pub const fn updater_manifest_name(self) -> &'static str {
        match self {
            Self::Normal => "latest.json",
            Self::Debug => "latest-debug.json",
        }
    }

    pub fn updater_endpoint(self) -> String {
        format!(
            "https://github.com/NOirBRight/CodexHub/releases/latest/download/{}",
            self.updater_manifest_name()
        )
    }

    pub fn installer_name(self, version: &str) -> String {
        let base = match self {
            Self::Normal => format!("CodexHub_{version}"),
            Self::Debug => format!("CodexHub_{version}_debug"),
        };
        // The updater consumes AppImage on Linux (the only Linux format
        // tauri-plugin-updater can self-update); deb packages ship without
        // self-update. Windows keeps the NSIS artifact name.
        #[cfg(windows)]
        {
            format!("{base}_x64-setup.exe")
        }
        #[cfg(target_os = "linux")]
        {
            format!("{base}_amd64.AppImage")
        }
        #[cfg(not(any(windows, target_os = "linux")))]
        {
            format!("{base}_x64-setup.exe")
        }
    }

    pub const fn accepts_legacy_flavorless_manifest(self) -> bool {
        matches!(self, Self::Normal)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct BuildInfo {
    pub semantic_version: &'static str,
    pub flavor: BuildFlavor,
    pub source_revision: &'static str,
    pub diagnostics_enabled: bool,
}

pub fn current() -> BuildInfo {
    let flavor = BuildFlavor::from_name(option_env!("CODEXHUB_BUILD_FLAVOR").unwrap_or("normal"))
        .expect("build.rs must provide a valid CodexHub build flavor");
    let diagnostics_enabled = cfg!(feature = "debug-diagnostics");
    assert_eq!(
        flavor.diagnostics_enabled(),
        diagnostics_enabled,
        "CodexHub build flavor and debug-diagnostics feature must agree"
    );

    BuildInfo {
        semantic_version: env!("CARGO_PKG_VERSION"),
        flavor,
        source_revision: option_env!("CODEXHUB_SOURCE_REVISION").unwrap_or("unknown"),
        diagnostics_enabled,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flavor_parser_accepts_only_supported_compile_time_values() {
        assert_eq!(BuildFlavor::from_name("normal"), Some(BuildFlavor::Normal));
        assert_eq!(BuildFlavor::from_name(" DEBUG "), Some(BuildFlavor::Debug));
        assert_eq!(BuildFlavor::from_name("stable"), None);
        assert_eq!(BuildFlavor::from_name("beta"), None);
    }

    #[test]
    fn artifact_and_manifest_identity_is_flavor_specific_without_changing_semver() {
        let version = "0.2.0";

        assert_eq!(BuildFlavor::Normal.updater_manifest_name(), "latest.json");
        assert_eq!(
            BuildFlavor::Debug.updater_manifest_name(),
            "latest-debug.json"
        );
        #[cfg(windows)]
        let (normal_artifact, debug_artifact) = (
            "CodexHub_0.2.0_x64-setup.exe",
            "CodexHub_0.2.0_debug_x64-setup.exe",
        );
        #[cfg(target_os = "linux")]
        let (normal_artifact, debug_artifact) = (
            "CodexHub_0.2.0_amd64.AppImage",
            "CodexHub_0.2.0_debug_amd64.AppImage",
        );
        #[cfg(not(any(windows, target_os = "linux")))]
        let (normal_artifact, debug_artifact) = (
            "CodexHub_0.2.0_x64-setup.exe",
            "CodexHub_0.2.0_debug_x64-setup.exe",
        );
        assert_eq!(BuildFlavor::Normal.installer_name(version), normal_artifact);
        assert_eq!(BuildFlavor::Debug.installer_name(version), debug_artifact);
    }

    #[test]
    fn current_build_info_reports_one_semver_and_matching_diagnostic_capability() {
        let info = current();

        assert_eq!(info.semantic_version, env!("CARGO_PKG_VERSION"));
        assert!(!info.source_revision.is_empty());
        assert_eq!(info.flavor.diagnostics_enabled(), info.diagnostics_enabled);
    }
}
