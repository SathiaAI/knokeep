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
import time
import types

import pytest

from store import gate
from store.backend import Lock
from store.git_backend import GitBackend, _extract_generation
from store.types import ERROR, EXISTS, OK, STALE, ErrorKind, sha256_hex
from tests.ctx_helpers import create_ctx, fenced_ctx, overwrite_ctx


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
    r0 = gate.persist(backend, "k1", b"base", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r0, OK)
    base_hash = r0.new_hash

    results = [None, None]
    barrier = threading.Barrier(2)

    def worker(i, body):
        barrier.wait()
        results[i] = gate.persist(
            backend, "k1", body, ctx=fenced_ctx(backend, "k1", base_hash), doc_type="system_state"
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
    # Final committed state is unambiguously the winner (the safety property).
    assert backend.read("k1").version_hash == oks[0].new_hash
    # STALE.current_hash is BEST-EFFORT after the owner-approved 2026-09-24
    # simplification of _settle_current_hash_after_fence_loss to a single
    # re-read (D-008): git has no global mutex, so a loser may re-read the head
    # before the winner's commit lands. It is therefore either the winner's hash
    # or the pre-race base; the caller reconciles on STALE (contract §7), so a
    # momentary lag self-corrects. Only the immediate "loser hash == winner hash"
    # convenience was relaxed, never the fence rejection or the final state.
    assert stales[0].current_hash in (oks[0].new_hash, base_hash)

    winner_index = results.index(oks[0])
    winner_body = b"writer-0" if winner_index == 0 else b"writer-1"
    final = backend.read("k1")
    assert final is not None
    assert final.body == winner_body
    assert final.version_hash == oks[0].new_hash


def test_c1_eight_way_race_exactly_one_ok(tmp_path):
    backend = _make_backend(tmp_path, "c1b")
    r0 = gate.persist(backend, "race", b"base", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r0, OK)
    base_hash = r0.new_hash

    n = 8
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        results[i] = gate.persist(
            backend, "race", f"writer-{i}".encode(), ctx=fenced_ctx(backend, "race", base_hash), doc_type="system_state"
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
    assert final.version_hash == oks[0].new_hash        # final state = winner (safety)
    # STALE.current_hash is best-effort after the owner-approved single re-read
    # simplification (D-008, 2026-09-24): a git loser may re-read before the
    # winner's commit lands, so each is either the winner's hash or the pre-race
    # base -- a momentary lag the caller reconciles away on STALE (contract §7).
    # The fence rejection + final winner are unchanged.
    for s in stales:
        assert s.current_hash in (oks[0].new_hash, base_hash)


# ---------------------------------------------------------------------------
# C2 — create-only collision (single + race)
# ---------------------------------------------------------------------------


def test_c2_create_only_collision_single(tmp_path):
    backend = _make_backend(tmp_path, "c2")
    r0 = gate.persist(backend, "k2", b"first", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r0, OK)
    r1 = gate.persist(backend, "k2", b"second", ctx=create_ctx(), doc_type="system_state")
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
            backend, "newkey", f"creator-{i}".encode(), ctx=create_ctx(), doc_type="system_state"
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
    r_a0 = gate.persist(backend, "keyA", b"a-v0", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r_a0, OK)

    # 2. A DISTINCT, unrelated key changes (advances the ref; does not touch A).
    r_b0 = gate.persist(backend, "keyB", b"b-v0", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r_b0, OK)

    # 3. Key A is legitimately updated for real (a proper CAS write), so the
    #    caller's earlier knowledge of A's hash (r_a0.new_hash) is now stale.
    r_a1 = gate.persist(
        backend, "keyA", b"a-v1", ctx=fenced_ctx(backend, "keyA", r_a0.new_hash), doc_type="system_state"
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
        ctx=fenced_ctx(backend, "keyA", r_a0.new_hash), doc_type="system_state",
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

    ok = gate.persist(backend, "notes/after", b"clean content", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(ok, OK)
    assert backend.read("notes/after").body == b"clean content"
    assert backend.read("secrets/x") is None


# ---------------------------------------------------------------------------
# Generation monotonicity (residual, same shape as LocalBackend's)
# ---------------------------------------------------------------------------


def test_generation_monotonicity_rejects_non_increasing_update(tmp_path):
    backend = _make_backend(tmp_path, "gen")
    key = "lease/holder"

    r0 = gate.persist(backend, key, _gen(1) + b"leaseholder=alice", ctx=create_ctx(), doc_type="lease")
    assert isinstance(r0, OK)

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

    assert backend.read(key).body == _gen(1) + b"leaseholder=alice"

    r_higher_gen = gate.persist(
        backend, key, _gen(2) + b"leaseholder=bob",
        ctx=fenced_ctx(backend, key, r0.new_hash), doc_type="lease",
    )
    assert isinstance(r_higher_gen, OK)
    assert backend.read(key).body == _gen(2) + b"leaseholder=bob"


# ---------------------------------------------------------------------------
# Adapter accepts only ScannedBody/ScannedKey — raw bytes -> TypeError
# ---------------------------------------------------------------------------


def test_write_rejects_raw_bytes_before_any_io(tmp_path):
    backend = _make_backend(tmp_path, "typeerror")
    with pytest.raises(TypeError):
        backend.write("plain-str-key", b"plain-bytes-body", ctx=create_ctx())  # type: ignore[arg-type]
    assert backend.read("plain-str-key") is None
    assert list(backend.list("")) == []


class _CaptureBackend:
    """Stand-in `backend` for gate.persist() that captures the gate-issued
    ScannedKey/ScannedBody instead of doing I/O, so a test can obtain a
    legitimately-issued pair and then tamper with it (mirrors
    tests/test_local_backend.py's helper of the same name/purpose)."""

    def __init__(self) -> None:
        self.captured = None

    def write(self, key, body, *, ctx):
        self.captured = (key, body)
        return OK("0" * 64)


def test_scanned_body_is_immutable_after_construction(tmp_path):
    """Hardening: ScannedKey/ScannedBody are now frozen after construction —
    a would-be tamper (rewriting `_body` post-issuance) must raise TypeError
    immediately rather than silently succeeding."""
    capture = _CaptureBackend()
    r = gate.persist(capture, "k", b"hello", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r, OK)
    key_obj, body_obj = capture.captured

    with pytest.raises(TypeError):
        body_obj._body = b"tampered-after-the-fact"
    with pytest.raises(TypeError):
        key_obj._key = "tampered"

    # Untampered, the legitimately-issued pair still writes fine.
    backend = _make_backend(tmp_path, "tamper")
    result = backend.write(key_obj, body_obj, ctx=create_ctx())
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
        backend.write(forged_key, forged_body, ctx=create_ctx())
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
    r0 = gate.persist(backend, "replay", body, ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r0, OK)
    r1 = gate.persist(backend, "replay", body, ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r1, OK)
    assert r1.new_hash == r0.new_hash


def test_only_target_keys_blob_mutated(tmp_path):
    """Writing key B must not change key A's stored bytes/hash at all."""
    backend = _make_backend(tmp_path, "isolation")
    r_a = gate.persist(backend, "keyA", b"a-content", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r_a, OK)
    r_b = gate.persist(backend, "keyB", b"b-content", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r_b, OK)

    blob_a = backend.read("keyA")
    assert blob_a.body == b"a-content"
    assert blob_a.version_hash == r_a.new_hash == sha256_hex(b"a-content")


# ---------------------------------------------------------------------------
# H1 Increment 2, Phase 3: renew() -- advisory lease + durable fence sidecar.
# GitBackend reads the wall clock directly (time.time()), so expiry is driven
# with short TTLs and sub-second sleeps rather than an injected clock.
# ---------------------------------------------------------------------------


def _sidecar(backend: GitBackend, key: str):
    """(owner_token, owner_expiry, owner_fence, last_accepted_fence) at the remote tip."""
    return backend._read_fence_sidecar(backend._fetch_head(), key)


def test_renew_extends_lease_so_write_past_original_expiry_succeeds(tmp_path):
    backend = _make_backend(tmp_path, "renew-extend")
    key = "renew-key"
    r0 = gate.persist(backend, key, b"v0", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r0, OK)

    lease = backend.lock(key, ttl_s=2.0)
    original_expiry = lease.expiry_epoch
    assert backend.renew(lease, ttl_s=30.0) is True

    time.sleep(max(0.0, original_expiry - time.time()) + 0.3)  # past the ORIGINAL expiry, well inside the renewed one
    assert time.time() > original_expiry
    r1 = gate.persist(
        backend, key, b"v1", ctx=overwrite_ctx(r0.new_hash, lease), doc_type="system_state"
    )
    assert isinstance(r1, OK), r1
    assert backend.read(key).body == b"v1"
    # The accepted write recorded the (unchanged) fence as last_accepted.
    assert _sidecar(backend, key)[3] == lease.fence
    assert backend.unlock(lease) is True


def test_write_past_expiry_without_renew_is_fence_stale(tmp_path):
    """Control for the renew test: same timeline, no renew -> the durable
    fence has lapsed and the CAS-update is refused with reason FENCE."""
    backend = _make_backend(tmp_path, "renew-control")
    key = "lapse-key"
    r0 = gate.persist(backend, key, b"v0", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r0, OK)

    lease = backend.lock(key, ttl_s=0.4)
    time.sleep(0.6)
    r1 = gate.persist(
        backend, key, b"v1", ctx=overwrite_ctx(r0.new_hash, lease), doc_type="system_state"
    )
    assert isinstance(r1, STALE), r1
    assert r1.reason == "FENCE"
    assert backend.read(key).body == b"v0"


def test_renew_false_for_wrong_released_and_expired_tokens(tmp_path):
    backend = _make_backend(tmp_path, "renew-false")
    key = "k"

    # Never-locked key.
    assert backend.renew(Lock(key="never-locked", token="00" * 16, expiry_epoch=0.0), 5.0) is False

    lease = backend.lock(key, ttl_s=30.0)

    # Wrong token: refused, and the durable sidecar is left exactly as lock() wrote it.
    wrong = Lock(key=key, token="00" * 16, expiry_epoch=lease.expiry_epoch, fence=lease.fence)
    assert backend.renew(wrong, ttl_s=5.0) is False
    assert _sidecar(backend, key) == (lease.token, lease.expiry_epoch, lease.fence, 0)

    # Released token.
    assert backend.unlock(lease) is True
    assert backend.renew(lease, ttl_s=5.0) is False

    # Expired token: refused, sidecar expiry not extended.
    lease2 = backend.lock(key, ttl_s=0.3)
    time.sleep(0.45)
    assert backend.renew(lease2, ttl_s=5.0) is False
    assert _sidecar(backend, key)[1] == lease2.expiry_epoch


def test_renew_extends_durable_sidecar_expiry_with_same_fence(tmp_path):
    backend = _make_backend(tmp_path, "renew-sidecar")
    key = "fenced"
    lease = backend.lock(key, ttl_s=30.0)

    before = _sidecar(backend, key)
    assert before == (lease.token, lease.expiry_epoch, lease.fence, 0)

    t0 = time.time()
    assert backend.renew(lease, ttl_s=60.0) is True
    after = _sidecar(backend, key)
    assert after is not None
    assert after[0] == lease.token
    assert after[2] == lease.fence  # fence number unchanged by renew
    assert after[3] == before[3]
    assert after[1] > before[1]
    assert after[1] >= t0 + 60.0

    # A second renew keeps moving expiry forward, still on the same fence.
    t1 = time.time()
    assert backend.renew(lease, ttl_s=120.0) is True
    again = _sidecar(backend, key)
    assert again[2] == lease.fence
    assert again[1] >= t1 + 120.0 > after[1]

    # renew() consumed no fence: the next lock() advances by exactly one.
    assert backend.unlock(lease) is True
    lease2 = backend.lock(key, ttl_s=1.0)
    assert lease2.fence == lease.fence + 1


# ---------------------------------------------------------------------------
# The fence sidecar namespace (`.knokeep-fence/`) is INTERNAL: reserved at the
# gate, hidden from list(), absent to read(), and never overwritten when a
# non-sidecar blob already sits at a sidecar path.
# ---------------------------------------------------------------------------


def _tree_paths(backend: GitBackend):
    head = backend._fetch_head()
    proc = backend._run(["ls-tree", "-r", "--name-only", head], check=True)
    return sorted(n for n in proc.stdout.decode("utf-8").splitlines() if n)


def test_fence_sidecars_are_hidden_from_list_and_read(tmp_path):
    backend = _make_backend(tmp_path, "hide-sidecars")
    r0 = gate.persist(backend, "docs/a", b"a", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r0, OK)
    r1 = gate.persist(backend, "docs/a", b"a2", ctx=fenced_ctx(backend, "docs/a", r0.new_hash), doc_type="system_state")
    assert isinstance(r1, OK)
    backend.lock("docs/only-locked", ttl_s=30)  # sidecar with no data key

    sidecar_a = backend._fence_sidecar_path("docs/a")
    sidecar_locked = backend._fence_sidecar_path("docs/only-locked")
    # The sidecars really are committed tree paths...
    assert sidecar_a in _tree_paths(backend) and sidecar_locked in _tree_paths(backend)
    # ...but never logical keys.
    assert list(backend.list("")) == ["docs/a"]
    assert list(backend.list(".knokeep-fence/")) == []
    assert list(backend.list(".")) == []
    assert backend.read(sidecar_a) is None
    assert backend.read(sidecar_locked) is None
    assert backend.read("docs/only-locked") is None


@pytest.mark.parametrize("key", [".knokeep-fence/x", ".knokeep-fence/deadbeef.fence", ".knokeep-fence"])
def test_fence_namespace_is_rejected_by_the_gate(tmp_path, key):
    backend = _make_backend(tmp_path, "reserved")
    r = gate.persist(backend, key, b"user data", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r, ERROR) and r.kind is ErrorKind.INVALID_ARGUMENT
    assert list(backend.list("")) == []
    # Look-alikes that are NOT the reserved segment are still ordinary keys.
    ok = gate.persist(backend, "knokeep-fence/x", b"fine", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(ok, OK)


def test_lock_refuses_to_overwrite_foreign_blob_at_sidecar_path(tmp_path):
    """A blob that already occupies a sidecar path but is not a sidecar
    (written before the namespace was reserved, or by another tool) is user
    data: lock() must refuse rather than commit fence state over it."""
    backend = _make_backend(tmp_path, "foreign-sidecar")
    r0 = gate.persist(backend, "docs/k", b"k", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r0, OK)
    path = backend._fence_sidecar_path("docs/k")
    # Plant the foreign blob through the private commit helpers (the public
    # door now refuses this path), exactly as the secret-in-history test does.
    head = backend._fetch_head()
    commit = backend._build_commit(head, path, b"legacy user data at a reserved path")
    ok, rejected, _ = backend._push(commit)
    assert ok and not rejected

    from store.backend import BackendBusyError

    with pytest.raises(BackendBusyError, match="non-sidecar"):
        backend.lock("docs/k", ttl_s=30)
    assert backend._read_blob_at(backend._fetch_head(), path) == b"legacy user data at a reserved path"
    # Other keys are unaffected.
    lease = backend.lock("docs/other", ttl_s=30)
    backend.unlock(lease)


def test_renew_reports_false_when_durable_sidecar_push_fails(tmp_path, monkeypatch):
    """A renew whose sidecar update never reached the remote must report
    False and leave the local expiry unchanged; once the remote is reachable
    again the same lease renews normally."""
    backend = _make_backend(tmp_path, "renew-push-fails")
    key = "renew/push"
    lease = backend.lock(key, ttl_s=30.0)
    before_local = backend._locks[key]
    before_sidecar = _sidecar(backend, key)

    monkeypatch.setattr(backend, "_push", lambda sha: (False, False, "simulated remote outage"))
    assert backend.renew(lease, ttl_s=300.0) is False
    assert backend._locks[key] == before_local
    monkeypatch.undo()
    assert _sidecar(backend, key) == before_sidecar
    assert backend.renew(lease, ttl_s=300.0) is True
    assert _sidecar(backend, key)[1] > before_sidecar[1] + 200.0


def test_renew_reports_false_when_another_instance_superseded_the_sidecar(tmp_path):
    remote = tmp_path / "remote-shared.git"
    _init_bare(remote)
    a = GitBackend(tmp_path / "work-a", remote)
    b = GitBackend(tmp_path / "work-b", remote)
    key = "renew/shared"
    mine = a.lock(key, ttl_s=30.0)
    theirs = b.lock(key, ttl_s=30.0)
    assert theirs.fence > mine.fence
    assert a.renew(mine, ttl_s=300.0) is False
    assert _sidecar(a, key)[0] == theirs.token


def test_legacy_logical_key_under_fence_prefix_stays_visible(tmp_path):
    """Only canonical `<sha256>.fence` sidecars are internal. A logical key a
    pre-reservation gate accepted under the same prefix must still be
    listed and readable after the upgrade (it can be migrated out; the gate
    just refuses NEW writes there)."""
    backend = _make_backend(tmp_path, "legacy-prefix")
    r0 = gate.persist(backend, "docs/a", b"a", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r0, OK)
    backend.lock("docs/a", ttl_s=30)  # a real sidecar
    head = backend._fetch_head()
    commit = backend._build_commit(head, ".knokeep-fence/notes", b"legacy value")
    ok, rejected, _ = backend._push(commit)
    assert ok and not rejected

    assert list(backend.list("")) == [".knokeep-fence/notes", "docs/a"]
    assert backend.read(".knokeep-fence/notes").body == b"legacy value"
    assert backend.read(backend._fence_sidecar_path("docs/a")) is None
    assert not any(n.endswith(".fence") for n in backend.list(""))
    # Still reserved for new writes.
    r = gate.persist(backend, ".knokeep-fence/notes", b"update", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r, ERROR) and r.kind is ErrorKind.INVALID_ARGUMENT


def test_legacy_user_blob_at_canonical_sidecar_path_stays_visible(tmp_path):
    """Hiding is decided by CONTENT, not path shape: a pre-reservation user
    blob whose key happens to have the canonical `<sha256>.fence` shape
    (but is not a sidecar) is still listed and readable, while a real
    sidecar at another such path is hidden."""
    backend = _make_backend(tmp_path, "legacy-canonical")
    r0 = gate.persist(backend, "docs/a", b"a", ctx=create_ctx(), doc_type="system_state")
    assert isinstance(r0, OK)
    backend.lock("docs/a", ttl_s=30)                                      # real sidecar
    legacy_path = ".knokeep-fence/" + "ab" * 32 + ".fence"                # canonical shape, user content
    head = backend._fetch_head()
    commit = backend._build_commit(head, legacy_path, b"this is not a sidecar, it is user data")
    ok, rejected, _ = backend._push(commit)
    assert ok and not rejected

    assert list(backend.list("")) == [legacy_path, "docs/a"]
    assert backend.read(legacy_path).body == b"this is not a sidecar, it is user data"
    assert backend.read(backend._fence_sidecar_path("docs/a")) is None
