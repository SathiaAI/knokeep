#!/usr/bin/env python3
"""Read-only health inspection: corrupt stores must not report healthy; health must not mutate storage."""
import hashlib
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "skill", "knokeep_state.py")
sys.path.insert(0, ROOT)

from store import gate
from store.local import LocalBackend
from store.types import sha256_hex, OK
from tests.ctx_helpers import create_ctx

results = []


def check(name, cond):
    results.append((name, cond))
    print(("PASS " if cond else "FAIL ") + name)


def run_health(store):
    p = subprocess.run(
        [sys.executable, STATE, "health", "--store", store],
        capture_output=True,
        text=True,
    )
    try:
        body = json.loads(p.stdout)
    except json.JSONDecodeError:
        body = {}
    return p.returncode, body, p.stdout + p.stderr


def snapshot_tree(root):
    snap = {}
    if not os.path.exists(root):
        return snap
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git",)]
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            try:
                st = os.stat(path)
                with open(path, "rb") as f:
                    snap[path] = (st.st_mtime_ns, st.st_size, f.read())
            except OSError:
                snap[path] = None
    return snap


def assert_unchanged(root, before, after):
    if not os.path.exists(root) and not before:
        return True
    return before == after


def legacy_journal_record(key: str, body: bytes) -> bytes:
    kb = key.encode("utf-8")
    return (
        struct.pack(">I", len(kb))
        + kb
        + struct.pack(">Q", len(body))
        + body
        + hashlib.sha256(body).digest()
    )


# --- valid v2 store ---------------------------------------------------------

good_root = tempfile.mkdtemp(prefix="kk_health_good_")
backend = LocalBackend(good_root)
key = "demo/system_state"
body = b"---\nrevision: 1\nupdated: 2020-01-01T00:00:00Z\n---\n# Demo\n"
r = gate.persist(
    backend, key, body, ctx=create_ctx(), doc_type="system_state"
)
assert isinstance(r, OK)
backend.close()

rc, hj, _ = run_health(good_root)
check("valid v2 store: exit 0", rc == 0 and hj.get("verdict") == "healthy")
check("valid v2 store: store ok", hj.get("store", {}).get("ok") is True)
check("valid v2 projects listed", any(p.get("project") == "demo" for p in hj.get("projects", [])))

# legacy-shaped record only (pre-KKJ2) on fresh dirs
legacy_root = tempfile.mkdtemp(prefix="kk_health_legacy_")
os.makedirs(os.path.join(legacy_root, "journal"), exist_ok=True)
os.makedirs(os.path.join(legacy_root, "data", "leg"), exist_ok=True)
leg_body = b"---\nrevision: 1\n---\nlegacy\n"
leg_key = "leg/system_state"
with open(os.path.join(legacy_root, "data", "leg", "system_state"), "wb") as f:
    f.write(leg_body)
with open(os.path.join(legacy_root, "journal", "journal.log"), "wb") as f:
    f.write(legacy_journal_record(leg_key, leg_body))
rc, hj, _ = run_health(legacy_root)
check("legacy journal + published blob: healthy", rc == 0 and hj.get("verdict") == "healthy")

# --- false healthy repro (UINT64_MAX tail) ----------------------------------

bad_root = tempfile.mkdtemp(prefix="kk_health_bad_")
b2 = LocalBackend(bad_root)
b2._journal_append("proj/system_state", b"---\nrevision: 1\n---\nok\n")
b2.close()
journal_path = os.path.join(bad_root, "journal", "journal.log")
kb = b"proj/system_state"
bad_tail = (
    b"KKJ2"
    + struct.pack(">I", len(kb))
    + kb
    + struct.pack(">Q", (1 << 64) - 1)
    + b"x" * 32
)
with open(journal_path, "ab") as f:
    f.write(bad_tail)
rc, hj, out = run_health(bad_root)
check("overflow journal: nonzero exit", rc != 0)
check("overflow journal: not healthy", hj.get("verdict") != "healthy")
check(
    "overflow journal: store reason present",
    any(p.startswith("store:") for p in hj.get("problems", [])),
)

# --- impossible / torn / digest ---------------------------------------------

imp_root = tempfile.mkdtemp(prefix="kk_health_imp_")
os.makedirs(os.path.join(imp_root, "journal"))
with open(os.path.join(imp_root, "journal", "journal.log"), "wb") as f:
    f.write(b"KKJ2" + struct.pack(">I", 3) + b"abc" + struct.pack(">Q", 9 * 1024 * 1024 + 1))
