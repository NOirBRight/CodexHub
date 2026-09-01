use super::super::{
    command_output_no_window, endpoints, gateway_models_from_config, parse_version_output,
    version_output_for_path,
};
use crate::{config, Provider, Settings};
use serde::Serialize;
use serde_json::Value;
use std::path::PathBuf;
use std::process::Command;

const DSH_PINNED_VERSION: &str = "0.1.0-rc.6";

#[derive(Debug, Clone, Serialize)]
pub struct DshClientInfo {
    pub client_id: String,
    pub installed: bool,
    pub executable_path: Option<PathBuf>,
    pub package_name: String,
    pub config_path: PathBuf,
    pub version: Option<String>,
    pub qualification: String,
    pub drift_details: Vec<String>,
    pub restart_required: String,
}

fn dsh_executable_path() -> Option<PathBuf> {
    std::env::var_os("CODEXHUB_DSH_EXECUTABLE")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .filter(|p| p.exists())
        .or_else(|| which::which("dsh").ok())
}

fn dsh_package_version() -> Option<String> {
    let mut command = Command::new("npm");
    command.args(["list", "-g", "@deepseek-ai/dsh", "--json", "--depth=0"]);
    let output = command_output_no_window(command)?;
    let value: Value = serde_json::from_slice(&output.stdout).ok()?;
    value
        .get("dependencies")?
        .get("@deepseek-ai/dsh")?
        .get("version")?
        .as_str()
        .map(ToOwned::to_owned)
}

fn dsh_version() -> Option<String> {
    dsh_executable_path()
        .and_then(|path| version_output_for_path(&path))
        .and_then(|output| {
            let text = if output.stdout.is_empty() {
                String::from_utf8_lossy(&output.stderr)
            } else {
                String::from_utf8_lossy(&output.stdout)
            };
            parse_version_output(&text)
        })
        .or_else(dsh_package_version)
}

pub fn detect_dsh_client() -> DshClientInfo {
    let descriptor = crate::injection::dsh_descriptor();
    let config_path = descriptor.config_file.resolve(
        &descriptor
            .client_home()
            .unwrap_or_else(|| PathBuf::from("~/.dsh")),
    );
    let executable_path = dsh_executable_path();
    let version = dsh_version();
    let installed = executable_path.is_some() || version.is_some() || config_path.exists();
    let mut drift_details = Vec::new();
    let qualification = match version.as_deref() {
        Some(DSH_PINNED_VERSION) => "qualified",
        Some(found) => {
            drift_details.push(format!(
                "installed DSH version {found} differs from pinned baseline {DSH_PINNED_VERSION}"
            ));
            "drifted"
        }
        None if installed => {
            drift_details.push(format!(
                "DSH version could not be determined; pinned baseline is {DSH_PINNED_VERSION}"
            ));
            "unqualified"
        }
        None => "unavailable",
    };
    DshClientInfo {
        client_id: "dsh".to_owned(),
        installed,
        executable_path,
        package_name: "@deepseek-ai/dsh".to_owned(),
        config_path,
        version,
        qualification: qualification.to_owned(),
        drift_details,
        restart_required: "none".to_owned(),
    }
}

fn dsh_adapter_ctx<'a>(
    settings: &'a Settings,
    providers: &'a [Provider],
) -> Result<super::super::managed_clients::AdapterCtx<'a>, String> {
    let root = crate::injection::dsh_descriptor()
        .client_home()
        .ok_or_else(|| "home directory could not be resolved".to_owned())?;
    let base_url = endpoints(settings.proxy_port).base_url;
    let models = gateway_models_from_config(settings, providers)
        .into_iter()
        .map(|m| m.id)
        .collect();
    Ok(super::super::managed_clients::AdapterCtx {
        settings,
        providers,
        base_url,
        models,
        target: super::super::managed_clients::AdapterTarget::Live {
            client_root: root,
            backup_roots: Vec::new(),
        },
    })
}

pub fn dsh_client_connect() -> Result<crate::injection::DshLifecycleReport, String> {
    // DM-4: DSH lifecycle goes through the managed-client coordinator seam.
    let settings = config::get_settings()?;
    let providers = config::get_providers()?;
    let ctx = dsh_adapter_ctx(&settings, &providers)?;
    super::super::managed_clients::dsh_connect_plan(&ctx)
}

pub fn dsh_client_disconnect() -> Result<crate::injection::DshLifecycleReport, String> {
    let settings = config::get_settings()?;
    let providers = config::get_providers()?;
    let ctx = dsh_adapter_ctx(&settings, &providers)?;
    super::super::managed_clients::dsh_disconnect_plan(&ctx)
}

pub fn dsh_client_readback() -> Result<crate::injection::DshLifecycleReport, String> {
    let settings = config::get_settings()?;
    let providers = config::get_providers()?;
    let ctx = dsh_adapter_ctx(&settings, &providers)?;
    super::super::managed_clients::dsh_readback_plan(&ctx)
}
