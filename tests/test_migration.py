"""Tests for migrate/migrate.py (design/store_backend_contract.md §5).

Covers the behaviors specific to `migrate()` itself (freeze, two-pass
scan-before-upload, create-only divergence handling, read-back verification,
manifest/unexpected-key reporting, partial-failure resumability) using
`FakeBackend` for speed, plus two explicit real-backend-pair happy paths
(local -> fake, local -> objectstore against a real, in-process moto S3
server) as the task requires. The "every registered adapter as a migration
target" coverage (contract §9's mandatory-for-all-four-adapters "migration"
item) lives in `conformance/suite.py::test_migration_into_every_backend`,
which reuses that suite's existing backend_factory parametrization instead
of duplicating it here.
"""
from __future__ import annotations

import pytest

from migrate.migrate import FrozenBackend, MigrationReport, SourceFrozenError, migrate
from store import gate
from store.fake import FakeBackend
from store.local import LocalBackend
from store.objectstore import ObjectStoreBackend
from store.types import Blob, ERROR, ErrorKind, OK, sha256_hex
from tests.moto_support import (
    DUMMY_ACCESS_KEY_ID,
    DUMMY_REGION,
    DUMMY_SECRET_ACCESS_KEY,
    get_moto_endpoint,
    make_bucket,
)


def _put(backend, key: str, body: bytes):
    r = gate.persist(backend, key, body, expected_hash=None, doc_type="system_state")
    assert isinstance(r, OK), f"setup write for {key!r} failed: {r!r}"
    return r


def _make_objectstore() -> ObjectStoreBackend:
    return ObjectStoreBackend(
        endpoint=get_moto_endpoint(),
        bucket=make_bucket(),
        region=DUMMY_REGION,
        access_key_id=DUMMY_ACCESS_KEY_ID,
        secret_access_key=DUMMY_SECRET_ACCESS_KEY,
    )


# ---------------------------------------------------------------------------
# Happy path: local -> fake, local -> objectstore (moto)
# ---------------------------------------------------------------------------


def test_happy_path_local_to_fake(tmp_path):
    source = LocalBackend(tmp_path / "src")
    fixtures = {
        "docs/a": b"alpha content",
        "docs/b": b"beta content",
        "empty/c": b"",
        "nested/d/e": b"nested value",
    }
    for key, body in fixtures.items():
        _put(source, key, body)

    target = FakeBackend()
    report = migrate(source, target)

    assert report.status == "success", report.reason
    assert report.ok is True
    assert report.source_key_count == len(fixtures)
    assert report.keys_copied == len(fixtures)
    assert report.keys_already_present == 0
    assert report.keys_verified == len(fixtures)
    assert report.unexpected_target_keys == ()
    assert report.cutover_signaled is True
    assert "rollback" in report.rollback_note.lower()

    for key, body in fixtures.items():
        blob = target.read(key)
        assert blob is not None
        assert blob.body == body
        assert blob.version_hash == sha256_hex(body)

    # Source must be completely untouched (frozen for the whole operation).
    for key, body in fixtures.items():
        blob = source.read(key)
        assert blob.body == body

    source.close()


def test_happy_path_local_to_objectstore(tmp_path):
    source = LocalBackend(tmp_path / "src2")
    fixtures = {
        "obj/a": b"object store alpha",
        "obj/b": b"object store beta" * 100,  # a bit larger, still one PUT
    }
    for key, body in fixtures.items():
        _put(source, key, body)

    target = _make_objectstore()
    report = migrate(source, target)

    assert report.status == "success", report.reason
    assert report.keys_copied == len(fixtures)
    assert report.keys_verified == len(fixtures)

    for key, body in fixtures.items():
        blob = target.read(key)
        assert blob is not None
        assert blob.body == body
        assert blob.version_hash == sha256_hex(body)

    source.close()


