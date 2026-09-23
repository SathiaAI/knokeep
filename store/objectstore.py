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
implements exactly the three operations this adapter needs — HEAD, GET, PUT
(with conditional headers) — and nothing else (no multipart, no
ListObjectsV2-alternatives beyond what `list()` needs). `boto3` is NOT
imported anywhere in this module; it may only ever appear in test code as an
independent oracle (contract task instructions), never in the adapter.

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
     compares ETag bytes to anything — it is captured from one HEAD response
     header and echoed back VERBATIM (quotes included, exactly as the server
     sent it) into the next PUT's `If-Match` header. It is treated as a
     fully opaque native token, by construction (there is no code path that
     could put a sha256 value into `If-Match` even by mistake — the only
     value ever placed there is a value the client itself received FROM the
     server as an `ETag` header, never a locally-computed hash).

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

  4. CAS-update generation-monotonicity check costs one extra GET. §4.2's
     single-HEAD requirement ("a single HEAD reads the knokeep-sha256
     metadata AND captures the native token... from the SAME object
     version") is about the CAS decision itself (stale-hash / native-token
     capture) — exactly one HEAD is issued for that. Contract §6 separately
     requires rejecting a non-increasing generation for non-STATE doc types,
     and the generation lives INSIDE the body (§6: "MUST carry a monotonic
     uint64 generation in the scanned body"), which a HEAD (headers only)
     cannot see. This adapter therefore issues one additional GET — ONLY
     when the new body carries a generation header — to read the current
     body and extract its generation, mirroring the identical judgment call
     already made in `store/local.py` and `store/git_backend.py` (both of
     which have the current body in hand anyway from their own read path).
     This does not add a second HEAD and does not change the CAS
     linearization point, which remains the single HEAD-then-conditional-PUT
     pair against the native token.

  5. Fallback content-hash on a create-only conflict against a foreign
     object. If a create-only PUT's `If-None-Match: *` is rejected (412 →
     EXISTS class), the adapter re-reads via HEAD to report `current_hash`.
     If that object was NOT written by this adapter (no
     `x-amz-meta-knokeep-sha256` present — e.g. probe litter from another
     process, or an out-of-band write), there is no metadata hash to report.
     Rather than fabricate one, the adapter falls back to one GET and
     computes `sha256(body)` directly ONLY on this rare, already-failed
     branch (never on the hot success path, never instead of the mandatory
     metadata-hash comparison in the CAS-update path).

  6. Advisory `lock()`/`unlock()`/`renew()` are kept in-memory (one dict
     guarded by a `threading.Lock`), identical in shape and rationale to
     `store/git_backend.py`'s JUDGMENT CALL 1: contract §3 states plainly
     that "Advisory lock() is never the CAS mechanism" for any backend, and
     §4.2 does not mandate durable lock storage for object-store — only the
     native-precondition CAS is load-bearing. Adding a second, durable
     lock-object mechanism here would itself need its own CAS story that
     nothing in §4.2 calls for.

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
     `secret_access_key` (and optional `session_token`) directly (a caller
     wanting them sourced from its OWN environment reads `os.environ` itself
     before constructing this class; that is a caller-side decision, not
     behavior this module performs on its own). This trivially satisfies
     "NO default credential chain, NO IMDS/metadata calls" because no such
     chain is ever consulted — there was nothing to disable.

  9. Multipart is not merely "disabled" by a flag — no multipart code path
     exists anywhere in this module. `SigV4Client.put()` always issues one
     single-shot `PUT` with the full body; there is no upload-id/part-number
     concept, no `CreateMultipartUpload` call, nothing to accidentally
     trigger for a large body. A very large body simply means a very large
     single PUT (bounded by whatever the endpoint itself enforces).

  10. §8 load-time capability probe. On construction (unless explicitly
      disabled for a targeted unit test), the adapter runs three requests
      against a fresh random key under the reserved prefix `.knokeep/probe/`
      — create-only (expect success), duplicate create-only (expect the
      endpoint's own 412 → this adapter's own EXISTS-mapping logic is not
      what's being tested here, the RAW status code is asserted to be 412),
      and a CAS-update with a deliberately wrong `If-Match` token (expect
      412) — then deletes the probe object (best-effort; DELETE is not part
      of the `StoreBackend` protocol and is used ONLY for this internal
      hygiene, never exposed as a public method). Any of the three checks
      coming back other than expected, or any transport failure while
      running them, raises `RuntimeError` and the adapter refuses to be
      constructed at all — "no degraded mode" (§8) is implemented literally:
      there is no code path where `ObjectStoreBackend.__init__` returns
      successfully after a failed probe.

  11. Test-only transport injection seam (`_client=` constructor kwarg,
      leading underscore, not part of the public API). Contract-mandated
      behavior this module must prove includes "REFUSE TO START when the
      endpoint does not enforce preconditions" (§8) — which requires a
      transport that ACCEPTS every write, unconditionally, to prove the
      refusal fires. That transport cannot be a real moto server (moto
      faithfully enforces the preconditions, which is exactly why it is
      used for every OTHER test) — it must be a stub. Rather than reach into
      private internals from the test, the constructor accepts a
      caller-supplied client object satisfying the same duck-typed interface
      (`head`/`get`/`put`/`delete`/`list_objects`) as `SigV4Client`; this is
      the same "inject a fake collaborator" shape already used throughout
      this codebase (`store/fake.py`'s fault-injection knobs serve the same
      testing purpose for the conformance suite).
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import hmac
import http.client
import re
import ssl
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Dict, Iterator, List, Optional, Tuple
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

        # Call-count instrumentation (tests assert on this — e.g. "single
        # HEAD" per CAS-update — never used for adapter logic itself).
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
        `{"If-Match": "<etag captured from a prior HEAD, verbatim>"}`
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
        return Caps(atomic=True, cas=True, lock=True, durable=True, remote=True)

    def health(self) -> BackendHealth:
        try:
            self._client.head(f"{PROBE_PREFIX}healthcheck-{uuid.uuid4().hex}")
            return BackendHealth(ok=True, detail=f"object-store backend at {self._client.describe()}")
        except Exception as e:  # noqa: BLE001 - health() reports, never raises
            return BackendHealth(ok=False, detail=str(e))

    def read(self, key: str) -> Optional[Blob]:
        try:
            body = self._client.get(key)
        except (_PreSendNetworkError, _PostSendAckLostError, _ObjectStoreTransportError) as e:
            raise ObjectStoreBackendError(str(e)) from e
        if body is None:
            return None
        return Blob(body=body, version_hash=sha256_hex(body))

    def list(self, prefix: str) -> Iterator[str]:
        def _iter() -> Iterator[str]:
            token: Optional[str] = None
            while True:
                try:
                    keys, token = self._client.list_objects(prefix, continuation_token=token)
                except (_PreSendNetworkError, _PostSendAckLostError, _ObjectStoreTransportError) as e:
                    raise ObjectStoreBackendError(str(e)) from e
                for k in keys:
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

        # H1 Increment 2, Phase 0: ctx is required; derive expected_hash the
        # same way store/gate.py does. No fence enforcement yet.
        expected_hash: Optional[str] = (
            ctx.precondition.expected_hash if isinstance(ctx.precondition, Overwrite) else None
        )

        k = key.key
        raw = body.body
        new_hash = sha256_hex(raw)
        new_generation = _extract_generation(raw)

        if expected_hash is None:
            return self._write_create_only(k, raw, new_hash)
        return self._write_cas_update(k, raw, new_hash, expected_hash, new_generation)

    # -- create-only ----------------------------------------------------------

    def _write_create_only(self, key: str, raw: bytes, new_hash: str) -> WriteResult:
        try:
            status, _headers = self._client.put(
                key, raw, precondition={"If-None-Match": "*"}, meta_hash=new_hash
            )
        except _PreSendNetworkError:
            return ERROR(ErrorKind.NETWORK)
        except _PostSendAckLostError:
            return ERROR(ErrorKind.TIMEOUT_AFTER_COMMIT)

        if status in (200, 201):
            return OK(new_hash)
        if status == 412:
            # Map by OPERATION, not status (§4.2): create-only precondition
            # failure -> EXISTS.
            return self._resolve_create_conflict(key, new_hash)
        if status == 403:
            return ERROR(ErrorKind.PERMISSION)
        if 500 <= status < 600:
            # §4.2: "5xx ... after send -> TIMEOUT_AFTER_COMMIT" (we cannot
            # tell whether the object store applied the write before failing
            # to ack it cleanly).
            return ERROR(ErrorKind.TIMEOUT_AFTER_COMMIT)
        # Unclassifiable -> fail closed, never assume success.
        return ERROR(ErrorKind.CONFLICT_UNKNOWN)

    def _resolve_create_conflict(self, key: str, new_hash: str) -> WriteResult:
        try:
            head = self._client.head(key)
        except (_PreSendNetworkError, _PostSendAckLostError, _ObjectStoreTransportError):
            return ERROR(ErrorKind.CONFLICT_UNKNOWN)
        if head is None:
            # Raced away again between our PUT and this HEAD; cannot tell
            # whether anything landed. Never a false definitely-not-committed.
            return ERROR(ErrorKind.CONFLICT_UNKNOWN)

        current_hash = head.meta_hash
        if current_hash is None:
            # JUDGMENT CALL 5: object exists without our metadata header
            # (written out-of-band) — fall back to hashing the body itself,
            # ONLY on this already-failed branch.
            try:
                current_body = self._client.get(key)
            except (_PreSendNetworkError, _PostSendAckLostError, _ObjectStoreTransportError):
                return ERROR(ErrorKind.CONFLICT_UNKNOWN)
            if current_body is None:
                return ERROR(ErrorKind.CONFLICT_UNKNOWN)
            current_hash = sha256_hex(current_body)

        if current_hash == new_hash:
            # Contract §3: "Retried create-only returning EXISTS where stored
            # hash == new sha256 -> treat as OK (idempotent replay)."
            return OK(new_hash)
        return EXISTS(current_hash)

    # -- CAS-update -------------------------------------------------------------

    def _write_cas_update(
        self,
        key: str,
        raw: bytes,
        new_hash: str,
        expected_hash: str,
        new_generation: Optional[int],
    ) -> WriteResult:
        # §4.2: "a single HEAD reads the knokeep-sha256 metadata AND captures
        # the native token ... from the SAME object version" — exactly one
        # HEAD call for the CAS decision itself.
        try:
            head = self._client.head(key)
        except (_PreSendNetworkError, _PostSendAckLostError, _ObjectStoreTransportError):
            return ERROR(ErrorKind.NETWORK)

        if head is None:
            # CAS-update against an absent key -> STALE{None} (§3: "a
            # CAS-update NEVER creates").
            return STALE(None)
        if head.meta_hash != expected_hash:
            return STALE(head.meta_hash)
        if not head.etag:
            # Enforcement is broken/missing at the object level (no ETag to
            # condition on) — fail closed rather than ever issue an
            # unconditional PUT.
            return ERROR(ErrorKind.CONFLICT_UNKNOWN)

        # JUDGMENT CALL 4: generation monotonicity (residual from T1, same
        # rationale as store/local.py / store/git_backend.py) — only for
        # non-STATE doc types, which carry a generation header in the body.
        if new_generation is not None:
            try:
                current_body = self._client.get(key)
            except (_PreSendNetworkError, _PostSendAckLostError, _ObjectStoreTransportError):
                return ERROR(ErrorKind.NETWORK)
            current_generation = _extract_generation(current_body) if current_body is not None else None
            if current_generation is not None and new_generation <= current_generation:
                return STALE(head.meta_hash)

        try:
            status, _headers = self._client.put(
                key, raw, precondition={"If-Match": head.etag}, meta_hash=new_hash
            )
        except _PreSendNetworkError:
            return ERROR(ErrorKind.NETWORK)
        except _PostSendAckLostError:
            return ERROR(ErrorKind.TIMEOUT_AFTER_COMMIT)

        if status in (200, 201):
            return OK(new_hash)
        if status == 412:
            # Map by OPERATION, not status (§4.2): CAS-update precondition
            # failure -> STALE.
            return self._resolve_cas_conflict(key, head.meta_hash)
        if status == 403:
            return ERROR(ErrorKind.PERMISSION)
        if 500 <= status < 600:
            return ERROR(ErrorKind.TIMEOUT_AFTER_COMMIT)
        return ERROR(ErrorKind.CONFLICT_UNKNOWN)

    def _resolve_cas_conflict(self, key: str, fallback_hash: Optional[str]) -> WriteResult:
        try:
            head = self._client.head(key)
        except (_PreSendNetworkError, _PostSendAckLostError, _ObjectStoreTransportError):
            # Still definitely STALE per §4.2 ("CAS-update precondition
            # failure -> STALE") — only the reported current_hash detail is
            # best-effort here.
            return STALE(fallback_hash)
        if head is None:
            return STALE(None)
        return STALE(head.meta_hash)

    # -- advisory lock() API (JUDGMENT CALL 6 — NOT the CAS mechanism) --------

    def lock(self, key: str, ttl_s: float) -> Lock:
        with self._lock_mutex:
            now = time.time()
            existing = self._locks.get(key)
            if existing is not None and existing[1] > now:
                raise BackendBusyError(f"key {key!r} is locked")
            token = _new_token()
            expiry = now + ttl_s
            self._locks[key] = (token, expiry)
            return Lock(key=key, token=token, expiry_epoch=expiry)

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
            self._locks[lock.key] = (token, time.time() + ttl_s)
            return True


def _new_token() -> str:
    import secrets as _secrets

    return _secrets.token_hex(16)  # 128-bit CSPRNG token


__all__ = [
    "ObjectStoreBackend",
    "ObjectStoreBackendError",
    "SigV4Client",
    "PROBE_PREFIX",
]
