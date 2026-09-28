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
from typing import Callable, Dict, Iterator, List, Optional, Tuple

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
        journal_present = os.path.lexists(journal_path)
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
        if not reasons:
            reasons.extend(_check_data_layout(data_dir, root))
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


_AUDIT_MAX_DEPTH = 64
_AUDIT_MAX_ENTRIES = 4096
_AUDIT_MAX_TOTAL_BYTES = 64 * 1024 * 1024


def _bounded_tree_walk(
    start: Path,
    root: Path,
    on_file: Optional[Callable[[Path, os.DirEntry], None]] = None,
    start_depth: int = 0,
) -> List[str]:
    """Iterative lstat walk of `start` (no symlink/reparse following).

    Fails closed with one machine reason on the first unsafe entry or when a
    deterministic budget is exceeded: directory depth below data/
    (_AUDIT_MAX_DEPTH; `start_depth` is `start`'s own depth below data/), total entries (_AUDIT_MAX_ENTRIES) or aggregate
    regular-file st_size bytes (_AUDIT_MAX_TOTAL_BYTES). `on_file` is called for
    each regular file only while all budgets hold."""
    entries_seen = 0
    total_bytes = 0
    stack: List[Tuple[Path, int]] = [(start, start_depth)]
    while stack:
        dirpath, depth = stack.pop()
        try:
            with os.scandir(dirpath) as it:
                children = []
                for ent in it:
                    entries_seen += 1
                    if entries_seen > _AUDIT_MAX_ENTRIES:
                        return ["audit_entry_budget_exceeded"]
                    children.append(ent)
        except OSError:
            return ["data_unreadable"]
        children.sort(key=lambda e: e.name)
        for ent in children:
            try:
                est = ent.stat(follow_symlinks=False)
            except OSError:
                return ["data_unreadable"]
            if _is_reparse_point(est) or stat.S_ISLNK(est.st_mode):
                return ["store_symlink_escape"]
            full = Path(ent.path)
            if not _entry_contained(full, root):
                return ["store_symlink_escape"]
            if stat.S_ISDIR(est.st_mode):
                if depth + 1 > _AUDIT_MAX_DEPTH:
                    return ["audit_depth_budget_exceeded"]
                stack.append((full, depth + 1))
                continue
            if not stat.S_ISREG(est.st_mode):
                return ["data_layout_unsafe"]
            total_bytes += est.st_size
            if total_bytes > _AUDIT_MAX_TOTAL_BYTES:
                return ["audit_byte_budget_exceeded"]
            if on_file is not None:
                on_file(full, ent)
    return []


def safe_data_dir_for_audit(
    store: str, *, root: Optional[Path] = None
) -> Tuple[Optional[str], List[str]]:
    """Return data directory for audit when the tree has no symlink/reparse escapes."""
    if root is None:
        root, reasons = resolve_store_root(store)
        if root is None or reasons:
            return None, reasons
    data_dir = root / "data"
    layout_reasons = _check_data_layout(data_dir, root)
    if layout_reasons:
        return None, layout_reasons
    tree_reasons = _data_tree_safe_for_audit(data_dir, root)
    if tree_reasons:
        return None, tree_reasons
    return str(data_dir), []


_BENIGN_AUDIT_BASENAMES = frozenset({".ds_store", "thumbs.db", "desktop.ini"})


def is_os_metadata_basename(key: str) -> bool:
    """True for OS/tooling metadata basenames (.DS_Store, Thumbs.db, desktop.ini).

    Only exempts such files from the binary/non-text check; they are still
    returned by the audit walk and secret-scanned like any other file."""
    return key.rsplit("/", 1)[-1].lower() in _BENIGN_AUDIT_BASENAMES


