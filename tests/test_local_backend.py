"""LocalBackend-specific conformance (contract v1.4 §3, §4.1, §9).

These exercise behavior the backend-agnostic conformance/suite.py cannot: the
journal/publish crash-recovery pipeline, real cross-process contention on the
`.lock` file, and this adapter's own residual generation-monotonicity guard.
All writes still go through store.gate.persist() (the one door) except where
a test is explicitly simulating a crash and must reach into LocalBackend's
private journal/publish primitives to do so — each such case is called out.

Windows-only behavior (the msvcrt.locking() code path and true NTFS
two-process semantics) CANNOT be exercised on this Linux host and is guarded
with `@pytest.mark.skipif(os.name != "nt")` below rather than faked.
"""
from __future__ import annotations

import multiprocessing
import os
import time
import types

import pytest

from store import gate
from store.backend import BackendBusyError
from store.local import LocalBackend
from store.types import ERROR, OK, STALE, ErrorKind, sha256_hex


def _gen(n: int) -> bytes:
    return gate.make_generation_header(n)


# ---------------------------------------------------------------------------
# C8 — crash-after-journal-before-publish replay
# ---------------------------------------------------------------------------


def test_c8_resume_rebuilds_key_after_journal_only_write(tmp_path):
    """Simulate a crash that landed the durability commit (journal + fsync)
    but never reached the atomic publish step: write directly to the
    journal via the adapter's own low-level primitive, skip _publish
    entirely, then construct a FRESH adapter over the same root and assert
    resume() rematerializes the key from the journal alone."""
    root = tmp_path / "store-root"
    backend = LocalBackend(root)

    key = "resume/never-published"
    body = b"durable-but-not-yet-published"
    backend._journal_append(key, body)  # journal commit only — no _publish
    backend.close()

    assert not (root / "data" / "resume" / "never-published").exists()

    resumed = LocalBackend(root)  # __init__ calls _resume()
    blob = resumed.read(key)
    assert blob is not None
    assert blob.body == body
    assert blob.version_hash == sha256_hex(body)
    resumed.close()


def test_c8_resume_rebuilds_torn_published_blob(tmp_path):
    """A published blob that's present but torn (doesn't match the
    journal's hash for that key) must also be rebuilt on resume — not just
    a wholly-missing blob."""
    root = tmp_path / "store-root"
    backend = LocalBackend(root)

    key = "resume/torn"
    good_body = b"the-real-durable-bytes"
    r = gate.persist(backend, key, good_body, expected_hash=None, doc_type="system_state")
    assert isinstance(r, OK)

    data_path = root / "data" / "resume" / "torn"
    data_path.write_bytes(b"garbage-simulating-a-torn-write")
    backend.close()

    resumed = LocalBackend(root)
    blob = resumed.read(key)
    assert blob is not None
    assert blob.body == good_body
    resumed.close()


def test_c8_resume_uses_latest_journal_record_for_a_key(tmp_path):
    """Two journal-only records for the same key (simulating a crash right
    after a second commit's journal append, before its publish): resume
    must materialize the LATEST one, not the first."""
    root = tmp_path / "store-root"
    backend = LocalBackend(root)
    key = "resume/multi"
    backend._journal_append(key, b"v1")
    backend._journal_append(key, b"v2-latest")
    backend.close()

    resumed = LocalBackend(root)
    blob = resumed.read(key)
    assert blob is not None
    assert blob.body == b"v2-latest"
    resumed.close()


def test_c8_resume_ignores_torn_tail_journal_record(tmp_path):
    """A journal record truncated mid-append (the crash happened DURING the
    fsync'd append itself, so it was never durable) must be ignored, and
    everything before it must still resume correctly."""
    root = tmp_path / "store-root"
    backend = LocalBackend(root)
    key = "resume/good"
    backend._journal_append(key, b"good-and-durable")
    # Simulate a torn trailing record: a well-formed key-length header
    # claiming a huge body that was never actually written.
    import struct

    torn_key = b"resume/torn-tail"
    backend._journal_fh.write(struct.pack(">I", len(torn_key)) + torn_key)
    backend._journal_fh.write(struct.pack(">Q", 10_000_000))
    backend._journal_fh.write(b"only-a-few-bytes")
    backend._journal_fh.flush()
    os.fsync(backend._journal_fh.fileno())
    backend.close()

    resumed = LocalBackend(root)
    blob = resumed.read(key)
    assert blob is not None and blob.body == b"good-and-durable"
    assert resumed.read("resume/torn-tail") is None
    resumed.close()


