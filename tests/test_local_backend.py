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
import tempfile
from pathlib import Path
import time
import types

import pytest

from store import gate
from store.backend import BackendBusyError, BackendCorruptionError
from store.local import LocalBackend
from store.types import ERROR, OK, STALE, ErrorKind, sha256_hex
from tests.ctx_helpers import create_ctx, fenced_ctx, overwrite_ctx


def _gen(n: int) -> bytes:
    return gate.make_generation_header(n)


@pytest.mark.parametrize('v2', [False, True])
@pytest.mark.parametrize('frame', ['oversize-key', 'overflow-body', 'missing-digest', 'missing-fence'])
def test_journal_lengths_checked_before_allocating_and_writes_fail_closed(tmp_path, monkeypatch, v2, frame):
    """Corrupt lengths must not reach file.read(n) as allocation requests.

    A bounded read spy makes even UINT32 key-length coverage safe on the
    unfixed parser. A malformed tail makes the journal incomplete (end !=
    size); read/list report corruption and writes refuse without appending.
    """
    import builtins
    import hashlib
    import struct

    backend = LocalBackend(tmp_path)
    assert isinstance(gate.persist(backend, 'p/good', b'good', ctx=create_ctx(), doc_type='system_state'), OK)
    prefix = b'KKJ2' if v2 else b''
    if frame == 'oversize-key':
        tail = prefix + struct.pack('>I', 2**32-1)
    else:
        key = b'p/bad'
        length = 2**64-1 if frame == 'overflow-body' else 1
        tail = prefix + struct.pack('>I', len(key)) + key + struct.pack('>Q', length)
        if frame != 'overflow-body':
            tail += b'x'
        if frame == 'missing-fence':
            # On legacy this is a valid record; truncate the digest instead.
            tail += hashlib.sha256(b'x').digest()[:32 if v2 else 31]
    backend._journal_fh.write(tail)
    backend._journal_fh.flush()
    original = backend._journal_path.read_bytes()
    real_open = builtins.open

    class BoundedRead:
        def __init__(self, wrapped):
            self.wrapped = wrapped
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.wrapped.close()
        def __getattr__(self, name):
            return getattr(self.wrapped, name)
        def read(self, n=-1):
            assert 0 <= n <= len(original), 'untrusted journal length reached allocation'
            return self.wrapped.read(n)

    def guarded_open(path, mode='r', *args, **kwargs):
        opened = real_open(path, mode, *args, **kwargs)
        return BoundedRead(opened) if str(path) == str(backend._journal_path) and mode == 'rb' else opened

    monkeypatch.setattr(builtins, 'open', guarded_open)
    try:
        result = gate.persist(backend, 'p/new', b'new', ctx=create_ctx(), doc_type='system_state')
        assert result == ERROR(ErrorKind.CORRUPTION)
        assert backend._journal_path.read_bytes() == original
        with pytest.raises(BackendCorruptionError):
            backend.read('p/new')
        backend.close()
        fresh = LocalBackend(tmp_path)
        try:
            assert fresh._journal_ambiguous
            with pytest.raises(BackendCorruptionError):
                fresh.read('p/good')
            assert gate.persist(fresh, 'p/new', b'new', ctx=create_ctx(), doc_type='system_state') == ERROR(ErrorKind.CORRUPTION)
            assert fresh._journal_path.read_bytes() == original
        finally:
            fresh.close()
    finally:
        backend.close()


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
    r = gate.persist(backend, key, good_body, ctx=create_ctx(), doc_type="system_state")
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
    """Trailing bytes after the last complete record forbid recovery publish;
    journal and any published bytes are preserved and read/list fail closed."""
    root = tmp_path / "store-root"
    backend = LocalBackend(root)
    key = "resume/good"
    backend._journal_append(key, b"good-and-durable")
    import struct

    torn_key = b"resume/torn-tail"
    backend._journal_fh.write(struct.pack(">I", len(torn_key)) + torn_key)
    backend._journal_fh.write(struct.pack(">Q", 10_000_000))
    backend._journal_fh.write(b"only-a-few-bytes")
    backend._journal_fh.flush()
    os.fsync(backend._journal_fh.fileno())
    journal_before = (root / "journal" / "journal.log").read_bytes()
    backend.close()

    resumed = LocalBackend(root)
    try:
        assert resumed._journal_ambiguous
        with pytest.raises(BackendCorruptionError):
            resumed.read(key)
        assert (root / "journal" / "journal.log").read_bytes() == journal_before
    finally:
        resumed.close()


# ---------------------------------------------------------------------------
# H1 Increment 2, Phase 1 — last_accepted_fence survives a store reopen
# ---------------------------------------------------------------------------


def test_fence_survives_reload(tmp_path):
    """last_accepted_fence is journal-durable (H1 Increment 2, Phase 1 (A)):
    a fresh LocalBackend constructed over the same root after a reopen must
    still refuse a stale-fence Overwrite, proving the fence floor was
    correctly replayed from the journal rather than reset to 0."""
    root = tmp_path / "store-root"
    key = "fence/reload"

    backend = LocalBackend(root)
    r0 = gate.persist(backend, key, b"v0", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r0, OK)

    lease_a = backend.lock(key, ttl_s=30)
    backend.unlock(lease_a)
    r1 = gate.persist(backend, key, b"v1", ctx=overwrite_ctx(r0.new_hash, lease_a), doc_type="system_state")
    assert isinstance(r1, OK)  # last_accepted_fence is now durably lease_a.fence
    backend.close()

    # Reopen: a brand-new instance over the same root, forcing journal replay.
    resumed = LocalBackend(root)

    # A fresh lock() on `resumed` must mint a fence STRICTLY greater than
    # lease_a.fence -- if last_accepted_fence had NOT survived the reload
    # (regressed to 0), this would instead be able to reuse/alias a fence at
    # or below lease_a.fence.
    lease_b = resumed.lock(key, ttl_s=30)
    assert lease_b.fence > lease_a.fence
    resumed.unlock(lease_b)
    r2 = gate.persist(resumed, key, b"v2", ctx=overwrite_ctx(r1.new_hash, lease_b), doc_type="system_state")
    assert isinstance(r2, OK)

    # The old, pre-reload lease_a (whose fence is now behind the durably
    # replayed last_accepted_fence) must still be refused post-reload.
    r3 = gate.persist(
        resumed, key, b"v3-stale-a-replay", ctx=overwrite_ctx(r2.new_hash, lease_a), doc_type="system_state"
    )
    assert isinstance(r3, STALE)
    assert r3.reason == "FENCE"
    assert resumed.read(key).body == b"v2"  # unchanged by the rejected replay
    resumed.close()


