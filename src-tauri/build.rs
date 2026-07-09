fn main() {
    let flavor = std::env::var("CODEXHUB_BUILD_FLAVOR").unwrap_or_else(|_| "stable".to_string());
    println!("cargo:rustc-env=CODEXHUB_BUILD_FLAVOR={flavor}");
    println!("cargo:rerun-if-env-changed=CODEXHUB_BUILD_FLAVOR");
    tauri_build::build()
}
