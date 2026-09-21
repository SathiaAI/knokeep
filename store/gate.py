"""The secret gate — the ONE write door (contract v1.4, §1 gate-typed values, §5, §6, §7).

Every byte that reaches a backend's write() goes through persist() here first.
persist() is the only place that:
  1. validates arguments (before any I/O),
  2. enforces the key-name allowlist AND scans the key bytes,
  3. enforces the doc-type / generation ABA guard (§6),
  4. runs the secret scanner over BOTH key and body and fails closed on a hit,
  5. wraps the validated key/body in runtime-opaque, IMMUTABLE ScannedKey/ScannedBody
     objects that only this module can construct, and hands them to the backend.

Hardened after an independent adversarial review (2026-09-20): the key is now scanned
(a secret-shaped filename no longer bypasses the gate), the entropy heuristic no longer
false-positives on ordinary hex hashes (which would have bricked the tool's own state
docs), reconcile() validates its hash and returns the retry's true result, the wrappers
are frozen after construction, and the generation parser is bounded against a digit-run
DoS. These remain ONE of four defense layers, not a hard boundary (contract §1).
"""
from __future__ import annotations
import hmac
import math
import re
import secrets
from typing import Callable, List, Optional, Sequence

from .types import ERROR, OK, ErrorKind, STALE, EXISTS, WriteResult, sha256_hex

# --------------------------------------------------------------------------
# Runtime-opaque, immutable gate-typed values
# --------------------------------------------------------------------------

_SENTINEL = object()
_PROCESS_KEY = secrets.token_bytes(32)


def _compute_marker(role: bytes, payload: bytes, nonce: bytes) -> bytes:
    return hmac.new(_PROCESS_KEY, role + b"\x00" + payload + b"\x00" + nonce, "sha256").digest()


class _Frozen:
    """Mixin: immutable after construction. Adapters may verify() then rely on
    the object not changing under them (closes a verify-then-mutate TOCTOU)."""

    __slots__ = ()

    def __setattr__(self, name, value):  # pragma: no cover - defensive
        raise TypeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name):  # pragma: no cover - defensive
        raise TypeError(f"{type(self).__name__} is immutable")


class ScannedKey(_Frozen):
    """A key that has passed shape validation + secret scan, gate-issued only."""

    __slots__ = ("_key", "_nonce", "_marker")

    def __init__(self, sentinel: object, key: str, nonce: bytes) -> None:
        if sentinel is not _SENTINEL:
            raise TypeError("ScannedKey is constructible only inside store.gate.persist()")
        if not isinstance(key, str):
            raise TypeError("ScannedKey requires a str key")
        object.__setattr__(self, "_key", key)
        object.__setattr__(self, "_nonce", nonce)
        object.__setattr__(self, "_marker", _compute_marker(b"key", key.encode("utf-8"), nonce))

    def __init_subclass__(cls, **kwargs):  # pragma: no cover - defensive
        raise TypeError("ScannedKey may not be subclassed")

    def __reduce__(self):  # pragma: no cover - defensive
        raise TypeError("ScannedKey is not picklable")

    def __repr__(self) -> str:
        return "<ScannedKey (gate-issued, opaque)>"

    @property
    def key(self) -> str:
        return self._key


class ScannedBody(_Frozen):
    """A body that has passed the secret scan, gate-issued only. See ScannedKey."""

    __slots__ = ("_body", "_nonce", "_marker")

    def __init__(self, sentinel: object, body: bytes, nonce: bytes) -> None:
        if sentinel is not _SENTINEL:
            raise TypeError("ScannedBody is constructible only inside store.gate.persist()")
        if not isinstance(body, (bytes, bytearray)):
            raise TypeError("ScannedBody requires bytes")
        object.__setattr__(self, "_body", bytes(body))
        object.__setattr__(self, "_nonce", nonce)
        object.__setattr__(self, "_marker", _compute_marker(b"body", bytes(body), nonce))

    def __init_subclass__(cls, **kwargs):  # pragma: no cover - defensive
        raise TypeError("ScannedBody may not be subclassed")

    def __reduce__(self):  # pragma: no cover - defensive
        raise TypeError("ScannedBody is not picklable")

    def __repr__(self) -> str:
        return f"<ScannedBody (gate-issued, opaque, {len(self._body)} bytes)>"

    @property
    def body(self) -> bytes:
        return self._body


