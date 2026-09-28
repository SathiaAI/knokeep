"""Read-only LocalBackend store inspector for health checks.

Inspects journal framing and published blob consistency without constructing
LocalBackend, replaying, repairing, or creating store paths.
"""
from __future__ import annotations

import hashlib
import os
import struct
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from store.gate import _MAX_KEY_LEN, _MAX_SCAN_BYTES
from store.types import sha256_hex

_JOURNAL_V2_MAGIC = b"KKJ2"
_UINT64_MAX = (1 << 64) - 1


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def inspect_local_store(store: str) -> dict:
    """Return {"ok": bool, "reasons": [machine codes], "indeterminate": bool?}."""
    reasons: List[str] = []
    if store is None or str(store).strip() == "":
        return {"ok": False, "reasons": ["store_missing"]}

    root_input = os.path.abspath(store)
    if not os.path.lexists(root_input):
        return {"ok": False, "reasons": ["store_missing"]}

    try:
        if not os.path.isdir(root_input):
            return {"ok": False, "reasons": ["store_not_directory"]}
    except OSError:
        return {"ok": False, "reasons": ["store_unreadable"]}

    try:
        root = Path(root_input).resolve()
    except OSError:
        return {"ok": False, "reasons": ["store_unreadable"]}

    if _symlink_escapes_root(root_input, root):
        return {"ok": False, "reasons": ["store_symlink_escape"]}

    data_dir = root / "data"
    journal_path = root / "journal" / "journal.log"

    indeterminate = False
    size_before = size_after = None
    journal_present = False
    try:
        journal_present = journal_path.exists() or journal_path.is_symlink()
    except OSError:
        reasons.append("journal_unreadable")
        journal_present = False
    if journal_present:
        if not _path_contained(journal_path, root):
            reasons.append("store_symlink_escape")
        else:
            try:
                size_before = os.stat(journal_path).st_size
            except OSError:
                reasons.append("journal_unreadable")
            else:
                journal_reasons, latest = _inspect_journal(journal_path)
                reasons.extend(journal_reasons)
                try:
                    size_after = os.stat(journal_path).st_size
                except OSError:
                    indeterminate = True
                if size_before != size_after:
                    indeterminate = True
                if latest:
                    reasons.extend(
                        _check_published(data_dir, root, latest)
                    )
    elif not reasons and not _path_exists(data_dir):
        # Empty/non-local layout: no journal and no data is not a healthy knokeep store.
        reasons.append("journal_missing")

    out: dict = {"ok": not reasons, "reasons": reasons}
    if indeterminate:
        out["indeterminate"] = True
    return out


def read_project_summaries(store: str) -> List[dict]:
    """Read top-level project system_state metadata without LocalBackend."""
    root_input = os.path.abspath(store)
    if not os.path.isdir(root_input):
        return []
    root = Path(root_input).resolve()
    data_dir = root / "data"
    if not data_dir.is_dir():
        return []
    projects: List[dict] = []
    try:
        for name in os.listdir(data_dir):
            if name.startswith("."):
                continue
            proj_dir = data_dir / name
            if not _path_contained(proj_dir, root):
                continue
            if not proj_dir.is_dir():
                continue
            state_path = proj_dir / "system_state"
            if not state_path.is_file():
                continue
            try:
                raw = state_path.read_bytes()
            except OSError:
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            fm = _parse_frontmatter(text)
            projects.append(
                {
                    "project": name,
                    "revision": fm.get("revision"),
                    "updated": fm.get("updated"),
                }
            )
    except OSError:
        return []
    return projects


def _parse_frontmatter(text: str) -> dict:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    fm: dict = {}
    block = text[4:end]
    for line in block.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm


def _symlink_escapes_root(root_input: str, root_resolved: Path) -> bool:
    """True if root_input is a symlink whose target lies outside itself."""
    try:
        st = os.lstat(root_input)
    except OSError:
        return False
    if not os.path.islink(root_input):
        return False
    try:
        target = Path(os.path.realpath(root_input))
        target.relative_to(root_resolved)
        return False
    except ValueError:
        return True


