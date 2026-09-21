"""LocalBackend — filesystem StoreBackend adapter (contract v1.4, §3, §4.1).

Windows is the contract's normative host for this adapter; this module is
written to be correct on both platforms but can only be *exercised* on this
POSIX/Linux host (see tests/test_local_backend.py for what is skipped here
and must be re-run on Windows).

On-disk layout under `root`:

    root/
      data/                published key blobs — a rebuildable materialized
                            view of the journal (contract §3 durability
                            model), one file per key, key '/' segments mapped
                            straight onto path components (keys are already
                            gate-validated: charset-restricted, no '..', no
                            empty segments, so this mapping is safe)
      staging/              tempfile.mkstemp() targets for atomic publish,
                            guaranteed to be on the SAME volume as data/
      journal/journal.log   append-only durability-commit log (§3: "the
                            fsync'd append-only journal is the durability
                            source of truth")
      locks/cas.lock        dedicated lock file guarding the write()
                            read-compare-replace critical section (§4.1)
      locks/advisory.lock   dedicated lock file guarding the advisory
                            lock()/unlock()/renew() bookkeeping (§2) — kept
                            SEPARATE from cas.lock because the contract is
                            explicit that "Advisory lock() is never the CAS
                            mechanism" (§3): the two must not contend with
                            each other under load.
      locks/advisory/       one small file per advisory-locked key, holding
                            only "<128-bit-hex-token> <expiry-epoch-float>"
                            (§5: lock/marker file contents are limited to
                            token + expiry int)

JUDGMENT CALLS (each also called out inline at its point of use):

  1. Generation monotonicity (residual from T1, per task spec). The gate
     (store/gate.py `persist()`) only validates that a non-STATE doc type's
     body carries a well-formed `#knokeep-gen:<uint64>` header — it cannot
     check monotonicity because it is a stateless per-call function with no
     view of what is currently stored. This adapter *does* hold that view
     (the currently-published bytes at the key), so `write()` derives the
     stored generation from the current on-disk body and REJECTS a
     CAS-update whose new generation is not strictly greater, returning
     STALE(current_hash) (chosen because a generation regression under a
     hash-matching CAS is exactly the "your view of state is behind"
     situation STALE already means — no new ErrorKind was needed).

  2. Journal record framing. The contract mandates journal-then-publish and
     journal-replay-on-resume but does not specify a wire format. Records
     are length-prefixed (`>I` key length, key bytes, `>Q` body length, body
     bytes, 32-byte sha256 digest of the body) so a record can be parsed
     without scanning for delimiters that might collide with arbitrary body
     bytes. Replay stops at the first record that fails to parse fully or
     whose trailing digest does not match — that can only be an in-flight
     append truncated by a crash (recall the *previous* record's fsync
     already made it durable), so the safe reading is "ignore the torn
     tail, keep everything before it."

  3. Create-only publish uses O_EXCL only as a **name reservation**
     (open+immediately close an empty file), never to stream bytes — the
     actual bytes are always fully written and fsynced to a staging temp
     file first, then swapped in with `os.replace`. This is the literal
     reading of §4.1's "O_EXCL reserves the key name; publish by rename...
     Never O_EXCL-create the key path then stream bytes into it."

  4. `os.replace` retry-on-PermissionError is NOT gated on `os.name == "nt"`.
     The contract calls out "os.replace sharing-violation (Windows)"
     specifically, but a bounded retry-then-BUSY on `PermissionError` from
     `os.replace` is a harmless superset on POSIX (where such a transient
     PermissionError from a rename essentially never happens in practice)
     and lets the retry *logic itself* be exercised by a portable unit test
     on this Linux host (tests/test_local_backend.py), rather than being
     unverifiable dead code here. The real Windows sharing-violation
     *scenario* still cannot be produced on this host and is not claimed to
     be covered — only the retry mechanism is.

  5. Case-insensitive-volume collision detection is probed once at
     construction (write two same-name-different-case marker files under
     `data/` and see if they alias). On this Linux/ext4 host that probe will
     always find the volume case-SENSITIVE, so the refusal branch cannot be
     exercised end-to-end here; tests/test_local_backend.py exercises the
     refusal logic itself by monkeypatching the detected flag, and documents
     that this is not a substitute for running on an actual case-insensitive
     (NTFS/APFS-default) volume.

  6. Advisory lock() acquire and the internal CAS lock both use the same
     "non-blocking attempt in a polling loop bounded by a wall-clock
     deadline" shape rather than a single blocking syscall with a kernel
     timeout, because POSIX `fcntl.flock` and Windows `msvcrt.locking` are
     both **non-blocking-only** in their `_NB`/`LK_NBLCK` forms per the
     contract ("non-blocking `msvcrt.locking(LK_NBLCK)`... `fcntl.flock`");
     there is no portable "block with a timeout" primitive for either, so a
     poll loop is the natural way to get a bounded wait out of two
     inherently try-once primitives.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import struct
import tempfile
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from . import gate
from .backend import BackendBusyError, Lock
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

# Platform-specific locking primitive. Contract §4.1: "fcntl forbidden on
# Windows" (and, symmetrically, msvcrt is meaningless on POSIX) — select at
# import time by os.name so the wrong module is never even imported.
if os.name == "nt":  # pragma: no cover - exercised only on the Windows host
    import msvcrt
else:
    import fcntl


# ---------------------------------------------------------------------------
# Generation header (contract §6; same convention as store/gate.py's
# `make_generation_header`, re-implemented locally rather than imported so
# this adapter does not reach into gate.py's private helpers).
#
# The digit group is capped at 20 digits ({0,19} after the leading digit) —
# exactly enough to hold any uint64 value (2**64-1 is 20 digits) — rather
# than left unbounded (review finding). `gate.persist()` only checks that
# this header is WELL-FORMED for a non-STATE doc_type; it does not (and
# cannot, being a stateless per-call function) bound the digit count, so an
# unbounded regex here would let a body carrying an enormous digit run reach
# `int(m.group(1))` below. CPython's default `int()`-from-string conversion
# limit (sys.get_int_max_str_digits(), 4300 by default) would then raise
# ValueError, escaping write() as an unhandled exception instead of the
# WriteResult every caller expects. Bounding the regex makes that string
# always small and cheap to convert, and any value that overflows uint64 is
# still correctly rejected by the `value > _UINT64_MAX` check just below.
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
# Internal errors (never escape LocalBackend; caught and turned into
# ERROR(BUSY) / re-raised as BackendBusyError at the public-method boundary)
# ---------------------------------------------------------------------------


class _ReplaceBusy(Exception):
    """os.replace kept failing with PermissionError past the retry budget."""


class _ReservationRace(Exception):
    """O_EXCL name reservation lost a race that should have been impossible
    while holding cas.lock (e.g. an out-of-band writer touched data/
    directly). Treated as BUSY rather than corrupting/overwriting."""


# ---------------------------------------------------------------------------
# Cross-process, non-blocking, dedicated-file lock (contract §4.1)
# ---------------------------------------------------------------------------


class _FileLock:
    """A dedicated-file lock, acquired via a bounded poll loop over a
    single non-blocking syscall per attempt — never blocks indefinitely.
    """

    def __init__(self, path: Path):
        self._path = path
        self._fh = None

    def acquire(self, timeout_s: float, poll_interval_s: float = 0.01) -> bool:
        deadline = time.monotonic() + timeout_s
        fh = open(self._path, "a+b")
        if os.name == "nt":  # pragma: no cover - Windows-only branch
            try:
                fh.seek(0, os.SEEK_END)
                if fh.tell() == 0:
                    fh.write(b"\0")
                    fh.flush()
            except OSError:
                fh.close()
                raise
        while True:
            if self._try_lock(fh):
                self._fh = fh
                return True
            if time.monotonic() >= deadline:
                fh.close()
                return False
            time.sleep(poll_interval_s)

    def _try_lock(self, fh) -> bool:
        if os.name == "nt":  # pragma: no cover - Windows-only branch
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
        except OSError:
            return False

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":  # pragma: no cover - Windows-only branch
                with contextlib.suppress(OSError):
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "_FileLock":
        if not self.acquire(float("inf")):  # pragma: no cover - defensive
            raise BackendBusyError(str(self._path))
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _fsync_dir(path: Path) -> None:
    """Parent-dir fsync on POSIX; no-op on Windows (contract §4.1)."""
    if os.name == "nt":  # pragma: no cover - Windows-only branch
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class LocalBackend:
    """Local filesystem StoreBackend adapter. See module docstring."""

    def __init__(
        self,
        root,
        *,
        lock_timeout_s: float = 5.0,
        staging_ttl_s: float = 300.0,
        replace_retry_attempts: int = 5,
        replace_retry_backoff_s: float = 0.05,
    ) -> None:
        self._root = Path(root).resolve()
        self._data_dir = self._root / "data"
        self._staging_dir = self._root / "staging"
        self._journal_dir = self._root / "journal"
        self._journal_path = self._journal_dir / "journal.log"
        self._locks_dir = self._root / "locks"
        self._advisory_dir = self._locks_dir / "advisory"
        self._cas_lock_path = self._locks_dir / "cas.lock"
        self._advisory_guard_path = self._locks_dir / "advisory.lock"

        self._lock_timeout_s = lock_timeout_s
        self._staging_ttl_s = staging_ttl_s
        self._replace_retry_attempts = replace_retry_attempts
        self._replace_retry_backoff_s = replace_retry_backoff_s

        for d in (
            self._root,
            self._data_dir,
            self._staging_dir,
            self._journal_dir,
            self._locks_dir,
            self._advisory_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        # §4.1: "refuse cross-volume via st_dev". staging/ and data/ are both
        # fixed subdirectories of `root` created above, so this is checked
        # once at construction rather than on every write (JUDGMENT CALL:
        # they cannot drift apart without an operator mounting something
        # unusual inside `root` after the fact).
        if os.stat(self._staging_dir).st_dev != os.stat(self._data_dir).st_dev:
            raise RuntimeError(
                "LocalBackend: staging/ and data/ must be on the same volume"
            )

        # Journal kept open for the life of the adapter ("already open" per
        # contract §4.1's write-order description) and appended-to under
        # cas.lock in write().
        self._journal_fh = open(self._journal_path, "ab")

        # One-time case-insensitivity probe (JUDGMENT CALL #5 above).
        self._case_insensitive = self._detect_case_insensitive()

        # Resume: replay the journal to re-materialize any key whose
        # published blob is missing or torn (contract §3). This MUST hold
        # the same cas.lock write() uses: two LocalBackend instances (e.g.
        # in different processes) can be constructed concurrently, and
        # without the lock one process's resume-rebuild of a key can race
        # another process's in-flight write() to that same key (both doing
        # an O_EXCL create for the same path at once) — a real race found
        # by tests/test_local_backend.py's two-process tests, not a
        # theoretical concern.
        resume_lock = _FileLock(self._cas_lock_path)
        if not resume_lock.acquire(self._lock_timeout_s):
            raise RuntimeError(
                "LocalBackend: could not acquire cas.lock to resume/scavenge on startup"
            )
        try:
            self._resume()
            # Startup scavenger: remove staging temps older than TTL, left
            # behind by a process that crashed between mkstemp and the
            # final os.replace/unlink (contract §3/§4.1). Guarded by the
            # same lock so it never races a concurrent publish's mkstemp.
            self._scavenge_staging()
        finally:
            resume_lock.release()

    # -- construction helpers -------------------------------------------------

    def _detect_case_insensitive(self) -> bool:
        name = "case-probe-" + secrets.token_hex(8)
        lower = self._data_dir / name
        upper = self._data_dir / name.upper()
        try:
            lower.write_bytes(b"")
            return upper.exists()
        finally:
            with contextlib.suppress(OSError):
                lower.unlink()

    def _iter_journal_records(self) -> Iterator[Tuple[str, bytes]]:
        try:
            f = open(self._journal_path, "rb")
        except FileNotFoundError:
            return
        with f:
            while True:
                header = f.read(4)
                if len(header) < 4:
                    return  # clean EOF or torn tail — stop, ignore the rest
                (key_len,) = struct.unpack(">I", header)
                key_bytes = f.read(key_len)
                if len(key_bytes) < key_len:
                    return
                body_len_bytes = f.read(8)
                if len(body_len_bytes) < 8:
                    return
                (body_len,) = struct.unpack(">Q", body_len_bytes)
                body = f.read(body_len)
                if len(body) < body_len:
                    return
                digest = f.read(32)
                if len(digest) < 32:
                    return
                if hashlib.sha256(body).digest() != digest:
                    return  # torn/corrupt record — stop before it
                try:
                    key = key_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    return
                yield key, body

    def _resume(self) -> None:
        latest: Dict[str, bytes] = {}
        for key, raw in self._iter_journal_records():
            latest[key] = raw  # last record per key wins
        for key, raw in latest.items():
            rel_path = self._key_to_relpath(key)
            data_path = self._safe_join(self._data_dir, rel_path)
            expected_hash = sha256_hex(raw)
            needs_rebuild = True
            if data_path.exists():
                try:
                    if sha256_hex(data_path.read_bytes()) == expected_hash:
                        needs_rebuild = False
                except OSError:
                    needs_rebuild = True
            if needs_rebuild:
                self._publish(rel_path, data_path, raw, create=not data_path.exists())

    def _scavenge_staging(self) -> None:
        now = time.time()
        try:
            entries = list(self._staging_dir.iterdir())
        except FileNotFoundError:
            return
        for entry in entries:
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if age > self._staging_ttl_s:
                with contextlib.suppress(OSError):
                    entry.unlink()

    # -- key/path mapping -------------------------------------------------

    def _key_to_relpath(self, key: str) -> Path:
        # Keys reaching write() are gate-validated (charset
        # [A-Za-z0-9._/-], no empty/'.'/'..' segments, no leading/trailing
        # '/'); reads may be called with a caller-supplied string directly,
        # so _safe_join below is the actual defense against traversal.
        return Path(*key.split("/"))

    def _safe_join(self, base: Path, rel_path: Path) -> Path:
        candidate = (base / rel_path)
        resolved_base = base.resolve()
        # Resolve the parent (the file itself need not exist yet) so a
        # symlink or '..' component cannot walk the result outside `base`.
        resolved_parent = candidate.parent.resolve()
        if resolved_parent != resolved_base and resolved_base not in resolved_parent.parents:
            raise ValueError(f"key resolves outside the data root: {rel_path}")
        return resolved_parent / candidate.name

    def _check_case_collision(self, rel_path: Path) -> bool:
        """§4.1: keys are logically case-sensitive; on a case-insensitive
        volume, refuse a write that would collide by case with an existing
        entry rather than silently aliasing it."""
        if not self._case_insensitive:
            return False
        current_dir = self._data_dir
        for part in rel_path.parts:
            try:
                entries = os.listdir(current_dir)
            except FileNotFoundError:
                return False
            for entry in entries:
                if entry != part and entry.lower() == part.lower():
                    return True
            current_dir = current_dir / part
        return False

    # -- journal + publish (contract §3/§4.1 write order) -------------------

    def _journal_append(self, key: str, raw: bytes) -> None:
        key_bytes = key.encode("utf-8")
        digest = hashlib.sha256(raw).digest()
        record = (
            struct.pack(">I", len(key_bytes))
            + key_bytes
            + struct.pack(">Q", len(raw))
            + raw
            + digest
        )
        self._journal_fh.write(record)
        self._journal_fh.flush()
        os.fsync(self._journal_fh.fileno())  # durability commit (§3)

    def _replace_with_retry(self, src: Path, dst: Path) -> None:
        attempts = 0
        while True:
            try:
                os.replace(str(src), str(dst))
                return
            except PermissionError:
                # JUDGMENT CALL #4: bounded retry regardless of OS, then
                # BUSY — never a copy/move fallback (§4.1).
                attempts += 1
                if attempts >= self._replace_retry_attempts:
                    raise _ReplaceBusy() from None
                time.sleep(self._replace_retry_backoff_s)

    def _publish(self, rel_path: Path, data_path: Path, raw: bytes, *, create: bool) -> None:
        data_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path_str = tempfile.mkstemp(dir=str(self._staging_dir))
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(tmp_fd, "wb") as tmp_f:
                tmp_f.write(raw)
                tmp_f.flush()
                os.fsync(tmp_f.fileno())
            if create:
                # JUDGMENT CALL #3: O_EXCL reserves the name only; the body
                # was already fully written+fsynced to tmp_path above.
                try:
                    fd = os.open(str(data_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                    os.close(fd)
                except FileExistsError:
                    raise _ReservationRace() from None
            self._replace_with_retry(tmp_path, data_path)
            _fsync_dir(data_path.parent)
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()  # no-op once os.replace has moved it away

    # -- StoreBackend protocol ---------------------------------------------

    def capabilities(self) -> Caps:
        return Caps(atomic=True, cas=True, lock=True, durable=True, remote=False)

    def health(self) -> BackendHealth:
        return BackendHealth(ok=True, detail=f"local filesystem backend at {self._root}")

    def read(self, key: str) -> Optional[Blob]:
        rel_path = self._key_to_relpath(key)
        data_path = self._safe_join(self._data_dir, rel_path)
        try:
            raw = data_path.read_bytes()
        except FileNotFoundError:
            return None
        # PERMISSION / other OSErrors propagate per contract §2 ("None ONLY
        # for NOT_FOUND; PERMISSION/NETWORK/CORRUPTION RAISE").
        return Blob(body=raw, version_hash=sha256_hex(raw))

    def list(self, prefix: str) -> Iterator[str]:
        # Strong consistency: this reflects the on-disk data/ dir at call
        # time — a local filesystem has no eventual-consistency window.
        results: List[str] = []
        if self._data_dir.exists():
            for path in self._data_dir.rglob("*"):
                if path.is_file():
                    rel = path.relative_to(self._data_dir).as_posix()
                    if rel.startswith(prefix):
                        results.append(rel)
        results.sort()
        return iter(results)

    def write(
        self,
        key: ScannedKey,
        body: ScannedBody,
        *,
        expected_hash: Optional[str],
    ) -> WriteResult:
        # Adapter accepts only gate-issued values; raw bytes/str are a
        # TypeError before any I/O (contract §1/§5).
        if not isinstance(key, ScannedKey) or not isinstance(body, ScannedBody):
            raise TypeError(
                "LocalBackend.write requires ScannedKey/ScannedBody from store.gate.persist()"
            )
        if not gate.verify(key) or not gate.verify(body):
            raise TypeError("LocalBackend.write: gate marker verification failed")

        k = key.key
        raw = body.body
        new_hash = sha256_hex(raw)
        new_generation = _extract_generation(raw)

        rel_path = self._key_to_relpath(k)
        data_path = self._safe_join(self._data_dir, rel_path)

        lock = _FileLock(self._cas_lock_path)
        if not lock.acquire(self._lock_timeout_s):
            return ERROR(ErrorKind.BUSY)
        try:
            if self._check_case_collision(rel_path):
                return ERROR(ErrorKind.INVALID_ARGUMENT)

            current_raw: Optional[bytes]
            try:
                current_raw = data_path.read_bytes()
            except FileNotFoundError:
                current_raw = None
            current_hash = sha256_hex(current_raw) if current_raw is not None else None

            if expected_hash is None:
                # Create-only.
                if current_raw is None:
                    self._commit(k, rel_path, data_path, raw, create=True)
                    return OK(new_hash)
                if current_hash == new_hash:
                    return OK(new_hash)  # idempotent create replay (§3)
                return EXISTS(current_hash)

            # CAS-update.
            if current_raw is None:
                return STALE(None)
            if current_hash != expected_hash:
                return STALE(current_hash)

            # JUDGMENT CALL #1: generation monotonicity, residual from T1.
            if new_generation is not None:
                stored_generation = _extract_generation(current_raw)
                if stored_generation is not None and new_generation <= stored_generation:
                    return STALE(current_hash)

            self._commit(k, rel_path, data_path, raw, create=False)
            return OK(new_hash)
        except (_ReplaceBusy, _ReservationRace):
            return ERROR(ErrorKind.BUSY)
        finally:
            lock.release()

    def _commit(self, key: str, rel_path: Path, data_path: Path, raw: bytes, *, create: bool) -> None:
        self._journal_append(key, raw)  # (1) durability commit
        self._publish(rel_path, data_path, raw, create=create)  # (2) atomic publish

    # -- advisory lock() API (contract §2 — NOT the CAS mechanism) ----------

    def _advisory_lock_path(self, key: str) -> Path:
        name = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._advisory_dir / f"{name}.lock"

    def _read_advisory(self, path: Path) -> Optional[Tuple[str, float]]:
        try:
            content = path.read_text(encoding="ascii")
        except FileNotFoundError:
            return None
        try:
            token, expiry_s = content.split(" ", 1)
            return token, float(expiry_s)
        except ValueError:
            return None

    def _write_advisory(self, path: Path, token: str, expiry: float) -> None:
        tmp_fd, tmp_path_str = tempfile.mkstemp(dir=str(self._advisory_dir))
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(tmp_fd, "w", encoding="ascii") as f:
                f.write(f"{token} {expiry!r}")
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp_path), str(path))
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()

    def lock(self, key: str, ttl_s: float) -> Lock:
        # JUDGMENT CALL: "bounded, never indefinite" governs how long we wait
        # to acquire the *bookkeeping* guard file (a short, purely mechanical
        # contention that should always clear quickly) — it must NOT be
        # read as "poll until the current holder's TTL happens to expire".
        # A key already validly held by someone else is BUSY immediately,
        # exactly like FakeBackend.lock(): a caller wanting to wait out an
        # expiring lock retries at the call-site, the adapter doesn't do it
        # silently on their behalf.
        path = self._advisory_lock_path(key)
        result = self._try_acquire_advisory(path, ttl_s)
        if result is None:
            raise BackendBusyError(f"key {key!r} is locked")
        token, expiry = result
        return Lock(key=key, token=token, expiry_epoch=expiry)

    def _try_acquire_advisory(self, path: Path, ttl_s: float) -> Optional[Tuple[str, float]]:
        guard = _FileLock(self._advisory_guard_path)
        if not guard.acquire(self._lock_timeout_s):
            raise BackendBusyError("advisory lock bookkeeping is busy")
        try:
            now = time.time()
            existing = self._read_advisory(path)
            if existing is not None and existing[1] > now:
                return None  # still held by someone else
            token = secrets.token_hex(16)  # 128-bit CSPRNG token
            expiry = now + ttl_s
            self._write_advisory(path, token, expiry)
            return token, expiry
        finally:
            guard.release()

    def unlock(self, lock: Lock) -> bool:
        path = self._advisory_lock_path(lock.key)
        guard = _FileLock(self._advisory_guard_path)
        if not guard.acquire(self._lock_timeout_s):
            return False
        try:
            existing = self._read_advisory(path)
            if existing is None:
                return False
            token, expiry = existing
            if token != lock.token or expiry <= time.time():
                return False
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            return True
        finally:
            guard.release()

    def renew(self, lock: Lock, ttl_s: float) -> bool:
        path = self._advisory_lock_path(lock.key)
        guard = _FileLock(self._advisory_guard_path)
        if not guard.acquire(self._lock_timeout_s):
            return False
        try:
            existing = self._read_advisory(path)
            if existing is None:
                return False
            token, expiry = existing
            if token != lock.token or expiry <= time.time():
                return False
            self._write_advisory(path, token, time.time() + ttl_s)
            return True
        finally:
            guard.release()

    # -- test/ops convenience (not part of StoreBackend protocol) ----------

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._journal_fh.close()


__all__ = ["LocalBackend"]
