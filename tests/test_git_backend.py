"""GitBackend-specific conformance (contract v1.4 §4.3, §9).

These exercise behavior the backend-agnostic conformance/suite.py cannot
reach: the actual concurrent-push races against a real local bare git repo,
the "stale per-key hash even on a clean fast-forward" guarantee, the
pre-publish secret scan over the to-be-uploaded object set (including
history), generation monotonicity, and the ScannedKey/ScannedBody TypeError
boundary. All writes that CAN go through the public API do so via
`store.gate.persist()` (the one door); the secret-in-history test is the one
exception, and it is called out explicitly below (JUDGMENT CALL 3 in
store/git_backend.py's module docstring): a `ScannedBody` carrying a secret
can never reach `GitBackend.write()` through `gate.persist()` at all (the
gate scans and blocks first), so that specific guarantee can only be
exercised by calling the backend's own private commit-building/publish
helpers directly, the same way tests/test_local_backend.py reaches into
LocalBackend's private journal primitives for states the public API cannot
produce.

Every bare "remote" here is a `git init --bare` repo in a pytest tmp_path —
no real network, no real GitHub remote (deferred per the task until a token
is rotated). Synthetic, non-functional secret look-alikes only.
"""
from __future__ import annotations

import subprocess
import threading
import types

import pytest

from store import gate
from store.git_backend import GitBackend, _extract_generation
from store.types import ERROR, EXISTS, OK, STALE, ErrorKind, sha256_hex


def _gen(n: int) -> bytes:
    return gate.make_generation_header(n)


def test_extract_generation_rejects_oversized_digit_runs():
    oversized = b"#knokeep-gen:" + b"9" * 5000 + b"\nbody"

    assert _extract_generation(_gen(42) + b"body") == 42
    assert _extract_generation(b"#knokeep-gen:" + b"1" * 21 + b"\nbody") is None
    assert _extract_generation(oversized) is None


def _init_bare(path) -> None:
    subprocess.run(
        ["git", "init", "--quiet", "--bare", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )


def _make_backend(tmp_path, name: str = "default") -> GitBackend:
    remote = tmp_path / f"remote-{name}.git"
    _init_bare(remote)
    work_dir = tmp_path / f"work-{name}"
    # Task instruction: set git user.email/user.name locally for the test
    # repos. GitBackend itself never relies on this (commit-tree is always
    # given explicit GIT_AUTHOR_*/GIT_COMMITTER_* env, precisely so it does
    # not depend on ambient/repo config) — this is set defensively anyway,
    # per the task's explicit instruction, and to guard against any git
    # subcommand ever needing it.
    backend = GitBackend(work_dir, remote)
    subprocess.run(
        ["git", "-C", str(work_dir), "config", "user.email", "test@knokeep.local"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work_dir), "config", "user.name", "knokeep-test"],
        check=True,
        capture_output=True,
    )
    return backend


# ---------------------------------------------------------------------------
# C1 — stale-reject under two concurrent writers on a real local bare repo
# ---------------------------------------------------------------------------


def test_c1_two_concurrent_writers_exactly_one_ok_other_stale(tmp_path):
    backend = _make_backend(tmp_path, "c1")
    r0 = gate.persist(backend, "k1", b"base", expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK)
    base_hash = r0.new_hash

    results = [None, None]
    barrier = threading.Barrier(2)

    def worker(i, body):
        barrier.wait()
        results[i] = gate.persist(
            backend, "k1", body, expected_hash=base_hash, doc_type="system_state"
        )

    t0 = threading.Thread(target=worker, args=(0, b"writer-0"))
    t1 = threading.Thread(target=worker, args=(1, b"writer-1"))
    t0.start()
    t1.start()
    t0.join()
    t1.join()

    oks = [r for r in results if isinstance(r, OK)]
    stales = [r for r in results if isinstance(r, STALE)]
    assert len(oks) == 1, results
    assert len(stales) == 1, results
    assert stales[0].current_hash == oks[0].new_hash

    winner_index = results.index(oks[0])
    winner_body = b"writer-0" if winner_index == 0 else b"writer-1"
    final = backend.read("k1")
    assert final is not None
    assert final.body == winner_body
    assert final.version_hash == oks[0].new_hash


