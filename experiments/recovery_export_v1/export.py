"""Offline read-only store evidence export (experimental, stdlib-only).

Never constructs LocalBackend on the source store, never writes into the source
tree, never repairs or switches service. CLI: export and verify only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Sequence, Set, Tuple

from store import gate
from store.backend import BackendCorruptionError
from store.local import LocalBackend
from store.types import sha256_hex

CHUNK_SIZE = 65536
MAX_TOTAL_BYTES = 64 << 20
MAX_TREE_ENTRIES = 10_000
MAX_DEPTH = 64
MAX_MANIFEST_BYTES = 16 << 20
MAX_RETAINED_BODY_BYTES = 32 << 20
_WINDOWS = os.name == "nt"
_HEX = frozenset("0123456789abcdef")
LOCK_REL = "locks/cas.lock"
JOURNAL_REL = "journal/journal.log"

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class _BudgetExceeded(Exception):
    pass


class _SourceChanged(Exception):
    pass


class _VerifyReject(Exception):
    def __init__(self, reason: str, **extra: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.extra = extra


class _Budget:
    """Counts bytes actually read; raises once the limit is exceeded."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def add(self, n: int) -> None:
        self.used += n
        if self.used > self.limit:
            raise _BudgetExceeded()


def _open_read(path: Path) -> BinaryIO:
    return open(path, "rb")


def _hash_file_bounded(path: Path, budget: _Budget) -> Tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with _open_read(path) as f:
        while True:
            block = f.read(CHUNK_SIZE)
            if not block:
                break
            n += len(block)
            budget.add(len(block))
            h.update(block)
    return h.hexdigest(), n


class _JournalReadProxy:
    """Minimal object for LocalBackend._iter_journal_records without __init__."""

    __slots__ = ("_journal_path",)

    def __init__(self, journal_path: Path) -> None:
        self._journal_path = journal_path


@dataclass
class _JournalAnalysis:
    latest: Dict[str, Tuple[bytes, str]]
    prefix_end: int
    journal_size: int
    invalid_keys: Set[str]
    case_fold_collisions: Set[str]
    findings: List[str]
    published_mismatch_keys: List[str]
    materialized: Dict[str, str]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_reparse(st: os.stat_result) -> bool:
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", None)
    if attrs is not None:
        flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if flag and (attrs & flag):
            return True
    return False


def _rel_depth(rel: str) -> int:
    if not rel:
        return 0
    return len([p for p in rel.split("/") if p])


def _safe_archive_join(root: Path, rel: str) -> Optional[Path]:
    if not isinstance(rel, str) or rel == "":
        return None
    if rel.startswith("/") or rel.startswith("\\"):
        return None
    parts = rel.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    target = root.joinpath(*parts)
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target


def _try_exclusive_lock(lock_path: Path) -> Optional[Any]:
    try:
        fh = open(lock_path, "rb")
    except OSError:
        return None
    try:
        if os.name == "nt":
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


def _release_lock(fh: Any) -> None:
    try:
        if os.name == "nt":
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def _read_via_held(fh: Any, budget: _Budget) -> bytes:
    fh.seek(0)
    data = fh.read(MAX_TOTAL_BYTES + 1)
    fh.seek(0)
    budget.add(len(data))
    return data


def _inside(a: Path, b: Path) -> bool:
    try:
        a.relative_to(b)
        return True
    except ValueError:
        return False


def _fsync_dir(path: Path) -> str:
    if _WINDOWS:
        return "unsupported_windows"
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return "failed"
    try:
        try:
            os.fsync(fd)
            return "fsync_ok"
        except OSError:
            return "failed"
    finally:
        os.close(fd)


def _mkdirs_tracked(base: Path, parts: Sequence[str], created: List[Path]) -> Path:
    """Create each missing level under `base`, recording every directory created."""
    cur = base
    for part in parts:
        cur = cur / part
        try:
            os.mkdir(cur)
            created.append(cur)
        except FileExistsError:
            st = os.lstat(cur)
            if _is_reparse(st) or not stat.S_ISDIR(st.st_mode):
                raise OSError("unexpected non-directory in output tree")
    return cur


def _write_new_file(dst: Path, payload: bytes) -> None:
    with open(dst, "xb") as outf:
        outf.write(payload)
        outf.flush()
        os.fsync(outf.fileno())


def _copy_file_chunked(src: Path, dst: Path, *, budget: _Budget, expected_sha: str) -> None:
    h = hashlib.sha256()
    with _open_read(src) as inf, open(dst, "xb") as outf:
        while True:
            block = inf.read(CHUNK_SIZE)
            if not block:
                break
            budget.add(len(block))
            h.update(block)
            outf.write(block)
        outf.flush()
        os.fsync(outf.fileno())
    if h.hexdigest() != expected_sha:
        raise _SourceChanged()


