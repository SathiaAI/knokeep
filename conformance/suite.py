"""Backend-agnostic conformance suite (contract v1.4, §9).

Parametrized over `backend_factory`: add a new adapter by adding one entry to
BACKEND_FACTORIES below (or, for a real adapter, importing it and appending a
factory) — every test in this file then runs against it too.

All writes in this suite go through `store.gate.persist()`, never through
`backend.write()` directly and never by constructing ScannedKey/ScannedBody —
persist() is the one door (contract §1/§5), and tests/test_boundary.py
enforces that ScannedKey/ScannedBody are never constructed outside
store/gate.py, including from this file.
"""
from __future__ import annotations

import threading
import time
import uuid

import pytest

import subprocess

from migrate.migrate import migrate
from store import gate
from store.backend import BackendBusyError
from store.fake import FakeBackend
from store.git_backend import GitBackend
from store.local import LocalBackend
from store.objectstore import ObjectStoreBackend
from store.types import ERROR, EXISTS, OK, STALE, ErrorKind, commit_class, sha256_hex
from tests.moto_support import (
    DUMMY_ACCESS_KEY_ID,
    DUMMY_REGION,
    DUMMY_SECRET_ACCESS_KEY,
    get_moto_endpoint,
    make_bucket,
)

# Postgres connection params for the real, throwaway PostgreSQL 16 instance
# this task provides. Trust auth, no password (contract §4.4: adapter only,
# no credential-management layer of its own).
_PG_CONNECT_KWARGS = {
    "host": "127.0.0.1",
    "port": 5433,
    "user": "knokeep",
    "database": "knokeep_test",
}


def _make_fake(tmp_path):
    return FakeBackend()


def _make_local(tmp_path):
    return LocalBackend(tmp_path / "store-root")


def _make_git(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--quiet", "--bare", "-b", "main", str(remote)],
        check=True,
        capture_output=True,
    )
    work_dir = tmp_path / "git-work"
    return GitBackend(work_dir, remote)


def _pg_available() -> bool:
    try:
        import pg8000

        conn = pg8000.connect(timeout=2, **_PG_CONNECT_KWARGS)
        conn.close()
        return True
    except Exception:
        return False


def _make_postgres(tmp_path):
    from store.postgres import PostgresBackend

    # A fresh schema per test call -> full isolation between parametrized
    # runs (and between racing threads within one test) without needing to
    # touch any other test's rows; dropped by the `backend` fixture's
    # teardown below.
    schema = "knokeep_conf_" + uuid.uuid4().hex[:20]
    return PostgresBackend(dict(_PG_CONNECT_KWARGS), schema=schema)


def _make_objectstore(tmp_path):
    # Real S3-protocol end-to-end (contract §9): a fresh, DNS-valid bucket
    # per backend instance on the shared ThreadedMotoServer gives the same
    # test-to-test isolation `tmp_path` gives the local/git adapters.
    # `tmp_path` itself is unused here, kept only to match the
    # BACKEND_FACTORIES(tmp_path) call shape every other factory uses.
    bucket = make_bucket()
    return ObjectStoreBackend(
        endpoint=get_moto_endpoint(),
        bucket=bucket,
        region=DUMMY_REGION,
        access_key_id=DUMMY_ACCESS_KEY_ID,
        secret_access_key=DUMMY_SECRET_ACCESS_KEY,
    )


BACKEND_FACTORIES = {
    "fake": _make_fake,
    "local": _make_local,
    "git": _make_git,
    "objectstore": _make_objectstore,
}
if _pg_available():
    BACKEND_FACTORIES["postgres"] = _make_postgres


@pytest.fixture(params=sorted(BACKEND_FACTORIES.keys()))
def backend_factory(request):
    return BACKEND_FACTORIES[request.param]


@pytest.fixture
def backend(backend_factory, tmp_path):
    b = backend_factory(tmp_path)
    yield b
    # Backend-specific teardown (e.g. PostgresBackend drops its per-test
    # schema and closes its connections). Backends with no such need (fake,
    # local, git) simply don't define this hook.
    teardown = getattr(b, "_conformance_teardown", None)
    if teardown is not None:
        teardown()