def verify(obj: object) -> bool:
    """Adapters call this before any I/O (contract §1/§5)."""
    if isinstance(obj, ScannedKey):
        expected = _compute_marker(b"key", obj._key.encode("utf-8"), obj._nonce)
        return hmac.compare_digest(expected, obj._marker)
    if isinstance(obj, ScannedBody):
        expected = _compute_marker(b"body", obj._body, obj._nonce)
        return hmac.compare_digest(expected, obj._marker)
    return False


def verify_pair(scanned_key: object, scanned_body: object) -> bool:
    """Verify a key/body PAIR came from the same persist() call: exact types,
    both markers valid, and the SAME nonce. Adapters should use this so a key
    from one call cannot be recombined with a body from another."""
    if not isinstance(scanned_key, ScannedKey) or not isinstance(scanned_body, ScannedBody):
        return False
    if not verify(scanned_key) or not verify(scanned_body):
        return False
    return hmac.compare_digest(scanned_key._nonce, scanned_body._nonce)


# --------------------------------------------------------------------------
# Key-name allowlist (contract §5)
# --------------------------------------------------------------------------

KEY_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_MAX_KEY_LEN = 1024
_MAX_SEGMENT_LEN = 255

_NTFS_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _valid_key_shape(key: str) -> bool:
    if not isinstance(key, str) or key == "":
        return False
    if len(key) > _MAX_KEY_LEN:
        return False
    if key != key.strip():
        return False
    if not KEY_RE.match(key):
        return False
    if "//" in key or key.startswith("/") or key.endswith("/"):
        return False
    for segment in key.split("/"):
        if segment == "" or len(segment) > _MAX_SEGMENT_LEN:
            return False
        if segment in (".", ".."):
            return False
        # NTFS: a trailing dot or space on a segment is stripped by the OS and
        # collides with the stripped name — reject it (review finding #6).
        if segment.endswith(".") or segment.endswith(" "):
            return False
        base = segment.split(".", 1)[0]
        if base.upper() in _NTFS_RESERVED:
            return False
    return True


# --------------------------------------------------------------------------
# Doc-type allowlist / ABA guard (contract §6)
# --------------------------------------------------------------------------

STATE_DOC_TYPES = {"system_state", "session_log", "HANDOFF", "journal"}
# Bounded digit run (<=20 digits) so int() cannot be fed megabytes (review #5).
_GEN_HEADER_RE = re.compile(rb"^#knokeep-gen:(0|[1-9][0-9]{0,19})\n")
_UINT64_MAX = (1 << 64) - 1


def _extract_generation(raw_bytes: bytes) -> Optional[int]:
    m = _GEN_HEADER_RE.match(raw_bytes)
    if not m:
        return None
    value = int(m.group(1))  # bounded to <=20 digits by the regex
    if value > _UINT64_MAX:
        return None
    return value


def make_generation_header(generation: int) -> bytes:
    # `type(...) is int` rejects bool (True/False are ints) -> no "#knokeep-gen:True".
    if type(generation) is not int or generation < 0 or generation > _UINT64_MAX:
        raise ValueError("generation must be a uint64")
    return f"#knokeep-gen:{generation}\n".encode("ascii")


# --------------------------------------------------------------------------
# Secret scanner (contract §1, §5, §9)
# --------------------------------------------------------------------------

