//! Managed-client coordinator (ADR-0004 DM-4 amendment).
//!
//! One coordinator owns the managed-client use cases and the cross-cutting
//! guarantees: routing-owner gate, global write lock, baseline/legacy backup
//! adoption, atomic publish/rollback, block-fingerprint readback, pending-sync
//! cleanup, and result mapping.
//!
//! Per-client adapters are pure: they expose metadata(), inspect(ctx), and
//! plan(intent, ctx) -> ClientMutationPlan, and never publish files directly.
//! The DSH adapter is the reference implementation; generic copy-only clients
//! are handled without a native adapter.

use std::fs;
use std::path::{Path, PathBuf};

use super::clients::codex::{
    codex_home, isolated_apply_unsupported, isolated_preview_text, isolated_readback_unsupported,
};
use super::clients::omp::{
    detect_omp_config_paths, plan_omp_apply, preview_omp_config_with_paths, publish_omp_apply,
    restore_omp_config_with_paths,
};
use super::clients::opencode::{
    detect_opencode_config_path, plan_opencode_apply, preview_opencode_config_with_path,
    publish_opencode_apply, restore_opencode_config_with_backup_roots, OpenCodeApplyDecision,
};
use super::clients::pi::{
    detect_pi_config_paths, plan_pi_apply, preview_pi_config_with_paths, publish_pi_apply,
    restore_pi_config_with_paths,
};
use super::clients::zcode::{
    detect_zcode_config_targets, plan_zcode_apply, preview_zcode_config_with_targets,
    publish_zcode_apply, restore_zcode_config_with_targets, ZcodeConfigTargets,
};
use crate::injection::{self, DshLifecycleReport, MaskedSecret, ReadbackExpectation};
use crate::Provider;
use crate::Settings;

/// What a client integration can do.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClientIntent {
    Connect,
    Disconnect,
    /// Used by the catalog republish channel (#428); first consumer arrives
    /// with the #434/#435 adapters.
    #[allow(dead_code)]
    Republish,
}

/// Snapshot of a client's managed state (inspection result).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClientSnapshot {
    pub client_id: String,
    pub connected: bool,
    pub block_present: bool,
    pub activation: Option<String>,
    pub fingerprint: Option<String>,
    pub drift_details: Vec<String>,
    pub restart_required: String,
}

/// A pure mutation plan returned by an adapter. The coordinator performs it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClientMutationPlan {
    pub client_id: String,
    pub intent: ClientIntent,
    /// Paths the coordinator must publish atomically (config files).
    pub write_paths: Vec<PathBuf>,
    /// Expected fingerprint after the mutation, verified by readback.
    pub expected_fingerprint: Option<String>,
    /// Restart disclosure for the UI ("" = none).
    pub restart_required: String,
    /// True when the plan mutates the client's global activation key
    /// (the Codex stable-bucket exception); normal clients are false.
    pub activation_touched: bool,
}

/// Adapter context: everything an adapter needs to build a plan.
pub struct AdapterCtx<'a> {
    pub settings: &'a Settings,
    /// Read by the upcoming #434/#435 adapters (opencode/pi/omp/zcode/codex).
    #[allow(dead_code)]
    pub providers: &'a [Provider],
    pub base_url: String,
    pub models: Vec<String>,
    pub client_root: PathBuf,
}

/// The internal adapter interface. Adapters never publish files; they return
/// pure plans and the coordinator performs the mutation with all guarantees.
pub trait ManagedClientAdapter: Send + Sync {
    fn id(&self) -> &'static str;

    /// Default true; copy-only clients override to false. Consumed by the
    /// upcoming #434/#435 adapters.
    #[allow(dead_code)]
    fn supports_native(&self) -> bool {
        true
    }

    fn config_present(&self) -> bool {
        false
    }

    /// Consumed by the upcoming #434/#435 adapters.
    #[allow(dead_code)]
    fn inspect(&self, ctx: &AdapterCtx<'_>) -> Result<ClientSnapshot, String>;

    fn plan(
        &self,
        intent: ClientIntent,
        ctx: &AdapterCtx<'_>,
    ) -> Result<ClientMutationPlan, String>;

    fn publish_apply(
        &self,
        _settings: &Settings,
        _providers: &[Provider],
        _model: &str,
    ) -> Result<super::GatewayClientApplyResult, String> {
        Ok(copy_only_apply(self.id().to_owned()))
    }

    fn publish_restore(
        &self,
        _backup_roots: &[(PathBuf, super::BackupChannel)],
    ) -> Result<super::GatewayClientApplyResult, String> {
        Ok(copy_only_restore(self.id().to_owned()))
    }

    fn publish_preview(
        &self,
        settings: &Settings,
        providers: &[Provider],
        model: &str,
    ) -> Result<super::GatewayClientConfigPreview, String> {
        copy_only_preview(self.id(), settings, providers, model)
    }

    fn apply_isolated(
        &self,
        _writable_paths: &[PathBuf],
        _backup_root: &Path,
        _backup_roots: &[(PathBuf, super::BackupChannel)],
        _settings: &Settings,
        _providers: &[Provider],
        _model: &str,
    ) -> Result<super::GatewayClientApplyResult, String> {
        Err(format!("unsupported managed client for apply: {}", self.id()))
    }

    fn preview_isolated(
        &self,
        _writable_paths: &[PathBuf],
        _settings: &Settings,
        _providers: &[Provider],
        _model: &str,
    ) -> Result<super::GatewayClientConfigPreview, String> {
        Err(format!("unsupported managed client for preview: {}", self.id()))
    }

    fn readback_isolated(
        &self,
        paths: &[PathBuf],
        settings: &Settings,
        providers: &[Provider],
        model: &str,
    ) -> Result<(), String> {
        readback_native_at(self.id(), paths, settings, providers, model)
    }
}

/// The DSH reference adapter.
pub struct DshAdapter;

fn dsh_expectation_under(ctx: &AdapterCtx<'_>) -> ReadbackExpectation {
    ReadbackExpectation {
        base_url: ctx.base_url.clone(),
        models: ctx.models.clone(),
    }
}

impl ManagedClientAdapter for DshAdapter {
    fn id(&self) -> &'static str {
        "dsh"
    }