def _require_fault_injection(backend):
    """Fault-injection hooks (inject_timeout_after_commit,
    inject_conflict_unknown, ...) simulate a REMOTE backend's ack getting
    lost after/around a commit — an ambiguity that exists because a network
    round-trip has a send/ack the caller cannot always observe. A local,
    synchronous filesystem write has no such ambiguity: write() either
    returns having fully committed (journal fsync'd) or fails clearly
    (BUSY/STALE/EXISTS/INVALID_ARGUMENT), or the process dies and resume()
    reconciles on restart — there is no in-between "sent but no ack" state
    for `LocalBackend` to inject. So these scenarios are inapplicable to a
    backend without the injection hooks, and are skipped rather than faked,
    mirroring the backend-specific skip stubs at the bottom of this file."""
    if not hasattr(backend, "inject_timeout_after_commit"):
        pytest.skip(
            "uncertain-outcome (TIMEOUT_AFTER_COMMIT/CONFLICT_UNKNOWN) fault "
            "injection is not applicable to this backend: a local synchronous "
            "filesystem write has no lost-ack ambiguity to simulate."
        )


def _gen(n: int) -> bytes:
    return gate.make_generation_header(n)


# ---------------------------------------------------------------------------
# C1 — stale-write reject (incl. a threaded race)
# ---------------------------------------------------------------------------


def test_c1_stale_write_single(backend):
    r0 = gate.persist(backend, "k1", b"hello", expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK)
    h0 = r0.new_hash

    # A CAS-update against a hash that no longer matches must be rejected.
    r1 = gate.persist(
        backend, "k1", b"goodbye", expected_hash="0" * 64, doc_type="system_state"
    )
    assert isinstance(r1, STALE)
    assert r1.current_hash == h0
    # The rejected write must not have changed the stored value.
    assert backend.read("k1").body == b"hello"


def test_c1_stale_write_absent_key_is_stale_none(backend):
    r = gate.persist(
        backend, "does/not/exist", b"x",
        expected_hash="a" * 64, doc_type="system_state",
    )
    assert isinstance(r, STALE)
    assert r.current_hash is None


