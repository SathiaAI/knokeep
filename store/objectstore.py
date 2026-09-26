"""ObjectStoreBackend — S3-protocol StoreBackend adapter (contract v1.4, §3, §4.2).

"object-store (primary remote)" per contract §4.2. Every KnoKeep key maps to
one object at `{endpoint}/{bucket}/{key}` (path-style addressing, single
pinned endpoint from config — never virtual-hosted-style, never bucket
auto-discovery). CAS is implemented entirely through the object store's OWN
native preconditions (S3 `If-None-Match` / `If-Match`), never through an
application-side lock: the contract is explicit that "native precondition
where it exists (object-store/git/postgres) is the linearization point and is
mandatory on every write" (§3).

CLIENT — the contract's "core client = signed HTTP, zero core dependency"
requirement (§4.2): `SigV4Client` below is a minimal, from-scratch AWS SigV4
request signer built on `hashlib`/`hmac` for the signature math and
`http.client` (the stdlib layer `urllib.request` itself is a thin wrapper
around) for the transport, plus `urllib.parse` for URI/query encoding. It
implements exactly the operations this adapter needs — HEAD, GET, PUT (with
conditional headers) — and nothing else (no multipart, no
ListObjectsV2-alternatives beyond what `list()` needs). `boto3` is NOT
imported anywhere in this module; it may only ever appear in test code as an
independent oracle (contract task instructions), never in the adapter.

H1 INCREMENT 2, PHASE 2 — FENCE ENVELOPE (this section documents the change;
everything above/below it that still says "JUDGMENT CALLS" from Phase 0/1 is
otherwise unchanged):

  Server-side fence enforcement (contract: reject a stale-but-hash-matching
  lease even though the pre-existing content-hash CAS alone would accept it)
  requires durable, per-key owner_token/owner_expiry/owner_fence/
  last_accepted_fence state that a `lock()` call can advance WITHOUT any
  data write, and that a `write()` call can check atomically against the
  SAME native token used for the data CAS. Object storage offers exactly one
  atomicity boundary per key: one object, one ETag, one conditional PUT.
  There is no second, independently-CAS'd side-channel (no per-key "extra
  metadata API" in the S3 protocol this adapter targets) the way git has a
  second committed path or postgres has a second table+transaction.

  So this adapter now stores ONE CANONICAL OBJECT per key whose bytes are an
  ENVELOPE — `owner_token` / `owner_expiry` / `owner_fence` /
  `last_accepted_fence` / the logical `version_hash` / the logical body — all
  in the body (never in S3 metadata, which this endpoint's HEAD cannot
  update independently of a full PUT anyway). Every mutation (`lock()`'s
  fence advance, `write()`'s CAS-update, `renew()`'s expiry bump) reads the
  current envelope (GET, which also yields the object's ETag), computes the
  new envelope, and PUTs it back conditioned on that SAME ETag via
  `If-Match` — the object store's own native CAS is still the sole
  linearization point; nothing here adds an application-side lock. `read()`
  unwraps the envelope transparently so callers never see it.

  PHANTOM OBJECTS: `lock()` must work on a key that has never been written
  (contract: a caller may acquire a lease before its first CAS-update). Since
  ownership now lives inside the one object a key has, `lock()` on an
  unwritten key creates a PHANTOM envelope — owner/fence fields populated,
  `version_hash: null`, empty logical body — via `If-None-Match: *`. A
  create-only `write()` that lands on a key already holding a phantom (some
  caller already `lock()`'d it first) does not report a false EXISTS: it
  detects the phantom (`version_hash is None`) and fills it in with the real
  body via a CAS-update-shaped PUT that preserves the phantom's owner/fence
  fields, conditioned on the phantom's own ETag.

  SETTLING A LOST FENCE RACE: because owner/fence and data share one object,
  a `write()`'s conditional PUT can be rejected by an UNRELATED concurrent
  `lock()` (a fence advance for the same key, no data change) landing first
  — not only by a conflicting data write. A single immediate re-read right
  after such a rejection can therefore still show this caller's own
  pre-race snapshot if the actual new owner's data write has not landed yet.
  `_settle_current_hash_after_fence_loss` closes that gap: it polls, bounded
  (`_FENCE_LOSS_SETTLE_MAX_S`, and never past the owner's lease expiry), for
  as long as the envelope shows a fence has been allocated (`owner_fence`)
  that no write has yet consumed (`last_accepted_fence`), and returns the
  moment either the logical hash changes or nothing is left pending — so a
  genuinely non-racing fence rejection (an old lease replayed with nobody
  else contending) returns on its very first check, while every loser of a
  real race reports the true winner's hash.

  LISTING: a phantom is not a logical key, so `list()` HEADs each physical
  object and yields only those whose `x-amz-meta-knokeep-sha256` metadata is
  non-empty (every real value: envelope or legacy) — one HEAD per listed
  object, the price of keeping fence state inside the one object a key has.

  LEGACY (pre-envelope) OBJECTS: a bucket written by the previous adapter
  holds raw bodies with that same metadata set to sha256(body). They read
  back transparently (`_read_envelope` synthesizes an unowned envelope) and
  are upgraded to envelope form by the first `lock()`/`write()` on the key,
  conditioned on the legacy object's own ETag. Envelope `version_hash` is
  verified against the body on every read (`_EnvelopeCorruptionError`).

  CAS DECISION COST: Phase 0/1's JUDGMENT CALL 4 ("a single HEAD reads the
  metadata hash AND captures the native token from the same object version")
  no longer holds: since ownership/fence state lives in the body, every CAS
  decision now requires a GET (not just a HEAD) to see it, and this is the
  ONE GET per CAS-update (never more, on the non-racing path) — HEAD is kept
  only for the load-time capability probe and `health()`, which do not need
  envelope contents.

JUDGMENT CALLS (each also called out inline at its point of use):

  1. `http.client` instead of bare `urllib.request.urlopen()`. The task names
     `urllib.request` for the client, and `http.client` is the exact stdlib
     module `urllib.request` is built on top of (no third-party dependency is
     introduced either way). The reason to call it directly rather than go
     through `urlopen()` is contract-driven, not stylistic: §1 requires this
     adapter to distinguish a failure BEFORE the request was fully sent
     (→ `ERROR{NETWORK}`, definitely-not-committed) from a failure AFTER it
     was sent but before an ack (→ `ERROR{TIMEOUT_AFTER_COMMIT}`,
     outcome-unknown) — and `urlopen()` collapses both into one
     `URLError`/`socket.timeout` with no reliable way to tell them apart
     after the fact. `http.client.HTTPConnection`/`HTTPSConnection` expose
     `connect()`, `request()` (which sends the request line/headers/body) and
     `getresponse()` (which blocks for the ack) as three distinct calls, so
     each of PUT's three phases can be wrapped separately: an exception in
     `connect()` is unambiguously pre-send (`_PreSendNetworkError` →
     `ERROR{NETWORK}`); an exception in `request()` or `getresponse()` for a
     mutating call is unambiguously post-send (`_PostSendAckLostError` →
     `ERROR{TIMEOUT_AFTER_COMMIT}`) since bytes may already be on the wire /
     the server may already have acted on them.

  2. ETag handling. §4.2 is explicit: "ETag is NOT the content hash; sha256
     in `If-Match` forbidden". This adapter never inspects, parses, or
     compares ETag bytes to anything — it is captured from one GET/HEAD
     response header and echoed back VERBATIM (quotes included, exactly as
     the server sent it) into the next PUT's `If-Match` header. It is
     treated as a fully opaque native token, by construction.

  3. Structural "no unconditional PUT" guarantee. Rather than rely on
     review/tests alone to prove every PUT carries a precondition (contract
     §4.2: "there must be no code path that PUTs without a precondition"),
     `SigV4Client.put()` declares `precondition` as a required, no-default,
     keyword-ONLY parameter. Python itself raises `TypeError` for any call
     site that omits it — the guarantee is enforced by the interpreter, not
     just by inspection. `tests/test_objectstore.py` verifies this both by
     direct construction (calling `put()` without `precondition` raises
     `TypeError`) and via an AST scan of every `put(` call site in this
     module and confirming each carries a `precondition=` keyword literally.

  4. (superseded by the envelope section above for the CAS decision itself;
     generation monotonicity is still a residual, still one extra read of
     the CURRENT envelope's own logical body — already fetched as part of
     the same GET the fence/hash decision needs, so it costs nothing extra
     now.)

  5. Fallback content-hash on a create-only conflict against a foreign
     (non-envelope) object. If a create-only PUT's `If-None-Match: *` is
     rejected (412 → EXISTS class) and the existing object cannot be parsed
     as a KnoKeep envelope at all (e.g. probe litter from another process,
     or an out-of-band write), there is no logical hash to report from
     envelope metadata. Rather than fabricate one, the adapter falls back to
     `sha256(body)` of the raw (non-envelope) bytes, ONLY on this rare,
     already-failed branch (never on the hot success path).

  6. Advisory `lock()`/`unlock()` in-process bookkeeping (`self._locks`) is
     kept identical in shape to Phase 0/1 and to `store/git_backend.py`'s
     JUDGMENT CALL 1 — contract §3: "Advisory lock() is never the CAS
     mechanism" for any backend. What Phase 2 adds is the DURABLE fence
     envelope (above), not a second advisory-lock storage.

  7. TLS verification cannot be turned off by any constructor knob. §4.2:
     "TLS verify ON by default; allow plain http ONLY when the configured
     endpoint is explicitly a localhost test endpoint". Rather than expose
     an `insecure_skip_verify` flag that a caller could flip for a real
     endpoint by accident, the ONLY way to get an unverified connection at
     all is to configure an `http://` endpoint, and that is accepted ONLY
     when the endpoint's host is `127.0.0.1` / `localhost` / `::1`
     (raises `ValueError` at construction otherwise). Every `https://`
     endpoint always gets `ssl.create_default_context()` (certificate AND
     hostname verification on, using the platform's default trust store) —
     there is no parameter anywhere that weakens this.

  8. Credentials and endpoint are explicit-config-only. There is no
     `~/.aws/credentials` file read, no `AWS_*` environment variable read
     automatically, no EC2/ECS instance-metadata (IMDS) call anywhere in
     this module, and no "try region X, then Y" resolution — the caller
     passes `endpoint`, `bucket`, `region`, `access_key_id`,
     `secret_access_key` (and optional `session_token`) directly.

  9. Multipart is not merely "disabled" by a flag — no multipart code path
     exists anywhere in this module. `SigV4Client.put()` always issues one
     single-shot `PUT` with the full body.

  10. §8 load-time capability probe. On construction (unless explicitly
      disabled for a targeted unit test), the adapter runs three requests
      against a fresh random key under the reserved prefix `.knokeep/probe/`
      — create-only (expect success), duplicate create-only (expect the
      endpoint's own 412), and a CAS-update with a deliberately wrong
      `If-Match` token (expect 412) — then deletes the probe object
      (best-effort). Any of the three checks coming back other than
      expected, or any transport failure while running them, raises
      `RuntimeError` and the adapter refuses to be constructed at all —
      "no degraded mode" (§8). This probe uses raw (non-envelope) bytes and
      is entirely independent of the envelope format above (it never goes
      through `read()`/`write()`).

  11. Test-only transport injection seam (`_client=` constructor kwarg,
      leading underscore, not part of the public API). See Phase 0/1 notes;
      unchanged in Phase 2.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import hmac
import http.client
import json
import re
import ssl
import struct
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Dict, Iterator, List, NamedTuple, Optional, Tuple
from urllib.parse import quote, urlsplit

from . import gate
from .backend import BackendBusyError, Lock
from .context import Overwrite
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
# Reserved probe prefix (contract §8)
# ---------------------------------------------------------------------------

PROBE_PREFIX = ".knokeep/probe/"

# ---------------------------------------------------------------------------
# Generation header (contract §6; same convention as store/gate.py's
# `make_generation_header` / store/local.py's / store/git_backend.py's local
# copies — re-implemented here rather than importing gate's private helper,
# same rationale as those two adapters: this adapter should not reach into
# gate.py's private surface for a convention it also independently documents).
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
# H1 Increment 2, Phase 2: the canonical per-key fence envelope. See the
# module docstring's "FENCE ENVELOPE" section for the design rationale.
# Binary layout: 4-byte magic, big-endian uint32 header length, UTF-8 JSON
# header, then the raw logical body (never re-encoded/escaped, so an
# arbitrary-bytes body round-trips exactly).
# ---------------------------------------------------------------------------

_ENVELOPE_MAGIC = b"KFE1"
_ENVELOPE_SCHEMA_VERSION = 1


class _Envelope(NamedTuple):
    owner_token: Optional[str]
    owner_expiry: float
    owner_fence: int
    last_accepted_fence: int
    version_hash: Optional[str]  # None => phantom: lock()'d, never written
    body: bytes
    etag: Optional[str]


def _encode_envelope(
    owner_token: Optional[str],
    owner_expiry: float,
    owner_fence: int,
    last_accepted_fence: int,
    version_hash: Optional[str],
    body: bytes,
) -> bytes:
    header = {
        "schema_version": _ENVELOPE_SCHEMA_VERSION,
        "owner_token": owner_token,
        "owner_expiry": owner_expiry,
        "owner_fence": owner_fence,
        "last_accepted_fence": last_accepted_fence,
        "version_hash": version_hash,
    }
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return _ENVELOPE_MAGIC + struct.pack(">I", len(header_bytes)) + header_bytes + body


class _EnvelopeCorruptionError(ValueError):
    """An object that IS a KnoKeep envelope (magic + parseable header) but
    whose header `version_hash` does not describe its logical body — a
    truncated, corrupted or out-of-band-modified object. Distinguished from
    a plain "not an envelope" ValueError so write paths can report
    ERROR{CORRUPTION} rather than treat it as a foreign object."""


def _decode_envelope(raw: bytes) -> Tuple[dict, bytes]:
    """Raises ValueError if `raw` is not a KnoKeep fence envelope (e.g. probe
    litter or another out-of-band object) — callers decide how to handle
    that per call site (never silently treated as a real key's data)."""
    if raw[:4] != _ENVELOPE_MAGIC:
        raise ValueError("not a KnoKeep fence envelope (bad magic)")
    if len(raw) < 8:
        raise ValueError("not a KnoKeep fence envelope (truncated)")
    (header_len,) = struct.unpack_from(">I", raw, 4)
    header_start = 8
    header_end = header_start + header_len
    if header_end > len(raw):
        raise ValueError("not a KnoKeep fence envelope (truncated header)")
    header = json.loads(raw[header_start:header_end].decode("utf-8"))
    body = raw[header_end:]
    return header, body


# ---------------------------------------------------------------------------
# Adapter-visible exceptions
# ---------------------------------------------------------------------------


class ObjectStoreBackendError(Exception):
    """PERMISSION/NETWORK/CORRUPTION raised from read()/list() (contract §2)."""


class _PreSendNetworkError(Exception):
    """Failed before the request was fully sent — definitely-not-committed
    (contract §1: "NETWORK ... when it failed before the request was fully
    sent")."""


class _PostSendAckLostError(Exception):
    """Failed after the request was sent (headers/body on the wire, or while
    waiting for the response) but before an ack was received —
    outcome-unknown for a mutating call (contract §1: "A NETWORK failure
    after the request was sent but before ack is reported as
    TIMEOUT_AFTER_COMMIT")."""


class _ObjectStoreTransportError(Exception):
    """An HTTP response was received but its status could not be classified
    by the caller (e.g. an unexpected status for that operation)."""


# ---------------------------------------------------------------------------
# The signed-HTTP client (contract §4.2 "core client = signed HTTP")
# ---------------------------------------------------------------------------


class _HeadResult:
    __slots__ = ("meta_hash", "etag")

    def __init__(self, meta_hash: Optional[str], etag: Optional[str]) -> None:
        self.meta_hash = meta_hash
        self.etag = etag


def _local_tag(tag: str) -> str:
    """Strip an XML namespace URI wrapper (`{ns}Tag` -> `Tag`) — S3 XML
    responses use a default namespace and stdlib ElementTree does not offer
    namespace-agnostic tag matching."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


class SigV4Client:
    """Minimal stdlib AWS SigV4 signed-HTTP client for path-style S3-protocol
    endpoints. Implements exactly HEAD, GET, PUT (conditional), DELETE (probe
    cleanup only) and ListObjectsV2 (for `list()`). No multipart. No
    third-party dependency of any kind.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        region: str,
        access_key_id: str,
        secret_access_key: str,
        session_token: Optional[str] = None,
        timeout_s: float = 30.0,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"endpoint must be http:// or https://, got {endpoint!r}")
        host = parsed.hostname
        if not host:
            raise ValueError(f"endpoint has no host: {endpoint!r}")

        # JUDGMENT CALL 7: plain http permitted ONLY for an explicit
        # localhost test endpoint; https always gets certificate + hostname
        # verification on, with no knob anywhere to weaken it.
        if parsed.scheme == "http":
            if host not in ("127.0.0.1", "localhost", "::1"):
                raise ValueError(
                    "ObjectStoreBackend: plain http endpoint is only permitted for an "
                    "explicit localhost test endpoint (127.0.0.1/localhost/::1); "
                    f"got host={host!r}. Configure an https:// endpoint for anything else."
                )
            self._ssl_context: Optional[ssl.SSLContext] = None
        else:
            self._ssl_context = ssl.create_default_context()

        if not bucket or not re.match(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", bucket):
            raise ValueError(f"bucket must be a DNS-valid S3 bucket name, got {bucket!r}")
        if not access_key_id or not secret_access_key:
            raise ValueError("ObjectStoreBackend requires explicit access_key_id/secret_access_key")

        self._scheme = parsed.scheme
        self._host = host
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._bucket = bucket
        self._region = region
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._session_token = session_token
        self._timeout_s = timeout_s

        # Call-count instrumentation (tests assert on this — e.g. "exactly
        # one GET" per non-racing CAS-update — never used for adapter logic
        # itself).
        self.head_count = 0
        self.get_count = 0
        self.put_count = 0
        self.delete_count = 0

    @property
    def bucket_name(self) -> str:
        return self._bucket

    def describe(self) -> str:
        return f"{self._scheme}://{self._host}:{self._port}/{self._bucket}"

    # -- SigV4 signing -------------------------------------------------------

    def _host_header(self) -> str:
        default_port = 443 if self._scheme == "https" else 80
        if self._port == default_port:
            return self._host
        return f"{self._host}:{self._port}"

    @staticmethod
    def _canonical_uri(path: str) -> str:
        # S3 SigV4 URI-encodes each path segment but never encodes '/'.
        return quote(path, safe="/-_.~")

    @staticmethod
    def _canonical_query(query: Optional[Dict[str, str]]) -> str:
        if not query:
            return ""
        items = sorted(query.items())
        return "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in items)

    @staticmethod
    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    def _signing_key(self, datestamp: str) -> bytes:
        k_date = self._hmac(("AWS4" + self._secret_access_key).encode("utf-8"), datestamp)
        k_region = self._hmac(k_date, self._region)
        k_service = self._hmac(k_region, "s3")
        return self._hmac(k_service, "aws4_request")

    def _sign(
        self,
        method: str,
        canonical_uri: str,
        canonical_qs: str,
        headers_to_sign: Dict[str, str],
        payload_hash: str,
        amzdate: str,
        datestamp: str,
    ) -> str:
        signed_names = sorted(headers_to_sign.keys())
        canonical_headers = "".join(f"{h}:{headers_to_sign[h].strip()}\n" for h in signed_names)
        signed_headers = ";".join(signed_names)
        canonical_request = "\n".join(
            [method, canonical_uri, canonical_qs, canonical_headers, signed_headers, payload_hash]
        )
        credential_scope = f"{datestamp}/{self._region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amzdate,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signing_key = self._signing_key(datestamp)
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        return (
            f"AWS4-HMAC-SHA256 Credential={self._access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

    # -- transport: connect() / request() / getresponse() kept as three
    #    separate calls so pre-send vs post-send failures are distinguishable
    #    (JUDGMENT CALL 1) --------------------------------------------------

    def _request(
        self,
        method: str,
        key: Optional[str],
        *,
        body: Optional[bytes] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        query: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, "http.client.HTTPMessage", bytes]:
        path = f"/{self._bucket}/{key}" if key is not None else f"/{self._bucket}"
        canonical_uri = self._canonical_uri(path)
        canonical_qs = self._canonical_query(query)

        payload = body if body is not None else b""
        payload_hash = hashlib.sha256(payload).hexdigest()

        now = datetime.datetime.now(datetime.timezone.utc)
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")

        headers_to_sign: Dict[str, str] = {
            "host": self._host_header(),
            "x-amz-date": amzdate,
            "x-amz-content-sha256": payload_hash,
        }
        if self._session_token:
            headers_to_sign["x-amz-security-token"] = self._session_token
        unsigned_extra: Dict[str, str] = {}
        if extra_headers:
            for k, v in extra_headers.items():
                lk = k.lower()
                if lk.startswith("x-amz-meta-"):
                    headers_to_sign[lk] = v
                else:
                    unsigned_extra[k] = v

        auth = self._sign(method, canonical_uri, canonical_qs, headers_to_sign, payload_hash, amzdate, datestamp)

        wire_headers: Dict[str, str] = dict(headers_to_sign)
        wire_headers["Authorization"] = auth
        wire_headers.update(unsigned_extra)

        full_path = canonical_uri + (("?" + canonical_qs) if canonical_qs else "")

        if self._scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                self._host, self._port, timeout=self._timeout_s, context=self._ssl_context
            )
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout_s)

        is_mutating = method in ("PUT", "DELETE")
        try:
            try:
                conn.connect()  # phase 1: nothing sent yet
            except OSError as e:
                raise _PreSendNetworkError(f"{method} {path}: connect failed: {e}") from e

            try:
                conn.request(
                    method, full_path, body=payload if method == "PUT" else None, headers=wire_headers
                )  # phase 2: request line/headers/body go on the wire
            except OSError as e:
                if is_mutating:
                    raise _PostSendAckLostError(f"{method} {path}: send failed mid-flight: {e}") from e
                raise _PreSendNetworkError(f"{method} {path}: send failed: {e}") from e

            try:
                resp = conn.getresponse()  # phase 3: wait for the ack
                resp_body = resp.read()
            except (http.client.HTTPException, OSError) as e:
                if is_mutating:
                    raise _PostSendAckLostError(f"{method} {path}: no ack: {e}") from e
                raise _PreSendNetworkError(f"{method} {path}: no response: {e}") from e

            return resp.status, resp.headers, resp_body
        finally:
            conn.close()

    # -- high-level operations ------------------------------------------------

    def head(self, key: str) -> Optional[_HeadResult]:
        self.head_count += 1
        status, headers, _body = self._request("HEAD", key)
        if status == 404:
            return None
        if status == 200:
            etag = headers.get("ETag") or headers.get("etag")
            meta_hash = headers.get("x-amz-meta-knokeep-sha256")
            return _HeadResult(meta_hash=meta_hash, etag=etag)
        raise _ObjectStoreTransportError(f"HEAD {key}: unexpected status {status}")

    def get(self, key: str) -> Optional[bytes]:
        self.get_count += 1
        status, _headers, body = self._request("GET", key)
        if status == 404:
            return None
        if status == 200:
            return body
        raise _ObjectStoreTransportError(f"GET {key}: unexpected status {status}")

    def get_with_etag(self, key: str) -> Optional[Tuple[bytes, Optional[str], Optional[str]]]:
        """H1 Increment 2, Phase 2: like `get()`, but also returns the
        object's ETag from the SAME response — needed because fence/owner
        state now lives in the body, so every CAS decision must read the
        full object (not just HEAD), and the CAS-update PUT that follows
        must condition on the ETag of the EXACT version just read. The
        third element is the `x-amz-meta-knokeep-sha256` metadata (None if
        absent): it is what tells a LEGACY pre-envelope value (written by the
        previous adapter as the raw body + this metadata) apart from foreign
        litter -- see `ObjectStoreBackend._read_envelope`."""
        self.get_count += 1
        status, headers, body = self._request("GET", key)
        if status == 404:
            return None
        if status == 200:
            etag = headers.get("ETag") or headers.get("etag")
            meta_hash = headers.get("x-amz-meta-knokeep-sha256")
            return body, etag, meta_hash
        raise _ObjectStoreTransportError(f"GET {key}: unexpected status {status}")

    def put(
        self,
        key: str,
        body: bytes,
        *,
        precondition: Dict[str, str],
        meta_hash: str,
    ) -> Tuple[int, "http.client.HTTPMessage"]:
        """Issue a single PUT. `precondition` is REQUIRED and has no default
        (JUDGMENT CALL 3): it must be exactly one of
        `{"If-None-Match": "*"}` (create-only) or
        `{"If-Match": "<etag captured from a prior read, verbatim>"}`
        (CAS-update) — there is no way to call this method and skip sending
        a conditional header at all.
        """
        if not precondition or not (
            "If-None-Match" in precondition or "If-Match" in precondition
        ):
            # Defensive: a precondition dict was supplied but doesn't
            # actually contain a recognized conditional header. Refuse
            # rather than silently issue an unconditional PUT.
            raise ValueError(
                "SigV4Client.put: precondition must contain 'If-None-Match' or 'If-Match'"
            )
        self.put_count += 1
        extra = dict(precondition)
        extra["x-amz-meta-knokeep-sha256"] = meta_hash
        status, headers, _resp_body = self._request("PUT", key, body=body, extra_headers=extra)
        return status, headers

    def delete(self, key: str) -> int:
        """Probe cleanup only — NOT part of the StoreBackend protocol."""
        self.delete_count += 1
        status, _headers, _body = self._request("DELETE", key)
        return status

    def list_objects(
        self, prefix: str, *, continuation_token: Optional[str] = None
    ) -> Tuple[List[str], Optional[str]]:
        query = {"list-type": "2", "prefix": prefix}
        if continuation_token:
            query["continuation-token"] = continuation_token
        status, _headers, body = self._request("GET", None, query=query)
        if status != 200:
            raise _ObjectStoreTransportError(f"ListObjectsV2 prefix={prefix!r}: unexpected status {status}")
        root = ET.fromstring(body)
        keys: List[str] = []
        next_token: Optional[str] = None
        is_truncated = False
        for child in root:
            name = _local_tag(child.tag)
            if name == "Contents":
                for gc in child:
                    if _local_tag(gc.tag) == "Key":
                        keys.append(gc.text or "")
            elif name == "NextContinuationToken":
                next_token = child.text
            elif name == "IsTruncated":
                is_truncated = (child.text or "").strip().lower() == "true"
        return keys, (next_token if is_truncated else None)


