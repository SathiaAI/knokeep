"""Synthetic tests for recovery_export_v1 (Linux). Windows: UNVERIFIED."""
from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[2]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from store.local import LocalBackend  # noqa: E402

from experiments.recovery_export_v1 import export as rex  # noqa: E402

MAGIC = b"KKJ2"


def _frame(key: str, body: bytes, *, v2: bool = True, fence: int = 0, body_len: int | None = None) -> bytes:
    kb = key.encode()
    bl = len(body) if body_len is None else body_len
    out = (MAGIC if v2 else b"") + struct.pack(">I", len(kb)) + kb
    out += struct.pack(">Q", bl) + body + hashlib.sha256(body).digest()
    if v2:
        out += struct.pack(">Q", fence)
    return out


def _minimal_store(base: Path, journal: bytes, data: dict[str, bytes] | None = None) -> Path:
    store = base / "store"
    (store / "locks").mkdir(parents=True)
    (store / "journal").mkdir()
    (store / "data").mkdir()
    (store / "staging").mkdir()
    (store / "locks" / "cas.lock").write_bytes(b"held\n")
    (store / "journal" / "journal.log").write_bytes(journal)
    for rel, body in (data or {}).items():
        p = store / "data" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
    return store


class RecoveryExportTests(unittest.TestCase):
    def test_clean_parse_still_not_complete(self) -> None:
        key, body = "docs/a", b"#knokeep-gen:1\nok\n"
        j = _frame(key, body)
        with tempfile.TemporaryDirectory() as td:
            store = _minimal_store(Path(td), j, {"docs/a": body})
            out = Path(td) / "out"
            out.mkdir()
            r = rex.export_store(store, out)
            self.assertTrue(r["code"].startswith("EXPORT_"))
            self.assertFalse(r["completeness_proven"])
            v = rex.verify_export(Path(r["output"]))
            self.assertEqual(v["code"], "VERIFY_OK")

    def test_empty_journal_export_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _minimal_store(Path(td), b"")
            out = Path(td) / "out"
            out.mkdir()
            r = rex.export_store(store, out)
            self.assertTrue(r["code"].startswith("EXPORT_"))
            root = Path(r["output"])
            cand_root = root / "candidate-data"
            if cand_root.is_dir():
                self.assertEqual(list(cand_root.rglob("*")), [])
            self.assertEqual(rex.verify_export(root)["code"], "VERIFY_OK")

    def test_product_parser_used_via_unbound_iter(self) -> None:
        body = b"x"
        j = _frame("a/k", body) + _frame("a/k", body)[:-8]
        with tempfile.TemporaryDirectory() as td:
            jp = _minimal_store(Path(td), j) / "journal" / "journal.log"
            proxy = rex._JournalReadProxy(jp)
            st: dict = {"end": 0, "size": 0}
            keys = [k for k, _b, _f in LocalBackend._iter_journal_records(proxy, st)]
            self.assertEqual(keys, ["a/k"])
            latest, end, size = rex._product_iter_records(jp)
            self.assertEqual(end, st["end"])
            self.assertIn("a/k", latest)

    def test_repeated_key_materializes_latest_only(self) -> None:
        old, new = b"old-bytes", b"new-bytes"
        j = _frame("p/k", old) + _frame("p/k", new)
        with tempfile.TemporaryDirectory() as td:
            store = _minimal_store(Path(td), j, {"p/k": new})
            out = Path(td) / "out"
            out.mkdir()
            r = rex.export_store(store, out)
            self.assertTrue(r["code"].startswith("EXPORT_"))
            cand = Path(r["output"]) / "candidate-data" / "p" / "k"
            self.assertEqual(cand.read_bytes(), new)
            self.assertEqual(rex.verify_export(r["output"])["code"], "VERIFY_OK")

    def test_directory_symlink_refused(self) -> None:
        j = _frame("k/a", b"x")
        with tempfile.TemporaryDirectory() as td:
            store = _minimal_store(Path(td), j, {"k/a": b"x"})
            os.symlink(Path(td) / "else", store / "linked_dir")
            (Path(td) / "else").mkdir()
            out = Path(td) / "out"
            out.mkdir()
            r = rex.export_store(store, out)
            self.assertEqual(r["code"], "REFUSE_SYMLINK_HARDLINK_OR_SPECIAL")

    def test_deep_empty_directories_refused(self) -> None:
        j = _frame("k/a", b"x")
        with tempfile.TemporaryDirectory() as td:
            store = _minimal_store(Path(td), j, {"k/a": b"x"})
            deep = store / "data" / "deep"
            deep.mkdir()
            cur = deep
            for i in range(80):
                cur = cur / f"d{i}"
                cur.mkdir()
            out = Path(td) / "out"
            out.mkdir()
            r = rex.export_store(store, out)
            self.assertEqual(r["code"], "REFUSE_BUDGET_EXCEEDED")

    def test_partial_legacy_v2_tail(self) -> None:
        leg = _frame("a/l", b"legacy", v2=False)
        v2 = _frame("a/v", b"v2body")
        j = leg + v2[: len(v2) // 2]
        with tempfile.TemporaryDirectory() as td:
            store = _minimal_store(Path(td), j)
            out = Path(td) / "out"
            out.mkdir()
            r = rex.export_store(store, out)
            self.assertIn("TAIL_UNEXAMINED_AMBIGUOUS", r.get("findings", []))

    def test_source_unchanged_after_export(self) -> None:
        key, body = "doc/x", b"#knokeep-gen:1\nv\n"
        j = _frame(key, body)
        with tempfile.TemporaryDirectory() as td:
            store = _minimal_store(Path(td), j, {"doc/x": body})
            before = {str(p.relative_to(store)): p.read_bytes() for p in store.rglob("*") if p.is_file()}
            out = Path(td) / "out"
            out.mkdir()
            rex.export_store(store, out)
            after = {str(p.relative_to(store)): p.read_bytes() for p in store.rglob("*") if p.is_file()}
            self.assertEqual(before, after)

    def test_busy_lock_refused(self) -> None:
        j = _frame("k/a", b"b")
        with tempfile.TemporaryDirectory() as td:
            store = _minimal_store(Path(td), j, {"k/a": b"b"})
            fh = rex._try_exclusive_lock(store / "locks" / "cas.lock")
            self.assertIsNotNone(fh)
            try:
                out = Path(td) / "out"
                out.mkdir()
                self.assertEqual(rex.export_store(store, out)["code"], "REFUSE_BUSY_WRITER_ACTIVE")
            finally:
                rex._release_lock(fh)

    def test_output_overlap_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _minimal_store(Path(td), _frame("k/a", b"x"), {"k/a": b"x"})
            self.assertEqual(rex.export_store(store, store)["code"], "REFUSE_OUTPUT_OVERLAPS_STORE")

    def test_invalid_keys_not_materialized(self) -> None:
        j = _frame("../escape", b"no")
        with tempfile.TemporaryDirectory() as td:
            store = _minimal_store(Path(td), j)
            out = Path(td) / "out"
            out.mkdir()
            r = rex.export_store(store, out)
            manifest = json.loads((Path(r["output"]) / "MANIFEST.json").read_text())
            self.assertIn("INVALID_KEYS_NOT_MATERIALIZED", manifest["findings"])
            self.assertEqual(len(list((Path(r["output"]) / "candidate-data").rglob("*"))), 0)

    def test_case_fold_collision(self) -> None:
        j = _frame("a/Readme", b"one") + _frame("a/readme", b"two")
        with tempfile.TemporaryDirectory() as td:
            store = _minimal_store(Path(td), j)
            out = Path(td) / "out"
            out.mkdir()
            r = rex.export_store(store, out)
            manifest = json.loads((Path(r["output"]) / "MANIFEST.json").read_text())
            self.assertIn("CASE_FOLD_COLLISION_NOT_MATERIALIZED", manifest["findings"])
            self.assertEqual(manifest["candidate_materialized_keys"], [])

    def test_interrupted_attempt_retained(self) -> None:
        j = _frame("k/a", b"data")
        with tempfile.TemporaryDirectory() as td:
            store = _minimal_store(Path(td), j, {"k/a": b"data"})
            out = Path(td) / "out"
            out.mkdir()
            hit = {"v": False}

            def hook(name: str) -> None:
                if name.startswith("copied:") and not hit["v"]:
                    hit["v"] = True
                    raise RuntimeError("interrupt")

            with self.assertRaises(RuntimeError):
                rex.export_store(store, out, hook=hook)
            incomplete = list(out.glob(".incomplete-*"))
            self.assertEqual(len(incomplete), 1)
            foreign = incomplete[0] / "foreign.txt"
            foreign.write_text("keep", encoding="utf-8")
            r2 = rex.export_store(store, out)
            self.assertTrue(r2["code"].startswith("EXPORT_"))
            self.assertEqual(foreign.read_text(encoding="utf-8"), "keep")

    def test_verify_rejects_archive_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _minimal_store(Path(td), _frame("k/a", b"x"), {"k/a": b"x"})
            out = Path(td) / "out"
            out.mkdir()
            r = rex.export_store(store, out)
            arch = Path(r["output"]) / "archive" / "data" / "k" / "a"
            arch.write_bytes(b"tampered")
            self.assertEqual(rex.verify_export(r["output"])["code"], "REJECT_INCOMPLETE_OR_ALTERED")

    def test_verify_rejects_manifest_skip_list_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = _minimal_store(Path(td), _frame("k/a", b"x"), {"k/a": b"x"})
            out = Path(td) / "out"
            out.mkdir()
            r = rex.export_store(store, out)
            root = Path(r["output"])
            cand = root / "candidate-data" / "k" / "a"
            cand.write_bytes(b"tampered")
            manifest_path = root / "MANIFEST.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["invalid_keys"] = sorted(set(manifest["invalid_keys"]) | {"k/a"})
            body = dict(manifest)
            body.pop("manifest_sha256", None)
            manifest["manifest_sha256"] = rex._sha256_bytes(
                json.dumps(body, indent=1, sort_keys=True).encode("utf-8") + b"\n"
            )
            manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            v = rex.verify_export(root)
            self.assertEqual(v["code"], "REJECT_INCOMPLETE_OR_ALTERED")
            self.assertIn(
                v.get("reason"),
                ("invalid_keys_mismatch", "candidate_hash_mismatch", "findings_mismatch"),
            )

    def test_coherent_rollback_still_unproven(self) -> None:
        body_old = b"#knokeep-gen:1\nold\n"
        body_new = b"#knokeep-gen:2\nnew\n"
        j = _frame("doc/r", body_old) + _frame("doc/r", body_new)
        truncated = j[: len(_frame("doc/r", body_old))]
        with tempfile.TemporaryDirectory() as td:
            store = _minimal_store(Path(td), truncated, {"doc/r": body_old})
            out = Path(td) / "out"
            out.mkdir()
            r = rex.export_store(store, out)
            manifest = json.loads((Path(r["output"]) / "MANIFEST.json").read_text())
            self.assertFalse(manifest["completeness_proven"])
            self.assertFalse(manifest["candidate_is_authoritative"])


if __name__ == "__main__":
    unittest.main()