def _inventory_hashes(
    store: Path,
    lock_rel: str,
    lock_fh: Any,
) -> Tuple[Dict[str, str], List[str]]:
    """Inspect every directory entry (including dirs) before skipping anything.

    Entries are counted as scandir yields them (never materialized first) and
    file bytes are counted as actually read, both against budgets."""
    hashes: Dict[str, str] = {}
    problems: List[str] = []
    budget = _Budget(MAX_TOTAL_BYTES)
    entry_count = 0
    stack: List[Tuple[Path, str]] = [(store, "")]
    while stack:
        dirpath, rel_prefix = stack.pop()
        subdirs: List[Tuple[Path, str]] = []
        try:
            with os.scandir(dirpath) as scan:
                for entry in scan:
                    entry_count += 1
                    if entry_count > MAX_TREE_ENTRIES:
                        raise _BudgetExceeded()
                    child_rel = f"{rel_prefix}/{entry.name}" if rel_prefix else entry.name
                    if _rel_depth(child_rel) > MAX_DEPTH:
                        raise _BudgetExceeded()
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        problems.append(child_rel)
                        continue
                    if _is_reparse(st):
                        problems.append(child_rel)
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        subdirs.append((Path(entry.path), child_rel))
                        continue
                    if not stat.S_ISREG(st.st_mode):
                        problems.append(child_rel)
                        continue
                    try:
                        st = os.lstat(entry.path)
                    except OSError:
                        problems.append(child_rel)
                        continue
                    if _is_reparse(st) or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                        problems.append(child_rel)
                        continue
                    if budget.used + st.st_size > budget.limit:
                        raise _BudgetExceeded()
                    if child_rel == lock_rel:
                        payload = _read_via_held(lock_fh, budget)
                        digest, n = _sha256_bytes(payload), len(payload)
                    else:
                        try:
                            digest, n = _hash_file_bounded(Path(entry.path), budget)
                        except OSError:
                            problems.append(child_rel)
                            continue
                    if n != st.st_size:
                        raise _SourceChanged()
                    hashes[child_rel] = digest
        except OSError:
            problems.append(rel_prefix or ".")
            continue
        for sub in reversed(subdirs):
            stack.append(sub)
    return hashes, problems


def _product_iter_records(
    journal_path: Path,
) -> Tuple[Dict[str, Tuple[bytes, str]], int, int]:
    proxy = _JournalReadProxy(journal_path)
    scan_state: Dict[str, int] = {"end": 0, "size": 0}
    latest: Dict[str, Tuple[bytes, str]] = {}
    retained = 0
    for key, body, _fence in LocalBackend._iter_journal_records(proxy, scan_state):
        prev = latest.get(key)
        if prev is not None:
            retained -= len(prev[0])
        retained += len(body)
        latest[key] = (body, sha256_hex(body))
        if retained > MAX_RETAINED_BODY_BYTES or len(latest) > MAX_TREE_ENTRIES:
            raise _BudgetExceeded()
    return latest, int(scan_state.get("end", 0)), int(scan_state.get("size", 0))


def _classify_journal(
    latest: Dict[str, Tuple[bytes, str]],
    prefix_end: int,
    journal_size: int,
    archive_hashes: Dict[str, str],
) -> _JournalAnalysis:
    invalid_keys: Set[str] = set()
    for key in latest:
        if not gate._valid_key_shape(key):
            invalid_keys.add(key)
    folded: Dict[str, List[str]] = {}
    for key in latest:
        if key in invalid_keys:
            continue
        folded.setdefault(key.casefold(), []).append(key)
    case_fold: Set[str] = set()
    for keys in folded.values():
        if len(keys) > 1:
            case_fold.update(keys)
    findings: List[str] = []
    if invalid_keys:
        findings.append("INVALID_KEYS_NOT_MATERIALIZED")
    if case_fold:
        findings.append("CASE_FOLD_COLLISION_NOT_MATERIALIZED")
    if prefix_end < journal_size:
        findings.append("TAIL_UNEXAMINED_AMBIGUOUS")
    published_mismatch: List[str] = []
    prefix_hashes = {k: v[1] for k, v in latest.items()}
    for rel, h in archive_hashes.items():
        if not rel.startswith("data/"):
            continue
        key = rel[5:]
        if key not in prefix_hashes or prefix_hashes[key] != h:
            published_mismatch.append(key)
    for key in prefix_hashes:
        if f"data/{key}" not in archive_hashes:
            published_mismatch.append(key)
    if published_mismatch:
        findings.append("PUBLISHED_DATA_DIFFERS_FROM_PREFIX")
    materialized: Dict[str, str] = {}
    for key, (_body, h) in latest.items():
        if key in invalid_keys or key in case_fold:
            continue
        materialized[key] = h
    return _JournalAnalysis(
        latest=latest,
        prefix_end=prefix_end,
        journal_size=journal_size,
        invalid_keys=invalid_keys,
        case_fold_collisions=case_fold,
        findings=sorted(set(findings)),
        published_mismatch_keys=sorted(set(published_mismatch)),
        materialized=materialized,
    )