def test_fence_monotonic_across_instances(tmp_path):
    """H1 Increment 2, Phase 1b: the fence ALLOCATION counter must be
    durable and cross-process-monotonic, not just last_accepted_fence. Two
    separate LocalBackend instances over the SAME directory (standing in
    for two processes) both call lock() on the same key before either has
    written anything; they must never be handed the same fence number — a
    purely in-process allocation counter would let both compute the same
    'next' fence from the same durable last_accepted_fence floor, and the
    second writer would then silently clobber the first."""
    root = tmp_path / "store-root"
    key = "fence/two-instance"

    a = LocalBackend(root)
    r0 = gate.persist(a, key, b"v0", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r0, OK)

    b = LocalBackend(root)  # a SECOND instance over the SAME directory

    # A acquires then releases immediately (the fenced_ctx / production
    # pattern) before B ever tries — otherwise B's lock() would correctly
    # BackendBusyError on A's still-live advisory lock, which is a
    # DIFFERENT (already-working) mechanism than the fence-allocation
    # collision this test targets.
    lease_a = a.lock(key, ttl_s=30)
    a.unlock(lease_a)
    lease_b = b.lock(key, ttl_s=30)
    assert lease_b.fence > lease_a.fence, "two instances must never allocate the same fence"

    r1 = gate.persist(b, key, b"v2-from-b", ctx=overwrite_ctx(r0.new_hash, lease_b), doc_type="system_state")
    assert isinstance(r1, OK)

    # A's OLD lease replays against the CURRENT hash (the content-hash CAS
    # alone would accept this) — the fence, now correctly non-colliding,
    # must refuse it.
    r2 = gate.persist(
        a, key, b"v3-stale-a-replay", ctx=overwrite_ctx(r1.new_hash, lease_a), doc_type="system_state"
    )
    assert isinstance(r2, STALE)
    assert r2.reason == "FENCE"
    assert a.read(key).body == b"v2-from-b"
    assert b.read(key).body == b"v2-from-b"

    a.close()
    b.close()


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
    r0 = gate.persist(backend, key, b"old-complete-value", ctx=create_ctx(), doc_type="system_state")
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

    # Staging is half-written; the durable journal commit is authoritative on read.
    blob = backend.read(key)
    assert blob is not None
    assert blob.body == new_body

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
    """Deterministic by construction (review finding): the scavenger runs
    INSIDE the next `LocalBackend(root)` construction, after directory
    creation, the journal open, the case probe, the lock acquire, and
    `_resume()` -- on a loaded test runner that work can itself exceed a
    tiny TTL, making a TTL-vs-constructor-timing test flaky. A generous TTL
    (60s, vastly larger than any plausible construction time) plus mtimes
    set explicitly and far apart (now vs. one hour in the past) makes the
    outcome depend only on the mtimes this test sets, never on how long
    construction happens to take."""
    root = tmp_path / "store-root"
    backend = LocalBackend(root, staging_ttl_s=60.0)
    staging_dir = root / "staging"

    stale = staging_dir / "orphan-stale"
    stale.write_bytes(b"leftover-from-a-crashed-process")
    old_time = time.time() - 3600
    os.utime(stale, (old_time, old_time))

    fresh = staging_dir / "orphan-fresh"
    fresh.write_bytes(b"just-created")
    now = time.time()
    os.utime(fresh, (now, now))

    backend.close()

    # Re-run the scavenger by constructing a new adapter over the same root.
    resumed = LocalBackend(root, staging_ttl_s=60.0)
    assert not stale.exists(), "stale staging temp older than TTL must be removed"
    assert fresh.exists(), "a staging temp younger than TTL must be left alone"
    resumed.close()


# ---------------------------------------------------------------------------
# Generation-header regex is bounded — never a ValueError from int() on a
# pathologically long digit run
# ---------------------------------------------------------------------------


def test_extract_generation_is_bounded_and_never_raises_on_oversized_digits():
    from store.local import _extract_generation

    # Well-formed, within uint64 range.
    assert _extract_generation(_gen(42) + b"body") == 42

    # A pathologically long digit run must be rejected as malformed (None),
    # never reach int() and raise ValueError once it exceeds CPython's
    # int-string-conversion digit limit (review finding: an unbounded regex
    # would let this reach `int(m.group(1))` and crash write() instead of
    # returning a normal WriteResult).
    huge = b"#knokeep-gen:" + b"9" * 5000 + b"\nbody"
    assert _extract_generation(huge) is None

    # The first value beyond the 20-digit boundary is rejected too.
    assert _extract_generation(b"#knokeep-gen:" + b"1" * 21 + b"\nbody") is None

    # A 20-digit number that overflows uint64 is still correctly rejected
    # (matched by the regex, but caught by the explicit range check).
    too_big = b"#knokeep-gen:99999999999999999999\nbody"  # 20 nines > 2**64-1
    assert _extract_generation(too_big) is None


# ---------------------------------------------------------------------------
# Generation monotonicity (residual from T1) — enforced HERE, not in the gate
# ---------------------------------------------------------------------------


