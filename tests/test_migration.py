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

import json
import time

import pytest

from migrate.migrate import (
    FrozenBackend,
    MigrationReport,
    SourceFrozenError,
    migrate,
    _MIGRATION_HEARTBEAT_INTERVAL_S,
    _MIGRATION_LEASE_KEY,
    _MIGRATION_LEASE_TTL_S,
)
from store import gate
from store.fake import FakeBackend
from store.local import LocalBackend
from store.objectstore import ObjectStoreBackend
from store.types import Blob, ERROR, ErrorKind, OK, sha256_hex
from tests.ctx_helpers import create_ctx
from tests.moto_support import (
    DUMMY_ACCESS_KEY_ID,
    DUMMY_REGION,
    DUMMY_SECRET_ACCESS_KEY,
    get_moto_endpoint,
    make_bucket,
)


def _put(backend, key: str, body: bytes):
    r = gate.persist(backend, key, body, ctx=create_ctx(), doc_type="system_state")
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
    assert [k for k in target.list("") if k != _MIGRATION_LEASE_KEY] == []


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
    assert [k for k in target.list("") if k != _MIGRATION_LEASE_KEY] == []

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
    assert [k for k in target2.list("") if k != _MIGRATION_LEASE_KEY] == []


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
    assert [k for k in target.list("") if k != _MIGRATION_LEASE_KEY] == []


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

    def write(self, key, body, *, ctx):
        result = super().write(key, body, ctx=ctx)
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

    def write(self, key, body, *, ctx):
        if key.key == self._flaky_key:
            return ERROR(ErrorKind.TIMEOUT_AFTER_COMMIT)
        return super().write(key, body, ctx=ctx)


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

    def write(self, key, body, *, ctx):
        if self._armed and key.key == self._fail_key:
            self._armed = False
            raise ConnectionError("simulated transient network failure mid-migration")
        return super().write(key, body, ctx=ctx)


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
        frozen.write(None, None, ctx=create_ctx())

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


# ---------------------------------------------------------------------------
# H1 Increment 2 / D-008: session-scoped migration lease (mutex + takeover).
# ---------------------------------------------------------------------------


def test_concurrent_migration_refused_when_lease_held(tmp_path):
    """A second migration must NOT run while a live lease is held on the target
    (the session-scoped mutex). We simulate a live holder by taking the control
    lease directly, then migrate() must abort cleanly copying nothing."""
    source = FakeBackend()
    _put(source, "proj/system_state", b"the one source note")
    target = FakeBackend()

    held = target.lock(_MIGRATION_LEASE_KEY, 120.0)  # a live migration holds it
    try:
        report = migrate(source, target)
    finally:
        target.unlock(held)

    assert report.status == "aborted"
    assert "lease unavailable" in (report.reason or "")
    assert report.keys_copied == 0 and report.keys_already_present == 0
    # The blocked migration copied no DATA (the live holder's lease doc may exist).
    assert [k for k in target.list("") if k != _MIGRATION_LEASE_KEY] == []


def test_migration_rerun_after_clean_release_records_prior_owner_not_a_takeover(tmp_path):
    """After a migration finishes it marks its control doc RELEASED. A later
    run re-acquires the lease as a clean handoff: it records the previous
    holder in `prior_owner` (audit) but NOT in `took_over_from`, which is
    reserved for a stale-owner takeover of a crashed run (#18 item 8). Both
    runs succeed; the re-run is idempotent (create-only replay skips
    already-copied keys)."""
    source = FakeBackend()
    _put(source, "proj/system_state", b"the one source note")

    target = FakeBackend()
    r1 = migrate(source, target)
    assert r1.status == "success" and r1.keys_copied == 1

    doc1 = json.loads(target.read(_MIGRATION_LEASE_KEY).body.decode("utf-8"))
    assert doc1["kind"] == "knokeep-migration-lease"
    assert doc1["status"] == "released" and doc1["released_at"] is not None
    assert doc1["took_over_from"] is None and doc1["prior_owner"] is None
    owner1 = doc1["owner_id"]

    r2 = migrate(source, target)          # re-run: clean handoff from a released lease
    assert r2.status == "success"
    assert r2.keys_copied == 0 and r2.keys_already_present == 1  # idempotent replay

    doc2 = json.loads(target.read(_MIGRATION_LEASE_KEY).body.decode("utf-8"))
    assert doc2["owner_id"] != owner1
    assert doc2["prior_owner"] == owner1       # audit trail of the previous holder
    assert doc2["took_over_from"] is None      # a clean release is NOT a takeover
    assert doc2["status"] == "released"


