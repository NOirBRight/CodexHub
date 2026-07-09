use serde::Serialize;
use std::path::PathBuf;

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
        AppFlavorInfo {
            flavor: self,
            routing_owner: self.routing_owner(),
            product_name: self.product_name(),
            bridge_port: self.bridge_port(),
            gateway_port: self.gateway_port(),
            default_codex_home_suffix: self.default_codex_home_suffix(),
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
        match self {
            Self::Stable => ".codex",
            Self::Beta => ".codexhub-beta/codex-home",
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

pub fn default_codex_home_dir() -> Result<PathBuf, String> {
    dirs::home_dir()
        .ok_or_else(|| "failed to resolve user home directory".to_string())
        .map(|home| {
            current()
                .default_codex_home_suffix()
                .split('/')
                .fold(home, |path, segment| path.join(segment))
        })
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
}
