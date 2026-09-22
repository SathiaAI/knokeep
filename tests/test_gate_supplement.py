"""Regression tests for the T-6 round-2 hardening of store/gate.secret_scan
(the unified skill+MCP write door). Each pins a finding from the non-Anthropic
adversarial panel (astra/grok/gemini) so it cannot silently regress:

  - superset coverage: every V1-gate family (original + added) still blocks
  - NO false-positive on legitimate KnoKeep markdown (word-boundary + value gate)
  - encoding bypass closed (UTF-16 / invalid-UTF-8 / NUL refused by persist, and
    recovered by the scanner's byte-preserving view as defense-in-depth)
  - normalization anti-evasion covers ALL families (ZW / fullwidth)
  - quoted/JSON Authorization + getenv-with-literal caught
  - labels-only (no matched value), and no ReDoS on pathological input
"""
from __future__ import annotations

import hashlib
import sys
import time

import pytest

from store import gate
from store.fake import FakeBackend
from store.types import ERROR, OK, ErrorKind


def _blocked(s) -> bool:
    return bool(gate.secret_scan(s.encode("utf-8") if isinstance(s, str) else s))


# Original families (prior detections MUST remain active) + added families
# (superset of the retired V1 skill gate, tests/test_secretgate.py-pinned).
@pytest.mark.parametrize("sample", [
    # original / pre-existing
    "sk-ant-ABCDEFGHIJKLMNOPQRST", "sk_live_ABCDEFGHIJKLMNOPQRST",
    "ghp_ABCDEFGHIJKLMNOPQRSTUV12", "github_pat_ABCDEFGHIJKLMNOPQRST",
    "glpat-ABCDEFGHIJKLMNOPQRST", "AKIAABCDEFGHIJKL12",
    "xoxb-ABCDEFGHIJKLMNOP", "pypi-ABCDEFGHIJKLMNOPQRSTUV",
    "-----BEGIN RSA PRIVATE KEY-----", "postgres://user:s3cretPw@host/db",
    # added families (round 1 superset)
    "ghs_ABCDEFGHIJKLMNOPQRST12", "gho_ABCDEFGHIJKLMNOPQRST12",
    "hf_ABCDEFGHIJKLMNOPQRSTUV", "ya29.ABCDEFGHIJKLMNOP",
    "sk-proj-ABCDEFGHIJKLMNOP", "whsec_ABCDEFGHIJKLMNOPQRST",
    "ASIAABCDEFGHIJKL12", "AIzaSyABCDEFGHIJKLMNOPQRSTUVWX12",
    "npm_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123", "xoxe-ABCDEFGHIJKL",
    "db password = hunter2plaintextvalue",
    "Authorization: Bearer abcdef123456ABCDEF7890",
])
def test_all_families_still_blocked(sample):
    assert _blocked(sample)


# A corpus of real-shaped KnoKeep state/journal markdown + the panel's specific
# exclusion strings. None may be refused.
@pytest.mark.parametrize("legit", [
    "compass: north",
    "API key: see README",
    "token: abc",
    "## Secret: rotation documented",
    "Password: ask admin",
    "OPENROUTER_API_KEY (in .env, not stored)",
    "## Architecture\nPython 3.11 CLI. Store gate lives in store/gate.py.\n\n"
    "## Path & Variable Directory\nSTORE = ~/knokeep/store; the auth token flow "
    "is in docs/AUTH.md.\n\n## Hard Constraints\nNever write secret values.",
    "## Completed & Verified\n- wired skill onto store\n\n## Active State\n"
    "reviewing the secret gate; API key handling documented, not stored\n\n"
    "## Next Step\nrun pytest, then flush-log",
    "[2026-09-21T00:00:00Z] discussed the password reset flow and token rotation policy",
    "credentials are managed by the platform; see the secret manager, not here",
])
def test_real_markdown_corpus_is_not_blocked(legit):
    assert not _blocked(legit)


@pytest.mark.parametrize("payload", [
    "g\u200bhp_" + "a" * 36,                              # zero-width inside ghp_
    "\uff47\uff48\uff50\uff3f" + "\uff41" * 36,           # fullwidth ghp_ (NFKC-folds)
    '{"Authorization":"Bearer abcdefghijklmnop"}',        # quoted/JSON header
    'password=os.getenv("PASSWORD","correcthorsebatterystaple")',  # getenv literal fallback
    "password = hunter2plaintextvalue",
])
def test_round2_true_positives_are_blocked(payload):
    assert _blocked(payload)


