use crate::Model;

const SCAFFOLD_ONLY: &str =
    "CodexHub Tauri scaffold only; model discovery is implemented in Task 9";

pub fn refresh_official_models() -> Result<Vec<Model>, String> {
    Err(format!("{SCAFFOLD_ONLY}; no OpenAI network call was made"))
}

pub fn discover_provider_models(provider_id: &str) -> Result<Vec<Model>, String> {
    Err(format!(
        "{SCAFFOLD_ONLY}; provider discovery was not attempted for {provider_id}"
    ))
}

pub fn list_models() -> Result<Vec<Model>, String> {
    Ok(Vec::new())
}