def _analyze_archived_journal(
    journal_path: Path, archive_hashes: Dict[str, str]
) -> _JournalAnalysis:
    latest, prefix_end, journal_size = _product_iter_records(journal_path)
    return _classify_journal(latest, prefix_end, journal_size, archive_hashes)


def _candidate_within_budget(materialized: Dict[str, str]) -> bool:
    """Candidate tree (files + parent dirs) must fit verify's per-tree budgets."""
    dirs: Set[str] = set()
    for key in materialized:
        parts = key.split("/")
        if len(parts) > MAX_DEPTH:
            return False
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    return len(dirs) + len(materialized) <= MAX_TREE_ENTRIES


def _normalize_store_path(store: os.PathLike[str] | str) -> Tuple[Optional[Path], Optional[str]]:
    raw = Path(store)
    if not raw.is_absolute():
        raw = Path(os.path.abspath(str(raw)))
    try:
        st = os.lstat(raw)
    except OSError:
        return None, "REFUSE_STORE_UNREADABLE"
    if stat.S_ISLNK(st.st_mode) or _is_reparse(st):
        return None, "REFUSE_STORE_ROOT_SYMLINK_OR_REPARSE"
    if not stat.S_ISDIR(st.st_mode):
        return None, "REFUSE_NOT_A_STORE"
    return raw, None


