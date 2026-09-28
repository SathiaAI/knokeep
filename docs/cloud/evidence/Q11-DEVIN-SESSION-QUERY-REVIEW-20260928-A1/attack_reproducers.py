"""Q11 independent attack reproducers (stdlib only; disposable temp stores)."""
import base64, hashlib, json, os, subprocess, sys, tempfile
REPO = os.path.abspath(sys.argv[1]); sys.path.insert(0, REPO)
from application.session_queries import (list_sessions, read_session, SessionQueryError,
    MAX_LIST_SCAN_KEYS, validate_list_arguments)
from application.identifiers import valid_id
from store.fake import FakeBackend
from store.types import Blob
from store.local import LocalBackend
import store.local
CLI = [sys.executable, os.path.join(REPO, "skill/knokeep_state.py")]
R = []
def rec(name, ok, detail=""):
    R.append((name, ok)); print(("PASS " if ok else "FAIL ") + name + (" :: " + str(detail) if detail else ""))
def cli(*args, env=None):
    p = subprocess.run(CLI + list(args), capture_output=True, text=True, env=env)
    return p.returncode, p.stdout.strip(), p.stderr.strip()
def j(s):
    try: return json.loads(s.splitlines()[-1])
    except Exception: return None
def append(store, proj, sid, entry):
    return cli("session-append", "--store", store, "--project", proj, "--session-id", sid, "--entry", entry)

# --- identifiers
for v, exp in [("p\n", False), ("p\r", False), ("a b", False), ("p\u00a0", False), ("\u0661", False),
               ("con.txt", False), ("CON", False), (".x", False), ("x.", False), ("a"*64, True), ("a"*65, False),
               ("ok-1_2.3", True), (None, False), (b"x", False), ("", False)]:
    rec(f"valid_id({v!r})=={exp}", valid_id(v) == exp)

# --- validation before backend creation (CLI): nonexistent store must not be created
base = tempfile.mkdtemp(prefix="q11a-")
for args, code, reason in [
    (("session-list", "--project", "bad/x"), 2, "invalid_project_id"),
    (("session-list", "--project", "p", "--list-limit", "0"), 2, "invalid_limit"),
    (("session-list", "--project", "p", "--list-limit", "201"), 2, "invalid_limit"),
    (("session-list", "--project", "p", "--list-limit", "-1"), 2, "invalid_limit"),
    (("session-list", "--project", "p", "--list-limit", "1e2"), 2, "invalid_limit"),
    (("session-list", "--project", "p", "--list-limit", "\u0665"), 2, "invalid_limit"),
    (("session-list", "--project", "p", "--after", "../x"), 2, "invalid_after"),
    (("session-list", "--project", "p", "--session-id", "s"), 2, "invalid_argument"),
    (("session-read", "--project", "p"), 2, "missing_session_id"),
    (("session-read", "--project", "p", "--session-id", "s\n"), 2, "invalid_session_id"),
    (("session-read", "--project", "p", "--session-id", "s", "--after", "a"), 2, "invalid_argument"),
    (("session-read", "--project", "p", "--session-id", "s", "--entry", "x"), 2, "invalid_argument"),
    (("session-read", "--bogus"), 2, "invalid_argument"),
    (("session-read",), 2, "missing_project"),
    (("session-list", "--project", ""), 2, "missing_project"),
]:
    st = os.path.join(base, "never-" + str(len(R)))
    c, o, e = cli(*args, "--store", st)
    jj = j(o)
    rec(f"prevalidate {args[:1]} {reason}", c == code and jj and jj.get("reason") == reason and not os.path.exists(st),
        f"exit={c} out={o[:120]} created={os.path.exists(st)}")

# --- round trip, namespace isolation, exact bytes
st = os.path.join(base, "s1")
for proj, sid in [("demo", "a"), ("demo", "b"), ("demo-extra", "z"), ("demo.x", "y"), ("demo", "c")]:
    c, o, e = append(st, proj, sid, f"entry {proj}/{sid}")
    assert c == 0, (c, o, e)
c, o, e = cli("session-list", "--store", st, "--project", "demo")
rec("list demo isolated from demo-extra/demo.x", c == 0 and j(o)["session_ids"] == ["a", "b", "c"], o)
c, o, e = cli("--store", st, "--project", "demo", "session-list", "--list-limit", "2")
jj = j(o); rec("paging limit=2 truncated/next_after", c == 0 and jj["session_ids"] == ["a", "b"] and jj["truncated"] and jj["next_after"] == "b", o)
c, o, e = cli("session-list", "--store", st, "--project", "demo", "--list-limit", "2", "--after", "b")
jj = j(o); rec("paging after=b", c == 0 and jj["session_ids"] == ["c"] and not jj["truncated"] and "next_after" not in jj, o)
c, o, e = cli("session-list", "--store", st, "--project", "demo", "--after", "zzz")
rec("after beyond end -> empty ok", c == 0 and j(o)["session_ids"] == [] and j(o)["truncated"] is False, o)
c, o, e = cli("session-list", "--store", st, "--project", "nothere")
rec("unknown project -> empty ok exit0", c == 0 and j(o)["session_ids"] == [], o)
c, o, e = cli("session-read", "--store", st, "--project", "demo", "--session-id", "a")
jj = j(o); lb = LocalBackend(os.path.realpath(st)); blob = lb.read("demo/sessions/a"); lb.close()
raw = base64.b64decode(jj["body_base64"])
rec("read exact bytes == backend bytes; hash sha256", c == 0 and raw == blob.body and jj["content_hash"] == hashlib.sha256(raw).hexdigest()
    and jj["body_utf8"].encode() == raw, f"len={len(raw)}")
