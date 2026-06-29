const SCAFFOLD_ONLY: &str =
    "CodexHub Tauri scaffold only; catalog subprocess sync is implemented in Task 10";

pub fn generate_catalog() -> Result<String, String> {
    Err(format!(
        "{SCAFFOLD_ONLY}; catalog generation was not attempted"
    ))
}

pub fn sync_catalog() -> Result<String, String> {
    Err(format!("{SCAFFOLD_ONLY}; catalog sync was not attempted"))
}
