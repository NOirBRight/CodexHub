# Python runtime contract

CodexHub source requires **Python 3.13 or newer**. The source uses Python
3.13 syntax, so a 3.11 interpreter fails during collection before any test
can run.

The recurring failure is an environment-resolution problem, not a Gateway
protocol problem: the interactive Codex shell prepends the Hermes virtual
environment, so bare `python` and `pytest` resolve to Hermes Python 3.11 even
though the machine also has Python 3.13. Rust test fixtures that spawned
`Command::new("python")` reproduced the same mismatch in nested processes.

Use the repository entrypoint from the repository root:

```powershell
.\scripts\codexhub-python.cmd -m pytest -q --ignore=tests/test_real_client_e2e.py
.\scripts\codexhub-python.cmd scripts/report_quality_gates.py
```

The entrypoint uses `Resolve-CodexHubPython.ps1`, validates the selected
interpreter before running anything, and exports the selected path through
`CODEXHUB_PYTHON`, `CODEXHUB_PROXY_PYTHON`, and `CODEXHUB_E2E_PYTHON` for child
processes. These are hard bindings; when `CODEXHUB_E2E_PYTHON` is present, it
takes precedence at nested resolver/Rust boundaries so an isolated E2E child
cannot select another host interpreter. Gateway, packaging, and E2E PowerShell entrypoints use the same resolver in
bundled-preferred mode; the development wrapper also accepts a checkout
virtualenv before host discovery. Rust applies the same 3.13 probe to
configured, bundled, checkout, and host candidates. An explicit override is a
hard choice: an incompatible `CODEXHUB_PYTHON` or `CODEXHUB_PROXY_PYTHON`
fails closed and never falls through to another interpreter. A 3.11 ambient
interpreter is never accepted as a fallback. The wrapper also puts the chosen
interpreter directory and the repository `scripts` directory first on `PATH`.
That makes nested literal `python` calls use the same interpreter, while
`scripts/pytest.cmd` forwards nested `pytest` calls to `python -m pytest` rather
than the ambient 3.11 executable.
Every Rust-to-Python launch and the Rust version probes also pass through
`runtime_paths::configure_python_command`, which removes `PYTHONHOME` and the
activated-environment selectors at the child boundary. This prevents a valid
3.13 executable from being redirected to a Hermes/Conda/Pipenv prefix in
Catalog, Config, History, Model discovery, or Gateway paths.
When the command is `-m pytest`, the resolver also verifies that pytest is
installed in that exact interpreter; otherwise it fails before collection with
the command needed to install it instead of silently switching to another
3.13 environment.

If direct `python` and `pytest` commands are needed in an interactive
PowerShell session, activate the contract once by dot-sourcing:

```powershell
. .\scripts\Enter-CodexHubPython.ps1
```

Running that script without the leading dot only changes its child process;
PowerShell cannot let a child process rewrite its parent's environment.

The isolated Windows real-client runner passes an explicit Python path only
to a child that is expected to spawn Python. Fixture `.cmd` files use the
validated repository 3.13 interpreter; a native candidate or managed-client
materializer must use `<artifact>\python\python.exe` from its own packaged
directory. The runner probes that bundled executable and compares the
listening Gateway process's actual `MainModule.FileName` with the expected
path. Native clients and packaged desktop processes do not receive the host
Python override, so a test shell's Hermes 3.11 environment cannot silently
replace the artifact runtime.

For an explicit override:

```powershell
$env:CODEXHUB_PYTHON = 'C:\path\to\Python313\python.exe'
.\scripts\codexhub-python.cmd -m pytest -q
```

The resolver fails immediately if that override is not Python 3.13+, instead
of allowing a later `SyntaxError` to obscure the cause. Packaged builds use
the bundled runtime and do not depend on the user's PATH. The launch boundary
also removes `PYTHONHOME`, `PYTHONSTARTUP`, `VIRTUAL_ENV`, Conda, and Pipenv
runtime selectors before spawning Python; a selected executable must not inherit
a different environment prefix from the host shell.

The two source executables that are commonly launched directly,
`src-python/codex_proxy.py` and `src-python/catalog_sync.py`, also fail before
importing the 3.13-only provider module when an ambient 3.11 interpreter is
used. This turns a manual mis-invocation into the same actionable contract
error rather than another misleading syntax/import failure.