def test_generation_monotonicity_rejects_non_increasing_update(tmp_path):
    backend = LocalBackend(tmp_path / "store-root")
    key = "lease/holder"

    r0 = gate.persist(backend, key, _gen(1) + b"leaseholder=alice", ctx=create_ctx(), doc_type="lease")
    assert isinstance(r0, OK)

    # A same-or-lower generation under a MATCHING hash-CAS must be rejected
    # even though the plain hash-CAS check alone would have allowed it.
    r_same_gen = gate.persist(
        backend, key, _gen(1) + b"leaseholder=bob",
        ctx=fenced_ctx(backend, key, r0.new_hash), doc_type="lease",
    )
    assert isinstance(r_same_gen, STALE)

    r_lower_gen = gate.persist(
        backend, key, _gen(0) + b"leaseholder=bob",
        ctx=fenced_ctx(backend, key, r0.new_hash), doc_type="lease",
    )
    assert isinstance(r_lower_gen, STALE)

    # The store must be unchanged by either rejected attempt.
    assert backend.read(key).body == _gen(1) + b"leaseholder=alice"

    # A strictly-greater generation is accepted.
    r_higher_gen = gate.persist(
        backend, key, _gen(2) + b"leaseholder=bob",
        ctx=fenced_ctx(backend, key, r0.new_hash), doc_type="lease",
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
        backend.write("plain-str-key", b"plain-bytes-body", ctx=create_ctx())  # type: ignore[arg-type]
    assert backend.read("plain-str-key") is None
    assert list(backend.list("")) == []
    backend.close()


class _CaptureBackend:
    """A stand-in `backend` for gate.persist() that just captures the
    gate-issued ScannedKey/ScannedBody instead of doing any I/O, so a test
    can obtain a legitimately-issued pair and then tamper with it."""

    def __init__(self) -> None:
        self.captured = None

    def write(self, key, body, *, ctx):
        self.captured = (key, body)
        return OK("0" * 64)


def test_scanned_body_is_immutable_after_construction(tmp_path):
    """Hardening: ScannedKey/ScannedBody are now frozen after construction —
    a would-be tamper (e.g. rewriting `_body` post-issuance to smuggle a
    different payload past the marker) must raise TypeError immediately,
    rather than silently succeeding and leaving a stale-but-matching marker
    for the adapter to (previously) reject at write() time."""
    capture = _CaptureBackend()
    r = gate.persist(capture, "k", b"hello", ctx=create_ctx(), doc_type="system_state")
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
    result = backend.write(key_obj, body_obj, ctx=create_ctx())
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
        backend.write(forged_key, forged_body, ctx=create_ctx())
    assert backend.read("k") is None
    assert gate.verify(forged_key) is False
    assert gate.verify(forged_body) is False
    backend.close()


# ---------------------------------------------------------------------------
# Case-insensitive-volume collision refusal
# ---------------------------------------------------------------------------


def test_case_insensitive_collision_refused_when_flagged(tmp_path):
    """Exercise the case-collision refusal LOGIC by forcing the flag. This is
    only meaningful on a genuinely case-SENSITIVE host (e.g. Linux/ext4): there,
    the underlying filesystem keeps "Notes/foo" and "Notes/Foo" as distinct
    paths, so the refused colliding-case key is verifiably never created
    ("Notes/foo" reads back None). On a real case-insensitive volume (NTFS/APFS)
    "Notes/foo" ALIASES to the existing "Notes/Foo" file, so that assertion
    could not hold — skip there (the real behavior on such a volume is covered
    by running the full suite on that OS)."""
    probe = LocalBackend(tmp_path / "probe-root")
    real_case_insensitive = probe._case_insensitive
    probe.close()
    if real_case_insensitive:
        pytest.skip("real volume is case-insensitive; distinct-path assertion cannot hold here")

    backend = LocalBackend(tmp_path / "store-root")
    backend._case_insensitive = True  # simulate a case-insensitive volume on this case-sensitive host

    r0 = gate.persist(backend, "Notes/Foo", b"v1", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r0, OK)

    r1 = gate.persist(backend, "Notes/foo", b"v2", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r1, ERROR)
    assert r1.kind is ErrorKind.INVALID_ARGUMENT
    assert backend.read("Notes/foo") is None  # colliding-case key never created (distinct path on this host)
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
    if backend._case_insensitive:
        backend.close()
        pytest.skip("host temp volume is case-insensitive (e.g. macOS APFS); this test needs a case-sensitive volume")
    assert backend._case_insensitive is False

    r0 = gate.persist(backend, "Notes/Foo", b"v1", ctx=create_ctx(), doc_type="system_state")
    r1 = gate.persist(backend, "Notes/foo", b"v2", ctx=create_ctx(), doc_type="system_state")
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
        result = gate.persist(backend, "busy/k", b"v", ctx=create_ctx(), doc_type="system_state")
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


def _flaky_replace_into(monkeypatch, suffix: str, failures: int):
    """Deterministic injection: os.replace onto a destination ending in
    `suffix` raises PermissionError for the first `failures` attempts. Models
    a Windows sharing violation without claiming to reproduce one."""
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(a, b):
        if str(b).endswith(suffix):
            calls["n"] += 1
            if calls["n"] <= failures:
                raise PermissionError("simulated sharing violation")
        return real_replace(a, b)

    monkeypatch.setattr(os, "replace", flaky)
    return calls


@pytest.mark.parametrize("suffix", [".fence", ".lock"])
def test_lock_recovers_from_transient_lease_file_sharing_violation(tmp_path, monkeypatch, suffix):
    root = tmp_path / "store-root"
    backend = LocalBackend(root, replace_retry_attempts=5, replace_retry_backoff_s=0.001)
    key = "lease/transient"
    assert isinstance(gate.persist(backend, key, b"v0", ctx=create_ctx(), doc_type="system_state"), OK)
    calls = _flaky_replace_into(monkeypatch, suffix, failures=2)

    lease = backend.lock(key, ttl_s=30)
    assert calls["n"] == 3
    assert backend._read_fence_owner(backend._fence_owner_path(key))[0] == lease.token
    r = gate.persist(backend, key, b"v1", ctx=overwrite_ctx(sha256_hex(b"v0"), lease), doc_type="system_state")
    assert isinstance(r, OK)
    assert list((root / "locks" / "fence-alloc").glob("tmp*")) == []
    assert list((root / "locks" / "advisory").glob("tmp*")) == []
    backend.close()


def test_lock_fails_closed_when_fence_owner_replace_exhausted(tmp_path, monkeypatch):
    root = tmp_path / "store-root"
    backend = LocalBackend(root, replace_retry_attempts=3, replace_retry_backoff_s=0.001)
    key = "lease/exhausted"
    assert isinstance(gate.persist(backend, key, b"v0", ctx=create_ctx(), doc_type="system_state"), OK)
    holder = backend.lock(key, ttl_s=30)
    # Release the advisory slot so lock() reaches the injected replacement.
    # The prior durable fence remains until a successful new allocation.
    assert backend.unlock(holder)
    calls = _flaky_replace_into(monkeypatch, ".fence", failures=10**6)

    with pytest.raises(BackendBusyError):
        backend.lock(key, ttl_s=30)
    assert calls["n"] == 3
    # No new lease was issued: the durable owner is still the old holder,
    # the data is untouched, and no temp file leaked.
    owner = backend._read_fence_owner(backend._fence_owner_path(key))
    assert owner[0] == holder.token and owner[2] == holder.fence
    assert backend.read(key).body == b"v0"
    assert list((root / "locks" / "fence-alloc").glob("tmp*")) == []
    monkeypatch.undo()
    r = gate.persist(backend, key, b"v1", ctx=overwrite_ctx(sha256_hex(b"v0"), holder), doc_type="system_state")
    assert isinstance(r, OK)
    held = backend.lock(key, ttl_s=30)
    prior_owner = backend._read_fence_owner(backend._fence_owner_path(key))
    calls = _flaky_replace_into(monkeypatch, ".fence", failures=10**6)
    assert backend.renew(held, ttl_s=30) is False
    assert calls["n"] == 3
    assert backend._read_fence_owner(backend._fence_owner_path(key)) == prior_owner
    backend.close()


def test_advisory_replace_exhaustion_never_issues_a_lease(tmp_path, monkeypatch):
    backend = LocalBackend(tmp_path / "store-root", replace_retry_attempts=3, replace_retry_backoff_s=0.001)
    key = "lease/advisory-exhausted"
    assert isinstance(gate.persist(backend, key, b"v0", ctx=create_ctx(), doc_type="system_state"), OK)
    old = backend.lock(key, ttl_s=30)
    assert backend.unlock(old)
    calls = _flaky_replace_into(monkeypatch, ".lock", failures=10**6)
    with pytest.raises(BackendBusyError):
        backend.lock(key, ttl_s=30)
    assert calls["n"] == 3
    # Allocation can supersede the old fence before advisory publication
    # fails. No lease is returned and an old writer must not regain rights.
    owner = backend._read_fence_owner(backend._fence_owner_path(key))
    assert owner[2] > old.fence
    assert backend.read(key).body == b"v0"
    monkeypatch.undo()
    monkeypatch.setattr(LocalBackend, "_FENCE_LOSS_SETTLE_MAX_S", 0)
    assert isinstance(gate.persist(backend, key, b"bad", ctx=overwrite_ctx(sha256_hex(b"v0"), old), doc_type="system_state"), STALE)
    recovered = backend.lock(key, ttl_s=30)
    assert isinstance(gate.persist(backend, key, b"v1", ctx=overwrite_ctx(sha256_hex(b"v0"), recovered), doc_type="system_state"), OK)
    assert list(backend._locks_dir.rglob("tmp*")) == []
    backend.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows open-file replacement semantics")
def test_real_open_fence_handle_replacement_recovers(tmp_path, monkeypatch):
    import threading
    backend = LocalBackend(tmp_path / "store-root", replace_retry_attempts=20, replace_retry_backoff_s=0.01)
    key = "lease/real-sharing"
    old = backend.lock(key, ttl_s=30)
    assert backend.unlock(old)
    path = backend._fence_owner_path(key)
    reader = path.open("rb")
    release = threading.Event()
    errors = []
    real_replace = os.replace

    def observed_replace(src, dst):
        try:
            return real_replace(src, dst)
        except PermissionError:
            if str(dst) == str(path):
                errors.append("actual PermissionError on fence replacement")
                release.set()
            raise

    def close_reader():
        release.wait(2)
        reader.close()

    worker = threading.Thread(target=close_reader)
    worker.start()
    monkeypatch.setattr(os, "replace", observed_replace)
    try:
        lease = backend.lock(key, ttl_s=30)
        assert errors, "Windows did not demonstrate the assumed sharing violation"
        assert lease.fence > old.fence
        assert backend._read_fence_owner(path)[0] == lease.token
    finally:
        release.set()
        worker.join(3)
        reader.close()
        backend.close()


def test_fence_loss_settle_tolerates_transient_owner_read_denial(tmp_path, monkeypatch):
    backend = LocalBackend(tmp_path / "store-root")
    key = "lease/settle"
    assert isinstance(gate.persist(backend, key, b"v0", ctx=create_ctx(), doc_type="system_state"), OK)
    backend.lock(key, ttl_s=30)  # pending owner that never writes
    real_read = LocalBackend._read_fence_owner
    calls = {"n": 0}

    def flaky_read(self, path):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("simulated read during replace")
        return real_read(self, path)

    monkeypatch.setattr(LocalBackend, "_read_fence_owner", flaky_read)
    monkeypatch.setattr(LocalBackend, "_FENCE_LOSS_SETTLE_MAX_S", 0.2)
    assert backend._settle_current_hash_after_fence_loss(key, sha256_hex(b"v0")) == sha256_hex(b"v0")
    assert calls["n"] >= 3
    backend.close()


def test_lock_permanent_permission_error_on_other_paths_is_not_swallowed(tmp_path, monkeypatch):
    # Only os.replace's PermissionError is retried; a failure elsewhere (here
    # the temp-file creation) still propagates unchanged.
    backend = LocalBackend(tmp_path / "store-root", replace_retry_attempts=3, replace_retry_backoff_s=0.001)

    def denied(*a, **kw):
        raise PermissionError("simulated ACL denial")

    monkeypatch.setattr(tempfile, "mkstemp", denied)
    with pytest.raises(PermissionError):
        backend.lock("lease/acl", ttl_s=30)
    backend.close()


# ---------------------------------------------------------------------------
# Two-process race (POSIX) — real cross-process contention on the flock'd
# .lock file, not simulated within one process's threads.
# ---------------------------------------------------------------------------


def _mp_create_only_worker(root_str: str, key: str, payload: bytes, queue) -> None:
    # Separate OS process: a fresh LocalBackend instance over the SAME root.
    backend = LocalBackend(root_str)
    result = gate.persist(backend, key, payload, ctx=create_ctx(), doc_type="system_state")
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
    result = gate.persist(backend, key, payload, ctx=fenced_ctx(backend, key, base_hash), doc_type="system_state")
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
    r0 = gate.persist(setup, "race/cas", b"base", ctx=create_ctx(), doc_type="system_state")
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
        result = gate.persist(backend, "k", b"v", ctx=create_ctx(), doc_type="system_state")
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
@pytest.mark.skip(reason="not implemented yet: needs a handle opened without FILE_SHARE_DELETE")
def test_windows_replace_sharing_violation_retries_then_busy(tmp_path):  # pragma: no cover
    """MUST be run on the Windows host. Open the target file with a sharing
    mode that excludes delete/rename (e.g. via a second handle without
    FILE_SHARE_DELETE) while a publish's os.replace() is attempted, and
    assert LocalBackend retries a bounded number of times before returning
    ERROR{BUSY} — never falling back to a non-atomic copy/move."""
    ...


# ---------------------------------------------------------------------------
# Backward compatibility: journals written BEFORE the fence trailer existed
# (legacy record shape, no last_accepted_fence) must still replay in full,
# including when new-shape records are appended after them in the same file.
# ---------------------------------------------------------------------------


def _legacy_journal_record(key: str, body: bytes) -> bytes:
    """The pre-H1-Increment-2 record shape, byte for byte: no magic, no
    fence trailer (key_len, key, body_len, body, sha256 digest)."""
    import hashlib
    import struct

    key_bytes = key.encode("utf-8")
    return (
        struct.pack(">I", len(key_bytes))
        + key_bytes
        + struct.pack(">Q", len(body))
        + body
        + hashlib.sha256(body).digest()
    )


def test_legacy_journal_without_fence_trailer_replays_every_record(tmp_path):
    root = tmp_path / "store-root"
    backend = LocalBackend(root)
    backend.close()

    records = [("legacy/a", b"alpha"), ("legacy/b", b"beta"), ("legacy/a", b"alpha-v2"), ("legacy/c", b"")]
    with open(root / "journal" / "journal.log", "ab") as f:
        for key, body in records:
            f.write(_legacy_journal_record(key, body))

    resumed = LocalBackend(root)  # replays the legacy-only journal
    replayed = list(resumed._iter_journal_records())
    assert [(k, b) for k, b, _fence in replayed] == records, "every legacy record must be preserved, in order"
    assert all(fence == 0 for _k, _b, fence in replayed), "a legacy record replays with fence 0"
    assert resumed.read("legacy/a").body == b"alpha-v2"  # last record per key wins
    assert resumed.read("legacy/b").body == b"beta"
    assert resumed.read("legacy/c").body == b""
    resumed.close()


def test_new_records_appended_after_legacy_journal_replay_together(tmp_path):
    """The upgrade path: an existing (legacy) journal is appended to in place
    by the new adapter. Both shapes must replay from the same file, and the
    fence trailer must survive for the new records."""
    root = tmp_path / "store-root"
    backend = LocalBackend(root)
    backend.close()
    with open(root / "journal" / "journal.log", "ab") as f:
        f.write(_legacy_journal_record("mixed/old", b"old-value"))
        f.write(_legacy_journal_record("mixed/upgraded", b"v1"))

    upgraded = LocalBackend(root)
    assert upgraded.read("mixed/old").body == b"old-value"
    r1 = upgraded.read("mixed/upgraded")
    assert r1.body == b"v1"
    lease = upgraded.lock("mixed/upgraded", ttl_s=30)
    upgraded.unlock(lease)
    r2 = gate.persist(
        upgraded, "mixed/upgraded", b"v2", ctx=overwrite_ctx(r1.version_hash, lease), doc_type="system_state"
    )
    assert isinstance(r2, OK)
    r3 = gate.persist(upgraded, "mixed/new", b"brand-new", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r3, OK)
    upgraded.close()

    resumed = LocalBackend(root)
    replayed = list(resumed._iter_journal_records())
    assert [(k, b) for k, b, _f in replayed] == [
        ("mixed/old", b"old-value"),
        ("mixed/upgraded", b"v1"),
        ("mixed/upgraded", b"v2"),
        ("mixed/new", b"brand-new"),
    ]
    assert [f for _k, _b, f in replayed] == [0, 0, lease.fence, 0]
    assert resumed.read("mixed/old").body == b"old-value"
    assert resumed.read("mixed/upgraded").body == b"v2"
    assert resumed.read("mixed/new").body == b"brand-new"
    # The fence floor replayed from the v2 record (not reset to 0 by the
    # legacy records that precede it in the same file).
    assert resumed._last_accepted_fence["mixed/upgraded"] == lease.fence
    assert resumed._last_accepted_fence["mixed/old"] == 0
    resumed.close()


def test_torn_trailer_on_v2_record_drops_only_that_record(tmp_path):
    """Incomplete v2 fence trailer leaves end != size: no recovery publish."""
    root = tmp_path / "store-root"
    backend = LocalBackend(root)
    backend._journal_append("torn/keep", b"kept", 7)
    from store.local import _JOURNAL_V2_MAGIC

    full = _JOURNAL_V2_MAGIC + _legacy_journal_record("torn/tail", b"lost") + b"\x00\x00\x00"  # 3 of 8 trailer bytes
    backend._journal_fh.write(full)
    backend._journal_fh.flush()
    journal_before = (root / "journal" / "journal.log").read_bytes()
    backend.close()

    resumed = LocalBackend(root)
    try:
        assert resumed._journal_ambiguous
        assert list(resumed._iter_journal_records()) == [("torn/keep", b"kept", 7)]
        with pytest.raises(BackendCorruptionError):
            resumed.read("torn/keep")
        assert (root / "journal" / "journal.log").read_bytes() == journal_before
    finally:
        resumed.close()


# ---------------------------------------------------------------------------
# Durability of the fence-owner file: the directory entry is fsync'd after the
# atomic replace, so lock()'s new owner cannot revert on power loss.
# ---------------------------------------------------------------------------


def test_lock_fsyncs_fence_owner_directory_after_replace(tmp_path, monkeypatch):
    import store.local as local_module

    backend = LocalBackend(tmp_path / "store-root")
    synced = []
    real_fsync_dir = local_module._fsync_dir

    def _recording_fsync_dir(path):
        synced.append(path)
        return real_fsync_dir(path)

    monkeypatch.setattr(local_module, "_fsync_dir", _recording_fsync_dir)
    synced.clear()
    lease = backend.lock("durable/owner", ttl_s=30)
    assert backend._fence_alloc_dir in synced, "lock() must fsync the fence-alloc directory after replacing the owner file"
    synced.clear()
    assert backend.renew(lease, ttl_s=30) is True
    assert backend._fence_alloc_dir in synced, "renew() rewrites the owner file and must fsync its directory too"
    backend.unlock(lease)
    backend.close()


# ---------------------------------------------------------------------------
# lock() may not advance a key's fence owner while a write() that already
# passed its fence check is still committing (both are serialized on cas.lock).
# ---------------------------------------------------------------------------


def test_lock_waits_for_an_in_flight_fenced_write_to_commit(tmp_path):
    import threading

    root = tmp_path / "store-root"
    writer = LocalBackend(root)
    other = LocalBackend(root)          # a second process, sharing the directory
    key = "serialize/k"
    r0 = gate.persist(writer, key, b"v0", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r0, OK)
    lease = writer.lock(key, ttl_s=30)
    writer.unlock(lease)                 # released-but-unexpired: still the fence owner

    in_commit = threading.Event()
    proceed = threading.Event()
    real_commit = writer._commit

    def _slow_commit(*args, **kwargs):
        in_commit.set()                  # past _fence_ok(), cas.lock held, not yet durable
        assert proceed.wait(10)
        return real_commit(*args, **kwargs)

    writer._commit = _slow_commit
    results = {}
    t_write = threading.Thread(target=lambda: results.update(
        w=gate.persist(writer, key, b"v1", ctx=overwrite_ctx(r0.new_hash, lease), doc_type="system_state")))
    t_write.start()
    assert in_commit.wait(10)

    lock_done = threading.Event()
    t_lock = threading.Thread(target=lambda: (results.update(l=other.lock(key, ttl_s=30)), lock_done.set()))
    t_lock.start()
    # The competing lock() must NOT complete while the fenced write is mid-commit.
    assert not lock_done.wait(0.6), "lock() advanced the fence owner underneath an in-flight fenced write"
    proceed.set()
    t_write.join(10)
    t_lock.join(10)
    assert isinstance(results["w"], OK), results
    assert lock_done.is_set()

    # Ordering held: the write landed under its (then-current) fence, and the
    # new lease now supersedes it.
    replay = gate.persist(writer, key, b"v2-replay", ctx=overwrite_ctx(results["w"].new_hash, lease), doc_type="system_state")
    assert isinstance(replay, STALE) and replay.reason == "FENCE"
    fresh = gate.persist(other, key, b"v2", ctx=overwrite_ctx(results["w"].new_hash, results["l"]), doc_type="system_state")
    assert isinstance(fresh, OK)
    writer.close()
    other.close()


def test_renew_never_extends_the_advisory_lease_when_the_durable_owner_cannot_be_rewritten(tmp_path, monkeypatch):
    """The durable owner file is what write() enforces; if it cannot be
    rewritten, renew() must report False and leave the advisory expiry
    unchanged (extending only the advisory would wedge the key: the lease
    could not write, and every successor would stay BUSY for the TTL)."""
    backend = LocalBackend(tmp_path / "store-root")
    key = "renew/durable-first"
    lease = backend.lock(key, ttl_s=30)
    adv_before = backend._read_advisory(backend._advisory_lock_path(key))
    own_before = backend._read_fence_owner(backend._fence_owner_path(key))

    def _unwritable(*a, **kw):
        raise OSError("simulated: locks/fence-alloc became unwritable")

    monkeypatch.setattr(backend, "_write_fence_owner", _unwritable)
    assert backend.renew(lease, ttl_s=300) is False
    assert backend._read_advisory(backend._advisory_lock_path(key)) == adv_before
    assert backend._read_fence_owner(backend._fence_owner_path(key)) == own_before
    monkeypatch.undo()
    assert backend.renew(lease, ttl_s=300) is True
    assert backend._read_fence_owner(backend._fence_owner_path(key))[1] > own_before[1] + 200
    backend.close()


def test_renew_reports_false_once_a_second_instance_superseded_the_durable_owner(tmp_path):
    root = tmp_path / "store-root"
    a = LocalBackend(root)
    b = LocalBackend(root)
    key = "renew/superseded"
    mine = a.lock(key, ttl_s=0.4)
    time.sleep(0.5)                                   # advisory lapses, so b can acquire
    theirs = b.lock(key, ttl_s=30)
    assert theirs.fence > mine.fence
    # a's advisory record is gone/expired anyway; even a still-valid one must not renew.
    assert a.renew(mine, ttl_s=300) is False
    assert a._read_fence_owner(a._fence_owner_path(key))[0] == theirs.token
    a.close()
    b.close()


# ---------------------------------------------------------------------------
# Write-time recovery: a LONG-LIVED instance must complete another process's
# committed-but-unpublished journal record before deciding a write. The child
# is a real subprocess that os._exit()s immediately after _journal_append's
# fsync returns, before _publish runs.
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

from store.types import EXISTS  # noqa: E402

_CRASH_EXIT_CODE = 86
_CHILD_TTL_S = 1.0  # explicit short TEST lease, recorded by the child

_CRASH_CHILD = r"""
import os, sys
from store import gate
from store.local import LocalBackend
from tests.ctx_helpers import create_ctx, overwrite_ctx

mode, root, key, body, base_hash, ttl_s, crash_code = sys.argv[1:8]
backend = LocalBackend(root)
if mode == "cas":
    lease = backend.lock(key, ttl_s=float(ttl_s))
    sys.stdout.write("%r\n" % lease.expiry_epoch)
    sys.stdout.flush()
    ctx = overwrite_ctx(base_hash, lease)
else:
    ctx = create_ctx()
real_append = backend._journal_append

def append_then_die(*args, **kwargs):
    real_append(*args, **kwargs)  # returns only after the journal fsync
    os._exit(int(crash_code))  # crash BEFORE _publish

backend._journal_append = append_then_die
gate.persist(backend, key, body.encode(), ctx=ctx, doc_type="system_state")
os._exit(0)  # reaching here means the crash point was never hit
"""


def _run_crash_child(mode, root, key, body, base_hash=""):
    repo_root = _Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", _CRASH_CHILD, mode, str(root), key, body, base_hash,
         repr(_CHILD_TTL_S), str(_CRASH_EXIT_CODE)],
        cwd=str(repo_root), env=env, capture_output=True, text=True, timeout=60,
    )