def export_store(
    store: os.PathLike[str] | str,
    out_parent: os.PathLike[str] | str,
    *,
    hook: Optional[Any] = None,
) -> Dict[str, Any]:
    store_p, store_err = _normalize_store_path(store)
    if store_err:
        return {"code": store_err}
    assert store_p is not None

    out_p = Path(out_parent)
    if not out_p.is_absolute():
        out_p = Path(os.path.abspath(str(out_p)))
    if not out_p.is_dir():
        return {"code": "REFUSE_OUTPUT_PARENT_MISSING"}
    try:
        rs = Path(os.path.realpath(store_p))
        ro = Path(os.path.realpath(out_p))
    except OSError:
        return {"code": "REFUSE_STORE_UNREADABLE"}
    if os.path.normcase(str(rs)) == os.path.normcase(str(ro)) or _inside(ro, rs) or _inside(rs, ro):
        return {"code": "REFUSE_OUTPUT_OVERLAPS_STORE"}

    lock_path = store_p / "locks" / "cas.lock"
    journal_path = store_p / "journal" / "journal.log"
    try:
        lock_st = os.lstat(lock_path)
    except OSError:
        return {"code": "REFUSE_LOCK_MISSING"}
    if not stat.S_ISREG(lock_st.st_mode) or stat.S_ISLNK(lock_st.st_mode):
        return {"code": "REFUSE_LOCK_MISSING"}
    lock_fh = _try_exclusive_lock(lock_path)
    if lock_fh is None:
        return {"code": "REFUSE_BUSY_WRITER_ACTIVE"}
    attempt_dir: Optional[Path] = None
    try:
        try:
            j_st = os.lstat(journal_path)
        except OSError:
            return {"code": "REFUSE_JOURNAL_MISSING"}
        if not stat.S_ISREG(j_st.st_mode) or stat.S_ISLNK(j_st.st_mode):
            return {"code": "REFUSE_JOURNAL_MISSING"}

        try:
            hashes1, odd = _inventory_hashes(store_p, LOCK_REL, lock_fh)
        except _BudgetExceeded:
            return {"code": "REFUSE_BUDGET_EXCEEDED"}
        except _SourceChanged:
            return {"code": "REFUSE_STORE_CHANGED_DURING_EXPORT"}
        if odd:
            return {"code": "REFUSE_SYMLINK_HARDLINK_OR_SPECIAL", "paths": sorted(odd)}
        if hook:
            hook("after_inventory_pass1")
        try:
            hashes2, odd2 = _inventory_hashes(store_p, LOCK_REL, lock_fh)
        except _BudgetExceeded:
            return {"code": "REFUSE_BUDGET_EXCEEDED"}
        except _SourceChanged:
            return {"code": "REFUSE_STORE_CHANGED_DURING_EXPORT"}
        if odd2:
            return {"code": "REFUSE_SYMLINK_HARDLINK_OR_SPECIAL", "paths": sorted(odd2)}
        if hashes1 != hashes2:
            return {"code": "REFUSE_STORE_CHANGED_DURING_EXPORT"}

        journal_sha = hashes1.get(JOURNAL_REL, "")
        export_name = "recovery-export-" + journal_sha[:12]
        final_dir = out_p / export_name
        if final_dir.exists():
            return {"code": "REFUSE_OUTPUT_EXISTS", "name": export_name}

        attempt_name = f".incomplete-{export_name}-{secrets.token_hex(8)}"
        attempt_dir = out_p / attempt_name
        try:
            os.mkdir(attempt_dir)
        except FileExistsError:
            return {"code": "REFUSE_ATTEMPT_DIR_COLLISION"}
        except OSError:
            return {"code": "REFUSE_OUTPUT_IO_ERROR"}

        attempt_path = attempt_dir / "ATTEMPT.json"
        try:
            with open(attempt_path, "x", encoding="utf-8") as af:
                json.dump(
                    {"status": "incomplete", "journal_sha256": journal_sha, "started_epoch": time.time()},
                    af,
                    indent=1,
                    sort_keys=True,
                )
                af.write("\n")
                af.flush()
                os.fsync(af.fileno())
        except OSError:
            return {"code": "REFUSE_OUTPUT_IO_ERROR", "attempt": attempt_name}

        archive_manifest: Dict[str, str] = {}
        budget = _Budget(MAX_TOTAL_BYTES)
        created_dirs: List[Path] = []
        try:
            archive_dir = _mkdirs_tracked(attempt_dir, ["archive"], created_dirs)
            for rel in sorted(hashes1):
                parts = rel.split("/")
                if hook:
                    hook("copying:" + rel)
                dst_parent = _mkdirs_tracked(archive_dir, parts[:-1], created_dirs)
                dst = dst_parent / parts[-1]
                if rel == LOCK_REL:
                    payload = _read_via_held(lock_fh, budget)
                    if _sha256_bytes(payload) != hashes1[rel]:
                        raise _SourceChanged()
                    _write_new_file(dst, payload)
                else:
                    _copy_file_chunked(
                        store_p.joinpath(*parts), dst, budget=budget, expected_sha=hashes1[rel]
                    )
                archive_manifest[rel] = hashes1[rel]
                if hook:
                    hook("copied:" + rel)
        except _BudgetExceeded:
            return {"code": "REFUSE_BUDGET_EXCEEDED", "attempt": attempt_name}
        except _SourceChanged:
            return {"code": "REFUSE_STORE_CHANGED_DURING_EXPORT", "attempt": attempt_name}
        except OSError:
            return {"code": "REFUSE_OUTPUT_IO_ERROR", "attempt": attempt_name}

        try:
            hashes3, odd3 = _inventory_hashes(store_p, LOCK_REL, lock_fh)
        except _BudgetExceeded:
            return {"code": "REFUSE_BUDGET_EXCEEDED", "attempt": attempt_name}
        except _SourceChanged:
            return {"code": "REFUSE_STORE_CHANGED_DURING_EXPORT", "attempt": attempt_name}
        if odd3:
            return {"code": "REFUSE_SYMLINK_HARDLINK_OR_SPECIAL", "paths": sorted(odd3), "attempt": attempt_name}
        if hashes3 != hashes1:
            return {"code": "REFUSE_STORE_CHANGED_DURING_EXPORT", "attempt": attempt_name}

        verify_budget = _Budget(MAX_TOTAL_BYTES)
        try:
            for rel, expected in archive_manifest.items():
                ap = _safe_archive_join(archive_dir, rel)
                if ap is None or not ap.is_file():
                    return {"code": "REFUSE_ARCHIVE_VERIFY_FAILED", "path": rel, "attempt": attempt_name}
                if _hash_file_bounded(ap, verify_budget)[0] != expected:
                    return {"code": "REFUSE_ARCHIVE_VERIFY_FAILED", "path": rel, "attempt": attempt_name}
            analysis = _analyze_archived_journal(archive_dir / "journal" / "journal.log", archive_manifest)
        except _BudgetExceeded:
            return {"code": "REFUSE_BUDGET_EXCEEDED", "attempt": attempt_name}
        except (OSError, BackendCorruptionError):
            return {"code": "REFUSE_ARCHIVE_VERIFY_FAILED", "attempt": attempt_name}

        if not _candidate_within_budget(analysis.materialized):
            return {"code": "REFUSE_BUDGET_EXCEEDED", "attempt": attempt_name}
        materialized_written: Dict[str, str] = {}
        try:
            for key in sorted(analysis.materialized.keys()):
                body, body_hash = analysis.latest[key]
                parts = key.split("/")
                parent = _mkdirs_tracked(attempt_dir, ["candidate-data", *parts[:-1]], created_dirs)
                _write_new_file(parent / parts[-1], body)
                materialized_written[key] = body_hash
        except OSError:
            return {"code": "REFUSE_OUTPUT_IO_ERROR", "attempt": attempt_name}

        dir_fsync_results: Dict[str, str] = {}
        for d in sorted(set(created_dirs), key=lambda p: len(p.parts), reverse=True):
            dir_fsync_results[d.relative_to(attempt_dir).as_posix()] = _fsync_dir(d)
        dir_fsync_results["."] = _fsync_dir(attempt_dir)
        if "failed" in dir_fsync_results.values():
            return {
                "code": "REFUSE_OUTPUT_DURABILITY_FAILED",
                "attempt": attempt_name,
                "directory_fsync_results": dir_fsync_results,
            }

        manifest_core: Dict[str, Any] = {
            "schema": "recovery_export_v1",
            "source_inventory_sha256": dict(sorted(archive_manifest.items())),
            "journal_prefix_end": analysis.prefix_end,
            "journal_size": analysis.journal_size,
            "prefix_key_hashes": {k: analysis.latest[k][1] for k in sorted(analysis.latest)},
            "candidate_materialized_keys": sorted(materialized_written.keys()),
            "findings": analysis.findings,
            "invalid_keys": sorted(analysis.invalid_keys),
            "case_fold_collisions": sorted(analysis.case_fold_collisions),
            "published_mismatch_keys": analysis.published_mismatch_keys,
            "unparsed_byte_range": [analysis.prefix_end, analysis.journal_size]
            if analysis.prefix_end < analysis.journal_size
            else None,
            "completeness_proven": False,
            "candidate_is_authoritative": False,
            "external_anchor_present": False,
            "platform": os.name,
            "limits": {
                "max_total_bytes": MAX_TOTAL_BYTES,
                "max_tree_entries": MAX_TREE_ENTRIES,
                "max_depth": MAX_DEPTH,
                "chunk_size": CHUNK_SIZE,
            },
            "durability_notes": {
                "per_file_fsync": True,
                "directory_fsync_posix": "every_created_directory_bottom_up_then_attempt_root",
                "directory_fsync_after_manifest": "attempt_root_and_output_parent_reported_in_result_only",
                "directory_fsync_windows": "unsupported_no_directory_fsync",
                "directory_fsync_results": dir_fsync_results,
                "final_rename_atomic": "name_swap_only_not_power_loss_proof",
                "manifest_hash_is_not_authentication": True,
            },
        }
        manifest_body = json.dumps(manifest_core, indent=1, sort_keys=True).encode("utf-8") + b"\n"
        manifest_core["manifest_sha256"] = _sha256_bytes(manifest_body)
        manifest_bytes = json.dumps(manifest_core, indent=1, sort_keys=True).encode("utf-8") + b"\n"
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            return {"code": "REFUSE_BUDGET_EXCEEDED", "attempt": attempt_name}
        manifest_path = attempt_dir / "MANIFEST.json"
        try:
            _write_new_file(manifest_path, manifest_bytes)
        except OSError:
            return {"code": "REFUSE_OUTPUT_IO_ERROR", "attempt": attempt_name}
        post: Dict[str, str] = {"attempt_root_after_manifest": _fsync_dir(attempt_dir)}
        if post["attempt_root_after_manifest"] == "failed":
            return {"code": "REFUSE_OUTPUT_DURABILITY_FAILED", "attempt": attempt_name, "post_manifest_fsync": post}
        if hook:
            hook("manifest_written")

        if os.path.lexists(final_dir):
            return {"code": "REFUSE_OUTPUT_EXISTS", "name": export_name, "attempt": attempt_name}

        try:
            os.rename(attempt_dir, final_dir)
        except OSError:
            return {"code": "REFUSE_OUTPUT_IO_ERROR", "attempt": attempt_name}
        post["output_parent_after_rename"] = _fsync_dir(out_p)

        if _WINDOWS:
            durability = "unsupported_windows_no_directory_fsync"
        elif post["output_parent_after_rename"] == "fsync_ok":
            durability = "posix_directory_fsync_ok_not_power_loss_proof"
        else:
            durability = "posix_output_parent_fsync_failed"
        if durability == "posix_output_parent_fsync_failed":
            code = "EXPORT_DURABILITY_UNCONFIRMED"
        elif analysis.findings:
            code = "EXPORT_INCOMPLETE_HUMAN_DECISION_REQUIRED"
        else:
            code = "EXPORT_SYNTACTICALLY_CONSISTENT_COMPLETENESS_UNPROVEN"
        return {
            "code": code,
            "output": str(final_dir),
            "name": export_name,
            "findings": analysis.findings,
            "completeness_proven": False,
            "candidate_is_authoritative": False,
            "manifest_sha256": manifest_core["manifest_sha256"],
            "journal_prefix_end": analysis.prefix_end,
            "directory_durability": durability,
            "post_manifest_fsync": post,
        }
    finally:
        _release_lock(lock_fh)


