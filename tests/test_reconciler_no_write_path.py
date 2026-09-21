"""Static proof that no reconciler code path can write to an external
source (task requirement #3): inspects the source-client Protocols/classes'
surfaces, and statically scans reconciler/*.py for any write-shaped call.

This mirrors the style of tests/test_boundary.py (AST-based boundary guard
for store/gate.py's ScannedKey/ScannedBody) applied to the reconciler's own
read-only invariant.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from reconciler import sources as sources_module
from reconciler.sources import FilesSource, GitHubSource, StoreSource, TrackerSource

RECONCILER_DIR = Path(sources_module.__file__).resolve().parent

# Verb names that would indicate a write/mutate capability if they appeared
# as a *called method* anywhere in the reconciler package, or as a *public
# method* on any source type. `read`/`fetch_state`/`list` are intentionally
# excluded — those are the read-only surface this package is built on.
WRITE_VERBS = {
    "write",
    "create",
    "update",
    "delete",
    "push",
    "put",
    "post",
    "patch",
    "remove",
    "merge",
    "close",
    "comment",
    "commit",
    "set_status",
    "create_issue",
    "update_issue",
    "close_issue",
    "merge_pr",
    "push_branch",
    "lock",
    "unlock",
    "renew",
}


def _public_method_names(cls) -> set:
    names = set()
    for name, value in vars(cls).items():
        if name.startswith("_"):
            continue
        if inspect.isfunction(value):
            names.add(name)
    return names


@pytest.mark.parametrize("cls", [StoreSource, FilesSource, GitHubSource, TrackerSource])
def test_source_type_public_surface_is_fetch_state_only(cls):
    """Every source type's entire public surface is `fetch_state()` — there
    is no create/update/delete/push/lock/etc. method to (mis)call, on the
    concrete classes or on the injectable Protocols."""
    public = _public_method_names(cls)
    assert public == {"fetch_state"}, (
        f"{cls.__name__} exposes {public!r}; a reconciler source must expose "
        "exactly fetch_state() and nothing write-shaped"
    )


@pytest.mark.parametrize("cls", [GitHubSource, TrackerSource])
def test_protocols_declare_no_write_method(cls):
    """Even independent of the 'surface is exactly fetch_state' check above,
    explicitly confirm no WRITE_VERBS name is declared on the protocol."""
    declared = set(vars(cls).keys())
    overlap = declared & WRITE_VERBS
    assert not overlap, f"{cls.__name__} declares write-shaped method(s): {overlap}"


def test_reconciler_package_never_calls_a_write_shaped_method():
    """Parse reconcile.py and sources.py — the two modules that touch
    `sources`/`StoreBackend` — and fail if any attribute CALL uses a
    write-shaped verb name. Catches both a write to an external source and
    a write to the StoreBackend (write/lock/unlock/renew).

    telemetry.py is deliberately NOT scanned here: it appends to a LOCAL
    JSONL file under `.knokeep-eval/` (an explicit, sanctioned part of this
    task's spec — "write it to a local JSONL"), which is not a write to any
    external source/system-of-record. Its file handle's `.write(...)` call
    is checked separately, below, to confirm it only ever targets a local
    path under a telemetry directory.
    """
    violations = []
    for path in (RECONCILER_DIR / "reconcile.py", RECONCILER_DIR / "sources.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in WRITE_VERBS:
                    violations.append(f"{path.name}:{node.lineno}: calls .{node.func.attr}(...)")
    assert not violations, "reconciler calls a write-shaped method:\n" + "\n".join(violations)


def test_telemetry_write_call_is_only_to_a_local_file_handle():
    """telemetry.py's one `.write(...)` call is on a local file handle
    opened from a `Path` under a telemetry directory — never on a
    `StoreBackend`, a source object, or a socket. This is a narrow,
    intentional exception to the "no write-shaped call" rule enforced
    above for reconcile.py/sources.py."""
    telemetry_path = RECONCILER_DIR / "telemetry.py"
    tree = ast.parse(telemetry_path.read_text(encoding="utf-8"), filename=str(telemetry_path))

    write_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "write"
    ]
    assert len(write_calls) == 1, (
        "expected exactly one .write(...) call in telemetry.py (the local "
        f"JSONL append); found {len(write_calls)}"
    )
    # It must be a call on a `with ... as fh:` handle named `fh`, not on
    # anything resembling a backend/source object.
    call = write_calls[0]
    assert isinstance(call.func.value, ast.Name) and call.func.value.id == "fh"

    # No other write-shaped verb appears in this file at all (only the one
    # local file .write(...) checked above).
    other_verbs = WRITE_VERBS - {"write"}
    violations = [
        f"{telemetry_path.name}:{node.lineno}: calls .{node.func.attr}(...)"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in other_verbs
    ]
    assert not violations, "telemetry.py calls an unexpected write-shaped method:\n" + "\n".join(violations)


def test_reconcile_only_ever_calls_fetch_state_on_a_source():
    """Specifically confirm reconcile.py's interaction with `sources` is
    limited to `.fetch_state()` / `getattr(..., "fetch_state", ...)` — no
    other attribute of a source object is ever accessed by name in a way
    that could be a disguised write."""
    reconcile_path = RECONCILER_DIR / "reconcile.py"
    tree = ast.parse(reconcile_path.read_text(encoding="utf-8"), filename=str(reconcile_path))

    called_attrs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called_attrs.add(node.func.attr)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
            # getattr(source, "fetch_state", None) — check the literal name arg
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    called_attrs.add(arg.value)

    # Everything else called in reconcile.py: plain dict methods on
    # `sources`/`raw_state`/`values` (items/get/keys/values), str methods
    # used while building a report-safe value (encode/format), and the
    # local telemetry emitter — none of these touch a source object and
    # none is write-shaped. Only `fetch_state` is ever called on `source`.
    allowed = {
        "fetch_state",
        "items",
        "get",
        "keys",
        "values",
        "encode",
        "format",
        "emit_reconcile_event",
        "append",  # list.append on the locally-built `facts` list
    }
    unexpected = called_attrs - allowed
    assert not unexpected, (
        f"reconcile.py calls unexpected attribute(s) {unexpected!r} — verify "
        "none of these represent a write path onto a source object"
    )
    # The load-bearing assertion: fetch_state is actually used (not vacuous).
    assert "fetch_state" in called_attrs


def test_reconciler_module_has_no_import_of_network_or_subprocess_libraries():
    """Stdlib-only / no-network-calls guard: the reconciler package itself
    must not import anything that could reach out over the network or shell
    out — GitHubSource/TrackerSource are interfaces only, so no such import
    should ever be needed here."""
    forbidden_modules = {
        "socket",
        "requests",
        "urllib",
        "urllib2",
        "http.client",
        "httpx",
        "subprocess",
        "os.system",
    }
    violations = []
    for path in sorted(RECONCILER_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in forbidden_modules or alias.name.split(".")[0] in forbidden_modules:
                        violations.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module in forbidden_modules or node.module.split(".")[0] in forbidden_modules:
                    violations.append(f"{path.name}: from {node.module} import ...")
    assert not violations, "reconciler imports a network/subprocess module:\n" + "\n".join(violations)
