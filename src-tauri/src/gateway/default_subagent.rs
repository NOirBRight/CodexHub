use super::{
    codexhub_client_provider_id, gateway_client_provider_groups, split_gateway_model_id,
    GatewayClientProviderGroup, GatewayClientProviderModel,
};
use crate::{Provider, Settings};
use serde_json::{Map, Value};

pub(in crate::gateway) const DEFAULT_SUBAGENT_EFFORTS: &[&str] =
    &["none", "minimal", "low", "medium", "high", "xhigh", "max"];

pub(in crate::gateway) const OPENCODE_SPAWN_AGENTS: &[&str] = &["general", "explore", "scout"];
pub(in crate::gateway) const OPENCODE_PARENT_AGENTS: &[&str] = &["build", "plan"];
pub(in crate::gateway) const OMP_BUNDLED_AGENTS: &[&str] = &[
    "task",
    "scout",
    "reviewer",
    "security-reviewer",
    "librarian",
    "designer",
    "sonic",
];
pub(in crate::gateway) const GROK_SPAWN_TYPES: &[&str] = &["general-purpose", "explore", "plan"];
pub(in crate::gateway) const ZCODE_SPAWN_AGENTS: &[(&str, &str)] =
    &[("general-purpose", "general-purpose.md"), ("Explore", "Explore.md")];

#[derive(Debug, Clone, PartialEq, Eq)]
pub(in crate::gateway) struct ClientDefaultSubagentPin {
    pub catalog_slug: String,
    pub effort: String,
    pub client_provider_id: String,
    pub short_id: String,
}

impl ClientDefaultSubagentPin {
    pub(in crate::gateway) fn opencode_model_id(&self) -> String {
        format!(
            "{}/{}#{}",
            self.client_provider_id, self.short_id, self.effort
        )
    }

    pub(in crate::gateway) fn omp_selector(&self) -> String {
        format!(
            "{}/{}:{}",
            self.client_provider_id, self.short_id, self.effort
        )
    }

    pub(in crate::gateway) fn grok_picker_key(&self) -> String {
        format!(
            "{}-{}",
            self.client_provider_id,
            self.short_id.replace(['/', '\\'], "-")
        )
    }

    pub(in crate::gateway) fn zcode_model_id(&self) -> String {
        format!("{}/{}", self.client_provider_id, self.short_id)
    }

    pub(in crate::gateway) fn zcode_thought_level(&self) -> String {
        if self.effort == "none" {
            "off".to_string()
        } else {
            self.effort.clone()
        }
    }
}

pub(in crate::gateway) fn client_default_subagent_fields(
    settings: &Settings,
    client_id: &str,
) -> (String, String) {
    match client_id {
        "opencode" => (
            settings.opencode_default_subagent_model.clone(),
            settings.opencode_default_subagent_reasoning_effort.clone(),
        ),
        "zcode" => (
            settings.zcode_default_subagent_model.clone(),
            settings.zcode_default_subagent_reasoning_effort.clone(),
        ),
        "omp" => (
            settings.omp_default_subagent_model.clone(),
            settings.omp_default_subagent_reasoning_effort.clone(),
        ),
        "grok" => (
            settings.grok_default_subagent_model.clone(),
            settings.grok_default_subagent_reasoning_effort.clone(),
        ),
        _ => (String::new(), String::new()),
    }
}

pub(in crate::gateway) fn resolve_client_default_subagent_pin(
    settings: &Settings,
    providers: &[Provider],
    client_id: &str,
    default_model: &str,
) -> Result<Option<ClientDefaultSubagentPin>, String> {
    let (model, effort) = client_default_subagent_fields(settings, client_id);
    let model = model.trim();
    if model.is_empty() {
        return Ok(None);
    }
    let effort = effort.trim().to_ascii_lowercase();
    if effort.is_empty() || !DEFAULT_SUBAGENT_EFFORTS.contains(&effort.as_str()) {
        return Err(format!(
            "unsupported default subagent reasoning effort: {effort}"
        ));
    }
    let groups = pin_provider_groups(settings, providers, client_id, default_model)?;
    let Some((group, gateway_model)) = find_catalog_model(&groups, model) else {
        return Ok(None);
    };
    Ok(Some(ClientDefaultSubagentPin {
        catalog_slug: model.to_string(),
        effort,
        client_provider_id: group.client_provider_id.clone(),
        short_id: gateway_model.id.clone(),
    }))
}