def test_c1_stale_write_reject_race(backend):
    """Two writers race a CAS-update against the same expected_hash. Exactly
    one gets OK, the other gets STALE, and the store ends up holding exactly
    the winner's bytes (the loser never clobbers it)."""
    r0 = gate.persist(backend, "race1", b"base", expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK)
    base_hash = r0.new_hash

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    results = [None] * n_threads
    bodies = [f"writer-{i}".encode() for i in range(n_threads)]

    def worker(i):
        barrier.wait()
        results[i] = gate.persist(
            backend, "race1", bodies[i], expected_hash=base_hash, doc_type="system_state"
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [r for r in results if isinstance(r, OK)]
    stales = [r for r in results if isinstance(r, STALE)]
    assert len(oks) == 1, f"expected exactly one OK, got {results}"
    assert len(stales) == n_threads - 1

    winner_index = results.index(oks[0])
    final = backend.read("race1")
    assert final.body == bodies[winner_index]
    assert final.version_hash == oks[0].new_hash
    # No STALE result may report a current_hash other than the true winner's.
    for s in stales:
        assert s.current_hash == oks[0].new_hash


# ---------------------------------------------------------------------------
# C2 — create-only collision (incl. a threaded race)
# ---------------------------------------------------------------------------


def test_c2_create_only_collision_single(backend):
    r0 = gate.persist(backend, "k2", b"first", expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK)
    r1 = gate.persist(backend, "k2", b"second", expected_hash=None, doc_type="system_state")
    assert isinstance(r1, EXISTS)
    assert r1.current_hash == r0.new_hash
    assert backend.read("k2").body == b"first"


def test_c2_create_only_collision_race(backend):
    """N create-only writers race the same brand-new key with DIFFERENT
    bodies. Exactly one gets OK; the rest get EXISTS."""
    n_threads = 8
    barrier = threading.Barrier(n_threads)
    results = [None] * n_threads
    bodies = [f"creator-{i}".encode() for i in range(n_threads)]

    def worker(i):
        barrier.wait()
        results[i] = gate.persist(
            backend, "race2", bodies[i], expected_hash=None, doc_type="system_state"
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [r for r in results if isinstance(r, OK)]
    exists = [r for r in results if isinstance(r, EXISTS)]
    assert len(oks) == 1, f"expected exactly one OK, got {results}"
    assert len(exists) == n_threads - 1
    for e in exists:
        assert e.current_hash == oks[0].new_hash


# ---------------------------------------------------------------------------
# C4 — planted-secret via persist() is blocked; nothing written
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "planted, expect_label_substr",
    [
        # Synthetic, non-functional look-alikes — never real credentials.
        (b"api_key = sk-" + b"A" * 40, "openai"),
        (b"token: " + b"ghp_" + b"B" * 36, "github"),
        (b"aws_key=AKIA" + b"C" * 16, "AWS"),
        (
            b"-----BEGIN RSA PRIVATE KEY-----\nZmFrZQ==\n-----END RSA PRIVATE KEY-----\n",
            "PEM",
        ),
        (b"postgres://user:hunter2@db.example.internal:5432/app", "credential URI"),
    ],
)
def test_c4_secret_blocked_nothing_written(backend, planted, expect_label_substr):
    result = gate.persist(
        backend, "secrets/planted", planted, expected_hash=None, doc_type="system_state"
    )
    assert isinstance(result, ERROR)
    assert result.kind is ErrorKind.SECRET_BLOCKED
    assert result.labels, "SECRET_BLOCKED must carry at least one label"
    assert any(expect_label_substr.lower() in lbl.lower() for lbl in result.labels)
    # Fail closed: nothing must have reached the backend.
    assert backend.read("secrets/planted") is None
    # And the label list must never contain the planted secret value itself.
    joined = " ".join(result.labels)
    assert "hunter2" not in joined
    assert "AKIA" + "C" * 16 not in joined
    assert "sk-" + "A" * 40 not in joined


def test_c4_high_entropy_token_blocked(backend):
    """A real high-entropy secret (mixed-case, not plain hex) must still be
    blocked. `token_hex` is pure hex — after the hardening, plain hex is
    legitimate content (sha256/git-SHA) and is intentionally NOT flagged; use
    `token_urlsafe` (base64url, mixed case) here so this test still exercises
    the entropy heuristic's real catch, not the now-corrected false positive."""
    import secrets as _secrets

    planted = b"session_secret=" + _secrets.token_urlsafe(32).encode()
    result = gate.persist(
        backend, "secrets/entropy", planted, expected_hash=None, doc_type="system_state"
    )
    assert isinstance(result, ERROR)
    assert result.kind is ErrorKind.SECRET_BLOCKED
    assert backend.read("secrets/entropy") is None


def test_c4_clean_body_not_blocked(backend):
    result = gate.persist(
        backend, "notes/clean", b"just an ordinary status note, nothing secret here",
        expected_hash=None, doc_type="system_state",
    )
    assert isinstance(result, OK)


# ---------------------------------------------------------------------------
# C5 — pointer stays pointer (write a reference string, read back identical bytes)
# ---------------------------------------------------------------------------


def test_c5_pointer_stays_pointer(backend):
    pointer_body = _gen(1) + b"s3://knokeep-bucket/objects/1234-abcd-pointer"
    result = gate.persist(
        backend, "pointers/ref-1", pointer_body, expected_hash=None, doc_type="pointer_ref"
    )
    assert isinstance(result, OK)
    blob = backend.read("pointers/ref-1")
    assert blob is not None
    # Verbatim bytes back — the store never follows/resolves the reference.
    assert blob.body == pointer_body
    assert blob.version_hash == sha256_hex(pointer_body)


# ---------------------------------------------------------------------------
# C7 — lock TTL + token
# ---------------------------------------------------------------------------


def test_c7_lock_ttl_and_token(backend):
    lock1 = backend.lock("lockable", ttl_s=0.05)
    assert len(bytes.fromhex(lock1.token)) * 8 == 128  # 128-bit token

    # While held and unexpired, a second acquire must not succeed.
    with pytest.raises(BackendBusyError):
        backend.lock("lockable", ttl_s=5)

    time.sleep(0.08)  # let lock1 expire

    # A successor can now acquire the same key.
    lock2 = backend.lock("lockable", ttl_s=5)
    assert lock2.token != lock1.token

    # The original (expired, superseded) token must not unlock the new holder.
    assert backend.unlock(lock1) is False

    # CAS succeeds regardless of an active lock (locks are advisory, never
    # the CAS mechanism for this in-memory backend).
    r = gate.persist(backend, "under-lock", b"v1", expected_hash=None, doc_type="system_state")
    assert isinstance(r, OK)

    # Clean up the still-valid lock, then let it expire and confirm CAS still
    # succeeds with only an EXPIRED lock present (never removed explicitly).
    assert backend.unlock(lock2) is True

    lock3 = backend.lock("under-lock", ttl_s=0.05)
    time.sleep(0.08)
    # lock3 is now expired but still present in the backend's bookkeeping.
    r2 = gate.persist(
        backend, "under-lock", b"v2", expected_hash=r.new_hash, doc_type="system_state"
    )
    assert isinstance(r2, OK)
    # Renew must fail once expired, even with the correct token.
    assert backend.renew(lock3, ttl_s=5) is False


# ---------------------------------------------------------------------------
# §7 reconciliation — committed-then-superseded -> CONFLICT_UNKNOWN, NEVER STALE
# ---------------------------------------------------------------------------


def test_reconcile_our_write_landed_returns_ok(backend):
    """TIMEOUT_AFTER_COMMIT where the write actually landed: reconcile must
    report OK, and must not re-write."""
    _require_fault_injection(backend)
    r0 = gate.persist(backend, "amb1", b"v0", expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK)

    backend.inject_timeout_after_commit(1)
    intended = b"v1"
    intended_hash = sha256_hex(intended)
    r1 = gate.persist(
        backend, "amb1", intended, expected_hash=r0.new_hash, doc_type="system_state"
    )
    assert isinstance(r1, ERROR) and r1.kind is ErrorKind.TIMEOUT_AFTER_COMMIT
    assert commit_class(r1) == "outcome_unknown"

    result = gate.reconcile(
        backend, "amb1", intended_new_hash=intended_hash, expected_hash=r0.new_hash
    )
    assert isinstance(result, OK)
    assert result.new_hash == intended_hash
    assert backend.read("amb1").body == intended  # still just the one write


def test_reconcile_committed_then_superseded_is_conflict_unknown_never_stale(backend):
    """The ambiguous branch: our CAS write's ack was lost (CONFLICT_UNKNOWN),
    and by the time we look, someone else's write has landed instead. This
    MUST resolve to CONFLICT_UNKNOWN and must NEVER be reported as STALE,
    per contract §7 ("a lost ack cannot distinguish 'our write committed then
    was superseded' from 'our write never committed'")."""
    _require_fault_injection(backend)
    r0 = gate.persist(backend, "amb2", b"v0", expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK)

    backend.inject_conflict_unknown(1)
    intended = b"v1-ours"
    intended_hash = sha256_hex(intended)
    r1 = gate.persist(
        backend, "amb2", intended, expected_hash=r0.new_hash, doc_type="system_state"
    )
    assert isinstance(r1, ERROR) and r1.kind is ErrorKind.CONFLICT_UNKNOWN
    assert commit_class(r1) == "outcome_unknown"
    # Our write did NOT actually land (fake's conflict_unknown fault path).
    assert backend.read("amb2").body == b"v0"

    # Now someone else supersedes the key before we reconcile.
    other = gate.persist(
        backend, "amb2", b"v1-someone-else", expected_hash=r0.new_hash, doc_type="system_state"
    )
    assert isinstance(other, OK)

    result = gate.reconcile(
        backend, "amb2", intended_new_hash=intended_hash, expected_hash=r0.new_hash
    )
    assert isinstance(result, ERROR)
    assert result.kind is ErrorKind.CONFLICT_UNKNOWN
    assert not isinstance(result, STALE)  # the load-bearing assertion: never a false STALE


def test_reconcile_still_current_no_retry_stays_conflict_unknown(backend):
    """current == expected_hash (nothing has changed since) and no retry is
    performed: outcome remains CONFLICT_UNKNOWN, not STALE and not OK."""
    _require_fault_injection(backend)
    r0 = gate.persist(backend, "amb3", b"v0", expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK)

    backend.inject_conflict_unknown(1)
    intended = b"v1"
    intended_hash = sha256_hex(intended)
    r1 = gate.persist(
        backend, "amb3", intended, expected_hash=r0.new_hash, doc_type="system_state"
    )
    assert isinstance(r1, ERROR) and r1.kind is ErrorKind.CONFLICT_UNKNOWN

    result = gate.reconcile(
        backend, "amb3", intended_new_hash=intended_hash, expected_hash=r0.new_hash, retry=None
    )
    assert isinstance(result, ERROR)
    assert result.kind is ErrorKind.CONFLICT_UNKNOWN


def test_reconcile_retry_stale_is_returned_as_the_true_outcome(backend):
    """current == expected_hash (our original write demonstrably did NOT
    land — the store still holds the pre-write value), we retry once, and
    the retry itself comes back STALE (someone raced the retry). Per the
    hardened §7 semantics, reconcile() now returns the retry's REAL result
    unchanged: this STALE is reported as reconcile()'s outcome. This is NOT
    a false STALE — the original write is proven not to have landed, so a
    STALE here is a TRUE stale (the retry lost a fresh race), and it is the
    truthful, actionable result for the caller. (Previously this branch
    swallowed the retry's result and always reported CONFLICT_UNKNOWN; that
    was the old, less-informative behavior this test used to encode.)"""
    _require_fault_injection(backend)
    r0 = gate.persist(backend, "amb4", b"v0", expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK)

    backend.inject_conflict_unknown(1)
    intended = b"v1"
    intended_hash = sha256_hex(intended)
    r1 = gate.persist(
        backend, "amb4", intended, expected_hash=r0.new_hash, doc_type="system_state"
    )
    assert isinstance(r1, ERROR) and r1.kind is ErrorKind.CONFLICT_UNKNOWN
    # Our write did NOT actually land: the store still holds the pre-write value.
    assert backend.read("amb4").body == b"v0"

    def flaky_retry():
        # Simulate a third party racing in right as we retry: make the retry
        # itself observe a mismatched expected_hash -> STALE.
        gate.persist(backend, "amb4", b"someone-else", expected_hash=r0.new_hash, doc_type="system_state")
        return gate.persist(
            backend, "amb4", intended, expected_hash=r0.new_hash, doc_type="system_state"
        )

    result = gate.reconcile(
        backend, "amb4", intended_new_hash=intended_hash, expected_hash=r0.new_hash, retry=flaky_retry
    )
    assert isinstance(result, STALE)
    assert result.current_hash == sha256_hex(b"someone-else")


def test_reconcile_retry_ok_is_a_fresh_commit(backend):
    _require_fault_injection(backend)
    r0 = gate.persist(backend, "amb5", b"v0", expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK)

    backend.inject_conflict_unknown(1)
    intended = b"v1"
    intended_hash = sha256_hex(intended)
    r1 = gate.persist(
        backend, "amb5", intended, expected_hash=r0.new_hash, doc_type="system_state"
    )
    assert isinstance(r1, ERROR) and r1.kind is ErrorKind.CONFLICT_UNKNOWN

    def retry():
        return gate.persist(
            backend, "amb5", intended, expected_hash=r0.new_hash, doc_type="system_state"
        )

    result = gate.reconcile(
        backend, "amb5", intended_new_hash=intended_hash, expected_hash=r0.new_hash, retry=retry
    )
    assert isinstance(result, OK)
    assert backend.read("amb5").body == intended


# ---------------------------------------------------------------------------
# INVALID_ARGUMENT for malformed hash / bad key, before any I/O
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_hash",
    ["not-hex", "a" * 63, "a" * 65, "A" * 64, "", 12345, b"a" * 64],
)
def test_invalid_argument_for_malformed_hash(backend, bad_hash):
    result = gate.persist(
        backend, "badhash", b"body", expected_hash=bad_hash, doc_type="system_state"
    )
    assert isinstance(result, ERROR)
    assert result.kind is ErrorKind.INVALID_ARGUMENT
    assert commit_class(result) == "definitely_not_committed"
    assert backend.read("badhash") is None  # rejected before any I/O


