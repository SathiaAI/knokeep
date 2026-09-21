"""Shared moto ThreadedMotoServer + boto3-oracle helpers for object-store
tests (contract §9: "real S3-protocol server").

NOTE: `boto3`/`moto` are imported ONLY from this test-support module and from
`tests/test_objectstore.py` — never from `store/objectstore.py`, which is a
zero-dependency stdlib adapter (contract §4.2 "core client = signed HTTP").
This module is not itself a test file (no `test_` prefix, not matched by
`pytest.ini`'s `python_files`), so it is never collected as a test — it is
plain shared test infrastructure, imported by both `tests/test_objectstore.py`
and `conformance/suite.py` (to add the `objectstore` backend to the
backend-agnostic conformance parametrization).
"""
from __future__ import annotations

import atexit
import socket
import threading
import uuid
from typing import Optional

import boto3
from moto.server import ThreadedMotoServer

DUMMY_ACCESS_KEY_ID = "knokeep-test-access-key"
DUMMY_SECRET_ACCESS_KEY = "knokeep-test-secret-key"  # pragma: allowlist secret - synthetic, moto-only
DUMMY_REGION = "us-east-1"

_lock = threading.Lock()
_server: Optional[ThreadedMotoServer] = None
_endpoint: Optional[str] = None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get_moto_endpoint() -> str:
    """Start (once per test process) a real `ThreadedMotoServer` and return
    its base URL. Shared across every test in this process — callers isolate
    themselves from each other with a fresh bucket (`make_bucket()`), the
    same role `tmp_path` plays for the local/git adapters."""
    global _server, _endpoint
    with _lock:
        if _server is None:
            port = _free_port()
            _server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
            _server.start()
            _endpoint = f"http://127.0.0.1:{port}"
            atexit.register(_server.stop)
    assert _endpoint is not None
    return _endpoint


def oracle_client():
    """A `boto3` S3 client pointed at the shared moto server — the
    independent oracle used by tests to verify server-side state directly,
    never used by `store/objectstore.py` itself."""
    return boto3.client(
        "s3",
        endpoint_url=get_moto_endpoint(),
        region_name=DUMMY_REGION,
        aws_access_key_id=DUMMY_ACCESS_KEY_ID,
        aws_secret_access_key=DUMMY_SECRET_ACCESS_KEY,
    )


def make_bucket(prefix: str = "knokeep-test") -> str:
    """Create a fresh, DNS-valid bucket on the shared moto server and return
    its name. A new random bucket per call keeps tests isolated from each
    other on one shared server, exactly like `tmp_path` isolates local/git
    adapters on one shared filesystem."""
    name = f"{prefix}-{uuid.uuid4().hex[:16]}"
    oracle_client().create_bucket(Bucket=name)
    return name


__all__ = [
    "DUMMY_ACCESS_KEY_ID",
    "DUMMY_SECRET_ACCESS_KEY",
    "DUMMY_REGION",
    "get_moto_endpoint",
    "oracle_client",
    "make_bucket",
]