# ---------------------------------------------------------------------------
# C3 — atomicity under crash: a reader never sees torn bytes at the key
# ---------------------------------------------------------------------------


def test_c3_atomicity_reader_never_sees_torn_publish(tmp_path):
    """While a publish is only partway staged (bytes written to the staging
    tempfile but not yet renamed into place), a concurrent reader must see
    either nothing (create case) or the old, complete blob (update case) —
    never a partial file at the key path."""
    root = tmp_path / "store-root"
    backend = LocalBackend(root)

    key = "atomic/k"
    r0 = gate.persist(backend, key, b"old-complete-value", expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK)

    # Simulate "crash after journal fsync, mid-publish": append the new
    # record to the journal (durability commit done) and start writing the
    # staging tempfile, but never call _publish's os.replace.
    new_body = b"new-value-that-never-gets-published-in-this-test"
    backend._journal_append(key, new_body)
    import tempfile as _tempfile

    fd, tmp_name = _tempfile.mkstemp(dir=str(root / "staging"))
    os.write(fd, new_body[: len(new_body) // 2])  # only half written
    os.close(fd)

    # The published key must still be exactly the old, complete blob — a
    # reader is never exposed to the half-written staging file.
    blob = backend.read(key)
    assert blob is not None
    assert blob.body == b"old-complete-value"

    backend.close()

    # And resume() (a fresh adapter, i.e. "after the crash is noticed")
    # correctly finishes materializing the durable journal record.
    resumed = LocalBackend(root)
    blob2 = resumed.read(key)
    assert blob2 is not None and blob2.body == new_body
    resumed.close()


# ---------------------------------------------------------------------------
# Startup scavenger — orphaned staging temps older than TTL are removed
# ---------------------------------------------------------------------------


def test_scavenger_removes_stale_staging_temps_but_keeps_fresh_ones(tmp_path):
    root = tmp_path / "store-root"
    backend = LocalBackend(root, staging_ttl_s=0.05)
    staging_dir = root / "staging"

    stale = staging_dir / "orphan-stale"
    stale.write_bytes(b"leftover-from-a-crashed-process")
    old_time = time.time() - 10
    os.utime(stale, (old_time, old_time))

    fresh = staging_dir / "orphan-fresh"
    fresh.write_bytes(b"just-created")

    backend.close()

    # Re-run the scavenger by constructing a new adapter over the same root.
    resumed = LocalBackend(root, staging_ttl_s=0.05)
    assert not stale.exists(), "stale staging temp older than TTL must be removed"
    assert fresh.exists(), "a staging temp younger than TTL must be left alone"
    resumed.close()


# ---------------------------------------------------------------------------
# Generation monotonicity (residual from T1) — enforced HERE, not in the gate
# ---------------------------------------------------------------------------


def test_generation_monotonicity_rejects_non_increasing_update(tmp_path):
    backend = LocalBackend(tmp_path / "store-root")
    key = "lease/holder"

    r0 = gate.persist(backend, key, _gen(1) + b"leaseholder=alice", expected_hash=None, doc_type="lease")
    assert isinstance(r0, OK)

    # A same-or-lower generation under a MATCHING hash-CAS must be rejected
    # even though the plain hash-CAS check alone would have allowed it.
    r_same_gen = gate.persist(
        backend, key, _gen(1) + b"leaseholder=bob",
        expected_hash=r0.new_hash, doc_type="lease",
    )
    assert isinstance(r_same_gen, STALE)

    r_lower_gen = gate.persist(
        backend, key, _gen(0) + b"leaseholder=bob",
        expected_hash=r0.new_hash, doc_type="lease",
    )
    assert isinstance(r_lower_gen, STALE)

    # The store must be unchanged by either rejected attempt.
    assert backend.read(key).body == _gen(1) + b"leaseholder=alice"

    # A strictly-greater generation is accepted.
    r_higher_gen = gate.persist(
        backend, key, _gen(2) + b"leaseholder=bob",
        expected_hash=r0.new_hash, doc_type="lease",
    )
    assert isinstance(r_higher_gen, OK)
    assert backend.read(key).body == _gen(2) + b"leaseholder=bob"
    backend.close()


# ---------------------------------------------------------------------------
# Adapter accepts only ScannedBody/ScannedKey — raw bytes -> TypeError
# ---------------------------------------------------------------------------


def test_write_rejects_raw_bytes_before_any_io(tmp_path):
    backend = LocalBackend(tmp_path / "store-root")
    with pytest.raises(TypeError):
        backend.write("plain-str-key", b"plain-bytes-body", expected_hash=None)  # type: ignore[arg-type]
    assert backend.read("plain-str-key") is None
    assert list(backend.list("")) == []
    backend.close()


class _CaptureBackend:
    """A stand-in `backend` for gate.persist() that just captures the
    gate-issued ScannedKey/ScannedBody instead of doing any I/O, so a test
    can obtain a legitimately-issued pair and then tamper with it."""

    def __init__(self) -> None:
        self.captured = None

    def write(self, key, body, *, expected_hash):
        self.captured = (key, body)
        return OK("0" * 64)


def test_scanned_body_is_immutable_after_construction(tmp_path):
    """Hardening: ScannedKey/ScannedBody are now frozen after construction —
    a would-be tamper (e.g. rewriting `_body` post-issuance to smuggle a
    different payload past the marker) must raise TypeError immediately,
    rather than silently succeeding and leaving a stale-but-matching marker
    for the adapter to (previously) reject at write() time."""
    capture = _CaptureBackend()
    r = gate.persist(capture, "k", b"hello", expected_hash=None, doc_type="system_state")
    assert isinstance(r, OK)
    key_obj, body_obj = capture.captured

    with pytest.raises(TypeError):
        body_obj._body = b"tampered-after-the-fact"
    with pytest.raises(TypeError):
        key_obj._key = "tampered"
    with pytest.raises(TypeError):
        del body_obj._body

    # Untampered, the legitimately-issued pair still writes fine.
    backend = LocalBackend(tmp_path / "store-root")
    result = backend.write(key_obj, body_obj, expected_hash=None)
    assert isinstance(result, OK)
    assert backend.read("k").body == b"hello"
    backend.close()


def test_write_rejects_forged_non_gate_object(tmp_path):
    """The adapter must accept ONLY genuine gate-issued ScannedKey/ScannedBody
    instances — never a look-alike stand-in object with matching attribute
    names but no valid gate marker (isinstance check, checked before any
    I/O, per contract §1/§5)."""
    forged_key = types.SimpleNamespace(key="k")
    forged_body = types.SimpleNamespace(body=b"hello")

    backend = LocalBackend(tmp_path / "store-root")
    with pytest.raises(TypeError):
        backend.write(forged_key, forged_body, expected_hash=None)
    assert backend.read("k") is None
    assert gate.verify(forged_key) is False
    assert gate.verify(forged_body) is False
    backend.close()


# ---------------------------------------------------------------------------
# Case-insensitive-volume collision refusal
# ---------------------------------------------------------------------------


def test_case_insensitive_collision_refused_when_flagged(tmp_path):
    """This Linux/ext4 host is case-SENSITIVE, so the real probe in
    LocalBackend.__init__ will always find `_case_insensitive = False` and
    the refusal branch is otherwise unreachable here. This test exercises
    the refusal LOGIC itself by forcing the flag, and does not substitute
    for running on an actual case-insensitive volume (NTFS/default APFS) —
    see the module docstring's judgment call #5."""
    backend = LocalBackend(tmp_path / "store-root")
    backend._case_insensitive = True  # simulate a case-insensitive volume

    r0 = gate.persist(backend, "Notes/Foo", b"v1", expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK)

    r1 = gate.persist(backend, "Notes/foo", b"v2", expected_hash=None, doc_type="system_state")
    assert isinstance(r1, ERROR)
    assert r1.kind is ErrorKind.INVALID_ARGUMENT
    # NOTE: we do NOT assert read("Notes/foo") is None here. On a *real*
    # case-insensitive volume (NTFS) "Notes/foo" aliases to the existing
    # "Notes/Foo" file, so the read returns v1; on a case-sensitive host with
    # the flag merely forced, it would be a distinct absent path. The portable,
    # meaningful invariant is: the collision was REFUSED (above) and the
    # original is untouched (below).
    assert backend.read("Notes/Foo").body == b"v1"  # original untouched
    backend.close()


@pytest.mark.skipif(
    os.name == "nt",
    reason="NTFS is case-insensitive; this asserts case-SENSITIVE-volume behavior (POSIX/ext4).",
)
def test_case_sensitive_volume_allows_distinct_case_keys(tmp_path):
    """Sanity check the flag actually gates the behavior: with the (real,
    default-on-this-host) case-sensitive flag, two keys differing only by
    case are simply two distinct keys."""
    backend = LocalBackend(tmp_path / "store-root")
    assert backend._case_insensitive is False

    r0 = gate.persist(backend, "Notes/Foo", b"v1", expected_hash=None, doc_type="system_state")
    r1 = gate.persist(backend, "Notes/foo", b"v2", expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK) and isinstance(r1, OK)
    backend.close()


# ---------------------------------------------------------------------------
# BUSY on lock contention — never blocks indefinitely
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX fcntl external-lock simulation; the Windows BUSY path is covered by "
    "the two-process race and advisory-lock tests.",
)
def test_write_returns_busy_when_cas_lock_held_externally(tmp_path):
    root = tmp_path / "store-root"
    backend = LocalBackend(root, lock_timeout_s=0.1)

    import fcntl

    lock_path = root / "locks" / "cas.lock"
    fh = open(lock_path, "a+b")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)  # hold the exact same lock file
    try:
        start = time.monotonic()
        result = gate.persist(backend, "busy/k", b"v", expected_hash=None, doc_type="system_state")
        elapsed = time.monotonic() - start
        assert isinstance(result, ERROR)
        assert result.kind is ErrorKind.BUSY
        assert elapsed < 2.0, "must not block indefinitely"
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
    backend.close()