@pytest.mark.parametrize(
    "bad_key",
    ["", "..", "a/../b", "a//b", "/leading", "trailing/", "CON", "con.txt", "a b", "has\\backslash", "trail "],
)
def test_invalid_argument_for_malformed_key(backend, bad_key):
    result = gate.persist(
        backend, bad_key, b"body", expected_hash=None, doc_type="system_state"
    )
    assert isinstance(result, ERROR)
    assert result.kind is ErrorKind.INVALID_ARGUMENT


# ---------------------------------------------------------------------------
# Idempotent create replay
# ---------------------------------------------------------------------------


def test_idempotent_create_replay(backend):
    body = b"same-bytes-both-times"
    r0 = gate.persist(backend, "replay", body, expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK)
    r1 = gate.persist(backend, "replay", body, expected_hash=None, doc_type="system_state")
    assert isinstance(r1, OK)
    assert r1.new_hash == r0.new_hash


# ---------------------------------------------------------------------------
# Doc-type allowlist refusal for a non-STATE type without a generation (§6)
# ---------------------------------------------------------------------------


def test_doc_type_allowlist_refuses_non_state_without_generation(backend):
    result = gate.persist(
        backend, "lease/x", b"no generation header here",
        expected_hash=None, doc_type="lease",
    )
    assert isinstance(result, ERROR)
    assert result.kind is ErrorKind.INVALID_ARGUMENT
    assert backend.read("lease/x") is None