_EXPORT_SUCCESS_CODES = frozenset(
    {
        "EXPORT_INCOMPLETE_HUMAN_DECISION_REQUIRED",
        "EXPORT_SYNTACTICALLY_CONSISTENT_COMPLETENESS_UNPROVEN",
    }
)
_ROOT_ALLOWED = frozenset({"MANIFEST.json", "ATTEMPT.json", "archive", "candidate-data"})
_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "source_inventory_sha256",
        "journal_prefix_end",
        "journal_size",
        "prefix_key_hashes",
        "candidate_materialized_keys",
        "findings",
        "invalid_keys",
        "case_fold_collisions",
        "published_mismatch_keys",
        "unparsed_byte_range",
        "completeness_proven",
        "candidate_is_authoritative",
        "external_anchor_present",
        "platform",
        "limits",
        "durability_notes",
        "manifest_sha256",
    }
)
_LIMIT_FIELDS = frozenset({"max_total_bytes", "max_tree_entries", "max_depth", "chunk_size"})
_NOTE_FIELDS: Dict[str, Any] = {
    "per_file_fsync": bool,
    "directory_fsync_posix": str,
    "directory_fsync_after_manifest": str,
    "directory_fsync_windows": str,
    "directory_fsync_results": dict,
    "final_rename_atomic": str,
    "manifest_hash_is_not_authentication": bool,
}
_STR_LIST_FIELDS = (
    "candidate_materialized_keys",
    "findings",
    "invalid_keys",
    "case_fold_collisions",
    "published_mismatch_keys",
)