def _plant_control_doc(target, *, owner_id, status, expires_at, **extra):
    doc = {"kind": "knokeep-migration-lease", "owner_id": owner_id, "status": status,
           "expires_at": expires_at, "write_id": "w", "started_at": 0.0,
           "heartbeat_at": 0.0, "prior_owner": None, "took_over_from": None,
           "released_at": None}
    doc.update(extra)
    body = json.dumps(doc, sort_keys=True).encode("utf-8")
    blob = target.read(_MIGRATION_LEASE_KEY)
    if blob is None:
        _put(target, _MIGRATION_LEASE_KEY, body)
    else:
        lease = target.lock(_MIGRATION_LEASE_KEY, 30.0)
        target.unlock(lease)
        from tests.ctx_helpers import overwrite_ctx
        r = gate.persist(target, _MIGRATION_LEASE_KEY, body,
                         ctx=overwrite_ctx(blob.version_hash, lease), doc_type="system_state")
        assert isinstance(r, OK), r
    return body


def test_stale_owner_takeover_of_crashed_migration_is_audited(tmp_path):
    """A control doc still marked "held" whose expires_at has passed is a
    crashed migration: the next run takes the lease over and records that
    owner in `took_over_from`."""
    source = FakeBackend()
    _put(source, "docs/a", b"alpha")
    target = FakeBackend()
    _plant_control_doc(target, owner_id="crashed-owner", status="held",
                       expires_at=time.time() - 1.0)

    report = migrate(source, target)
    assert report.status == "success", report.reason
    doc = json.loads(target.read(_MIGRATION_LEASE_KEY).body.decode("utf-8"))
    assert doc["took_over_from"] == "crashed-owner"
    assert doc["prior_owner"] == "crashed-owner"
    assert doc["status"] == "released"
    assert target.read("docs/a").body == b"alpha"


def test_live_durable_lease_refuses_second_migration_without_touching_target(tmp_path):
    """The DURABLE control doc is the primary mutex: a "held" doc with a
    future expires_at refuses a second migration even though nobody holds the
    in-memory advisory lock (the cross-process case). The refusing run must
    not write anything -- not even advance the holder's fence via lock()."""
    source = FakeBackend()
    _put(source, "docs/a", b"alpha")
    target = FakeBackend()
    body = _plant_control_doc(target, owner_id="live-owner", status="held",
                              expires_at=time.time() + 300.0)
    lock_calls = []
    real_lock = target.lock

    def _spy_lock(key, ttl_s):
        lock_calls.append(key)
        return real_lock(key, ttl_s)

    target.lock = _spy_lock
    report = migrate(source, target)
    assert report.status == "aborted"
    assert "live lease" in report.reason and "live-owner" in report.reason
    assert lock_calls == [], "a refused run must not advance the live holder's fence"
    assert target.read(_MIGRATION_LEASE_KEY).body == body  # untouched
    assert target.read("docs/a") is None


def test_control_key_holding_real_data_on_target_refuses_migration(tmp_path):
    source = FakeBackend()
    _put(source, "docs/a", b"alpha")
    target = FakeBackend()
    _put(target, _MIGRATION_LEASE_KEY, b"this is somebody's real document")

    report = migrate(source, target)
    assert report.status == "aborted"
    assert "holds real data" in report.reason
    assert target.read(_MIGRATION_LEASE_KEY).body == b"this is somebody's real document"
    assert target.read("docs/a") is None


def test_control_key_holding_real_data_on_source_aborts_instead_of_silently_omitting(tmp_path):
    source = FakeBackend()
    _put(source, "docs/a", b"alpha")
    _put(source, _MIGRATION_LEASE_KEY, b"a real value that happens to live at the control key")
    target = FakeBackend()

    report = migrate(source, target)
    assert report.status == "aborted"
    assert "collides with the migration control key" in report.reason
    assert report.aborted_key == _MIGRATION_LEASE_KEY
    assert report.keys_copied == 0
    assert target.read("docs/a") is None  # nothing was uploaded


