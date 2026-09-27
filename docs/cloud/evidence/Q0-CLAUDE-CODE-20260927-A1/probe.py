#!/usr/bin/env python3
"""Q0 capability probe for job Q0-CLAUDE-CODE-20260927-A1 (Claude Code, node A).

Synthetic inputs only. Stdlib only. Run from the repository root:

    python docs/cloud/evidence/Q0-CLAUDE-CODE-20260927-A1/probe.py run
    python docs/cloud/evidence/Q0-CLAUDE-CODE-20260927-A1/probe.py export
    python docs/cloud/evidence/Q0-CLAUDE-CODE-20260927-A1/probe.py verify [DIR]

run     creates a disposable LocalBackend store under .q0-run/store (inside the
        worktree, never committed), drives the KnoKeep CLI (skill/knokeep_state.py)
        as subprocesses, attempts a secret-gate negative control, drives the MCP
        server over stdio with an explicit --backend local, and reads the saved
        bytes back from separate processes. Logs go to the evidence directory.
export  copies the quiesced store into the fixture export directory and writes
        manifest.json (SHA-256 + byte size of original and copy).
verify  re-hashes an export directory (default: the fixture export directory)
        against manifest.json. Exit 0 only if every member matches.

Absolute paths of the repository root are replaced by "$REPO" in logs.
The environment is never dumped; the child processes inherit it unchanged.
"""
import base64, hashlib, json, os, shutil, subprocess, sys, time

JOB = "Q0-CLAUDE-CODE-20260927-A1"
PROJECT = "q0-claude-code-a1"
MCP_PROJECT = "q0-claude-code-a1-mcp"
SESSION = "q0-cc-a1-s1"
CLIENT = "claude-code"
REPO = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))
EVID = os.path.join(REPO, "docs", "cloud", "evidence", JOB)
EXPORT = os.path.join(REPO, "tests", "fixtures", "q0", JOB, "store")
MANIFEST = os.path.join(REPO, "tests", "fixtures", "q0", JOB, "manifest.json")
RUN = os.path.join(REPO, ".q0-run")
STORE = os.path.join(RUN, "store")
INPUTS = os.path.join(EVID, "inputs")
PY = sys.executable
CLI = [PY, os.path.join("skill", "knokeep_state.py")]

# Transient files deliberately omitted from the export (see report.md).
TRANSIENT_PREFIXES = ("locks/cas.lock", "locks/advisory.lock", "locks/advisory/", "staging/")


def sani(s):
    if s is None:
        return s
    for p in {REPO, REPO.replace("\\", "/"), REPO.replace("\\", "\\\\")}:
        s = s.replace(p, "$REPO")
    return s


def sha(b):
    return hashlib.sha256(b).hexdigest()


LOG = []


def run(label, argv, stdin=None):
    t0 = time.time()
    p = subprocess.run(argv, cwd=REPO, input=stdin, capture_output=True, text=True, timeout=120)
    rec = {"label": label,
           "argv": [sani(a).replace(sani(PY), "python") if a == PY else sani(a) for a in argv],
           "exit": p.returncode, "stdout": sani(p.stdout), "stderr": sani(p.stderr),
           "elapsed_s": round(time.time() - t0, 3)}
    LOG.append(rec)
    print(f"[{label}] exit={p.returncode} {sani(p.stdout).strip()[:300]}")
    if p.stderr.strip():
        print(f"[{label}] stderr: {sani(p.stderr).strip()[:300]}")
    return p


def jl(p):
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        return None


def data_file(key):
    return os.path.join(STORE, "data", *key.split("/"))


READER = r"""
import hashlib, json, sys
sys.path.insert(0, '.')
from store.local import LocalBackend
b = LocalBackend(sys.argv[1])
out = {}
for k in sys.argv[2:]:
    blob = b.read(k)
    out[k] = None if blob is None else {"version_hash": blob.version_hash,
        "sha256_body": hashlib.sha256(blob.body).hexdigest(), "bytes": len(blob.body),
        "head": blob.body.decode('utf-8', 'replace').splitlines()[:8]}
print(json.dumps(out))
"""