c, o, e = cli("session-read", "--store", st, "--project", "demo", "--session-id", "missing")
rec("read missing -> found false exit0", c == 0 and j(o)["found"] is False, o)
c, o, e = cli("session-read", "--store", st, "--project", "demo-extra", "--session-id", "a")
rec("read cross-namespace -> found false", c == 0 and j(o)["found"] is False, o)
ev = os.path.join(os.path.realpath(st), ".knokeep-eval", "events.jsonl")
before = open(ev).read() if os.path.exists(ev) else ""
cli("session-list", "--store", st, "--project", "demo"); cli("session-read", "--store", st, "--project", "demo", "--session-id", "a")
after = open(ev).read() if os.path.exists(ev) else ""
rec("queries do not append telemetry", before == after)

# --- conflict notes session ('conflicts' session id) is listed as a normal session? (informational)
# --- non-UTF8 bytes / CRLF / BOM via application layer
fb = FakeBackend()
for sid, body in [("bin", b"\xff\xfe\x00raw"), ("crlf", b"a\r\nb\r\n"), ("bom", b"\xef\xbb\xbfhi")]:
    fb._data = getattr(fb, "_data", None)
def fake_read_factory(mapping):
    class B:
        closed = False
        def read(self, k):
            v = mapping.get(k)
            return v
        def list(self, p):
            return iter([k for k in sorted(mapping) if k.startswith(p)])
        def close(self): self.closed = True
    return B()
def mkblob(key, body, vh=None):
    return Blob(body=body, version_hash=vh or hashlib.sha256(body).hexdigest())
try:
    m = {f"p/sessions/{s}": mkblob(f"p/sessions/{s}", b) for s, b in [("bin", b"\xff\xfe\x00raw"), ("crlf", b"a\r\nb\r\n"), ("bom", b"\xef\xbb\xbfhi")]}
    B = fake_read_factory(m)
    r = read_session(B, "p", "bin"); rec("non-utf8: base64 exact, body_utf8 None", base64.b64decode(r.body_base64) == b"\xff\xfe\x00raw" and r.body_utf8 is None)
    r = read_session(B, "p", "crlf"); rec("CRLF preserved", r.body_utf8 == "a\r\nb\r\n")
    r = read_session(B, "p", "bom"); rec("BOM preserved in utf8+b64", r.body_utf8 == "\ufeffhi" and base64.b64decode(r.body_base64)[:3] == b"\xef\xbb\xbf")
    m2 = {"p/sessions/x": mkblob("p/sessions/x", b"abc", "0"*64)}
    try: read_session(fake_read_factory(m2), "p", "x"); rec("hash_mismatch raises", False)
    except SessionQueryError as ex: rec("hash_mismatch raises (not notfound)", ex.reason == "hash_mismatch")
except Exception as ex:
    rec("fake-blob setup", False, repr(ex))

# --- iterator failures / limits / malformed keys (app layer)
class Boom:
    def __init__(self, keys, fail_at=None, exc=RuntimeError): self.keys, self.fail_at, self.exc = keys, fail_at, exc
    def list(self, p):
        for i, k in enumerate(self.keys):
            if self.fail_at is not None and i == self.fail_at: raise self.exc("mid-iter")
            yield k
for n, ok in [(MAX_LIST_SCAN_KEYS, True), (MAX_LIST_SCAN_KEYS + 1, False)]:
    try: r = list_sessions(Boom([f"p/sessions/s{i:05d}" for i in range(n)]), "p", limit=200); got = ("ok", len(r.session_ids), r.truncated)
    except SessionQueryError as ex: got = ("err", ex.reason)
    rec(f"scan bound n={n}", (got[0] == "ok") == ok, got)
try: list_sessions(Boom(["p/sessions/a", "p/sessions/b"], fail_at=1), "p"); rec("mid-iter exception propagates", False)
except RuntimeError: rec("mid-iter exception propagates (no partial page)", True)
for bad in ["p/sessions/a/b", "p/sessions/", "p/sessions/.x", "p/sessions/con", "p/sessions/a\n"]:
    try: list_sessions(Boom([bad]), "p"); rec(f"malformed key {bad!r} errors", False)
    except SessionQueryError as ex: rec(f"malformed key {bad!r} errors", ex.reason == "invalid_session_key")
