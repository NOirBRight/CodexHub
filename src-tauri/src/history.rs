const SCAFFOLD_ONLY: &str =
    "CodexHub Tauri scaffold only; history subprocess sync is implemented in Task 10";

pub fn sync_history(target_provider: Option<&str>) -> Result<String, String> {
    let target = target_provider.unwrap_or("current");
    Err(format!(
        "{SCAFFOLD_ONLY}; history sync was not attempted for {target}"
    ))
}