def cmd_run():
    if os.path.exists(RUN):
        sys.exit("refusing to run: .q0-run already exists; this probe never overwrites a prior store")
    os.makedirs(STORE)
    receipts = {"job": JOB, "project": PROJECT, "store": "$REPO/.q0-run/store", "backend": "LocalBackend (persistent, explicit --store)"}
    state_in = os.path.join(INPUTS, "system_state.md")
    log_in = os.path.join(INPUTS, "session_log.md")
    entry_in = os.path.join(INPUTS, "journal_entry.txt")

    # 1. synthetic input read (record exact bytes)
    receipts["inputs"] = {os.path.relpath(f, REPO).replace("\\", "/"): {"sha256": sha(open(f, "rb").read()), "bytes": os.path.getsize(f)}
                          for f in (state_in, log_in, entry_in)}

    # 2. CLI path
    run("cli-init", CLI + ["init", "--store", STORE, "--project", PROJECT])
    boot0 = jl(run("cli-bootstrap-pre", CLI + ["bootstrap", "--store", STORE, "--project", PROJECT])) or {}
    vh = boot0.get("version_hash"); lh = boot0.get("log_hash")
    fs = run("cli-flush-state", CLI + ["flush-state", "--store", STORE, "--project", PROJECT, "--body-file", state_in]
             + (["--expect-hash", vh] if vh else []))
    fl = run("cli-flush-log", CLI + ["flush-log", "--store", STORE, "--project", PROJECT, "--body-file", log_in]
             + (["--expect-hash", lh] if lh else []))
    sa = run("cli-session-append", CLI + ["session-append", "--store", STORE, "--project", PROJECT,
                                          "--session-id", SESSION, "--client", CLIENT, "--entry-file", entry_in])
    receipts["cli"] = {"flush_state": jl(fs), "flush_log": jl(fl), "session_append": jl(sa),
                       "exits": {"flush_state": fs.returncode, "flush_log": fl.returncode, "session_append": sa.returncode}}

    # 3. secret-gate negative control: synthetic fake AWS-style key id, built at runtime,
    #    written only to the scratch dir (never to evidence). Rejection is the expected outcome.
    fake = "AK" + "IA" + "Q0SYNTH" + "ETICFAKE1"
    neg = os.path.join(RUN, "negative_control_body.md")
    with open(neg, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("## Architecture\nsynthetic negative control\n\n## Path & Variable Directory\naws_access_key_id = " + fake + "\n\n## Hard Constraints\nnone\n")
    boot1 = jl(run("cli-bootstrap-mid", CLI + ["bootstrap", "--store", STORE, "--project", PROJECT])) or {}
    ng = run("cli-flush-state-secret-negative-control", CLI + ["flush-state", "--store", STORE, "--project", PROJECT,
             "--body-file", neg] + (["--expect-hash", boot1["version_hash"]] if boot1.get("version_hash") else []))
    receipts["secret_gate_negative_control"] = {"exit": ng.returncode, "result": jl(ng),
                                                "value_logged": False, "expected": "rejected"}

    # 4. MCP protocol harness (shell-created; NOT native MCP enrollment), explicit persistent backend
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "q0-shell-harness", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "knokeep_write", "arguments": {
            "key": MCP_PROJECT + "/system_state", "doc_type": "system_state",
            "body": open(state_in, encoding="utf-8").read()}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "knokeep_read", "arguments": {"key": MCP_PROJECT + "/system_state"}}},
    ]
    mp = run("mcp-stdio-harness-local-backend", [PY, "-m", "mcp.server", "--backend", "local", "--root", STORE],
             stdin="".join(json.dumps(r) + "\n" for r in reqs))
    mcp_out = []
    for ln in mp.stdout.splitlines():
        try:
            mcp_out.append(json.loads(ln))
        except Exception:
            mcp_out.append({"unparsed": sani(ln)})
    for r in mcp_out:  # keep tools/list short
        if r.get("id") == 2 and "result" in r:
            r["result"] = {"tool_names": [t.get("name") for t in r["result"].get("tools", [])]}
        if r.get("id") == 4 and "result" in r:
            r["result"] = {"truncated_for_log": True, "text_sha256": sha(json.dumps(r["result"], sort_keys=True).encode())}
    receipts["mcp_harness"] = {"exit": mp.returncode, "responses": mcp_out}

    # 5. independent verification: separate reader process + raw file bytes
    keys = [f"{PROJECT}/system_state", f"{PROJECT}/session_log", f"{PROJECT}/sessions/{SESSION}", f"{MCP_PROJECT}/system_state"]
    rp = run("independent-reader-process", [PY, "-c", READER, STORE] + keys)
    reader = jl(rp) or {}
    raw = {}
    for k in keys:
        f = data_file(k)
        if os.path.exists(f):
            b = open(f, "rb").read()
            raw[k] = {"sha256_file": sha(b), "bytes": len(b)}
        else:
            raw[k] = None
    cmp_ = {}
    for k in keys:
        r, f = reader.get(k), raw.get(k)
        cmp_[k] = None if not (r and f) else {
            "reader_sha256_body == raw_file_sha256": r["sha256_body"] == f["sha256_file"],
            "version_hash == raw_file_sha256": r["version_hash"] == f["sha256_file"]}
    receipts["independent_read"] = {"reader": reader, "raw_files": raw, "comparison": cmp_}

    # 6. fresh-process bootstrap (same session: this is NOT fresh-session resume)
    bt = run("cli-bootstrap-post-separate-process", CLI + ["bootstrap", "--store", STORE, "--project", PROJECT])
    receipts["bootstrap_post"] = jl(bt)

    # 7. persisted client identity vs actual runtime
    ident = {}
    for k in keys:
        r = reader.get(k)
        if r:
            ident[k] = [ln for ln in r["head"] if ln.startswith("client:")] or ["(no client field)"]
    receipts["client_identity"] = {"actual_runtime": "claude-code (Claude Code print runtime)", "persisted": ident}

    with open(os.path.join(EVID, "commands.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
        for rec in LOG:
            fh.write(json.dumps(rec) + "\n")
    with open(os.path.join(EVID, "receipts.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(receipts, fh, indent=1, sort_keys=True); fh.write("\n")
    print(json.dumps(receipts["independent_read"]["comparison"], indent=1))


def members(root):
    out = []
    for d, _, files in os.walk(root):
        for n in files:
            rel = os.path.relpath(os.path.join(d, n), root).replace("\\", "/")
            out.append(rel)
    return sorted(out)


def cmd_export():
    if os.path.exists(EXPORT):
        sys.exit("refusing to export: export directory already exists")
    kept, omitted = [], []
    for rel in members(STORE):
        (omitted if rel.startswith(TRANSIENT_PREFIXES) else kept).append(rel)
    man = {"job": JOB, "project": PROJECT, "source": "$REPO/.q0-run/store", "export": "tests/fixtures/q0/%s/store" % JOB,
           "members": [], "omitted_transient": []}
    for rel in omitted:
        p = os.path.join(STORE, *rel.split("/"))
        man["omitted_transient"].append({"path": rel, "bytes": os.path.getsize(p)})
    for rel in kept:
        src = os.path.join(STORE, *rel.split("/"))
        dst = os.path.join(EXPORT, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        a, b = open(src, "rb").read(), open(dst, "rb").read()
        man["members"].append({"path": rel, "bytes": len(a), "sha256": sha(a), "copy_matches": a == b})
    with open(MANIFEST, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(man, fh, indent=1); fh.write("\n")
    bad = [m for m in man["members"] if not m["copy_matches"]]
    print(json.dumps({"members": len(kept), "omitted_transient": len(omitted), "copy_mismatches": len(bad)}))
    sys.exit(1 if bad else 0)


def cmd_verify(root=None):
    root = root or EXPORT
    man = json.load(open(os.path.join(os.path.dirname(root.rstrip("/\\")), "manifest.json") if root != EXPORT else MANIFEST, encoding="utf-8"))
    fails = []
    for m in man["members"]:
        p = os.path.join(root, *m["path"].split("/"))
        if not os.path.exists(p):
            fails.append({"path": m["path"], "problem": "missing"}); continue
        b = open(p, "rb").read()
        if len(b) != m["bytes"] or sha(b) != m["sha256"]:
            fails.append({"path": m["path"], "problem": "mismatch", "bytes": len(b), "sha256": sha(b)})
    extra = sorted(set(members(root)) - {m["path"] for m in man["members"]})
    print(json.dumps({"checked": len(man["members"]), "failures": fails, "unexpected_extra": extra}))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    c = sys.argv[1] if len(sys.argv) > 1 else ""
    if c == "run": cmd_run()
    elif c == "export": cmd_export()
    elif c == "verify": cmd_verify(sys.argv[2] if len(sys.argv) > 2 else None)
    else: sys.exit(__doc__)
