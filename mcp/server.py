"""KnoKeep MCP server — the access layer, built LAST over the finished
StoreBackend socket (store/backend.py, store/gate.py) and the read-only
reconciler (reconciler/).

A minimal JSON-RPC 2.0 server speaking the Model Context Protocol over
stdio, using stdlib `json`/`sys` only — no third-party MCP SDK, even though
one (`mcp`, the official Anthropic Python SDK) may be installed in this
environment; see mcp/__init__.py's module docstring for why importing this
package as `mcp` is still safe.

TRANSPORT / FRAMING (JUDGMENT CALL):
The task says "JSON-RPC 2.0 over stdio" but does not pin an exact framing.
Two shapes exist in the wild for JSON-RPC-over-stdio tooling: (a) LSP-style
`Content-Length: N\r\n\r\n<N bytes of JSON>` framing, and (b) newline-
delimited JSON, one complete, self-contained JSON-RPC message per line, no
embedded raw newlines. This server implements (b) — one JSON object per
line — because that is what the actual Model Context Protocol stdio
transport specifies ("Messages are delimited by newlines, and MUST NOT
contain embedded newlines"). `json.dumps(..., separators=(",", ":"))` is
used on the way out precisely so no message can ever contain a literal
newline. tests/test_mcp.py drives the server exactly this way: it writes one
JSON-encoded line per request to the child process's stdin and reads one
line per response from its stdout — "framed requests"/"framed responses" in
the newline sense, not Content-Length framing.

TOOL RESULT SHAPE (JUDGMENT CALL):
Per the MCP tools/call convention, a tool's return value is wrapped as
`{"content": [{"type": "text", "text": "<json>"}], "isError": <bool>}`. The
structured payload (WriteResult, DriftReport, Blob, ...) is JSON-encoded
into that single text block rather than invented as a bespoke top-level
shape, so every tool result is uniformly `content[0].text` a caller can
`json.loads()`. `isError` is set to true for: (1) a Python-level exception
while executing the tool body (a real fault), and (2) every
`knokeep_write` result whose `status == "ERROR"` — SECRET_BLOCKED and every
other ErrorKind (NETWORK, CORRUPTION, SCAN_FAILURE, TIMEOUT_AFTER_COMMIT,
CONFLICT_UNKNOWN, INVALID_ARGUMENT, ...) alike — so a client cannot mistake
a failed or outcome-uncertain write for an ordinary successful call by only
checking `isError`. `OK`, `STALE`, and `EXISTS` are reported with
`isError: false` and a `status`/`commit_class` field in the payload — these
are ordinary, expected CAS outcomes per the store contract (§1), not tool
failures.

BYTES OVER JSON (JUDGMENT CALL):
JSON has no bytes type. `knokeep_read` returns the blob body as
base64 (`body_b64`, always present when found) plus a best-effort UTF-8
`text` field (present only when the bytes happen to decode cleanly) as a
convenience for callers working with text docs. The base64 field is the
one that is bytes-verbatim and load-bearing; `text` is never used to decide
anything server-side. `knokeep_write` accepts the body either as UTF-8
`body` (a plain string — the common case for KnoKeep's markdown/state docs)
or as base64 `body_b64` (for arbitrary bytes); exactly one of the two must
be given.

SECRET HYGIENE AT THE MCP BOUNDARY:
Every tool input is treated as untrusted data (validated by shape/type
before use, never eval'd/exec'd). This layer never constructs an error
string containing raw body bytes or a raw key value; where a message must
reference content, it uses a sha256 prefix, per contract §5
("error strings carry a sha256 prefix only"). No new secret-scanning logic
lives here — knokeep_write routes every byte through `store.gate.persist`
(the one write door), and knokeep_reconcile routes every fetched value
through the reconciler's own `secret_scan`-quarantine path
(reconciler/reconcile.py); this server neither duplicates nor bypasses
either.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import sys
from typing import Any, Callable, Dict, IO, List, Mapping, Optional, Sequence

from reconciler.reconcile import DEFAULT_FACT_SPECS, FactSpec, reconcile as run_reconcile
from reconciler.sources import StoreSource
from store import gate
from store.backend import BackendBusyError, StoreBackend
from store.context import create_ctx, overwrite_ctx
from store.fake import FakeBackend
from store.local import LocalBackend
from store.config import default_store_root
from store.types import ERROR, EXISTS, OK, STALE, ErrorKind, WriteResult, commit_class

JSONRPC_VERSION = "2.0"
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "knokeep-mcp"
SERVER_VERSION = "0.1.0"

# Standard JSON-RPC 2.0 error codes (spec-fixed, not ours to choose).
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


def _sha256_prefix(raw: bytes, n: int = 12) -> str:
    """A safe, non-reversible reference to bytes for error strings — never
    the bytes themselves (contract §5: "error strings carry a sha256 prefix
    only — never body bytes or key values")."""
    import hashlib

    return hashlib.sha256(raw).hexdigest()[:n]


# ---------------------------------------------------------------------------
# A tiny read-only "external fact" source for knokeep_reconcile.
# ---------------------------------------------------------------------------


class _StaticSource:
    """Wraps a plain, caller-supplied dict as a read-only reconciler source.

    This MCP server has no live GitHub/tracker client of its own — it holds
    only a StoreBackend (per the task: "backend-agnostic ... the server
    holds a configured StoreBackend + the gate"). So a caller wanting to
    compare the store's view against GitHub's or a tracker's supplies that
    other system's already-fetched facts as plain tool arguments, and this
    class just presents them to reconciler.reconcile() as a `fetch_state()`
    source, matching the `GitHubSource`/`TrackerSource` Protocol shape in
    reconciler/sources.py (exactly one method, no write-shaped method to
    misuse). It performs no I/O of its own.
    """

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def fetch_state(self) -> dict:
        return dict(self._values)


# ---------------------------------------------------------------------------
# WriteResult / DriftReport -> JSON-safe dict
# ---------------------------------------------------------------------------


def _write_result_to_dict(result: WriteResult) -> Dict[str, Any]:
    base: Dict[str, Any] = {"commit_class": commit_class(result)}
    if isinstance(result, OK):
        base.update(status="OK", new_hash=result.new_hash)
    elif isinstance(result, STALE):
        base.update(status="STALE", current_hash=result.current_hash)
    elif isinstance(result, EXISTS):
        base.update(status="EXISTS", current_hash=result.current_hash)
    elif isinstance(result, ERROR):
        base.update(status="ERROR", kind=result.kind.value)
        if result.kind is ErrorKind.SECRET_BLOCKED:
            # labels only — secret_scan() never returns matched values
            # (store/gate.py), so this can never leak a secret.
            base["labels"] = list(result.labels)
    else:  # pragma: no cover - defensive, WriteResult is a closed union
        raise TypeError(f"not a WriteResult: {result!r}")
    return base


def _is_secret_blocked(result: WriteResult) -> bool:
    return isinstance(result, ERROR) and result.kind is ErrorKind.SECRET_BLOCKED


def _drift_report_to_dict(report) -> Dict[str, Any]:
    return {
        "has_drift": report.has_drift,
        "sources_consulted": list(report.sources_consulted),
        "facts": [
            {
                "fact_key": f.fact_key,
                "values": dict(f.values),
                "quarantined": {k: list(v) for k, v in f.quarantined.items()},
                "source_of_truth": f.source_of_truth,
                "source_of_truth_value": f.source_of_truth_value,
                "agree": f.agree,
                "diverging_sources": list(f.diverging_sources),
                "missing_sources": list(f.missing_sources),
            }
            for f in report.facts
        ],
    }


# ---------------------------------------------------------------------------
# Tool argument validation helpers — every tool input is untrusted (task
# instruction: "Treat all tool inputs as untrusted data").
# ---------------------------------------------------------------------------


class ToolInputError(Exception):
    """Raised by a tool handler for a malformed argument. Never carries raw
    body bytes or key values in its message — see module docstring."""


def _require_str(args: Mapping[str, Any], name: str, *, default: Optional[str] = None,
                  required: bool = True) -> Optional[str]:
    if name not in args or args[name] is None:
        if required and default is None:
            raise ToolInputError(f"missing required string argument '{name}'")
        return default
    value = args[name]
    if not isinstance(value, str):
        raise ToolInputError(f"argument '{name}' must be a string")
    return value


def _optional_str(args: Mapping[str, Any], name: str) -> Optional[str]:
    if name not in args or args[name] is None:
        return None
    value = args[name]
    if not isinstance(value, str):
        raise ToolInputError(f"argument '{name}' must be a string or null")
    return value


# ---------------------------------------------------------------------------
# Tool implementations. Each takes (backend, args_dict) and returns a JSON-
# safe dict payload, or raises ToolInputError for a bad argument.
# ---------------------------------------------------------------------------


def _tool_knokeep_read(backend: StoreBackend, args: Mapping[str, Any]) -> Dict[str, Any]:
    key = _require_str(args, "key")
    blob = backend.read(key)  # None ONLY for NOT_FOUND (contract §2); other
    # failure kinds raise, and are turned into a tool-level error by the
    # caller (_call_tool), never silently swallowed here.
    if blob is None:
        return {"found": False}
    result: Dict[str, Any] = {
        "found": True,
        "version_hash": blob.version_hash,
        "body_b64": base64.b64encode(blob.body).decode("ascii"),
    }
    try:
        result["text"] = blob.body.decode("utf-8")
    except UnicodeDecodeError:
        pass  # not text; body_b64 is the load-bearing field either way
    return result


def _tool_knokeep_list(backend: StoreBackend, args: Mapping[str, Any]) -> Dict[str, Any]:
    prefix = _require_str(args, "prefix", default="", required=False) or ""
    keys = list(backend.list(prefix))
    return {"keys": keys}


def _tool_knokeep_write(backend: StoreBackend, args: Mapping[str, Any]) -> Dict[str, Any]:
    key = _require_str(args, "key")
    doc_type = _require_str(args, "doc_type")
    expected_hash = _optional_str(args, "expected_hash")

    body_text = _optional_str(args, "body")
    body_b64 = _optional_str(args, "body_b64")
    if body_text is not None and body_b64 is not None:
        raise ToolInputError("provide exactly one of 'body' or 'body_b64', not both")
    if body_text is not None:
        raw_bytes = body_text.encode("utf-8")
    elif body_b64 is not None:
        try:
            raw_bytes = base64.b64decode(body_b64, validate=True)
        except Exception:
            raise ToolInputError("argument 'body_b64' is not valid base64")
    else:
        raise ToolInputError("provide one of 'body' (utf-8 text) or 'body_b64' (base64 bytes)")

    # H1 Increment 2: build the required OperationContext. create-only when
    # expected_hash is omitted/null; otherwise a CAS-update carrying a real,
    # fence-enforced advisory lease that is HELD across persist() (D-006 on
    # the MCP side, same as the skill's read->compute->persist section):
    # releasing it before the write would open a window in which another
    # writer could take the key and advance the fence, turning this request
    # into a spurious fence-STALE even though its content hash is current.
    # The ONE write door (contract §1/§5): never backend.write() directly,
    # never a hand-built ScannedKey/ScannedBody.
    if expected_hash is None:
        result = gate.persist(backend, key, raw_bytes, ctx=create_ctx(), doc_type=doc_type)
        return _write_result_to_dict(result)

    # Refuse a malformed request BEFORE taking the lease: lock() advances the
    # key's durable fence (and materializes a phantom/sidecar on remote
    # backends), which would supersede a still-valid lease some other caller
    # holds -- for a write the gate was going to reject anyway. persist()
    # re-runs the same checks; this is the pure, side-effect-free copy.
    err = gate.validate_write(key, raw_bytes, doc_type=doc_type, expected_hash=expected_hash)
    if err is not None:
        return _write_result_to_dict(err)

    try:
        lease = backend.lock(key, ttl_s=30)
    except BackendBusyError:
        # Another holder has a live lease on this key (e.g. the skill's
        # per-key lease across its read-modify-write). An ordinary, retryable
        # write outcome -- not a tool fault.
        return _write_result_to_dict(ERROR(ErrorKind.BUSY))
    try:
        result = gate.persist(
            backend,
            key,
            raw_bytes,
            ctx=overwrite_ctx(expected_hash, lease),
            doc_type=doc_type,
        )
    finally:
        try:
            backend.unlock(lease)  # ownership-conditional; never releases a successor's lease
        except Exception:  # noqa: BLE001 - the write outcome is already decided
            pass
    return _write_result_to_dict(result)


def _tool_knokeep_reconcile(
    backend: StoreBackend, args: Mapping[str, Any], *, telemetry_dir: Optional[str] = None
) -> Dict[str, Any]:
    keys = args.get("keys", [])
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise ToolInputError("argument 'keys' must be a list of strings")

    sources: Dict[str, Any] = {"store": StoreSource(backend, keys)}

    github_head = _optional_str(args, "github_head")
    if github_head is not None:
        sources["github"] = _StaticSource({"head": github_head})

    tracker_status = _optional_str(args, "tracker_status")
    if tracker_status is not None:
        sources["tracker"] = _StaticSource({"status": tracker_status})

    fact_specs_arg = args.get("fact_specs")
    fact_specs: Sequence[FactSpec]
    if fact_specs_arg is None:
        fact_specs = DEFAULT_FACT_SPECS
    else:
        if not isinstance(fact_specs_arg, list):
            raise ToolInputError("argument 'fact_specs' must be a list")
        built: List[FactSpec] = []
        for spec in fact_specs_arg:
            if not isinstance(spec, dict):
                raise ToolInputError("each 'fact_specs' entry must be an object")
            fact_key = spec.get("fact_key")
            fields = spec.get("fields")
            source_of_truth = spec.get("source_of_truth")
            if not isinstance(fact_key, str) or not isinstance(source_of_truth, str):
                raise ToolInputError(
                    "each 'fact_specs' entry needs string 'fact_key' and 'source_of_truth'"
                )
            if not isinstance(fields, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in fields.items()
            ):
                raise ToolInputError("'fact_specs[].fields' must be a string->string object")
            built.append(FactSpec(fact_key=fact_key, fields=fields, source_of_truth=source_of_truth))
        fact_specs = built

    # READ-ONLY: reconcile() calls nothing on any source but .fetch_state()
    # (reconciler/reconcile.py's own safety invariant #1). Telemetry is
    # local-JSONL-only per contract; disabled by default here so an MCP tool
    # call never writes outside a directory the operator explicitly chose
    # (see KnoKeepServer.__init__'s `telemetry_dir`).
    report = run_reconcile(
        sources,
        fact_specs,
        emit_telemetry=telemetry_dir is not None,
        telemetry_dir=telemetry_dir,
    )
    return _drift_report_to_dict(report)


def _tool_knokeep_health(backend: StoreBackend, args: Mapping[str, Any]) -> Dict[str, Any]:
    health = backend.health()  # cheap for every adapter this task covers
    # (in-memory flag / local fs stat / a single lightweight remote check) —
    # per the task's "if cheap; otherwise omit" this tool is always included.
    return {"ok": health.ok, "detail": health.detail}


# Handlers that don't need any server-level config take (backend, args).
# knokeep_reconcile additionally needs the server's configured
# `telemetry_dir`, threaded through explicitly by KnoKeepServer._handle_tools_call
# rather than via a module global, so multiple KnoKeepServer instances in one
# process (e.g. several unit tests in the same pytest session) never share
# mutable state.
_TOOL_HANDLERS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "knokeep_read": _tool_knokeep_read,
    "knokeep_list": _tool_knokeep_list,
    "knokeep_write": _tool_knokeep_write,
    "knokeep_reconcile": _tool_knokeep_reconcile,
    "knokeep_health": _tool_knokeep_health,
}
_HANDLERS_NEEDING_TELEMETRY_DIR = {"knokeep_reconcile"}