def _journal_size(root):
    return os.path.getsize(root / "journal" / "journal.log")


def _lock_after_expiry(backend, key, child_expiry, budget_s=15.0):
    deadline = time.monotonic() + budget_s
    while True:
        if time.time() > child_expiry:
            try:
                return backend.lock(key, ttl_s=30)
            except Exception:
                pass
        assert time.monotonic() < deadline, "child's test lease never became acquirable"
        time.sleep(0.05)


def _data_path(root, key):
    return root / "data" / Path(*key.split("/"))


def _committed_unpublished_cas(root, key):
    """Parent opens first and stays open; the child then commits v1-child to
    the journal and dies before publishing. Returns (parent, base_hash,
    child_hash, child_expiry)."""
    parent = LocalBackend(root)
    r0 = gate.persist(parent, key, b"v0-base", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r0, OK)
    size_before = _journal_size(root)

    proc = _run_crash_child("cas", root, key, "v1-child", r0.new_hash)
    assert proc.returncode == _CRASH_EXIT_CODE, (proc.returncode, proc.stderr[-2000:])
    child_expiry = float(proc.stdout.strip().splitlines()[0])
    assert _journal_size(root) > size_before  # the child's record is durable
    # Published view is still stale on disk until write() (or a dedicated read test) replays.
    assert _data_path(root, key).read_bytes() == b"v0-base"
    return parent, r0.new_hash, sha256_hex(b"v1-child"), child_expiry


