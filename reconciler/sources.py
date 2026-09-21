"""Small, injectable, READ-ONLY source clients for the reconciler.

None of these types expose any write method. That is enforced by their
surface, not just by convention:
  - `StoreSource` and `FilesSource` are concrete classes whose only public
    method is `fetch_state()`; they call `StoreBackend.read()`/`.list()` or
    `pathlib.Path.read_text()` and nothing else — they hold no reference to
    any write-capable call (`StoreBackend.write/lock/unlock/renew` are never
    invoked from this module).
  - `GitHubSource` and `TrackerSource` are `typing.Protocol` classes that
    declare exactly one method, `fetch_state() -> dict`. A test double built
    against either protocol is free to add its own methods, but the
    reconciler (reconcile.py) only ever calls `.fetch_state()` on any source
    — see tests/test_reconciler_no_write_path.py, which statically confirms
    both facts (protocol surface has no write-shaped method; reconcile.py
    never calls one).

Stdlib-only. No network calls anywhere in this module — GitHubSource and
TrackerSource are interfaces only; a real network-backed implementation
would live elsewhere (outside this read-only package) and tests inject a
plain mock instead.
"""
from __future__ import annotations

import typing
from pathlib import Path
from typing import Dict, Sequence, Union

if typing.TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from store.backend import StoreBackend


class StoreSource:
    """Reads a fixed set of keys from a StoreBackend. Read-only.

    Calls ONLY `backend.read(key)` (never `.write`, `.lock`, `.unlock`,
    `.renew`). A missing key is simply absent from the returned dict — this
    class never raises NOT_FOUND-shaped errors of its own.
    """

    def __init__(self, backend: "StoreBackend", keys: Sequence[str]) -> None:
        self._backend = backend
        self._keys = tuple(keys)

    def fetch_state(self) -> Dict[str, str]:
        state: Dict[str, str] = {}
        for key in self._keys:
            blob = self._backend.read(key)
            if blob is None:
                continue
            state[key] = blob.body.decode("utf-8", errors="replace")
        return state


class FilesSource:
    """Reads a fixed set of files from a project directory. Read-only.

    Calls ONLY `Path.read_text()`. Never writes, never follows a symlink
    outside `root` on purpose (though it does not attempt to sandbox a
    maliciously-crafted symlink — that is a filesystem-permissions concern
    outside this reconciler's scope).
    """

    def __init__(self, root: Union[str, Path], filenames: Sequence[str]) -> None:
        self._root = Path(root)
        self._filenames = tuple(filenames)

    def fetch_state(self) -> Dict[str, str]:
        state: Dict[str, str] = {}
        for name in self._filenames:
            path = self._root / name
            try:
                state[name] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        return state


@typing.runtime_checkable
class GitHubSource(typing.Protocol):
    """Interface for a read-only GitHub state source.

    Exactly one method. No `create_issue`, `push`, `merge`, `comment`,
    `update_status`, or any other write-shaped method belongs on this
    surface — the reconciler never needs one, because it never writes back
    to GitHub (design/store_backend_contract.md §6 / knokeep-solution-design
    "Reconciler": a write-back to an external system of record is a
    separate, explicit, human-gated action outside this package).
    """

    def fetch_state(self) -> dict:
        """Return a flat dict of field_name -> value describing GitHub's
        current view of whatever the reconciler cares about (e.g. the HEAD
        ref/message of a tracked branch). Implementations should use
        separately-scoped READ-ONLY credentials (contract requirement) —
        this protocol has no write capability to misuse even if they didn't.
        """
        ...


@typing.runtime_checkable
class TrackerSource(typing.Protocol):
    """Interface for a read-only issue-tracker state source (Linear/Jira/
    Asana/etc.). Exactly one method — see GitHubSource for why."""

    def fetch_state(self) -> dict:
        """Return a flat dict of field_name -> value describing the
        tracker's current view (e.g. an item's status)."""
        ...


__all__ = ["StoreSource", "FilesSource", "GitHubSource", "TrackerSource"]
