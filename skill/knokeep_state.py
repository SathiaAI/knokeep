#!/usr/bin/env python3
"""KnoKeep state helper v2.1 (stdlib + V2 store engine) — the skill unified onto ONE engine.

Every write goes through THE single write door: store.gate.persist(backend, key, raw_bytes,
expected_hash=, doc_type=). The gate scans key+body and fails closed on a secret; the
LocalBackend adapter owns concurrency + durability (journal-then-atomic-publish, OS-level
cas.lock, full-sha256 content-hash CAS). The skill keeps its higher-level behavior:
bootstrap/resume_line, sectioned flushes, revisions, per-session journals, telemetry,
health/eval. Skill + MCP share ONE store (the LocalBackend root). Schema v1.

V2.1 changes vs V1: safe_write/_atomic/Lock/body_hash-CAS and the write-path secret scan
are gone; the CAS token is the store's 64-hex sha256 (was a 12-hex body hash). Structural
validation (identifier shape, no-newline metadata) stays skill-side. The bootstrap/health
out-of-band store audit enumerates via backend.list()/read() and re-scans each blob through
store.gate (content-based: reject non-utf-8/secret-bearing blobs). knokeep_secretgate retired."""
import sys, os, re, json, datetime, argparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)   # store package (parent) — the shared V2 engine
from store import gate
from store.local import LocalBackend
from store.types import OK, STALE, EXISTS, ERROR, ErrorKind
from store.config import default_store_root

SCHEMA_VERSION = 1
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _auto_sid():                                              # session-append without --session-id: generate a valid one
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M") + "-" + ("%04d" % (os.getpid() % 10000))

def die(**kw):
    kw["blocked"] = True
    raise SystemExit(json.dumps(kw))

RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
def valid_id(v):
    if not isinstance(v, str):                                # missing/None arg -> clean reject, not a TypeError
        return False
    if not ID_RE.match(v) or v in (".", "..") or v.startswith(".") or v.endswith("."):
        return False
    return v.split(".")[0].lower() not in RESERVED and v.lower() not in RESERVED

FM = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.S)

def parse(text):
    m = FM.match(text)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1); fm[k.strip()] = v.strip()
    return fm, m.group(2)

def dump(fm, body):
    head = "\n".join(f"{k}: {v}" for k, v in fm.items())
    return f"---\n{head}\n---\n{body.strip()}\n"

def _readfile(p):
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""

# --- store wiring (V2 engine) ------------------------------------------------

def _validate_project(project):
    if not valid_id(project):
        die(reason="invalid project id", value=project)

def _backend(store):
    return LocalBackend(os.path.realpath(store))

def _data_dir(store, project=None):
    d = os.path.join(os.path.realpath(store), "data")
    return os.path.join(d, project) if project else d

def _key(project, kind, sid=None):
    if kind == "state":
        return f"{project}/system_state"
    if kind == "log":
        return f"{project}/session_log"
    if kind == "journal":
        return f"{project}/sessions/{sid}"
    raise ValueError(kind)

_DOC_TYPE = {"state": "system_state", "log": "session_log", "journal": "journal"}

def _validate_fm(fm):
    """Structural metadata validation kept skill-side (the gate validates the KEY and
    scans bytes, not the skill's frontmatter semantics)."""
    for k, v in fm.items():
        s = str(v)
        if "\n" in s or "\r" in s:
            die(reason="metadata contains newline", field=k)
        if k in ("project_id", "session_id", "client") and not valid_id(s):
            die(reason="invalid identifier", field=k, value=s)
    return fm

def _bytes(fm, body):
    _validate_fm(fm)
    return dump(fm, body).encode("utf-8")

def _persist(backend, key, kind, raw_bytes, expected_hash):
    return gate.persist(backend, key, raw_bytes, expected_hash=expected_hash, doc_type=_DOC_TYPE[kind])

def _require_ok(res):
    """Map a WriteResult onto the skill's die()/JSON contract; return the new 64-hex on OK."""
    if isinstance(res, OK):
        return res.new_hash
    if isinstance(res, STALE):
        die(reason="stale", current_hash=res.current_hash)
    if isinstance(res, EXISTS):
        die(reason="exists", current_hash=res.current_hash)
    if isinstance(res, ERROR):
        if res.kind == ErrorKind.SECRET_BLOCKED:
            die(reasons=list(res.labels))
        die(reason="write_error", kind=res.kind.value)
    die(reason="write_error", kind="unknown")

EVENTS_DIR = ".knokeep-eval"
_SAFE_KEYS = ("reason", "reasons", "findings", "field", "current_hash")

def _labels(code):
    """Pull ONLY non-secret labels from a die() JSON string (findings carry labels, never values)."""
    try:
        d = json.loads(code) if isinstance(code, str) else {}
    except Exception:
        return {"reason": "unparsed"}
    return {k: d[k] for k in _SAFE_KEYS if k in d}

