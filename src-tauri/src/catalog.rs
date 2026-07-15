use crate::{config, models, runtime_paths, Model};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

const GENERATED_CATALOG_FILE: &str = "codexhub-model-catalog.json";
const CODEX_TARGET_HOME_ENV: &str = "CODEXHUB_CODEX_TARGET_HOME";

pub fn generate_catalog() -> Result<Vec<Model>, String> {
    models::generate_catalog()
}

pub fn sync_catalog() -> Result<String, String> {
    let paths = CatalogPaths::runtime()?;
    let python = config::find_python();
    let runner = ProcessCatalogSyncCommandRunner;

    sync_catalog_with_paths(&paths, &python, &runner)
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
        let mut command = Command::new(program);
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
        sync_catalog_with_paths, CatalogCommandOutcome, CatalogPaths, CatalogSyncCommandRunner,
        CODEX_TARGET_HOME_ENV, GENERATED_CATALOG_FILE,
    };
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
