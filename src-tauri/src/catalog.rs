use crate::{models, Model};

const SCAFFOLD_ONLY: &str =
    "CodexHub Tauri scaffold only; catalog subprocess sync is implemented in Task 10";

pub fn generate_catalog() -> Result<Vec<Model>, String> {
    models::generate_catalog()
}

pub fn sync_catalog() -> Result<String, String> {
    Err(format!("{SCAFFOLD_ONLY}; catalog sync was not attempted"))
}
