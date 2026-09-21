"""PostgresBackend-specific conformance (contract v1.4 §4.4, §8, §9).

These exercise behavior against the REAL local throwaway PostgreSQL 16
instance (pg8000, trust auth, no password) that the backend-agnostic
conformance/suite.py's "postgres" parametrization already covers generically
(C1/C2 single + threaded races, idempotent create replay, doc-type/
generation allowlist, INVALID_ARGUMENT-before-I/O, pointer-stays-pointer,
lock TTL+token, the ScannedKey/ScannedBody TypeError boundary, and the §7
reconciliation tests -- which are auto-skipped here since PostgresBackend
does not implement the timeout/conflict-unknown fault-injection hooks, see
store/postgres.py's module docstring JUDGMENT CALL 4).

This file adds what is specific to postgres: the explicit 0-row-update
disambiguation (STALE{None} vs STALE{actual_hash}), the load-time capability
probe (§8), and a dedicated, more thorough two-writer race + generation
test written directly against this adapter (rather than relying solely on
the generic suite's 8-way race) so the row-lock-is-the-CAS argument in the
module docstring is verified end to end, including the final stored value.

Every test uses its own fresh, uuid-suffixed schema (via `_backend`) so
parallel test runs and reruns never collide, and each cleans its schema up
via `PostgresBackend._conformance_teardown()` (already covered by the
`backend` fixture in conformance/suite.py; this file supplies its own local
equivalent since it does not use that shared fixture).
"""
from __future__ import annotations

import threading
import types
import uuid

import pytest

pg8000 = pytest.importorskip("pg8000")

from store import gate
from store.backend import BackendBusyError
from store.postgres import PostgresBackend, _extract_generation
from store.types import ERROR, EXISTS, OK, STALE, ErrorKind, sha256_hex

_PG_CONNECT_KWARGS = {
    "host": "127.0.0.1",
    "port": 5433,
    "user": "knokeep",
    "database": "knokeep_test",
}


def _pg_reachable() -> bool:
    try:
        conn = pg8000.connect(timeout=2, **_PG_CONNECT_KWARGS)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason=(
        "real local PostgreSQL 16 (127.0.0.1:5433, trust auth, db "
        "knokeep_test) is not reachable in this environment"
    ),
)


def _gen(n: int) -> bytes:
    return gate.make_generation_header(n)


def test_extract_generation_rejects_oversized_digit_runs():
    oversized = b"#knokeep-gen:" + b"9" * 5000 + b"\nbody"

    assert _extract_generation(_gen(42) + b"body") == 42
    assert _extract_generation(b"#knokeep-gen:" + b"1" * 21 + b"\nbody") is None
    assert _extract_generation(oversized) is None


@pytest.fixture
def backend():
    schema = "knokeep_t_" + uuid.uuid4().hex[:20]
    b = PostgresBackend(
        dict(_PG_CONNECT_KWARGS),
        schema=schema,
        # Short, bounded timeouts (task requirement: "a test can never
        # hang"). 3s is generous for local loopback Postgres but still
        # bounded.
        statement_timeout_ms=3000,
        lock_timeout_ms=3000,
    )
    yield b
    b._conformance_teardown()


# ---------------------------------------------------------------------------
# Sanity: this really is the real Postgres, not a mock/fake.
# ---------------------------------------------------------------------------


def test_this_is_a_real_postgres_server(backend):
    conn = backend._get_conn()
    cur = conn.cursor()
    cur.execute("SELECT version()")
    (version_string,) = cur.fetchone()
    conn.commit()
    assert "PostgreSQL" in version_string

    cur.execute("SHOW transaction_isolation")
    (isolation,) = cur.fetchone()
    conn.commit()
    assert isolation.lower() == "read committed"


def test_schema_and_table_created_with_contract_columns(backend):
    conn = backend._get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (backend._schema, backend._table),
    )
    cols = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
    conn.commit()
    assert cols["key"][0] == "text" and cols["key"][1] == "NO"
    assert cols["body"][0] == "bytea" and cols["body"][1] == "NO"
    assert cols["version_hash"][0] == "text" and cols["version_hash"][1] == "NO"
    assert cols["generation"][0] == "bigint" and cols["generation"][1] == "YES"
    assert cols["updated_at"][0] == "timestamp with time zone" and cols["updated_at"][1] == "NO"


# ---------------------------------------------------------------------------
# C1 — stale-reject, single + a REAL threaded two-writer race
# ---------------------------------------------------------------------------


