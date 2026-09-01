"""Deep Modules v4 closeout gates: no lookup/api/__getattr__, no RelaySymbols, ratcheted sizes."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PYTHON = REPO_ROOT / "src-python"
COMPAT_ROOT = SRC_PYTHON / "gateway_compat"

# Measured sizes + 10%, pinned so the files cannot grow back to the 3000 cap.
HANDLER_IMPL_LINE_BUDGET = 1972  # 1812 <= 1972 (handler did not need a raise)
RELAY_LINE_BUDGET = 2986  # 2715 * 1.10
EXCHANGE_LINE_BUDGET = 1040  # policy moved into owning module (was 689)
BINDINGS_LINE_BUDGET = 310  # 282 * 1.10
PORTS_LINE_BUDGET = 130  # gateway_exchange_ports.py
ADAPTERS_LINE_BUDGET = 300  # gateway_exchange_adapters.py
DISPATCH_LINE_BUDGET = 920  # 837 * 1.10


def _line_count(path: Path) -> int:
    source = path.read_text(encoding="utf-8")
    return source.count("\n") + (0 if source.endswith("\n") else 1)


def _module_level_names(tree: ast.AST, kind: type[ast.AST], name: str) -> list[int]:
    hits: list[int] = []
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, kind) and getattr(node, "name", None) == name:
            hits.append(getattr(node, "lineno", 0))
    return hits


def lookup_api_getattr_violations(source: str, *, filename: str = "<memory>") -> list[str]:
    """Flag the deleted gateway_compat lookup/api/__getattr__ surface."""

    tree = ast.parse(source, filename=filename)
    violations: list[str] = []
    for lineno in _module_level_names(tree, ast.FunctionDef, "lookup"):
        violations.append(f"{filename}:{lineno} def lookup(")
    for lineno in _module_level_names(tree, ast.AsyncFunctionDef, "lookup"):
        violations.append(f"{filename}:{lineno} async def lookup(")
    for lineno in _module_level_names(tree, ast.FunctionDef, "__getattr__"):
        violations.append(f"{filename}:{lineno} def __getattr__(")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "gateway_compat.api" or alias.name.startswith("gateway_compat.api."):
                    violations.append(f"{filename}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "gateway_compat.api" or module.startswith("gateway_compat.api."):
                violations.append(f"{filename}:{node.lineno} from {module} import ...")
            if module == "api" and node.level >= 1:
                violations.append(f"{filename}:{node.lineno} from .api import ...")
            if node.level >= 1 and (module == "" or module is None):
                for alias in node.names:
                    if alias.name == "api":
                        violations.append(f"{filename}:{node.lineno} from . import api")
            if module == "gateway_compat":
                for alias in node.names:
                    if alias.name == "api":
                        violations.append(f"{filename}:{node.lineno} from gateway_compat import api")
    return violations


def relaysymbols_violations(source: str, *, filename: str = "<memory>") -> list[str]:
    tree = ast.parse(source, filename=filename)
    return [
        f"{filename}:{lineno} class RelaySymbols"
        for lineno in _module_level_names(tree, ast.ClassDef, "RelaySymbols")
    ]


def test_lookup_api_getattr_detector_flags_synthetic_mutations() -> None:
    lookup_hits = lookup_api_getattr_violations("def lookup(name):\n    return name\n")
    assert lookup_hits, "detector must fail closed on a reintroduced lookup()"
    getattr_hits = lookup_api_getattr_violations("def __getattr__(name):\n    raise AttributeError(name)\n")
    assert getattr_hits, "detector must fail closed on package __getattr__"
    api_hits = lookup_api_getattr_violations("from . import api\n")
    assert api_hits, "detector must fail closed on from . import api"
    clean = lookup_api_getattr_violations("def compatible_request_body():\n    return None\n")
    assert clean == []


def test_relaysymbols_detector_flags_synthetic_mutations() -> None:
    hits = relaysymbols_violations("class RelaySymbols:\n    pass\n")
    assert hits, "detector must fail closed on class RelaySymbols"
    assert relaysymbols_violations("class RelayGlue:\n    pass\n") == []


def test_gateway_compat_lookup_api_and_module_getattr_stay_gone() -> None:
    assert not (COMPAT_ROOT / "api.py").exists()
    violations: list[str] = []
    for path in sorted(COMPAT_ROOT.glob("*.py")):
        violations.extend(
            lookup_api_getattr_violations(path.read_text(encoding="utf-8"), filename=str(path.relative_to(REPO_ROOT)))
        )
    assert violations == [], "gateway_compat lookup/api/__getattr__ surface returned:\n" + "\n".join(violations)


def test_relaysymbols_class_stays_gone() -> None:
    violations: list[str] = []
    for path in sorted(SRC_PYTHON.glob("*.py")):
        violations.extend(
            relaysymbols_violations(path.read_text(encoding="utf-8"), filename=str(path.relative_to(REPO_ROOT)))
        )
    assert violations == [], "RelaySymbols returned:\n" + "\n".join(violations)


def test_handler_impl_and_relay_line_budgets_are_ratcheted() -> None:
    handler_count = _line_count(SRC_PYTHON / "gateway_handler_impl.py")
    relay_count = _line_count(SRC_PYTHON / "gateway_relay.py")
    exchange_count = _line_count(SRC_PYTHON / "gateway_exchange.py")
    bindings_count = _line_count(SRC_PYTHON / "gateway_exchange_bindings.py")
    ports_count = _line_count(SRC_PYTHON / "gateway_exchange_ports.py")
    adapters_count = _line_count(SRC_PYTHON / "gateway_exchange_adapters.py")
    dispatch_count = _line_count(SRC_PYTHON / "gateway_error_dispatch.py")
    assert handler_count <= HANDLER_IMPL_LINE_BUDGET, f"gateway_handler_impl.py is {handler_count} lines"
    assert relay_count <= RELAY_LINE_BUDGET, f"gateway_relay.py is {relay_count} lines"
    assert exchange_count <= EXCHANGE_LINE_BUDGET, f"gateway_exchange.py is {exchange_count} lines"
    assert bindings_count <= BINDINGS_LINE_BUDGET, f"gateway_exchange_bindings.py is {bindings_count} lines"
    assert ports_count <= PORTS_LINE_BUDGET, f"gateway_exchange_ports.py is {ports_count} lines"
    assert adapters_count <= ADAPTERS_LINE_BUDGET, f"gateway_exchange_adapters.py is {adapters_count} lines"
    assert dispatch_count <= DISPATCH_LINE_BUDGET, f"gateway_error_dispatch.py is {dispatch_count} lines"
    assert HANDLER_IMPL_LINE_BUDGET < 3000
    assert RELAY_LINE_BUDGET < 3000
    assert EXCHANGE_LINE_BUDGET < 3000
    assert BINDINGS_LINE_BUDGET < 3000
    assert PORTS_LINE_BUDGET < 3000
    assert ADAPTERS_LINE_BUDGET < 3000
    assert DISPATCH_LINE_BUDGET < 3000


def _contains_handler_impl_import(source: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "gateway_handler_impl" or alias.name.startswith("gateway_handler_impl."):
                    return True
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("gateway_handler_impl"):
            return True
    return False


def test_extracted_post_modules_do_not_import_handler_impl() -> None:
    for name in ("gateway_error_dispatch.py", "gateway_exchange_adapters.py"):
        source = (SRC_PYTHON / name).read_text(encoding="utf-8")
        assert not _contains_handler_impl_import(source), f"{name} imports gateway_handler_impl"
        assert "post_request_live_from_locals" not in source
        assert "locals()" not in source


def test_extracted_post_modules_do_not_shadow_imported_modules() -> None:
    """Assignment like `route_plan = live.route_plan` must not hide `import route_plan`."""

    for name in ("gateway_error_dispatch.py", "gateway_exchange_adapters.py"):
        tree = ast.parse((SRC_PYTHON / name).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.asname or alias.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name != "*":
                        imported.add(alias.asname or alias.name)
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            assigned = {
                child.id
                for child in ast.walk(node)
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
            }
            shadowed = sorted(assigned & imported)
            assert not shadowed, f"{name}::{node.name} shadows imported names {shadowed}"


def test_gateway_exchange_has_no_mid_file_imports() -> None:
    tree = ast.parse((SRC_PYTHON / "gateway_exchange.py").read_text(encoding="utf-8"))
    seen_def = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            seen_def = True
            continue
        if seen_def and isinstance(node, (ast.Import, ast.ImportFrom)):
            raise AssertionError(f"gateway_exchange.py has a mid-file import at line {node.lineno}")


def test_gateway_exchange_bindings_owns_handler_relay_bindings() -> None:
    exchange = (SRC_PYTHON / "gateway_exchange.py").read_text(encoding="utf-8")
    bindings = (SRC_PYTHON / "gateway_exchange_bindings.py").read_text(encoding="utf-8")
    assert "def handler_downstream_stream_commit(" not in exchange
    assert "def handler_downstream_stream_commit(" in bindings
    assert "def execute_exchange(" in exchange
    assert "def execute_exchange(" not in bindings

