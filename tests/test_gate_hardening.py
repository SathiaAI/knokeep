"""Regression tests for the independent adversarial review's findings against
store/gate.py (2026-09-20 hardening pass, see gate.py's module docstring).

Each test here pins down ONE specific finding so it cannot silently regress:
  1. persist() now scans the KEY bytes as well as the body.
  2. The entropy heuristic no longer flags plain hex (sha256/git-SHA) but
     still flags mixed-case high-entropy tokens (and now requires mixed case).
  3. reconcile() requires a valid 64-hex intended_new_hash, returns the
     retry's real result unchanged when current == expected_hash, and only
     the superseded branch resolves to CONFLICT_UNKNOWN.
  4. ScannedKey/ScannedBody are immutable after construction.
  5. Other hardening: key length caps, trailing '.'/' ' segment rejection,
     bounded generation digit-run, make_generation_header rejects bool,
     doc_type must be str, oversized body -> INVALID_ARGUMENT, added
     prefixes (sk-ant-, stripe sk_live_, glpat-), and verify_pair().

All writes go through store.gate.persist() (the one door), except where a
test is specifically exercising the adapter-facing verify()/verify_pair()
boundary, which is this module's own subject matter.
"""
from __future__ import annotations

import hashlib
import secrets
import types

import pytest

from store import gate
from store.types import (
    ERROR,
    EXISTS,
    OK,
    STALE,
    Blob,
    ErrorKind,
    sha256_hex,
)
from tests.ctx_helpers import create_ctx, fenced_ctx


# ---------------------------------------------------------------------------
# RecordingBackend — a StoreBackend stub that counts write() calls and keeps
# an in-memory dict (so read()-dependent flows like reconcile() still work),
# without any real I/O. Used wherever "no I/O happened on reject" matters.
# ---------------------------------------------------------------------------


class RecordingBackend:
    def __init__(self) -> None:
        self.write_calls = 0
        self.captured = []  # list of (ScannedKey, ScannedBody) ever passed in
        self._store: dict = {}
        self._locks: dict = {}

    def read(self, key):
        return self._store.get(key)

    def list(self, prefix):
        return iter(k for k in self._store if k.startswith(prefix))

    def write(self, key, body, *, ctx):
        from store.context import Overwrite

        expected_hash = ctx.precondition.expected_hash if isinstance(ctx.precondition, Overwrite) else None
        self.write_calls += 1
        self.captured.append((key, body))
        if not isinstance(key, gate.ScannedKey) or not isinstance(body, gate.ScannedBody):
            raise TypeError("RecordingBackend.write requires gate-issued values")
        if not gate.verify(key) or not gate.verify(body):
            raise TypeError("RecordingBackend.write: gate marker verification failed")

        current = self._store.get(key.key)
        current_hash = current.version_hash if current is not None else None

        if expected_hash is None:
            if current is not None:
                return EXISTS(current_hash)
        else:
            if current_hash != expected_hash:
                return STALE(current_hash)

        new_hash = sha256_hex(body.body)
        self._store[key.key] = Blob(body=body.body, version_hash=new_hash)
        return OK(new_hash)

    def lock(self, key, ttl_s):
        # Minimal in-memory advisory lock (same shape as store/fake.py),
        # needed by tests.ctx_helpers.fenced_ctx() to build a real lease for
        # this stub's CAS-update calls. Never the CAS mechanism itself.
        import secrets as _secrets
        import time as _time

        from store.backend import BackendBusyError, Lock

        now = _time.time()
        existing = self._locks.get(key)
        if existing is not None and existing[1] > now:
            raise BackendBusyError(f"key {key!r} is locked")
        token = _secrets.token_hex(16)
        expiry = now + ttl_s
        self._locks[key] = (token, expiry)
        return Lock(key=key, token=token, expiry_epoch=expiry)

    def unlock(self, lock):
        import time as _time

        existing = self._locks.get(lock.key)
        if existing is None:
            return False
        token, expiry = existing
        if token != lock.token or expiry <= _time.time():
            return False
        del self._locks[lock.key]
        return True

    def renew(self, lock, ttl_s):  # pragma: no cover - unused
        raise NotImplementedError

    def capabilities(self):  # pragma: no cover - unused
        raise NotImplementedError

    def health(self):  # pragma: no cover - unused
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Finding #1 — the KEY is scanned too, not just the body
# ---------------------------------------------------------------------------