def _event(store, project, op, decision, detail=None):
    """Append one telemetry line. Fail-OPEN: never blocks, alters, or crashes a real operation.
    Lives OUTSIDE the store's data/ dir (never seen by the bootstrap audit); labels/hashes/counts only."""
    try:
        d = os.path.join(os.path.realpath(store), EVENTS_DIR)
        os.makedirs(d, exist_ok=True)
        rec = {"ts": now(), "project": project, "op": op, "decision": decision}
        if detail:
            rec.update(detail)
        with open(os.path.join(d, "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass

# --- commands ----------------------------------------------------------------

def init(store, project):
    _validate_project(project)
    backend = _backend(store)
    skey, lkey = _key(project, "state"), _key(project, "log")
    if backend.read(skey) is None:
        body = "## Architecture\n(tbd)\n\n## Path & Variable Directory\n(tbd)\n\n## Hard Constraints\n(tbd)"
        fm = {"schema_version": SCHEMA_VERSION, "project_id": project, "client": "cowork", "revision": 1, "updated": now()}
        _require_ok(_persist(backend, skey, "state", _bytes(fm, body), None))
    if backend.read(lkey) is None:
        fm = {"schema_version": SCHEMA_VERSION, "project_id": project, "revision": 1, "updated": now()}
        _require_ok(_persist(backend, lkey, "log", _bytes(fm, "## Completed & Verified\n\n## Active State\n\n## Next Step\n"), None))
    return {"ok": True, "base": _data_dir(store, project)}

def flush_state(store, project, new_body, expect_hash=None):
    _validate_project(project)
    backend = _backend(store)
    skey = _key(project, "state")
    blob = backend.read(skey)
    if blob is not None:
        if not expect_hash:
            die(reason="expect_hash required for update", current_hash=blob.version_hash)
        fm, _ = parse(blob.body.decode("utf-8"))
        if not fm:
            die(reason="malformed state frontmatter")
        fm["revision"] = int(fm.get("revision", 0)) + 1
        fm["updated"] = now()
        res = _persist(backend, skey, "state", _bytes(fm, new_body), expect_hash)   # user's hash IS the CAS guard
    else:
        fm = {"schema_version": SCHEMA_VERSION, "project_id": project, "client": "cowork", "revision": 1, "updated": now()}
        res = _persist(backend, skey, "state", _bytes(fm, new_body), None)
    new_hash = _require_ok(res)
    return {"ok": True, "revision": fm["revision"], "version_hash": new_hash}

def flush_log(store, project, new_body, expect_hash=None):
    _validate_project(project)
    backend = _backend(store)
    lkey = _key(project, "log")
    blob = backend.read(lkey)
    if blob is not None:
        if not expect_hash:
            die(reason="expect_hash required for log update", current_hash=blob.version_hash)
        fm, _ = parse(blob.body.decode("utf-8"))
        if not fm:
            die(reason="malformed log frontmatter")
        fm["revision"] = int(fm.get("revision", 0)) + 1
        fm["updated"] = now()
        res = _persist(backend, lkey, "log", _bytes(fm, new_body), expect_hash)
    else:
        fm = {"schema_version": SCHEMA_VERSION, "project_id": project, "revision": 1, "updated": now()}
        res = _persist(backend, lkey, "log", _bytes(fm, new_body), None)
    new_hash = _require_ok(res)
    return {"ok": True, "revision": fm["revision"], "version_hash": new_hash}

def session_append(store, project, session_id, client, entry):
    _validate_project(project)
    if not valid_id(session_id):
        die(reason="invalid session id", value=session_id)
    if not valid_id(client):
        die(reason="invalid client", value=client)
    backend = _backend(store)
    jkey = _key(project, "journal", session_id)
    for _ in range(50):                                       # bounded CAS retry: same-session concurrent appends
        blob = backend.read(jkey)
        if blob is None:
            fm = {"schema_version": SCHEMA_VERSION, "project_id": project,
                  "session_id": session_id, "client": client, "updated": now()}
            body = "## Journal\n"
            expect = None
        else:
            fm, body = parse(blob.body.decode("utf-8"))
            fm = fm or {"schema_version": SCHEMA_VERSION, "project_id": project,
                        "session_id": session_id, "client": client}
            fm["updated"] = now()
            body = body or "## Journal\n"
            expect = blob.version_hash
        body = body + f"[{now()}] {entry}\n"
        res = _persist(backend, jkey, "journal", _bytes(fm, body), expect)
        if isinstance(res, OK):
            return {"ok": True, "log": jkey}
        if isinstance(res, ERROR) and res.kind == ErrorKind.SECRET_BLOCKED:
            die(reasons=list(res.labels))
        if isinstance(res, (STALE, EXISTS)):
            continue                                          # concurrent append/create race — re-read and retry
        if isinstance(res, ERROR):
            die(reason="write_error", kind=res.kind.value)
    die(reason="append_retry_exhausted")

def _section(b, h):
    m = re.search(rf"##\s*{re.escape(h)}\s*\n(.*?)(?=\n##|\Z)", b, re.S)
    return (m.group(1).strip() if m else "")[:400]

_BENIGN_STORE_FILES = {".ds_store", "thumbs.db", "desktop.ini"}

def _audit_store(backend, project):
    """Out-of-band store audit (single gate, T-2/T-3). Every in-helper write is
    already gate-scanned, so this catches a BYPASS: a secret or non-text blob
    written straight to the store. Enumerates via backend.list() (not a raw dir
    walk) and re-scans each blob's bytes through store.gate — content-based, so a
    stray binary blob is refused regardless of its file name/extension. Benign
    OS/tooling files (.DS_Store, Thumbs.db, desktop.ini) are skipped."""
    findings = []
    for key in backend.list(project + "/"):
        name = key.rsplit("/", 1)[-1].lower()
        if name in _BENIGN_STORE_FILES:
            continue
        try:
            blob = backend.read(key)
        except Exception:
            findings.append({"key": key, "reason": "unreadable blob in store"}); continue
        if blob is None:
            continue
        if not gate._is_acceptable_text(blob.body):
            findings.append({"key": key, "reason": "non-text/binary content in store"}); continue
        hits = gate.secret_scan(blob.body)
        if hits:
            findings.append({"key": key, "reasons": hits})
    return findings


def bootstrap(store, project):
    _validate_project(project)
    backend = _backend(store)
    findings = _audit_store(backend, project)
    if findings:
        die(reason="store contains secrets - refusing to resume", findings=findings[:10])
    sblob = backend.read(_key(project, "state"))
    lblob = backend.read(_key(project, "log"))
    fm, _ = parse(sblob.body.decode("utf-8")) if sblob else ({}, "")
    _, lbody = parse(lblob.body.decode("utf-8")) if lblob else ({}, "")
    active = _section(lbody, "Active State"); nxt = _section(lbody, "Next Step")
    vh = sblob.version_hash if sblob else None
    lh = lblob.version_hash if lblob else None
    rev = int(fm["revision"]) if fm.get("revision") else None
    return {"version_hash": vh, "revision": rev, "log_hash": lh,
            "active": active, "next": nxt,
            "resume_line": f"resuming: {active or '(none)'} / next: {nxt or '(none)'} / v{(vh or '?')[:12]}"}

def rollup(store, project):
    _validate_project(project)
    backend = _backend(store)
    n = 0
    for key in backend.list(project + "/sessions/"):
        blob = backend.read(key)
        if blob is None:
            continue
        _, b = parse(blob.body.decode("utf-8"))
        n += sum(1 for ln in b.splitlines() if ln.startswith("["))
    return {"ok": True, "session_entries": n}                # consolidation writer (future) must use _persist

def evaluate(store):
    """Layer-3 scorecard: aggregate the telemetry log into the dogfood soak metrics.
    Reads <store>/.knokeep-eval/events.jsonl. The one metric it CANNOT produce is a
    leaked secret (a miss is invisible here by definition) - that is Layer-2's job."""
    ev = os.path.join(os.path.realpath(store), EVENTS_DIR, "events.jsonl")
    rows = []
    if os.path.exists(ev):
        for ln in open(ev, encoding="utf-8"):
            ln = ln.strip()
            if ln:
                try: rows.append(json.loads(ln))
                except Exception: pass
    def _has(r, label):
        rs = r.get("reasons") or []
        fs = [f.get("reason", "") for f in (r.get("findings") or [])]
        return any(label in str(x) for x in rs + fs) or label in str(r.get("reason", ""))
    allow = [r for r in rows if r.get("decision") == "allow"]
    block = [r for r in rows if r.get("decision") == "block"]
    error = [r for r in rows if r.get("decision") == "error"]
    boot = [r for r in rows if r.get("op") == "bootstrap"]
    sc = {
        "events": len(rows),
        "writes_allowed": len(allow),
        "blocks_total": len(block),
        "secret_blocks": sum(1 for r in block if _has(r, "key") or _has(r, "secret")
                             or _has(r, "token") or _has(r, "private key") or _has(r, "URI")),
        "concurrency_blocks": sum(1 for r in block if _has(r, "stale") or _has(r, "lock_timeout")
                                  or _has(r, "BUSY")),
        "bootstrap_refusals": sum(1 for r in boot if r.get("decision") == "block"),
        "clean_resumes": sum(1 for r in boot if r.get("decision") == "allow"),
        "errors": len(error),
        "note": "leaks past the gate are NOT measurable here - run the Layer-2 audit scan",
    }
    return sc

def health(store, window_hours=24):
    """One-shot health verdict: scorecard + independent audit + per-project freshness.
    Verdict reflects RECENT activity (default 24h): 'attention' (exit 1) if the gate
    errored in the window or a leak is present now; else 'healthy'. Missing gitleaks is a note."""
    sc = evaluate(store)
    recent_errors = 0
    ev = os.path.join(os.path.realpath(store), EVENTS_DIR, "events.jsonl")
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=window_hours)
    if os.path.exists(ev):
        for ln in open(ev, encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
                if r.get("decision") == "error":
                    ts = datetime.datetime.strptime(r.get("ts", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
                    if ts >= cutoff:
                        recent_errors += 1
            except Exception:
                pass
    leaks = None; scanner = "unavailable"
    try:                                                    # Layer-2 audit, best-effort; scan the published blobs only
        sys.path.insert(0, os.path.join(_ROOT, "tools"))
        import knokeep_audit
        a = knokeep_audit.audit(_data_dir(store))
        leaks = a.get("leaks"); scanner = a.get("scanner")
    except Exception:
        pass
    projects = []
    try:
        b = _backend(store)
        for key in b.list(""):
            if not key.endswith("/system_state") or key.count("/") != 1:
                continue                                     # top-level project state docs only
            blob = b.read(key)
            if blob is None:
                continue
            fm, _ = parse(blob.body.decode("utf-8"))
            projects.append({"project": key[: -len("/system_state")],
                             "revision": fm.get("revision"), "updated": fm.get("updated")})
    except Exception:
        pass
    problems, notes = [], []
    if recent_errors > 0: problems.append("errors_last_%dh=%d" % (window_hours, recent_errors))
    if leaks: problems.append("leaks>0")
    if sc.get("errors", 0) > 0 and recent_errors == 0:
        notes.append("%d historical error(s) outside the %dh window (resolved)" % (sc.get("errors", 0), window_hours))
    if leaks is None: notes.append("audit_unavailable (gitleaks not found)")
    verdict = "healthy" if not problems else "attention"
    return {"verdict": verdict, "window_hours": window_hours, "recent_errors": recent_errors,
            "problems": problems, "notes": notes,
            "scorecard": sc, "audit": {"scanner": scanner, "leaks": leaks}, "projects": projects}

def _bodyfile(path):
    if not path or not os.path.exists(path):
        die(reason="missing --body-file", path=path)         # empty file is allowed; missing is an error
    return _readfile(path)

def _entry(a):
    if a.entry_file == "-":
        return sys.stdin.read().rstrip("\n")
    if a.entry_file:
        return _bodyfile(a.entry_file)
    return a.entry or ""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["init", "flush-state", "flush-log", "session-append", "bootstrap", "rollup", "eval", "health"])
    ap.add_argument("--store"); ap.add_argument("--project")   # --store optional: defaults to the shared cross-tool root
    ap.add_argument("--session-id"); ap.add_argument("--client", default="cowork")
    ap.add_argument("--body-file"); ap.add_argument("--entry"); ap.add_argument("--entry-file"); ap.add_argument("--expect-hash")
    a = ap.parse_args()
    if not a.store:
        a.store = default_store_root()                     # shared cross-tool default (T-4)
    if a.cmd == "eval":                                     # read-only scorecard, no project needed
        print(json.dumps(evaluate(a.store))); return
    if a.cmd == "health":                                   # read-only verdict; exit 1 on attention
        h = health(a.store); print(json.dumps(h, indent=1)); sys.exit(0 if h["verdict"] == "healthy" else 1)
    if not a.project:
        print(json.dumps({"blocked": True, "reason": "--project required"})); sys.exit(2)
    try:
        if a.cmd == "init": result = init(a.store, a.project)
        elif a.cmd == "flush-state": result = flush_state(a.store, a.project, _bodyfile(a.body_file), a.expect_hash)
        elif a.cmd == "flush-log": result = flush_log(a.store, a.project, _bodyfile(a.body_file), a.expect_hash)
        elif a.cmd == "session-append": result = session_append(a.store, a.project, a.session_id or _auto_sid(), a.client, _entry(a))
        elif a.cmd == "bootstrap": result = bootstrap(a.store, a.project)
        elif a.cmd == "rollup": result = rollup(a.store, a.project)
    except SystemExit as e:
        _event(a.store, a.project, a.cmd, "block", _labels(e.code))   # observe the block; never alter it
        raise
    except Exception as e:
        _event(a.store, a.project, a.cmd, "error", {"error": type(e).__name__})
        print(json.dumps({"blocked": True, "reason": "internal error", "error": type(e).__name__})); sys.exit(1)
    _event(a.store, a.project, a.cmd, "allow", {"ok": result.get("ok")})
    print(json.dumps(result))

if __name__ == "__main__":
    main()
