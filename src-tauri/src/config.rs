use crate::{AppStatus, Provider, Settings};

const SCAFFOLD_ONLY: &str =
    "CodexHub Tauri scaffold only; config persistence is implemented in Task 7";

pub fn get_providers() -> Result<Vec<Provider>, String> {
    Ok(Vec::new())
}

pub fn save_providers(_providers: Vec<Provider>) -> Result<Vec<Provider>, String> {
    Err(format!(
        "{SCAFFOLD_ONLY}; provider config persistence is not implemented until Task 7"
    ))
}

pub fn get_settings() -> Result<Settings, String> {
    Ok(Settings::default())
}

pub fn save_settings(_settings: Settings) -> Result<Settings, String> {
    Err(format!(
        "{SCAFFOLD_ONLY}; settings persistence is not implemented until Task 7"
    ))
}

pub fn switch_mode(mode: &str) -> Result<AppStatus, String> {
    match mode {
        "official" | "custom" => Err(format!("{SCAFFOLD_ONLY}; requested mode: {mode}")),
        _ => Err(format!(
            "unsupported mode: {mode}; expected official or custom"
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::{save_providers, save_settings};
    use crate::Settings;

    #[test]
    fn save_providers_fails_closed_until_task_7() {
        let error = save_providers(Vec::new()).expect_err("save should fail closed");

        assert!(error.contains("Task 7"));
        assert!(error.contains("not implemented"));
    }

    #[test]
    fn save_settings_fails_closed_until_task_7() {
        let error = save_settings(Settings::default()).expect_err("save should fail closed");

        assert!(error.contains("Task 7"));
        assert!(error.contains("not implemented"));
    }
}