    fn inspect(&self, ctx: &AdapterCtx<'_>) -> Result<ClientSnapshot, String> {
        let expectation = dsh_expectation_under(ctx);
        let report = injection::dsh_readback(&ctx.client_root, &expectation)?;
        Ok(ClientSnapshot {
            client_id: "dsh".to_owned(),
            connected: report.connected,
            block_present: report.block_present,
            activation: report.activation,
            fingerprint: report.fingerprint,
            drift_details: report.drift_details,
            restart_required: report.restart_required,
        })
    }

    fn plan(
        &self,
        intent: ClientIntent,
        ctx: &AdapterCtx<'_>,
    ) -> Result<ClientMutationPlan, String> {
        // Pure plan: expectation + fingerprint + write paths. The mutation
        // itself happens in perform_plan via injection::inject/detach so every
        // guarantee (backup, atomic write, masked secret, fingerprint
        // readback) lives in the coordinator path.
        let expectation = dsh_expectation_under(ctx);
        let descriptor = crate::injection::dsh_descriptor();
        let fingerprint =
            injection::expected_block_fingerprint(&descriptor, &expectation.base_url, &expectation.models);
        Ok(ClientMutationPlan {
            client_id: "dsh".to_owned(),
            intent,
            write_paths: vec![
                ctx.client_root.join("settings.yaml"),
                ctx.client_root.join(".credentials.yaml"),
            ],
            expected_fingerprint: Some(fingerprint),
            restart_required: "none".to_owned(),
            activation_touched: false,
        })
    }
}

/// Codex CLI overlay is owned by config.rs / config_overlay.py. This adapter
/// is the coordinator registry entry: plan/inspect without touching
/// activation. Isolated apply/readback stay fail-closed to the overlay path.
pub struct CodexAdapter;

impl ManagedClientAdapter for CodexAdapter {
    fn id(&self) -> &'static str {
        "codex"
    }

    fn config_present(&self) -> bool {
        codex_home().join("config.toml").exists()
    }

    fn inspect(&self, _ctx: &AdapterCtx<'_>) -> Result<ClientSnapshot, String> {
        Ok(native_snapshot(self.id(), self.config_present()))
    }

    fn plan(
        &self,
        intent: ClientIntent,
        _ctx: &AdapterCtx<'_>,
    ) -> Result<ClientMutationPlan, String> {
        Ok(native_plan(
            self.id(),
            intent,
            vec![codex_home().join("config.toml")],
        ))
    }

    fn publish_apply(
        &self,
        _settings: &Settings,
        _providers: &[Provider],
        _model: &str,
    ) -> Result<super::GatewayClientApplyResult, String> {
        Ok(super::GatewayClientApplyResult {
            client_id: self.id().to_owned(),
            applied: false,
            config_path: None,
            backup_path: None,
            message: "Codex apply is owned by config_overlay.py.".to_string(),
        })
    }

    fn publish_restore(
        &self,
        _backup_roots: &[(PathBuf, super::BackupChannel)],
    ) -> Result<super::GatewayClientApplyResult, String> {
        Ok(super::GatewayClientApplyResult {
            client_id: self.id().to_owned(),
            applied: false,
            config_path: None,
            backup_path: None,
            message: "Codex restore is owned by config_overlay.py.".to_string(),
        })
    }

    fn publish_preview(
        &self,
        _settings: &Settings,
        _providers: &[Provider],
        _model: &str,
    ) -> Result<super::GatewayClientConfigPreview, String> {
        Ok(super::GatewayClientConfigPreview {
            client_id: self.id().to_owned(),
            can_apply: false,
            strategy: "overlay".to_string(),
            config_path: None,
            current_redacted: None,
            next_redacted: isolated_preview_text(),
            backup_required: false,
            message: "Codex preview is owned by config_overlay.py.".to_string(),
        })
    }

    fn apply_isolated(
        &self,
        _writable_paths: &[PathBuf],
        _backup_root: &Path,
        _backup_roots: &[(PathBuf, super::BackupChannel)],
        _settings: &Settings,
        _providers: &[Provider],
        _model: &str,
    ) -> Result<super::GatewayClientApplyResult, String> {
        isolated_apply_unsupported()
    }

    fn preview_isolated(
        &self,
        _writable_paths: &[PathBuf],
        _settings: &Settings,
        _providers: &[Provider],
        _model: &str,
    ) -> Result<super::GatewayClientConfigPreview, String> {
        Ok(super::GatewayClientConfigPreview {
            client_id: self.id().to_owned(),
            can_apply: false,
            strategy: "overlay".to_string(),
            config_path: None,
            current_redacted: None,
            next_redacted: isolated_preview_text(),
            backup_required: false,
            message: "Codex preview is owned by config_overlay.py.".to_string(),
        })
    }

    fn readback_isolated(
        &self,
        _paths: &[PathBuf],
        _settings: &Settings,
        _providers: &[Provider],
        _model: &str,
    ) -> Result<(), String> {
        isolated_readback_unsupported()
    }
}