def test_key_scan_blocks_secret_shaped_key_name():
    rec = RecordingBackend()
    result = gate.persist(
        rec,
        "sk-abcdefghijklmnopqrstuvwxyz1234567890",
        b"hello",
        ctx=create_ctx(),
        doc_type="journal",
    )
    assert isinstance(result, ERROR)
    assert result.kind is ErrorKind.SECRET_BLOCKED
    assert rec.write_calls == 0


# ---------------------------------------------------------------------------
# Finding #2 — plain hex is OK, mixed-case high-entropy tokens are still blocked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc_type", ["journal", "HANDOFF", "system_state"])
def test_plain_hex_hashes_are_not_blocked(doc_type):
    rec = RecordingBackend()
    sha256_like = hashlib.sha256(b"some content").hexdigest()  # 64 hex chars
    git_sha_like = hashlib.sha1(b"some other content").hexdigest()  # 40 hex chars
    body = f"version_hash={sha256_like}\ncommit={git_sha_like}\n".encode()
    result = gate.persist(rec, "state/doc", body, ctx=create_ctx(), doc_type=doc_type)
    assert isinstance(result, OK)
    assert rec.write_calls == 1
    assert rec.read("state/doc").body == body


def test_mixed_case_high_entropy_token_is_blocked():
    rec = RecordingBackend()
    body = b"secret=" + secrets.token_urlsafe(40).encode()
    result = gate.persist(rec, "journal/entry", body, ctx=create_ctx(), doc_type="journal")
    assert isinstance(result, ERROR)
    assert result.kind is ErrorKind.SECRET_BLOCKED
    assert rec.write_calls == 0


# ---------------------------------------------------------------------------
# Finding #3 — reconcile() hardening
# ---------------------------------------------------------------------------


def test_reconcile_none_intended_hash_is_invalid_argument():
    rec = RecordingBackend()
    result = gate.reconcile(rec, "somekey", intended_new_hash=None, expected_hash=None)
    assert isinstance(result, ERROR)
    assert result.kind is ErrorKind.INVALID_ARGUMENT


def test_reconcile_malformed_intended_hash_is_invalid_argument():
    rec = RecordingBackend()
    result = gate.reconcile(
        rec, "somekey", intended_new_hash="not-a-hash", expected_hash=None
    )
    assert isinstance(result, ERROR)
    assert result.kind is ErrorKind.INVALID_ARGUMENT


def test_reconcile_current_equals_expected_returns_retrys_real_ok():
    rec = RecordingBackend()
    r0 = gate.persist(rec, "k", b"v0", ctx=create_ctx(), doc_type="journal")
    assert isinstance(r0, OK)

    intended = b"v1"
    intended_hash = sha256_hex(intended)

    def retry():
        return gate.persist(rec, "k", intended, ctx=fenced_ctx(rec, "k", r0.new_hash), doc_type="journal")

    result = gate.reconcile(
        rec, "k", intended_new_hash=intended_hash, expected_hash=r0.new_hash, retry=retry
    )
    assert isinstance(result, OK)
    assert result.new_hash == intended_hash
    assert rec.read("k").body == intended


def test_reconcile_current_equals_expected_returns_retrys_real_stale_unchanged():
    rec = RecordingBackend()
    r0 = gate.persist(rec, "k", b"v0", ctx=create_ctx(), doc_type="journal")
    assert isinstance(r0, OK)

    intended = b"v1"
    intended_hash = sha256_hex(intended)

    def retry():
        # The retry itself races against a stale expected_hash -> STALE.
        return gate.persist(rec, "k", intended, ctx=fenced_ctx(rec, "k", "f" * 64), doc_type="journal")

    result = gate.reconcile(
        rec, "k", intended_new_hash=intended_hash, expected_hash=r0.new_hash, retry=retry
    )
    assert isinstance(result, STALE)
    assert result.current_hash == r0.new_hash


# ---------------------------------------------------------------------------
# Finding #4 — ScannedKey/ScannedBody are immutable
# ---------------------------------------------------------------------------


def test_scanned_key_and_body_reject_attribute_mutation():
    rec = RecordingBackend()
    r = gate.persist(rec, "k", b"hello", ctx=create_ctx(), doc_type="journal")
    assert isinstance(r, OK)
    key_obj, body_obj = rec.captured[0]

    with pytest.raises(TypeError):
        body_obj._body = b"forged"
    with pytest.raises(TypeError):
        key_obj._key = "forged"
    with pytest.raises(TypeError):
        del body_obj._body
    with pytest.raises(TypeError):
        del key_obj._key


# ---------------------------------------------------------------------------
# Finding #5 — other hardening
# ---------------------------------------------------------------------------


