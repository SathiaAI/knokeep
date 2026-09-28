"""Independent tiny probes for PR66 head 9cd040c. Run from repo root: python3 <this file>. Exit 0 iff all PASS."""
import os, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from store import gate  # noqa: E402
from store.local import LocalBackend  # noqa: E402
from store.types import OK, ERROR, ErrorKind  # noqa: E402
from tests.test_local_backend import create_ctx  # noqa: E402

fails = 0
def rep(name, ok, info=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name + (" -> " + info if info else "")); fails += not ok

def aged(p):
    p.write_bytes(b"evidence"); old = time.time() - 3600; os.utime(p, (old, old))

# 1 ambiguous journal keeps aged staging; read raises corruption; journal bytes unchanged
d = Path(tempfile.mkdtemp()); b = LocalBackend(d)
gate.persist(b, "p/a", b"one", ctx=create_ctx(), doc_type="system_state")
b._journal_fh.write(b"\x00\x00\x00\x05p/b"); b._journal_fh.flush(); b.close()
aged(d / "staging" / "x"); jb = (d / "journal" / "journal.log").read_bytes()
f = LocalBackend(d)
try:
    try: f.read("p/a"); r = "no-raise"
    except Exception as e: r = type(e).__name__
    rep("ambiguous-legacy-tail-keeps-staging", (d / "staging" / "x").exists() and f._journal_ambiguous and r == "BackendCorruptionError" and (d / "journal" / "journal.log").read_bytes() == jb, r)
finally: f.close()
# 2 clean store still scavenges aged staging
d = Path(tempfile.mkdtemp()); b = LocalBackend(d); gate.persist(b, "p/a", b"one", ctx=create_ctx(), doc_type="system_state"); b.close()
aged(d / "staging" / "y"); f = LocalBackend(d)
rep("clean-store-scavenges-aged-staging", not (d / "staging" / "y").exists() and not f._journal_ambiguous); f.close()
# 3 orphan published file with empty journal: write fails closed, orphan preserved
d = Path(tempfile.mkdtemp()); b = LocalBackend(d); b.close()
(d / "data" / "p").mkdir(parents=True, exist_ok=True); (d / "data" / "p" / "o").write_bytes(b"orphan")
f = LocalBackend(d)
res = gate.persist(f, "p/o", b"new", ctx=create_ctx(), doc_type="system_state")
other = gate.persist(f, "p/other", b"ok", ctx=create_ctx(), doc_type="system_state")
rep("orphan-empty-journal-write-fails-closed", res == ERROR(ErrorKind.CORRUPTION) and (d / "data" / "p" / "o").read_bytes() == b"orphan", f"target={res} unrelated={type(other).__name__}")
f.close()
# 4 child journal fsync before publish: parent's FIRST op is a write on same key (must see child's record)
d = Path(tempfile.mkdtemp()); parent = LocalBackend(d)
gate.persist(parent, "p/k", gate.make_generation_header(1) + b"v1", ctx=create_ctx(), doc_type="system_state")
child = LocalBackend(d); child._journal_append("p/k", gate.make_generation_header(2) + b"child"); child.close()
res = gate.persist(parent, "p/k", gate.make_generation_header(2) + b"parent", ctx=create_ctx(), doc_type="system_state")
body = parent.read("p/k").body
rep("child-durable-before-publish-parent-write-first", body.endswith(b"child") and not isinstance(res, OK), f"write={res} read_tail={body[-6:]!r}")
parent.close()
print("RESULT", "OK" if not fails else f"{fails} FAILED"); sys.exit(1 if fails else 0)
