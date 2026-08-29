use crate::{
    config,
    file_transaction::{PreparedFileNamespace, PreparedTextFile},
    models, runtime_paths, Model,
};
use serde::Serialize;
use serde_json::Value;
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

const GENERATED_CATALOG_FILE: &str = "codexhub-model-catalog.json";
const GENERATED_STATE_FILE: &str = "codex-proxy-state.json";
const CODEX_TARGET_HOME_ENV: &str = "CODEXHUB_CODEX_TARGET_HOME";
const MAX_DIAGNOSTIC_COUNT: u64 = 100;
const PREPARED_CATALOG_FILES: &[(&str, bool)] = &[
    (GENERATED_CATALOG_FILE, false),
    ("codexhub-model-catalog-baseline.json", false),
    ("codexhub-model-catalog-overrides.json", false),
    (GENERATED_STATE_FILE, false),
    (".codexhub-catalog-owner-key", true),
];

#[derive(Debug, Clone, Default, Serialize, PartialEq, Eq)]
pub struct CatalogOverrideDiagnostics {
    pub accepted: u64,
    pub rejected: u64,
    pub migrated: u64,
    pub reasons: BTreeMap<String, u64>,
}

pub(crate) fn generate_catalog_with_existing_lock() -> Result<Vec<Model>, String> {
    sync_catalog_with_existing_lock()?;
    models::list_models()
}

pub(crate) fn sync_catalog_with_existing_lock() -> Result<String, String> {
    let paths = CatalogPaths::runtime()?;
    let python = config::find_python()?;
    let runner = ProcessCatalogSyncCommandRunner;

    sync_catalog_with_paths(&paths, &python, &runner)
}

pub(crate) struct PreparedCatalogPublication {
    files: Vec<PreparedTextFile>,
    catalog_payload: Value,
}

impl PreparedCatalogPublication {
    pub(crate) fn catalog_payload(&self) -> &Value {
        &self.catalog_payload
    }

    pub(crate) fn into_files(self) -> Vec<PreparedTextFile> {
        self.files
    }
}

/// Run discovery and catalog construction in an isolated runtime. The result
/// contains only bounded text replacements for the final commit gate; no
/// production cache, catalog, or Codex target file is written here.
pub(crate) fn prepare_catalog(
    overlays: &[PreparedTextFile],
) -> Result<PreparedCatalogPublication, String> {
    let actual = CatalogPaths::runtime()?;
    let python = config::find_python()?;
    prepare_catalog_with_paths(
        &actual,
        overlays,
        &python,
        &ProcessCatalogSyncCommandRunner,
    )
}

fn prepare_catalog_with_paths(
    actual: &CatalogPaths,
    overlays: &[PreparedTextFile],
    python: &Path,
    runner: &dyn CatalogSyncCommandRunner,
) -> Result<PreparedCatalogPublication, String> {
    let staging = CatalogStaging::new()?;
    let staged = CatalogPaths::new(
        staging.runtime_dir(),
        staging.target_dir(),
        actual.repo_root.clone(),
    );
    copy_catalog_inputs(actual, &staged)?;
    for overlay in overlays {
        let staged_path = staged_path_for_overlay(actual, &staged, overlay)?;
        crate::safe_file::write_text_atomic_with_mode(
            &staged_path,
            &overlay.text,
            overlay.unix_mode,
        )?;
    }

    sync_catalog_with_paths(&staged, python, runner)?;

    let staged_catalog_dir = staged.codex_dir.join("model-catalogs");
    let actual_catalog_dir = actual.codex_dir.join("model-catalogs");
    let mut files = overlays.to_vec();
    for (name, owner_only) in PREPARED_CATALOG_FILES {
        let text = fs::read_to_string(staged_catalog_dir.join(name))
            .map_err(|error| format!("failed to read prepared catalog output {name}: {error}"))?;
        let target = actual_catalog_dir.join(name);
        files.retain(|file| file.path != target);
        files.push(if *owner_only {
            PreparedTextFile::owner_only(target, text)
        } else {
            PreparedTextFile::new(target, text)
        });
    }
    let catalog_payload = serde_json::from_str(
        &fs::read_to_string(staged.generated_catalog_path()).map_err(|error| {
            format!("failed to read prepared Official context catalog: {error}")
        })?,
    )
    .map_err(|error| format!("failed to parse prepared Official context catalog: {error}"))?;
    Ok(PreparedCatalogPublication {
        files,
        catalog_payload,
    })
}

