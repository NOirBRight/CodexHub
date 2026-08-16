# Python runtime contract

CodexHub's runtime contract requires **Python 3.13 or newer**. The source
contains PEP 695 type-parameter syntax (introduced in Python 3.12), so a
3.11 interpreter fails during collection before any test can run. We still
standardize on 3.13+ for development, packaged Gateway, and release tooling;
3.13 is the product/runtime floor, not a claim that every syntax feature is
unique to 3.13.

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
processes. It also replaces ambient `PYTHONPATH` with this checkout's
`src-python` import root. These are hard bindings; when `CODEXHUB_E2E_PYTHON` is present, it
takes precedence at nested resolver/Rust boundaries so an isolated E2E child
cannot select another host interpreter. Gateway, packaging, and E2E PowerShell
entrypoints use the resolver in bundled-preferred mode. The repository source
launcher deliberately uses the checkout or host 3.13 environment for every
script, not the embedded runtime: a script can invoke pytest through
`sys.executable`, while the embedded application runtime intentionally does
not contain development modules. Rust applies the same 3.13 probe to
configured, bundled, checkout, and host candidates. An explicit override is a
hard choice: an incompatible `CODEXHUB_PYTHON` or `CODEXHUB_PROXY_PYTHON`
fails closed and never falls through to another interpreter. A 3.11 ambient
interpreter is never accepted as a fallback. The wrapper also puts the chosen
interpreter directory and the repository `scripts` directory first on `PATH`.
That makes nested literal `python` calls use the same interpreter, while
`scripts/pytest.cmd` forwards nested `pytest` calls to `python -m pytest` rather
than the ambient 3.11 executable.
Every Rust-to-Python launch and the Rust version probes also pass through
`runtime_paths::configured_python_command` (or its underlying
`configure_python_command` boundary), which removes `PYTHONHOME` and the
activated-environment selectors, including ambient `PYTHONPATH`, at the child
boundary. This prevents a valid 3.13 executable from being redirected to a
Hermes/Conda/Pipenv prefix or importing host modules in Catalog, Config,
History, Model discovery, Gateway, and cross-language lock-test paths.
The constructor is shared by production runners and Rust subprocess fixtures,
so a newly added Python child cannot accidentally omit the environment
sanitisation step.
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
to a child that is expected to spawn Python. Fixture `.cmd` files require the
validated repository 3.13 interpreter through `CODEXHUB_E2E_PYTHON`; they do
not fall back to a copied candidate runtime or ambient `PATH`. A native
candidate or managed-client materializer must use
`<artifact>\python\python.exe` from its own packaged directory. The runner
probes that bundled executable and compares the
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

The packaged-runtime preparation script is compatible with both Windows
PowerShell 5.1 and PowerShell 7. Its version preflight deliberately uses a
quote-free numeric Python probe, because Windows PowerShell 5.1 can rewrite
embedded quotes in native `-c` arguments. To check an existing packaged runtime
without downloading or changing anything, run:

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File .\scripts\Prepare-PythonRuntime.ps1 -CheckOnly
```

The same command may be run with `pwsh.exe`; both must select the bundled
3.13.x runtime. A build failure in this preflight is a runtime-boundary issue,
not a reason to retry with the ambient `python` or `pytest` command. The
preparation script also removes host Python selectors before checking or
extracting the embedded runtime, so an activated 3.11 shell cannot contaminate
the package.

Every directly executable Python entrypoint under `src-python/`, `scripts/`,
the checked-in evidence validators under `tests/`, and the E2E fixture Python
files runs a small preflight before importing production or fixture logic.
The canonical preflight lives in `src-python/python_runtime_contract.py`; the
scripts-side module is only a compatibility import for it. This includes
catalog/config/history utilities and evidence tools, not just the Gateway.
Therefore a direct ambient-3.11 invocation fails with the actionable contract
error instead of partially running, mutating state, or reaching a later
syntax/import failure. The checked-in entrypoint audit and runtime tests keep
new direct scripts from reopening this boundary. Fixture scripts additionally
reject an invocation without the runner's exact interpreter binding, so a
second Python 3.13 installation cannot silently replace the selected runtime.