def test_long_lived_instance_recovers_unpublished_commit_before_cas(tmp_path):
    root = tmp_path / "store-root"
    key = "recover/cas"
    parent, base_hash, child_hash, child_expiry = _committed_unpublished_cas(root, key)
    try:
        lease = _lock_after_expiry(parent, key, child_expiry)
        assert lease.expiry_epoch > child_expiry
        stale = gate.persist(
            parent, key, b"v2-stale-competitor", ctx=overwrite_ctx(base_hash, lease), doc_type="system_state"
        )
        assert isinstance(stale, STALE), stale
        assert stale.current_hash == child_hash
        assert parent.read(key).body == b"v1-child"
        # Fence cache reflects the recovered record (the child's fence).
        assert parent._last_accepted_fence[key] >= 1
    finally:
        parent.close()

    fresh = LocalBackend(root)
    assert fresh.read(key).body == b"v1-child"  # durable successor survived
    fresh.close()


def test_long_lived_instance_create_only_sees_unpublished_creation(tmp_path):
    root = tmp_path / "store-root"
    key = "recover/create"
    parent = LocalBackend(root)
    try:
        proc = _run_crash_child("create", root, key, "child-created")
        assert proc.returncode == _CRASH_EXIT_CODE, (proc.returncode, proc.stderr[-2000:])
        assert not _data_path(root, key).exists()

        other = gate.persist(parent, key, b"different-body", ctx=create_ctx(), doc_type="system_state")
        assert isinstance(other, EXISTS), other
        assert other.current_hash == sha256_hex(b"child-created")
        assert parent.read(key).body == b"child-created"

        size = _journal_size(root)
        retry = gate.persist(parent, key, b"child-created", ctx=create_ctx(), doc_type="system_state")
        assert isinstance(retry, OK) and retry.new_hash == sha256_hex(b"child-created")
        assert _journal_size(root) == size  # idempotent retry appends nothing
    finally:
        parent.close()