/// Perform a plan with the coordinator's guarantees.
pub fn perform_plan(
    adapter: &dyn ManagedClientAdapter,
    plan: &ClientMutationPlan,
    ctx: &AdapterCtx<'_>,
) -> Result<ClientSnapshot, String> {
    if plan.activation_touched {
        return Err(format!(
            "{} adapter must not touch the activation key",
            adapter.id()
        ));
    }
    match plan.intent {
        ClientIntent::Connect => {
            let api_key = MaskedSecret::new(ctx.settings.gateway_client_key.clone());
            let report =
                injection::dsh_connect(&ctx.client_root, ctx.base_url.clone(), api_key, ctx.models.clone())?;
            Ok(ClientSnapshot {
                client_id: report.client_id,
                connected: report.connected,
                block_present: report.block_present,
                activation: report.activation,
                fingerprint: report.fingerprint,
                drift_details: report.drift_details,
                restart_required: report.restart_required,
            })
        }
        ClientIntent::Disconnect => {
            let expectation = dsh_expectation_under(ctx);
            let report = injection::dsh_disconnect(&ctx.client_root, &expectation)?;
            Ok(ClientSnapshot {
                client_id: report.client_id,
                connected: report.connected,
                block_present: report.block_present,
                activation: report.activation,
                fingerprint: report.fingerprint,
                drift_details: report.drift_details,
                restart_required: report.restart_required,
            })
        }
        ClientIntent::Republish => {
            let expectation = dsh_expectation_under(ctx);
            let report = injection::dsh_readback(&ctx.client_root, &expectation)?;
            Ok(ClientSnapshot {
                client_id: report.client_id,
                connected: report.connected,
                block_present: report.block_present,
                activation: report.activation,
                fingerprint: report.fingerprint,
                drift_details: report.drift_details,
                restart_required: report.restart_required,
            })
        }
    }
}

/// DSH lifecycle entrypoints through the coordinator. Behavior is identical
/// to the previous direct injection calls (same report shapes and errors);
/// the callers in gateway/clients/dsh.rs provide the live context.
pub fn dsh_connect_plan(ctx: &AdapterCtx<'_>) -> Result<DshLifecycleReport, String> {
    let plan = DshAdapter.plan(ClientIntent::Connect, ctx)?;
    perform_plan(&DshAdapter, &plan, ctx)?;
    let expectation = dsh_expectation_under(ctx);
    injection::dsh_readback(&ctx.client_root, &expectation)
}

pub fn dsh_disconnect_plan(ctx: &AdapterCtx<'_>) -> Result<DshLifecycleReport, String> {
    let plan = DshAdapter.plan(ClientIntent::Disconnect, ctx)?;
    perform_plan(&DshAdapter, &plan, ctx)?;
    let expectation = dsh_expectation_under(ctx);
    injection::dsh_readback(&ctx.client_root, &expectation)
}

pub fn dsh_readback_plan(ctx: &AdapterCtx<'_>) -> Result<DshLifecycleReport, String> {
    let expectation = dsh_expectation_under(ctx);
    injection::dsh_readback(&ctx.client_root, &expectation)
}

fn native_snapshot(id: &'static str, present: bool) -> ClientSnapshot {
    ClientSnapshot {
        client_id: id.to_owned(),
        connected: present,
        block_present: present,
        activation: None,
        fingerprint: None,
        drift_details: Vec::new(),
        restart_required: "none".to_owned(),
    }
}

fn copy_only_apply(client_id: String) -> super::GatewayClientApplyResult {
    super::GatewayClientApplyResult {
        client_id,
        applied: false,
        config_path: None,
        backup_path: None,
        message: "This client is copy-only; no native adapter is registered.".to_string(),
    }
}

fn copy_only_restore(client_id: String) -> super::GatewayClientApplyResult {
    super::GatewayClientApplyResult {
        client_id,
        applied: false,
        config_path: None,
        backup_path: None,
        message: "Restore is not available for this copy-only client.".to_string(),
    }
}