rc, hj, _ = run_health(imp_root)
check(
    "impossible body length flagged",
    "store:journal_impossible_body_length" in hj.get("problems", []),
)

torn_root = tempfile.mkdtemp(prefix="kk_health_torn_")
os.makedirs(os.path.join(torn_root, "journal"))
with open(os.path.join(torn_root, "journal", "journal.log"), "wb") as f:
    f.write(b"KKJ2" + struct.pack(">I", 1) + b"x")
rc, hj, _ = run_health(torn_root)
check("torn tail flagged", "store:journal_torn_tail" in hj.get("problems", []))

digest_root = tempfile.mkdtemp(prefix="kk_health_digest_")
os.makedirs(os.path.join(digest_root, "journal"))
good = b"payload"
with open(os.path.join(digest_root, "journal", "journal.log"), "wb") as f:
    f.write(
        b"KKJ2"
        + struct.pack(">I", 3)
        + b"k/b"
        + struct.pack(">Q", len(good))
        + good
        + b"\0" * 32
        + struct.pack(">Q", 0)
    )
rc, hj, _ = run_health(digest_root)
check("bad digest flagged", "store:journal_bad_digest" in hj.get("problems", []))

# --- unpublished durable record ---------------------------------------------

unpub_root = tempfile.mkdtemp(prefix="kk_health_unpub_")
b3 = LocalBackend(unpub_root)
b3._journal_append("p/system_state", b"---\nrevision: 2\n---\nonly-journal\n")
b3.close()
rc, hj, _ = run_health(unpub_root)
check("unpublished journal record flagged", "store:data_unpublished" in hj.get("problems", []))

# --- missing store (must not create root) -----------------------------------

missing_parent = tempfile.mkdtemp(prefix="kk_health_missing_parent_")
missing = os.path.join(missing_parent, "no_such_store")
before_exists = os.path.exists(missing)
snap_before = snapshot_tree(missing_parent)
rc, hj, _ = run_health(missing)
snap_after = snapshot_tree(missing_parent)
check("missing store: exit nonzero", rc != 0)
check("store:store_missing in problems", "store:store_missing" in hj.get("problems", []))
check("missing path not created", not os.path.exists(missing) and not before_exists)
check("missing parent unchanged", assert_unchanged(missing_parent, snap_before, snap_after))

# --- unreadable store (injected permission error) ---------------------------

if os.name != "nt":
    unread_root = tempfile.mkdtemp(prefix="kk_health_unread_")
    os.makedirs(os.path.join(unread_root, "journal"))
    with open(os.path.join(unread_root, "journal", "journal.log"), "wb") as f:
        f.write(b"")
    os.chmod(unread_root, 0)
    try:
        rc, hj, _ = run_health(unread_root)
        check(
            "unreadable store flagged",
            rc != 0
            and (
                "store:store_unreadable" in hj.get("problems", [])
                or "store:journal_unreadable" in hj.get("problems", [])
            ),
        )
    finally:
        os.chmod(unread_root, stat.S_IRWXU)

# --- redaction --------------------------------------------------------------

secret_key = "sekret/proj/system_state"
redact_root = tempfile.mkdtemp(prefix="kk_health_redact_")
b4 = LocalBackend(redact_root)
b4._journal_append(secret_key, b"---\nrevision: 1\n---\nbody\n")
b4.close()
with open(os.path.join(redact_root, "journal", "journal.log"), "ab") as f:
    f.write(b"\xff\xff\xff\xff")
_, _, combined = run_health(redact_root)
check("diagnostics omit absolute store path", redact_root not in combined)
check("diagnostics omit journal key string", secret_key not in combined)

# --- no mutation on synthetic store -----------------------------------------

mut_root = tempfile.mkdtemp(prefix="kk_health_mut_")
b5 = LocalBackend(mut_root)
gate.persist(
    b5,
    "m/system_state",
    b"---\nrevision: 1\n---\nstable\n",
    ctx=create_ctx(),
    doc_type="system_state",
)
b5.close()
before = snapshot_tree(mut_root)
time.sleep(0.05)
run_health(mut_root)
after = snapshot_tree(mut_root)
check("health does not mutate store bytes/mtimes", before == after)

for d in (
    good_root,
    legacy_root,
    bad_root,
    imp_root,
    torn_root,
    digest_root,
    unpub_root,
    missing_parent,
    redact_root,
    mut_root,
):
    if os.path.exists(d):
        import shutil

        shutil.rmtree(d, ignore_errors=True)
if os.name != "nt":
    import shutil

    shutil.rmtree(unread_root, ignore_errors=True)

passed = sum(1 for _, c in results if c)
print(f"\n{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