struct CatalogStaging {
    root: PathBuf,
}

impl CatalogStaging {
    fn new() -> Result<Self, String> {
        static NEXT_ID: AtomicU64 = AtomicU64::new(1);
        let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
        let root = std::env::temp_dir().join(format!(
            "codexhub-catalog-prepare-{}-{id}",
            std::process::id()
        ));
        fs::create_dir(&root)
            .map_err(|error| format!("failed to create catalog staging directory: {error}"))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).map_err(|error| {
                format!("failed to secure catalog staging directory: {error}")
            })?;
        }
        Ok(Self { root })
    }

    fn runtime_dir(&self) -> PathBuf {
        self.root.join("runtime")
    }

    fn target_dir(&self) -> PathBuf {
        self.root.join("target")
    }
}

impl Drop for CatalogStaging {
    fn drop(&mut self) {
        if let Err(error) = fs::remove_dir_all(&self.root) {
            log::warn!("failed to remove isolated catalog staging directory: {error}");
        }
    }
}

fn copy_catalog_inputs(actual: &CatalogPaths, staged: &CatalogPaths) -> Result<(), String> {
    for relative in [
        "model-catalogs/openai-plus-ollama-cloud.json",
        "model-catalogs/ollama-cloud.json",
        "model-catalogs/codexhub-model-catalog.json",
        "model-catalogs/codex-proxy-official-ollama.json",
        "model-catalogs/codexhub-model-catalog-baseline.json",
        "model-catalogs/codexhub-model-catalog-overrides.json",
        "model-catalogs/codex-proxy-state.json",
        "model-catalogs/.codexhub-catalog-owner-key",
        "proxy/settings.json",
        "proxy/config/providers.toml",
    ] {
        copy_regular_file_if_present(
            &actual.codex_dir.join(relative),
            &staged.codex_dir.join(relative),
        )?;
    }
    copy_regular_file_if_present(
        &actual.codex_target_dir.join("models_cache.json"),
        &staged.codex_target_dir.join("models_cache.json"),
    )
}

fn copy_regular_file_if_present(source: &Path, target: &Path) -> Result<(), String> {
    let metadata = match fs::symlink_metadata(source) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(format!("failed to inspect catalog staging input: {error}")),
    };
    if !metadata.file_type().is_file() {
        return Err("catalog staging input is not a regular file".to_string());
    }
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("failed to create catalog staging input directory: {error}"))?;
    }
    fs::copy(source, target)
        .map(|_| ())
        .map_err(|error| format!("failed to copy catalog staging input: {error}"))
}

fn staged_path_for_overlay(
    actual: &CatalogPaths,
    staged: &CatalogPaths,
    overlay: &PreparedTextFile,
) -> Result<PathBuf, String> {
    let (actual_root, staged_root) = match overlay.namespace {
        PreparedFileNamespace::Runtime => (&actual.codex_dir, &staged.codex_dir),
        PreparedFileNamespace::CodexTarget => {
            (&actual.codex_target_dir, &staged.codex_target_dir)
        }
        PreparedFileNamespace::Absolute => {
            return Err(
                "prepared catalog overlay is missing an explicit runtime or Codex target namespace"
                    .to_string(),
            )
        }
    };
    let relative = overlay.path.strip_prefix(actual_root).map_err(|_| {
        "prepared catalog overlay is outside its declared managed root".to_string()
    })?;
    Ok(staged_root.join(relative))
}

/// Read the bounded catalog ownership diagnostics emitted by Python sync.
///
/// The state file is an implementation detail and may be missing while a
/// first-run sync is in progress.  A malformed or unknown payload therefore
/// degrades to an empty diagnostic set instead of turning a successful catalog
/// publication into a UI error.  Only the whitelisted counters/reasons cross
/// the Rust/frontend boundary.
pub fn catalog_override_diagnostics() -> Result<CatalogOverrideDiagnostics, String> {
    let paths = CatalogPaths::runtime()?;
    Ok(read_catalog_override_diagnostics(
        &paths.catalog_state_path(),
    ))
}