fn copy_only_preview(
    client_id: &str,
    _settings: &Settings,
    _providers: &[Provider],
    model: &str,
) -> Result<super::GatewayClientConfigPreview, String> {
    let config = super::gateway_copy_client_config(
        Some(client_id.to_string()),
        Some(model.to_string()),
    )?;
    Ok(super::GatewayClientConfigPreview {
        client_id: client_id.to_string(),
        can_apply: false,
        strategy: "copy_only".to_string(),
        config_path: None,
        current_redacted: None,
        next_redacted: config.json,
        backup_required: false,
        message: "Generic and unknown clients are copy-only in this release.".to_string(),
    })
}

fn native_plan(id: &'static str, intent: ClientIntent, write_paths: Vec<PathBuf>) -> ClientMutationPlan {
    ClientMutationPlan {
        client_id: id.to_owned(),
        intent,
        write_paths,
        expected_fingerprint: None,
        restart_required: "none".to_owned(),
        activation_touched: false,
    }
}

pub struct OpenCodeAdapter;
pub struct PiAdapter;
pub struct OmpAdapter;
pub struct ZcodeAdapter;

impl ManagedClientAdapter for OpenCodeAdapter {
    fn id(&self) -> &'static str {
        "opencode"
    }

    fn config_present(&self) -> bool {
        detect_opencode_config_path()
            .as_ref()
            .map(|path| path.exists())
            .unwrap_or(false)
    }

    fn inspect(&self, _ctx: &AdapterCtx<'_>) -> Result<ClientSnapshot, String> {
        Ok(native_snapshot(self.id(), self.config_present()))
    }

    fn plan(
        &self,
        intent: ClientIntent,
        _ctx: &AdapterCtx<'_>,
    ) -> Result<ClientMutationPlan, String> {
        Ok(native_plan(
            self.id(),
            intent,
            detect_opencode_config_path().into_iter().collect(),
        ))
    }

    fn publish_apply(
        &self,
        settings: &Settings,
        providers: &[Provider],
        model: &str,
    ) -> Result<super::GatewayClientApplyResult, String> {
        let path = detect_opencode_config_path()
            .ok_or_else(|| "OpenCode config path could not be resolved".to_string())?;
        let backup_roots = super::client_backup_roots_for_apply("opencode");
        apply_native_at(
            NativeApplySpec::OpenCode {
                path: &path,
                backup_roots: &backup_roots,
            },
            settings,
            providers,
            model,
        )
    }

    fn publish_restore(
        &self,
        backup_roots: &[(PathBuf, super::BackupChannel)],
    ) -> Result<super::GatewayClientApplyResult, String> {
        let path = detect_opencode_config_path()
            .ok_or_else(|| "OpenCode config path could not be resolved".to_string())?;
        restore_opencode_config_with_backup_roots(&path, backup_roots)
    }

    fn publish_preview(
        &self,
        settings: &Settings,
        providers: &[Provider],
        model: &str,
    ) -> Result<super::GatewayClientConfigPreview, String> {
        let path = detect_opencode_config_path()
            .ok_or_else(|| "OpenCode config path could not be resolved".to_string())?;
        preview_native_at(
            NativePreviewSpec::OpenCode { path: &path },
            settings,
            providers,
            model,
        )
    }

    fn apply_isolated(
        &self,
        writable_paths: &[PathBuf],
        _backup_root: &Path,
        backup_roots: &[(PathBuf, super::BackupChannel)],
        settings: &Settings,
        providers: &[Provider],
        model: &str,
    ) -> Result<super::GatewayClientApplyResult, String> {
        let path = writable_paths
            .first()
            .ok_or_else(|| "opencode isolated targets are missing files".to_string())?;
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)
                .map_err(|error| format!("failed to create opencode dir: {error}"))?;
        }
        if !path.exists() {
            fs::write(path, r#"{"model":"anthropic/claude-sonnet-4"}"#)
                .map_err(|error| format!("failed to seed opencode config: {error}"))?;
        }
        apply_native_at(
            NativeApplySpec::OpenCode {
                path,
                backup_roots,
            },
            settings,
            providers,
            model,
        )
    }

    fn preview_isolated(
        &self,
        writable_paths: &[PathBuf],
        settings: &Settings,
        providers: &[Provider],
        model: &str,
    ) -> Result<super::GatewayClientConfigPreview, String> {
        let path = writable_paths
            .first()
            .ok_or_else(|| "opencode isolated targets are missing files".to_string())?;
        preview_native_at(
            NativePreviewSpec::OpenCode { path },
            settings,
            providers,
            model,
        )
    }
}