def test_recovery_publish_failure_fails_closed_and_keeps_durable_record(tmp_path, monkeypatch):
    import store.local as local_module

    root = tmp_path / "store-root"
    key = "recover/publish-fails"
    parent, base_hash, child_hash, child_expiry = _committed_unpublished_cas(root, key)
    try:
        lease = _lock_after_expiry(parent, key, child_expiry)

        def always_busy(src, dst):
            raise local_module._ReplaceBusy()

        monkeypatch.setattr(parent, "_replace_with_retry", always_busy)
        busy = gate.persist(parent, key, b"v2-stale", ctx=overwrite_ctx(base_hash, lease), doc_type="system_state")
        assert isinstance(busy, ERROR) and busy.kind == ErrorKind.BUSY
        assert _data_path(root, key).read_bytes() == b"v0-base"  # write failed closed
        assert [b for k, b, _f in parent._iter_journal_records() if k == key][-1] == b"v1-child"

        monkeypatch.undo()
        stale = gate.persist(parent, key, b"v2-stale", ctx=overwrite_ctx(base_hash, lease), doc_type="system_state")
        assert isinstance(stale, STALE) and stale.current_hash == child_hash
        assert parent.read(key).body == b"v1-child"
    finally:
        parent.close()


def test_write_fails_closed_when_journal_scan_stops_before_eof(tmp_path):
    """Unparsable bytes before EOF may hide later durable records, so the
    latest committed value is unknown: refuse rather than decide."""
    root = tmp_path / "store-root"
    backend = LocalBackend(root)
    try:
        r0 = gate.persist(backend, "torn/k", b"v0", ctx=create_ctx(), doc_type="system_state")
        assert isinstance(r0, OK)
        backend._journal_fh.write(b"\x00\x00\x00\x05ab")  # torn record header
        backend._journal_fh.flush()
        size = _journal_size(root)
        r1 = gate.persist(backend, "torn/other", b"x", ctx=create_ctx(), doc_type="system_state")
        assert isinstance(r1, ERROR) and r1.kind == ErrorKind.CORRUPTION
        assert _journal_size(root) == size
        with pytest.raises(BackendCorruptionError):
            backend.read("torn/other")
    finally:
        backend.close()


