use super::clients::omp::{omp_config_text, omp_models_yml_text};
use super::clients::opencode::opencode_config_text;
use super::clients::pi::pi_models_text;
use super::clients::zcode::{
    persisted_zcode_collection_timestamp, zcode_provider_collection_text_with_now,
    zcode_v2_config_text, ZcodeProviderFileKind,
};
use super::{
    gateway_client_model_selector, gateway_exported_model_default_reasoning_effort,
    gateway_exported_model_supports_image, reject_hard_link, timestamp_millis,
};
use crate::{Provider, Settings};
use serde_json::Value;
use std::fs;
use std::path::PathBuf;

/// Post-apply readback verifier shared by the production GUI/Web Bridge apply
/// entrypoints and the headless CLI. Reopens the produced files, rejects
/// missing/partial/malformed/linked output, and asserts round-trip parity
/// against the production serializer for the same settings/providers/model.
///
/// `target_paths` are the absolute host paths the apply step wrote; this
/// verifier does not confine them to an isolated root (that confinement is the
/// caller's responsibility — the isolated CLI path validates the root up
/// front).
pub fn verify_apply_readback(
    client_id: &str,
    target_paths: &[PathBuf],
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<(), String> {
    let required_paths: &[PathBuf] = match client_id {
        // settings.json is user-owned under Provider Injection and is not an
        // apply output. Only models.json is produced/verified.
        "pi" if target_paths.len() >= 2 => &target_paths[1..],
        _ => target_paths,
    };
    for path in required_paths {
        if !path.exists() {
            return Err(format!(
                "readback failed: missing output file {}",
                path.file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or("unknown")
            ));
        }
        let metadata = fs::symlink_metadata(path)
            .map_err(|error| format!("readback failed: cannot stat {}: {error}", path.display()))?;
        if metadata.file_type().is_symlink() {
            return Err(format!(
                "readback failed: {} is a symlink",
                path.file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or("unknown")
            ));
        }
        #[cfg(windows)]
        {
            use std::os::windows::fs::MetadataExt;
            const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
            if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                return Err(format!(
                    "readback failed: {} is a reparse point",
                    path.file_name()
                        .and_then(|n| n.to_str())
                        .unwrap_or("unknown")
                ));
            }
        }
        // Reject hard-linked output: a single-link namespace is required so
        // the produced file is owned by exactly one path beneath the root.
        reject_hard_link(path)?;
    }
    match client_id {
        "opencode" => {
            let written = fs::read_to_string(&target_paths[0])
                .map_err(|error| format!("readback failed: {error}"))?;
            // Provider Injection merge is idempotent: re-merging the written
            // config must reproduce it exactly (foreign providers preserved).
            let expected = opencode_config_text(Some(&written), settings, providers, model)?;
            if written != expected {
                return Err(
                    "readback failed: opencode output does not round-trip production preview"
                        .to_string(),
                );
            }
        }
        "pi" => {
            let written_models = fs::read_to_string(&target_paths[1])
                .map_err(|error| format!("readback failed: {error}"))?;
            let expected_models = pi_models_text(&target_paths[1], settings, providers, model)?;
            if written_models != expected_models {
                return Err(
                    "readback failed: pi output does not round-trip production preview".to_string(),
                );
            }
        }
        "omp" => {
            let written_config = fs::read_to_string(&target_paths[0])
                .map_err(|error| format!("readback failed: {error}"))?;
            let written_models = fs::read_to_string(&target_paths[1])
                .map_err(|error| format!("readback failed: {error}"))?;
            let selector = gateway_client_model_selector(settings, providers, model)?;
            let vision = if gateway_exported_model_supports_image(settings, providers, model) {
                Some(selector.as_str())
            } else {
                None
            };
            let reasoning =
                gateway_exported_model_default_reasoning_effort(settings, providers, model);
            let expected_config = omp_config_text(
                Some(&written_config),
                &selector,
                vision,
                reasoning.as_deref(),
            );
            let expected_models = omp_models_yml_text(None, settings, providers, model)?;
            if written_config != expected_config || written_models != expected_models {
                return Err(
                    "readback failed: omp output does not round-trip production preview"
                        .to_string(),
                );
            }
        }
        "zcode" => {
            let written_catalog = fs::read_to_string(&target_paths[0])
                .map_err(|error| format!("readback failed: {error}"))?;
            let written_config = fs::read_to_string(&target_paths[1])
                .map_err(|error| format!("readback failed: {error}"))?;
            let written_cache = fs::read_to_string(&target_paths[2])
                .map_err(|error| format!("readback failed: {error}"))?;
            for (label, text) in [
                ("codexhub.json", &written_catalog),
                ("config.json", &written_config),
                ("bots-model-cache.v2.json", &written_cache),
            ] {
                serde_json::from_str::<Value>(text)
                    .map_err(|error| format!("readback failed: {label} is malformed: {error}"))?;
            }
            // Deterministic round-trip: reuse the timestamps already persisted
            // in each written collection file instead of regenerating a fresh
            // `timestamp_millis()`. The apply step calls the serializer twice
            // (once for the catalog, once for the v2 cache), so the two files
            // can carry different timestamps a few milliseconds apart; using a
            // single shared `now` would falsely contradict one of them. Each
            // file's expected text is regenerated with that file's own
            // persisted timestamp, so the comparison is stable across
            // wall-clock time and only fails on a real semantic contradiction.
            let catalog_now = persisted_zcode_collection_timestamp(&target_paths[0])
                .unwrap_or_else(|| timestamp_millis() as u64);
            let cache_now = persisted_zcode_collection_timestamp(&target_paths[2])
                .unwrap_or_else(|| timestamp_millis() as u64);
            let expected_catalog = zcode_provider_collection_text_with_now(
                settings,
                providers,
                model,
                ZcodeProviderFileKind::Catalog,
                catalog_now,
            )?;
            let expected_cache = zcode_provider_collection_text_with_now(
                settings,
                providers,
                model,
                ZcodeProviderFileKind::V2Cache,
                cache_now,
            )?;
            let expected_config =
                zcode_v2_config_text(&target_paths[1], settings, providers, model)?;
            if written_catalog != expected_catalog
                || written_config != expected_config
                || written_cache != expected_cache
            {
                return Err(
                    "readback failed: zcode output does not round-trip production preview"
                        .to_string(),
                );
            }
        }
        "codex" => {
            // Codex readback is owned by config::readback_codex_config_isolated;
            // the host apply path is the Python overlay, whose owner marker is
            // verified there.
        }
        other => return Err(format!("unsupported managed client for readback: {other}")),
    }
    Ok(())
}
