"""Protocol-level tests for the KnoKeep MCP server (mcp/server.py).

These drive the server as a REAL subprocess speaking newline-delimited
JSON-RPC 2.0 over its actual stdin/stdout — never by importing
`mcp.server` and calling its Python methods directly — so what is verified
is the actual wire protocol a real MCP client would use: one JSON object
per line in, one JSON object per line out (see mcp/server.py's module
docstring "TRANSPORT / FRAMING" for why newline-delimited rather than
Content-Length framing).

Required coverage (parametrized over BOTH backends, per the task):
  - `local`   (LocalBackend, fresh tmp_path root per test)
  - `fake`    (FakeBackend, in-memory)

Optional (run only if reachable/available, skipped otherwise; run
separately, NOT parametrized into the required matrix):
  - `objectstore` (moto ThreadedMotoServer, if `moto`/`boto3` are importable)
  - `postgres`    (real local PostgreSQL at 127.0.0.1:5433, trust auth,
                    user knokeep, db knokeep_test — if reachable)

Every test exercises: initialize handshake; tools/list; a write via
knokeep_write that lands (OK); a write that is secret-blocked; a CAS write
against a stale expected_hash (STALE); a read that returns the bytes back
verbatim; and a knokeep_reconcile call that returns a drift report.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


class MCPClient:
    """A minimal newline-delimited JSON-RPC 2.0 client over a subprocess's
    stdio — the actual protocol path, not a Python-level shortcut into
    mcp.server's internals."""

    def __init__(self, args: List[str], *, cwd: Path = REPO_ROOT) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mcp.server", *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(cwd),
        )
        self._next_id = 1

    def _send(self, obj: Dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        line = json.dumps(obj)
        assert "\n" not in line
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def _recv(self) -> Dict[str, Any]:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if line == "":
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            raise AssertionError(f"MCP server closed stdout unexpectedly. stderr:\n{stderr}")
        return json.loads(line)

    def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        response = self._recv()
        assert response["id"] == request_id, response
        return response

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self) -> Dict[str, Any]:
        resp = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "knokeep-mcp-tests", "version": "0"},
            },
        )
        self.notify("notifications/initialized")
        return resp

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        resp = self.request("tools/call", {"name": name, "arguments": arguments})
        assert "result" in resp, resp
        return resp["result"]

    def call_tool_payload(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call a tool and decode its single text content block as JSON."""
        result = self.call_tool(name, arguments)
        content = result["content"]
        assert len(content) == 1
        assert content[0]["type"] == "text"
        return json.loads(content[0]["text"])

    def close(self) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.close()
        except BrokenPipeError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Required coverage: local + fake, parametrized
# ---------------------------------------------------------------------------


@pytest.fixture(params=["local", "fake"])
def mcp_client(request, tmp_path):
    backend_name = request.param
    if backend_name == "local":
        root = tmp_path / "store-root"
        args = ["--backend", "local", "--root", str(root)]
    else:
        args = ["--backend", "fake"]
    client = MCPClient(args)
    try:
        yield client, backend_name
    finally:
        client.close()


def test_initialize_handshake(mcp_client):
    client, _ = mcp_client
    resp = client.initialize()
    assert resp["jsonrpc"] == "2.0"
    result = resp["result"]
    assert result["protocolVersion"]
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "knokeep-mcp"


def test_tools_list(mcp_client):
    client, _ = mcp_client
    client.initialize()
    resp = client.request("tools/list")
    names = {t["name"] for t in resp["result"]["tools"]}
    assert names == {
        "knokeep_read",
        "knokeep_list",
        "knokeep_write",
        "knokeep_reconcile",
        "knokeep_health",
    }
    # Every tool advertises a JSON-Schema object inputSchema.
    for tool in resp["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object"


def test_write_lands_ok_and_read_returns_bytes_verbatim(mcp_client):
    client, _ = mcp_client
    client.initialize()

    write_payload = client.call_tool_payload(
        "knokeep_write",
        {"key": "docs/hello.txt", "body": "hello knokeep", "doc_type": "system_state"},
    )
    assert write_payload["status"] == "OK"
    assert write_payload["commit_class"] == "committed"
    new_hash = write_payload["new_hash"]
    assert len(new_hash) == 64  # lowercase 64-hex sha256

    read_result = client.call_tool("knokeep_read", {"key": "docs/hello.txt"})
    assert read_result["isError"] is False
    read_payload = json.loads(read_result["content"][0]["text"])
    assert read_payload["found"] is True
    assert read_payload["version_hash"] == new_hash
    assert read_payload["text"] == "hello knokeep"
    import base64

    assert base64.b64decode(read_payload["body_b64"]) == b"hello knokeep"


def test_read_not_found(mcp_client):
    client, _ = mcp_client
    client.initialize()
    payload = client.call_tool_payload("knokeep_read", {"key": "docs/does-not-exist.txt"})
    assert payload == {"found": False}


def test_list_returns_keys(mcp_client):
    client, _ = mcp_client
    client.initialize()
    client.call_tool_payload(
        "knokeep_write", {"key": "docs/a.txt", "body": "a", "doc_type": "system_state"}
    )
    client.call_tool_payload(
        "knokeep_write", {"key": "docs/b.txt", "body": "b", "doc_type": "system_state"}
    )
    client.call_tool_payload(
        "knokeep_write", {"key": "other/c.txt", "body": "c", "doc_type": "system_state"}
    )
    payload = client.call_tool_payload("knokeep_list", {"prefix": "docs/"})
    assert set(payload["keys"]) == {"docs/a.txt", "docs/b.txt"}


def test_write_secret_blocked_never_leaks_value(mcp_client):
    client, _ = mcp_client
    client.initialize()

    secret_body = "here is my key: AKIAABCDEFGHIJKLMNOP"  # synthetic AWS-shaped look-alike
    result = client.call_tool(
        "knokeep_write",
        {"key": "docs/leaky.txt", "body": secret_body, "doc_type": "system_state"},
    )
    # A secret-blocked write is surfaced as a clear, unambiguous tool error.
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "ERROR"
    assert payload["kind"] == "SECRET_BLOCKED"
    assert payload["commit_class"] == "definitely_not_committed"
    assert "AWS access key" in payload["labels"]

    # The secret value itself must never appear anywhere in the response.
    raw_response_text = json.dumps(result)
    assert "AKIAABCDEFGHIJKLMNOP" not in raw_response_text
    assert secret_body not in raw_response_text

    # And the write must not have landed at all.
    read_payload = client.call_tool_payload("knokeep_read", {"key": "docs/leaky.txt"})
    assert read_payload == {"found": False}


def test_cas_stale_write_returns_stale(mcp_client):
    client, _ = mcp_client
    client.initialize()

    first = client.call_tool_payload(
        "knokeep_write", {"key": "docs/versioned.txt", "body": "v1", "doc_type": "system_state"}
    )
    assert first["status"] == "OK"
    real_hash = first["new_hash"]

    wrong_hash = "0" * 64
    assert wrong_hash != real_hash
    stale = client.call_tool_payload(
        "knokeep_write",
        {
            "key": "docs/versioned.txt",
            "body": "v2-should-not-land",
            "doc_type": "system_state",
            "expected_hash": wrong_hash,
        },
    )
    assert stale["status"] == "STALE"
    assert stale["current_hash"] == real_hash
    assert stale["commit_class"] == "definitely_not_committed"

    # Confirm v1 is still what's stored — the stale write never landed.
    read_payload = client.call_tool_payload("knokeep_read", {"key": "docs/versioned.txt"})
    assert read_payload["text"] == "v1"
    assert read_payload["version_hash"] == real_hash

    # A correct CAS update, by contrast, lands.
    updated = client.call_tool_payload(
        "knokeep_write",
        {
            "key": "docs/versioned.txt",
            "body": "v2",
            "doc_type": "system_state",
            "expected_hash": real_hash,
        },
    )
    assert updated["status"] == "OK"


def test_create_only_exists_when_key_present(mcp_client):
    client, _ = mcp_client
    client.initialize()
    first = client.call_tool_payload(
        "knokeep_write", {"key": "docs/once.txt", "body": "only-once", "doc_type": "system_state"}
    )
    assert first["status"] == "OK"
    again = client.call_tool_payload(
        "knokeep_write",
        {"key": "docs/once.txt", "body": "different-body", "doc_type": "system_state"},
    )
    assert again["status"] == "EXISTS"
    assert again["current_hash"] == first["new_hash"]


def test_write_invalid_argument_before_persist(mcp_client):
    client, _ = mcp_client
    client.initialize()
    # Non-STATE doc_type without the required generation header is rejected
    # by store.gate.persist() before any I/O (contract §6).
    payload = client.call_tool_payload(
        "knokeep_write",
        {"key": "leases/one", "body": "no generation header here", "doc_type": "lease"},
    )
    assert payload["status"] == "ERROR"
    assert payload["kind"] == "INVALID_ARGUMENT"


def test_reconcile_returns_drift_report(mcp_client):
    client, _ = mcp_client
    client.initialize()

    client.call_tool_payload(
        "knokeep_write", {"key": "next_step", "body": "deploy", "doc_type": "system_state"}
    )

    # Agreeing case: no drift.
    agree_payload = client.call_tool_payload(
        "knokeep_reconcile",
        {"keys": ["next_step"], "github_head": "deploy", "tracker_status": "deploy"},
    )
    assert agree_payload["has_drift"] is False
    fact = agree_payload["facts"][0]
    assert fact["fact_key"] == "next_step"
    assert fact["agree"] is True
    assert fact["source_of_truth"] == "store"
    assert fact["source_of_truth_value"] == "deploy"

    # Diverging case: tracker disagrees -> drift is reported, never silently
    # resolved or written back anywhere.
    drift_payload = client.call_tool_payload(
        "knokeep_reconcile",
        {"keys": ["next_step"], "github_head": "deploy", "tracker_status": "blocked"},
    )
    assert drift_payload["has_drift"] is True
    drift_fact = drift_payload["facts"][0]
    assert drift_fact["agree"] is False
    assert set(drift_fact["diverging_sources"]) == {"github", "store", "tracker"}


def test_reconcile_quarantines_secret_looking_external_fact(mcp_client):
    client, _ = mcp_client
    client.initialize()
    client.call_tool_payload(
        "knokeep_write", {"key": "next_step", "body": "deploy", "doc_type": "system_state"}
    )
    payload = client.call_tool_payload(
        "knokeep_reconcile",
        {
            "keys": ["next_step"],
            # synthetic look-alike secret arriving as an "external" tracker fact
            "tracker_status": "ghp_ABCDEFGHIJ0123456789KLMNOPQRSTUVWXYZ",
        },
    )
    fact = payload["facts"][0]
    assert "tracker" in fact["quarantined"]
    assert fact["values"]["tracker"].startswith("[secret:")
    assert "ghp_ABCDEFGHIJ0123456789KLMNOPQRSTUVWXYZ" not in json.dumps(payload)


def test_health(mcp_client):
    client, _ = mcp_client
    client.initialize()
    payload = client.call_tool_payload("knokeep_health", {})
    assert payload["ok"] is True
    assert isinstance(payload["detail"], str)


def test_unknown_tool_is_invalid_params_error(mcp_client):
    client, _ = mcp_client
    client.initialize()
    resp = client.request("tools/call", {"name": "knokeep_does_not_exist", "arguments": {}})
    assert "error" in resp
    assert resp["error"]["code"] == -32602


def test_unknown_method_is_method_not_found(mcp_client):
    client, _ = mcp_client
    client.initialize()
    resp = client.request("totally/unknown/method")
    assert "error" in resp
    assert resp["error"]["code"] == -32601


def test_explicit_null_id_is_rejected_before_dispatch(mcp_client):
    """An explicit `"id": null` is NOT a notification (only an ABSENT 'id'
    is) -- it must be rejected with INVALID_REQUEST before the method (here
    a tools/call write) is ever dispatched, so the write can never commit."""
    client, _ = mcp_client
    client.initialize()

    client.proc.stdin.write(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": None,
                "method": "tools/call",
                "params": {
                    "name": "knokeep_write",
                    "arguments": {
                        "key": "docs/null-id-should-not-land.txt",
                        "body": "should never be written",
                        "doc_type": "system_state",
                    },
                },
            }
        )
        + "\n"
    )
    client.proc.stdin.flush()
    resp = client._recv()
    assert resp.get("id") is None
    assert "error" in resp
    assert resp["error"]["code"] == -32600

    # And the write genuinely never landed.
    read_payload = client.call_tool_payload(
        "knokeep_read", {"key": "docs/null-id-should-not-land.txt"}
    )
    assert read_payload == {"found": False}


def test_tools_call_before_initialized_notification_is_rejected(mcp_client):
    """`initialize` alone does not complete the MCP lifecycle -- a tools/call
    that arrives before the client's `notifications/initialized` must be
    rejected, not served."""
    client, _ = mcp_client
    # Send the initialize REQUEST but deliberately skip the
    # 'notifications/initialized' notification that client.initialize()
    # would normally send.
    resp = client.request(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "knokeep-mcp-tests", "version": "0"},
        },
    )
    assert "result" in resp

    call_resp = client.request(
        "tools/call",
        {
            "name": "knokeep_write",
            "arguments": {
                "key": "docs/pre-lifecycle.txt",
                "body": "should not be servable yet",
                "doc_type": "system_state",
            },
        },
    )
    assert "error" in call_resp
    assert call_resp["error"]["code"] == -32600

    # Completing the handshake now makes the same call servable.
    client.notify("notifications/initialized")
    ok_payload = client.call_tool_payload(
        "knokeep_write",
        {
            "key": "docs/pre-lifecycle.txt",
            "body": "now this lands",
            "doc_type": "system_state",
        },
    )
    assert ok_payload["status"] == "OK"


@pytest.mark.parametrize("bad_id", [True, False, {}, {"a": 1}, [], [1, 2]])
def test_bool_and_container_ids_are_rejected_before_dispatch(mcp_client, bad_id):
    """JSON-RPC 2.0 'id' must be a string or number. `bool` is a Python
    subclass of `int` and must be explicitly rejected rather than silently
    accepted as a number; `dict`/`list` ids must also be rejected. All of
    these must fail with INVALID_REQUEST before any method dispatch -- the
    accompanying tools/call (knokeep_write) must never run/commit
    (CodeRabbit 4062128227)."""
    client, _ = mcp_client
    client.initialize()

    client.proc.stdin.write(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": bad_id,
                "method": "tools/call",
                "params": {
                    "name": "knokeep_write",
                    "arguments": {
                        "key": "docs/bad-id-should-not-land.txt",
                        "body": "should never be written",
                        "doc_type": "system_state",
                    },
                },
            }
        )
        + "\n"
    )
    client.proc.stdin.flush()
    resp = client._recv()
    assert resp.get("id") is None
    assert "error" in resp
    assert resp["error"]["code"] == -32600

    # And the write genuinely never landed (handler never ran).
    read_payload = client.call_tool_payload(
        "knokeep_read", {"key": "docs/bad-id-should-not-land.txt"}
    )
    assert read_payload == {"found": False}


@pytest.mark.parametrize("bad_id_literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_float_ids_are_rejected_before_dispatch(mcp_client, bad_id_literal):
    """Python's default `json.loads` accepts the non-standard JSON constants
    NaN/Infinity/-Infinity as floats, and the default `json.dumps` echoes
    them back as invalid JSON tokens the client cannot correlate. A
    non-finite float 'id' must therefore be rejected with INVALID_REQUEST
    before any method dispatch -- the accompanying tools/call
    (knokeep_write) must never run/commit (CodeRabbit 4062128227,
    non-finite follow-up)."""
    client, _ = mcp_client
    client.initialize()

    # Emit the non-standard constant on the wire literally, exactly as a
    # permissive client's json.dumps(allow_nan=True) would.
    client.proc.stdin.write(
        '{"jsonrpc": "2.0", "id": '
        + bad_id_literal
        + ', "method": "tools/call", "params": {"name": "knokeep_write",'
        ' "arguments": {"key": "docs/nonfinite-id-should-not-land.txt",'
        ' "body": "should never be written", "doc_type": "system_state"}}}\n'
    )
    client.proc.stdin.flush()
    resp = client._recv()
    assert resp.get("id") is None
    assert "error" in resp
    assert resp["error"]["code"] == -32600

    # And the write genuinely never landed (handler never ran).
    read_payload = client.call_tool_payload(
        "knokeep_read", {"key": "docs/nonfinite-id-should-not-land.txt"}
    )
    assert read_payload == {"found": False}


def test_initialized_notification_before_initialize_is_ignored(mcp_client):
    """`notifications/initialized` received BEFORE any `initialize` request
    has completed must be rejected/ignored -- it must never set the server's
    `_initialized` flag on its own (CodeRabbit 4062128236). tools/call must
    stay rejected afterward, and only a proper initialize -> initialized
    sequence can complete the handshake."""
    client, _ = mcp_client

    # Send the completing notification with no prior 'initialize' at all.
    client.notify("notifications/initialized")

    call_resp = client.request(
        "tools/call",
        {
            "name": "knokeep_write",
            "arguments": {
                "key": "docs/initialized-first.txt",
                "body": "should not be servable",
                "doc_type": "system_state",
            },
        },
    )
    assert "error" in call_resp
    assert call_resp["error"]["code"] == -32600

    # A real initialize() (request + notification, in the right order) still
    # completes the handshake normally afterward.
    client.initialize()
    ok_payload = client.call_tool_payload(
        "knokeep_write",
        {
            "key": "docs/initialized-first.txt",
            "body": "now this lands",
            "doc_type": "system_state",
        },
    )
    assert ok_payload["status"] == "OK"


def test_write_error_kinds_other_than_secret_blocked_are_also_isError(mcp_client):
    """Not just SECRET_BLOCKED: EVERY failed knokeep_write result
    (status == 'ERROR') must be isError: true, so a client can't mistake a
    failed/uncertain write for success by only checking the top-level flag.
    INVALID_ARGUMENT (non-STATE doc_type missing its generation header) is
    the one ERROR kind reachable end-to-end through this server without
    fault-injecting a backend."""
    client, _ = mcp_client
    client.initialize()
    result = client.call_tool(
        "knokeep_write",
        {"key": "leases/x", "body": "no generation header", "doc_type": "lease"},
    )
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "ERROR"
    assert payload["kind"] == "INVALID_ARGUMENT"


# ---------------------------------------------------------------------------
# Optional smoke tests: objectstore (moto) and postgres. Not part of the
# required local+fake matrix; skipped when the dependency/service is
# unavailable, per the task ("You may also smoke-test... if convenient").
# ---------------------------------------------------------------------------


def test_mcp_over_objectstore_smoke(tmp_path):
    moto = pytest.importorskip("moto")
    boto3 = pytest.importorskip("boto3")
    from tests.moto_support import (
        DUMMY_ACCESS_KEY_ID,
        DUMMY_REGION,
        DUMMY_SECRET_ACCESS_KEY,
        get_moto_endpoint,
        make_bucket,
    )

    endpoint = get_moto_endpoint()
    bucket = make_bucket()

    client = MCPClient(
        [
            "--backend",
            "objectstore",
            "--endpoint",
            endpoint,
            "--bucket",
            bucket,
            "--region",
            DUMMY_REGION,
            "--access-key-id",
            DUMMY_ACCESS_KEY_ID,
            "--secret-access-key",
            DUMMY_SECRET_ACCESS_KEY,
        ]
    )
    try:
        client.initialize()
        write_payload = client.call_tool_payload(
            "knokeep_write",
            {"key": "smoke/objectstore.txt", "body": "hello objectstore", "doc_type": "system_state"},
        )
        assert write_payload["status"] == "OK"
        read_payload = client.call_tool_payload("knokeep_read", {"key": "smoke/objectstore.txt"})
        assert read_payload["text"] == "hello objectstore"
    finally:
        client.close()


def _pg_reachable() -> bool:
    try:
        pg8000 = __import__("pg8000")
    except ImportError:
        return False
    try:
        conn = pg8000.connect(
            host="127.0.0.1", port=5433, user="knokeep", database="knokeep_test", timeout=2
        )
        conn.close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _pg_reachable(), reason="real local PostgreSQL not reachable at 127.0.0.1:5433")
def test_mcp_over_postgres_smoke():
    import uuid

    schema = f"knokeep_mcp_smoke_{uuid.uuid4().hex[:12]}"
    client = MCPClient(["--backend", "postgres", "--pg-schema", schema])
    try:
        client.initialize()
        write_payload = client.call_tool_payload(
            "knokeep_write",
            {"key": "smoke/postgres.txt", "body": "hello postgres", "doc_type": "system_state"},
        )
        assert write_payload["status"] == "OK"
        read_payload = client.call_tool_payload("knokeep_read", {"key": "smoke/postgres.txt"})
        assert read_payload["text"] == "hello postgres"
    finally:
        client.close()
        # Best-effort schema cleanup so repeated runs don't accumulate.
        try:
            import pg8000

            conn = pg8000.connect(
                host="127.0.0.1", port=5433, user="knokeep", database="knokeep_test"
            )
            cur = conn.cursor()
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.commit()
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# knokeep_write with expected_hash: the advisory lease is HELD through
# persist() (D-006 on the MCP side) and a key already leased elsewhere is an
# ordinary ERROR{BUSY} write outcome, never a generic tool exception.
# ---------------------------------------------------------------------------


def test_overwrite_returns_busy_while_another_holder_leases_the_key(tmp_path):
    """Local backend only: a second LocalBackend over the SAME root (the
    cross-process case, e.g. the skill holding its per-key lease) leases the
    key; the server's CAS-update must report ERROR/BUSY (isError) instead of
    TOOL_EXCEPTION, and succeed once the lease is released."""
    from store.local import LocalBackend

    root = tmp_path / "store-root"
    client = MCPClient(["--backend", "local", "--root", str(root)])
    try:
        client.initialize()
        first = client.call_tool_payload(
            "knokeep_write", {"key": "docs/leased.txt", "body": "v1", "doc_type": "system_state"}
        )
        assert first["status"] == "OK"

        other = LocalBackend(root)
        held = other.lock("docs/leased.txt", ttl_s=30)
        try:
            result = client.call_tool(
                "knokeep_write",
                {"key": "docs/leased.txt", "body": "v2", "doc_type": "system_state",
                 "expected_hash": first["new_hash"]},
            )
        finally:
            other.unlock(held)
        assert result["isError"] is True
        payload = json.loads(result["content"][0]["text"])
        assert payload == {"status": "ERROR", "kind": "BUSY", "commit_class": "definitely_not_committed"}
        assert client.call_tool_payload("knokeep_read", {"key": "docs/leased.txt"})["text"] == "v1"

        retry = client.call_tool_payload(
            "knokeep_write",
            {"key": "docs/leased.txt", "body": "v2", "doc_type": "system_state",
             "expected_hash": first["new_hash"]},
        )
        assert retry["status"] == "OK"
        other.close()
    finally:
        client.close()


def test_overwrite_lease_is_held_across_persist_and_released_after():
    """Unit-level: lock() -> write() -> unlock(), in that order, with the write
    carrying the held lease -- and unlock() still runs when persist raises."""
    from mcp.server import _tool_knokeep_write
    from store.context import Overwrite
    from store.fake import FakeBackend

    class _Recording(FakeBackend):
        def __init__(self):
            super().__init__()
            self.events = []

        def lock(self, key, ttl_s):
            lease = super().lock(key, ttl_s)
            self.events.append(("lock", lease.token))
            return lease

        def write(self, key, body, *, ctx):
            lease = ctx.precondition.lease if isinstance(ctx.precondition, Overwrite) else None
            # The lease must still be HELD (advisory side) at write time.
            self.events.append(("write", lease.token if lease else None, (lease.key in self._locks) if lease else None))
            return super().write(key, body, ctx=ctx)

        def unlock(self, lock):
            self.events.append(("unlock", lock.token))
            return super().unlock(lock)

    backend = _Recording()
    first = _tool_knokeep_write(backend, {"key": "k", "body": "v1", "doc_type": "system_state"})
    assert first["status"] == "OK"
    assert backend.events == [("write", None, None)]  # create-only takes no lease
    backend.events.clear()

    second = _tool_knokeep_write(
        backend, {"key": "k", "body": "v2", "doc_type": "system_state", "expected_hash": first["new_hash"]}
    )
    assert second["status"] == "OK"
    kinds = [e[0] for e in backend.events]
    assert kinds == ["lock", "write", "unlock"]
    token = backend.events[0][1]
    assert backend.events[1] == ("write", token, True)  # held at write time, same lease
    assert backend.events[2] == ("unlock", token)

    # Busy: another holder -> ERROR/BUSY, and no write/unlock is attempted.
    held = backend.lock("k", ttl_s=30)
    backend.events.clear()
    busy = _tool_knokeep_write(
        backend, {"key": "k", "body": "v3", "doc_type": "system_state", "expected_hash": second["new_hash"]}
    )
    assert busy["status"] == "ERROR" and busy["kind"] == "BUSY"
    assert backend.events == []
    backend.unlock(held)

    # persist raising still releases the lease.
    backend.events.clear()

    def _boom(*a, **kw):
        raise RuntimeError("simulated persist failure")

    backend.write = _boom
    with pytest.raises(RuntimeError):
        _tool_knokeep_write(
            backend, {"key": "k", "body": "v4", "doc_type": "system_state", "expected_hash": second["new_hash"]}
        )
    assert [e[0] for e in backend.events] == ["lock", "unlock"]
    assert "k" not in backend._locks


def test_malformed_overwrite_is_refused_before_any_lease_is_taken():
    """A CAS-update request the gate will reject (bad hash / key / body /
    doc_type) must not call lock() first: lock() advances the key's durable
    fence and would supersede a valid lease some other caller still holds."""
    from mcp.server import _tool_knokeep_write
    from store.fake import FakeBackend
    from store.context import overwrite_ctx
    from store import gate

    class _Recording(FakeBackend):
        def __init__(self):
            super().__init__()
            self.lock_calls = 0

        def lock(self, key, ttl_s):
            self.lock_calls += 1
            return super().lock(key, ttl_s)

    backend = _Recording()
    first = _tool_knokeep_write(backend, {"key": "k", "body": "v1", "doc_type": "system_state"})
    assert first["status"] == "OK"
    # Some other caller's still-valid (released-but-unexpired) lease.
    other = backend.lock("k", ttl_s=30)
    backend.unlock(other)
    backend.lock_calls = 0

    bad_requests = [
        {"key": "k", "body": "v2", "doc_type": "system_state", "expected_hash": "not-a-hash"},
        {"key": "../k", "body": "v2", "doc_type": "system_state", "expected_hash": first["new_hash"]},
        {"key": "k", "body": "v2", "doc_type": "lease", "expected_hash": first["new_hash"]},
        {"key": "k", "body": "AKIAIOSFODNN7EXAMPLE token", "doc_type": "system_state", "expected_hash": first["new_hash"]},
    ]
    for args in bad_requests:
        out = _tool_knokeep_write(backend, args)
        assert out["status"] == "ERROR", (args, out)
        assert out["kind"] in ("INVALID_ARGUMENT", "SECRET_BLOCKED"), (args, out)
    assert backend.lock_calls == 0, "malformed requests must never take (and advance) the fence lease"

    # The other caller's lease is still the fence owner and its write lands.
    r = gate.persist(backend, "k", b"v2-other", ctx=overwrite_ctx(first["new_hash"], other), doc_type="system_state")
    assert r.__class__.__name__ == "OK", r
