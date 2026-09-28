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
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from store import gate
from store.local import LocalBackend
from store.types import sha256_hex

CHUNK_SIZE = 65536
MAX_TOTAL_BYTES = 64 << 20
MAX_TREE_ENTRIES = 10_000
MAX_DEPTH = 64
LOCK_REL = "locks/cas.lock"
JOURNAL_REL = "journal/journal.log"

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class _BudgetExceeded(Exception):
    pass


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


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(CHUNK_SIZE)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


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


def _read_via_held(fh: Any) -> bytes:
    fh.seek(0)
    data = fh.read()
    fh.seek(0)
    return data


def _inside(a: Path, b: Path) -> bool:
    try:
        a.relative_to(b)
        return True
    except ValueError:
        return False


def _fsync_dir(path: Path) -> str:
    if os.name == "nt":
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


def _copy_file_chunked(src: Path, dst: Path, *, bytes_budget: List[int]) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as inf, open(dst, "xb") as outf:
        while True:
            block = inf.read(CHUNK_SIZE)
            if not block:
                break
            bytes_budget[0] += len(block)
            if bytes_budget[0] > MAX_TOTAL_BYTES:
                raise _BudgetExceeded()
            outf.write(block)
        outf.flush()
        os.fsync(outf.fileno())


def _inventory_hashes(
    store: Path,
    lock_rel: str,
    lock_fh: Any,
) -> Tuple[Dict[str, str], List[str]]:
    """Inspect every directory entry (including dirs) before skipping anything."""
    hashes: Dict[str, str] = {}
    problems: List[str] = []
    total_bytes = 0
    entry_count = 0
    stack: List[Tuple[Path, str]] = [(store, "")]
    while stack:
        dirpath, rel_prefix = stack.pop()
        dir_rel = rel_prefix
        if dir_rel and _rel_depth(dir_rel) > MAX_DEPTH:
            raise _BudgetExceeded()
        if dir_rel:
            entry_count += 1
            if entry_count > MAX_TREE_ENTRIES:
                raise _BudgetExceeded()
        try:
            with os.scandir(dirpath) as scan:
                entries = list(scan)
        except OSError:
            problems.append(dir_rel or ".")
            continue
        subdirs: List[Tuple[Path, str]] = []
        for entry in entries:
            name = entry.name
            child_rel = f"{rel_prefix}/{name}" if rel_prefix else name
            if _rel_depth(child_rel) > MAX_DEPTH:
                raise _BudgetExceeded()
            entry_count += 1
            if entry_count > MAX_TREE_ENTRIES:
                raise _BudgetExceeded()
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                problems.append(child_rel)
                continue
            if _is_reparse(st) or stat.S_ISLNK(st.st_mode):
                problems.append(child_rel)
                continue
            if stat.S_ISDIR(st.st_mode):
                subdirs.append((Path(entry.path), child_rel))
                continue
            if not stat.S_ISREG(st.st_mode):
                problems.append(child_rel)
                continue
            if st.st_nlink > 1:
                problems.append(child_rel)
                continue
            total_bytes += st.st_size
            if total_bytes > MAX_TOTAL_BYTES:
                raise _BudgetExceeded()
            if child_rel == lock_rel:
                hashes[child_rel] = _sha256_bytes(_read_via_held(lock_fh))
            else:
                hashes[child_rel] = _sha256_file(Path(entry.path))
        for sub in reversed(subdirs):
            stack.append(sub)
    return hashes, problems


