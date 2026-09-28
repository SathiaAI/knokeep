"""Read-only LocalBackend store inspector for health checks.

Inspects journal framing and published blob consistency without constructing
LocalBackend, replaying, repairing, or creating store paths.
"""
from __future__ import annotations

import hashlib
import os
import stat
import struct
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from store.gate import _MAX_KEY_LEN, _MAX_SCAN_BYTES, _valid_key_shape
from store.types import sha256_hex

_JOURNAL_V2_MAGIC = b"KKJ2"
_UINT64_MAX = (1 << 64) - 1


def store_aux_reads_allowed(store_check: dict) -> bool:
    """True when scorecard/audit/project reads may touch store content safely."""
    if not store_check.get("ok"):
        return False
    if store_check.get("indeterminate"):
        return False
    return True


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def resolve_store_root(store: str) -> Tuple[Optional[Path], List[str]]:
    """Resolve store root for read-only inspection; never creates paths."""
    reasons: List[str] = []
    if store is None or str(store).strip() == "":
        return None, ["store_missing"]

    root_input = os.path.abspath(store)
    if not os.path.lexists(root_input):
        return None, ["store_missing"]

    try:
        if not os.path.isdir(root_input):
            return None, ["store_not_directory"]
    except OSError:
        return None, ["store_unreadable"]

    if os.path.islink(root_input):
        return None, ["store_symlink_escape"]

    try:
        root = Path(root_input).resolve()
    except OSError:
        return None, ["store_unreadable"]

    return root, reasons


def inspect_local_store(store: str) -> dict:
    """Return {"ok": bool, "reasons": [machine codes], "indeterminate": bool?}."""
    root, reasons = resolve_store_root(store)
    if root is None:
        return {"ok": False, "reasons": reasons}

    data_dir = root / "data"
    journal_path = root / "journal" / "journal.log"

    indeterminate = False
    size_before: Optional[int] = None
    journal_present = False
    try:
        journal_present = journal_path.exists() or journal_path.is_symlink()
    except OSError:
        reasons.append("journal_unreadable")
        journal_present = False

    if journal_present:
        if not _entry_contained(journal_path, root):
            reasons.append("store_symlink_escape")
        elif not _is_safe_regular_file(journal_path, root):
            reasons.append("journal_unreadable")
        else:
            try:
                size_before = os.stat(journal_path).st_size
            except OSError:
                reasons.append("journal_unreadable")
            else:
                journal_reasons, latest = _inspect_journal(journal_path)
                reasons.extend(journal_reasons)
                if latest is not None:
                    reasons.extend(_check_published(data_dir, root, latest))
                try:
                    size_final = os.stat(journal_path).st_size
                except OSError:
                    indeterminate = True
                else:
                    if size_before != size_final:
                        indeterminate = True
    elif not reasons:
        reasons.append("journal_missing")

    if indeterminate:
        if "inspect_indeterminate" not in reasons:
            reasons.append("inspect_indeterminate")

    return {
        "ok": not reasons,
        "reasons": reasons,
        **({"indeterminate": True} if indeterminate else {}),
    }


def read_project_summaries(store: str, *, root: Optional[Path] = None) -> List[dict]:
    """Read top-level project system_state metadata without LocalBackend."""
    if root is None:
        root, reasons = resolve_store_root(store)
        if root is None or reasons:
            return []
    data_dir = root / "data"
    if not _path_exists(data_dir) or not data_dir.is_dir():
        return []
    projects: List[dict] = []
    try:
        for name in os.listdir(data_dir):
            if name.startswith("."):
                continue
            proj_dir = data_dir / name
            if not _entry_contained(proj_dir, root):
                continue
            if not proj_dir.is_dir():
                continue
            state_path = proj_dir / "system_state"
            if not _is_safe_regular_file(state_path, root):
                continue
            raw = _read_bounded_regular_file(state_path)
            if raw is None:
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


