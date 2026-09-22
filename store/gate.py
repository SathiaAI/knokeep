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
import unicodedata
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

STATE_DOC_TYPES = {"system_state", "session_log", "HANDOFF", "journal", "conflict"}
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
_CRED_URI_RE = re.compile(rb"[A-Za-z][A-Za-z0-9+.-]{0,31}://[^\s/:@]+:[^\s/:@]+@[^\s/]+")

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


# --------------------------------------------------------------------------
# Superset coverage (V2.1 unify). The skill's write door moved from the V1
# gate (knokeep_secretgate) onto THIS gate; the byte pass above was NARROWER
# than the V1 gate, so these patterns — ported verbatim from the V1 gate that
# tests/test_secretgate.py already pins — are added as a TEXT pass so the one
# store gate is a proven superset of both scanners. It can only ADD labels; it
# never runs the entropy heuristic (the byte pass owns the hex-safe check) and
# never returns a matched value. Reviewed under the T-6 adversarial gate.
# --------------------------------------------------------------------------

_ZW_TRANS = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)

def _normalize_text_str(text: str) -> str:
    # NFKC fold, then drop ALL Unicode format (Cf) chars — not just the five in
    # _ZW_TRANS — so an invisible char (bidi override, soft hyphen, variation
    # selector, ...) cannot split a token in the normalized scan view (T-6 r3).
    folded = unicodedata.normalize("NFKC", text)
    return "".join(c for c in folded if unicodedata.category(c) != "Cf")

