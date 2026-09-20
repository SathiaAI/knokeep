#!/usr/bin/env python3
"""KnoKeep state helper v2 (stdlib) - single enforced write boundary + hash-guarded
concurrency + fail-closed secret gate. Schema v1 (see docs/SCHEMA.md).
Every write goes through safe_write(): metadata validated, FULL serialized content scanned,
atomic (mkstemp+fsync+os.replace). No other write path exists."""
import sys, os, re, json, time, hashlib, datetime, argparse, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import knokeep_secretgate as sg

SCHEMA_VERSION = 1
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _auto_sid():                                              # session-append without --session-id: generate a valid one
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M") + "-" + ("%04d" % (os.getpid() % 10000))

def body_hash(body):
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()[:12]

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

def paths(store, project):
    if not valid_id(project):
        die(reason="invalid project id", value=project)
    store_abs = os.path.realpath(store)                       # resolve symlinks for containment
    base = os.path.realpath(os.path.join(store_abs, project))
    if os.path.commonpath([store_abs, base]) != store_abs:
        die(reason="path traversal", value=project)
    return {"root": store_abs, "base": base,
            "state": os.path.join(base, "system_state.md"),
            "log": os.path.join(base, "session_log.md"),
            "sessions": os.path.join(base, "sessions")}

def read(p):
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""

