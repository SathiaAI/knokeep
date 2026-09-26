"""PostgresBackend — PostgreSQL StoreBackend adapter (contract v1.4, §3, §4.4).

**Adapter ONLY** — no service/auth/multi-tenant layer. This module owns
exactly the schema and CAS statements the contract mandates for postgres; it
does not manage credentials, connection pooling policy, tenancy, or anything
above the StoreBackend socket. Callers are expected to hand this adapter
connect parameters for a database it may write a single `store` table into
(or, for test isolation, a dedicated schema of its own).

Driver: **pg8000**, the pure-Python driver — the contract's sanctioned
optional extra for this backend (stdlib-first everywhere else in the
codebase; this is the one adapter allowed a third-party dependency, per
contract §4.2's "boto3/gcs optional extras under the same rules" precedent
extended here to pg8000).

Schema (contract §4.4, verbatim):

    CREATE TABLE store (
        key text PRIMARY KEY,
        body bytea NOT NULL,
        version_hash text NOT NULL,
        generation bigint,
        updated_at timestamptz NOT NULL DEFAULT now()
    )

Connection isolation level: **READ COMMITTED**, set explicitly per session
(`SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL READ
COMMITTED`) rather than relying on whatever the server's default happens to
be. All SQL is parameterized via pg8000's `%s` ("format") paramstyle — key,
body, hash and generation values are NEVER string-interpolated into a query.
The only string-formatted pieces of SQL in this module are the schema/table
*identifiers* themselves (validated against a strict identifier allowlist at
construction, see `_validate_identifier` — Postgres has no parameter-binding
syntax for identifiers, and these come from adapter configuration, not from
gate-scanned key/body content, the same way `LocalBackend`'s `root` path or
`GitBackend`'s `work_dir`/`remote` are configuration, not gated payload).

Concurrency / the linearization point (contract §3: "native precondition ...
is the linearization point and is mandatory on every write"): this adapter
does **no** application-level mutex around its CAS logic. The single
parameterized `UPDATE ... WHERE key = %s AND version_hash = %s [AND
<generation guard>]` statement (or `INSERT ... ON CONFLICT (key) DO NOTHING`
for create-only) IS the CAS — Postgres's own row-level locking on the target
row serializes concurrent writers, and under READ COMMITTED a writer that
was blocked on that row lock re-evaluates its WHERE clause against the row
as the winner committed it once the lock is released, so a loser's UPDATE
naturally affects 0 rows. This holds regardless of how many separate
connections converge on the same key.

pg8000 connections are, however, **not** safe for concurrent use by multiple
threads (the wire protocol is stateful; interleaved use from two threads
would corrupt the session). Each calling thread gets its own lazily-created
connection (`threading.local()`), so N threads racing `write()` on the same
`PostgresBackend` instance (exactly what conformance/suite.py's threaded
races and this module's own `tests/test_postgres.py` C1 race do) genuinely
hit the database as N separate sessions, and the row-lock argument above is
what actually resolves the race — not any Python-level lock.

JUDGMENT CALLS (numbered, also called out inline at point of use):

  1. **Generation monotonicity is enforced inside the same UPDATE statement**
     as the hash CAS (`AND (generation IS NULL OR generation < %s)`), not as
     a separate read-then-write step under an application lock the way
     `LocalBackend`/`GitBackend` do it (their "residual from T1" judgment
     call). Postgres lets this be one atomic statement instead, which is
     strictly stronger: there is no window between checking the stored
     generation and applying the write. The check is applied whenever the
     new body carries a `#knokeep-gen:<uint64>` header (i.e. `new_generation
     is not None`), regardless of `doc_type` — the adapter is never told
     `doc_type` (it is not part of `StoreBackend.write()`'s signature), so,
     identically to `local.py`/`git_backend.py`, it keys off header
     *presence* rather than the doc-type allowlist name. A body with no
     generation header (the STATE/snapshot allowlist path) applies no
     generation guard, matching contract §6 ("content-hash CAS only for the
     named STATE/snapshot allowlist... where identical bytes = identical
     state").

  2. **0-row-update disambiguation is a single follow-up `SELECT
     version_hash WHERE key = %s`, in the SAME transaction**, exactly as
     contract §4.4 specifies: absent -> `STALE(None)`; present ->
     `STALE(current_hash)`. This is used for BOTH ways an UPDATE can affect
     0 rows — a genuine hash mismatch, or a hash match with a non-increasing
     generation (judgment call 1's guard firing) — because in the latter
     case the follow-up SELECT naturally returns the *unchanged* current
     hash (which still equals `expected_hash`), so the caller sees
     `STALE(expected_hash)`: "your write didn't apply," which is exactly
     what happened. The contract's §4.4 prose only spells this out for the
     hash-mismatch case, but the same statement and the same follow-up query
     serve both, and returning anything other than STALE for a rejected
     generation-regression update would be inconsistent with how
     `LocalBackend`/`GitBackend` already report that exact scenario
     (`STALE(current_hash)`, contract residual-generation judgment call).

  3. **Lock/statement timeouts, not an application mutex, bound contention.**
     Every connection sets `statement_timeout` and `lock_timeout` (both
     configurable, short by default) immediately after connecting. A writer
     that cannot acquire the target row's lock within `lock_timeout` gets
     Postgres error `55P03 lock_not_available` (or a `57014
     query_canceled` if the whole statement instead ran past
     `statement_timeout`); both are caught and reported as `ERROR{BUSY}`
     after a `ROLLBACK` — contract §2/§3's "never block indefinitely" and
     the existing `ErrorKind.BUSY` ("lock-acquire timeout... ") reading
     extended from file-locks to a row-lock wait, which is the same kind of
     event for a backend whose CAS mechanism *is* a native row lock. Neither
     case can have committed (the statement was cancelled and the
     transaction is aborted), so `BUSY` (definitely-not-committed) is
     correct, never an outcome-unknown kind.

  4. **A connection-level failure is classified by IS the transaction still
     open.** If the dropped/broken connection is caught while no `COMMIT`
     has yet been sent for this write's transaction, the write cannot have
     been acknowledged, so it is reported as `ERROR{NETWORK}`
     (definitely-not-committed, matching contract §1: "NETWORK... before the
     request was fully sent"/"before ack"). This adapter does not currently
     implement the `inject_timeout_after_commit`/`inject_conflict_unknown`
     fault-injection hooks `FakeBackend`/`GitBackend` expose for exercising
     contract §7's uncertain-outcome reconciliation against a *simulated*
     lost ack — `conformance/suite.py`'s `_require_fault_injection` already
     skips those tests for any backend lacking the hooks, and the task
     driving this adapter's tests does not ask for them. A real deployment
     talking to a real network would need that fault surface; it is called
     out here as an explicit, documented gap rather than silently absent.

  5. **`capabilities().durable = True`** — contract §3: "remote backends
     derive durability from their server ACK." A `COMMIT` that returns
     successfully from `pg8000` has been fsync'd by the Postgres server (the
     default `synchronous_commit` behavior) before the ACK is sent back over
     the wire; this adapter never uses `synchronous_commit = off` or any
     other setting that would weaken that guarantee, so `OK` here really
     does mean durably committed, matching contract §3's definition of `OK`.

  6. **`lock()`/`unlock()`/`renew()` are in-memory, per-process** (a dict
     guarded by a `threading.Lock`) — identical shape to `GitBackend`'s
     advisory lock and explicitly sanctioned by the task ("may be in-memory
     per-process like the other adapters"). Contract §3 is explicit that
     "Advisory lock() is never the CAS mechanism" for any backend; the WHERE
     `version_hash = ...` predicate on the UPDATE is the actual CAS
     mechanism here, exactly as the task states.

  7. **Table/schema identifiers are validated, then string-formatted into
     DDL/DML**, since Postgres has no bind-parameter syntax for identifiers.
     `_validate_identifier` enforces a conservative allowlist
     (`^[A-Za-z_][A-Za-z0-9_]{0,62}$`) before any such string ever reaches a
     query, so this is not "string interpolation of values" in the sense
     contract §4.4 forbids (that sentence governs key/body/hash/generation
     *content*, which is 100% parameterized below) — it is the same class of
     adapter-configuration trust already extended to `LocalBackend.root` and
     `GitBackend.work_dir`/`remote`.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid
from typing import Dict, Iterator, List, Optional, Tuple

import pg8000
import pg8000.exceptions

from . import gate
from .backend import BackendBusyError, Lock
from .context import Overwrite, create_ctx, overwrite_ctx
from .gate import ScannedBody, ScannedKey
from .types import (
    BackendHealth,
    Blob,
    Caps,
    EXISTS,
    ERROR,
    ErrorKind,
    OK,
    STALE,
    WriteResult,
    sha256_hex,
)

# ---------------------------------------------------------------------------
# Generation header (contract §6; same convention/regex as store/local.py and
# store/git_backend.py — re-implemented locally rather than importing gate's
# private helper, same rationale as those two adapters: the adapter layer
# does not reach into store/gate.py's internals beyond the public API.)
# ---------------------------------------------------------------------------

_GEN_HEADER_RE = re.compile(rb"^#knokeep-gen:(0|[1-9][0-9]{0,19})\n")
_UINT64_MAX = (1 << 64) - 1


def _extract_generation(raw: bytes) -> Optional[int]:
    m = _GEN_HEADER_RE.match(raw)
    if not m:
        return None
    value = int(m.group(1))
    if value > _UINT64_MAX:
        return None
    return value


# ---------------------------------------------------------------------------
# Identifier validation (JUDGMENT CALL 7) — schema/table names only, never
# key/body/hash content, which is always sent as a bound parameter below.
# ---------------------------------------------------------------------------

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _validate_identifier(name: str, what: str) -> str:
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise ValueError(
            f"PostgresBackend: invalid {what} identifier {name!r} "
            "(must match ^[A-Za-z_][A-Za-z0-9_]{0,62}$)"
        )
    return name


_FENCE_TABLE_SUFFIX = "_fence"
_PG_IDENTIFIER_MAX = 63


def _fence_table_name(table: str) -> str:
    """The companion fence table's identifier, derived from the (already
    validated) store table name WITHOUT narrowing the accepted range: any
    table name that was valid before the fence table existed (up to 63
    chars) must still construct. When `<table>_fence` would exceed
    Postgres's 63-char identifier limit, the table name is truncated and
    disambiguated with a stable 8-hex digest of the full name, so two
    long names that share a prefix never share a fence table."""
    candidate = f"{table}{_FENCE_TABLE_SUFFIX}"
    if len(candidate) <= _PG_IDENTIFIER_MAX:
        return candidate
    digest = hashlib.sha256(table.encode("utf-8")).hexdigest()[:8]
    keep = _PG_IDENTIFIER_MAX - len(_FENCE_TABLE_SUFFIX) - 1 - len(digest)
    return _validate_identifier(f"{table[:keep]}_{digest}{_FENCE_TABLE_SUFFIX}", "fence table")


# Postgres SQLSTATEs that mean "couldn't get the row lock / ran past our own
# timeout" — definitely-not-committed, never an outcome-unknown kind
# (JUDGMENT CALL 3).
# 40P01 (deadlock_detected) is included defensively: Postgres aborts one of
# the two transactions, so it is definitely-not-committed and retryable --
# though write() takes its row locks in one fixed order (store row, then
# fence row) on every path precisely so it never deadlocks with itself.
_BUSY_SQLSTATES = frozenset({"55P03", "57014", "40P01"})  # lock_not_available, query_canceled, deadlock_detected


def _sqlstate(exc: BaseException) -> Optional[str]:
    if exc.args and isinstance(exc.args[0], dict):
        return exc.args[0].get("C")
    return None


class PostgresBackend:
    """PostgreSQL-backed StoreBackend adapter. See module docstring."""

    def __init__(
        self,
        connect_kwargs: Dict,
        *,
        table: str = "store",
        schema: str = "public",
        statement_timeout_ms: int = 5000,
        lock_timeout_ms: int = 5000,
        create_schema: bool = True,
        run_probe: bool = True,
    ) -> None:
        self._connect_kwargs = dict(connect_kwargs)
        self._table = _validate_identifier(table, "table")
        self._schema = _validate_identifier(schema, "schema")
        self._statement_timeout_ms = int(statement_timeout_ms)
        self._lock_timeout_ms = int(lock_timeout_ms)
        self._qualified_table = f'"{self._schema}"."{self._table}"'
        # H1 Increment 2, Phase 2: durable, cross-process fence-ownership
        # state lives in a companion table, kept SEPARATE from the main
        # `store` table (mirrors store/local.py's locks/fence-alloc/ being a
        # separate directory from data/) rather than extra columns on
        # `store` itself -- `store.body`/`store.version_hash` are NOT NULL
        # (contract §4.4's schema, verified by
        # tests/test_postgres.py::test_schema_and_table_created_with_contract_columns),
        # but lock() must work on a key that has never been written yet
        # (conformance/suite.py's C7 calls backend.lock() before any write)
        # -- a companion table with no such constraint is the only way to
        # satisfy both without weakening that existing test.
        self._fence_table = _fence_table_name(self._table)
        self._qualified_fence_table = f'"{self._schema}"."{self._fence_table}"'

        self._local = threading.local()
        self._conn_registry_lock = threading.Lock()
        self._all_connections: List["pg8000.Connection"] = []

        # -- advisory lock() bookkeeping (JUDGMENT CALL 6: in-memory only) --
        self._lock_mutex = threading.Lock()
        self._locks: Dict[str, Tuple[str, float]] = {}

        conn = self._get_conn()
        cur = conn.cursor()
        if create_schema:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._qualified_table} (
                key text PRIMARY KEY,
                body bytea NOT NULL,
                version_hash text NOT NULL,
                generation bigint,
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        # H1 Increment 2, Phase 2: companion fence-ownership table (see the
        # comment on self._fence_table above for why this is separate from
        # {self._qualified_table} rather than extra columns on it).
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._qualified_fence_table} (
                key text PRIMARY KEY,
                owner_token text,
                owner_expiry double precision,
                owner_fence bigint NOT NULL DEFAULT 0,
                last_accepted_fence bigint NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()
        # CREATE TABLE IF NOT EXISTS never checks an EXISTING relation's
        # layout: two stores in one schema configured as e.g. `foo` and
        # `foo_fence` would otherwise silently share a table (one's data
        # table is the other's fence table) and fail later on a missing
        # column. Refuse to start instead (contract §8 spirit: no degraded
        # mode) when either relation is not what this adapter created.
        self._verify_columns(
            cur, self._table,
            {"key", "body", "version_hash", "generation", "updated_at"}, "store",
        )
        self._verify_columns(
            cur, self._fence_table,
            {"key", "owner_token", "owner_expiry", "owner_fence", "last_accepted_fence"}, "fence",
        )
        conn.commit()

        # Contract §8: load-time capability probe against a reserved prefix,
        # cleaned up afterwards. "Wrong mapping or missing native
        # precondition -> REFUSE TO START. No degraded mode."
        if run_probe:
            self._run_capability_probe()

    def _verify_columns(self, cur, table: str, expected: set, what: str) -> None:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            """,
            (self._schema, table),
        )
        present = {row[0] for row in cur.fetchall()}
        missing = expected - present
        if missing:
            raise RuntimeError(
                f"PostgresBackend: {what} table \"{self._schema}\".\"{table}\" already exists "
                f"with an incompatible layout (missing columns: {sorted(missing)}); it is not "
                "a relation this adapter created -- another store's table probably collides "
                "with this name. REFUSING TO START."
            )

    # -- connection management ----------------------------------------------

    def _new_connection(self):
        conn = pg8000.connect(**self._connect_kwargs)
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute(
                "SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL READ COMMITTED"
            )
            cur.execute(f"SET statement_timeout = {self._statement_timeout_ms}")
            cur.execute(f"SET lock_timeout = {self._lock_timeout_ms}")
        finally:
            conn.autocommit = False
        with self._conn_registry_lock:
            self._all_connections.append(conn)
        return conn

    def _get_conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    def _reset_conn(self) -> None:
        """Drop this thread's connection so the next call reconnects fresh
        (used after a connection-level failure we cannot trust anymore)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def close_all(self) -> None:
        """Test/ops convenience — not part of the StoreBackend protocol.
        Closes every connection this adapter has opened across all threads
        that have used it."""
        with self._conn_registry_lock:
            conns = list(self._all_connections)
            self._all_connections.clear()
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        self._local.conn = None

    def _conformance_teardown(self) -> None:
        """Test/ops convenience — not part of the StoreBackend protocol.
        Used by conformance/suite.py's `backend` fixture to drop this
        instance's per-test schema (it was created fresh, per call, by
        `_make_postgres`) and close every connection it opened, so
        parametrized/racing test runs never accumulate schemas or leaked
        sockets against the real database."""
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')
            conn.commit()
        except Exception:
            pass
        self.close_all()

    # -- load-time capability probe (contract §8) ----------------------------

    def _run_capability_probe(self) -> None:
        probe_key = f".knokeep/probe/{uuid.uuid4().hex}"
        try:
            r1 = gate.persist(
                self, probe_key, b"probe-create", ctx=create_ctx(), doc_type="system_state"
            )
            if not isinstance(r1, OK):
                raise RuntimeError(f"probe create-only did not return OK: {r1!r}")

            r2 = gate.persist(
                self, probe_key, b"probe-duplicate", ctx=create_ctx(), doc_type="system_state"
            )
            if not isinstance(r2, EXISTS):
                raise RuntimeError(f"probe duplicate create-only did not return EXISTS: {r2!r}")

            # Phase 0: ctx requires a real (advisory) lease for an Overwrite
            # precondition; acquired-then-released immediately since fence
            # enforcement is a later phase and this probe must not hold a
            # lock past its own call.
            probe_lease = self.lock(probe_key, ttl_s=30)
            self.unlock(probe_lease)
            r3 = gate.persist(
                self,
                probe_key,
                b"probe-stale-cas",
                ctx=overwrite_ctx("0" * 64, probe_lease),
                doc_type="system_state",
            )
            if not isinstance(r3, STALE):
                raise RuntimeError(f"probe stale CAS-update did not return STALE: {r3!r}")
        except Exception as exc:
            raise RuntimeError(
                "PostgresBackend: load-time capability probe (contract §8) failed "
                f"-- wrong CAS mapping or missing native precondition, REFUSING TO START: {exc}"
            ) from exc
        finally:
            self._probe_cleanup(probe_key)

    def _probe_cleanup(self, probe_key: str) -> None:
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {self._qualified_table} WHERE key = %s", (probe_key,))
        cur.execute(f"DELETE FROM {self._qualified_fence_table} WHERE key = %s", (probe_key,))
        conn.commit()

    # -- StoreBackend protocol -----------------------------------------------

    def capabilities(self) -> Caps:
        # H1 Increment 2, Phase 2: this backend now enforces lock()-issued
        # fence ordering on every Overwrite CAS-update (see write() and the
        # companion self._qualified_fence_table).
        return Caps(atomic=True, cas=True, lock=True, durable=True, remote=True, fence=True)

    def health(self) -> BackendHealth:
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            conn.commit()
            # Identity = connection + relation (never credentials): two
            # distinct servers/databases with the same schema.table must not
            # report the same detail (migrate() compares adapter identities).
            ck = self._connect_kwargs
            return BackendHealth(
                ok=True,
                detail=(f"postgres backend host={ck.get('host')} port={ck.get('port')} "
                        f"database={ck.get('database')} table={self._qualified_table}"),
            )
        except Exception as exc:
            return BackendHealth(ok=False, detail=str(exc))

    def read(self, key: str) -> Optional[Blob]:
        # PERMISSION/NETWORK/CORRUPTION RAISE per contract §2 — nothing here
        # is caught and turned into a return value.
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            f"SELECT body, version_hash FROM {self._qualified_table} WHERE key = %s",
            (key,),
        )
        row = cur.fetchone()
        conn.commit()
        if row is None:
            return None
        body, version_hash = row
        return Blob(body=bytes(body), version_hash=version_hash)

    def list(self, prefix: str) -> Iterator[str]:
        # Strong consistency: a single SELECT against one committed snapshot
        # at call time (Postgres READ COMMITTED) — not eventually consistent.
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            f"SELECT key FROM {self._qualified_table} "
            "WHERE left(key, %s) = %s ORDER BY key",
            (len(prefix), prefix),
        )
        rows = cur.fetchall()
        conn.commit()
        return iter(row[0] for row in rows)

    def write(
        self,
        key: ScannedKey,
        body: ScannedBody,
        *,
        ctx,
    ) -> WriteResult:
        # Adapter accepts only gate-issued values; raw bytes/str are a
        # TypeError before any I/O (contract §1/§5).
        if not isinstance(key, ScannedKey) or not isinstance(body, ScannedBody):
            raise TypeError(
                "PostgresBackend.write requires ScannedKey/ScannedBody from store.gate.persist()"
            )
        if not gate.verify(key) or not gate.verify(body):
            raise TypeError("PostgresBackend.write: gate marker verification failed")

        # H1 Increment 2, Phase 2: ctx is required; derive expected_hash the
        # same way store/gate.py does, plus the caller's lease for fence
        # enforcement (Phase 0/1 only carried it structurally).
        precondition_lease: Optional[Lock] = (
            ctx.precondition.lease if isinstance(ctx.precondition, Overwrite) else None
        )
        expected_hash: Optional[str] = (
            ctx.precondition.expected_hash if isinstance(ctx.precondition, Overwrite) else None
        )

        k = key.key
        raw = body.body
        new_hash = sha256_hex(raw)
        new_generation = _extract_generation(raw)  # JUDGMENT CALL 1

        try:
            conn = self._get_conn()
        except (pg8000.exceptions.InterfaceError, OSError):
            # Couldn't even open/reuse a connection -> nothing was sent.
            return ERROR(ErrorKind.NETWORK)

        try:
            cur = conn.cursor()

            if expected_hash is None:
                # --- Create-only: INSERT ... ON CONFLICT (key) DO NOTHING ---
                cur.execute(
                    f"""
                    INSERT INTO {self._qualified_table}
                        (key, body, version_hash, generation)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (key) DO NOTHING
                    """,
                    (k, raw, new_hash, new_generation),
                )
                if cur.rowcount == 1:
                    # H1 Increment 2, Phase 2: a fresh create starts the
                    # key's fence floor at 0, regardless of any earlier
                    # lock() activity on this (previously nonexistent) key
                    # -- mirrors store/local.py's
                    # _commit(..., last_accepted_fence=0) on create.
                    cur.execute(
                        f"""
                        INSERT INTO {self._qualified_fence_table} (key, last_accepted_fence)
                        VALUES (%s, 0)
                        ON CONFLICT (key) DO UPDATE SET last_accepted_fence = 0
                        """,
                        (k,),
                    )
                    conn.commit()
                    return OK(new_hash)

                # 0 rows inserted -> the key already exists.
                cur.execute(
                    f"SELECT version_hash FROM {self._qualified_table} WHERE key = %s",
                    (k,),
                )
                row = cur.fetchone()
                conn.commit()
                if row is None:
                    # This adapter never deletes rows itself; a missing row
                    # here means something outside this adapter's control
                    # raced a delete between our INSERT and this SELECT.
                    # Genuinely outcome-unknown from our point of view.
                    return ERROR(ErrorKind.CONFLICT_UNKNOWN)
                current_hash = row[0]
                if current_hash == new_hash:
                    return OK(new_hash)  # idempotent create replay (§3)
                return EXISTS(current_hash)

            # --- CAS-update ---
            # H1 Increment 2, Phase 2: ownership/fence FIRST, checked under
            # the SAME transaction's row locks (SELECT ... FOR UPDATE) as
            # the hash check and the eventual UPDATE -- Postgres's own row
            # lock is the linearization point for this whole check-then-act
            # sequence (the module docstring's existing rationale for the
            # plain hash CAS, extended here to cover fence: no other writer
            # can change either row while we hold both locks).
            # ROW-LOCK ORDER: store row first, then fence row -- the same
            # order the create-only path above takes (INSERT store, then
            # upsert fence), so a create racing a fenced CAS on the same key
            # can never deadlock (each would otherwise hold the row the other
            # needs). The fence is still EVALUATED first.
            cur.execute(
                f"SELECT version_hash, generation FROM {self._qualified_table} WHERE key = %s FOR UPDATE",
                (k,),
            )
            srow = cur.fetchone()
            current_hash = srow[0] if srow else None

            cur.execute(
                f"""
                SELECT owner_token, owner_expiry, owner_fence, last_accepted_fence
                FROM {self._qualified_fence_table} WHERE key = %s FOR UPDATE
                """,
                (k,),
            )
            frow = cur.fetchone()
            owner_token, owner_expiry, owner_fence, last_accepted_fence = (
                frow if frow else (None, None, 0, 0)
            )

            fence_ok = (
                precondition_lease is not None
                and precondition_lease.token == owner_token
                and owner_expiry is not None
                and owner_expiry > time.time()
                and precondition_lease.fence == owner_fence  # exactly what lock() allocated for this token
                and precondition_lease.fence >= last_accepted_fence
            )

            if not fence_ok:
                # Release the row locks FIRST (the racer holding the current
                # fence needs them to land its write), then settle (bounded)
                # so STALE carries the true winner's hash rather than this
                # caller's own pre-race snapshot.
                conn.commit()
                settled_hash = self._settle_current_hash_after_fence_loss(k, expected_hash)
                return STALE(settled_hash, reason="FENCE")

            if srow is None:
                conn.commit()
                return STALE(None)  # §4.4: absent -> STALE{None}; a CAS-update never creates

            # The content-hash CAS itself is the NATIVE precondition mandated
            # by contract §3/§4.4 -- `UPDATE ... WHERE key = ? AND
            # version_hash = ?` (rowcount 1 -> OK) -- not a Python-side
            # compare of the value read above. The row lock held since the
            # SELECT ... FOR UPDATE makes the two equivalent for correctness,
            # but only the native form lets the load-time capability probe
            # (§8) detect a wrong mapping / missing precondition: a
            # Python-side compare would pass the probe's stale-CAS check
            # even on a backend whose conditional UPDATE never matched.
            # JUDGMENT CALL 1: the generation-monotonicity guard is folded
            # into the same atomic UPDATE.
            if new_generation is None:
                cur.execute(
                    f"""
                    UPDATE {self._qualified_table}
                    SET body = %s, version_hash = %s, generation = %s, updated_at = now()
                    WHERE key = %s AND version_hash = %s
                    """,
                    (raw, new_hash, new_generation, k, expected_hash),
                )
            else:
                cur.execute(
                    f"""
                    UPDATE {self._qualified_table}
                    SET body = %s, version_hash = %s, generation = %s, updated_at = now()
                    WHERE key = %s AND version_hash = %s
                      AND (generation IS NULL OR generation < %s)
                    """,
                    (raw, new_hash, new_generation, k, expected_hash, new_generation),
                )
            if cur.rowcount != 1:
                # 0-row update -> hash mismatch or generation regressed. The
                # row is still locked by this transaction, so the hash read
                # under that lock IS the follow-up SELECT §4.4 asks for.
                conn.commit()
                return STALE(current_hash)

            # last_accepted_fence lands in the SAME transaction/commit as
            # the body -- atomic together, per the contract requirement.
            cur.execute(
                f"UPDATE {self._qualified_fence_table} SET last_accepted_fence = %s WHERE key = %s",
                (precondition_lease.fence, k),
            )
            conn.commit()
            return OK(new_hash)

        except pg8000.exceptions.DatabaseError as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            if _sqlstate(exc) in _BUSY_SQLSTATES:  # JUDGMENT CALL 3
                return ERROR(ErrorKind.BUSY)
            # An unexpected server error is not a normal CAS outcome; the
            # transaction is guaranteed aborted (rolled back above), so this
            # is still definitely-not-committed, but it is not one of the
            # contract's ordinary WriteResult shapes for a well-formed call
            # -- surface it rather than silently mis-mapping to some
            # ErrorKind that would mislead a caller about what happened.
            raise
        except (pg8000.exceptions.InterfaceError, OSError):
            # JUDGMENT CALL 4: connection dropped/broken mid-operation. We
            # never issued (or never received the ack for) COMMIT, so this
            # write cannot have been acknowledged as committed.
            self._reset_conn()
            return ERROR(ErrorKind.NETWORK)

    # Upper bound on how long a fence-losing write() waits for the current
    # fence owner's pending write to land before reporting STALE. Only ever
    # reached when the owner never writes (it lost interest, or is slow
    # beyond this bound); the wait also ends as soon as the owner's lease
    # expires, since no fenced write can land after that.
    _FENCE_LOSS_SETTLE_MAX_S = 5.0
    _FENCE_LOSS_SETTLE_POLL_S = 0.01

    def _settle_current_hash_after_fence_loss(self, key: str, expected_hash: str) -> Optional[str]:
        """Called with NO transaction open (write() commits -- releasing its
        row locks -- before calling this, since the racer holding the
        current fence needs those locks to land its write). Returns the
        key's current hash once the race that superseded this caller's
        fence has SETTLED: either the hash moved away from `expected_hash`
        (the winner landed), or no fenced write is pending any more (the
        fence row's owner_fence has been consumed by a committed write, or
        the owner's lease expired), or the bounded wait ran out. Returns
        the last successfully observed hash (initially `expected_hash`) if
        a re-read fails -- never a value worse than the pre-read one."""
        deadline = time.monotonic() + self._FENCE_LOSS_SETTLE_MAX_S
        current_hash: Optional[str] = expected_hash
        while True:
            try:
                conn = self._get_conn()
                cur = conn.cursor()
                # READ ORDER MATTERS: fence row FIRST, store row SECOND. Under
                # READ COMMITTED each statement has its own snapshot, so the
                # opposite order can straddle the winner's commit -- hash read
                # before it (still the pre-race value), fence read after it
                # (consumed -> "nothing pending") -- and settle on the stale
                # hash. Reading the fence first means a "not pending"
                # observation is followed by a hash read that also postdates
                # the commit that consumed the fence.
                cur.execute(
                    f"""
                    SELECT owner_fence, last_accepted_fence, owner_expiry
                    FROM {self._qualified_fence_table} WHERE key = %s
                    """,
                    (key,),
                )
                frow = cur.fetchone()
                cur.execute(
                    f"SELECT version_hash FROM {self._qualified_table} WHERE key = %s", (key,)
                )
                srow = cur.fetchone()
                conn.commit()  # READ COMMITTED: end the snapshot so the next poll sees new commits
            except pg8000.exceptions.DatabaseError:
                try:
                    conn.rollback()
                except Exception:
                    pass
                return current_hash
            except (pg8000.exceptions.InterfaceError, OSError):
                self._reset_conn()
                return current_hash
            current_hash = srow[0] if srow else None
            if current_hash != expected_hash:
                return current_hash
            pending = (
                frow is not None
                and frow[0] > frow[1]
                and frow[2] is not None
                and frow[2] > time.time()
            )
            if not pending or time.monotonic() >= deadline:
                return current_hash
            time.sleep(self._FENCE_LOSS_SETTLE_POLL_S)

    # -- advisory lock() API (JUDGMENT CALL 6 — NOT the CAS mechanism) -------

    def lock(self, key: str, ttl_s: float) -> Lock:
        import secrets

        with self._lock_mutex:
            now = time.time()
            existing = self._locks.get(key)
            if existing is not None and existing[1] > now:
                raise BackendBusyError(f"key {key!r} is locked")
            token = secrets.token_hex(16)  # 128-bit CSPRNG token
            expiry = now + ttl_s
            new_fence = self._advance_durable_fence(key, token, expiry)
            self._locks[key] = (token, expiry)
            return Lock(key=key, token=token, expiry_epoch=expiry, fence=new_fence)

    def _advance_durable_fence(self, key: str, token: str, expiry: float) -> int:
        """H1 Increment 2, Phase 2: durable, cross-process/-connection/
        -instance fence allocation. Row-locked via SELECT ... FOR UPDATE
        inside one transaction, so two concurrent lock() calls on the same
        key -- including from separate PostgresBackend instances/
        connections/processes -- can never compute the same "next" fence:
        Postgres's own row lock is the serialization point, mirroring the
        same argument write() already relies on for its CAS (module
        docstring)."""
        try:
            conn = self._get_conn()
        except (pg8000.exceptions.InterfaceError, OSError) as exc:
            raise BackendBusyError(f"lock({key!r}): could not open a connection") from exc
        try:
            cur = conn.cursor()
            cur.execute(
                f"INSERT INTO {self._qualified_fence_table} (key) VALUES (%s) ON CONFLICT (key) DO NOTHING",
                (key,),
            )
            cur.execute(
                f"""
                SELECT owner_fence, last_accepted_fence FROM {self._qualified_fence_table}
                WHERE key = %s FOR UPDATE
                """,
                (key,),
            )
            owner_fence, last_accepted_fence = cur.fetchone()
            new_fence = max(owner_fence, last_accepted_fence) + 1
            cur.execute(
                f"""
                UPDATE {self._qualified_fence_table}
                SET owner_token = %s, owner_expiry = %s, owner_fence = %s
                WHERE key = %s
                """,
                (token, expiry, new_fence, key),
            )
            conn.commit()
            return new_fence
        except pg8000.exceptions.DatabaseError as exc:
            # lock_timeout/statement_timeout (a concurrent write() holds the
            # fence row) or any other server error: the transaction is now
            # aborted and MUST be rolled back, or every later statement on
            # this thread-local connection fails with "current transaction
            # is aborted". Surface as BackendBusyError -- lock()'s one
            # failure shape -- with the cause chained.
            try:
                conn.rollback()
            except Exception:
                pass
            raise BackendBusyError(
                f"lock({key!r}): fence allocation failed "
                f"(sqlstate={_sqlstate(exc) or 'unknown'})"
            ) from exc
        except (pg8000.exceptions.InterfaceError, OSError) as exc:
            # Connection dropped mid-operation: nothing was committed.
            self._reset_conn()
            raise BackendBusyError(f"lock({key!r}): connection failed during fence allocation") from exc

    def unlock(self, lock: Lock) -> bool:
        import time

        with self._lock_mutex:
            existing = self._locks.get(lock.key)
            if existing is None:
                return False
            token, expiry = existing
            if token != lock.token or expiry <= time.time():
                return False
            del self._locks[lock.key]
            return True

    def renew(self, lock: Lock, ttl_s: float) -> bool:
        with self._lock_mutex:
            existing = self._locks.get(lock.key)
            if existing is None:
                return False
            token, expiry = existing
            if token != lock.token or expiry <= time.time():
                return False
            new_expiry = time.time() + ttl_s
            # H1 Increment 2, Phase 2: extend the DURABLE fence-owner's
            # expiry too (fence unchanged) -- mirrors store/local.py's
            # renew() -- so a renewed lease keeps its CAS-write
            # authorization when write() reads the durable fence table
            # fresh. Done BEFORE extending the in-memory advisory lock, and
            # rolled back on failure (an aborted transaction left open
            # would poison every later statement on this connection): a
            # renew whose durable half did not land reports False.
            try:
                conn = self._get_conn()
                cur = conn.cursor()
                cur.execute(
                    f"""
                    UPDATE {self._qualified_fence_table} SET owner_expiry = %s
                    WHERE key = %s AND owner_token = %s
                    """,
                    (new_expiry, lock.key, token),
                )
                matched = cur.rowcount
                conn.commit()
            except pg8000.exceptions.DatabaseError:
                try:
                    conn.rollback()
                except Exception:
                    pass
                return False
            except (pg8000.exceptions.InterfaceError, OSError):
                self._reset_conn()
                return False
            if matched != 1:
                # The durable owner is no longer this token: another
                # PostgresBackend instance/process lock()'d the key and
                # superseded this lease (the advisory registry is per
                # instance, so only the fence row can tell). Contract:
                # renew "succeeds ONLY if this token still owns".
                return False
            self._locks[lock.key] = (token, new_expiry)
            return True


__all__ = ["PostgresBackend"]