def _assert_incomplete_journal_preserves_evidence(root, key, journal_before, data_before):
    fresh = LocalBackend(root)
    try:
        assert fresh._journal_ambiguous
        with pytest.raises(BackendCorruptionError):
            fresh.read(key)
        with pytest.raises(BackendCorruptionError):
            list(fresh.list(""))
        assert (root / "journal" / "journal.log").read_bytes() == journal_before
        assert _data_path(root, key).read_bytes() == data_before
    finally:
        fresh.close()


def test_incomplete_journal_opaque_only_tail_preserves_published(tmp_path):
    """Six-byte opaque tail alone: must not replay prefix over published data."""
    root = tmp_path / "store-root"
    key = "regress/opaque-only"
    backend = LocalBackend(root)
    gate.persist(backend, key, b"old-prefix", ctx=create_ctx(), doc_type="system_state")
    backend._journal_fh.write(b"\x00\x00\x00\x05ab")
    backend._journal_fh.flush()
    os.fsync(backend._journal_fh.fileno())
    rel = backend._key_to_relpath(key)
    data_path = backend._safe_join(backend._data_dir, rel)
    backend._publish(rel, data_path, b"published-later", create=False)
    journal_before = (root / "journal" / "journal.log").read_bytes()
    data_before = _data_path(root, key).read_bytes()
    assert data_before == b"published-later"
    backend.close()
    _assert_incomplete_journal_preserves_evidence(root, key, journal_before, data_before)


def test_incomplete_journal_opaque_then_legacy_frame_preserves_published(tmp_path):
    root = tmp_path / "store-root"
    key = "regress/opaque-legacy"
    backend = LocalBackend(root)
    gate.persist(backend, key, b"old-prefix", ctx=create_ctx(), doc_type="system_state")
    backend._journal_fh.write(b"\x00\x00\x00\x05ab")
    backend._journal_fh.write(_legacy_journal_record(key, b"new-from-legacy"))
    backend._journal_fh.flush()
    os.fsync(backend._journal_fh.fileno())
    backend._publish(backend._key_to_relpath(key), backend._safe_join(backend._data_dir, backend._key_to_relpath(key)), b"published-later", create=False)
    journal_before = (root / "journal" / "journal.log").read_bytes()
    data_before = _data_path(root, key).read_bytes()
    assert data_before == b"published-later"
    backend.close()
    _assert_incomplete_journal_preserves_evidence(root, key, journal_before, data_before)


