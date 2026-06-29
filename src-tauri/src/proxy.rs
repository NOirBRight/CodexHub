use crate::AppStatus;

const SCAFFOLD_ONLY: &str =
    "CodexHub Tauri scaffold only; proxy lifecycle is implemented in Task 8";

pub fn status() -> Result<AppStatus, String> {
    Ok(AppStatus::scaffold(
        "Proxy status placeholder: lifecycle checks are implemented in Task 8",
    ))
}

pub fn start() -> Result<AppStatus, String> {
    Err(format!("{SCAFFOLD_ONLY}; start was not attempted"))
}

pub fn stop() -> Result<AppStatus, String> {
    Err(format!("{SCAFFOLD_ONLY}; stop was not attempted"))
}

pub fn restart() -> Result<AppStatus, String> {
    Err(format!("{SCAFFOLD_ONLY}; restart was not attempted"))
}