def test_scanner_recovers_utf16_ascii_token_defense_in_depth():
    # persist() refuses this outright; the scanner ALSO recovers it via the
    # byte-preserving (NUL-stripped) view, so the closure is layered.
    assert _blocked("ghs_ABCDEFGHIJKLMNOPQRST12".encode("utf-16le"))


def _persist(body):
    return gate.persist(FakeBackend(), "demo/system_state", body,
                        expected_hash=None, doc_type="system_state")


def test_persist_refuses_utf16_body():
    r = _persist("secret=correcthorsebatterystaple".encode("utf-16le"))
    assert isinstance(r, ERROR) and r.kind is ErrorKind.INVALID_ARGUMENT


def test_persist_refuses_nul_body():
    r = _persist(b"has\x00nul here")
    assert isinstance(r, ERROR) and r.kind is ErrorKind.INVALID_ARGUMENT


def test_persist_accepts_plain_utf8_markdown():
    assert isinstance(_persist("## Architecture\nplain text".encode("utf-8")), OK)


def test_labels_never_carry_the_matched_value():
    val = "ghs_ABCDEFGHIJKLMNOPQRST12"
    labels = gate.secret_scan(("token=" + val).encode("utf-8"))
    assert labels and all(val not in lbl for lbl in labels)


@pytest.mark.parametrize("expr", [
    "'eyJ' + 'a'*N", "'a'*N", "'https' + 'a'*N", "'ghp_' + 'a'*N",
    "'password=' + 'a'*N", "'x'*N + '://'",
])
@pytest.mark.parametrize("N", [10_000, 100_000, 1_000_000])
def test_no_redos_subprocess_deadline(expr, N):
    # A SUBPROCESS with a hard timeout: if secret_scan ever hangs on pathological
    # input the child is killed (TimeoutExpired -> test fails) instead of wedging
    # the whole test runner. Multiple input sizes (T-6 r3 refinement).
    import subprocess
    code = ("import sys; sys.path.insert(0, '.'); from store import gate; "
            "N=%d; gate.secret_scan((%s).encode('utf-8')); print('ok')" % (N, expr))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0 and "ok" in r.stdout


def test_persist_fails_closed_on_oversize_without_raising():
    # secret_scan raises ValueError on oversize; persist() must catch it (or its
    # own size guard) and return a WriteResult ERROR, never an unhandled exception.
    r = gate.persist(FakeBackend(), "demo/system_state", b"a" * (8 * 1024 * 1024 + 1),
                     expected_hash=None, doc_type="system_state")
    assert isinstance(r, ERROR)


def test_own_64hex_version_hash_still_accepted():
    h = hashlib.sha256(b"state").hexdigest()
    r = _persist(f"---\nversion_hash: {h}\n---\n## Architecture\nok".encode("utf-8"))
    assert isinstance(r, OK)


# --- T-6 round 3: invisible-char smuggling, quoted-value hiding, getenv scope ---

def test_format_chars_do_not_split_a_token():
    # zero-width, bidi override, soft hyphen, bidi isolate, BOM — a category-based
    # rule strips ALL Cf chars, so none can split a token in the normalized view.
    for cp in (0x200B, 0x202E, 0x00AD, 0x2066, 0xFEFF):
        cf = chr(cp)
        assert _blocked("ghp" + cf + "_" + "a" * 36), hex(cp)
        assert _blocked("ghp_" + cf + "a" * 36), hex(cp)


def test_persist_refuses_invisible_format_chars():
    for cp in (0x202E, 0x00AD, 0x2066):
        r = _persist(("ok " + chr(cp) + "text").encode("utf-8"))
        assert isinstance(r, ERROR) and r.kind is ErrorKind.INVALID_ARGUMENT, hex(cp)


def test_quoted_value_behind_see_prefix_is_blocked():
    # 'see '/'ask ' are no longer placeholder prefixes, so a secret can't hide
    # behind them in a quoted value.
    assert _blocked('password: "see hunter2plaintextvalue"')


def test_unrelated_getenv_in_window_is_not_a_false_positive():
    # A getenv("X") assignment must WRITE even if an unrelated getenv("Y","example")
    # sample sits nearby — the literal check is scoped to this call's parens.
    doc = 'api_key = os.getenv("API_KEY")\n# sample: os.getenv("MODE", "example")\n'
    assert not _blocked(doc)


def test_see_readme_still_writes():
    # the legitimate 'see README' documentation value must remain writable.
    assert not _blocked('secret: "see README"')
    assert not _blocked("token: see README")


def test_secret_scan_fails_closed_on_oversize():
    with pytest.raises(ValueError):
        gate.secret_scan(b"a" * (8 * 1024 * 1024 + 1))