def _reject(reason: str, **extra: Any) -> Dict[str, Any]:
    return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": reason, **extra}


def _is_hex64(v: Any) -> bool:
    return isinstance(v, str) and len(v) == 64 and all(c in _HEX for c in v)


def _is_nonneg_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _is_relative_label(v: Any) -> bool:
    if not isinstance(v, str) or v == "":
        return False
    if v == ".":
        return True
    if v.startswith(("/", "\\")) or (len(v) > 1 and v[1] == ":"):
        return False
    return all(p not in ("", ".", "..") for p in v.replace("\\", "/").split("/"))


def _validate_manifest_shape(m: Any) -> Optional[str]:
    if not isinstance(m, dict):
        return "manifest_not_object"
    if set(m) != _MANIFEST_FIELDS:
        return "manifest_unknown_or_missing_field"
    if m.get("schema") != "recovery_export_v1":
        return "manifest_schema"
    for field in ("completeness_proven", "candidate_is_authoritative", "external_anchor_present"):
        if m.get(field) is not False:
            return f"flag_{field}"
    if not _is_hex64(m.get("manifest_sha256")):
        return "manifest_sha256_shape"
    inv = m.get("source_inventory_sha256")
    if not isinstance(inv, dict) or len(inv) > MAX_TREE_ENTRIES:
        return "source_inventory_shape"
    if not all(isinstance(k, str) and _is_hex64(v) for k, v in inv.items()):
        return "source_inventory_shape"
    pkh = m.get("prefix_key_hashes")
    if not isinstance(pkh, dict) or len(pkh) > MAX_TREE_ENTRIES:
        return "prefix_key_hashes_shape"
    if not all(isinstance(k, str) and _is_hex64(v) for k, v in pkh.items()):
        return "prefix_key_hashes_shape"
    for field in ("journal_prefix_end", "journal_size"):
        if not _is_nonneg_int(m.get(field)):
            return f"{field}_shape"
    if m["journal_prefix_end"] > m["journal_size"]:
        return "journal_prefix_end_shape"
    for field in _STR_LIST_FIELDS:
        v = m.get(field)
        cap = 2 * MAX_TREE_ENTRIES if field == "published_mismatch_keys" else MAX_TREE_ENTRIES
        if not isinstance(v, list) or len(v) > cap or not all(isinstance(x, str) for x in v):
            return f"{field}_shape"
    if not isinstance(m.get("platform"), str):
        return "platform_shape"
    lim = m.get("limits")
    if not isinstance(lim, dict) or set(lim) != _LIMIT_FIELDS or not all(_is_nonneg_int(v) for v in lim.values()):
        return "limits_shape"
    ur = m.get("unparsed_byte_range", "missing")
    if ur is not None and not (
        isinstance(ur, list) and len(ur) == 2 and all(_is_nonneg_int(x) for x in ur)
    ):
        return "unparsed_byte_range_shape"
    notes = m.get("durability_notes")
    if not isinstance(notes, dict) or set(notes) != set(_NOTE_FIELDS):
        return "durability_notes_shape"
    for k, typ in _NOTE_FIELDS.items():
        if not isinstance(notes[k], typ):
            return "durability_notes_shape"
    results = notes["directory_fsync_results"]
    if len(results) > 2 * MAX_TREE_ENTRIES + 1:
        return "durability_notes_shape"
    for k, v in results.items():
        if not _is_relative_label(k):
            return "manifest_absolute_or_unsafe_path"
        if v not in ("fsync_ok", "failed", "unsupported_windows"):
            return "durability_notes_shape"
    return None


