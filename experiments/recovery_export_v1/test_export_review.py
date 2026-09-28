"""Review regressions for recovery_export_v1: budgets, verifier shape/link/IO
handling, incomplete roots, durability reporting. Stdlib-only; no silent skips
of core behavior (link cases require os.symlink/os.link and fail if absent)."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PKG_ROOT = Path(__file__).resolve().parents[2]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from experiments.recovery_export_v1 import export as rex  # noqa: E402

MAGIC = b"KKJ2"


def _frame(key: str, body: bytes) -> bytes:
    kb = key.encode()
    return (
        MAGIC + struct.pack(">I", len(kb)) + kb + struct.pack(">Q", len(body)) + body
        + hashlib.sha256(body).digest() + struct.pack(">Q", 0)
    )


def _store(base: Path, journal: bytes, data: dict | None = None) -> Path:
    store = base / "store"
    for d in ("locks", "journal", "data", "staging"):
        (store / d).mkdir(parents=True)
    (store / "locks" / "cas.lock").write_bytes(b"held\n")
    (store / "journal" / "journal.log").write_bytes(journal)
    for rel, body in (data or {}).items():
        p = store / "data" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
    return store


def _snapshot(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        st = os.lstat(p)
        out[str(p.relative_to(root))] = (st.st_mode, None if p.is_dir() else p.read_bytes())
    return out


def _resign(root: Path, mutate) -> None:
    mp = root / "MANIFEST.json"
    m = json.loads(mp.read_text())
    mutate(m)
    body = dict(m)
    body.pop("manifest_sha256", None)
    m["manifest_sha256"] = rex._sha256_bytes(json.dumps(body, indent=1, sort_keys=True).encode() + b"\n")
    mp.write_text(json.dumps(m, indent=1, sort_keys=True) + "\n")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.td = Path(self._td.name)
        self.out = self.td / "out"
        self.out.mkdir()

    def tearDown(self) -> None:
        self._td.cleanup()

    def export_ok(self, journal=None, data=None):
        j = journal if journal is not None else _frame("k/a/b", b"body\n")
        store = _store(self.td, j, data if data is not None else {"k/a/b": b"body\n"})
        r = rex.export_store(store, self.out)
        self.assertIn(r["code"], rex._EXPORT_SUCCESS_CODES, r)
        return store, Path(r["output"]), r

    def assertReject(self, root, reason=None):
        v = rex.verify_export(root)
        self.assertEqual(v["code"], "REJECT_INCOMPLETE_OR_ALTERED", v)
        if reason is not None:
            self.assertEqual(v["reason"], reason, v)
        return v


class InventoryBudgetTests(_Base):
    def test_entry_budget_counts_while_scanning(self) -> None:
        store = _store(self.td, _frame("k/a", b"x"), {f"k/{i}": b"x" for i in range(200)})
        yielded = [0]
        real = os.scandir

        class Counting:
            def __init__(self, it):
                self.it = it

            def __enter__(self):
                self.it.__enter__()
                return self

            def __exit__(self, *a):
                return self.it.__exit__(*a)

            def __iter__(self):
                for e in self.it:
                    yielded[0] += 1
                    yield e

        with mock.patch.object(rex, "MAX_TREE_ENTRIES", 10), mock.patch.object(
            rex.os, "scandir", lambda p: Counting(real(p))
        ):
            r = rex.export_store(store, self.out)
        self.assertEqual(r["code"], "REFUSE_BUDGET_EXCEEDED")
        self.assertLessEqual(yielded[0], 11)

    def test_actual_bytes_counted_not_st_size(self) -> None:
        store = _store(self.td, _frame("k/a", b"x"), {"k/a": b"x"})
        real = rex._open_read

        class Lying:
            def __init__(self, f):
                self.f = f
                self.extra = True

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.f.close()

            def read(self, n):
                b = self.f.read(n)
                if not b and self.extra:
                    self.extra = False
                    return b"z" * 4096
                return b

        with mock.patch.object(rex, "_open_read", lambda p: Lying(real(p))):
            r = rex.export_store(store, self.out)
        self.assertEqual(r["code"], "REFUSE_STORE_CHANGED_DURING_EXPORT")
        with mock.patch.object(rex, "MAX_TOTAL_BYTES", 200), mock.patch.object(
            rex, "_open_read", lambda p: Lying(real(p))
        ):
            self.assertEqual(rex.export_store(store, self.out)["code"], "REFUSE_BUDGET_EXCEEDED")

    def test_retained_history_body_cap(self) -> None:
        j = _frame("k/a", b"a" * 100) + _frame("k/b", b"b" * 100)
        store = _store(self.td, j, {"k/a": b"a" * 100, "k/b": b"b" * 100})
        before = _snapshot(store)
        with mock.patch.object(rex, "MAX_RETAINED_BODY_BYTES", 150):
            r = rex.export_store(store, self.out)
        self.assertEqual(r["code"], "REFUSE_BUDGET_EXCEEDED")
        self.assertTrue((self.out / r["attempt"]).is_dir())
        self.assertEqual(_snapshot(store), before)
        with mock.patch.object(rex, "MAX_RETAINED_BODY_BYTES", 150):
            j2 = _frame("k/a", b"a" * 100) + _frame("k/a", b"c" * 100)
            latest, _e, _s = rex._product_iter_records(_store(self.td / "s2", j2) / "journal" / "journal.log")
        self.assertEqual(latest["k/a"][0], b"c" * 100)


class ExportFailureTests(_Base):
    def test_source_change_during_copy_refused_and_attempt_kept(self) -> None:
        store = _store(self.td, _frame("k/a", b"one"), {"k/a": b"one"})

        def hook(name: str) -> None:
            if name == "copying:data/k/a":
                (store / "data" / "k" / "a").write_bytes(b"two")

        r = rex.export_store(store, self.out, hook=hook)
        self.assertEqual(r["code"], "REFUSE_STORE_CHANGED_DURING_EXPORT")
        self.assertTrue((self.out / r["attempt"] / "ATTEMPT.json").is_file())
        self.assertEqual(list(self.out.glob("recovery-export-*")), [])

    def test_partial_write_failure_refuses(self) -> None:
        store = _store(self.td, _frame("k/a", b"one"), {"k/a": b"one"})
        before = _snapshot(store)
        real = rex._write_new_file

        def failing(dst, payload):
            if "candidate-data" in dst.parts:
                raise OSError(28, "No space left")
            return real(dst, payload)

        with mock.patch.object(rex, "_write_new_file", failing):
            r = rex.export_store(store, self.out)
        self.assertEqual(r["code"], "REFUSE_OUTPUT_IO_ERROR")
        self.assertTrue((self.out / r["attempt"]).is_dir())
        self.assertEqual(_snapshot(store), before)

    def test_directory_fsync_failure_is_not_success(self) -> None:
        store = _store(self.td, _frame("k/a", b"one"), {"k/a": b"one"})
        with mock.patch.object(rex, "_WINDOWS", False), mock.patch.object(rex, "_fsync_dir", lambda p: "failed"):
            r = rex.export_store(store, self.out)
        self.assertEqual(r["code"], "REFUSE_OUTPUT_DURABILITY_FAILED")
        self.assertEqual(list(self.out.glob("recovery-export-*")), [])

    def test_output_parent_fsync_failure_after_rename_unconfirmed(self) -> None:
        store = _store(self.td, _frame("k/a", b"one"), {"k/a": b"one"})
        real = rex._fsync_dir
        out = self.out
        with mock.patch.object(rex, "_WINDOWS", False), mock.patch.object(
            rex, "_fsync_dir", lambda p: "failed" if Path(p) == out else real(p)
        ):
            r = rex.export_store(store, self.out)
        self.assertEqual(r["code"], "EXPORT_DURABILITY_UNCONFIRMED")
        self.assertNotIn(r["code"], rex._EXPORT_SUCCESS_CODES)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rex._main(["verify", r["output"]]), 0)

    def test_windows_directory_durability_is_unsupported_not_ok(self) -> None:
        with mock.patch.object(rex, "_WINDOWS", True):
            self.assertEqual(rex._fsync_dir(self.out), "unsupported_windows")


class DurabilityManifestTests(_Base):
    def test_intermediate_parents_fsynced_and_paths_relative(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX directory fsync specific; Windows covered by unsupported test")
        _store_p, root, r = self.export_ok()
        raw = (root / "MANIFEST.json").read_bytes()
        self.assertNotIn(str(self.td).encode(), raw)
        res = json.loads(raw)["durability_notes"]["directory_fsync_results"]
        for rel in ("archive", "archive/data", "archive/data/k", "archive/data/k/a",
                    "candidate-data", "candidate-data/k", "candidate-data/k/a", "."):
            self.assertEqual(res.get(rel), "fsync_ok", rel)
        self.assertTrue(all(not k.startswith("/") for k in res))
        self.assertEqual(r["post_manifest_fsync"]["attempt_root_after_manifest"], "fsync_ok")
        self.assertEqual(r["post_manifest_fsync"]["output_parent_after_rename"], "fsync_ok")
        self.assertEqual(r["directory_durability"], "posix_directory_fsync_ok_not_power_loss_proof")

    def test_absolute_path_manifest_rejected(self) -> None:
        _s, root, _r = self.export_ok()
        _resign(root, lambda m: m["durability_notes"]["directory_fsync_results"].update({str(self.td): "fsync_ok"}))
        self.assertReject(root, "manifest_absolute_or_unsafe_path")


class VerifierTests(_Base):
    def test_incomplete_attempt_root_rejected_even_with_manifest(self) -> None:
        store = _store(self.td, _frame("k/a", b"x"), {"k/a": b"x"})

        def hook(name: str) -> None:
            if name == "manifest_written":
                raise RuntimeError("interrupt before rename")

        with self.assertRaises(RuntimeError):
            rex.export_store(store, self.out, hook=hook)
        (attempt,) = list(self.out.glob(".incomplete-*"))
        self.assertTrue((attempt / "MANIFEST.json").is_file())
        self.assertReject(attempt, "incomplete_attempt_root")

    def test_malformed_field_types_refuse_without_exception(self) -> None:
        mutations = {
            "source_inventory_shape": lambda m: m.update(source_inventory_sha256=["x"]),
            "journal_prefix_end_shape": lambda m: m.update(journal_prefix_end="0"),
            "findings_shape": lambda m: m.update(findings={"a": 1}),
            "invalid_keys_shape": lambda m: m.update(invalid_keys=[1]),
            "unparsed_byte_range_shape": lambda m: m.update(unparsed_byte_range="bad"),
            "prefix_key_hashes_shape": lambda m: m.update(prefix_key_hashes={"k": 5}),
            "durability_notes_shape": lambda m: m.update(durability_notes=None),
            "journal_size_shape": lambda m: m.update(journal_size=True),
        }
        for reason, mut in mutations.items():
            with self.subTest(reason=reason):
                sub = self.td / reason
                sub.mkdir()
                store = _store(sub, _frame("k/a", b"x"), {"k/a": b"x"})
                out = sub / "out"
                out.mkdir()
                root = Path(rex.export_store(store, out)["output"])
                _resign(root, mut)
                self.assertReject(root, reason)

    def test_non_object_and_oversized_manifest(self) -> None:
        _s, root, _r = self.export_ok()
        (root / "MANIFEST.json").write_text("[1, 2]")
        self.assertReject(root, "manifest_not_object")
        (root / "MANIFEST.json").write_bytes(b" " * 300)
        with mock.patch.object(rex, "MAX_MANIFEST_BYTES", 100):
            self.assertReject(root, "manifest_too_large")

    def test_missing_and_extra_files(self) -> None:
        _s, root, _r = self.export_ok()
        (root / "archive" / "data" / "extra").write_bytes(b"e")
        self.assertReject(root, "archive_extra_files")
        os.unlink(root / "archive" / "data" / "extra")
        os.unlink(root / "archive" / "data" / "k" / "a" / "b")
        self.assertReject(root, "archive_missing_files")

    def test_extra_root_entry_and_candidate_extra(self) -> None:
        _s, root, _r = self.export_ok()
        (root / "notes.txt").write_text("x")
        self.assertReject(root, "root_extra_entry")
        os.unlink(root / "notes.txt")
        (root / "candidate-data" / "k" / "z").write_bytes(b"z")
        self.assertReject(root, "candidate_extra_files")

    def test_archive_symlink_rejected_before_reading_target(self) -> None:
        _s, root, _r = self.export_ok()
        victim = root / "archive" / "data" / "k" / "a" / "b"
        outside = self.td / "outside"
        outside.write_bytes(victim.read_bytes())
        os.unlink(victim)
        os.symlink(outside, victim)
        opened = []
        real = rex._open_read
        with mock.patch.object(rex, "_open_read", lambda p: opened.append(Path(p)) or real(p)):
            self.assertReject(root, "link_or_reparse")
        self.assertNotIn(victim, opened)
        self.assertEqual(
            [p for p in opened if p.name != "MANIFEST.json"], [], "no data read before link rejection"
        )

    def test_candidate_dir_symlink_and_hardlink_rejected(self) -> None:
        _s, root, _r = self.export_ok()
        cand = root / "candidate-data"
        moved = self.td / "moved-candidate"
        os.rename(cand, moved)
        os.symlink(moved, cand)
        self.assertReject(root, "root_entry_unsafe")
        os.unlink(cand)
        os.rename(moved, cand)
        self.assertEqual(rex.verify_export(root)["code"], "VERIFY_OK")
        arch = root / "archive" / "data" / "k" / "a" / "b"
        os.link(arch, self.td / "hl")
        self.assertReject(root, "hardlink")

    def test_manifest_traversal_rejected_before_reading(self) -> None:
        _s, root, _r = self.export_ok()
        _resign(root, lambda m: m["source_inventory_sha256"].update({"../../escape": "0" * 64}))
        self.assertReject(root, "manifest_path_traversal")

    def test_verify_entry_and_byte_budgets(self) -> None:
        _s, root, _r = self.export_ok()
        with mock.patch.object(rex, "MAX_TREE_ENTRIES", 3):
            self.assertReject(root, "verify_entry_budget_exceeded")
        with mock.patch.object(rex, "MAX_TOTAL_BYTES", 10):
            self.assertReject(root, "verify_byte_budget_exceeded")

    def test_io_error_during_verify_refuses(self) -> None:
        _s, root, _r = self.export_ok()
        real = rex._open_read

        def boom(p):
            if Path(p).name != "MANIFEST.json":
                raise PermissionError(13, "denied")
            return real(p)

        with mock.patch.object(rex, "_open_read", boom):
            self.assertReject(root, "io_error")

    def test_consistent_hash_does_not_suppress_derivable_corruption(self) -> None:
        _s, root, _r = self.export_ok()
        cand = root / "candidate-data" / "k" / "a" / "b"
        cand.write_bytes(b"forged\n")
        forged = rex._sha256_bytes(b"forged\n")
        _resign(root, lambda m: m["prefix_key_hashes"].update({"k/a/b": forged}))
        self.assertReject(root, "prefix_hashes_mismatch")

    def test_verify_ok_is_not_authentication(self) -> None:
        _s, root, _r = self.export_ok()
        v = rex.verify_export(root)
        self.assertEqual(v["code"], "VERIFY_OK")
        self.assertFalse(v["source_authenticated"])
        self.assertFalse(v["completeness_proven"])


if __name__ == "__main__":
    unittest.main()