impl ManagedClientAdapter for PiAdapter {
    fn id(&self) -> &'static str {
        "pi"
    }

    fn config_present(&self) -> bool {
        let paths = detect_pi_config_paths();
        paths.settings_path.exists() || paths.models_path.exists()
    }

    fn inspect(&self, _ctx: &AdapterCtx<'_>) -> Result<ClientSnapshot, String> {
        Ok(native_snapshot(self.id(), self.config_present()))
    }

    fn plan(
        &self,
        intent: ClientIntent,
        _ctx: &AdapterCtx<'_>,
    ) -> Result<ClientMutationPlan, String> {
        let paths = detect_pi_config_paths();
        Ok(native_plan(
            self.id(),
            intent,
            vec![paths.settings_path, paths.models_path],
        ))
    }

    fn publish_apply(
        &self,
        settings: &Settings,
        providers: &[Provider],
        model: &str,
    ) -> Result<super::GatewayClientApplyResult, String> {
        let paths = detect_pi_config_paths();
        let backup_roots = super::client_backup_roots_for_apply("pi");
        apply_native_at(
            NativeApplySpec::Pi {
                settings_path: &paths.settings_path,
                models_path: &paths.models_path,
                backup_roots: &backup_roots,
            },
            settings,
            providers,
            model,
        )
    }

    fn publish_restore(
        &self,
        backup_roots: &[(PathBuf, super::BackupChannel)],
    ) -> Result<super::GatewayClientApplyResult, String> {
        let paths = detect_pi_config_paths();
        restore_pi_config_with_paths(&paths.settings_path, &paths.models_path, backup_roots)
    }

    fn publish_preview(
        &self,
        settings: &Settings,
        providers: &[Provider],
        model: &str,
    ) -> Result<super::GatewayClientConfigPreview, String> {
        let paths = detect_pi_config_paths();
        preview_native_at(
            NativePreviewSpec::Pi {
                settings_path: &paths.settings_path,
                models_path: &paths.models_path,
            },
            settings,
            providers,
            model,
        )
    }

    fn apply_isolated(
        &self,
        writable_paths: &[PathBuf],
        _backup_root: &Path,
        backup_roots: &[(PathBuf, super::BackupChannel)],
        settings: &Settings,
        providers: &[Provider],
        model: &str,
    ) -> Result<super::GatewayClientApplyResult, String> {
        if writable_paths.len() < 2 {
            return Err("pi isolated targets are missing files".to_string());
        }
        apply_native_at(
            NativeApplySpec::Pi {
                settings_path: &writable_paths[0],
                models_path: &writable_paths[1],
                backup_roots,
            },
            settings,
            providers,
            model,
        )
    }

    fn preview_isolated(
        &self,
        writable_paths: &[PathBuf],
        settings: &Settings,
        providers: &[Provider],
        model: &str,
    ) -> Result<super::GatewayClientConfigPreview, String> {
        if writable_paths.len() < 2 {
            return Err("pi isolated targets are missing files".to_string());
        }
        preview_native_at(
            NativePreviewSpec::Pi {
                settings_path: &writable_paths[0],
                models_path: &writable_paths[1],
            },
            settings,
            providers,
            model,
        )
    }
}

impl ManagedClientAdapter for OmpAdapter {
    fn id(&self) -> &'static str {
        "omp"
    }

    fn config_present(&self) -> bool {
        let paths = detect_omp_config_paths();
        paths.config_path.exists() || paths.models_path.exists()
    }

    fn inspect(&self, _ctx: &AdapterCtx<'_>) -> Result<ClientSnapshot, String> {
        Ok(native_snapshot(self.id(), self.config_present()))
    }

    fn plan(
        &self,
        intent: ClientIntent,
        _ctx: &AdapterCtx<'_>,
    ) -> Result<ClientMutationPlan, String> {
        let paths = detect_omp_config_paths();
        Ok(native_plan(
            self.id(),
            intent,
            vec![paths.config_path, paths.models_path],
        ))
    }

    fn publish_apply(
        &self,
        settings: &Settings,
        providers: &[Provider],
        model: &str,
    ) -> Result<super::GatewayClientApplyResult, String> {
        let paths = detect_omp_config_paths();
        let backup_root = super::client_backup_root("omp");
        apply_native_at(
            NativeApplySpec::Omp {
                config_path: &paths.config_path,
                models_path: &paths.models_path,
                backup_root: &backup_root,
            },
            settings,
            providers,
            model,
        )
    }

    fn publish_restore(
        &self,
        backup_roots: &[(PathBuf, super::BackupChannel)],
    ) -> Result<super::GatewayClientApplyResult, String> {
        let paths = detect_omp_config_paths();
        super::restore_with_backup_roots(backup_roots, |backup_root| {
            restore_omp_config_with_paths(&paths.config_path, &paths.models_path, backup_root)
        })
    }

    fn publish_preview(
        &self,
        settings: &Settings,
        providers: &[Provider],
        model: &str,
    ) -> Result<super::GatewayClientConfigPreview, String> {
        let paths = detect_omp_config_paths();
        preview_native_at(
            NativePreviewSpec::Omp {
                config_path: &paths.config_path,
                models_path: &paths.models_path,
            },
            settings,
            providers,
            model,
        )
    }

    fn apply_isolated(
        &self,
        writable_paths: &[PathBuf],
        backup_root: &Path,
        _backup_roots: &[(PathBuf, super::BackupChannel)],
        settings: &Settings,
        providers: &[Provider],
        model: &str,
    ) -> Result<super::GatewayClientApplyResult, String> {
        if writable_paths.len() < 2 {
            return Err("omp isolated targets are missing files".to_string());
        }
        apply_native_at(
            NativeApplySpec::Omp {
                config_path: &writable_paths[0],
                models_path: &writable_paths[1],
                backup_root,
            },
            settings,
            providers,
            model,
        )
    }

    fn preview_isolated(
        &self,
        writable_paths: &[PathBuf],
        settings: &Settings,
        providers: &[Provider],
        model: &str,
    ) -> Result<super::GatewayClientConfigPreview, String> {
        if writable_paths.len() < 2 {
            return Err("omp isolated targets are missing files".to_string());
        }
        preview_native_at(
            NativePreviewSpec::Omp {
                config_path: &writable_paths[0],
                models_path: &writable_paths[1],
            },
            settings,
            providers,
            model,
        )
    }
}