def test_rerun_after_success_is_fully_idempotent(tmp_path):
    source = LocalBackend(tmp_path / "src3")
    fixtures = {"k1": b"v1", "k2": b"v2"}
    for key, body in fixtures.items():
        _put(source, key, body)
    target = FakeBackend()

    r1 = migrate(source, target)
    assert r1.status == "success"
    assert r1.keys_copied == 2 and r1.keys_already_present == 0

    r2 = migrate(source, target)
    assert r2.status == "success"
    assert r2.keys_copied == 0 and r2.keys_already_present == 2
    assert r2.keys_verified == 2

    source.close()


# ---------------------------------------------------------------------------
# Diverged existing target key -> abort, no overwrite
# ---------------------------------------------------------------------------


def test_diverged_target_key_aborts_without_overwriting():
    source = FakeBackend()
    _put(source, "a", b"same-on-both")
    _put(source, "b", b"source-version-of-b")

    target = FakeBackend()
    _put(target, "b", b"DIFFERENT-target-version-of-b")

    report = migrate(source, target)

    assert report.status == "aborted"
    assert report.aborted_key == "b"
    assert "diverge" in report.reason.lower()

    # Not overwritten.
    blob = target.read("b")
    assert blob.body == b"DIFFERENT-target-version-of-b"

    # Key "a" sorts before "b" and had no conflict -- it should already have
    # been copied before the abort (demonstrates the safe, resumable partial
    # state the module docstring promises).
    blob_a = target.read("a")
    assert blob_a is not None
    assert blob_a.body == b"same-on-both"
    assert report.keys_copied == 1


# ---------------------------------------------------------------------------
# Missing key on target is copied normally
# ---------------------------------------------------------------------------


def test_missing_target_key_is_copied():
    source = FakeBackend()
    _put(source, "a", b"content-a")
    _put(source, "b", b"content-b")
    _put(source, "c", b"content-c")

    target = FakeBackend()
    _put(target, "a", b"content-a")  # already correct

    report = migrate(source, target)

    assert report.status == "success", report.reason
    assert report.keys_already_present == 1
    assert report.keys_copied == 2
    for key, body in (("a", b"content-a"), ("b", b"content-b"), ("c", b"content-c")):
        blob = target.read(key)
        assert blob is not None and blob.body == body


# ---------------------------------------------------------------------------
# Extra/unexpected target key detected & reported, never deleted
# ---------------------------------------------------------------------------


def test_unexpected_target_key_reported_not_deleted():
    source = FakeBackend()
    _put(source, "a", b"content-a")
    _put(source, "b", b"content-b")

    target = FakeBackend()
    _put(target, "a", b"content-a")
    _put(target, "b", b"content-b")
    _put(target, "zzz/not-in-source", b"pre-existing unrelated data")

    report = migrate(source, target)

    assert report.status == "success", report.reason
    assert report.unexpected_target_keys == ("zzz/not-in-source",)

    # Never deleted.
    blob = target.read("zzz/not-in-source")
    assert blob is not None
    assert blob.body == b"pre-existing unrelated data"


# ---------------------------------------------------------------------------
# Planted secret in a source value -> abort before ANY upload
# ---------------------------------------------------------------------------


def test_planted_secret_aborts_before_any_upload():
    source = FakeBackend()
    _put(source, "clean/first", b"a perfectly ordinary note")
    # Plant a synthetic, non-functional secret-shaped value directly into the
    # source's internal storage, bypassing store.gate.persist() entirely --
    # gate.persist() would itself refuse to write this, so this simulates
    # data that already existed in the source before/outside today's gate
    # (e.g. written by an older version of the system). This is test-only
    # direct manipulation of FakeBackend's internal dict, exactly like
    # tests/test_local_backend.py plants raw bytes straight onto disk for its
    # own adapter-level tests -- it never constructs ScannedKey/ScannedBody.
    planted = b"api_key = sk-" + b"A" * 40  # synthetic look-alike, not a real key
    source._store["secrets/planted"] = (planted, sha256_hex(planted))
    _put(source, "clean/last", b"another perfectly ordinary note")

    target = FakeBackend()
    report = migrate(source, target)

    assert report.status == "aborted"
    assert report.aborted_key == "secrets/planted"
    assert report.secret_scan_labels
    assert "hunter2" not in " ".join(report.secret_scan_labels)
    assert planted.decode() not in " ".join(report.secret_scan_labels)

    # Nothing at all was uploaded -- not even the clean keys that sort before
    # the planted one in the manifest (two-pass scan-then-copy).
    assert report.keys_copied == 0
    assert report.keys_already_present == 0
    assert list(target.list("")) == []