def test_incomplete_journal_torn_v2_fence_preserves_published(tmp_path):
    root = tmp_path / "store-root"
    key = "regress/torn-fence"
    backend = LocalBackend(root)
    gate.persist(backend, key, b"old-prefix", ctx=create_ctx(), doc_type="system_state")
    from store.local import _JOURNAL_V2_MAGIC

    torn = _JOURNAL_V2_MAGIC + _legacy_journal_record(key, b"would-be-new") + b"\x00\x00\x00"
    backend._journal_fh.write(torn)
    backend._journal_fh.flush()
    rel = backend._key_to_relpath(key)
    data_path = backend._safe_join(backend._data_dir, rel)
    backend._publish(rel, data_path, b"published-later", create=False)
    journal_before = (root / "journal" / "journal.log").read_bytes()
    data_before = _data_path(root, key).read_bytes()
    assert data_before == b"published-later"
    backend.close()
    _assert_incomplete_journal_preserves_evidence(root, key, journal_before, data_before)


def test_read_list_after_child_crash_without_write(tmp_path):
    """read/list must observe another process's durable journal commit."""
    root = tmp_path / "store-root"
    key = "recover/read-only"
    parent = LocalBackend(root)
    try:
        proc = _run_crash_child("create", root, key, "from-child")
        assert proc.returncode == _CRASH_EXIT_CODE
        assert not _data_path(root, key).exists()
        assert parent.read(key).body == b"from-child"
        assert key in list(parent.list("recover/"))
    finally:
        parent.close()


def test_startup_opaque_suffix_preserves_bytes_and_fails_read(tmp_path):
    """Valid prefix + opaque suffix + later durable record: never clobber."""
    root = tmp_path / "store-root"
    key = "ambig/same"
    backend = LocalBackend(root)
    r0 = gate.persist(backend, key, b"old-body", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r0, OK)
    backend._journal_fh.write(b"\x00\x00\x00\x05ab")
    backend._journal_fh.flush()
    os.fsync(backend._journal_fh.fileno())
    backend._journal_append(key, b"new-body")
    rel = backend._key_to_relpath(key)
    data_path = backend._safe_join(backend._data_dir, rel)
    backend._publish(rel, data_path, b"new-body", create=False)
    backend.close()

    journal_before = (root / "journal" / "journal.log").read_bytes()
    data_before = _data_path(root, key).read_bytes()
    assert data_before == b"new-body"

    fresh = LocalBackend(root)
    try:
        assert fresh._journal_ambiguous
        with pytest.raises(BackendCorruptionError):
            fresh.read(key)
        with pytest.raises(BackendCorruptionError):
            list(fresh.list(""))
        assert (root / "journal" / "journal.log").read_bytes() == journal_before
        assert _data_path(root, key).read_bytes() == data_before
    finally:
        fresh.close()


def test_missing_journal_with_nonempty_data_fails_without_creating_log(tmp_path):
    root = tmp_path / "store-root"
    root.mkdir()
    (root / "data" / "orphan").mkdir(parents=True)
    (root / "data" / "orphan" / "blob").write_bytes(b"published-without-journal")
    journal = root / "journal" / "journal.log"
    assert not journal.exists()

    with pytest.raises(BackendCorruptionError, match="journal.log is missing"):
        LocalBackend(root)

    assert not journal.exists()
    assert (root / "data" / "orphan" / "blob").read_bytes() == b"published-without-journal"


def test_genuinely_empty_store_still_creates_journal(tmp_path):
    root = tmp_path / "store-root"
    backend = LocalBackend(root)
    try:
        assert (root / "journal" / "journal.log").is_file()
        r = gate.persist(backend, "new/k", b"first", ctx=create_ctx(), doc_type="system_state")
        assert isinstance(r, OK)
        assert backend.read("new/k").body == b"first"
    finally:
        backend.close()


def test_runtime_journal_deletion_fails_read_and_list(tmp_path):
    """LocalBackend keeps journal.log open for append for its whole lifetime.

    On Windows the file cannot be deleted while that append handle is open
    (unlike POSIX unlink-with-open-fd). Close only this fixture's append
    handle, then remove the path, while keeping the same backend instance
    for read/list — the adapter must not treat stale data/ as authoritative."""
    root = tmp_path / "store-root"
    key = "live/k"
    backend = LocalBackend(root)
    try:
        r = gate.persist(backend, key, b"authoritative", ctx=create_ctx(), doc_type="system_state")
        assert isinstance(r, OK)
        journal = root / "journal" / "journal.log"
        data_bytes = _data_path(root, key).read_bytes()
        backend._journal_fh.close()
        backend._journal_fh = None
        journal.unlink()
        assert not journal.exists()
        assert _data_path(root, key).read_bytes() == data_bytes
        with pytest.raises(BackendCorruptionError, match="journal.log is missing"):
            backend.read(key)
        with pytest.raises(BackendCorruptionError, match="journal.log is missing"):
            list(backend.list(""))
    finally:
        backend.close()

    with pytest.raises(BackendCorruptionError, match="journal.log is missing"):
        LocalBackend(root)


def test_constructor_closes_journal_handle_when_resume_fails(tmp_path, monkeypatch):
    root = tmp_path / "store-root"
    LocalBackend(root).close()

    def _boom(self):
        raise RuntimeError("resume failed for test")

    monkeypatch.setattr(LocalBackend, "_resume", _boom)
    with pytest.raises(RuntimeError, match="resume failed"):
        LocalBackend(root)
    monkeypatch.undo()

    again = LocalBackend(root)
    again.close()


def test_orphan_published_blob_without_journal_record(tmp_path):
    root = tmp_path / "store-root"
    LocalBackend(root).close()  # genuinely new store: creates empty journal.log
    orphan = root / "data" / "p" / "orphan"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"unjournaled-materialized-view")
    journal = root / "journal" / "journal.log"
    assert journal.is_file() and journal.read_bytes() == b""

    backend = LocalBackend(root)
    try:
        assert journal.is_file()
        with pytest.raises(BackendCorruptionError, match="no durable journal record"):
            backend.read("p/orphan")
        with pytest.raises(BackendCorruptionError, match="no durable journal record"):
            list(backend.list("p/"))
        assert orphan.read_bytes() == b"unjournaled-materialized-view"
        assert journal.read_bytes() == b""
    finally:
        backend.close()


def test_read_returns_none_for_missing_key_on_empty_store(tmp_path):
    root = tmp_path / "store-root"
    backend = LocalBackend(root)
    try:
        assert backend.read("no/such/key") is None
        assert list(backend.list("no/")) == []
    finally:
        backend.close()


def test_case_insensitivity_probe_leaves_no_data_files(tmp_path):
    """Probe markers must not remain under data/ after startup (lock-guarded)."""
    root = tmp_path / "store-root"
    backend = LocalBackend(root)
    try:
        for path in (root / "data").rglob("*"):
            if path.is_file():
                assert not path.name.startswith("case-probe-")
    finally:
        backend.close()