def _product_iter_records(
    journal_path: Path,
) -> Tuple[Dict[str, Tuple[bytes, str]], int, int]:
    proxy = _JournalReadProxy(journal_path)
    scan_state: Dict[str, int] = {"end": 0, "size": 0}
    latest: Dict[str, Tuple[bytes, str]] = {}
    for key, body, _fence in LocalBackend._iter_journal_records(proxy, scan_state):
        latest[key] = (body, sha256_hex(body))
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
        if odd:
            return {"code": "REFUSE_SYMLINK_HARDLINK_OR_SPECIAL", "paths": sorted(odd)}
        if hook:
            hook("after_inventory_pass1")
        try:
            hashes2, odd2 = _inventory_hashes(store_p, LOCK_REL, lock_fh)
        except _BudgetExceeded:
            return {"code": "REFUSE_BUDGET_EXCEEDED"}
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
        bytes_read: List[int] = [0]
        created_dirs: List[Path] = [attempt_dir / "archive"]
        try:
            for rel in sorted(hashes1):
                src = store_p.joinpath(*rel.split("/"))
                dst = attempt_dir / "archive" / Path(*rel.split("/"))
                if rel == LOCK_REL:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    created_dirs.append(dst.parent)
                    with open(dst, "xb") as outf:
                        payload = _read_via_held(lock_fh)
                        bytes_read[0] += len(payload)
                        if bytes_read[0] > MAX_TOTAL_BYTES:
                            raise _BudgetExceeded()
                        outf.write(payload)
                        outf.flush()
                        os.fsync(outf.fileno())
                else:
                    _copy_file_chunked(src, dst, bytes_budget=bytes_read)
                    created_dirs.append(dst.parent)
                archive_manifest[rel] = hashes1[rel]
                if hook:
                    hook("copied:" + rel)
        except _BudgetExceeded:
            return {"code": "REFUSE_BUDGET_EXCEEDED", "attempt": attempt_name}
        except OSError:
            return {"code": "REFUSE_OUTPUT_IO_ERROR", "attempt": attempt_name}

        try:
            hashes3, odd3 = _inventory_hashes(store_p, LOCK_REL, lock_fh)
        except _BudgetExceeded:
            return {"code": "REFUSE_BUDGET_EXCEEDED", "attempt": attempt_name}
        if odd3:
            return {"code": "REFUSE_SYMLINK_HARDLINK_OR_SPECIAL", "paths": sorted(odd3), "attempt": attempt_name}
        if hashes3 != hashes1:
            return {"code": "REFUSE_STORE_CHANGED_DURING_EXPORT", "attempt": attempt_name}

        for rel, expected in archive_manifest.items():
            ap = _safe_archive_join(attempt_dir / "archive", rel)
            if ap is None or not ap.is_file():
                return {"code": "REFUSE_ARCHIVE_VERIFY_FAILED", "path": rel, "attempt": attempt_name}
            if _sha256_file(ap) != expected:
                return {"code": "REFUSE_ARCHIVE_VERIFY_FAILED", "path": rel, "attempt": attempt_name}

        archived_journal = attempt_dir / "archive" / "journal" / "journal.log"
        analysis = _analyze_archived_journal(archived_journal, archive_manifest)

        candidate_dir = attempt_dir / "candidate-data"
        materialized_written: Dict[str, str] = {}
        try:
            for key in sorted(analysis.materialized.keys()):
                body, body_hash = analysis.latest[key]
                dst = candidate_dir.joinpath(*key.split("/"))
                dst.parent.mkdir(parents=True, exist_ok=True)
                created_dirs.append(dst.parent)
                with open(dst, "xb") as cf:
                    cf.write(body)
                    cf.flush()
                    os.fsync(cf.fileno())
                materialized_written[key] = body_hash
        except FileExistsError:
            return {"code": "REFUSE_OUTPUT_IO_ERROR", "attempt": attempt_name}
        except OSError:
            return {"code": "REFUSE_OUTPUT_IO_ERROR", "attempt": attempt_name}

        dir_fsync_results: Dict[str, str] = {}
        if os.name != "nt":
            seen: Set[str] = set()
            for d in sorted({p for p in created_dirs}, key=lambda p: len(p.parts), reverse=True):
                key = str(d)
                if key in seen:
                    continue
                seen.add(key)
                dir_fsync_results[key] = _fsync_dir(d)
            dir_fsync_results[str(out_p)] = _fsync_dir(out_p)

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
                "directory_fsync_posix": "attempted_bottom_up_where_supported",
                "directory_fsync_windows": "unsupported_no_directory_fsync",
                "directory_fsync_results": dir_fsync_results,
                "final_rename_atomic": "name_swap_only_not_power_loss_proof",
                "manifest_hash_is_not_authentication": True,
            },
        }
        manifest_body = json.dumps(manifest_core, indent=1, sort_keys=True).encode("utf-8") + b"\n"
        manifest_core["manifest_sha256"] = _sha256_bytes(manifest_body)
        manifest_bytes = json.dumps(manifest_core, indent=1, sort_keys=True).encode("utf-8") + b"\n"
        manifest_path = attempt_dir / "MANIFEST.json"
        try:
            with open(manifest_path, "xb") as mf:
                mf.write(manifest_bytes)
                mf.flush()
                os.fsync(mf.fileno())
        except OSError:
            return {"code": "REFUSE_OUTPUT_IO_ERROR", "attempt": attempt_name}

        if final_dir.exists():
            return {"code": "REFUSE_OUTPUT_EXISTS", "name": export_name, "attempt": attempt_name}

        try:
            os.rename(attempt_dir, final_dir)
        except OSError:
            return {"code": "REFUSE_OUTPUT_IO_ERROR", "attempt": attempt_name}

        code = (
            "EXPORT_INCOMPLETE_HUMAN_DECISION_REQUIRED"
            if analysis.findings
            else "EXPORT_SYNTACTICALLY_CONSISTENT_COMPLETENESS_UNPROVEN"
        )
        return {
            "code": code,
            "output": str(final_dir),
            "name": export_name,
            "findings": analysis.findings,
            "completeness_proven": False,
            "candidate_is_authoritative": False,
            "manifest_sha256": manifest_core["manifest_sha256"],
            "journal_prefix_end": analysis.prefix_end,
        }
    finally:
        _release_lock(lock_fh)