def _path_contained(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def _inspect_journal(
    journal_path: Path,
) -> Tuple[List[str], Optional[Dict[str, bytes]]]:
    reasons: List[str] = []
    latest: Dict[str, bytes] = {}
    scan_state = {"end": 0, "size": 0}
    try:
        for key, body in _iter_journal_records(journal_path, scan_state, reasons):
            latest[key] = body
    except OSError:
        return (["journal_unreadable"], None)
    end = scan_state.get("end", 0)
    size = scan_state.get("size", 0)
    if size > end:
        if "journal_torn_tail" not in reasons:
            reasons.append("journal_torn_tail")
    return (reasons, latest)


def _iter_journal_records(
    journal_path: Path,
    scan_state: dict,
    reasons: List[str],
) -> Iterator[Tuple[str, bytes]]:
    with open(journal_path, "rb") as f:
        scan_state["end"] = 0
        scan_state["size"] = os.fstat(f.fileno()).st_size
        while True:
            scan_state["end"] = f.tell()
            header = f.read(4)
            if len(header) < 4:
                return
            is_v2 = header == _JOURNAL_V2_MAGIC
            if is_v2:
                header = f.read(4)
                if len(header) < 4:
                    reasons.append("journal_torn_tail")
                    return
            try:
                (key_len,) = struct.unpack(">I", header)
            except struct.error:
                reasons.append("journal_bad_record")
                return
            if key_len == 0 or key_len > _MAX_KEY_LEN:
                reasons.append("journal_bad_key_length")
                return
            key_bytes = f.read(key_len)
            if len(key_bytes) < key_len:
                reasons.append("journal_torn_tail")
                return
            body_len_bytes = f.read(8)
            if len(body_len_bytes) < 8:
                reasons.append("journal_torn_tail")
                return
            (body_len,) = struct.unpack(">Q", body_len_bytes)
            if body_len > _UINT64_MAX:
                reasons.append("journal_impossible_body_length")
                return
            if body_len > _MAX_SCAN_BYTES:
                reasons.append("journal_impossible_body_length")
                return
            body = f.read(body_len)
            if len(body) < body_len:
                reasons.append("journal_torn_tail")
                return
            digest = f.read(32)
            if len(digest) < 32:
                reasons.append("journal_torn_tail")
                return
            if hashlib.sha256(body).digest() != digest:
                reasons.append("journal_bad_digest")
                return
            if is_v2:
                fence_bytes = f.read(8)
                if len(fence_bytes) < 8:
                    reasons.append("journal_torn_tail")
                    return
            try:
                key = key_bytes.decode("utf-8")
            except UnicodeDecodeError:
                reasons.append("journal_bad_record")
                return
            yield key, body


def _check_published(
    data_dir: Path, root: Path, latest: Dict[str, bytes]
) -> List[str]:
    reasons: List[str] = []
    for key, body in latest.items():
        rel = Path(*key.split("/"))
        data_path = _safe_join(data_dir, rel)
        if not _path_contained(data_path, root):
            reasons.append("store_symlink_escape")
            continue
        if data_path.is_symlink():
            reasons.append("store_symlink_escape")
            continue
        if not data_path.is_file():
            reasons.append("data_unpublished")
            continue
        try:
            on_disk = data_path.read_bytes()
        except OSError:
            reasons.append("data_unreadable")
            continue
        if sha256_hex(on_disk) != sha256_hex(body):
            reasons.append("data_mismatch")
    return reasons


def _safe_join(base: Path, rel_path: Path) -> Path:
    candidate = base / rel_path
    resolved_base = base.resolve()
    try:
        resolved_parent = candidate.parent.resolve()
    except OSError:
        return candidate
    if resolved_parent != resolved_base and resolved_base not in resolved_parent.parents:
        raise ValueError("path outside data root")
    return resolved_parent / candidate.name
