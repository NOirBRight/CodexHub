use super::clients::codex::{
    isolated_apply_unsupported, isolated_preview_text, isolated_readback_unsupported,
};
use super::clients::omp::{apply_omp_config_with_paths, omp_config_text, omp_models_yml_text};
use super::clients::opencode::{apply_opencode_config_with_paths, opencode_config_text};
use super::clients::pi::{apply_pi_config_with_paths, pi_models_text, pi_settings_text};
use super::clients::zcode::{
    apply_zcode_config_with_targets, zcode_catalog_text, zcode_targets_from_writable,
    zcode_v2_cache_text, zcode_v2_config_text,
};
use super::*;
use crate::{Provider, Settings};
use serde::Serialize;
use std::fs;
use std::path::{Path, PathBuf};

// ----- Isolated managed-client configuration seam -----
//
// Headless, caller-supplied-root preview/apply/readback for the five managed
// clients (codex, opencode, zcode, pi, omp). The four native clients reuse the
// existing Rust serializers/apply functions above without duplicating them.
// Codex is owned by `config.rs` and the Python overlay serializer; this module
// only builds an isolated `ConfigPaths` and delegates to it.
//
// Guarantees:
// - Every read and write stays beneath a fresh caller-supplied isolated root.
// - No host discovery: settings, providers, catalog, and backups come only from
//   caller-owned inputs beneath the root.
// - Post-apply readback reopens the produced files, validates ownership and
//   round-trip parity against production preview, and fails closed.
// - Bounded JSON output: client id, selector, canonical model, route/protocol,
//   relative target names, apply/readback status, and approved hashes only.

pub(in crate::gateway) const ISOLATED_CLIENTS: &[&str] =
    &["codex", "opencode", "zcode", "pi", "omp"];

#[derive(Debug, Clone)]
pub struct IsolatedClientRoot {
    root: PathBuf,
}

impl IsolatedClientRoot {
    pub fn root(&self) -> &Path {
        &self.root
    }
}

pub fn isolated_managed_client_ids() -> Vec<String> {
    ISOLATED_CLIENTS
        .iter()
        .map(|id| (*id).to_string())
        .collect()
}

pub fn validate_isolated_root(root: &Path) -> Result<IsolatedClientRoot, String> {
    validate_isolated_root_inner(root, RequireFresh::Yes)
}

/// Validate an isolated root that already contains produced output (used by
/// the readback verb). The root must exist, be a regular directory beneath
/// its parent, and contain no reparse/symlink targets, but it need not be
/// empty.
pub fn validate_existing_isolated_root(root: &Path) -> Result<IsolatedClientRoot, String> {
    validate_isolated_root_inner(root, RequireFresh::No)
}

#[derive(Clone, Copy)]
pub(in crate::gateway) enum RequireFresh {
    Yes,
    No,
}

pub(in crate::gateway) fn validate_isolated_root_inner(
    root: &Path,
    fresh: RequireFresh,
) -> Result<IsolatedClientRoot, String> {
    if root.as_os_str().is_empty() {
        return Err("isolated root path is empty".to_string());
    }
    let canonical_parent = root
        .parent()
        .ok_or_else(|| "isolated root has no parent directory".to_string())?;
    if !canonical_parent.exists() {
        return Err(format!(
            "isolated root parent does not exist: {}",
            canonical_parent.display()
        ));
    }
    // Reject any path component that escapes (.., absolute, or drive-relative).
    for component in root.components() {
        use std::path::Component;
        match component {
            Component::CurDir | Component::RootDir | Component::Prefix(_) => {}
            Component::ParentDir => {
                return Err(
                    "isolated root must not contain relative parent-dir (..) components"
                        .to_string(),
                );
            }
            Component::Normal(_) => {}
        }
    }
    if root.exists() {
        let is_empty = fs::read_dir(root)
            .map_err(|error| format!("failed to read isolated root {}: {error}", root.display()))?
            .next()
            .is_none();
        if matches!(fresh, RequireFresh::Yes) && !is_empty {
            return Err(format!(
                "isolated root is not fresh; refusing to reuse {}: remove it first",
                root.display()
            ));
        }
        // Reject reparse points / symlinks / junctions on the existing root.
        reject_reparse_or_link(root)?;
    } else {
        fs::create_dir_all(root).map_err(|error| {
            format!("failed to create isolated root {}: {error}", root.display())
        })?;
    }
    Ok(IsolatedClientRoot {
        root: root.to_path_buf(),
    })
}