def test_custom_scanner_is_additive_defense_in_depth():
    """The `scanner` kwarg governs migrate()'s OWN pre-upload scan pass, in
    ADDITION to store.gate.persist()'s own mandatory internal secret scan --
    every write to `target` goes through the gate regardless of what is
    passed here (the gate is a non-negotiable "one door", contract §1/§5;
    migrate() has no way to turn it off, nor should it). Two things follow:

      1. A custom, STRICTER scanner can abort on content the gate's own
         default scanner would have let through (it runs first, in PASS 1,
         before any write is attempted).
      2. A permissive/no-op scanner passed here can NEVER let a genuine
         secret reach target -- the gate's own mandatory scan still fires at
         write time in PASS 2 and blocks it, exactly as if no override had
         been given at all.
    """
    # (1) stricter custom scanner flags something gate.secret_scan would not.
    source = FakeBackend()
    _put(source, "notes/word", b"this body merely contains the word banana")

    def _flag_bananas(body: bytes):
        return ["contains-banana"] if b"banana" in body else []

    target = FakeBackend()
    report = migrate(source, target, scanner=_flag_bananas)
    assert report.status == "aborted"
    assert report.aborted_key == "notes/word"
    assert report.secret_scan_labels == ("contains-banana",)
    assert list(target.list("")) == []

    # (2) a permissive pre-scanner does NOT let a real secret through --
    # store.gate.persist()'s own mandatory scan still blocks it at write time.
    source2 = FakeBackend()
    planted = b"api_key = sk-" + b"A" * 40
    source2._store["secrets/planted"] = (planted, sha256_hex(planted))
    target2 = FakeBackend()

    def _never_flags(_body: bytes):
        return []

    report2 = migrate(source2, target2, scanner=_never_flags)
    assert report2.status == "aborted"
    assert report2.aborted_key == "secrets/planted"
    assert list(target2.list("")) == []


# ---------------------------------------------------------------------------
# Secret-shaped KEY NAME (not just a secret-shaped body) -> caught in Pass 1,
# raw key never disclosed in the report.
# ---------------------------------------------------------------------------


def test_secret_shaped_key_name_aborts_before_any_upload_and_key_is_not_leaked():
    """A secret-SHAPED KEY NAME must be caught in Pass 1 -- before ANY target
    write -- exactly like a secret-shaped BODY is. Unlike the body case, the
    raw key text must never appear in the abort reason or aborted_key: the
    key itself IS the secret here, so echoing it back would defeat the whole
    point of catching it."""
    source = FakeBackend()
    _put(source, "clean/before", b"ordinary")
    secret_key = "sk-" + "A" * 40  # synthetic OpenAI-style secret used AS a key name
    source._store[secret_key] = (b"ordinary value", sha256_hex(b"ordinary value"))
    _put(source, "zzz/after", b"ordinary")

    target = FakeBackend()
    report = migrate(source, target)

    assert report.status == "aborted"
    assert report.secret_scan_labels
    assert secret_key not in (report.reason or "")
    assert report.aborted_key != secret_key
    assert secret_key not in (report.aborted_key or "")

    # Nothing at all was uploaded, including keys that sort before OR after
    # the secret-shaped key name in the manifest.
    assert report.keys_copied == 0
    assert report.keys_already_present == 0
    assert list(target.list("")) == []