def test_doc_type_allowlist_accepts_non_state_with_generation(backend):
    result = gate.persist(
        backend, "lease/y", _gen(1) + b"leaseholder=alice",
        expected_hash=None, doc_type="lease",
    )
    assert isinstance(result, OK)


@pytest.mark.parametrize("state_doc_type", sorted(gate.STATE_DOC_TYPES))
def test_doc_type_allowlist_state_types_do_not_need_generation(backend, state_doc_type):
    result = gate.persist(
        backend, f"state/{state_doc_type}", b"plain content, no generation header",
        expected_hash=None, doc_type=state_doc_type,
    )
    assert isinstance(result, OK)


# ---------------------------------------------------------------------------
# Backend-specific — SKIPPED here, documented for what a real adapter must add
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "C3 atomicity-under-crash is meaningful only for a backend with real "
        "durable storage and a crash boundary (e.g. the local filesystem "
        "adapter's journal-append + staged-rename pipeline, contract §3/§4.1). "
        "A real adapter must add a test that kills the process between the "
        "journal fsync and the atomic publish rename, then asserts the "
        "recovered key is either the old blob or the new blob in full, never "
        "torn or partially written, and that the startup scavenger clears "
        "orphaned staging files by TTL."
    )
)
def test_c3_atomicity_under_crash_is_backend_specific():
    ...