# ---------------------------------------------------------------------------
# ObjectStoreBackend — the StoreBackend adapter
# ---------------------------------------------------------------------------


class ObjectStoreBackend:
    """S3-protocol object-store StoreBackend adapter. See module docstring."""

    _FENCE_RETRY_ATTEMPTS = 50
    _FENCE_LOSS_SETTLE_POLL_S = 0.01
    # Upper bound on how long a fence-losing write() waits for the current
    # fence owner's pending write to land before reporting STALE (see
    # _settle_current_hash_after_fence_loss); the wait also ends as soon as
    # the owner's lease expires, since no fenced write can land after that.
    _FENCE_LOSS_SETTLE_MAX_S = 5.0

    def __init__(
        self,
        *,
        endpoint: Optional[str] = None,
        bucket: Optional[str] = None,
        region: str = "us-east-1",
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        session_token: Optional[str] = None,
        timeout_s: float = 30.0,
        run_capability_probe: bool = True,
        _client: Optional["SigV4Client"] = None,
    ) -> None:
        # JUDGMENT CALL 11: `_client` is a private, test-only seam for
        # injecting a stub transport (e.g. one that never enforces
        # preconditions, to exercise the §8 REFUSE-TO-START path without a
        # real non-conforming S3 endpoint). Production callers never pass it.
        if _client is not None:
            self._client = _client
        else:
            if not endpoint or not bucket or not access_key_id or not secret_access_key:
                raise ValueError(
                    "ObjectStoreBackend requires endpoint, bucket, access_key_id, "
                    "secret_access_key (JUDGMENT CALL 8: no default credential chain, "
                    "no config-file/IMDS lookup — pass them explicitly)"
                )
            self._client = SigV4Client(
                endpoint=endpoint,
                bucket=bucket,
                region=region,
                access_key_id=access_key_id,
                secret_access_key=secret_access_key,
                session_token=session_token,
                timeout_s=timeout_s,
            )

        self._lock_mutex = threading.Lock()
        self._locks: Dict[str, Tuple[str, float]] = {}

        if run_capability_probe:
            self._run_capability_probe()

    # -- §8 load-time capability probe --------------------------------------
    # Raw (non-envelope) bytes on purpose: this probes the ENDPOINT's own
    # precondition enforcement, entirely independent of this adapter's
    # envelope format above.

    def _run_capability_probe(self) -> None:
        probe_key = f"{PROBE_PREFIX}{uuid.uuid4().hex}"
        try:
            self._probe_steps(probe_key)
        except Exception as e:
            raise RuntimeError(
                f"ObjectStoreBackend: capability probe against "
                f"{getattr(self._client, 'describe', lambda: '<stub>')()} failed — "
                f"REFUSING TO START per contract §8 (no degraded mode): {e}"
            ) from e
        finally:
            with contextlib.suppress(Exception):
                self._client.delete(probe_key)

    def _probe_steps(self, probe_key: str) -> None:
        status1, _ = self._client.put(
            probe_key, b"probe-create", precondition={"If-None-Match": "*"},
            meta_hash=sha256_hex(b"probe-create"),
        )
        if status1 not in (200, 201):
            raise RuntimeError(f"create-only PUT returned {status1}, expected 200/201")

        status2, _ = self._client.put(
            probe_key, b"probe-duplicate", precondition={"If-None-Match": "*"},
            meta_hash=sha256_hex(b"probe-duplicate"),
        )
        if status2 != 412:
            raise RuntimeError(
                f"duplicate create-only PUT returned {status2}, expected 412 "
                "(endpoint does not enforce If-None-Match)"
            )

        head = self._client.head(probe_key)
        if head is None or not head.etag:
            raise RuntimeError("HEAD after create-only PUT returned no ETag")

        status3, _ = self._client.put(
            probe_key,
            b"probe-stale-cas",
            precondition={"If-Match": '"0000000000000000000000000000000"'},
            meta_hash=sha256_hex(b"probe-stale-cas"),
        )
        if status3 != 412:
            raise RuntimeError(
                f"CAS-update PUT with a wrong If-Match returned {status3}, expected 412 "
                "(endpoint does not enforce If-Match)"
            )

    # -- StoreBackend protocol -----------------------------------------------

    def capabilities(self) -> Caps:
        # H1 Increment 2, Phase 2: this backend now enforces lock()-issued
        # fence ordering on every Overwrite CAS-update via the canonical
        # per-key fence envelope (see module docstring).
        return Caps(atomic=True, cas=True, lock=True, durable=True, remote=True, fence=True)

    def health(self) -> BackendHealth:
        try:
            self._client.head(f"{PROBE_PREFIX}healthcheck-{uuid.uuid4().hex}")
            return BackendHealth(ok=True, detail=f"object-store backend at {self._client.describe()}")
        except Exception as e:  # noqa: BLE001 - health() reports, never raises
            return BackendHealth(ok=False, detail=str(e))

    # -- H1 Increment 2, Phase 2: canonical fence envelope I/O --------------

    def _read_envelope(self, key: str) -> Optional[_Envelope]:
        """Read+parse the canonical envelope for `key`. Returns None only
        when the object truly does not exist (never created, or a phantom
        that was somehow removed out-of-band).

        BACKWARD COMPATIBILITY: an object written by the pre-envelope
        adapter is the raw logical body with `x-amz-meta-knokeep-sha256 ==
        sha256(body)` (that adapter set the metadata on every PUT). Such a
        LEGACY object is returned as an envelope with empty owner/fence
        fields and `version_hash = sha256(raw)`; the first fenced mutation
        (`lock()`/`write()`) then rewrites it in envelope form, conditioned
        on the legacy object's own ETag. A non-envelope object WITHOUT that
        matching metadata is foreign/probe litter -> ValueError (callers
        decide: `read()` treats it as corruption, `_resolve_create_conflict`
        falls back to a raw content hash).

        INTEGRITY: an envelope's header `version_hash` is verified against
        its logical body on every read; a mismatch raises
        `_EnvelopeCorruptionError` (a ValueError) so a torn/corrupted/
        out-of-band-modified object is never reported as a valid Blob whose
        hash a later CAS could accept."""
        got = self._client.get_with_etag(key)
        if got is None:
            return None
        raw, etag, meta_hash = got
        try:
            header, body = _decode_envelope(raw)
        except ValueError:
            raw_hash = sha256_hex(raw)
            if meta_hash is not None and meta_hash == raw_hash:
                return _Envelope(
                    owner_token=None, owner_expiry=0.0, owner_fence=0,
                    last_accepted_fence=0, version_hash=raw_hash, body=raw, etag=etag,
                )
            raise
        version_hash = header.get("version_hash")
        if version_hash is not None and version_hash != sha256_hex(body):
            raise _EnvelopeCorruptionError(
                f"envelope version_hash does not match its body at {key!r}"
            )
        return _Envelope(
            owner_token=header.get("owner_token"),
            owner_expiry=float(header.get("owner_expiry") or 0.0),
            owner_fence=int(header.get("owner_fence") or 0),
            last_accepted_fence=int(header.get("last_accepted_fence") or 0),
            version_hash=header.get("version_hash"),
            body=body,
            etag=etag,
        )

    def read(self, key: str) -> Optional[Blob]:
        try:
            env = self._read_envelope(key)
        except (_PreSendNetworkError, _PostSendAckLostError, _ObjectStoreTransportError) as e:
            raise ObjectStoreBackendError(str(e)) from e
        except ValueError as e:
            # Foreign/probe litter, or an envelope whose hash does not
            # describe its body: CORRUPTION raises (contract §2), never a
            # Blob with a hash a later CAS could accept.
            raise ObjectStoreBackendError(f"corrupt/non-envelope object at {key!r}: {e}") from e
        if env is None or env.version_hash is None:
            # Absent, or a phantom: lock()'d but never written — logically
            # absent either way.
            return None
        return Blob(body=env.body, version_hash=env.version_hash)

    def list(self, prefix: str) -> Iterator[str]:
        """Logical keys only. A key that has only ever been `lock()`'d holds
        a PHANTOM envelope (fence state, no data) that `read()` reports as
        absent, and foreign/probe litter is not KnoKeep data at all; neither
        may be listed as a key (a caller -- e.g. a migration using this
        backend as its source -- would otherwise be handed keys that then
        read back as None). ListObjectsV2 carries no metadata, so each
        physical object costs one HEAD: the `x-amz-meta-knokeep-sha256`
        metadata is the logical hash for every real value (envelope or
        legacy pre-envelope object) and empty for a fence-only PUT."""
        def _iter() -> Iterator[str]:
            token: Optional[str] = None
            while True:
                try:
                    keys, token = self._client.list_objects(prefix, continuation_token=token)
                except (_PreSendNetworkError, _PostSendAckLostError, _ObjectStoreTransportError) as e:
                    raise ObjectStoreBackendError(str(e)) from e
                for k in keys:
                    try:
                        head = self._client.head(k)
                    except (_PreSendNetworkError, _PostSendAckLostError, _ObjectStoreTransportError) as e:
                        raise ObjectStoreBackendError(str(e)) from e
                    if head is None or not head.meta_hash:
                        continue  # vanished, phantom (fence-only), or foreign litter
                    yield k
                if not token:
                    return

        return _iter()

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
                "ObjectStoreBackend.write requires ScannedKey/ScannedBody from store.gate.persist()"
            )
        if not gate.verify(key) or not gate.verify(body):
            raise TypeError("ObjectStoreBackend.write: gate marker verification failed")

        # H1 Increment 2, Phase 2: ctx is required; derive expected_hash the
        # same way store/gate.py does, plus the caller's lease for fence
        # enforcement.
        precondition_lease: Optional[Lock] = (
            ctx.precondition.lease if isinstance(ctx.precondition, Overwrite) else None
        )
        expected_hash: Optional[str] = (
            ctx.precondition.expected_hash if isinstance(ctx.precondition, Overwrite) else None
        )

        k = key.key
        raw = body.body
        new_hash = sha256_hex(raw)
        new_generation = _extract_generation(raw)

        if expected_hash is None:
            return self._write_create_only(k, raw, new_hash)
        return self._write_cas_update(k, raw, new_hash, expected_hash, new_generation, precondition_lease)

    # -- create-only ----------------------------------------------------------

    def _write_create_only(self, key: str, raw: bytes, new_hash: str) -> WriteResult:
        envelope = _encode_envelope(None, 0.0, 0, 0, new_hash, raw)
        try:
            status, _headers = self._client.put(
                key, envelope, precondition={"If-None-Match": "*"}, meta_hash=new_hash
            )
        except _PreSendNetworkError:
            return ERROR(ErrorKind.NETWORK)
        except _PostSendAckLostError:
            return ERROR(ErrorKind.TIMEOUT_AFTER_COMMIT)

        if status in (200, 201):
            return OK(new_hash)
        if status == 412:
            # Map by OPERATION, not status (§4.2): create-only precondition
            # failure -> EXISTS (unless the existing object is a PHANTOM —
            # see _resolve_create_conflict).
            return self._resolve_create_conflict(key, raw, new_hash)
        if status == 403:
            return ERROR(ErrorKind.PERMISSION)
        if 500 <= status < 600:
            # §4.2: "5xx ... after send -> TIMEOUT_AFTER_COMMIT" (we cannot
            # tell whether the object store applied the write before failing
            # to ack it cleanly).
            return ERROR(ErrorKind.TIMEOUT_AFTER_COMMIT)
        # Unclassifiable -> fail closed, never assume success.
        return ERROR(ErrorKind.CONFLICT_UNKNOWN)

    def _resolve_create_conflict(self, key: str, raw: bytes, new_hash: str) -> WriteResult:
        """H1 Increment 2, Phase 2: the existing object may be a PHANTOM
        (some caller already `lock()`'d this key before any data existed) —
        in that case a create-only write must still succeed, filling in the
        phantom's body/version_hash while preserving its owner/fence fields,
        via a CAS-update-shaped PUT conditioned on the phantom's own ETag.
        Bounded retry: a concurrent racer (another create-only, another
        lock()) can invalidate our read between GET and PUT."""
        for _attempt in range(self._FENCE_RETRY_ATTEMPTS):
            try:
                env = self._read_envelope(key)
            except _EnvelopeCorruptionError:
                return ERROR(ErrorKind.CORRUPTION)
            except ValueError:
                # Existing object is not a KnoKeep envelope at all (foreign/
                # probe litter). JUDGMENT CALL 5: fall back to a raw content
                # hash rather than fabricate envelope semantics for it.
                try:
                    foreign_body = self._client.get(key)
                except (_PreSendNetworkError, _PostSendAckLostError, _ObjectStoreTransportError):
                    return ERROR(ErrorKind.CONFLICT_UNKNOWN)
                if foreign_body is None:
                    return ERROR(ErrorKind.CONFLICT_UNKNOWN)
                foreign_hash = sha256_hex(foreign_body)
                if foreign_hash == new_hash:
                    return OK(new_hash)
                return EXISTS(foreign_hash)
            except (_PreSendNetworkError, _PostSendAckLostError, _ObjectStoreTransportError):
                return ERROR(ErrorKind.CONFLICT_UNKNOWN)

            if env is None:
                # Raced away again (deleted or never really existed) — retry
                # a plain create-only once more.
                envelope = _encode_envelope(None, 0.0, 0, 0, new_hash, raw)
                try:
                    status, _ = self._client.put(
                        key, envelope, precondition={"If-None-Match": "*"}, meta_hash=new_hash
                    )
                except (_PreSendNetworkError, _PostSendAckLostError):
                    return ERROR(ErrorKind.CONFLICT_UNKNOWN)
                if status in (200, 201):
                    return OK(new_hash)
                if status == 412:
                    continue
                return ERROR(ErrorKind.CONFLICT_UNKNOWN)

            if env.version_hash is None:
                # Phantom: no real data yet — fill it in, preserving the
                # owner/fence fields exactly as read.
                new_envelope = _encode_envelope(
                    env.owner_token, env.owner_expiry, env.owner_fence,
                    env.last_accepted_fence, new_hash, raw,
                )
                try:
                    status, _ = self._client.put(
                        key, new_envelope, precondition={"If-Match": env.etag}, meta_hash=new_hash
                    )
                except (_PreSendNetworkError, _PostSendAckLostError):
                    return ERROR(ErrorKind.CONFLICT_UNKNOWN)
                if status in (200, 201):
                    return OK(new_hash)
                if status == 412:
                    continue  # someone else raced (another fill-in or a lock()); retry
                return ERROR(ErrorKind.CONFLICT_UNKNOWN)

            # Real data already present under this key.
            if env.version_hash == new_hash:
                # Contract §3: "Retried create-only returning EXISTS where
                # stored hash == new sha256 -> treat as OK (idempotent
                # replay)."
                return OK(new_hash)
            return EXISTS(env.version_hash)

        return ERROR(ErrorKind.CONFLICT_UNKNOWN)

    # -- CAS-update -------------------------------------------------------------

    def _write_cas_update(
        self,
        key: str,
        raw: bytes,
        new_hash: str,
        expected_hash: str,
        new_generation: Optional[int],
        precondition_lease: Optional[Lock],
    ) -> WriteResult:
        # H1 Increment 2, Phase 2: one GET reads the canonical envelope —
        # owner/fence state AND the logical hash/body AND the native ETag,
        # all from the SAME object version (see module docstring's "CAS
        # DECISION COST" note: HEAD alone can no longer answer this).
        try:
            env = self._read_envelope(key)
        except _EnvelopeCorruptionError:
            # An envelope whose header hash does not describe its body:
            # never CAS against (or report) a hash that lies about the data.
            return ERROR(ErrorKind.CORRUPTION)
        except ValueError:
            # Not a KnoKeep envelope at all — treat as no usable current
            # version to CAS against.
            return ERROR(ErrorKind.CONFLICT_UNKNOWN)
        except (_PreSendNetworkError, _PostSendAckLostError, _ObjectStoreTransportError):
            return ERROR(ErrorKind.NETWORK)

        if env is None:
            # CAS-update against an absent key -> STALE{None} (§3: "a
            # CAS-update NEVER creates").
            return STALE(None)

        # Ownership/fence FIRST (H1 Increment 2, Phase 2), before the
        # pre-existing content-hash CAS guard — this is what makes "reject a
        # stale-but-hash-matching lease" hold, independent of the hash check.
        fence_ok = (
            precondition_lease is not None
            and precondition_lease.token == env.owner_token
            and env.owner_expiry > time.time()
            and precondition_lease.fence >= env.last_accepted_fence
        )
        if not fence_ok:
            settled_hash = self._settle_current_hash_after_fence_loss(key, expected_hash)
            return STALE(settled_hash, reason="FENCE")

        current_hash = env.version_hash
        if current_hash is None:
            return STALE(None)  # phantom: fence holder, but nothing to CAS against yet
        if current_hash != expected_hash:
            return STALE(current_hash)

        # JUDGMENT CALL (residual from Phase 0/1's #4): generation
        # monotonicity, only for non-STATE doc types. `env.body` is already
        # in hand from the same GET — no extra read needed.
        if new_generation is not None:
            current_generation = _extract_generation(env.body)
            if current_generation is not None and new_generation <= current_generation:
                return STALE(current_hash)

        assert precondition_lease is not None  # fence_ok requires this
        new_envelope = _encode_envelope(
            env.owner_token, env.owner_expiry, env.owner_fence,
            precondition_lease.fence, new_hash, raw,
        )
        try:
            status, _headers = self._client.put(
                key, new_envelope, precondition={"If-Match": env.etag}, meta_hash=new_hash
            )
        except _PreSendNetworkError:
            return ERROR(ErrorKind.NETWORK)
        except _PostSendAckLostError:
            return ERROR(ErrorKind.TIMEOUT_AFTER_COMMIT)

        if status in (200, 201):
            return OK(new_hash)
        if status == 412:
            # H1 Increment 2, Phase 2: this rejection is not necessarily a
            # conflicting DATA write — it can equally be another caller's
            # fence-only advance (lock()) for this same key landing first
            # (owner/fence and data share one ETag). Settle rather than
            # trust a single immediate re-read, same rationale and same
            # technique as store/git_backend.py's Phase 2 fix.
            settled_hash = self._settle_current_hash_after_fence_loss(key, expected_hash)
            return STALE(settled_hash)
        if status == 403:
            return ERROR(ErrorKind.PERMISSION)
        if 500 <= status < 600:
            return ERROR(ErrorKind.TIMEOUT_AFTER_COMMIT)
        return ERROR(ErrorKind.CONFLICT_UNKNOWN)

    def _settle_current_hash_after_fence_loss(self, key: str, expected_hash: str) -> Optional[str]:
        """See the module docstring's "SETTLING A LOST FENCE RACE". Returns
        the key's current logical hash once the race that superseded this
        caller's fence has SETTLED: either the hash moved away from
        `expected_hash` (the winner landed), or no fenced write is pending
        any more (the envelope's `owner_fence` has been consumed by a
        committed write -- `last_accepted_fence` caught up -- or the owner's
        lease expired), or the bounded wait (`_FENCE_LOSS_SETTLE_MAX_S`) ran
        out. So a genuinely non-racing fence rejection (an old lease replayed
        with nobody else contending) returns on its very first check, while
        the C1 race's losers report the true winner's hash. Returns the last
        successfully observed hash (initially `expected_hash`) if a re-read
        fails -- never a value worse than the pre-read one."""
        deadline = time.monotonic() + self._FENCE_LOSS_SETTLE_MAX_S
        current_hash: Optional[str] = expected_hash
        while True:
            try:
                env = self._read_envelope(key)
            except (ValueError, _PreSendNetworkError, _PostSendAckLostError, _ObjectStoreTransportError):
                return current_hash
            current_hash = env.version_hash if env is not None else None
            if current_hash != expected_hash:
                return current_hash
            pending = (
                env is not None
                and env.owner_fence > env.last_accepted_fence
                and env.owner_expiry > time.time()
            )
            if not pending or time.monotonic() >= deadline:
                return current_hash
            time.sleep(self._FENCE_LOSS_SETTLE_POLL_S)

    # -- advisory lock() API (JUDGMENT CALL 6 — NOT the CAS mechanism) --------

    def lock(self, key: str, ttl_s: float) -> Lock:
        with self._lock_mutex:
            now = time.time()
            existing = self._locks.get(key)
            if existing is not None and existing[1] > now:
                raise BackendBusyError(f"key {key!r} is locked")
            token = _new_token()
            expiry = now + ttl_s
            new_fence = self._advance_durable_fence(key, token, expiry)
            self._locks[key] = (token, expiry)
            return Lock(key=key, token=token, expiry_epoch=expiry, fence=new_fence)

    def _advance_durable_fence(self, key: str, token: str, expiry: float) -> int:
        """Durable fence allocation via the canonical envelope's native CAS
        (If-None-Match for a brand-new key's PHANTOM, If-Match to advance an
        existing envelope's owner fields) — bounded retry on a 412 (a
        genuinely concurrent lock()/write() by this or another caller moved
        the object first)."""
        for _attempt in range(self._FENCE_RETRY_ATTEMPTS):
            try:
                env = self._read_envelope(key)
            except ValueError:
                raise BackendBusyError(f"lock({key!r}): existing object is not a KnoKeep envelope")
            except (_PreSendNetworkError, _PostSendAckLostError, _ObjectStoreTransportError) as e:
                raise BackendBusyError(f"lock({key!r}): read failed: {e}") from e

            if env is None:
                new_fence = 1
                envelope = _encode_envelope(token, expiry, new_fence, 0, None, b"")
                precondition: Dict[str, str] = {"If-None-Match": "*"}
                meta_hash = ""  # phantom: fence state only, no logical value
            else:
                new_fence = max(env.owner_fence, env.last_accepted_fence) + 1
                envelope = _encode_envelope(
                    token, expiry, new_fence, env.last_accepted_fence, env.version_hash, env.body,
                )
                precondition = {"If-Match": env.etag}
                # Keep the logical-hash metadata in step with the value this
                # envelope still carries (list() relies on it to tell a real
                # key from a phantom); a legacy object is upgraded to envelope
                # form here, conditioned on its own ETag.
                meta_hash = env.version_hash or ""

            try:
                status, _ = self._client.put(key, envelope, precondition=precondition, meta_hash=meta_hash)
            except (_PreSendNetworkError, _PostSendAckLostError) as e:
                raise BackendBusyError(f"lock({key!r}): fence PUT failed: {e}") from e
            if status in (200, 201):
                return new_fence
            if status == 412:
                continue  # lost the race; re-read and retry
            raise BackendBusyError(f"lock({key!r}): unexpected status {status} advancing fence")

        raise BackendBusyError(
            f"lock({key!r}): fence allocation did not converge after "
            f"{self._FENCE_RETRY_ATTEMPTS} attempts"
        )

    def unlock(self, lock: Lock) -> bool:
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
            self._locks[lock.key] = (token, new_expiry)
            self._renew_durable_fence(lock.key, token, new_expiry)
            return True

    def _renew_durable_fence(self, key: str, token: str, new_expiry: float) -> None:
        """Best-effort: extend the canonical envelope's owner_expiry (fence
        unchanged) to match a renewed advisory lock. Bounded retry on a 412;
        gives up silently (renew() itself already reports the advisory-lock
        success it determined) rather than raising."""
        for _attempt in range(self._FENCE_RETRY_ATTEMPTS):
            try:
                env = self._read_envelope(key)
            except (ValueError, _PreSendNetworkError, _PostSendAckLostError, _ObjectStoreTransportError):
                return
            if env is None or env.owner_token != token:
                return  # nothing to extend, or already superseded
            new_envelope = _encode_envelope(
                token, new_expiry, env.owner_fence, env.last_accepted_fence, env.version_hash, env.body,
            )
            try:
                status, _ = self._client.put(
                    key, new_envelope, precondition={"If-Match": env.etag}, meta_hash=(env.version_hash or "")
                )
            except (_PreSendNetworkError, _PostSendAckLostError):
                return
            if status in (200, 201):
                return
            if status == 412:
                continue
            return


def _new_token() -> str:
    import secrets as _secrets

    return _secrets.token_hex(16)  # 128-bit CSPRNG token


__all__ = [
    "ObjectStoreBackend",
    "ObjectStoreBackendError",
    "SigV4Client",
    "PROBE_PREFIX",
]