# ---------------------------------------------------------------------------
# OUTCOME-UNKNOWN target writes (raised exception / TIMEOUT_AFTER_COMMIT) are
# resolved via gate.reconcile() rather than assumed uncommitted.
# ---------------------------------------------------------------------------


class _TimeoutAfterCommitThenLandedBackend(FakeBackend):
    """Reports ERROR(TIMEOUT_AFTER_COMMIT) for `flaky_key`'s first write
    while the write ACTUALLY lands underneath -- simulating an ack lost
    after a real commit. migrate() must resolve this via gate.reconcile()
    and treat it as OK, not as a hard failure."""

    def __init__(self, flaky_key: str):
        super().__init__()
        self._flaky_key = flaky_key
        self._armed = True

    def write(self, key, body, *, expected_hash):
        result = super().write(key, body, expected_hash=expected_hash)
        if self._armed and key.key == self._flaky_key:
            self._armed = False
            if isinstance(result, OK):
                return ERROR(ErrorKind.TIMEOUT_AFTER_COMMIT)
        return result


def test_timeout_after_commit_that_actually_landed_is_reconciled_to_ok():
    source = FakeBackend()
    for k in ("a", "b", "c"):
        _put(source, k, f"v-{k}".encode())

    target = _TimeoutAfterCommitThenLandedBackend(flaky_key="b")
    report = migrate(source, target)

    assert report.status == "success"
    assert report.keys_copied == 3
    assert report.keys_verified == 3
    assert target.read("b").body == b"v-b"


class _TimeoutAfterCommitNeverLandedBackend(FakeBackend):
    """Reports ERROR(TIMEOUT_AFTER_COMMIT) for `flaky_key` WITHOUT writing
    anything underneath -- migrate() must resolve this via gate.reconcile(),
    find nothing there, and abort rather than silently treating it as
    success."""

    def __init__(self, flaky_key: str):
        super().__init__()
        self._flaky_key = flaky_key

    def write(self, key, body, *, expected_hash):
        if key.key == self._flaky_key:
            return ERROR(ErrorKind.TIMEOUT_AFTER_COMMIT)
        return super().write(key, body, expected_hash=expected_hash)


def test_timeout_after_commit_that_never_landed_aborts():
    source = FakeBackend()
    for k in ("a", "b", "c"):
        _put(source, k, f"v-{k}".encode())

    target = _TimeoutAfterCommitNeverLandedBackend(flaky_key="b")
    report = migrate(source, target)

    assert report.status == "aborted"
    assert report.aborted_key == "b"
    assert target.read("b") is None
    assert target.read("a") is not None  # "a" sorts before "b", already copied


# ---------------------------------------------------------------------------
# Read-back hash-mismatch -> abort
# ---------------------------------------------------------------------------


class _CorruptsReadOnceBackend(FakeBackend):
    """A FakeBackend whose read() returns corrupted bytes for one specific
    key, exactly once, simulating a target that accepted a write but whose
    read path (or underlying storage) returns something other than what was
    written -- the scenario migrate()'s read-back verify step exists to
    catch."""

    def __init__(self, corrupt_key: str):
        super().__init__()
        self._corrupt_key = corrupt_key
        self._armed = True

    def read(self, key: str):
        blob = super().read(key)
        if blob is not None and key == self._corrupt_key and self._armed:
            self._armed = False
            return Blob(body=b"CORRUPTED-BYTES", version_hash="0" * 64)
        return blob


def test_read_back_hash_mismatch_aborts():
    source = FakeBackend()
    _put(source, "a", b"content-a")
    _put(source, "b", b"content-b")
    _put(source, "c", b"content-c")

    target = _CorruptsReadOnceBackend(corrupt_key="b")
    report = migrate(source, target)

    assert report.status == "aborted"
    assert report.aborted_key == "b"
    assert "mismatch" in report.reason.lower()
    # "a" (sorts first) was copied and verified fine before the corrupt read
    # for "b". "b" itself DID get a successful gate.persist() (the write
    # genuinely landed on target -- keys_copied counts write attempts that
    # returned OK) but failed its OWN read-back verify, so keys_verified
    # stops one short of keys_copied.
    assert report.keys_verified == 1
    assert report.keys_copied == 2