impl ManagedClientAdapter for ZcodeAdapter {
    fn id(&self) -> &'static str {
        "zcode"
    }

    fn config_present(&self) -> bool {
        let targets = detect_zcode_config_targets();
        targets.v2_config_path.exists()
            || targets.catalog_path.exists()
            || targets.v2_cache_path.exists()
    }

    fn inspect(&self, _ctx: &AdapterCtx<'_>) -> Result<ClientSnapshot, String> {
        Ok(native_snapshot(self.id(), self.config_present()))
    }

    fn plan(
        &self,
        intent: ClientIntent,
        _ctx: &AdapterCtx<'_>,
    ) -> Result<ClientMutationPlan, String> {
        let targets = detect_zcode_config_targets();
        Ok(native_plan(
            self.id(),
            intent,
            vec![
                targets.catalog_path,
                targets.v2_config_path,
                targets.v2_cache_path,
            ],
        ))
    }

    fn publish_apply(
        &self,
        settings: &Settings,
        providers: &[Provider],
        model: &str,
    ) -> Result<super::GatewayClientApplyResult, String> {
        let targets = detect_zcode_config_targets();
        let backup_root = super::client_backup_root("zcode");
        apply_native_at(
            NativeApplySpec::Zcode {
                targets: &targets,
                backup_root: &backup_root,
            },
            settings,
            providers,
            model,
        )
    }

    fn publish_restore(
        &self,
        backup_roots: &[(PathBuf, super::BackupChannel)],
    ) -> Result<super::GatewayClientApplyResult, String> {
        let targets = detect_zcode_config_targets();
        super::restore_with_backup_roots(backup_roots, |backup_root| {
            restore_zcode_config_with_targets(&targets, backup_root)
        })
    }

    fn publish_preview(
        &self,
        settings: &Settings,
        providers: &[Provider],
        model: &str,
    ) -> Result<super::GatewayClientConfigPreview, String> {
        let targets = detect_zcode_config_targets();
        preview_native_at(
            NativePreviewSpec::Zcode { targets: &targets },
            settings,
            providers,
            model,
        )
    }

    fn apply_isolated(
        &self,
        writable_paths: &[PathBuf],
        backup_root: &Path,
        _backup_roots: &[(PathBuf, super::BackupChannel)],
        settings: &Settings,
        providers: &[Provider],
        model: &str,
    ) -> Result<super::GatewayClientApplyResult, String> {
        if writable_paths.len() < 3 {
            return Err("zcode isolated targets are missing files".to_string());
        }
        let targets = ZcodeConfigTargets {
            catalog_path: writable_paths[0].clone(),
            v2_config_path: writable_paths[1].clone(),
            v2_cache_path: writable_paths[2].clone(),
        };
        apply_native_at(
            NativeApplySpec::Zcode {
                targets: &targets,
                backup_root,
            },
            settings,
            providers,
            model,
        )
    }

    fn preview_isolated(
        &self,
        writable_paths: &[PathBuf],
        settings: &Settings,
        providers: &[Provider],
        model: &str,
    ) -> Result<super::GatewayClientConfigPreview, String> {
        if writable_paths.len() < 3 {
            return Err("zcode isolated targets are missing files".to_string());
        }
        let targets = ZcodeConfigTargets {
            catalog_path: writable_paths[0].clone(),
            v2_config_path: writable_paths[1].clone(),
            v2_cache_path: writable_paths[2].clone(),
        };
        preview_native_at(
            NativePreviewSpec::Zcode { targets: &targets },
            settings,
            providers,
            model,
        )
    }
}

pub fn adapter_for(id: &str) -> Option<&'static dyn ManagedClientAdapter> {
    match id {
        "dsh" => Some(&DshAdapter),
        "codex" => Some(&CodexAdapter),
        "opencode" => Some(&OpenCodeAdapter),
        "pi" => Some(&PiAdapter),
        "omp" => Some(&OmpAdapter),
        "zcode" => Some(&ZcodeAdapter),
        _ => None,
    }
}

/// Resolved native apply targets. Live apply discovers these; isolated apply
/// supplies them from the caller-owned root.
pub enum NativeApplySpec<'a> {
    OpenCode {
        path: &'a Path,
        backup_roots: &'a [(PathBuf, super::BackupChannel)],
    },
    Pi {
        settings_path: &'a Path,
        models_path: &'a Path,
        backup_roots: &'a [(PathBuf, super::BackupChannel)],
    },
    Omp {
        config_path: &'a Path,
        models_path: &'a Path,
        backup_root: &'a Path,
    },
    Zcode {
        targets: &'a ZcodeConfigTargets,
        backup_root: &'a Path,
    },
}

