use serde::Serialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
#[allow(dead_code)]
pub enum RoutingOwner {
    Official,
    Release,
    Beta,
    UnknownExternal,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RuntimeFlavor {
    Stable,
    Beta,
}

#[derive(Debug, Clone, Serialize)]
pub struct AppFlavorInfo {
    pub flavor: RuntimeFlavor,
    pub routing_owner: RoutingOwner,
    pub product_name: &'static str,
    pub bridge_port: u16,
    pub gateway_port: u16,
    pub default_codex_home_suffix: &'static str,
    pub runtime_home_suffix: &'static str,
    pub codex_target_home_suffix: &'static str,
    pub codex_target_owner: Option<RoutingOwner>,
    pub codex_takeover_required: bool,
}

pub fn current() -> RuntimeFlavor {
    RuntimeFlavor::from_name(option_env!("CODEXHUB_BUILD_FLAVOR").unwrap_or("stable"))
}

pub fn current_info() -> AppFlavorInfo {
    current().info()
}

pub fn default_gateway_port() -> u16 {
    current().gateway_port()
}

pub fn bridge_addr() -> String {
    format!("127.0.0.1:{}", current().bridge_port())
}

impl RuntimeFlavor {
    pub fn from_name(value: &str) -> Self {
        match value.trim().to_ascii_lowercase().as_str() {
            "beta" => Self::Beta,
            _ => Self::Stable,
        }
    }

    pub fn info(self) -> AppFlavorInfo {
        let codex_target_owner = crate::runtime_paths::codex_target_home_dir()
            .ok()
            .and_then(|home| std::fs::read_to_string(home.join("config.toml")).ok())
            .as_deref()
            .and_then(crate::config::codex_overlay_owner);
        AppFlavorInfo {
            flavor: self,
            routing_owner: self.routing_owner(),
            product_name: self.product_name(),
            bridge_port: self.bridge_port(),
            gateway_port: self.gateway_port(),
            default_codex_home_suffix: self.default_codex_home_suffix(),
            runtime_home_suffix: self.runtime_home_suffix(),
            codex_target_home_suffix: self.codex_target_home_suffix(),
            codex_target_owner,
            codex_takeover_required: self.codex_takeover_required(codex_target_owner),
        }
    }

    pub fn routing_owner(self) -> RoutingOwner {
        match self {
            Self::Stable => RoutingOwner::Release,
            Self::Beta => RoutingOwner::Beta,
        }
    }

    pub fn product_name(self) -> &'static str {
        match self {
            Self::Stable => "CodexHub",
            Self::Beta => "CodexHub Beta",
        }
    }

    pub fn bridge_port(self) -> u16 {
        match self {
            Self::Stable => 1421,
            Self::Beta => 1431,
        }
    }

    pub fn gateway_port(self) -> u16 {
        match self {
            Self::Stable => 9099,
            Self::Beta => 9109,
        }
    }

    pub fn default_codex_home_suffix(self) -> &'static str {
        self.runtime_home_suffix()
    }

    pub fn runtime_home_suffix(self) -> &'static str {
        match self {
            Self::Stable => ".codex",
            Self::Beta => ".codexhub-beta",
        }
    }

    pub fn codex_target_home_suffix(self) -> &'static str {
        ".codex"
    }

    pub fn codex_takeover_required(self, target_owner: Option<RoutingOwner>) -> bool {
        match self {
            Self::Beta => target_owner != Some(RoutingOwner::Beta),
            Self::Stable => target_owner.is_some_and(|owner| {
                owner != RoutingOwner::Official && owner != RoutingOwner::Release
            }),
        }
    }

    pub fn autostart_task_name(self) -> &'static str {
        match self {
            Self::Stable => "CodexHubProxy",
            Self::Beta => "CodexHubBetaProxy",
        }
    }

    pub fn macos_label(self) -> &'static str {
        match self {
            Self::Stable => "com.codexhub.proxy",
            Self::Beta => "com.codexhub.beta.proxy",
        }
    }

    pub fn macos_plist_file(self) -> &'static str {
        match self {
            Self::Stable => "com.codexhub.proxy.plist",
            Self::Beta => "com.codexhub.beta.proxy.plist",
        }
    }

    pub fn linux_service_file(self) -> &'static str {
        match self {
            Self::Stable => "codexhub-proxy.service",
            Self::Beta => "codexhub-beta-proxy.service",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stable_defaults_match_existing_ports_and_identity() {
        let flavor = RuntimeFlavor::Stable;
        assert_eq!(flavor.routing_owner(), RoutingOwner::Release);
        assert_eq!(flavor.product_name(), "CodexHub");
        assert_eq!(flavor.bridge_port(), 1421);
        assert_eq!(flavor.gateway_port(), 9099);
        assert_eq!(flavor.autostart_task_name(), "CodexHubProxy");
    }

    #[test]
    fn beta_defaults_are_isolated_from_stable() {
        let flavor = RuntimeFlavor::Beta;
        assert_eq!(flavor.routing_owner(), RoutingOwner::Beta);
        assert_eq!(flavor.product_name(), "CodexHub Beta");
        assert_eq!(flavor.bridge_port(), 1431);
        assert_eq!(flavor.gateway_port(), 9109);
        assert_eq!(flavor.autostart_task_name(), "CodexHubBetaProxy");
        assert_ne!(
            flavor.default_codex_home_suffix(),
            RuntimeFlavor::Stable.default_codex_home_suffix()
        );
    }

    #[test]
    fn beta_runtime_home_is_separate_but_codex_target_stays_real() {
        let flavor = RuntimeFlavor::Beta;

        assert_eq!(flavor.runtime_home_suffix(), ".codexhub-beta");
        assert_eq!(flavor.codex_target_home_suffix(), ".codex");
        assert_ne!(flavor.runtime_home_suffix(), flavor.codex_target_home_suffix());
    }

    #[test]
    fn beta_frontend_takeover_state_includes_unowned_and_official_targets() {
        assert!(RuntimeFlavor::Beta.codex_takeover_required(None));
        assert!(RuntimeFlavor::Beta.codex_takeover_required(Some(RoutingOwner::Official)));
        assert!(!RuntimeFlavor::Beta.codex_takeover_required(Some(RoutingOwner::Beta)));
        assert!(!RuntimeFlavor::Stable.codex_takeover_required(None));
        assert!(!RuntimeFlavor::Stable.codex_takeover_required(Some(RoutingOwner::Official)));
    }
}