def _lstat_kind(path: Path) -> str:
    st = os.lstat(path)
    if _is_reparse(st):
        return "link"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    if stat.S_ISREG(st.st_mode):
        return "file" if st.st_nlink == 1 else "hardlink"
    return "special"


def _bounded_file_set(root: Path) -> Dict[str, int]:
    """rel -> st_size via lstat only; rejects links/reparse/hard links/special files.

    Regular-file link counts come from os.lstat(path), not DirEntry.stat(),
    whose Windows metadata does not carry st_nlink. Each tree has its own
    MAX_TREE_ENTRIES budget, matching what export_store permits."""
    out: Dict[str, int] = {}
    counter = [0]
    stack: List[Tuple[Path, str]] = [(root, "")]
    while stack:
        dirpath, prefix = stack.pop()
        with os.scandir(dirpath) as it:
            for ent in it:
                counter[0] += 1
                if counter[0] > MAX_TREE_ENTRIES:
                    raise _VerifyReject("verify_entry_budget_exceeded")
                rel = f"{prefix}/{ent.name}" if prefix else ent.name
                if _rel_depth(rel) > MAX_DEPTH:
                    raise _VerifyReject("verify_depth_budget_exceeded")
                st = ent.stat(follow_symlinks=False)
                if _is_reparse(st):
                    raise _VerifyReject("link_or_reparse", path=rel)
                if stat.S_ISDIR(st.st_mode):
                    stack.append((Path(ent.path), rel))
                    continue
                if not stat.S_ISREG(st.st_mode):
                    raise _VerifyReject("special_file", path=rel)
                st = os.lstat(ent.path)
                if _is_reparse(st):
                    raise _VerifyReject("link_or_reparse", path=rel)
                if not stat.S_ISREG(st.st_mode):
                    raise _VerifyReject("special_file", path=rel)
                if st.st_nlink != 1:
                    raise _VerifyReject("hardlink", path=rel)
                out[rel] = st.st_size
    return out


def _hash_expected(root: Path, sizes: Dict[str, int], expected: Dict[str, str], budget: _Budget, label: str) -> None:
    for rel in sorted(expected):
        path = _safe_archive_join(root, rel)
        if path is None:
            raise _VerifyReject(f"{label}_path_traversal", path=rel)
        digest, n = _hash_file_bounded(path, budget)
        if n != sizes[rel]:
            raise _VerifyReject(f"{label}_changed_during_verify", path=rel)
        if digest != expected[rel]:
            raise _VerifyReject(f"{label}_hash_mismatch", path=rel)


def _check_file_set(actual: Dict[str, int], expected: Set[str], label: str) -> None:
    missing = sorted(expected - set(actual))
    extra = sorted(set(actual) - expected)
    if missing:
        raise _VerifyReject(f"{label}_missing_files", paths=missing[:20])
    if extra:
        raise _VerifyReject(f"{label}_extra_files", paths=extra[:20])