def test_two_backend_instances_same_objectstore_cannot_both_hold_the_migration_lease():
    """Two PROCESSES (simulated by two ObjectStoreBackend instances on the
    same bucket: separate in-memory advisory registries) against one target:
    the durable control doc keeps the second migration out while the first
    holds the lease, and lets it in after a clean release."""
    from migrate.migrate import _acquire_migration_lease

    endpoint, bucket = get_moto_endpoint(), make_bucket()

    def _instance():
        return ObjectStoreBackend(
            endpoint=endpoint, bucket=bucket, region=DUMMY_REGION,
            access_key_id=DUMMY_ACCESS_KEY_ID, secret_access_key=DUMMY_SECRET_ACCESS_KEY,
        )

    target_a, target_b = _instance(), _instance()
    source = FakeBackend()
    _put(source, "docs/a", b"alpha")

    lease_a, err = _acquire_migration_lease(target_a, "proc-a", "w-a", time.time())
    assert err is None and lease_a is not None
    try:
        # Process B: its advisory registry is empty, so ONLY the durable doc
        # can keep it out.
        report_b = migrate(source, target_b)
        assert report_b.status == "aborted", report_b
        assert "live lease" in report_b.reason and "proc-a" in report_b.reason
        assert target_b.read("docs/a") is None
        # A's own lease keeps heartbeating fine meanwhile.
        assert lease_a.heartbeat_if_due() is None
    finally:
        lease_a.release()

    report_b2 = migrate(source, target_b)
    assert report_b2.status == "success", report_b2.reason
    assert target_a.read("docs/a").body == b"alpha"
    doc = json.loads(target_a.read(_MIGRATION_LEASE_KEY).body.decode("utf-8"))
    assert doc["prior_owner"] == "proc-a" and doc["took_over_from"] is None


def test_two_backend_instances_same_git_remote_cannot_both_hold_the_migration_lease(tmp_path):
    import subprocess

    from migrate.migrate import _acquire_migration_lease
    from store.git_backend import GitBackend

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(remote)],
                   check=True, capture_output=True)
    target_a = GitBackend(tmp_path / "work-a", remote)
    target_b = GitBackend(tmp_path / "work-b", remote)
    source = FakeBackend()
    _put(source, "docs/a", b"alpha")

    lease_a, err = _acquire_migration_lease(target_a, "proc-a", "w-a", time.time())
    assert err is None and lease_a is not None
    try:
        report_b = migrate(source, target_b)
        assert report_b.status == "aborted", report_b
        assert "live lease" in report_b.reason
        assert target_b.read("docs/a") is None
    finally:
        lease_a.release()

    report_b2 = migrate(source, target_b)
    assert report_b2.status == "success", report_b2.reason
    assert target_a.read("docs/a").body == b"alpha"
    # Fence sidecars created by the lease never leak into the logical keyspace.
    assert sorted(target_a.list("")) == ["docs/a", _MIGRATION_LEASE_KEY]


# ---------------------------------------------------------------------------
# H1 Increment 2 / D-008: heartbeat by ELAPSED TIME (both passes) + control-key
# exclusion.
# ---------------------------------------------------------------------------


class _FakeClock:
    """Drives migrate.migrate._monotonic deterministically. Each DATA-key
    write on the target advances it by `per_write_s` (a slow copy); control
    doc writes do not."""

    def __init__(self, per_write_s: float):
        self.now = 1000.0
        self.per_write_s = per_write_s

    def __call__(self) -> float:
        return self.now


class _SlowCopyTarget(FakeBackend):
    """A target whose renew() succeeds for the first `ok_renewals` heartbeat
    calls and then returns False forever -- simulating the migration lease
    being taken over mid-copy (#18 item 4) -- and whose data-key writes
    advance the injected clock. Records every renew call so the test can
    assert the heartbeat cadence and the lock/TTL it was fed."""

    def __init__(self, clock: _FakeClock, ok_renewals: int):
        super().__init__()
        self._clock = clock
        self._ok_renewals = ok_renewals
        self.renew_calls = []

    def renew(self, lock, ttl_s):
        self.renew_calls.append((lock, ttl_s))
        if len(self.renew_calls) > self._ok_renewals:
            return False
        return super().renew(lock, ttl_s)

    def write(self, key, body, *, ctx):
        res = super().write(key, body, ctx=ctx)
        if key.key != _MIGRATION_LEASE_KEY:
            self._clock.now += self._clock.per_write_s
        return res


