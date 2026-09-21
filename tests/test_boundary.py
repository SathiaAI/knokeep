"""AST-based boundary test (contract §1): ScannedBody/ScannedKey must never be
constructed, subclassed, or pickled anywhere outside store/gate.py.

This is a static/textual guard on top of the runtime guards already in
store/gate.py (sentinel-gated __init__, __init_subclass__, __reduce__). It
exists so that a future edit which tries to bypass the gate from application
code fails CI immediately, with a clear message, rather than relying solely
on the runtime TypeError.
"""
from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GATE_MODULE_PATH = (PROJECT_ROOT / "store" / "gate.py").resolve()

GATED_NAMES = {"ScannedBody", "ScannedKey"}

# Directories we never want to walk (VCS metadata, caches, virtualenvs if any
# ever appear under the project root).
_EXCLUDED_DIR_NAMES = {".git", "__pycache__", ".venv", "venv", ".pytest_cache"}


def _iter_project_py_files():
    for path in PROJECT_ROOT.rglob("*.py"):
        if any(part in _EXCLUDED_DIR_NAMES for part in path.parts):
            continue
        yield path


class _GateBoundaryVisitor(ast.NodeVisitor):
    def __init__(self, filename: str):
        self.filename = filename
        self.violations: list[str] = []

    def _name_of_callable(self, func: ast.AST):
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return None

    def visit_Call(self, node: ast.Call):
        name = self._name_of_callable(node.func)
        if name in GATED_NAMES:
            self.violations.append(
                f"{self.filename}:{node.lineno}: constructs {name}(...) outside store/gate.py"
            )
        # Flag pickling of a gated name passed by identifier, e.g.
        # pickle.dumps(ScannedBody(...)) or pickle.dumps(some_scanned_body) is
        # not staticly attributable in the general case, but a direct
        # construct-then-pickle in one call IS caught by the construction
        # check above already firing on the inner Call. We additionally flag
        # any pickle.dumps/dump/dumps-like call whose sole positional arg is
        # literally named ScannedBody/ScannedKey (covers `pickle.dumps(ScannedBody)`
        # style misuse of the class object itself).
        pickle_call_name = self._name_of_callable(node.func)
        if pickle_call_name in {"dumps", "dump"}:
            for arg in node.args:
                if isinstance(arg, ast.Name) and arg.id in GATED_NAMES:
                    self.violations.append(
                        f"{self.filename}:{node.lineno}: pickles {arg.id} outside store/gate.py"
                    )
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef):
        for base in node.bases:
            base_name = None
            if isinstance(base, ast.Name):
                base_name = base.id
            elif isinstance(base, ast.Attribute):
                base_name = base.attr
            if base_name in GATED_NAMES:
                self.violations.append(
                    f"{self.filename}:{node.lineno}: class {node.name} subclasses {base_name} outside store/gate.py"
                )
        self.generic_visit(node)


def test_scanned_types_never_constructed_subclassed_or_pickled_outside_gate():
    violations: list[str] = []

    for path in _iter_project_py_files():
        if path.resolve() == GATE_MODULE_PATH:
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - defensive
            violations.append(f"{path}: could not parse for boundary check: {exc}")
            continue
        visitor = _GateBoundaryVisitor(str(path.relative_to(PROJECT_ROOT)))
        visitor.visit(tree)
        violations.extend(visitor.violations)

    assert not violations, "gate boundary violated:\n" + "\n".join(violations)


def test_gate_module_actually_defines_the_gated_names():
    """Sanity check that the scan above isn't vacuously passing because the
    classes moved or were renamed without updating this test."""
    source = GATE_MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(GATE_MODULE_PATH))
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name in GATED_NAMES
    }
    assert defined == GATED_NAMES, f"expected {GATED_NAMES} defined in gate.py, found {defined}"