# ---------------------------------------------------------------------------
# Tool schemas (advertised via tools/list)
# ---------------------------------------------------------------------------

_TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "knokeep_read",
        "description": (
            "Read a blob by key. Returns its bytes verbatim (base64) plus "
            "its version_hash, or found=false if the key does not exist. "
            "Never follows a URL/path/ref or resolves a pointer to a live "
            "secret — bytes are returned exactly as stored."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "knokeep_list",
        "description": "List stored keys matching a prefix.",
        "inputSchema": {
            "type": "object",
            "properties": {"prefix": {"type": "string", "default": ""}},
            "additionalProperties": False,
        },
    },
    {
        "name": "knokeep_write",
        "description": (
            "Write a blob through the secret gate (store.gate.persist) — "
            "never a raw adapter write. Provide exactly one of 'body' "
            "(UTF-8 text) or 'body_b64' (base64 bytes). Omit expected_hash "
            "(or pass null) for create-only; pass the current version_hash "
            "for a compare-and-swap update. Returns the WriteResult "
            "(OK/STALE/EXISTS/ERROR, including SECRET_BLOCKED) faithfully, "
            "with its commit-class."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "body": {"type": "string", "description": "UTF-8 text body"},
                "body_b64": {"type": "string", "description": "base64-encoded bytes body"},
                "expected_hash": {
                    "type": ["string", "null"],
                    "description": "64-hex sha256; null/omitted means create-only",
                },
                "doc_type": {
                    "type": "string",
                    "description": (
                        "One of the STATE allowlist (system_state, session_log, "
                        "HANDOFF, journal) for content-hash CAS, or any other "
                        "type whose body carries a '#knokeep-gen:<uint64>\\n' "
                        "generation header."
                    ),
                },
            },
            "required": ["key", "doc_type"],
            "additionalProperties": False,
        },
    },
    {
        "name": "knokeep_reconcile",
        "description": (
            "Run the read-only reconciler over the store plus any supplied "
            "external facts (github_head/tracker_status) and return a "
            "drift report. Every fetched value is secret-scanned and "
            "quarantined before it can appear in the report."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Store keys to read as the 'store' source.",
                },
                "github_head": {"type": ["string", "null"]},
                "tracker_status": {"type": ["string", "null"]},
                "fact_specs": {
                    "type": "array",
                    "description": "Optional custom fact specs; defaults to DEFAULT_FACT_SPECS.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "fact_key": {"type": "string"},
                            "fields": {"type": "object", "additionalProperties": {"type": "string"}},
                            "source_of_truth": {"type": "string"},
                        },
                        "required": ["fact_key", "fields", "source_of_truth"],
                    },
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "knokeep_health",
        "description": "Cheap backend health/verdict summary.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 / MCP server