fn read_catalog_override_diagnostics(path: &Path) -> CatalogOverrideDiagnostics {
    let Ok(text) = fs::read_to_string(path) else {
        return CatalogOverrideDiagnostics::default();
    };
    let Ok(payload) = serde_json::from_str::<Value>(&text) else {
        return CatalogOverrideDiagnostics::default();
    };
    let Some(diagnostics) = payload.get("catalog_override_diagnostics") else {
        return CatalogOverrideDiagnostics::default();
    };
    let Some(object) = diagnostics.as_object() else {
        return CatalogOverrideDiagnostics::default();
    };
    let mut result = CatalogOverrideDiagnostics {
        accepted: bounded_count(object.get("accepted")),
        rejected: bounded_count(object.get("rejected")),
        migrated: bounded_count(object.get("migrated")),
        reasons: BTreeMap::new(),
    };
    let Some(reasons) = object.get("reasons").and_then(Value::as_object) else {
        return result;
    };
    for reason in [
        "invalid_sidecar",
        "invalid_catalog",
        "invalid_baseline",
        "invalid_row_identity",
        "invalid_override_fields",
        "missing_managed_model",
    ] {
        let count = bounded_count(reasons.get(reason));
        if count > 0 {
            result.reasons.insert(reason.to_string(), count);
        }
    }
    result
}

fn bounded_count(value: Option<&Value>) -> u64 {
    value
        .and_then(Value::as_u64)
        .map(|count| count.min(MAX_DIAGNOSTIC_COUNT))
        .unwrap_or(0)
}

fn sync_catalog_with_paths(
    paths: &CatalogPaths,
    python: &Path,
    runner: &dyn CatalogSyncCommandRunner,
) -> Result<String, String> {
    let catalog_path = paths.generated_catalog_path();
    if let Some(parent) = catalog_path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            format!(
                "failed to create catalog output directory {}: {error}",
                parent.display()
            )
        })?;
    }

    let args = vec![
        paths.catalog_sync_script().to_string_lossy().into_owned(),
        "--sync".to_string(),
    ];
    let env = vec![
        ("CODEX_HOME".to_string(), paths.codex_dir.clone()),
        (
            CODEX_TARGET_HOME_ENV.to_string(),
            paths.codex_target_dir.clone(),
        ),
    ];
    let outcome = runner
        .run(python, &args, &env)
        .map_err(|error| format!("catalog sync failed to start: {error}"))?;

    if outcome.code != Some(0) {
        return Err(config::format_command_failure(
            "catalog sync",
            python,
            &args,
            &outcome,
        ));
    }

    Ok(catalog_path.to_string_lossy().into_owned())
}

#[derive(Debug, Clone)]
struct CatalogPaths {
    codex_dir: PathBuf,
    codex_target_dir: PathBuf,
    repo_root: PathBuf,
}

impl CatalogPaths {
    fn runtime() -> Result<Self, String> {
        let codex_dir = runtime_paths::codex_home_dir()?;
        let codex_target_dir = runtime_paths::codex_target_home_dir()?;
        let repo_root = runtime_paths::resource_root()?;

        Ok(Self::new(codex_dir, codex_target_dir, repo_root))
    }

    fn new(
        codex_dir: impl Into<PathBuf>,
        codex_target_dir: impl Into<PathBuf>,
        repo_root: impl Into<PathBuf>,
    ) -> Self {
        Self {
            codex_dir: codex_dir.into(),
            codex_target_dir: codex_target_dir.into(),
            repo_root: repo_root.into(),
        }
    }

    fn catalog_sync_script(&self) -> PathBuf {
        self.repo_root.join("src-python").join("catalog_sync.py")
    }

    fn generated_catalog_path(&self) -> PathBuf {
        self.codex_dir
            .join("model-catalogs")
            .join(GENERATED_CATALOG_FILE)
    }

    fn catalog_state_path(&self) -> PathBuf {
        self.codex_dir
            .join("model-catalogs")
            .join(GENERATED_STATE_FILE)
    }
}

type CatalogCommandOutcome = config::CommandOutcome;

trait CatalogSyncCommandRunner {
    fn run(
        &self,
        program: &Path,
        args: &[String],
        env: &[(String, PathBuf)],
    ) -> Result<CatalogCommandOutcome, String>;
}

struct ProcessCatalogSyncCommandRunner;