/// Apply a resolved native spec and run production readback. Isolated and live
/// apply share this path so a second client-id match cannot drift.
pub fn apply_native_at(
    spec: NativeApplySpec<'_>,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<super::GatewayClientApplyResult, String> {
    match spec {
        NativeApplySpec::OpenCode { path, backup_roots } => {
            match plan_opencode_apply(path, settings, providers, model)? {
                OpenCodeApplyDecision::NotApplied(result) => Ok(result),
                OpenCodeApplyDecision::Apply(plan) => {
                    let result = publish_opencode_apply(&plan, backup_roots)?;
                    if result.applied {
                        readback_native_at(
                            "opencode",
                            &[path.to_path_buf()],
                            settings,
                            providers,
                            model,
                        )?;
                    }
                    Ok(result)
                }
            }
        }
        NativeApplySpec::Pi {
            settings_path,
            models_path,
            backup_roots,
        } => {
            let plan = plan_pi_apply(settings_path, models_path, settings, providers, model)?;
            let result = publish_pi_apply(&plan, backup_roots)?;
            if result.applied {
                readback_native_at(
                    "pi",
                    &[settings_path.to_path_buf(), models_path.to_path_buf()],
                    settings,
                    providers,
                    model,
                )?;
            }
            Ok(result)
        }
        NativeApplySpec::Omp {
            config_path,
            models_path,
            backup_root,
        } => {
            let plan = plan_omp_apply(config_path, models_path, settings, providers, model)?;
            let result = publish_omp_apply(&plan, backup_root)?;
            if result.applied {
                readback_native_at(
                    "omp",
                    &[config_path.to_path_buf(), models_path.to_path_buf()],
                    settings,
                    providers,
                    model,
                )?;
            }
            Ok(result)
        }
        NativeApplySpec::Zcode {
            targets,
            backup_root,
        } => {
            let plan = plan_zcode_apply(targets, settings, providers, model)?;
            let result = publish_zcode_apply(&plan, backup_root)?;
            if result.applied {
                readback_native_at(
                    "zcode",
                    &[
                        targets.catalog_path.clone(),
                        targets.v2_config_path.clone(),
                        targets.v2_cache_path.clone(),
                    ],
                    settings,
                    providers,
                    model,
                )?;
            }
            Ok(result)
        }
    }
}