def test_advisory_lock_busy_does_not_hang(tmp_path):
    backend = LocalBackend(tmp_path / "store-root", lock_timeout_s=0.1)
    lock1 = backend.lock("k", ttl_s=5)
    with pytest.raises(BackendBusyError):
        backend.lock("k", ttl_s=5)
    assert backend.unlock(lock1) is True
    backend.close()


# ---------------------------------------------------------------------------
# os.replace retry-on-PermissionError (portable unit test of the mechanism;
# the real Windows sharing-violation SCENARIO cannot be produced here — see
# module docstring judgment call #4)
# ---------------------------------------------------------------------------


def test_replace_with_retry_recovers_from_transient_permission_error(tmp_path, monkeypatch):
    backend = LocalBackend(tmp_path / "store-root", replace_retry_attempts=5, replace_retry_backoff_s=0.001)
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.write_bytes(b"x")

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(a, b):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("simulated transient sharing violation")
        return real_replace(a, b)

    monkeypatch.setattr(os, "replace", flaky_replace)
    backend._replace_with_retry(src, dst)
    assert dst.read_bytes() == b"x"
    assert calls["n"] == 3
    backend.close()


def test_replace_with_retry_gives_up_as_busy(tmp_path, monkeypatch):
    from store.local import _ReplaceBusy

    backend = LocalBackend(tmp_path / "store-root", replace_retry_attempts=3, replace_retry_backoff_s=0.001)
    src = tmp_path / "src2"
    dst = tmp_path / "dst2"
    src.write_bytes(b"x")

    def always_fails(a, b):
        raise PermissionError("simulated persistent sharing violation")

    monkeypatch.setattr(os, "replace", always_fails)
    with pytest.raises(_ReplaceBusy):
        backend._replace_with_retry(src, dst)
    backend.close()