_PREFIX_PATTERNS: Sequence[tuple] = (
    (re.compile(rb"sk-ant-[A-Za-z0-9_-]{10,}"), "anthropic key"),
    (re.compile(rb"sk-or-[A-Za-z0-9]{10,}"), "openrouter-style key"),
    (re.compile(rb"sk-(?!ant-|or-)[A-Za-z0-9]{10,}"), "openai-style key"),
    (re.compile(rb"(?:sk|rk|pk)_live_[A-Za-z0-9]{10,}"), "stripe-style live key"),
    (re.compile(rb"ghp_[A-Za-z0-9]{20,}"), "github personal access token"),
    (re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"), "github fine-grained PAT"),
    (re.compile(rb"glpat-[A-Za-z0-9_-]{10,}"), "gitlab PAT"),
    (re.compile(rb"AKIA[A-Z0-9]{12,}"), "AWS access key"),
    (re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(rb"pypi-[A-Za-z0-9_-]{20,}"), "PyPI upload token"),
)

_PEM_RE = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_CRED_URI_RE = re.compile(rb"[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]+:[^\s/:@]+@[^\s/]+")

_TOKEN_RE = re.compile(rb"[A-Za-z0-9+_=.-]{32,}")
_ENTROPY_MIN_LEN = 32
_ENTROPY_THRESHOLD = 3.5
# DoS bound: never scan more than this many bytes in one call (a state doc is
# ~2k tokens; migration copies are bounded upstream). Oversized -> caller error.
_MAX_SCAN_BYTES = 8 * 1024 * 1024


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    entropy = 0.0
    for c in counts.values():
        p = c / n
        entropy -= p * math.log2(p)
    return entropy


def _looks_like_plain_hex(tok: str) -> bool:
    """A pure hex run (sha256, git SHA, uuid-hex) — legitimate content in this
    tool, not a secret. Single-case or mixed-case hex, no other charset."""
    return re.fullmatch(r"[0-9a-fA-F]+", tok) is not None


def secret_scan(raw: bytes) -> List[str]:
    """Return LABELS ONLY for what looks like a secret in `raw`. Never the value.
    Best-effort heuristic (one of four layers), hardened against the two review
    findings: (a) it is also run on key bytes by persist(); (b) the entropy
    branch no longer flags ordinary hex hashes, which are legitimate state."""
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("secret_scan requires bytes")
    raw = bytes(raw)
    labels: List[str] = []

    for pattern, label in _PREFIX_PATTERNS:
        if pattern.search(raw):
            labels.append(label)

    if _PEM_RE.search(raw):
        labels.append("PEM private key")
    if _CRED_URI_RE.search(raw):
        labels.append("credential URI")

    for m in _TOKEN_RE.finditer(raw):
        tok = m.group()
        if len(tok) < _ENTROPY_MIN_LEN:
            continue
        try:
            text = tok.decode("ascii")
        except UnicodeDecodeError:
            continue
        # Ordinary hex hashes (sha256/git-SHA/uuid) are legitimate content in a
        # secret-safe memory tool (its own version_hash is 64-hex). Do not flag
        # them, or the tool would refuse its own state docs. A real hex secret
        # is left to layers 2/3 (resume scan + gitleaks) — documented tradeoff.
        if _looks_like_plain_hex(text):
            continue
        # Require mixed case (a real random token typically has both) so a
        # single-case hash-like run does not trip the heuristic.
        has_upper = any("A" <= c <= "Z" for c in text)
        has_lower = any("a" <= c <= "z" for c in text)
        if not (has_upper and has_lower):
            continue
        if _shannon_entropy(text) >= _ENTROPY_THRESHOLD:
            labels.append("high-entropy token")
            break

    return labels


# --------------------------------------------------------------------------
# The one write door
# --------------------------------------------------------------------------

_HASH_RE = re.compile(r"[0-9a-f]{64}")  # used with fullmatch


def _is_valid_hash(h: object) -> bool:
    return isinstance(h, str) and _HASH_RE.fullmatch(h) is not None


def persist(
    backend,
    key: str,
    raw_bytes: bytes,
    *,
    expected_hash: Optional[str],
    doc_type: str,
) -> WriteResult:
    """The gate's single write entry point (contract §1/§5/§6). Every step
    happens before any I/O."""
    # 1. expected_hash shape
    if expected_hash is not None and not _is_valid_hash(expected_hash):
        return ERROR(ErrorKind.INVALID_ARGUMENT)

    # 2. doc_type must be a string
    if not isinstance(doc_type, str):
        return ERROR(ErrorKind.INVALID_ARGUMENT)

    # 3. key shape (incl. length caps, NTFS trailing dot/space)
    if not isinstance(key, str) or not _valid_key_shape(key):
        return ERROR(ErrorKind.INVALID_ARGUMENT)

    if not isinstance(raw_bytes, (bytes, bytearray)):
        return ERROR(ErrorKind.INVALID_ARGUMENT)
    raw_bytes = bytes(raw_bytes)
    if len(raw_bytes) > _MAX_SCAN_BYTES:
        return ERROR(ErrorKind.INVALID_ARGUMENT)

    # 4. doc-type allowlist / ABA guard (§6)
    if doc_type not in STATE_DOC_TYPES:
        if _extract_generation(raw_bytes) is None:
            return ERROR(ErrorKind.INVALID_ARGUMENT)

    # 5. secret scan of BOTH key and body — fail closed (review finding #1)
    try:
        labels = list(secret_scan(key.encode("utf-8"))) + list(secret_scan(raw_bytes))
    except Exception:
        return ERROR(ErrorKind.SCAN_FAILURE)
    if labels:
        # dedupe while preserving order
        seen = []
        for l in labels:
            if l not in seen:
                seen.append(l)
        return ERROR(ErrorKind.SECRET_BLOCKED, labels=tuple(seen))

    # 6. wrap and call the adapter
    nonce = secrets.token_bytes(16)
    scanned_key = ScannedKey(_SENTINEL, key, nonce)
    scanned_body = ScannedBody(_SENTINEL, raw_bytes, nonce)
    return backend.write(scanned_key, scanned_body, expected_hash=expected_hash)


# --------------------------------------------------------------------------
# Uncertain-outcome reconciliation (contract §7)
# --------------------------------------------------------------------------


def reconcile(
    backend,
    key: str,
    *,
    intended_new_hash: str,
    expected_hash: Optional[str],
    retry: Optional[Callable[[], WriteResult]] = None,
) -> WriteResult:
    """Resolve a TIMEOUT_AFTER_COMMIT / CONFLICT_UNKNOWN outcome per §7.

    - `intended_new_hash` MUST be a valid 64-hex hash (never None) — otherwise
      a None==None coincidence could report a false OK for a key that does not
      exist (review finding #3).
    - current == intended_new_hash          -> OK (our write landed).
    - current == expected_hash              -> the store still holds the pre-write
      value, so our write did not land; run the caller's single retry and return
      ITS real result unchanged (OK / STALE / EXISTS / ERROR). A STALE here is a
      TRUE stale (the retry lost a fresh race), not a false claim about the
      original — so the "never a false STALE" rule is preserved.
    - otherwise (superseded / indeterminate) -> CONFLICT_UNKNOWN, never STALE.
    Read/retry exceptions are wrapped as CONFLICT_UNKNOWN.
    """
    if not _is_valid_hash(intended_new_hash):
        return ERROR(ErrorKind.INVALID_ARGUMENT)
    if expected_hash is not None and not _is_valid_hash(expected_hash):
        return ERROR(ErrorKind.INVALID_ARGUMENT)

    try:
        blob = backend.read(key)
    except Exception:
        return ERROR(ErrorKind.CONFLICT_UNKNOWN)
    current_hash = blob.version_hash if blob is not None else None

    if current_hash is not None and current_hash == intended_new_hash:
        return OK(intended_new_hash)

    if expected_hash is not None and current_hash == expected_hash:
        if retry is not None:
            try:
                return retry()  # the retry's real, actionable result
            except Exception:
                return ERROR(ErrorKind.CONFLICT_UNKNOWN)
        return ERROR(ErrorKind.CONFLICT_UNKNOWN)

    return ERROR(ErrorKind.CONFLICT_UNKNOWN)


__all__ = [
    "ScannedKey",
    "ScannedBody",
    "verify",
    "verify_pair",
    "KEY_RE",
    "STATE_DOC_TYPES",
    "secret_scan",
    "persist",
    "reconcile",
    "make_generation_header",
]