pub fn readback_native_at(
    client_id: &str,
    paths: &[PathBuf],
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<(), String> {
    match client_id {
        "opencode" | "pi" | "omp" | "zcode" => {
            super::verify_apply_readback(client_id, paths, settings, providers, model)
        }
        _ => Err(format!("unsupported managed client for readback: {client_id}")),
    }
}

/// Native apply dispatch owned by the coordinator. Live apply discovers host
/// paths then calls apply_native_at.
pub fn apply_native(
    client_id: String,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<super::GatewayClientApplyResult, String> {
    match adapter_for(&client_id) {
        Some(adapter) => adapter.publish_apply(settings, providers, model),
        None => Ok(copy_only_apply(client_id)),
    }
}

pub fn has_existing_config(client_id: &str) -> bool {
    adapter_for(client_id).is_some_and(ManagedClientAdapter::config_present)
}

pub enum NativePreviewSpec<'a> {
    OpenCode { path: &'a Path },
    Pi {
        settings_path: &'a Path,
        models_path: &'a Path,
    },
    Omp {
        config_path: &'a Path,
        models_path: &'a Path,
    },
    Zcode { targets: &'a ZcodeConfigTargets },
}

pub fn preview_native_at(
    spec: NativePreviewSpec<'_>,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<super::GatewayClientConfigPreview, String> {
    match spec {
        NativePreviewSpec::OpenCode { path } => {
            preview_opencode_config_with_path(path, settings, providers, model)
        }
        NativePreviewSpec::Pi {
            settings_path,
            models_path,
        } => preview_pi_config_with_paths(settings_path, models_path, settings, providers, model),
        NativePreviewSpec::Omp {
            config_path,
            models_path,
        } => preview_omp_config_with_paths(config_path, models_path, settings, providers, model),
        NativePreviewSpec::Zcode { targets } => {
            preview_zcode_config_with_targets(targets, settings, providers, model)
        }
    }
}

/// Native preview dispatch owned by the coordinator.
pub fn preview_native(
    client_id: &str,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<super::GatewayClientConfigPreview, String> {
    match adapter_for(client_id) {
        Some(adapter) => adapter.publish_preview(settings, providers, model),
        None => copy_only_preview(client_id, settings, providers, model),
    }
}

/// Native restore dispatch owned by the coordinator. The caller holds the write
/// lock and supplies channel backup roots.
pub fn restore_native(
    client_id: String,
    backup_roots: &[(PathBuf, super::BackupChannel)],
) -> Result<super::GatewayClientApplyResult, String> {
    match adapter_for(&client_id) {
        Some(adapter) => adapter.publish_restore(backup_roots),
        None => Ok(copy_only_restore(client_id)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_ctx() -> (Settings, AdapterCtx<'static>) {
        // Box the settings so the borrow outlives the returned ctx.
        let settings: Settings = Settings::default();
        let boxed: &'static Settings = Box::leak(Box::new(settings));
        (
            Settings::default(),
            AdapterCtx {
                settings: boxed,
                providers: &[],
                base_url: "http://127.0.0.1:9109/v1".to_owned(),
                models: vec![],
                client_root: std::env::temp_dir(),
            },
        )
    }

    #[test]
    fn dsh_adapter_plan_is_pure_and_activation_safe() {
        let (_settings, ctx) = test_ctx();
        let plan = DshAdapter
            .plan(ClientIntent::Connect, &ctx)
            .expect("plan");
        assert_eq!(plan.client_id, "dsh");
        assert!(!plan.activation_touched, "activation must stay user-owned");
        assert!(plan.expected_fingerprint.is_some());
        assert_eq!(plan.restart_required, "none");
        assert_eq!(plan.write_paths.len(), 2);
    }

    #[test]
    fn perform_plan_rejects_activation_touching_adapters() {
        let (_settings, ctx) = test_ctx();
        let mut plan = DshAdapter
            .plan(ClientIntent::Connect, &ctx)
            .expect("plan");
        plan.activation_touched = true;
        let result = perform_plan(&DshAdapter, &plan, &ctx);
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("activation"));
    }

    #[test]
    fn apply_native_unknown_client_is_copy_only() {
        let settings = Settings::default();
        let result = apply_native("unknown".to_owned(), &settings, &[], "gpt-5.6-luna")
            .expect("copy-only result");
        assert!(!result.applied);
        assert_eq!(result.client_id, "unknown");
        assert!(result.message.contains("copy-only"));
        let dsh = apply_native("dsh".to_owned(), &settings, &[], "gpt-5.6-luna")
            .expect("dsh is not native apply");
        assert!(!dsh.applied);
        assert_eq!(dsh.client_id, "dsh");
    }

    #[test]
    fn restore_native_unknown_client_is_copy_only() {
        let result = restore_native("unknown".to_owned(), &[])
            .expect("copy-only result");
        assert!(!result.applied);
        assert_eq!(result.client_id, "unknown");
        assert!(result.message.contains("copy-only"));
        let dsh = restore_native("dsh".to_owned(), &[]).expect("dsh is not native restore");
        assert!(!dsh.applied);
        assert_eq!(dsh.client_id, "dsh");
    }

    #[test]
    fn preview_native_unknown_client_is_copy_only() {
        let settings = Settings::default();
        let preview = preview_native("unknown", &settings, &[], "gpt-5.6-luna")
            .expect("copy-only preview");
        assert!(!preview.can_apply);
        assert_eq!(preview.client_id, "unknown");
        assert_eq!(preview.strategy, "copy_only");
        let dsh = preview_native("dsh", &settings, &[], "gpt-5.6-luna")
            .expect("dsh is copy-only preview");
        assert!(!dsh.can_apply);
        assert_eq!(dsh.client_id, "dsh");
        assert_eq!(dsh.strategy, "copy_only");
    }

    #[test]
    fn has_existing_config_is_false_for_unknown_clients() {
        assert!(!has_existing_config("unknown"));
        assert!(!has_existing_config("dsh"));
    }

    #[test]
    fn has_existing_config_matches_adapter_config_present() {
        for id in ["codex", "opencode", "pi", "omp", "zcode", "dsh"] {
            let adapter = adapter_for(id).expect("registered adapter");
            assert_eq!(
                has_existing_config(id),
                adapter.config_present(),
                "{id} presence must come from the adapter"
            );
        }
    }

    #[test]
    fn readback_native_at_rejects_unknown_clients() {
        let settings = Settings::default();
        let error = readback_native_at("unknown", &[], &settings, &[], "gpt-5.6-luna")
            .expect_err("unknown clients have no native readback");
        assert!(error.contains("unsupported managed client for readback"));
    }

    #[test]
    fn adapter_for_registers_native_clients_and_rejects_unknown() {
        assert_eq!(adapter_for("opencode").map(ManagedClientAdapter::id), Some("opencode"));
        assert_eq!(adapter_for("pi").map(ManagedClientAdapter::id), Some("pi"));
        assert_eq!(adapter_for("omp").map(ManagedClientAdapter::id), Some("omp"));
        assert_eq!(adapter_for("zcode").map(ManagedClientAdapter::id), Some("zcode"));
        assert_eq!(adapter_for("dsh").map(ManagedClientAdapter::id), Some("dsh"));
        assert_eq!(adapter_for("codex").map(ManagedClientAdapter::id), Some("codex"));
        assert!(adapter_for("unknown").is_none());
    }

    #[test]
    fn native_adapters_plan_without_touching_activation() {
        let (_settings, ctx) = test_ctx();
        for id in ["codex", "opencode", "pi", "omp", "zcode"] {
            let adapter = adapter_for(id).expect("native adapter");
            let snapshot = adapter.inspect(&ctx).expect("inspect");
            assert_eq!(snapshot.client_id, id);
            assert!(snapshot.activation.is_none());
            let plan = adapter
                .plan(ClientIntent::Connect, &ctx)
                .expect("plan");
            assert_eq!(plan.client_id, id);
            assert!(!plan.activation_touched, "{id} must not touch activation");
            assert_eq!(plan.restart_required, "none");
        }
    }
}