def _list_relative_files(root: Path) -> Set[str]:
    out: Set[str] = set()
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(dirnames)
        for name in filenames:
            p = Path(dirpath) / name
            rel = str(p.relative_to(root)).replace(os.sep, "/")
            out.add(rel)
    return out


def verify_export(export_dir: os.PathLike[str] | str) -> Dict[str, Any]:
    root = Path(export_dir)
    manifest_path = root / "MANIFEST.json"
    archive_root = root / "archive"
    candidate_root = root / "candidate-data"
    if not manifest_path.is_file() or not archive_root.is_dir():
        return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": "layout_missing"}
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": "manifest_invalid"}

    required_false = ("completeness_proven", "candidate_is_authoritative", "external_anchor_present")
    for field in required_false:
        if manifest.get(field) is not False:
            return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": f"flag_{field}"}

    embedded_sha = manifest.get("manifest_sha256")
    body_copy = dict(manifest)
    body_copy.pop("manifest_sha256", None)
    canonical = json.dumps(body_copy, indent=1, sort_keys=True).encode("utf-8") + b"\n"
    recomputed_sha = _sha256_bytes(canonical)
    if not embedded_sha or embedded_sha != recomputed_sha:
        return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": "manifest_hash_mismatch"}

    for key in (
        "source_inventory_sha256",
        "journal_prefix_end",
        "journal_size",
        "prefix_key_hashes",
        "candidate_materialized_keys",
        "findings",
        "invalid_keys",
        "case_fold_collisions",
        "published_mismatch_keys",
    ):
        if key not in manifest:
            return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": "manifest_field_missing", "field": key}

    expected_inv: Dict[str, str] = manifest["source_inventory_sha256"]
    for rel in expected_inv:
        if _safe_archive_join(archive_root, rel) is None:
            return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": "manifest_path_traversal", "path": rel}

    archive_files = _list_relative_files(archive_root)
    if archive_files != set(expected_inv.keys()):
        return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": "archive_file_set_mismatch"}

    for rel, expected_hash in expected_inv.items():
        ap = _safe_archive_join(archive_root, rel)
        assert ap is not None
        if _sha256_file(ap) != expected_hash:
            return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": "archive_tampered", "path": rel}

    journal_path = archive_root / "journal" / "journal.log"
    if not journal_path.is_file():
        return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": "journal_missing"}

    analysis = _analyze_archived_journal(journal_path, expected_inv)
    if analysis.prefix_end != manifest["journal_prefix_end"]:
        return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": "prefix_end_mismatch"}
    if analysis.journal_size != manifest["journal_size"]:
        return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": "journal_size_mismatch"}

    recomputed_prefix = {k: analysis.latest[k][1] for k in sorted(analysis.latest)}
    if recomputed_prefix != manifest["prefix_key_hashes"]:
        return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": "prefix_hashes_mismatch"}
    if sorted(analysis.invalid_keys) != manifest["invalid_keys"]:
        return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": "invalid_keys_mismatch"}
    if sorted(analysis.case_fold_collisions) != manifest["case_fold_collisions"]:
        return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": "case_collisions_mismatch"}
    if analysis.findings != manifest["findings"]:
        return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": "findings_mismatch"}
    if analysis.published_mismatch_keys != manifest["published_mismatch_keys"]:
        return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": "published_mismatch_mismatch"}
    expected_unparsed = (
        [analysis.prefix_end, analysis.journal_size]
        if analysis.prefix_end < analysis.journal_size
        else None
    )
    if expected_unparsed != manifest.get("unparsed_byte_range"):
        return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": "unparsed_range_mismatch"}

    expected_materialized = sorted(analysis.materialized.keys())
    if expected_materialized != manifest["candidate_materialized_keys"]:
        return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": "candidate_keys_mismatch"}

    candidate_files = _list_relative_files(candidate_root) if candidate_root.is_dir() else set()
    expected_candidate_rels: Set[str] = set()
    for key in expected_materialized:
        expected_candidate_rels.add("/".join(key.split("/")))
    if candidate_files != expected_candidate_rels:
        return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": "candidate_file_set_mismatch"}

    for key in expected_materialized:
        rel = "/".join(key.split("/"))
        cp = candidate_root / Path(*key.split("/"))
        if _sha256_file(cp) != analysis.materialized[key]:
            return {"code": "REJECT_INCOMPLETE_OR_ALTERED", "reason": "candidate_hash_mismatch", "key": key}

    extra = [p.name for p in root.iterdir() if p.name.startswith(".incomplete-")]
    if extra and manifest_path.is_file():
        pass

    return {
        "code": "VERIFY_OK",
        "completeness_proven": False,
        "candidate_is_authoritative": False,
        "journal_prefix_end": analysis.prefix_end,
        "manifest_sha256": recomputed_sha,
    }


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
        return 0 if result.get("code", "").startswith("EXPORT_") else 1
    result = verify_export(args.export_dir)
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0 if result.get("code") == "VERIFY_OK" else 1


if __name__ == "__main__":
    sys.exit(_main())