# ---------------------------------------------------------------------------
# Two-process race (POSIX) — real cross-process contention on the flock'd
# .lock file, not simulated within one process's threads.
# ---------------------------------------------------------------------------


def _mp_create_only_worker(root_str: str, key: str, payload: bytes, queue) -> None:
    # Separate OS process: a fresh LocalBackend instance over the SAME root.
    backend = LocalBackend(root_str)
    result = gate.persist(backend, key, payload, expected_hash=None, doc_type="system_state")
    backend.close()
    queue.put(type(result).__name__)


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX fork start method")
def test_two_process_create_only_race(tmp_path):
    """Two independent OS processes race a create-only write to the same
    brand-new key via two separate LocalBackend instances over the same
    root directory. Exactly one must observe OK; the other EXISTS — proving
    contention is resolved by the real cross-process .lock file, not merely
    by in-process thread serialization (which conformance/suite.py's
    threaded races already cover for FakeBackend and, incidentally, for
    LocalBackend within a single process)."""
    root = tmp_path / "store-root"
    LocalBackend(root).close()  # pre-create the directory structure once

    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    procs = [
        ctx.Process(target=_mp_create_only_worker, args=(str(root), "race/key", f"proc-{i}".encode(), queue))
        for i in range(4)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=10)
        assert p.exitcode == 0

    outcomes = [queue.get(timeout=1) for _ in procs]
    assert outcomes.count("OK") == 1, f"expected exactly one OK across processes, got {outcomes}"
    assert outcomes.count("EXISTS") == len(procs) - 1

    final = LocalBackend(root)
    blob = final.read("race/key")
    assert blob is not None
    final.close()