pub(in crate::gateway) fn reject_reparse_or_link(path: &Path) -> Result<(), String> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("failed to stat {}: {error}", path.display()))?;
    if metadata.file_type().is_symlink() {
        return Err(format!(
            "isolated root {} is a symlink; refusing to use it",
            path.display()
        ));
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
        if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err(format!(
                "isolated root {} is a reparse point/junction; refusing to use it",
                path.display()
            ));
        }
    }
    Ok(())
}

/// Confine a caller-supplied path beneath the isolated root with no lexical
/// fallback. Both the root and (when it exists) the candidate's parent must
/// resolve to canonical absolute paths; when the root cannot be
/// canonicalized the path is rejected instead of silently compared against a
/// non-canonical lexical form. The candidate is accepted only when its
/// canonical parent rejoined with its file name starts with the canonical
/// root, so symlinked or reparse-pointed escapes cannot pass a lexical prefix
/// check. Parent directories are never created here — preview/apply callers
/// must ensure they exist, which keeps this verifier side-effect free.
pub(in crate::gateway) fn ensure_path_beneath_root(
    root: &Path,
    path: &Path,
) -> Result<PathBuf, String> {
    let candidate = if path.is_absolute() {
        path.to_path_buf()
    } else {
        root.join(path)
    };
    // Reject any `..` component up front so a lexical escape cannot reach a
    // canonicalizable parent outside the root.
    for component in candidate.components() {
        use std::path::Component;
        match component {
            Component::ParentDir => {
                return Err(format!(
                    "path {} escapes isolated root {} (parent-dir component)",
                    candidate.display(),
                    root.display()
                ));
            }
            Component::CurDir
            | Component::Normal(_)
            | Component::RootDir
            | Component::Prefix(_) => {}
        }
    }
    let root_canonical = fs::canonicalize(root).map_err(|error| {
        format!(
            "path {} cannot be confined: isolated root {} is not canonicalizable: {error}",
            candidate.display(),
            root.display()
        )
    })?;
    let parent = candidate.parent().unwrap_or(Path::new(""));
    // When the parent exists, canonicalize it and require the canonical form
    // to be beneath the root — this is the real anti-symlink guard. When the
    // parent does not exist, fall back to a lexical join with the canonical
    // root and rely on the `..`-component guard above plus the existing-file
    // canonical check below; this never silently accepts an escaped path
    // because the only non-canonical case is a not-yet-created child of the
    // root itself.
    let resolved = if !parent.as_os_str().is_empty() && parent.exists() {
        let canonical_parent = fs::canonicalize(parent).map_err(|error| {
            format!(
                "path {} cannot be confined: parent {} is not canonicalizable: {error}",
                candidate.display(),
                parent.display()
            )
        })?;
        canonical_parent.join(candidate.file_name().unwrap_or_default())
    } else {
        root_canonical.join(candidate.strip_prefix(root).unwrap_or(&candidate))
    };
    if !resolved.starts_with(&root_canonical) {
        return Err(format!(
            "path {} escapes isolated root {}",
            candidate.display(),
            root.display()
        ));
    }
    // If the candidate itself already exists, require its canonical form to
    // stay beneath the root so a symlinked file cannot pass the parent check.
    if candidate.exists() {
        let canonical_candidate = fs::canonicalize(&candidate).map_err(|error| {
            format!(
                "path {} cannot be confined: candidate is not canonicalizable: {error}",
                candidate.display()
            )
        })?;
        if !canonical_candidate.starts_with(&root_canonical) {
            return Err(format!(
                "path {} escapes isolated root {}",
                candidate.display(),
                root.display()
            ));
        }
        return Ok(canonical_candidate);
    }
    Ok(resolved)
}

