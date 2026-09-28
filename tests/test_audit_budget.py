"""Bootstrap raw-audit: OS-metadata secret bypass closed; bounded data-tree walks."""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "skill", "knokeep_state.py")
sys.path.insert(0, ROOT)

from store import gate
from store import health_inspect as hi
from store.local import LocalBackend
from tests.ctx_helpers import create_ctx
import skill.knokeep_state as knokeep_state

SECRET = b"ghp_EXAMPLE0000000000000000000000"
results = []
roots = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


def make_store():
    r = tempfile.mkdtemp(prefix="kk_audit_budget_")
    roots.append(r)
    b = LocalBackend(r)
    gate.persist(b, "p/system_state", b"---\nrevision: 1\n---\nok\n", ctx=create_ctx(), doc_type="system_state")
    gate.persist(
        b,
        "p/session_log",
        b"---\nrevision: 1\n---\n## Active State\n\n## Next Step\n\n",
        ctx=create_ctx(),
        doc_type="session_log",
    )
    b.close()
    return r


def put(root, rel, data):
    path = os.path.join(root, "data", *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def nest(base, depth):
    cwd = os.getcwd()
    os.makedirs(base, exist_ok=True)
    os.chdir(base)
    try:
        for _ in range(depth):
            os.mkdir("d")
            os.chdir("d")
    finally:
        os.chdir(cwd)


def _iter_tree(root):
    stack = [root]
    while stack:
        d = stack.pop()
        for ent in os.scandir(d):
            yield ent.path
            if ent.is_dir(follow_symlinks=False):
                stack.append(ent.path)


def snapshot(root):
    out = {}
    for p in _iter_tree(root):
        st = os.lstat(p)
        digest = None
        if os.path.isfile(p) and not os.path.islink(p):
            with open(p, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
        out[os.path.relpath(p, root)] = (st.st_mode, st.st_size, st.st_mtime_ns, digest)
    return out


def remove_tree(root):
    paths = sorted(_iter_tree(root), key=len, reverse=True)
    for p in paths:
        if os.path.isdir(p) and not os.path.islink(p):
            os.rmdir(p)
        else:
            os.unlink(p)
    os.rmdir(root)


def bootstrap(root):
    return subprocess.run(
        [sys.executable, STATE, "bootstrap", "--store", root, "--project", "p"],
        capture_output=True,
        text=True,
    )


def reasons(findings):
    return [f.get("reason", "") for f in findings]


# --- secret under every OS-metadata basename is scanned, never exempt --------
for rel, data in (
    ("p/.DS_Store", SECRET),
    ("p/Thumbs.db", SECRET),
    ("p/desktop.ini", SECRET),
    ("p/sub/deep/ThUmBs.Db", SECRET),
    ("p/x/.ds_STORE", b"\x00\x00\x00\x01Bud1\x00" + SECRET + b"\xff\x00"),
    ("p/DESKTOP.INI", b"\xff\xfe" + SECRET.decode().encode("utf-16-le")),
):
    r = make_store()
    put(r, rel, data)
    before = snapshot(r)
    findings = knokeep_state._audit_store(r, "p")
    check(f"secret under OS-metadata name flagged: {rel}", any(f.get("key") == rel and f.get("reasons") for f in findings))
    check(f"audit leaves bytes unchanged: {rel}", snapshot(r) == before)
    p = bootstrap(r)
    check(f"bootstrap refuses secret in {rel}", p.returncode != 0 and "store contains secrets" in p.stderr)

# --- benign binary OS metadata stays allowed; binary elsewhere still refused ---
r = make_store()
put(r, "p/.DS_Store", b"\x00\x00\x00\x01Bud1\x00\x00\x10\x00\xff")
put(r, "p/sub/Thumbs.db", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1\x00\x00")
check("benign binary OS metadata: no findings", knokeep_state._audit_store(r, "p") == [])
check("benign binary OS metadata: bootstrap proceeds", bootstrap(r).returncode == 0)
put(r, "p/blob.bin", b"\x00\xffbinary")
check(
    "binary under ordinary name still refused",
    "non-text/binary content in store" in reasons(knokeep_state._audit_store(r, "p")),
)

# --- budgets fail closed with machine reasons (both walks) --------------------
r = make_store()
# macOS rejects the snapshot helper's 1100-level absolute paths before audit runs.
# Eighty levels still exceed the product's 64-level limit without hitting PATH_MAX.
probe_depth = 80 if sys.platform == "darwin" else 1100
nest(os.path.join(r, "data", "p"), probe_depth)
before = snapshot(r)
try:
    tree = hi._data_tree_safe_for_audit(hi.Path(r) / "data", hi.Path(r).resolve())
    findings = knokeep_state._audit_store(r, "p")
    ok = tree == ["audit_depth_budget_exceeded"] and "audit blocked: audit_depth_budget_exceeded" in reasons(findings)
except RecursionError:
    ok = False
check(f"{probe_depth}-deep tree: depth budget, no RecursionError", ok)
check("depth budget: bytes unchanged", snapshot(r) == before)
p = bootstrap(r)
check("bootstrap refuses depth budget", p.returncode != 0 and "audit_depth_budget_exceeded" in p.stderr)

r = make_store()
nest(os.path.join(r, "data", "p"), hi._AUDIT_MAX_DEPTH - 1)
check("depth exactly at limit (64 below data/) is allowed", knokeep_state._audit_store(r, "p") == [])
r = make_store()
nest(os.path.join(r, "data", "p"), hi._AUDIT_MAX_DEPTH)
check("depth one past limit is refused", "audit blocked: audit_depth_budget_exceeded" in reasons(knokeep_state._audit_store(r, "p")))
with mock.patch.object(hi, "_data_tree_safe_for_audit", return_value=[]):
    entries, block, _u = hi.enumerate_project_published_files_for_audit(r, "p")
check("project walk alone enforces depth", entries == [] and block == ["audit_depth_budget_exceeded"])

r = make_store()
nest(os.path.join(r, "data", "other"), 4)
with mock.patch.object(hi, "_AUDIT_MAX_DEPTH", 3):
    f = knokeep_state._audit_store(r, "p")
check("prerequisite data-tree walk enforces depth (other project)", "audit blocked: audit_depth_budget_exceeded" in reasons(f))

r = make_store()
for i in range(10):
    put(r, f"p/n/{i}.md", b"ok\n")
before = snapshot(r)
with mock.patch.object(hi, "_AUDIT_MAX_ENTRIES", 5):
    entries, block, _u = hi.enumerate_project_published_files_for_audit(r, "p")
    f = knokeep_state._audit_store(r, "p")
check("entry budget: no entries returned", entries == [] and block == ["audit_entry_budget_exceeded"])
check("entry budget: audit blocked", "audit blocked: audit_entry_budget_exceeded" in reasons(f))
check("entry budget: bytes unchanged", snapshot(r) == before)

r = make_store()
put(r, "p/a.md", b"x" * 300)
before = snapshot(r)
with mock.patch.object(hi, "_AUDIT_MAX_TOTAL_BYTES", 200):
    tree = hi._data_tree_safe_for_audit(hi.Path(r) / "data", hi.Path(r).resolve())
    f = knokeep_state._audit_store(r, "p")
check("byte budget: prerequisite walk fails closed", tree == ["audit_byte_budget_exceeded"])
check("byte budget: audit blocked", "audit blocked: audit_byte_budget_exceeded" in reasons(f))
check("byte budget: bytes unchanged", snapshot(r) == before)

# --- link refusal unchanged ---------------------------------------------------
if hasattr(os, "symlink"):
    r = make_store()
    outside = tempfile.mkdtemp(prefix="kk_audit_budget_out_")
    roots.append(outside)
    with open(os.path.join(outside, "s.md"), "wb") as fh:
        fh.write(SECRET)
    try:
        os.symlink(os.path.join(outside, "s.md"), os.path.join(r, "data", "p", "Thumbs.db"))
        check(
            "symlink under OS-metadata name still refused",
            "audit blocked: store_symlink_escape" in reasons(knokeep_state._audit_store(r, "p")),
        )
    except OSError:
        print("SKIP symlink under OS-metadata name (symlink unavailable)")

for d in roots:
    try:
        remove_tree(d)
    except OSError:
        shutil.rmtree(d, ignore_errors=True)

passed = sum(1 for _, c in results if c)
print(f"\n{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