# ---------------------------------------------------------------------------


class KnoKeepServer:
    """Holds one configured StoreBackend and dispatches JSON-RPC 2.0
    requests for the MCP handshake, tools/list, and tools/call. Framing-
    agnostic: `handle(request_dict) -> response_dict | None` does no I/O of
    its own (None means "no response" — the request was a notification)."""

    def __init__(self, backend: StoreBackend, *, telemetry_dir: Optional[str] = None) -> None:
        self.backend = backend
        self.telemetry_dir = telemetry_dir
        # Lifecycle state (MCP spec): `initialize` merely OFFERS a session;
        # the client only completes the handshake by sending the
        # `notifications/initialized` notification afterwards. `_initialized`
        # therefore becomes True ONLY in response to that notification, never
        # inside `_handle_initialize` itself — a client that calls
        # `tools/call` (e.g. knokeep_write) in between must be rejected, not
        # served (review finding: no tool execution before the handshake is
        # actually complete).
        self._initialized = False
        # Intermediate handshake state (CodeRabbit 4062128236): set True only
        # by `_handle_initialize`, and consumed (cleared) the moment a
        # `notifications/initialized` is accepted. If `notifications/
        # initialized` arrives while this is still False — i.e. before any
        # `initialize` request completed — it is rejected/ignored and
        # `_initialized` is never set, closing the ordering gap where a
        # client could send `initialized` first (or twice) and be treated as
        # handshake-complete without ever having called `initialize`.
        self._awaiting_initialized_ack = False

    # -- JSON-RPC plumbing --------------------------------------------------

    @staticmethod
    def _result(request_id: Any, result: Any) -> Dict[str, Any]:
        return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
        err: Dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": err}

    def handle(self, request: Any) -> Optional[Dict[str, Any]]:
        """Dispatch one already-decoded JSON-RPC message. Returns a response
        dict, or None for a notification (no 'id') which gets no reply."""
        if not isinstance(request, dict):
            return self._error(None, _INVALID_REQUEST, "Request must be a JSON object")

        has_id = "id" in request
        request_id = request.get("id")
        is_notification = not has_id

        if has_id and request_id is None:
            # An explicit `"id": null` is NOT a notification — JSON-RPC 2.0
            # notifications are identified by the ABSENCE of the 'id'
            # member, not by it being present-and-null. Reject before any
            # method dispatch (review finding: a write must never be able
            # to commit ahead of a response the client can correlate back
            # to nothing).
            return self._error(None, _INVALID_REQUEST, "'id' must not be null")

        if has_id and (
            type(request_id) is bool
            or not isinstance(request_id, (str, int, float))
            or (isinstance(request_id, float) and not math.isfinite(request_id))
        ):
            # JSON-RPC 2.0 'id' MUST be a string or number (never null here —
            # that case is handled above). `bool` is a Python subclass of
            # `int`, so it is checked FIRST and explicitly rejected — a
            # `dict`/`list` (or any other type) id is rejected by the
            # isinstance check. Python's default `json.loads` accepts the
            # non-standard constants `NaN`/`Infinity`/`-Infinity` as floats,
            # and the default `json.dumps` would echo them back as invalid
            # JSON tokens the client cannot correlate — so a non-finite float
            # id is rejected here too. Reject before any method dispatch — an
            # invalid-type id must never reach tools/call/knokeep_write
            # (CodeRabbit 4062128227, non-finite follow-up).
            return self._error(None, _INVALID_REQUEST, "'id' must be a string or number")

        if request.get("jsonrpc") != JSONRPC_VERSION:
            if is_notification:
                return None
            return self._error(request_id, _INVALID_REQUEST, "Missing/invalid 'jsonrpc' version")

        method = request.get("method")
        if not isinstance(method, str):
            if is_notification:
                return None
            return self._error(request_id, _INVALID_REQUEST, "Missing/invalid 'method'")

        params = request.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if is_notification:
                return None
            return self._error(request_id, _INVALID_PARAMS, "'params' must be an object")

        try:
            if method == "initialize":
                result = self._handle_initialize(params)
            elif method in ("notifications/initialized", "initialized"):
                # THE lifecycle-completing signal — only now is the server
                # allowed to serve tool calls (see __init__'s docstring note).
                # Accepted ONLY when an `initialize` request has already run
                # and left us awaiting this ack (CodeRabbit 4062128236); an
                # `initialized` received first (or replayed after already
                # being consumed) is rejected/ignored — `_initialized` is
                # never set from it.
                if self._awaiting_initialized_ack:
                    self._initialized = True
                    self._awaiting_initialized_ack = False
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = self._handle_tools_list(params)
            elif method == "tools/call":
                if not self._initialized:
                    if is_notification:
                        return None
                    return self._error(
                        request_id,
                        _INVALID_REQUEST,
                        "tools/call received before initialization completed "
                        "('notifications/initialized' not yet received)",
                    )
                result = self._handle_tools_call(params)
            else:
                if is_notification:
                    return None
                return self._error(request_id, _METHOD_NOT_FOUND, f"Unknown method '{method}'")
        except ToolInputError as exc:
            if is_notification:
                return None
            return self._error(request_id, _INVALID_PARAMS, str(exc))
        except Exception as exc:  # noqa: BLE001 - last-resort protocol boundary
            if is_notification:
                return None
            # Same rule as the tool-body handler above: never str(exc) in an
            # error response that reaches the wire.
            return self._error(request_id, _INTERNAL_ERROR, f"{type(exc).__name__}: request failed")

        if is_notification:
            return None
        return self._result(request_id, result)

    # -- method handlers ------------------------------------------------

    def _handle_initialize(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        # Deliberately does NOT set self._initialized — that happens only
        # when 'notifications/initialized' is later received (see __init__).
        # It DOES arm the intermediate ack-awaiting flag so that a
        # subsequent 'notifications/initialized' is accepted (CodeRabbit
        # 4062128236) — an 'initialized' received without this having run
        # first is rejected/ignored (see the dispatch branch above).
        self._awaiting_initialized_ack = True
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def _handle_tools_list(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        return {"tools": _TOOL_DEFINITIONS}

    def _handle_tools_call(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise ToolInputError("'name' must be a string")
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ToolInputError("'arguments' must be an object")

        handler = _TOOL_HANDLERS.get(name)
        if handler is None:
            raise ToolInputError(f"unknown tool '{name}'")

        try:
            if name in _HANDLERS_NEEDING_TELEMETRY_DIR:
                payload = handler(self.backend, arguments, telemetry_dir=self.telemetry_dir)
            else:
                payload = handler(self.backend, arguments)
            is_error = False
        except ToolInputError:
            raise  # bad arguments -> JSON-RPC INVALID_PARAMS, not a tool result
        except Exception as exc:  # noqa: BLE001 - tool-body fault, not a protocol fault
            # NEVER include str(exc) here: `exc` can originate from an
            # injected backend or reconciler source, and neither contract
            # guarantees its message excludes keys/paths/connection details/
            # body content (review finding). Report a closed, generic
            # message — the exception TYPE name only, never its text.
            payload = {
                "status": "ERROR",
                "kind": "TOOL_EXCEPTION",
                "message": f"{type(exc).__name__}: tool execution failed",
            }
            is_error = True
        else:
            # Every failed/uncertain write result (status == "ERROR" — this
            # covers SECRET_BLOCKED and every other ErrorKind: NETWORK,
            # CORRUPTION, SCAN_FAILURE, TIMEOUT_AFTER_COMMIT, CONFLICT_UNKNOWN,
            # INVALID_ARGUMENT, ...) must be flagged isError so a client can't
            # mistake it for success by only checking the top-level flag
            # (review finding — see module docstring "TOOL RESULT SHAPE").
            # STALE/EXISTS are ordinary CAS outcomes, not "ERROR" status, so
            # they correctly stay isError: false.
            if name == "knokeep_write" and payload.get("status") == "ERROR":
                is_error = True

        text = json.dumps(payload, sort_keys=True)
        return {"content": [{"type": "text", "text": text}], "isError": is_error}


# ---------------------------------------------------------------------------
# stdio transport
# ---------------------------------------------------------------------------


def serve_stdio(
    backend: StoreBackend,
    *,
    telemetry_dir: Optional[str] = None,
    in_stream: Optional[IO[str]] = None,
    out_stream: Optional[IO[str]] = None,
) -> None:
    """Run the newline-delimited JSON-RPC loop until stdin is closed."""
    server = KnoKeepServer(backend, telemetry_dir=telemetry_dir)
    reader = in_stream if in_stream is not None else sys.stdin
    writer = out_stream if out_stream is not None else sys.stdout

    for line in reader:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            response: Optional[Dict[str, Any]] = KnoKeepServer._error(
                None, _PARSE_ERROR, "Parse error: invalid JSON"
            )
        else:
            response = server.handle(request)
        if response is not None:
            writer.write(json.dumps(response, sort_keys=True) + "\n")
            writer.flush()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_backend(args: argparse.Namespace) -> StoreBackend:
    if args.backend == "fake":
        return FakeBackend()
    if args.backend == "local":
        root = args.root or default_store_root()          # shared cross-tool default (T-4)
        return LocalBackend(root)
    if args.backend == "objectstore":
        # JUDGMENT CALL: wired in for the task's optional "smoke-test
        # against objectstore (moto)" convenience only — not part of the
        # required local+fake coverage. boto3/moto are never imported here;
        # this only talks to whatever S3-protocol endpoint the caller points
        # it at (e.g. a locally-run moto ThreadedMotoServer in a test).
        from store.objectstore import ObjectStoreBackend

        missing = [
            flag
            for flag, val in (
                ("--endpoint", args.endpoint),
                ("--bucket", args.bucket),
                ("--access-key-id", args.access_key_id),
                ("--secret-access-key", args.secret_access_key),
            )
            if not val
        ]
        if missing:
            raise SystemExit(f"--backend objectstore requires {', '.join(missing)}")
        return ObjectStoreBackend(
            endpoint=args.endpoint,
            bucket=args.bucket,
            region=args.region,
            access_key_id=args.access_key_id,
            secret_access_key=args.secret_access_key,
        )
    if args.backend == "postgres":
        # JUDGMENT CALL: same convenience wiring as objectstore, for the
        # task's optional postgres smoke test (127.0.0.1:5433, trust auth).
        from store.postgres import PostgresBackend

        return PostgresBackend(
            {
                "host": args.pg_host,
                "port": args.pg_port,
                "user": args.pg_user,
                "database": args.pg_database,
            },
            schema=args.pg_schema,
        )
    raise SystemExit(f"unknown --backend '{args.backend}'")


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m mcp.server", description=__doc__)
    parser.add_argument(
        "--backend", choices=["fake", "local", "objectstore", "postgres"], default="fake"
    )
    parser.add_argument("--root", default=None, help="LocalBackend root directory")
    parser.add_argument(
        "--telemetry-dir",
        default=None,
        help="Directory for reconciler telemetry JSONL; omit to disable telemetry entirely",
    )
    # --backend objectstore (optional smoke-test convenience)
    parser.add_argument("--endpoint", default=None)
    parser.add_argument("--bucket", default=None)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--access-key-id", default=None)
    parser.add_argument("--secret-access-key", default=None)
    # --backend postgres (optional smoke-test convenience)
    parser.add_argument("--pg-host", default="127.0.0.1")
    parser.add_argument("--pg-port", type=int, default=5433)
    parser.add_argument("--pg-user", default="knokeep")
    parser.add_argument("--pg-database", default="knokeep_test")
    parser.add_argument("--pg-schema", default="public")
    args = parser.parse_args(argv)
    backend = _build_backend(args)
    serve_stdio(backend, telemetry_dir=args.telemetry_dir)


if __name__ == "__main__":
    main()