def test_heartbeat_renew_failure_mid_copy_aborts_and_writes_no_further_keys(monkeypatch):
    """With each key copy taking 10s and the heartbeat interval at TTL/3 =
    40s, the lease is renewed before the 5th write (elapsed 40s) and again
    before the 9th (elapsed 80s). When that second renewal fails (ownership
    lost), migrate() must abort BEFORE writing that key: target holds exactly
    the keys copied before the failed heartbeat, none after."""
    import migrate.migrate as migrate_module

    per_write = _MIGRATION_HEARTBEAT_INTERVAL_S / 4.0        # 10s per key at the default TTL
    clock = _FakeClock(per_write)
    monkeypatch.setattr(migrate_module, "_monotonic", clock)

    n_keys = 12
    keys = [f"k/{i:03d}" for i in range(n_keys)]              # zero-padded -> sorted == insertion
    source = FakeBackend()
    for k in keys:
        _put(source, k, f"value-{k}".encode())

    target = _SlowCopyTarget(clock, ok_renewals=1)            # 1st heartbeat ok, 2nd fails
    report = migrate(source, target)

    boundary = 8                                              # index of the key at the failed heartbeat
    assert report.status == "aborted"
    assert report.ok is False
    assert "lease" in report.reason.lower() and "renew" in report.reason.lower()
    assert report.aborted_key == keys[boundary]
    assert report.cutover_signaled is False
    assert report.source_key_count == n_keys
    assert report.keys_scanned == n_keys                      # PASS 1 completed before any write
    assert report.keys_copied == boundary
    assert report.keys_verified == boundary
    assert report.keys_already_present == 0

    # Exactly the keys before the failed heartbeat landed; nothing at/after it.
    landed = sorted(k for k in target.list("") if k != _MIGRATION_LEASE_KEY)
    assert landed == keys[:boundary]
    for k in keys[boundary:]:
        assert target.read(k) is None
    for k in keys[:boundary]:
        assert target.read(k).body == f"value-{k}".encode()

    # Heartbeat cadence: one renew per elapsed interval (before keys 4 and 8),
    # each on the migration control lease with the lease TTL.
    assert len(target.renew_calls) == 2
    for lock, ttl in target.renew_calls:
        assert lock.key == _MIGRATION_LEASE_KEY
        assert ttl == _MIGRATION_LEASE_TTL_S

    # Source untouched (frozen).
    for k in keys:
        assert source.read(k).body == f"value-{k}".encode()


def test_no_heartbeat_when_no_time_elapses_and_one_renew_per_interval(monkeypatch):
    """Heartbeats are driven by elapsed time, not key count: a fast copy of
    many keys issues NO renew; a slow copy renews once per interval, and a
    single successful renew lets the run complete."""
    import migrate.migrate as migrate_module

    clock = _FakeClock(per_write_s=0.0)                       # time never moves
    monkeypatch.setattr(migrate_module, "_monotonic", clock)
    source = FakeBackend()
    for i in range(60):
        _put(source, f"k/{i:03d}", b"v")
    target = _SlowCopyTarget(clock, ok_renewals=0)            # ANY renew would fail
    report = migrate(source, target)
    assert report.status == "success", report.reason
    assert target.renew_calls == []

    clock2 = _FakeClock(per_write_s=_MIGRATION_HEARTBEAT_INTERVAL_S / 4.0)
    monkeypatch.setattr(migrate_module, "_monotonic", clock2)
    source2 = FakeBackend()
    for i in range(5):                                        # 4 writes elapse one interval -> 1 heartbeat before key 4
        _put(source2, f"k/{i:03d}", b"v")
    target2 = _SlowCopyTarget(clock2, ok_renewals=1)
    report2 = migrate(source2, target2)
    assert report2.status == "success", report2.reason
    assert len(target2.renew_calls) == 1
    assert report2.keys_copied == 5
    # The heartbeat also pushed the durable control doc's expiry forward.
    doc = json.loads(target2.read(_MIGRATION_LEASE_KEY).body.decode("utf-8"))
    assert doc["heartbeat_at"] >= doc["started_at"]


def test_heartbeat_during_pre_scan_aborts_before_any_write_when_lease_lost(monkeypatch):
    """A slow SOURCE (pre-scan reads take long) must heartbeat too; losing
    the lease there aborts before anything is written to the target."""
    import migrate.migrate as migrate_module

    clock = _FakeClock(per_write_s=0.0)
    monkeypatch.setattr(migrate_module, "_monotonic", clock)

    class _SlowSource(FakeBackend):
        def read(self, key):
            clock.now += _MIGRATION_HEARTBEAT_INTERVAL_S      # every source read takes a full interval
            return super().read(key)

    source = _SlowSource()
    for i in range(3):
        _put(source, f"k/{i}", b"v")
    target = _SlowCopyTarget(clock, ok_renewals=0)
    report = migrate(source, target)
    assert report.status == "aborted"
    assert "pre-scan" in report.reason
    assert report.keys_copied == 0
    assert [k for k in target.list("") if k != _MIGRATION_LEASE_KEY] == []


