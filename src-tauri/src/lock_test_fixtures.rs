//! Shared fixtures for stale-lock recovery tests.
//!
//! Kept out of `safe_file.rs` because that file must compile standalone via
//! `rustc --test` in the safe_file Linux CI job; it keeps its own inline copy.

use std::{fs, path::Path};

/// Keeps the dead child's handle open so the PID stays resolvable on
/// Windows for the test duration; reaps the (already exited) child on drop.
pub(crate) struct DeadChildGuard(std::process::Child);

impl Drop for DeadChildGuard {
    fn drop(&mut self) {
        let _ = self.0.wait();
    }
}

/// Write a legacy record whose PID is provably dead (recoverable).
/// The returned guard must stay alive for the test duration: dropping its
/// handle would make the dead PID unresolvable on Windows.
pub(crate) fn write_dead_legacy_lock(lock: &Path) -> DeadChildGuard {
    let mut child = std::process::Command::new("python")
        .arg("-c")
        .arg("pass")
        .spawn()
        .expect("python is required for stale lock fixtures");
    let pid = child.id();
    assert!(child.wait().expect("wait dead child").success());
    fs::write(lock, format!("pid={pid}\nacquired_at_millis=0\n")).expect("write stale lock");
    DeadChildGuard(child)
}