def enumerate_project_published_files_for_audit(
    store: str,
    project: str,
    *,
    root: Optional[Path] = None,
) -> Tuple[List[Tuple[str, Optional[bytes]]], List[str], List[str]]:
    """Read-only walk of on-disk files under data/{project}/ for bootstrap audit.

    Does not construct LocalBackend and does not treat files as journal-committed
    records. Returns (entries, block_reasons, uncertainty_reasons) where each
    entry is (logical_key, raw_bytes_or_None_if_unreadable).
    """
    block_reasons: List[str] = []
    uncertainty_reasons: List[str] = []
    entries: List[Tuple[str, Optional[bytes]]] = []

    store_check = inspect_local_store(store)
    if store_check.get("indeterminate"):
        uncertainty_reasons.append("inspect_indeterminate")
    for code in store_check.get("reasons") or []:
        if code not in uncertainty_reasons:
            uncertainty_reasons.append(code)

    if root is None:
        root, root_reasons = resolve_store_root(store)
        if root is None:
            return [], list(root_reasons), uncertainty_reasons

    data_dir_str, layout_reasons = safe_data_dir_for_audit(store, root=root)
    if layout_reasons:
        return [], layout_reasons, uncertainty_reasons
    if data_dir_str is None:
        return [], ["data_layout_missing"], uncertainty_reasons

    data_dir = Path(data_dir_str)
    project_dir = data_dir / project
    if not os.path.lexists(project_dir):
        return [], block_reasons, uncertainty_reasons

    try:
        pst = os.lstat(project_dir)
    except OSError:
        return [], ["data_unreadable"], uncertainty_reasons
    if _is_reparse_point(pst) or stat.S_ISLNK(pst.st_mode):
        return [], ["store_symlink_escape"], uncertainty_reasons
    if not stat.S_ISDIR(pst.st_mode):
        return [], ["data_layout_unsafe"], uncertainty_reasons
    if not _entry_contained(project_dir, root):
        return [], ["store_symlink_escape"], uncertainty_reasons

    prefix = project + "/"

    def collect(full: Path, ent: os.DirEntry) -> None:
        try:
            rel = full.relative_to(data_dir).as_posix()
        except ValueError:
            block_reasons.append("store_symlink_escape")
            return
        if rel.startswith(prefix):
            entries.append((rel, _read_bounded_regular_file(full)))

    walk_reasons = _bounded_tree_walk(project_dir, root, collect, start_depth=1)
    if walk_reasons or block_reasons:
        return [], block_reasons + walk_reasons, uncertainty_reasons
    return entries, block_reasons, uncertainty_reasons


def safe_events_path(store: str, *, root: Optional[Path] = None) -> Optional[Path]:
    path, reasons = _telemetry_events_path(store, root=root)
    if reasons:
        return None
    return path


def read_telemetry_bytes(
    store: str, *, root: Optional[Path] = None
) -> Tuple[Optional[bytes], List[str]]:
    """Read events.jsonl bytes when path is safe; else return reason codes."""
    ev, reasons = _telemetry_events_path(store, root=root)
    if reasons:
        return None, reasons
    if ev is None:
        return None, []
    raw = _read_bounded_regular_file(ev)
    if raw is None:
        return None, ["telemetry_unreadable"]
    return raw, []


def _telemetry_events_path(
    store: str, *, root: Optional[Path] = None
) -> Tuple[Optional[Path], List[str]]:
    if root is None:
        root, reasons = resolve_store_root(store)
        if root is None or reasons:
            return None, reasons
    eval_dir = root / ".knokeep-eval"
    ev = eval_dir / "events.jsonl"
    if os.path.lexists(eval_dir):
        dir_reasons = _check_optional_dir_entry(eval_dir, root)
        if dir_reasons:
            return None, dir_reasons
    if not os.path.lexists(ev):
        return None, []
    try:
        st = os.lstat(ev)
    except OSError:
        return None, ["telemetry_unreadable"]
    if _is_reparse_point(st) or stat.S_ISLNK(st.st_mode):
        return None, ["telemetry_unreadable"]
    if not stat.S_ISREG(st.st_mode):
        return None, ["telemetry_unreadable"]
    if not _entry_contained(ev, root):
        return None, ["telemetry_unreadable"]
    return ev, []


def _check_data_layout(data_dir: Path, root: Path) -> List[str]:
    if not os.path.lexists(data_dir):
        return ["data_layout_missing"]
    try:
        st = os.lstat(data_dir)
    except OSError:
        return ["data_unreadable"]
    if _is_reparse_point(st) or stat.S_ISLNK(st.st_mode):
        return ["store_symlink_escape"]
    if not stat.S_ISDIR(st.st_mode):
        return ["data_layout_missing"]
    if not _entry_contained(data_dir, root):
        return ["store_symlink_escape"]
    return []


def _check_optional_dir_entry(path: Path, root: Path) -> List[str]:
    try:
        st = os.lstat(path)
    except OSError:
        return ["telemetry_unreadable"]
    if _is_reparse_point(st) or stat.S_ISLNK(st.st_mode):
        return ["telemetry_unreadable"]
    if not stat.S_ISDIR(st.st_mode):
        return ["telemetry_unreadable"]
    if not _entry_contained(path, root):
        return ["telemetry_unreadable"]
    return []


def _data_tree_safe_for_audit(data_dir: Path, root: Path) -> List[str]:
    """Walk data/ without following symlinks; external scanners are not a boundary."""
    return _bounded_tree_walk(data_dir, root)


def _is_reparse_point(st: os.stat_result) -> bool:
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", None)
    if attrs is not None:
        flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if flag and (attrs & flag):
            return True
    return False


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