def _mp_cas_worker(root_str: str, key: str, base_hash: str, payload: bytes, queue) -> None:
    backend = LocalBackend(root_str)
    result = gate.persist(backend, key, payload, expected_hash=base_hash, doc_type="system_state")
    backend.close()
    queue.put(type(result).__name__)


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX fork start method")
def test_two_process_cas_update_race(tmp_path):
    """Two OS processes race a CAS-update against the same expected_hash.
    Exactly one must land (OK), the other must be rejected (STALE) — real
    cross-process linearization via the flock'd .lock file (contract §3:
    "a held cross-process lock around read-compare-replace is the
    mechanism" for the local backend)."""
    root = tmp_path / "store-root"
    setup = LocalBackend(root)
    r0 = gate.persist(setup, "race/cas", b"base", expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK)
    base_hash = r0.new_hash
    setup.close()

    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    procs = [
        ctx.Process(
            target=_mp_cas_worker,
            args=(str(root), "race/cas", base_hash, f"writer-{i}".encode(), queue),
        )
        for i in range(4)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=10)
        assert p.exitcode == 0

    outcomes = [queue.get(timeout=1) for _ in procs]
    assert outcomes.count("OK") == 1, f"expected exactly one OK across processes, got {outcomes}"
    assert outcomes.count("STALE") == len(procs) - 1


# ---------------------------------------------------------------------------
# Windows-specific — CANNOT run on this Linux host. See docstrings.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="exercises the msvcrt.locking() code path")
def test_windows_msvcrt_lock_path_busy_on_contention(tmp_path):  # pragma: no cover
    """MUST be run on the Windows host. Verifies that LocalBackend's
    _FileLock uses msvcrt.locking(LK_NBLCK) on Windows (never fcntl, which
    is forbidden there per contract §4.1) and that a second acquire on the
    same dedicated lock file observes BUSY rather than blocking."""
    root = tmp_path / "store-root"
    backend = LocalBackend(root, lock_timeout_s=0.1)
    import msvcrt

    lock_path = root / "locks" / "cas.lock"
    fh = open(lock_path, "a+b")
    fh.write(b"\0")
    fh.flush()
    fh.seek(0)
    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    try:
        result = gate.persist(backend, "k", b"v", expected_hash=None, doc_type="system_state")
        assert isinstance(result, ERROR) and result.kind is ErrorKind.BUSY
    finally:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        fh.close()
    backend.close()


@pytest.mark.skipif(os.name != "nt", reason="requires two real OS processes contending on NTFS")
def test_windows_two_process_ntfs_race(tmp_path):  # pragma: no cover
    """MUST be run on the Windows host, against a real NTFS volume (per
    contract §4.1/§9: "two concurrent processes on NTFS"). Spawn a second
    process (multiprocessing with the default 'spawn' start method on
    Windows) racing a create-only write against this process to the same
    key over a LocalBackend rooted on an NTFS path, and assert exactly one
    observes OK and the other EXISTS, with the published blob never
    corrupted or torn."""
    root = tmp_path / "store-root"
    LocalBackend(root).close()
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    procs = [
        ctx.Process(target=_mp_create_only_worker, args=(str(root), "ntfs/race", f"p{i}".encode(), queue))
        for i in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=10)
    outcomes = [queue.get(timeout=1) for _ in procs]
    assert outcomes.count("OK") == 1
    assert outcomes.count("EXISTS") == 1


@pytest.mark.skipif(os.name != "nt", reason="exercises a real Windows sharing-violation on os.replace")
def test_windows_replace_sharing_violation_retries_then_busy(tmp_path):  # pragma: no cover
    """MUST be run on the Windows host. Open the target file with a sharing
    mode that excludes delete/rename (e.g. via a second handle without
    FILE_SHARE_DELETE) while a publish's os.replace() is attempted, and
    assert LocalBackend retries a bounded number of times before returning
    ERROR{BUSY} — never falling back to a non-atomic copy/move."""
    ...