dup = Boom(["p/sessions/a", "p/sessions/a", "p/sessions/b"])
r = list_sessions(dup, "p", limit=1); rec("duplicate keys dedup + truncated", r.session_ids == ("a",) and r.truncated and r.next_after == "a")
for lim in [True, 1.0, "5", 0, 201, None]:
    try: validate_list_arguments("p", limit=lim); rec(f"limit {lim!r} rejected", False)
    except SessionQueryError as ex: rec(f"limit {lim!r} rejected", ex.reason == "invalid_limit")
class NoList:
    def list(self, p): raise AssertionError("backend touched")
    def read(self, k): raise AssertionError("backend touched")
for fn in [lambda: list_sessions(NoList(), "bad/p"), lambda: list_sessions(NoList(), "p", after="a b"),
           lambda: read_session(NoList(), "p", "..")]:
    try: fn(); rec("app validates before backend", False)
    except SessionQueryError: rec("app validates before backend", True)
    except AssertionError: rec("app validates before backend", False, "backend touched")

# --- CLI backend errors: corruption -> exit1 JSON, never found:false
st2 = os.path.join(base, "s2"); append(st2, "demo", "a", "x")
pub = os.path.join(os.path.realpath(st2), "data", "demo", "sessions", "orphan")
open(pub, "wb").write(b"no journal record")
c, o, e = cli("session-list", "--store", st2, "--project", "demo")
rec("orphan published blob -> exit1 error JSON", c == 1 and j(o) and j(o).get("blocked") is True, f"exit={c} out={o[:160]} err={e[-160:]}")
c, o, e = cli("session-read", "--store", st2, "--project", "demo", "--session-id", "orphan")
rec("read orphan -> exit1 error (not found:false)", c == 1 and j(o) and j(o).get("blocked") is True, f"exit={c} out={o[:160]}")
c, o, e = cli("session-read", "--store", st2, "--project", "demo", "--session-id", "a")
rec("healthy key still readable beside orphan", c == 0 and j(o)["found"] is True, f"exit={c} out={o[:120]}")

# --- legacy CLI compatibility
st3 = os.path.join(base, "s3")
c, o, e = cli("init", "--store", st3, "--project", "leg"); rec("legacy init exit0", c == 0, o[:120])
c, o, e = append(st3, "leg", "s", "hello"); rec("legacy session-append exit0", c == 0, o[:120])
c, o, e = cli("health", "--store", st3, "--project", "leg"); rec("legacy health runs (pretty JSON)", c == 0 and json.loads(o) is not None, f"exit={c}")
c, o, e = cli("init", "--bogus", "--store", st3); rec("legacy argparse error stays prose (exit2, stderr)", c == 2 and o == "" and "usage" in e, f"out={o[:80]}")
c, o, e = cli("init", "--project", "session-list", "--bogus"); rec("value 'session-list' after --project not misdetected", c == 2 and o == "", f"out={o[:120]}")
c, o, e = cli("init", "--proj", "session-list", "--bogus"); rec("abbrev --proj value not misdetected as query", c == 2 and o == "", f"exit={c} out={o[:120]}")
c, o, e = cli("init", "--store=session-read", "--bogus"); rec("--store=session-read value not misdetected", c == 2 and o == "", f"exit={c} out={o[:120]}")
c, o, e = cli("session-append", "--store", st3, "--project", "leg", "--session-id", "s\n", "--entry", "x")
rec("write path now rejects trailing-LF id", c != 0, f"exit={c} out={o[:120]}")
ev3 = os.path.join(os.path.realpath(st3), ".knokeep-eval", "events.jsonl")
rec("legacy commands still emit telemetry", os.path.exists(ev3) and os.path.getsize(ev3) > 0)
# abbreviations for query options
c, o, e = cli("session-list", "--store", st, "--proj", "demo", "--list", "1"); rec("argparse abbrev --list accepted (info)", c == 0, f"exit={c} out={o[:100]}")

# --- base comparison for abbreviated-option misdetection
import shutil
bd = tempfile.mkdtemp(prefix="q11base-"); bks = os.path.join(bd, "knokeep_state.py")
subprocess.run(["/usr/bin/git", "-C", REPO, "show", "bc9fe11004c4568a72adf08d463048aca26daa92:skill/knokeep_state.py"], stdout=open(bks, "w"), check=True)
shutil.copy(bks, os.path.join(REPO, "skill", "_q11_base_tmp.py"))
try:
    p = subprocess.run([sys.executable, os.path.join(REPO, "skill", "_q11_base_tmp.py"), "init", "--proj", "session-list", "--bogus"], capture_output=True, text=True)
    print(f"INFO base init --proj session-list --bogus: exit={p.returncode} stdout={p.stdout.strip()!r} stderr_has_usage={'usage' in p.stderr}")
finally:
    os.remove(os.path.join(REPO, "skill", "_q11_base_tmp.py"))
print(f"TOTAL={len(R)} PASS={sum(ok for _, ok in R)} FAIL={sum(not ok for _, ok in R)}")
