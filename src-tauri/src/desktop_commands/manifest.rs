//! Public re-exports for the Rust-owned Desktop Command registry.
//!
//! The implementation lives in the parent module so the command rows are the
//! one source of truth for Tauri, Web Bridge, and frontend contract metadata.

#[allow(unused_imports)]
pub use super::{
    bridge_exposed_names, command_manifest_json, command_meta, command_names,
    frontend_exposed_names, parse_command, Command, CommandMeta, COMMANDS,
};