def _normalize_bytes(raw: bytes):
    """NFKC + zero-width-strip view of `raw`, re-encoded to utf-8, or None if raw
    is not valid utf-8 or the normalized view is unchanged. Scanning this view in
    addition to raw gives EVERY family the same ZW/fullwidth anti-evasion
    (T-6 round-2 finding: normalization must not be applied to added families only)."""
    try:
        norm = _normalize_text_str(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return None
    nb = norm.encode("utf-8")
    return nb if nb != raw else None

_ALLOWED_C0 = frozenset({0x09, 0x0A, 0x0D})

def _is_acceptable_text(raw: bytes) -> bool:
    """Write-door content contract (T-6 round 2): a KnoKeep doc is valid utf-8
    text with no NUL / C0 control (except tab/newline/CR). Refusing anything else
    in persist() closes the 'encode a secret in UTF-16/invalid-utf-8 to skip the
    text-family scan' bypass at the source rather than scanning every encoding."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    # str.isprintable() is False for C0/C1 controls, DEL, line/paragraph
    # separators, and ALL Unicode format (Cf) chars — zero-width, bidi overrides
    # (U+202E/U+2066), soft hyphen (U+00AD), etc. A category rule (not a five-char
    # denylist) refuses every invisible smuggling char at the source (T-6 r3);
    # tab / newline / CR are the only non-printables allowed.
    return all(c.isprintable() or ord(c) in _ALLOWED_C0 for c in text)

# Added families as BYTE patterns (length-bounded, no ReDoS) so they run on the
# raw bytes AND the normalized view, exactly like the original _PREFIX_PATTERNS.
_ADDED_PREFIX_PATTERNS: Sequence[tuple] = (
    (re.compile(rb"sk-(?:proj|svcacct)-[A-Za-z0-9_-]{6,200}"), "openai project/service key"),
    (re.compile(rb"gh[osur]_[A-Za-z0-9]{20,255}"), "github token"),
    (re.compile(rb"hf_[A-Za-z0-9]{20,255}"), "huggingface token"),
    (re.compile(rb"ya29\.[A-Za-z0-9_-]{10,512}"), "google oauth token"),
    (re.compile(rb"AIza[A-Za-z0-9_-]{20,200}"), "google api key"),
    (re.compile(rb"ASIA[A-Z0-9]{12,200}"), "AWS temp key"),
    (re.compile(rb"whsec_[A-Za-z0-9]{16,200}"), "stripe webhook secret"),
    (re.compile(rb"npm_[A-Za-z0-9]{30,200}"), "npm token"),
    (re.compile(rb"(?:sk|rk)_test_[A-Za-z0-9]{10,200}"), "stripe-style test key"),
    (re.compile(rb"eyJ[A-Za-z0-9_-]{10,1024}\.[A-Za-z0-9_-]{10,1024}\.[A-Za-z0-9_-]{4,1024}"), "JWT"),
    (re.compile(rb"xoxe-[A-Za-z0-9-]{8,200}"), "slack app token"),
)

_ALL_PREFIX_PATTERNS: Sequence[tuple] = tuple(_PREFIX_PATTERNS) + _ADDED_PREFIX_PATTERNS

# key (optionally quoted) :/= value (optionally quoted) — env, JSON/YAML, ODBC Pwd=
# Assignment scan: word-boundary-anchored key (no 'compass' -> 'pass'), and the
# VALUE is gated by _value_is_credential_like so 'token: see README' does not
# refuse a legitimate write. Value groups are length-bounded (no ReDoS).
_ASSIGN_KEY = (
    r"password|passwd|passphrase|pwd|secret|api[_\- ]?key|access[_\- ]?key|"
    r"secret[_\- ]?key|auth[_\- ]?token|token|credential|client[_\- ]?secret|private[_\- ]?key"
)
_ASSIGN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])[\"']?(" + _ASSIGN_KEY + r")[\"']?\s*[:=]\s*"
    r"(?:\"([^\"\n]{1,4096})\"|'([^'\n]{1,4096})'|([^\s\"';,}]{1,4096}))"
)
_PLACEHOLDER_RE = re.compile(
    r"(?i)^(?:<[^>]*>|\{\{?[^}]*\}?\}|x{3,}|\*{3,}|none|null|nil|true|false|yes|no|"
    r"enabled|disabled|tbd|todo|changeme|example|redacted|value|placeholder|"
    r"your[_\-]?\w+|not[_\- ]?stored|in[_\- ]?\.?env|n/?a|omitted|\.\.\.)$"
)
_ENV_REF_RE = re.compile(r"^(?:\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|%[A-Za-z_][A-Za-z0-9_]*%)$")
_GETENV_RE = re.compile(r"(?i)^(?:os\.getenv\(|getenv\(|process\.env\b|import\.meta\.env\b)")
# a getenv/environ call carrying a quoted string literal fallback -> the fallback
# is a hard-coded secret; do NOT exempt it (round-2 finding: getenv-with-literal).
_GETENV_LITERAL_RE = re.compile(r"(?i)(?:getenv|environ\.get)\s*\([^)\n]*,\s*[\"'][^\"'\n]{4,}[\"']")
_BEARER_RE = re.compile(
    r"(?i)authoriz(?:ation|e)[\"']?\s*[:=]\s*[\"']?\s*(?:bearer|basic)\s+[A-Za-z0-9+/=._-]{8,4096}"
)


def _value_is_credential_like(val: str, quoted: bool) -> bool:
    """Round-2 policy: a keyword assignment is a secret only when the VALUE looks
    like a credential — a known prefix, or a compact high-entropy token — so
    'token: see README' / 'compass: north' / 'API key: not provided' stay writable
    while 'password = hunter2plaintextvalue' and 'api_key=sk-ant-...' do not."""
    if not val or _PLACEHOLDER_RE.match(val) or _ENV_REF_RE.match(val):
        return False
    b = val.encode("utf-8", "ignore")
    for pat, _lbl in _ALL_PREFIX_PATTERNS:      # a known secret prefix is decisive
        if pat.search(b):
            return True
    ent = _shannon_entropy(val)
    mixed = any(c.isupper() for c in val) and any(c.islower() for c in val)
    has_digit = any(c.isdigit() for c in val)
    if len(val) >= 12 and ent >= 3.5 and (mixed or has_digit) and not _looks_like_plain_hex(val):
        return True
    if quoted and len(val) >= 8 and ent >= 3.3 and (mixed or has_digit):
        return True
    return False


def _scan_assignments(text: str) -> List[str]:
    labels: List[str] = []
    for m in _ASSIGN_RE.finditer(text):
        qv = m.group(2) if m.group(2) is not None else m.group(3)
        quoted = qv is not None
        val = qv if quoted else m.group(4)
        if val is None:
            continue
        if not quoted and _GETENV_RE.search(val):
            _end = text.find(")", m.end())               # scope to THIS getenv call's parens,
            _seg = text[m.start():(_end + 1 if _end != -1 else m.end() + 256)]  # not a fixed window
            if _GETENV_LITERAL_RE.search(_seg):
                labels.append("secret assignment (getenv-fallback)")
                break
            continue
        if _value_is_credential_like(val, quoted):
            labels.append("secret assignment (" + m.group(1).lower().replace(" ", "") + ")")
            break
    return labels


def _scan_normalized_text(text: str) -> List[str]:
    """ASSIGN + Authorization scanning over a normalized text view. Added prefix
    families are scanned at the byte level (raw + normalized) inside secret_scan."""
    labels: List[str] = []
    if _BEARER_RE.search(text):
        labels.append("authorization header token")
    labels.extend(_scan_assignments(text))
    return labels


def secret_scan(raw: bytes) -> List[str]:
    """Return LABELS ONLY for what looks like a secret in `raw`. Never the value.
    Best-effort heuristic (one of four layers), hardened against the two review
    findings: (a) it is also run on key bytes by persist(); (b) the entropy
    branch no longer flags ordinary hex hashes, which are legitimate state."""
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("secret_scan requires bytes")
    raw = bytes(raw)
    if len(raw) > _MAX_SCAN_BYTES:                     # fail-closed in-function (T-6 round 2)
        raise ValueError("secret_scan: input exceeds _MAX_SCAN_BYTES")
    labels: List[str] = []

    # Byte pass over the raw bytes AND the NFKC + zero-width-normalized view, for
    # ALL families (original + added) so a zero-width / fullwidth trick cannot skip
    # any of them (T-6 round 2).
    _views = [raw]
    _norm_view = _normalize_bytes(raw)
    if _norm_view is not None:
        _views.append(_norm_view)
    if b"\x00" in raw:                                 # byte-preserving recovery of a UTF-16/32-
        _views.append(raw.replace(b"\x00", b""))       # encoded ASCII token (defense-in-depth; persist
                                                       # also refuses NUL/non-utf-8 via the accept contract)
    for _view in _views:
        for pattern, label in _ALL_PREFIX_PATTERNS:
            if pattern.search(_view):
                labels.append(label)
        if _PEM_RE.search(_view):
            labels.append("PEM private key")
        if _CRED_URI_RE.search(_view):
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

    # Assignment / Authorization scanning over the normalized text view.
    try:
        _norm_text = _normalize_text_str(raw.decode("utf-8"))
    except UnicodeDecodeError:
        _norm_text = None
    if _norm_text is not None:
        labels.extend(_scan_normalized_text(_norm_text))

    seen: List[str] = []                              # dedupe, preserve order
    for _l in labels:
        if _l not in seen:
            seen.append(_l)
    return seen


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

    # 4b. Write-door content contract (T-6 round 2): the body must be valid utf-8
    # text with no NUL / C0 control (except tab/newline/CR). This refuses
    # UTF-16 / invalid-utf-8 / NUL-laced content that could smuggle a secret past
    # the text-family scan, closing that encoding bypass at the source.
    if not _is_acceptable_text(raw_bytes):
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