@pytest.mark.skip(
    reason=(
        "C6 (independent gitleaks audit) is an out-of-process, CI-level scan "
        "of the object set actually written (e.g. `git` adapter's history), "
        "not something an in-memory fake can exercise. A real adapter's "
        "conformance run must include a gitleaks pass over all blobs/trees/"
        "commits/notes it wrote (contract §4.3, 'scan the entire "
        "to-be-uploaded object set incl. history BEFORE any network write')."
    )
)
def test_c6_gitleaks_audit_is_backend_specific():
    ...


@pytest.mark.skip(
    reason=(
        "C8 (resume) exercises a real backend's own crash-recovery/replay "
        "path (contract §3 durability model: 'on resume, the journal is "
        "replayed to re-materialize any key whose published copy was lost or "
        "torn'). The in-memory fake has no journal and no resume path to "
        "test; a real adapter must add a test that restarts the adapter "
        "against a journal containing an unpublished commit and asserts the "
        "key is correctly re-materialized."
    )
)
def test_c8_resume_is_backend_specific():
    ...


@pytest.mark.skip(
    reason=(
        "C9 (no-egress) verifies a backend makes no unexpected network calls "
        "(e.g. no default SDK credential chain, no IMDS/telemetry, pinned "
        "endpoint only — contract §4.2). The in-memory fake never touches the "
        "network by construction, so this check is vacuous here; a real "
        "remote adapter must add a test that fails if any socket/connection "
        "is opened outside the explicitly configured endpoint."
    )
)
def test_c9_no_egress_is_backend_specific():
    ...