# ---------------------------------------------------------------------------
# Interrupted mid-copy, then re-run completes idempotently
# ---------------------------------------------------------------------------


class _FlakyOnceBackend(FakeBackend):
    """Raises ConnectionError (a pre-send-style, definitely-not-committed
    failure -- see FakeBackend.inject_network_failure's own docstring) the
    FIRST time write() is called for `fail_key`, then behaves normally for
    every call after that (including a retry of the same key) -- simulating
    one transient network blip partway through a migration run."""

    def __init__(self, fail_key: str):
        super().__init__()
        self._fail_key = fail_key
        self._armed = True

    def write(self, key, body, *, expected_hash):
        if self._armed and key.key == self._fail_key:
            self._armed = False
            raise ConnectionError("simulated transient network failure mid-migration")
        return super().write(key, body, expected_hash=expected_hash)


def test_interrupted_cutover_then_rerun_completes_idempotently():
    source = FakeBackend()
    for k in ("k0", "k1", "k2", "k3", "k4"):
        _put(source, k, f"value-{k}".encode())

    target = _FlakyOnceBackend(fail_key="k2")

    report1 = migrate(source, target)
    assert report1.status == "aborted"
    assert report1.aborted_key == "k2"
    # k0, k1 sort before k2 and must already be copied+verified.
    assert report1.keys_copied == 2
    assert report1.keys_verified == 2
    assert target.read("k0") is not None
    assert target.read("k1") is not None
    assert target.read("k2") is None
    assert target.read("k3") is None
    assert target.read("k4") is None

    # Re-run against the SAME (partially-populated) target, same source, no
    # further fault injected (the flaky backend only fails once).
    report2 = migrate(source, target)
    assert report2.status == "success", report2.reason
    assert report2.keys_already_present == 2  # k0, k1
    assert report2.keys_copied == 3  # k2, k3, k4
    assert report2.keys_verified == 5

    for k in ("k0", "k1", "k2", "k3", "k4"):
        blob = target.read(k)
        assert blob is not None
        assert blob.body == f"value-{k}".encode()


# ---------------------------------------------------------------------------
# Active-writer guard: source.write() raises while frozen
# ---------------------------------------------------------------------------


def test_frozen_backend_write_raises():
    raw = FakeBackend()
    _put(raw, "x", b"y")

    frozen = FrozenBackend(raw)
    assert frozen.frozen is True

    with pytest.raises(SourceFrozenError):
        frozen.write(None, None, expected_hash=None)

    # Reads still work normally through the wrapper.
    blob = frozen.read("x")
    assert blob is not None and blob.body == b"y"
    assert list(frozen.list("")) == ["x"]

    # The underlying backend itself is untouched by the failed write attempt.
    assert raw.read("x").body == b"y"


def test_migrate_never_writes_to_source():
    """End-to-end confirmation that a normal migrate() run never mutates
    source: the source's own internal dict identity/contents are unchanged
    (same keys, same bytes) after a successful migration."""
    source = FakeBackend()
    _put(source, "a", b"v1")
    _put(source, "b", b"v2")
    before = dict(source._store)

    target = FakeBackend()
    report = migrate(source, target)
    assert report.status == "success", report.reason

    after = dict(source._store)
    assert before == after


# ---------------------------------------------------------------------------
# MigrationReport shape sanity
# ---------------------------------------------------------------------------


def test_report_is_a_migration_report_instance():
    source = FakeBackend()
    target = FakeBackend()
    report = migrate(source, target)
    assert isinstance(report, MigrationReport)
    assert report.status == "success"
    assert report.source_key_count == 0
    assert report.unexpected_target_keys == ()
    assert report.finished_at >= report.started_at
