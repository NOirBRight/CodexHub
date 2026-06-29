use crate::{AppStatus, Provider, Settings};

const SCAFFOLD_ONLY: &str =
    "CodexHub Tauri scaffold only; config persistence is implemented in Task 7";

pub fn get_providers() -> Result<Vec<Provider>, String> {
    Ok(Vec::new())
}

pub fn save_providers(providers: Vec<Provider>) -> Result<Vec<Provider>, String> {
    Ok(providers)
}

pub fn get_settings() -> Result<Settings, String> {
    Ok(Settings::default())
}

pub fn save_settings(settings: Settings) -> Result<Settings, String> {
    Ok(settings)
}

pub fn switch_mode(mode: &str) -> Result<AppStatus, String> {
    match mode {
        "official" | "custom" => Err(format!("{SCAFFOLD_ONLY}; requested mode: {mode}")),
        _ => Err(format!(
            "unsupported mode: {mode}; expected official or custom"
        )),
    }
}