def test_generation_header_with_21_digit_run_is_invalid_argument():
    rec = RecordingBackend()
    bad_header = b"#knokeep-gen:" + b"1" * 21 + b"\n" + b"leaseholder=alice"
    result = gate.persist(rec, "lease/x", bad_header, ctx=create_ctx(), doc_type="lease")
    assert isinstance(result, ERROR)
    assert result.kind is ErrorKind.INVALID_ARGUMENT
    assert rec.write_calls == 0


def test_key_segment_ending_in_dot_is_invalid_argument():
    rec = RecordingBackend()
    result = gate.persist(rec, "foo./bar", b"body", ctx=create_ctx(), doc_type="journal")
    assert isinstance(result, ERROR)
    assert result.kind is ErrorKind.INVALID_ARGUMENT
    assert rec.write_calls == 0


def test_key_segment_over_255_chars_is_invalid_argument():
    rec = RecordingBackend()
    long_segment = "a" * 256
    result = gate.persist(
        rec, f"dir/{long_segment}", b"body", ctx=create_ctx(), doc_type="journal"
    )
    assert isinstance(result, ERROR)
    assert result.kind is ErrorKind.INVALID_ARGUMENT
    assert rec.write_calls == 0


def test_key_over_1024_chars_total_is_invalid_argument():
    rec = RecordingBackend()
    # Build a key over 1024 chars total out of many short, otherwise-valid
    # segments so this specifically exercises the total-length cap, not the
    # per-segment cap.
    long_key = "/".join(["ab"] * 400)  # 400*2 + 399 separators = 1199 chars
    assert len(long_key) > 1024
    result = gate.persist(rec, long_key, b"body", ctx=create_ctx(), doc_type="journal")
    assert isinstance(result, ERROR)
    assert result.kind is ErrorKind.INVALID_ARGUMENT
    assert rec.write_calls == 0


def test_make_generation_header_rejects_bool():
    with pytest.raises(ValueError):
        gate.make_generation_header(True)
    with pytest.raises(ValueError):
        gate.make_generation_header(False)


def test_make_generation_header_accepts_int():
    header = gate.make_generation_header(5)
    assert header == b"#knokeep-gen:5\n"


def test_verify_pair_true_for_same_persist_call_false_otherwise():
    rec = RecordingBackend()
    r1 = gate.persist(rec, "pair/a", b"body-a", ctx=create_ctx(), doc_type="journal")
    assert isinstance(r1, OK)
    key_a, body_a = rec.captured[0]

    r2 = gate.persist(rec, "pair/b", b"body-b", ctx=create_ctx(), doc_type="journal")
    assert isinstance(r2, OK)
    key_b, body_b = rec.captured[1]

    # Same persist() call -> same nonce -> verify_pair True.
    assert gate.verify_pair(key_a, body_a) is True
    assert gate.verify_pair(key_b, body_b) is True

    # Cross-call recombination (different nonces) -> False, even though both
    # halves are individually genuine, gate-issued objects.
    assert gate.verify_pair(key_a, body_b) is False
    assert gate.verify_pair(key_b, body_a) is False

    # A forged, non-gate stand-in is never accepted either.
    forged_key = types.SimpleNamespace(key="pair/a")
    forged_body = types.SimpleNamespace(body=b"body-a")
    assert gate.verify_pair(forged_key, body_a) is False
    assert gate.verify_pair(key_a, forged_body) is False
    assert gate.verify(forged_key) is False
    assert gate.verify(forged_body) is False


def test_oversized_body_is_invalid_argument():
    rec = RecordingBackend()
    oversized = b"a" * (8 * 1024 * 1024 + 1)
    result = gate.persist(rec, "big/doc", oversized, ctx=create_ctx(), doc_type="journal")
    assert isinstance(result, ERROR)
    assert result.kind is ErrorKind.INVALID_ARGUMENT
    assert rec.write_calls == 0


@pytest.mark.parametrize(
    "planted, label_substr",
    [
        (b"api_key=sk-ant-" + b"A" * 20, "anthropic"),
        (b"token=glpat-" + b"B" * 20, "gitlab"),
        (b"stripe_key=sk_live_" + b"C" * 20, "stripe"),
    ],
)
def test_added_secret_prefixes_are_blocked(planted, label_substr):
    rec = RecordingBackend()
    result = gate.persist(rec, "secrets/x", planted, ctx=create_ctx(), doc_type="journal")
    assert isinstance(result, ERROR)
    assert result.kind is ErrorKind.SECRET_BLOCKED
    assert any(label_substr.lower() in lbl.lower() for lbl in result.labels)
    assert rec.write_calls == 0