fn pin_provider_groups(
    settings: &Settings,
    providers: &[Provider],
    client_id: &str,
    default_model: &str,
) -> Result<Vec<GatewayClientProviderGroup>, String> {
    let groups = gateway_client_provider_groups(settings, providers, default_model)?;
    Ok(groups
        .providers
        .into_iter()
        .filter(|group| client_id != "grok" || !grok_skips_group(group, providers))
        .collect())
}

fn grok_skips_group(group: &GatewayClientProviderGroup, providers: &[Provider]) -> bool {
    providers.iter().any(|provider| {
        super::codexhub_client_provider_id(&provider.id) == group.client_provider_id
            && (provider.id.eq_ignore_ascii_case("xai")
                || provider.auth_capabilities.as_ref().is_some_and(|caps| {
                    caps.iter().any(|capability| capability == "subscription:xai_oauth")
                }))
    })
}

fn find_catalog_model<'a>(
    groups: &'a [GatewayClientProviderGroup],
    slug: &str,
) -> Option<(&'a GatewayClientProviderGroup, &'a GatewayClientProviderModel)> {
    let (slug_provider, slug_short) = split_gateway_model_id(slug);
    groups.iter().find_map(|group| {
        group.models.iter().find_map(|model| {
            let expected_group = codexhub_client_provider_id(&slug_provider);
            let matches = model.id == slug
                || format!("{}/{}", group.client_provider_id, model.id) == slug
                || (group.client_provider_id == expected_group && model.id == slug_short);
            matches.then_some((group, model))
        })
    })
}

pub(in crate::gateway) fn rollback_file_text(
    client_id: &str,
    file_key: &str,
    current: Option<&str>,
) -> Option<String> {
    match super::read_rollback_baseline(client_id) {
        Ok(Some(baseline)) => match baseline.files.get(file_key) {
            Some(super::BaselineFile::Snapshot { content }) => Some(content.clone()),
            Some(super::BaselineFile::Absent) => Some(String::new()),
            None => current.map(ToOwned::to_owned),
        },
        _ => current.map(ToOwned::to_owned),
    }
}

pub(in crate::gateway) fn restore_json_agent_models(
    live: &mut Map<String, Value>,
    baseline: Option<&Value>,
    agent_names: &[&str],
) {
    let baseline_agent = baseline.and_then(|value| value.get("agent")).and_then(Value::as_object);
    let live_agent = match live.get_mut("agent") {
        Some(Value::Object(object)) => object,
        _ => {
            if baseline_agent.is_none() {
                return;
            }
            live.insert("agent".to_string(), Value::Object(Map::new()));
            live.get_mut("agent")
                .and_then(Value::as_object_mut)
                .expect("agent object")
        }
    };
    for name in agent_names {
        match baseline_agent.and_then(|object| object.get(*name)) {
            Some(value) => {
                live_agent.insert((*name).to_string(), value.clone());
            }
            None => {
                live_agent.remove(*name);
            }
        }
    }
    if live_agent.is_empty() {
        live.remove("agent");
    }
}

pub(in crate::gateway) fn pin_json_agent_models(
    live: &mut Map<String, Value>,
    agent_names: &[&str],
    model_id: &str,
) {
    if !matches!(live.get("agent"), Some(Value::Object(_))) {
        live.insert("agent".to_string(), Value::Object(Map::new()));
    }
    let live_agent = live
        .get_mut("agent")
        .and_then(Value::as_object_mut)
        .expect("agent object");
    for name in agent_names {
        let entry = live_agent
            .entry((*name).to_string())
            .or_insert_with(|| Value::Object(Map::new()));
        if let Some(object) = entry.as_object_mut() {
            object.insert("model".to_string(), Value::String(model_id.to_string()));
        }
    }
}