def test_c1_stale_reject_single(backend):
    r0 = gate.persist(backend, "k1", b"hello", expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK)

    r1 = gate.persist(
        backend, "k1", b"goodbye", expected_hash="0" * 64, doc_type="system_state"
    )
    assert isinstance(r1, STALE)
    assert r1.current_hash == r0.new_hash
    assert backend.read("k1").body == b"hello"


def test_c1_two_writer_threaded_race_exactly_one_ok(backend):
    """Two threads, each with its OWN pg8000 connection (per the adapter's
    thread-local connection model), race a CAS-update against the SAME
    expected_hash on the SAME row at the same real Postgres instance.
    Exactly one must observe OK, the other STALE, and the row must end up
    holding exactly the winner's bytes -- proving Postgres's own row lock
    (not any Python-level mutex) is the actual linearization point."""
    r0 = gate.persist(backend, "race-2writer", b"base", expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK)
    base_hash = r0.new_hash

    results = [None, None]
    barrier = threading.Barrier(2)

    def worker(i, body):
        barrier.wait()
        results[i] = gate.persist(
            backend, "race-2writer", body, expected_hash=base_hash, doc_type="system_state"
        )

    t0 = threading.Thread(target=worker, args=(0, b"writer-A"))
    t1 = threading.Thread(target=worker, args=(1, b"writer-B"))
    t0.start()
    t1.start()
    t0.join(timeout=10)
    t1.join(timeout=10)
    assert not t0.is_alive() and not t1.is_alive(), "race must never hang"

    oks = [r for r in results if isinstance(r, OK)]
    stales = [r for r in results if isinstance(r, STALE)]
    assert len(oks) == 1, f"expected exactly one OK, got {results}"
    assert len(stales) == 1, f"expected exactly one STALE, got {results}"
    assert stales[0].current_hash == oks[0].new_hash

    winner_index = results.index(oks[0])
    winner_body = b"writer-A" if winner_index == 0 else b"writer-B"
    final = backend.read("race-2writer")
    assert final.body == winner_body
    assert final.version_hash == oks[0].new_hash