def _verify(root: Path) -> Dict[str, Any]:
    if root.name.startswith(".incomplete-"):
        raise _VerifyReject("incomplete_attempt_root")
    if _lstat_kind(root) != "dir":
        raise _VerifyReject("root_not_plain_directory")
    top: Dict[str, str] = {}
    with os.scandir(root) as it:
        for ent in it:
            if ent.name not in _ROOT_ALLOWED:
                raise _VerifyReject("root_extra_entry", path=ent.name)
            top[ent.name] = _lstat_kind(Path(ent.path))
    if "MANIFEST.json" not in top or "archive" not in top:
        raise _VerifyReject("layout_missing")
    for name, want in (
        ("MANIFEST.json", "file"),
        ("ATTEMPT.json", "file"),
        ("archive", "dir"),
        ("candidate-data", "dir"),
    ):
        if name in top and top[name] != want:
            raise _VerifyReject("root_entry_unsafe", path=name, kind=top[name])

    manifest_path = root / "MANIFEST.json"
    if os.lstat(manifest_path).st_size > MAX_MANIFEST_BYTES:
        raise _VerifyReject("manifest_too_large")
    with _open_read(manifest_path) as f:
        raw = f.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise _VerifyReject("manifest_too_large")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _VerifyReject("manifest_invalid") from None
    shape = _validate_manifest_shape(manifest)
    if shape:
        raise _VerifyReject(shape)
    body_copy = dict(manifest)
    embedded_sha = body_copy.pop("manifest_sha256")
    canonical = json.dumps(body_copy, indent=1, sort_keys=True).encode("utf-8") + b"\n"
    recomputed_sha = _sha256_bytes(canonical)
    if embedded_sha != recomputed_sha:
        raise _VerifyReject("manifest_hash_mismatch")

    expected_inv: Dict[str, str] = manifest["source_inventory_sha256"]
    for rel in expected_inv:
        if _safe_archive_join(Path("."), rel) is None or "\\" in rel:
            raise _VerifyReject("manifest_path_traversal", path=rel)
    if JOURNAL_REL not in expected_inv:
        raise _VerifyReject("journal_missing")

    archive_root = root / "archive"
    candidate_root = root / "candidate-data"
    archive_sizes = _bounded_file_set(archive_root)
    _check_file_set(archive_sizes, set(expected_inv), "archive")
    candidate_sizes = _bounded_file_set(candidate_root) if "candidate-data" in top else {}
    if sum(archive_sizes.values()) > MAX_TOTAL_BYTES:
        raise _VerifyReject("verify_byte_budget_exceeded")
    if sum(candidate_sizes.values()) > MAX_RETAINED_BODY_BYTES:
        raise _VerifyReject("verify_byte_budget_exceeded")

    budget = _Budget(MAX_TOTAL_BYTES + MAX_RETAINED_BODY_BYTES)
    _hash_expected(archive_root, archive_sizes, expected_inv, budget, "archive")

    analysis = _analyze_archived_journal(archive_root / "journal" / "journal.log", expected_inv)
    checks = (
        ("prefix_end_mismatch", analysis.prefix_end, manifest["journal_prefix_end"]),
        ("journal_size_mismatch", analysis.journal_size, manifest["journal_size"]),
        (
            "prefix_hashes_mismatch",
            {k: analysis.latest[k][1] for k in sorted(analysis.latest)},
            manifest["prefix_key_hashes"],
        ),
        ("invalid_keys_mismatch", sorted(analysis.invalid_keys), manifest["invalid_keys"]),
        ("case_collisions_mismatch", sorted(analysis.case_fold_collisions), manifest["case_fold_collisions"]),
        ("findings_mismatch", analysis.findings, manifest["findings"]),
        ("published_mismatch_mismatch", analysis.published_mismatch_keys, manifest["published_mismatch_keys"]),
        (
            "unparsed_range_mismatch",
            [analysis.prefix_end, analysis.journal_size] if analysis.prefix_end < analysis.journal_size else None,
            manifest["unparsed_byte_range"],
        ),
        ("candidate_keys_mismatch", sorted(analysis.materialized), manifest["candidate_materialized_keys"]),
    )
    for reason, derived, recorded in checks:
        if derived != recorded:
            raise _VerifyReject(reason)

    _check_file_set(candidate_sizes, set(analysis.materialized), "candidate")
    _hash_expected(candidate_root, candidate_sizes, dict(analysis.materialized), budget, "candidate")
    return {
        "code": "VERIFY_OK",
        "completeness_proven": False,
        "candidate_is_authoritative": False,
        "source_authenticated": False,
        "journal_prefix_end": analysis.prefix_end,
        "manifest_sha256": recomputed_sha,
    }


def verify_export(export_dir: os.PathLike[str] | str) -> Dict[str, Any]:
    """Recompute every derivable field from the archive.

    Links, reparse points, hard links and special files are refused by lstat
    before any data read. That holds only on a cooperative, stable filesystem:
    a path swapped for a link between the lstat and the open is not detected
    (no O_NOFOLLOW/openat). VERIFY_OK means internal consistency only: it does not authenticate the
    source, prove completeness, or make the candidate authoritative."""
    root = Path(os.path.abspath(str(export_dir)))
    try:
        return _verify(root)
    except _VerifyReject as rej:
        return _reject(rej.reason, **rej.extra)
    except _BudgetExceeded:
        return _reject("verify_budget_exceeded")
    except BackendCorruptionError:
        return _reject("journal_unreadable")
    except OSError:
        return _reject("io_error")


def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Experimental recovery export (read-only toward store).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_exp = sub.add_parser("export", help="Export evidence from a local store root")
    p_exp.add_argument("store")
    p_exp.add_argument("out_parent")
    p_ver = sub.add_parser("verify", help="Verify an export directory")
    p_ver.add_argument("export_dir")
    args = parser.parse_args(argv)
    if args.cmd == "export":
        result = export_store(args.store, args.out_parent)
        print(json.dumps(result, indent=1, sort_keys=True))
        return 0 if result.get("code") in _EXPORT_SUCCESS_CODES else 1
    result = verify_export(args.export_dir)
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0 if result.get("code") == "VERIFY_OK" else 1


if __name__ == "__main__":
    sys.exit(_main())