def safe_data_dir_for_audit(store: str, *, root: Optional[Path] = None) -> Optional[str]:
    """Return data directory path for audit only when contained and not a symlink escape."""
    if root is None:
        root, reasons = resolve_store_root(store)
        if root is None or reasons:
            return None
    data_dir = root / "data"
    if not _path_exists(data_dir):
        return None
    if os.path.islink(data_dir):
        return None
    if not _entry_contained(data_dir, root):
        return None
    return str(data_dir)


def safe_events_path(store: str, *, root: Optional[Path] = None) -> Optional[Path]:
    if root is None:
        root, reasons = resolve_store_root(store)
        if root is None or reasons:
            return None
    ev = root / ".knokeep-eval" / "events.jsonl"
    if not _path_exists(ev):
        return None
    if not _is_safe_regular_file(ev, root):
        return None
    return ev


def read_telemetry_bytes(store: str, *, root: Optional[Path] = None) -> Tuple[Optional[bytes], List[str]]:
    """Read events.jsonl bytes when path is safe; else return reason codes."""
    ev = safe_events_path(store, root=root)
    if ev is None:
        root_res, rs = resolve_store_root(store)
        if root_res is None:
            return None, rs
        candidate = root_res / ".knokeep-eval" / "events.jsonl"
        if _path_exists(candidate) and not _is_safe_regular_file(candidate, root_res):
            return None, ["telemetry_unreadable"]
        return None, []
    raw = _read_bounded_regular_file(ev)
    if raw is None:
        return None, ["telemetry_unreadable"]
    return raw, []


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


def _entry_contained(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _is_safe_regular_file(path: Path, root: Path) -> bool:
    if not _entry_contained(path, root):
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    return True


def _read_bounded_regular_file(path: Path) -> Optional[bytes]:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
        return None
    if st.st_size > _MAX_SCAN_BYTES:
        return None
    try:
        with open(path, "rb") as f:
            return f.read(_MAX_SCAN_BYTES + 1)
    except OSError:
        return None


def _inspect_journal(
    journal_path: Path,
) -> Tuple[List[str], Optional[Dict[str, str]]]:
    reasons: List[str] = []
    latest: Dict[str, str] = {}
    scan_state = {"end": 0, "size": 0}
    try:
        for key, body_hash in _iter_journal_records(journal_path, scan_state, reasons):
            latest[key] = body_hash
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
) -> Iterator[Tuple[str, str]]:
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
            if not _valid_key_shape(key):
                reasons.append("journal_bad_key")
                return
            yield key, sha256_hex(body)


def _check_published(
    data_dir: Path, root: Path, latest: Dict[str, str]
) -> List[str]:
    reasons: List[str] = []
    for key, expected_hash in latest.items():
        if not _valid_key_shape(key):
            reasons.append("journal_bad_key")
            continue
        rel = Path(*key.split("/"))
        try:
            data_path = _safe_join(data_dir, rel)
        except (ValueError, OSError):
            reasons.append("store_symlink_escape")
            continue
        if not _entry_contained(data_path, root):
            reasons.append("store_symlink_escape")
            continue
        if not _is_safe_regular_file(data_path, root):
            if _path_exists(data_path) and os.path.islink(data_path):
                reasons.append("store_symlink_escape")
            else:
                reasons.append("data_unpublished")
            continue
        on_disk_hash = _file_hash(data_path)
        if on_disk_hash is None:
            reasons.append("data_unreadable")
            continue
        if on_disk_hash != expected_hash:
            reasons.append("data_mismatch")
    return reasons


def _file_hash(path: Path) -> Optional[str]:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
        return None
    if st.st_size > _MAX_SCAN_BYTES:
        return None
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            remaining = st.st_size
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                h.update(chunk)
                remaining -= len(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _safe_join(base: Path, rel_path: Path) -> Path:
    candidate = base / rel_path
    resolved_base = base.resolve()
    try:
        resolved_parent = candidate.parent.resolve()
    except OSError as exc:
        raise ValueError("path outside data root") from exc
    if resolved_parent != resolved_base and resolved_base not in resolved_parent.parents:
        raise ValueError("path outside data root")
    return resolved_parent / candidate.name