impl CatalogSyncCommandRunner for ProcessCatalogSyncCommandRunner {
    fn run(
        &self,
        program: &Path,
        args: &[String],
        env: &[(String, PathBuf)],
    ) -> Result<CatalogCommandOutcome, String> {
        let mut command = runtime_paths::configured_python_command(program);
        command.args(args);
        for (name, value) in env {
            command.env(name, value);
        }
        config::configure_no_window(&mut command);

        let output = command
            .output()
            .map_err(|error| format!("failed to start {}: {error}", program.display()))?;

        Ok(CatalogCommandOutcome {
            code: output.status.code(),
            stdout: String::from_utf8_lossy(&output.stdout).to_string(),
            stderr: String::from_utf8_lossy(&output.stderr).to_string(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::{
        prepare_catalog_with_paths, read_catalog_override_diagnostics, sync_catalog_with_paths,
        CatalogCommandOutcome, CatalogOverrideDiagnostics, CatalogPaths,
        CatalogSyncCommandRunner, CODEX_TARGET_HOME_ENV, GENERATED_CATALOG_FILE,
    };
    use crate::file_transaction::PreparedTextFile;
    use std::cell::RefCell;
    use std::collections::BTreeMap;
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn sync_catalog_preserves_runtime_and_target_homes_and_returns_runtime_catalog_path() {
        let root = temp_root("catalog-sync");
        let runtime_home = root.join("runtime-home");
        let target_home = root.join("codex-target-home");
        let repo_root = root.join("repo-root");
        let paths = CatalogPaths::new(&runtime_home, &target_home, &repo_root);
        write_fake_catalog_script(&repo_root);
        let catalog_path = runtime_home
            .join("model-catalogs")
            .join(GENERATED_CATALOG_FILE);
        let runner = RecordingCatalogRunner::successful("catalog written\n", catalog_path.clone());

        let result = sync_catalog_with_paths(&paths, Path::new("python-test"), &runner)
            .expect("catalog sync");

        assert_eq!(result, catalog_path.to_string_lossy().into_owned());
        assert!(catalog_path.parent().unwrap().is_dir());
        assert!(!result.contains(
            &repo_root
                .join("model-catalogs")
                .to_string_lossy()
                .to_string()
        ));

        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 1);
        assert_eq!(commands[0].program, PathBuf::from("python-test"));
        assert_eq!(
            commands[0].args,
            vec![
                repo_root
                    .join("src-python")
                    .join("catalog_sync.py")
                    .to_string_lossy()
                    .into_owned(),
                "--sync".to_string()
            ]
        );
        assert_eq!(commands[0].env.get("CODEX_HOME"), Some(&runtime_home));
        assert_eq!(
            commands[0].env.get(CODEX_TARGET_HOME_ENV),
            Some(&target_home)
        );
        assert_ne!(
            commands[0].env.get("CODEX_HOME"),
            commands[0].env.get(CODEX_TARGET_HOME_ENV)
        );
    }

    #[test]
    fn sync_catalog_failure_includes_command_stdout_and_stderr() {
        let root = temp_root("catalog-failure");
        let repo_root = root.join("repo-root");
        let paths = test_paths(&root);
        write_fake_catalog_script(&repo_root);
        let runner = RecordingCatalogRunner::failed(19, "printed stdout", "printed stderr");

        let error = sync_catalog_with_paths(&paths, Path::new("python-test"), &runner)
            .expect_err("catalog sync should fail");

        assert!(error.contains("catalog sync failed"));
        assert!(error.contains("exit code 19"));
        assert!(error.contains("command: python-test"));
        assert!(error.contains("catalog_sync.py"));
        assert!(error.contains("--sync"));
        assert!(error.contains("printed stdout"));
        assert!(error.contains("printed stderr"));
    }

    #[test]
    fn catalog_preparation_uses_isolated_overlays_and_does_not_touch_runtime_files() {
        let root = temp_root("catalog-prepare-isolated");
        let actual = test_paths(&root);
        let actual_catalog = actual.generated_catalog_path();
        let actual_seed = actual
            .codex_dir
            .join("model-catalogs/openai-plus-ollama-cloud.json");
        fs::create_dir_all(actual_catalog.parent().unwrap()).unwrap();
        fs::write(&actual_catalog, "old-catalog\n").unwrap();
        fs::write(&actual_seed, "old-seed\n").unwrap();
        let overlays = vec![PreparedTextFile::runtime(
            actual_seed.clone(),
            "prepared-seed\n".to_string(),
        )];
        let runner = PreparingCatalogRunner;

        let prepared = prepare_catalog_with_paths(
            &actual,
            &overlays,
            Path::new("python-test"),
            &runner,
        )
        .expect("isolated catalog preparation");

        assert_eq!(fs::read_to_string(&actual_catalog).unwrap(), "old-catalog\n");
        assert_eq!(fs::read_to_string(&actual_seed).unwrap(), "old-seed\n");
        assert_eq!(
            prepared.catalog_payload()["models"][0]["slug"],
            "gpt-5.6-luna"
        );
        let prepared_catalog = prepared
            .into_files()
            .into_iter()
            .find(|file| file.path == actual_catalog)
            .expect("prepared generated catalog");
        assert!(prepared_catalog.text.contains("gpt-5.6-luna"));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn catalog_preparation_keeps_runtime_and_target_overlays_distinct_when_roots_match() {
        let root = temp_root("catalog-prepare-shared-root");
        let shared_home = root.join("codex-home");
        let actual = CatalogPaths::new(&shared_home, &shared_home, root.join("repo-root"));
        let seed = shared_home.join("model-catalogs/openai-plus-ollama-cloud.json");
        let native_cache = shared_home.join("models_cache.json");
        fs::create_dir_all(seed.parent().unwrap()).unwrap();
        fs::write(&seed, "old-seed\n").unwrap();
        fs::write(&native_cache, "old-native-cache\n").unwrap();
        let overlays = vec![
            PreparedTextFile::runtime(seed.clone(), "prepared-seed\n".to_string()),
            PreparedTextFile::codex_target_owner_only(
                native_cache.clone(),
                "prepared-native-cache\n".to_string(),
            ),
        ];

        let prepared = prepare_catalog_with_paths(
            &actual,
            &overlays,
            Path::new("python-test"),
            &SharedRootPreparingCatalogRunner,
        )
        .expect("shared-root catalog preparation");

        assert_eq!(fs::read_to_string(&seed).unwrap(), "old-seed\n");
        assert_eq!(
            fs::read_to_string(&native_cache).unwrap(),
            "old-native-cache\n"
        );
        assert_eq!(
            prepared.catalog_payload()["models"][0]["slug"],
            "gpt-5.6-luna"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn catalog_override_diagnostics_are_bounded_and_whitelisted() {
        let root = temp_root("catalog-diagnostics");
        let path = root.join("codex-proxy-state.json");
        fs::write(
            &path,
            r#"{
                "catalog_override_diagnostics": {
                    "accepted": 999,
                    "rejected": 2,
                    "migrated": 1,
                    "reasons": {
                        "invalid_row_identity": 101,
                        "unknown": 77
                    }
                }
            }"#,
        )
        .unwrap();

        let diagnostics = read_catalog_override_diagnostics(&path);
        assert_eq!(diagnostics.accepted, 100);
        assert_eq!(diagnostics.rejected, 2);
        assert_eq!(diagnostics.migrated, 1);
        assert_eq!(diagnostics.reasons.get("invalid_row_identity"), Some(&100));
        assert!(!diagnostics.reasons.contains_key("unknown"));
    }

    #[test]
    fn malformed_or_missing_catalog_override_diagnostics_are_empty() {
        let root = temp_root("catalog-diagnostics-malformed");
        let missing = read_catalog_override_diagnostics(&root.join("missing.json"));
        assert_eq!(missing, CatalogOverrideDiagnostics::default());

        let malformed_path = root.join("malformed.json");
        fs::write(&malformed_path, "not json").unwrap();
        assert_eq!(
            read_catalog_override_diagnostics(&malformed_path),
            CatalogOverrideDiagnostics::default()
        );
    }

    #[derive(Debug, Clone)]
    struct RecordedCatalogCommand {
        program: PathBuf,
        args: Vec<String>,
        env: BTreeMap<String, PathBuf>,
    }

    struct RecordingCatalogRunner {
        commands: RefCell<Vec<RecordedCatalogCommand>>,
        outcome: CatalogCommandOutcome,
        expected_catalog_parent: Option<PathBuf>,
    }

    impl RecordingCatalogRunner {
        fn successful(stdout: &str, expected_catalog_path: PathBuf) -> Self {
            Self {
                commands: RefCell::new(Vec::new()),
                outcome: CatalogCommandOutcome {
                    code: Some(0),
                    stdout: stdout.to_string(),
                    stderr: String::new(),
                },
                expected_catalog_parent: expected_catalog_path.parent().map(Path::to_path_buf),
            }
        }

        fn failed(code: i32, stdout: &str, stderr: &str) -> Self {
            Self {
                commands: RefCell::new(Vec::new()),
                outcome: CatalogCommandOutcome {
                    code: Some(code),
                    stdout: stdout.to_string(),
                    stderr: stderr.to_string(),
                },
                expected_catalog_parent: None,
            }
        }
    }

    impl CatalogSyncCommandRunner for RecordingCatalogRunner {
        fn run(
            &self,
            program: &Path,
            args: &[String],
            env: &[(String, PathBuf)],
        ) -> Result<CatalogCommandOutcome, String> {
            if let Some(expected_catalog_parent) = &self.expected_catalog_parent {
                assert!(
                    expected_catalog_parent.is_dir(),
                    "catalog output directory should exist before sync runs"
                );
            }
            self.commands.borrow_mut().push(RecordedCatalogCommand {
                program: program.to_path_buf(),
                args: args.to_vec(),
                env: env.iter().cloned().collect(),
            });
            Ok(self.outcome.clone())
        }
    }

    struct PreparingCatalogRunner;

    impl CatalogSyncCommandRunner for PreparingCatalogRunner {
        fn run(
            &self,
            _program: &Path,
            _args: &[String],
            env: &[(String, PathBuf)],
        ) -> Result<CatalogCommandOutcome, String> {
            let env = env.iter().cloned().collect::<BTreeMap<_, _>>();
            let runtime = env.get("CODEX_HOME").expect("staged runtime");
            let catalog_dir = runtime.join("model-catalogs");
            assert_eq!(
                fs::read_to_string(catalog_dir.join("openai-plus-ollama-cloud.json")).unwrap(),
                "prepared-seed\n"
            );
            for (name, text) in [
                (
                    "codexhub-model-catalog.json",
                    r#"{"models":[{"slug":"gpt-5.6-luna"}]}"#,
                ),
                ("codexhub-model-catalog-baseline.json", r#"{"models":[]}"#),
                ("codexhub-model-catalog-overrides.json", r#"{"overrides":[]}"#),
                ("codex-proxy-state.json", r#"{"visible_models":[]}"#),
                (
                    ".codexhub-catalog-owner-key",
                    "0000000000000000000000000000000000000000000000000000000000000000",
                ),
            ] {
                fs::write(catalog_dir.join(name), format!("{text}\n")).unwrap();
            }
            Ok(CatalogCommandOutcome {
                code: Some(0),
                stdout: "prepared".to_string(),
                stderr: String::new(),
            })
        }
    }

    struct SharedRootPreparingCatalogRunner;

    impl CatalogSyncCommandRunner for SharedRootPreparingCatalogRunner {
        fn run(
            &self,
            _program: &Path,
            _args: &[String],
            env: &[(String, PathBuf)],
        ) -> Result<CatalogCommandOutcome, String> {
            let env = env.iter().cloned().collect::<BTreeMap<_, _>>();
            let runtime = env.get("CODEX_HOME").expect("staged runtime");
            let target = env
                .get(CODEX_TARGET_HOME_ENV)
                .expect("staged Codex target");
            assert_ne!(runtime, target);
            assert_eq!(
                fs::read_to_string(
                    runtime.join("model-catalogs/openai-plus-ollama-cloud.json")
                )
                .unwrap(),
                "prepared-seed\n"
            );
            assert_eq!(
                fs::read_to_string(target.join("models_cache.json")).unwrap(),
                "prepared-native-cache\n"
            );
            let catalog_dir = runtime.join("model-catalogs");
            for (name, text) in [
                (
                    "codexhub-model-catalog.json",
                    r#"{"models":[{"slug":"gpt-5.6-luna"}]}"#,
                ),
                ("codexhub-model-catalog-baseline.json", r#"{"models":[]}"#),
                ("codexhub-model-catalog-overrides.json", r#"{"overrides":[]}"#),
                ("codex-proxy-state.json", r#"{"visible_models":[]}"#),
                (
                    ".codexhub-catalog-owner-key",
                    "0000000000000000000000000000000000000000000000000000000000000000",
                ),
            ] {
                fs::write(catalog_dir.join(name), format!("{text}\n")).unwrap();
            }
            Ok(CatalogCommandOutcome {
                code: Some(0),
                stdout: "prepared".to_string(),
                stderr: String::new(),
            })
        }
    }

    fn test_paths(root: &Path) -> CatalogPaths {
        CatalogPaths::new(
            root.join("runtime-home"),
            root.join("codex-target-home"),
            root.join("repo-root"),
        )
    }

    fn write_fake_catalog_script(repo_root: &Path) {
        let script = repo_root.join("src-python").join("catalog_sync.py");
        fs::create_dir_all(script.parent().unwrap()).unwrap();
        fs::write(script, "# fake catalog sync").unwrap();
    }

    fn temp_root(name: &str) -> PathBuf {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "codexhub-catalog-{name}-{}-{suffix}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&path);
        fs::create_dir_all(&path).unwrap();
        path
    }
}