def _atomic(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text); fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, p)
        try:                                                  # durability: fsync parent dir where supported
            dfd = os.open(os.path.dirname(p), os.O_RDONLY); os.fsync(dfd); os.close(dfd)
        except (OSError, AttributeError):
            pass
    finally:
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except OSError: pass

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
    Lives OUTSIDE the scanned store (never seen by bootstrap); records labels/hashes/counts only."""
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

def safe_write(path, fm, body):
    """THE single write door: validate metadata, scan FULL serialized content, atomic write."""
    for k, v in fm.items():
        s = str(v)
        if "\n" in s or "\r" in s:
            die(reason="metadata contains newline", field=k)
        if k in ("project_id", "session_id", "client") and not valid_id(s):
            die(reason="invalid identifier", field=k, value=s)
    text = dump(fm, body)
    hits = sg.scan_text(text)                     # scans frontmatter + body together
    if hits:
        die(reasons=hits)
    _atomic(path, text)

class Lock:
    """Advisory lock: O_EXCL lockfile, stale-broken after TTL (crash-safe, cross-platform)."""
    TTL = 30
    def __init__(self, target): self.l = target + ".lock"
    def __enter__(self):
        for _ in range(400):
            try:
                fd = os.open(self.l, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode()); os.close(fd); return self
            except FileExistsError:
                b = self.l + ".breaking"
                try:                                           # a crashed breaker must not deadlock the lock
                    if time.time() - os.path.getmtime(b) > self.TTL:
                        os.remove(b)
                except OSError:
                    pass
                try:
                    stale = time.time() - os.path.getmtime(self.l) > self.TTL
                except OSError:
                    stale = False
                if stale:
                    try:
                        os.close(os.open(b, os.O_CREAT | os.O_EXCL | os.O_WRONLY))  # one breaker wins
                        try:                                   # re-verify SAME stale lock before removing (TOCTOU)
                            if os.path.exists(self.l) and time.time() - os.path.getmtime(self.l) > self.TTL:
                                os.remove(self.l)
                        except OSError:
                            pass
                        try: os.remove(b)
                        except OSError: pass
                        continue
                    except FileExistsError:
                        pass
                time.sleep(0.05)
        die(reason="lock_timeout")
    def __exit__(self, *a):
        try: os.remove(self.l)
        except OSError: pass

def init(store, project):
    P = paths(store, project); os.makedirs(P["sessions"], exist_ok=True)
    with Lock(P["state"]):
        if not os.path.exists(P["state"]):
            body = "## Architecture\n(tbd)\n\n## Path & Variable Directory\n(tbd)\n\n## Hard Constraints\n(tbd)"
            safe_write(P["state"], {"schema_version": SCHEMA_VERSION, "project_id": project, "client": "cowork",
                       "revision": 1, "version_hash": body_hash(body), "updated": now()}, body)
    with Lock(P["log"]):
        if not os.path.exists(P["log"]):
            safe_write(P["log"], {"schema_version": SCHEMA_VERSION, "project_id": project, "revision": 1,
                       "updated": now()}, "## Completed & Verified\n\n## Active State\n\n## Next Step\n")
    return {"ok": True, "base": P["base"]}

def flush_state(store, project, new_body, expect_hash=None):
    P = paths(store, project)
    with Lock(P["state"]):                                  # atomic critical section
        exists = os.path.exists(P["state"])
        fm, cur_body = parse(read(P["state"]))
        if exists and not fm:
            die(reason="malformed state frontmatter")
        cur = body_hash(cur_body) if exists else ""         # recompute, don't trust stored hash
        if exists and not expect_hash:
            die(reason="expect_hash required for update", current_hash=cur)
        if exists and expect_hash != cur:
            die(reason="stale", current_hash=cur)
        fm = fm or {"schema_version": SCHEMA_VERSION, "project_id": project, "client": "cowork"}
        fm["revision"] = int(fm.get("revision", 0)) + 1
        fm["version_hash"] = body_hash(new_body); fm["updated"] = now()
        safe_write(P["state"], fm, new_body)
        rt, rb = parse(read(P["state"]))                    # read-back: confirm own hash + body
    return {"ok": rt.get("version_hash") == body_hash(new_body) == body_hash(rb),
            "revision": fm["revision"], "version_hash": fm["version_hash"]}

def flush_log(store, project, new_body, expect_hash=None):
    P = paths(store, project)
    with Lock(P["log"]):
        exists = os.path.exists(P["log"])
        fm, cur_body = parse(read(P["log"]))
        if exists and not fm:
            die(reason="malformed log frontmatter")
        cur = body_hash(cur_body) if exists else ""
        if exists and not expect_hash:
            die(reason="expect_hash required for log update", current_hash=cur)
        if exists and expect_hash != cur:
            die(reason="stale", current_hash=cur)
        fm = fm or {"schema_version": SCHEMA_VERSION, "project_id": project}
        fm["revision"] = int(fm.get("revision", 0)) + 1
        fm["version_hash"] = body_hash(new_body); fm["updated"] = now()
        safe_write(P["log"], fm, new_body)
    return {"ok": True, "revision": fm["revision"], "version_hash": fm["version_hash"]}

def session_append(store, project, session_id, client, entry):
    P = paths(store, project)
    if not valid_id(session_id): die(reason="invalid session id", value=session_id)
    if not valid_id(client): die(reason="invalid client", value=client)
    logp = os.path.join(P["sessions"], session_id, "log.md")
    os.makedirs(os.path.dirname(logp), exist_ok=True)
    if os.path.commonpath([P["base"], os.path.realpath(os.path.dirname(logp))]) != P["base"]:
        die(reason="session path escapes store")
    with Lock(logp):
        fm, body = parse(read(logp))
        fm = fm or {"schema_version": SCHEMA_VERSION, "project_id": project,
                    "session_id": session_id, "client": client, "updated": now()}
        fm["updated"] = now()
        body = (body or "## Journal\n") + f"[{now()}] {entry}\n"
        safe_write(logp, fm, body)                          # full journal re-scanned each append
    return {"ok": True, "log": logp}

def _section(b, h):
    m = re.search(rf"##\s*{re.escape(h)}\s*\n(.*?)(?=\n##|\Z)", b, re.S)
    return (m.group(1).strip() if m else "")[:400]

def bootstrap(store, project):
    P = paths(store, project)
    findings = sg.scan(P["base"]) if os.path.isdir(P["base"]) else []
    if findings:                                            # detect helper-bypass / pre-existing secrets
        die(reason="store contains secrets - refusing to resume", findings=findings[:10])
    fm, _ = parse(read(P["state"])); _, lbody = parse(read(P["log"]))
    active = _section(lbody, "Active State"); nxt = _section(lbody, "Next Step")
    return {"version_hash": fm.get("version_hash"), "revision": fm.get("revision"),
        "log_hash": body_hash(lbody) if os.path.exists(P["log"]) else None,
        "active": active, "next": nxt,
        "resume_line": f"resuming: {active or '(none)'} / next: {nxt or '(none)'} / v{fm.get('version_hash','?')}"}

def rollup(store, project):
    P = paths(store, project); n = 0
    if os.path.isdir(P["sessions"]):
        for sid in sorted(os.listdir(P["sessions"])):
            lp = os.path.join(P["sessions"], sid, "log.md")
            if os.path.exists(lp):
                _, b = parse(read(lp)); n += sum(1 for ln in b.splitlines() if ln.startswith("["))
    return {"ok": True, "session_entries": n}                # consolidation writer (future) must use safe_write

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
        "concurrency_blocks": sum(1 for r in block if _has(r, "stale") or _has(r, "lock_timeout")),
        "bootstrap_refusals": sum(1 for r in boot if r.get("decision") == "block"),
        "clean_resumes": sum(1 for r in boot if r.get("decision") == "allow"),
        "errors": len(error),
        "note": "leaks past the gate are NOT measurable here - run the Layer-2 audit scan",
    }
    return sc

def health(store, window_hours=24):
    """One-shot health verdict: scorecard + independent audit + per-project freshness.
    Verdict reflects RECENT activity (default 24h): 'attention' (exit 1) if the gate
    errored in the window or a leak is present now; else 'healthy'. Old, resolved errors
    age out so the watchdog doesn't stay red forever. Missing gitleaks is a note, not a fail."""
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
    try:                                                    # Layer-2 audit, best-effort
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
        import knokeep_audit
        a = knokeep_audit.audit(store)
        leaks = a.get("leaks"); scanner = a.get("scanner")
    except Exception:
        pass
    projects = []
    try:
        root = os.path.realpath(store)
        for name in sorted(os.listdir(root)):
            base = os.path.join(root, name)
            statep = os.path.join(base, "system_state.md")
            if name.startswith(".") or not os.path.exists(statep):
                continue
            fm, _ = parse(read(statep))
            projects.append({"project": name, "revision": fm.get("revision"), "updated": fm.get("updated")})
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
    return read(path)

def _entry(a):
    if a.entry_file == "-":
        return sys.stdin.read().rstrip("\n")
    if a.entry_file:
        return _bodyfile(a.entry_file)
    return a.entry or ""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["init", "flush-state", "flush-log", "session-append", "bootstrap", "rollup", "eval", "health"])
    ap.add_argument("--store", required=True); ap.add_argument("--project")
    ap.add_argument("--session-id"); ap.add_argument("--client", default="cowork")
    ap.add_argument("--body-file"); ap.add_argument("--entry"); ap.add_argument("--entry-file"); ap.add_argument("--expect-hash")
    a = ap.parse_args()
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