def test_c1_eight_way_race_exactly_one_ok(tmp_path):
    backend = _make_backend(tmp_path, "c1b")
    r0 = gate.persist(backend, "race", b"base", expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK)
    base_hash = r0.new_hash

    n = 8
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        results[i] = gate.persist(
            backend, "race", f"writer-{i}".encode(), expected_hash=base_hash, doc_type="system_state"
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [r for r in results if isinstance(r, OK)]
    stales = [r for r in results if isinstance(r, STALE)]
    assert len(oks) == 1, results
    assert len(stales) == n - 1
    final = backend.read("race")
    assert final.version_hash == oks[0].new_hash
    for s in stales:
        assert s.current_hash == oks[0].new_hash


# ---------------------------------------------------------------------------
# C2 — create-only collision (single + race)
# ---------------------------------------------------------------------------


def test_c2_create_only_collision_single(tmp_path):
    backend = _make_backend(tmp_path, "c2")
    r0 = gate.persist(backend, "k2", b"first", expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK)
    r1 = gate.persist(backend, "k2", b"second", expected_hash=None, doc_type="system_state")
    assert isinstance(r1, EXISTS)
    assert r1.current_hash == r0.new_hash
    assert backend.read("k2").body == b"first"


def test_c2_create_only_collision_race(tmp_path):
    backend = _make_backend(tmp_path, "c2b")
    n = 8
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        results[i] = gate.persist(
            backend, "newkey", f"creator-{i}".encode(), expected_hash=None, doc_type="system_state"
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [r for r in results if isinstance(r, OK)]
    exists = [r for r in results if isinstance(r, EXISTS)]
    assert len(oks) == 1, results
    assert len(exists) == n - 1
    for e in exists:
        assert e.current_hash == oks[0].new_hash


# ---------------------------------------------------------------------------
# Stale-per-key-hash rejected even though the ref update would be a clean
# fast-forward (a DISTINCT, unrelated key changes in between; our write is
# independently stale, and must be rejected regardless) — contract §4.3.
# ---------------------------------------------------------------------------


def test_stale_per_key_hash_rejected_even_on_clean_fast_forward(tmp_path):
    backend = _make_backend(tmp_path, "stale-ff")

    # 1. Key A's first version.
    r_a0 = gate.persist(backend, "keyA", b"a-v0", expected_hash=None, doc_type="system_state")
    assert isinstance(r_a0, OK)

    # 2. A DISTINCT, unrelated key changes (advances the ref; does not touch A).
    r_b0 = gate.persist(backend, "keyB", b"b-v0", expected_hash=None, doc_type="system_state")
    assert isinstance(r_b0, OK)

    # 3. Key A is legitimately updated for real (a proper CAS write), so the
    #    caller's earlier knowledge of A's hash (r_a0.new_hash) is now stale.
    r_a1 = gate.persist(
        backend, "keyA", b"a-v1", expected_hash=r_a0.new_hash, doc_type="system_state"
    )
    assert isinstance(r_a1, OK)

    # 4. Our write targets A using the STALE hash from step 1. If the
    #    implementation only trusted git's own ref-level fast-forward check
    #    (rather than the mandatory explicit per-key comparison), this would
    #    build cleanly as a linear child of the CURRENT tip and push would
    #    succeed structurally — nothing at the git-DAG level conflicts. The
    #    contract requires STALE regardless (§4.3: "reject a stale per-key
    #    hash even if the push would fast-forward").
    result = gate.persist(
        backend, "keyA", b"a-v2-should-be-rejected",
        expected_hash=r_a0.new_hash, doc_type="system_state",
    )
    assert isinstance(result, STALE), result
    assert result.current_hash == r_a1.new_hash

    # Nothing durable changed: A is still exactly a-v1, B untouched.
    assert backend.read("keyA").body == b"a-v1"
    assert backend.read("keyB").body == b"b-v0"


# ---------------------------------------------------------------------------
# Secret-in-history refused before publish (§4.3: "scan the entire
# to-be-uploaded object set including history BEFORE any network/ref
# write"). Exercised via the backend's own private commit-building/publish
# helpers, since gate.persist() makes it impossible to hand a secret-laden
# ScannedBody to backend.write() through the public API at all (see
# store/git_backend.py's module docstring, JUDGMENT CALL 3).
# ---------------------------------------------------------------------------


def test_secret_blob_refused_before_publish_nothing_pushed(tmp_path):
    backend = _make_backend(tmp_path, "secret")
    remote = tmp_path / "remote-secret.git"

    secret_body = b"api_key = sk-" + b"A" * 40
    new_commit = backend._build_commit(None, "secrets/leaked", secret_body)

    result = backend._publish_checked(new_commit, None)
    assert isinstance(result, ERROR)
    assert result.kind is ErrorKind.SECRET_BLOCKED
    assert result.labels
    assert "hunter2" not in " ".join(result.labels)

    # Nothing durable/reachable-from-a-ref carries the secret: the remote
    # ref was never created.
    out = subprocess.run(
        ["git", "ls-remote", str(remote), "refs/heads/main"],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == ""
    # And the ordinary read path (which only ever looks at the published
    # ref) sees nothing either.
    assert backend.read("secrets/leaked") is None


def test_secret_in_history_blocks_an_otherwise_clean_new_commit(tmp_path):
    """A secret reachable ONLY through a prior, not-yet-pushed local commit
    (simulating history from a bypass of the gate, e.g. a prior process that
    wrote directly via GitBackend's own plumbing) must still block a LATER,
    individually-clean commit that is built on top of it — the scan walks
    the whole to-be-uploaded object set, not just the newest blob."""
    backend = _make_backend(tmp_path, "secret-history")
    remote = tmp_path / "remote-secret-history.git"

    # A poisoned commit, built directly via the backend's own plumbing
    # (bypassing gate.persist() entirely) — never published.
    secret_body = b"-----BEGIN RSA PRIVATE KEY-----\nZmFrZQ==\n-----END RSA PRIVATE KEY-----\n"
    poisoned_commit = backend._build_commit(None, "secrets/old-key", secret_body)

    # A perfectly clean, unrelated follow-up commit, built as a CHILD of the
    # poisoned commit (simulating: the poisoned commit is already "current
    # head" from the adapter's point of view, e.g. because a prior run
    # partially completed before crashing).
    clean_commit = backend._build_commit(poisoned_commit, "notes/clean", b"just a note")

    result = backend._publish_checked(clean_commit, None)
    assert isinstance(result, ERROR)
    assert result.kind is ErrorKind.SECRET_BLOCKED

    out = subprocess.run(
        ["git", "ls-remote", str(remote), "refs/heads/main"],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == ""


def test_clean_write_after_failed_secret_publish_still_works(tmp_path):
    """A blocked publish must not corrupt/wedge the backend: an ordinary,
    clean write through the public gate.persist() path afterwards succeeds
    normally."""
    backend = _make_backend(tmp_path, "secret-recover")
    secret_body = b"aws_key=AKIA" + b"C" * 16
    new_commit = backend._build_commit(None, "secrets/x", secret_body)
    result = backend._publish_checked(new_commit, None)
    assert isinstance(result, ERROR) and result.kind is ErrorKind.SECRET_BLOCKED

    ok = gate.persist(backend, "notes/after", b"clean content", expected_hash=None, doc_type="system_state")
    assert isinstance(ok, OK)
    assert backend.read("notes/after").body == b"clean content"
    assert backend.read("secrets/x") is None


# ---------------------------------------------------------------------------
# Generation monotonicity (residual, same shape as LocalBackend's)
# ---------------------------------------------------------------------------


def test_generation_monotonicity_rejects_non_increasing_update(tmp_path):
    backend = _make_backend(tmp_path, "gen")
    key = "lease/holder"

    r0 = gate.persist(backend, key, _gen(1) + b"leaseholder=alice", expected_hash=None, doc_type="lease")
    assert isinstance(r0, OK)

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

    assert backend.read(key).body == _gen(1) + b"leaseholder=alice"

    r_higher_gen = gate.persist(
        backend, key, _gen(2) + b"leaseholder=bob",
        expected_hash=r0.new_hash, doc_type="lease",
    )
    assert isinstance(r_higher_gen, OK)
    assert backend.read(key).body == _gen(2) + b"leaseholder=bob"


# ---------------------------------------------------------------------------
# Adapter accepts only ScannedBody/ScannedKey — raw bytes -> TypeError
# ---------------------------------------------------------------------------


def test_write_rejects_raw_bytes_before_any_io(tmp_path):
    backend = _make_backend(tmp_path, "typeerror")
    with pytest.raises(TypeError):
        backend.write("plain-str-key", b"plain-bytes-body", expected_hash=None)  # type: ignore[arg-type]
    assert backend.read("plain-str-key") is None
    assert list(backend.list("")) == []


class _CaptureBackend:
    """Stand-in `backend` for gate.persist() that captures the gate-issued
    ScannedKey/ScannedBody instead of doing I/O, so a test can obtain a
    legitimately-issued pair and then tamper with it (mirrors
    tests/test_local_backend.py's helper of the same name/purpose)."""

    def __init__(self) -> None:
        self.captured = None

    def write(self, key, body, *, expected_hash):
        self.captured = (key, body)
        return OK("0" * 64)


def test_scanned_body_is_immutable_after_construction(tmp_path):
    """Hardening: ScannedKey/ScannedBody are now frozen after construction —
    a would-be tamper (rewriting `_body` post-issuance) must raise TypeError
    immediately rather than silently succeeding."""
    capture = _CaptureBackend()
    r = gate.persist(capture, "k", b"hello", expected_hash=None, doc_type="system_state")
    assert isinstance(r, OK)
    key_obj, body_obj = capture.captured

    with pytest.raises(TypeError):
        body_obj._body = b"tampered-after-the-fact"
    with pytest.raises(TypeError):
        key_obj._key = "tampered"

    # Untampered, the legitimately-issued pair still writes fine.
    backend = _make_backend(tmp_path, "tamper")
    result = backend.write(key_obj, body_obj, expected_hash=None)
    assert isinstance(result, OK)
    assert backend.read("k").body == b"hello"


def test_write_rejects_forged_non_gate_object(tmp_path):
    """The adapter must accept ONLY genuine gate-issued ScannedKey/ScannedBody
    instances — never a look-alike stand-in with matching attribute names
    but no valid gate marker (checked before any I/O, contract §1/§5)."""
    forged_key = types.SimpleNamespace(key="k")
    forged_body = types.SimpleNamespace(body=b"hello")

    backend = _make_backend(tmp_path, "forged")
    with pytest.raises(TypeError):
        backend.write(forged_key, forged_body, expected_hash=None)
    assert backend.read("k") is None
    assert gate.verify(forged_key) is False
    assert gate.verify(forged_body) is False


# ---------------------------------------------------------------------------
# Sanity: idempotent create replay and pointer-stays-pointer also hold for
# the real git plumbing (already covered generically by
# conformance/suite.py parametrized over "git", exercised again here as a
# fast, git-specific smoke check since these are the properties the whole
# commit-building pipeline must never silently violate).
# ---------------------------------------------------------------------------


def test_idempotent_create_replay(tmp_path):
    backend = _make_backend(tmp_path, "idempotent")
    body = b"same-bytes-both-times"
    r0 = gate.persist(backend, "replay", body, expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK)
    r1 = gate.persist(backend, "replay", body, expected_hash=None, doc_type="system_state")
    assert isinstance(r1, OK)
    assert r1.new_hash == r0.new_hash


def test_only_target_keys_blob_mutated(tmp_path):
    """Writing key B must not change key A's stored bytes/hash at all."""
    backend = _make_backend(tmp_path, "isolation")
    r_a = gate.persist(backend, "keyA", b"a-content", expected_hash=None, doc_type="system_state")
    assert isinstance(r_a, OK)
    r_b = gate.persist(backend, "keyB", b"b-content", expected_hash=None, doc_type="system_state")
    assert isinstance(r_b, OK)

    blob_a = backend.read("keyA")
    assert blob_a.body == b"a-content"
    assert blob_a.version_hash == r_a.new_hash == sha256_hex(b"a-content")