@pytest.mark.skip(
    reason=(
        "Windows-two-process-on-NTFS (contract §4.1) requires two real OS "
        "processes contending for msvcrt.locking() on an actual NTFS volume "
        "and is only meaningful for the local filesystem adapter running on "
        "Windows. A real local adapter must add a test that spawns a second "
        "process and asserts one of them observes ERROR{BUSY} rather than "
        "corrupting the published blob."
    )
)
def test_windows_two_process_ntfs_is_backend_specific():
    ...


def test_migration_into_every_backend(backend):
    """Migration conformance (contract §9 "migration") — cross-backend store
    migration (design/store_backend_contract.md §5), exercised against EVERY
    registered backend as the migration TARGET via the same
    `backend`/`backend_factory` parametrization every other test in this
    suite uses (fake/local/git/objectstore/postgres-if-available). This
    supersedes an earlier skipped placeholder here that read "migration" as
    an intra-adapter format/schema upgrade (a different, also valid, reading
    of the bare word "migration" in §9) — with `migrate.migrate.migrate` now
    implemented, the store-to-store reading is what the dedicated
    `migrate/migrate.py` task actually built, and it fits this suite's
    existing multi-backend parametrization directly, so it lives here rather
    than only in tests/test_migration.py (which covers migrate()'s
    backend-agnostic edge cases: divergence/secret/corruption/interruption/
    freeze that don't need every backend re-parametrized).
    """
    source = FakeBackend()
    keys_and_bodies = {
        "notes/plain": b"an ordinary migrated value, nothing special",
        "state/with-generation": _gen(1) + b"lease-shaped body carried verbatim",
        "empty/body": b"",
    }
    for key, body in keys_and_bodies.items():
        r = gate.persist(source, key, body, expected_hash=None, doc_type="system_state")
        assert isinstance(r, OK)

    report = migrate(source, backend)
    assert report.status == "success", report.reason
    assert report.source_key_count == len(keys_and_bodies)
    assert report.keys_copied == len(keys_and_bodies)
    assert report.keys_already_present == 0
    assert report.keys_verified == len(keys_and_bodies)
    assert report.unexpected_target_keys == ()

    for key, body in keys_and_bodies.items():
        blob = backend.read(key)
        assert blob is not None
        assert blob.body == body
        assert blob.version_hash == sha256_hex(body)

    # Re-running against the same (now fully populated) target is a no-op
    # copy-wise and still reports success (contract §3 idempotent create-only
    # replay; module docstring requirement #7 partial-failure resumability).
    report2 = migrate(source, backend)
    assert report2.status == "success", report2.reason
    assert report2.keys_copied == 0
    assert report2.keys_already_present == len(keys_and_bodies)