#[derive(Debug, Clone)]
pub struct IsolatedClientApplyInput {
    pub client_id: String,
    pub model: Option<String>,
    pub settings: Settings,
    pub providers: Vec<Provider>,
    pub catalog_path: Option<PathBuf>,
    pub backup_subdir: Option<PathBuf>,
}

#[derive(Debug, Clone, Serialize)]
pub struct IsolatedClientPreview {
    pub client_id: String,
    pub selector: String,
    pub model: String,
    pub route_protocol: String,
    pub target_names: Vec<String>,
    pub next_redacted: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct IsolatedClientApplyResult {
    pub client_id: String,
    pub applied: bool,
    pub selector: String,
    pub model: String,
    pub route_protocol: String,
    pub target_names: Vec<String>,
    pub backup_dir_relative: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct IsolatedClientReadback {
    pub client_id: String,
    pub ok: bool,
    pub selector: String,
    pub model: String,
    pub route_protocol: String,
}

#[derive(Debug, Clone)]
pub struct IsolatedClientApplyTargets {
    writable_paths: Vec<PathBuf>,
    backup_path: PathBuf,
}

impl IsolatedClientApplyTargets {
    pub fn writable_paths(&self) -> &[PathBuf] {
        &self.writable_paths
    }
    pub fn backup_path(&self) -> &Path {
        &self.backup_path
    }
}

pub fn isolated_client_apply_targets(
    isolated: &IsolatedClientRoot,
    client_id: &str,
) -> Result<IsolatedClientApplyTargets, String> {
    let root = isolated.root();
    let backup_path = root.join("backups");
    let layout = crate::injection::isolated_managed_client(client_id)
        .ok_or_else(|| format!("unknown managed client id: {client_id}"))?;
    let writable_paths = layout
        .files
        .iter()
        .map(|relative| {
            relative
                .split('/')
                .fold(root.to_path_buf(), |path, segment| path.join(segment))
        })
        .collect();
    Ok(IsolatedClientApplyTargets {
        writable_paths,
        backup_path,
    })
}

pub(in crate::gateway) fn normalized_client_id(client_id: &str) -> String {
    normalize_client_id(client_id)
}

pub fn route_protocol_for_selection(provider_id: &str, providers: &[Provider]) -> String {
    match gateway_client_provider_endpoint_selection(provider_id, providers) {
        GatewayClientEndpointSelection::Responses => "responses".to_string(),
        GatewayClientEndpointSelection::ChatCompletions => "chat_completions".to_string(),
        GatewayClientEndpointSelection::AnthropicMessages => "chat_completions".to_string(),
    }
}

pub(in crate::gateway) fn selector_and_route(
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<(String, String, String), String> {
    let groups = gateway_client_provider_groups(settings, providers, model)?;
    let resolved = resolve_gateway_client_model_id(settings, providers, model)?;
    let (provider_id, _) = split_gateway_model_id(&resolved);
    let protocol = route_protocol_for_selection(&provider_id, providers);
    Ok((groups.default_selector, resolved, protocol))
}

pub(in crate::gateway) fn published_target_relative_names(
    client_id: &str,
    targets: &IsolatedClientApplyTargets,
    root: &Path,
) -> Vec<String> {
    targets
        .writable_paths()
        .iter()
        .filter(|path| {
            // ADR-0004: Pi settings.json is user-owned input. It participates
            // in readback/backup but Apply only materializes models.json.
            client_id != "pi" || path.file_name().is_some_and(|name| name == "models.json")
        })
        .map(|path| {
            path.strip_prefix(root)
                .unwrap_or(path)
                .to_string_lossy()
                .replace('\\', "/")
        })
        .collect()
}

pub fn isolated_client_preview(
    isolated: &IsolatedClientRoot,
    input: &IsolatedClientApplyInput,
) -> Result<IsolatedClientPreview, String> {
    let client_id = normalized_client_id(&input.client_id);
    let root = isolated.root();
    if let Some(catalog_path) = &input.catalog_path {
        ensure_path_beneath_root(root, catalog_path)?;
    }
    let targets = isolated_client_apply_targets(isolated, &client_id)?;
    let model = input
        .model
        .clone()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| DEFAULT_MODEL.to_string());
    let (selector, resolved_model, protocol) =
        selector_and_route(&input.settings, &input.providers, &model)?;
    let next_redacted = match client_id.as_str() {
        "opencode" => sanitize_text(&opencode_config_text(
            &input.settings,
            &input.providers,
            &model,
        )?),
        "pi" => {
            let s = pi_settings_text(
                &targets.writable_paths[0],
                &input.settings,
                &input.providers,
                &model,
            )?;
            let m = pi_models_text(
                &targets.writable_paths[1],
                &input.settings,
                &input.providers,
                &model,
            )?;
            sanitize_text(&combined_named_text(&[
                ("settings.json", &s),
                ("models.json", &m),
            ]))
        }
        "omp" => {
            let selector =
                gateway_client_model_selector(&input.settings, &input.providers, &model)?;
            let vision =
                if gateway_exported_model_supports_image(&input.settings, &input.providers, &model)
                {
                    Some(selector.as_str())
                } else {
                    None
                };
            let reasoning = gateway_exported_model_default_reasoning_effort(
                &input.settings,
                &input.providers,
                &model,
            );
            let cfg = omp_config_text(None, &selector, vision, reasoning.as_deref());
            let models = omp_models_yml_text(&input.settings, &input.providers, &model)?;
            sanitize_text(&combined_named_text(&[
                ("config.yml", &cfg),
                ("models.yml", &models),
            ]))
        }
        "zcode" => {
            let catalog = zcode_catalog_text(&input.settings, &input.providers, &model)?;
            let cache = zcode_v2_cache_text(&input.settings, &input.providers, &model)?;
            let config = zcode_v2_config_text(
                &targets.writable_paths[1],
                &input.settings,
                &input.providers,
                &model,
            )?;
            sanitize_text(&combined_named_text(&[
                ("codexhub.json", &catalog),
                ("config.json", &config),
                ("bots-model-cache.v2.json", &cache),
            ]))
        }
        "codex" => isolated_preview_text(),
        other => return Err(format!("unsupported managed client for preview: {other}")),
    };
    let target_names = published_target_relative_names(&client_id, &targets, root);
    Ok(IsolatedClientPreview {
        client_id,
        selector,
        model: resolved_model,
        route_protocol: protocol,
        target_names,
        next_redacted,
    })
}

pub fn apply_gateway_client_config_isolated(
    isolated: &IsolatedClientRoot,
    input: &IsolatedClientApplyInput,
) -> Result<IsolatedClientApplyResult, String> {
    apply_gateway_client_config_isolated_with_provenance(
        isolated,
        input,
        Some(std::path::Path::new("rollback-provenance")),
    )
}

/// Like [`apply_gateway_client_config_isolated`] but with an injectable
/// rollback-provenance root. When `provenance_root` is supplied, the apply seam
/// reads and writes canonical baselines beneath that root only and never
/// touches production `CODEXHUB_ROLLBACK_PROVENANCE_DIR`.
pub fn apply_gateway_client_config_isolated_with_provenance(
    isolated: &IsolatedClientRoot,
    input: &IsolatedClientApplyInput,
    provenance_root: Option<&Path>,
) -> Result<IsolatedClientApplyResult, String> {
    let client_id = normalized_client_id(&input.client_id);
    let root = isolated.root();
    if let Some(catalog_path) = &input.catalog_path {
        ensure_path_beneath_root(root, catalog_path)?;
    }
    let provenance_root = provenance_root
        .map(|provenance| {
            let resolved = root.join(provenance);
            ensure_path_beneath_root(root, &resolved).map(|_| resolved)
        })
        .transpose()?;
    let targets = isolated_client_apply_targets(isolated, &client_id)?;
    let model = input
        .model
        .clone()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| DEFAULT_MODEL.to_string());
    let (selector, resolved_model, protocol) =
        selector_and_route(&input.settings, &input.providers, &model)?;
    let backup_root = match &input.backup_subdir {
        Some(subdir) => {
            ensure_path_beneath_root(root, subdir)?;
            root.join(subdir)
        }
        None => targets.backup_path().to_path_buf(),
    };
    fs::create_dir_all(&backup_root).map_err(|error| {
        format!(
            "failed to create isolated backup root {}: {error}",
            backup_root.display()
        )
    })?;
    let applied = with_rollback_provenance_dir_override(
        provenance_root,
        || -> Result<GatewayClientApplyResult, String> {
            let labeled_backup_root = (backup_root.clone(), BackupChannel::Stable);
            match client_id.as_str() {
                "opencode" => {
                    let path = &targets.writable_paths[0];
                    fs::create_dir_all(path.parent().unwrap())
                        .map_err(|error| format!("failed to create opencode dir: {error}"))?;
                    // Write a non-managed baseline so the apply path creates a backup.
                    if !path.exists() {
                        fs::write(path, r#"{"model":"anthropic/claude-sonnet-4"}"#)
                            .map_err(|error| format!("failed to seed opencode config: {error}"))?;
                    }
                    apply_opencode_config_with_paths(
                        path,
                        std::slice::from_ref(&labeled_backup_root),
                        &input.settings,
                        &input.providers,
                        &model,
                    )
                }
                "pi" => apply_pi_config_with_paths(
                    &targets.writable_paths[0],
                    &targets.writable_paths[1],
                    std::slice::from_ref(&labeled_backup_root),
                    &input.settings,
                    &input.providers,
                    &model,
                ),
                "omp" => apply_omp_config_with_paths(
                    &targets.writable_paths[0],
                    &targets.writable_paths[1],
                    &backup_root,
                    &input.settings,
                    &input.providers,
                    &model,
                ),
                "zcode" => {
                    let zcode_targets = zcode_targets_from_writable(&targets)?;
                    apply_zcode_config_with_targets(
                        &zcode_targets,
                        &backup_root,
                        &input.settings,
                        &input.providers,
                        &model,
                    )
                }
                "codex" => isolated_apply_unsupported(),
                other => Err(format!("unsupported managed client for apply: {other}")),
            }
        },
    )?;
    // F2: the isolated CLI apply path must run the same fail-closed readback
    // verifier as the production GUI/Web Bridge entrypoint, so a partial or
    // tampered write is rejected before reporting success. This shares the
    // single `verify_apply_readback` path with `apply_gateway_client_config_locked`.
    if applied.applied {
        verify_apply_readback(
            &client_id,
            targets.writable_paths(),
            &input.settings,
            &input.providers,
            &model,
        )?;
    }
    let backup_dir_relative = applied
        .backup_path
        .as_ref()
        .and_then(|p| p.strip_prefix(root).ok())
        .map(|p| p.to_string_lossy().replace('\\', "/"));
    let target_names = published_target_relative_names(&client_id, &targets, root);
    Ok(IsolatedClientApplyResult {
        client_id,
        applied: applied.applied,
        selector,
        model: resolved_model,
        route_protocol: protocol,
        target_names,
        backup_dir_relative,
    })
}

pub fn readback_gateway_client_config_isolated(
    isolated: &IsolatedClientRoot,
    input: &IsolatedClientApplyInput,
) -> Result<IsolatedClientReadback, String> {
    let client_id = normalized_client_id(&input.client_id);
    let root = isolated.root();
    if let Some(catalog_path) = &input.catalog_path {
        ensure_path_beneath_root(root, catalog_path)?;
    }
    let targets = isolated_client_apply_targets(isolated, &client_id)?;
    let model = input
        .model
        .clone()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| DEFAULT_MODEL.to_string());
    let (selector, resolved_model, protocol) =
        selector_and_route(&input.settings, &input.providers, &model)?;

    // Every expected target must exist and be a regular file beneath the root.
    for path in targets.writable_paths() {
        ensure_path_beneath_root(root, path)?;
    }

    match client_id.as_str() {
        "opencode" | "pi" | "omp" | "zcode" => {
            verify_apply_readback(
                &client_id,
                targets.writable_paths(),
                &input.settings,
                &input.providers,
                &model,
            )?;
        }
        "codex" => {
            return isolated_readback_unsupported();
        }
        other => return Err(format!("unsupported managed client for readback: {other}")),
    }

    Ok(IsolatedClientReadback {
        client_id,
        ok: true,
        selector,
        model: resolved_model,
        route_protocol: protocol,
    })
}