def test_control_key_on_source_and_target_is_excluded_from_manifest_and_unexpected():
    """The reserved migration control key is never migratable data (#18 item 2)
    WHEN what sits there is a migration control doc: on the SOURCE it is
    dropped from the manifest (not scanned, not copied, not counted), and when
    a (crashed, expired) one pre-exists on the TARGET it is not reported as an
    unexpected/foreign key. The target's control doc after the run is THIS
    run's lease doc, not a copy of the source's."""
    source = FakeBackend()
    _put(source, "docs/a", b"alpha")
    _put(source, "docs/b", b"beta")
    stale_source_doc = json.dumps({"kind": "knokeep-migration-lease",
                                  "owner_id": "stale-source-owner",
                                  "status": "held"}, sort_keys=True).encode("utf-8")
    _put(source, _MIGRATION_LEASE_KEY, stale_source_doc)

    target = FakeBackend()
    prior_target_doc = json.dumps({"kind": "knokeep-migration-lease",
                                  "owner_id": "prior-target-owner",
                                  "status": "held"}, sort_keys=True).encode("utf-8")  # no expires_at: lapsed
    _put(target, _MIGRATION_LEASE_KEY, prior_target_doc)

    report = migrate(source, target)

    assert report.status == "success", report.reason
    assert report.source_key_count == 2                   # control key not in manifest
    assert report.keys_scanned == 2
    assert report.keys_copied == 2
    assert report.keys_already_present == 0
    assert report.keys_verified == 2
    assert report.unexpected_target_keys == ()            # pre-existing control doc not "unexpected"
    assert report.aborted_key != _MIGRATION_LEASE_KEY

    # Data landed; the control key on target is this run's own lease doc --
    # not the source's stale doc, and not the prior target doc either.
    assert target.read("docs/a").body == b"alpha"
    assert target.read("docs/b").body == b"beta"
    ctrl = json.loads(target.read(_MIGRATION_LEASE_KEY).body.decode("utf-8"))
    assert ctrl["owner_id"] not in ("stale-source-owner", "prior-target-owner")
    assert ctrl["took_over_from"] == "prior-target-owner"  # the target's prior holder had lapsed while "held"
    assert target.read(_MIGRATION_LEASE_KEY).body != stale_source_doc

    # Source's own control doc is untouched (frozen).
    assert source.read(_MIGRATION_LEASE_KEY).body == stale_source_doc


# ---------------------------------------------------------------------------
# Backend-internal fence artifacts are never migrated: git sidecars and
# object-store phantoms are invisible to list(), so a migration from either
# source copies only real keys.
# ---------------------------------------------------------------------------


def test_migration_from_git_source_ignores_fence_sidecars(tmp_path):
    import subprocess

    from store.git_backend import GitBackend
    from tests.ctx_helpers import fenced_ctx

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(remote)],
                   check=True, capture_output=True)
    source = GitBackend(tmp_path / "work", remote)
    r0 = _put(source, "docs/a", b"a")
    r1 = gate.persist(source, "docs/a", b"a2", ctx=fenced_ctx(source, "docs/a", r0.new_hash),
                      doc_type="system_state")
    assert isinstance(r1, OK)
    source.lock("docs/locked-only", ttl_s=30)                 # sidecar, no data
    _put(source, "docs/b", b"b")

    target = FakeBackend()
    report = migrate(source, target)
    assert report.status == "success", report.reason
    assert report.source_key_count == 2 and report.keys_copied == 2
    assert sorted(k for k in target.list("") if k != _MIGRATION_LEASE_KEY) == ["docs/a", "docs/b"]
    assert not any(k.startswith(".knokeep-fence/") for k in target.list(""))
    assert target.read("docs/a").body == b"a2"


def test_migration_from_objectstore_source_ignores_phantoms():
    source = _make_objectstore()
    _put(source, "docs/a", b"a")
    source.lock("docs/phantom", ttl_s=30)                     # lock()'d, never written
    target = FakeBackend()
    report = migrate(source, target)
    assert report.status == "success", report.reason
    assert report.source_key_count == 1 and report.keys_copied == 1
    assert sorted(k for k in target.list("") if k != _MIGRATION_LEASE_KEY) == ["docs/a"]
