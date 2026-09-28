"""Independent probes for the raw bootstrap audit (3821cdc). Standard library + repo only."""
import hashlib, os, subprocess, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
sys.path.insert(0, ROOT)
from store import gate
from store import health_inspect as hi
from store.local import LocalBackend
from tests.ctx_helpers import create_ctx
import skill.knokeep_state as ks
STATE = os.path.join(ROOT, "skill", "knokeep_state.py")
SECRET = "ghp_EXAMPLE0000000000000000000000"

def store(p="p"):
    r = tempfile.mkdtemp(prefix="kk_ba_")
    b = LocalBackend(r)
    gate.persist(b, f"{p}/system_state", b"---\nrevision: 1\n---\nok\n", ctx=create_ctx(), doc_type="system_state")
    gate.persist(b, f"{p}/session_log", b"---\nrevision: 1\n---\n## Active State\n\n## Next Step\n\n", ctx=create_ctx(), doc_type="session_log")
    b.close()
    return r

def tree(r):
    out = {}
    for d, dn, fn in os.walk(r):
        for n in dn + fn:
            q = os.path.join(d, n); st = os.lstat(q)
            out[os.path.relpath(q, r)] = (st.st_mode, st.st_size, None if os.path.isdir(q) or os.path.islink(q) else hashlib.sha256(open(q, "rb").read()).hexdigest())
    return out

def w(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f: f.write(data)

def boot(r):
    return subprocess.run([sys.executable, STATE, "bootstrap", "--store", r, "--project", "p"], capture_output=True, text=True)

res = []
def rec(name, verdict, detail):
    res.append((name, verdict)); print(f"{verdict} {name} -> {detail}", flush=True)

# 1 control: orphan secret detected
r = store(); w(f"{r}/data/p/leak.md", SECRET.encode())
f = ks._audit_store(r, "p"); rec("orphan-secret-detected", "PASS" if any(x.get("key") == "p/leak.md" and x.get("reasons") for x in f) else "FAIL", f)

# 2 binary orphan detected
r = store(); w(f"{r}/data/p/blob.bin", b"\x00\xff\x00binary")
f = ks._audit_store(r, "p"); rec("binary-orphan-detected", "PASS" if any("non-text" in x.get("reason", "") for x in f) else "FAIL", f)

# 3 benign basename carrying a secret (and binary) is skipped without content scan
r = store(); w(f"{r}/data/p/Thumbs.db", SECRET.encode()); w(f"{r}/data/p/sub/DESKTOP.INI", b"\x00\xffbin")
f = ks._audit_store(r, "p"); p = boot(r)
rec("benign-name-secret-bypass", "FINDING" if not f and p.returncode == 0 else "PASS", f"findings={f} bootstrap_exit={p.returncode}")

# 4 oversized sparse orphan (> _MAX_SCAN_BYTES) fails closed as unreadable
r = store(); q = f"{r}/data/p/big.md"; open(q, "wb").close(); os.truncate(q, 8 * 1024 * 1024 + 1)
f = ks._audit_store(r, "p"); rec("oversized-sparse-fails-closed", "PASS" if any(x.get("reason") == "unreadable blob in store" for x in f) else "FAIL", f)

# 5 symlink inside project blocks (target outside store holds secret)
r = store(); o = tempfile.mkdtemp(prefix="kk_out_"); w(f"{o}/s.md", SECRET.encode())
os.symlink(f"{o}/s.md", f"{r}/data/p/link.md")
f = ks._audit_store(r, "p"); rec("symlink-blocks", "PASS" if any("store_symlink_escape" in x.get("reason", "") for x in f) else "FAIL", f)

# 6 hard link to outside secret: regular file, scanned by content (detected, not classified as link)
r = store(); o = tempfile.mkdtemp(prefix="kk_out_"); w(f"{o}/h.md", SECRET.encode())
try:
    os.link(f"{o}/h.md", f"{r}/data/p/hard.md")
    f = ks._audit_store(r, "p"); rec("hardlink-content-scanned", "PASS" if any(x.get("key") == "p/hard.md" and x.get("reasons") for x in f) else "FAIL", f"{f} nlink={os.lstat(f'{r}/data/p/hard.md').st_nlink}")
except OSError as e:
    rec("hardlink-content-scanned", "SKIP", e)

# 7 ambiguous journal surfaces uncertainty (no silent all-clear)
r = store()
with open(f"{r}/journal/journal.log", "ab") as j: j.write(b"KKJ2\xff\xff")
f = ks._audit_store(r, "p"); rec("ambiguous-journal-uncertain", "PASS" if any("uncertain" in x.get("reason", "") for x in f) else "FAIL", f)

# 8 helper itself mutates nothing (staging + orphan + journal present)
r = store(); w(f"{r}/staging/old.tmp", b"staged"); os.utime(f"{r}/staging/old.tmp", (1, 1)); w(f"{r}/data/p/leak.md", SECRET.encode())
before = tree(r); hi.enumerate_project_published_files_for_audit(r, "p"); ks._audit_store(r, "p")
rec("helper-no-source-mutation", "PASS" if tree(r) == before else "FAIL", "tree unchanged" if tree(r) == before else "tree changed")

# 9 bootstrap (not the helper) mutates the store before audit refuses: LocalBackend startup scavenges aged staging
r = store(); w(f"{r}/staging/old.tmp", b"staged"); os.utime(f"{r}/staging/old.tmp", (1, 1)); w(f"{r}/data/p/leak.md", SECRET.encode())
before = tree(r); p = boot(r); after = tree(r)
gone = sorted(set(before) - set(after))
rec("bootstrap-mutates-before-refusal", "LIMIT" if p.returncode != 0 and gone else "INFO", f"exit={p.returncode} refused={'store contains secrets' in p.stderr} removed={gone}")

# 10 deep nesting: recursive walk vs Python recursion limit
r = store(); _cwd = os.getcwd(); os.chdir(f"{r}/data/p")
for _ in range(1100): os.mkdir("a"); os.chdir("a")
os.chdir(_cwd)
try:
    f = ks._audit_store(r, "p"); rec("deep-nesting", "PASS", f"findings={f[:2]}")
except RecursionError:
    p = boot(r)
    rec("deep-nesting-recursionerror", "FINDING", f"_audit_store raised RecursionError; bootstrap_exit={p.returncode} traceback={'RecursionError' in p.stderr}")

# 11 aggregate cost: many small files, no count/total-byte budget
r = store()
for i in range(2000): w(f"{r}/data/p/n/{i}.md", b"x" * 1024)
t = time.perf_counter(); f = ks._audit_store(r, "p"); dt = time.perf_counter() - t
rec("aggregate-2000-files", "INFO", f"findings={len(f)} seconds={dt:.3f} (no file-count or total-byte cap in helper)")

print("RESULT", "OK" if not any(v == "FAIL" for _, v in res) else "FAIL")
