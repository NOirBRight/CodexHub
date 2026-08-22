use crate::{config, models};
use serde_json::Value;

pub fn provider_probe_upstream_format(
    provider_id: String,
    model: Option<String>,
) -> Result<Value, String> {
    let providers = config::get_providers()?;
    let provider = providers
        .iter()
        .find(|candidate| candidate.id == provider_id)
        .ok_or_else(|| format!("provider not found: {provider_id}"))?;
    let probe_model = model.or_else(|| {
        provider
            .models
            .iter()
            .find(|item| item.enabled)
            .or_else(|| provider.models.first())
            .map(|item| {
                item.upstream_model
                    .clone()
                    .unwrap_or_else(|| item.id.clone())
            })
    });
    models::probe_upstream_format(
        &provider.base_url,
        provider.api_key.as_deref().unwrap_or(""),
        probe_model.as_deref(),
    )
}
