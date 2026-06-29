const SCAFFOLD_ONLY: &str =
    "CodexHub Tauri scaffold only; OS autostart registration is implemented in Task 11";

pub fn set_autostart(enabled: bool) -> Result<String, String> {
    Err(format!(
        "{SCAFFOLD_ONLY}; autostart change was not attempted (enabled={enabled})"
    ))
}

pub fn remove_autostart() -> Result<String, String> {
    Err(format!(
        "{SCAFFOLD_ONLY}; autostart removal was not attempted"
    ))
}