def test_c1_eight_writer_threaded_race_exactly_one_ok(backend):
    """Wider race (8 threads / 8 real connections) for extra confidence
    beyond the generic suite's own 8-way race, kept here so this file is a
    self-contained postgres conformance record."""
    r0 = gate.persist(backend, "race-8writer", b"base", expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK)
    base_hash = r0.new_hash

    n = 8
    barrier = threading.Barrier(n)
    results = [None] * n
    bodies = [f"writer-{i}".encode() for i in range(n)]

    def worker(i):
        barrier.wait()
        results[i] = gate.persist(
            backend, "race-8writer", bodies[i], expected_hash=base_hash, doc_type="system_state"
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert all(not t.is_alive() for t in threads), "race must never hang"

    oks = [r for r in results if isinstance(r, OK)]
    stales = [r for r in results if isinstance(r, STALE)]
    assert len(oks) == 1, f"expected exactly one OK, got {results}"
    assert len(stales) == n - 1
    winner_index = results.index(oks[0])
    final = backend.read("race-8writer")
    assert final.body == bodies[winner_index]
    for s in stales:
        assert s.current_hash == oks[0].new_hash


# ---------------------------------------------------------------------------
# C2 — create-only collision, single + race
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
    bodies, each from its own real connection/thread. Exactly one gets OK
    (a real `INSERT ... ON CONFLICT (key) DO NOTHING` winner); the rest get
    EXISTS."""
    n = 8
    barrier = threading.Barrier(n)
    results = [None] * n
    bodies = [f"creator-{i}".encode() for i in range(n)]

    def worker(i):
        barrier.wait()
        results[i] = gate.persist(
            backend, "race2-create", bodies[i], expected_hash=None, doc_type="system_state"
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert all(not t.is_alive() for t in threads), "race must never hang"

    oks = [r for r in results if isinstance(r, OK)]
    exists = [r for r in results if isinstance(r, EXISTS)]
    assert len(oks) == 1, f"expected exactly one OK, got {results}"
    assert len(exists) == n - 1
    for e in exists:
        assert e.current_hash == oks[0].new_hash


# ---------------------------------------------------------------------------
# 0-row-update disambiguation (contract §4.4, this adapter's load-bearing
# distinction): STALE{None} for a missing key vs STALE{actual_hash} for a
# hash mismatch on an existing key. Exercised directly against write() (with
# gate-issued values, via a capture backend) so both branches of the
# follow-up SELECT are pinned down explicitly, not just incidentally by C1.
# ---------------------------------------------------------------------------


class _CaptureBackend:
    """Stand-in `backend` for gate.persist() that just captures the
    gate-issued ScannedKey/ScannedBody instead of doing I/O -- mirrors the
    helper of the same name in tests/test_local_backend.py /
    tests/test_git_backend.py."""

    def __init__(self) -> None:
        self.captured = None

    def write(self, key, body, *, expected_hash):
        self.captured = (key, body)
        return OK("0" * 64)


def _issue(key: str, body: bytes, *, expected_hash, doc_type="system_state"):
    capture = _CaptureBackend()
    r = gate.persist(capture, key, body, expected_hash=expected_hash, doc_type=doc_type)
    assert isinstance(r, OK)
    return capture.captured


def test_zero_row_update_missing_key_is_stale_none(backend):
    key_obj, body_obj = _issue("does/not/exist", b"x", expected_hash="a" * 64)
    result = backend.write(key_obj, body_obj, expected_hash="a" * 64)
    assert isinstance(result, STALE)
    assert result.current_hash is None


def test_zero_row_update_hash_mismatch_is_stale_actual_hash(backend):
    r0 = gate.persist(backend, "mismatch/key", b"v0", expected_hash=None, doc_type="system_state")
    assert isinstance(r0, OK)

    key_obj, body_obj = _issue("mismatch/key", b"v1-wrong-base", expected_hash="f" * 64)
    result = backend.write(key_obj, body_obj, expected_hash="f" * 64)
    assert isinstance(result, STALE)
    assert result.current_hash == r0.new_hash  # the TRUE current hash, not None
    assert backend.read("mismatch/key").body == b"v0"  # untouched


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

    conn = backend._get_conn()
    cur = conn.cursor()
    cur.execute(
        f"SELECT count(*) FROM {backend._qualified_table} WHERE key = %s", ("replay",)
    )
    (count,) = cur.fetchone()
    conn.commit()
    assert count == 1, "idempotent replay must not create a second row"


# ---------------------------------------------------------------------------
# Generation monotonicity (contract §6 + §4.4 JUDGMENT CALL 1): enforced
# atomically inside the UPDATE's WHERE clause, not via a separate read+lock.
# ---------------------------------------------------------------------------


def test_generation_monotonicity_rejects_non_increasing_update(backend):
    key = "lease/holder"
    r0 = gate.persist(backend, key, _gen(1) + b"leaseholder=alice", expected_hash=None, doc_type="lease")
    assert isinstance(r0, OK)

    r_same_gen = gate.persist(
        backend, key, _gen(1) + b"leaseholder=bob", expected_hash=r0.new_hash, doc_type="lease"
    )
    assert isinstance(r_same_gen, STALE)
    assert r_same_gen.current_hash == r0.new_hash

    r_lower_gen = gate.persist(
        backend, key, _gen(0) + b"leaseholder=bob", expected_hash=r0.new_hash, doc_type="lease"
    )
    assert isinstance(r_lower_gen, STALE)

    assert backend.read(key).body == _gen(1) + b"leaseholder=alice"

    r_higher_gen = gate.persist(
        backend, key, _gen(2) + b"leaseholder=bob", expected_hash=r0.new_hash, doc_type="lease"
    )
    assert isinstance(r_higher_gen, OK)
    assert backend.read(key).body == _gen(2) + b"leaseholder=bob"

    conn = backend._get_conn()
    cur = conn.cursor()
    cur.execute(
        f"SELECT generation FROM {backend._qualified_table} WHERE key = %s", (key,)
    )
    (stored_generation,) = cur.fetchone()
    conn.commit()
    assert stored_generation == 2


def test_generation_monotonicity_race_only_one_writer_advances(backend):
    """Two threads race a CAS-update to the SAME lease key with the SAME
    expected_hash but DIFFERENT (both valid, higher) generations. Exactly
    one write must land; postgres's row lock plus the generation guard in
    the WHERE clause must never let both succeed nor let a later loser
    silently regress the stored generation."""
    key = "lease/race"
    r0 = gate.persist(backend, key, _gen(1) + b"init", expected_hash=None, doc_type="lease")
    assert isinstance(r0, OK)
    base_hash = r0.new_hash

    results = [None, None]
    barrier = threading.Barrier(2)

    def worker(i, gen_n, payload):
        barrier.wait()
        results[i] = gate.persist(
            backend, key, _gen(gen_n) + payload, expected_hash=base_hash, doc_type="lease"
        )

    t0 = threading.Thread(target=worker, args=(0, 2, b"from-thread-0"))
    t1 = threading.Thread(target=worker, args=(1, 3, b"from-thread-1"))
    t0.start()
    t1.start()
    t0.join(timeout=10)
    t1.join(timeout=10)

    oks = [r for r in results if isinstance(r, OK)]
    stales = [r for r in results if isinstance(r, STALE)]
    assert len(oks) == 1, f"expected exactly one OK, got {results}"
    assert len(stales) == 1

    final = backend.read(key)
    assert final.version_hash == oks[0].new_hash


# ---------------------------------------------------------------------------
# Adapter accepts only ScannedBody/ScannedKey -- raw bytes -> TypeError
# before any I/O.
# ---------------------------------------------------------------------------


def test_write_rejects_raw_bytes_before_any_io(backend):
    with pytest.raises(TypeError):
        backend.write("plain-str-key", b"plain-bytes-body", expected_hash=None)  # type: ignore[arg-type]
    assert backend.read("plain-str-key") is None
    assert list(backend.list("")) == []


def test_scanned_body_is_immutable_after_construction(backend):
    """Hardening: ScannedKey/ScannedBody are now frozen after construction —
    a would-be tamper (rewriting `_body` post-issuance) must raise TypeError
    immediately rather than silently succeeding."""
    key_obj, body_obj = _issue("k", b"hello", expected_hash=None)

    with pytest.raises(TypeError):
        body_obj._body = b"tampered-after-the-fact"
    with pytest.raises(TypeError):
        key_obj._key = "tampered"

    # Untampered, the legitimately-issued pair still writes fine.
    result = backend.write(key_obj, body_obj, expected_hash=None)
    assert isinstance(result, OK)
    assert backend.read("k").body == b"hello"


def test_write_rejects_forged_non_gate_object(backend):
    """The adapter must accept ONLY genuine gate-issued ScannedKey/ScannedBody
    instances — never a look-alike stand-in with matching attribute names
    but no valid gate marker (checked before any I/O, contract §1/§5)."""
    forged_key = types.SimpleNamespace(key="forged-key")
    forged_body = types.SimpleNamespace(body=b"hello")

    with pytest.raises(TypeError):
        backend.write(forged_key, forged_body, expected_hash=None)
    assert backend.read("forged-key") is None
    assert gate.verify(forged_key) is False
    assert gate.verify(forged_body) is False


# ---------------------------------------------------------------------------
# Load-time capability probe (contract §8): construction itself IS the test
# for the happy path (PostgresBackend.__init__ already ran it and didn't
# raise, or this fixture would never have yielded a backend at all). This
# test additionally confirms the probe's own reserved-prefix rows are
# actually cleaned up, and that a backend wired to reject a normally-passing
# CAS-update refuses to start.
# ---------------------------------------------------------------------------


def test_capability_probe_leaves_no_residue_under_reserved_prefix(backend):
    assert list(backend.list(".knokeep/probe/")) == []


def test_capability_probe_refuses_to_start_on_broken_cas_mapping(monkeypatch):
    """Simulate 'wrong mapping or missing native precondition' (contract §8)
    by making the CAS-update UPDATE always report success (rowcount forced
    to 1) regardless of whether it actually matched -- the probe's stale-CAS
    check must then fail and construction must raise, never silently start
    in a degraded mode."""
    import store.postgres as postgres_module

    class _AlwaysMatchingCursor:
        """Wraps a real pg8000 cursor; every UPDATE against the store table
        is forced to report rowcount=1 (as if the WHERE clause always
        matched), while every other statement passes through untouched."""

        def __init__(self, real_cursor):
            self._real = real_cursor
            self.rowcount = 0

        def execute(self, sql, params=None):
            result = self._real.execute(sql, params) if params is not None else self._real.execute(sql)
            if sql.strip().upper().startswith("UPDATE"):
                self.rowcount = 1
            else:
                self.rowcount = self._real.rowcount
            return result

        def fetchone(self):
            return self._real.fetchone()

        def fetchall(self):
            return self._real.fetchall()

    schema = "knokeep_broken_" + uuid.uuid4().hex[:20]
    backend = PostgresBackend(dict(_PG_CONNECT_KWARGS), schema=schema, run_probe=False)
    try:
        real_conn = backend._get_conn()
        real_cursor_factory = real_conn.cursor

        def patched_cursor():
            return _AlwaysMatchingCursor(real_cursor_factory())

        monkeypatch.setattr(real_conn, "cursor", patched_cursor)

        with pytest.raises(RuntimeError, match="REFUSING TO START"):
            backend._run_capability_probe()
    finally:
        backend._conformance_teardown()
