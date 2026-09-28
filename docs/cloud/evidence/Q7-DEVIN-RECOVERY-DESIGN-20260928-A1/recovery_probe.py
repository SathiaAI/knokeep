"""Synthetic, offline, read-only crash-tail recovery probe (standard library only).

Usage (from repo root):  python3 docs/cloud/evidence/Q7-DEVIN-RECOVERY-DESIGN-20260928-A1/recovery_probe.py
Builds synthetic LocalBackend-shaped stores in a temp dir, runs the proposed
`analyze_and_export` workflow on each, and asserts that it (a) never modifies
input bytes, (b) never labels an unprovable store as fully recovered, and
(c) writes only inside a separately named output directory.
This is a design probe, NOT a product repair tool and NOT a security sandbox.
Exit code 0 iff every case matches expectations.
"""
import hashlib, json, os, re, shutil, struct, sys, tempfile, types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
MAGIC, MAX_KEY = b"KKJ2", 1024
KEY_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
if os.name == "nt":
    import msvcrt
else:
    import fcntl


def sha(b): return hashlib.sha256(b).hexdigest()


def frame(key, body, v2=True, fence=0, body_len=None):
    kb = key.encode()
    out = (MAGIC if v2 else b"") + struct.pack(">I", len(kb)) + kb
    out += struct.pack(">Q", len(body) if body_len is None else body_len) + body + hashlib.sha256(body).digest()
    return out + (struct.pack(">Q", fence) if v2 else b"")


def parse_at(buf, off):
    """Parse one frame at `off` with the same bounds as store/local.py; None if invalid."""
    n = len(buf); p = off
    v2 = buf[p:p + 4] == MAGIC
    if v2: p += 4
    if p + 4 > n: return None
    (kl,) = struct.unpack(">I", buf[p:p + 4]); p += 4
    tr = 40 if v2 else 32
    if not 1 <= kl <= MAX_KEY or kl + 8 + tr > n - p: return None
    kb = buf[p:p + kl]; p += kl
    (bl,) = struct.unpack(">Q", buf[p:p + 8]); p += 8
    if bl + tr > n - p: return None
    body = buf[p:p + bl]; p += bl
    if hashlib.sha256(body).digest() != buf[p:p + 32]: return None
    p += 32; fence = 0
    if v2: (fence,) = struct.unpack(">Q", buf[p:p + 8]); p += 8
    try: key = kb.decode("utf-8")
    except UnicodeDecodeError: return None
    return {"offset": off, "end": p, "key": key, "sha256": sha(body), "fence": fence,
            "format": "v2" if v2 else "legacy", "body": body}


def strict_prefix(buf):
    recs, off = [], 0
    while True:
        r = parse_at(buf, off)
        if r is None: return recs, off
        recs.append(r); off = r["end"]


def resync(buf, start):
    """Every offset after the stop point that parses as a frame. AMBIGUOUS by
    construction: a hit may be a real later record OR bytes inside a body."""
    hits = []
    for o in range(start + 1, len(buf)):
        r = parse_at(buf, o)
        if r: hits.append(r)
    for h in hits:
        h["overlaps"] = [x["offset"] for x in hits if x is not h and x["offset"] < h["end"] and h["offset"] < x["end"]]
    return hits


def try_lock(path):
    """Non-blocking exclusive lock on EXISTING cas.lock, same primitive as _FileLock. Never creates/writes."""
    fh = open(path, "rb")
    try:
        if os.name == "nt": msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else: fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close(); return None


def unlock(fh):
    try:
        if os.name == "nt": fh.seek(0); msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else: fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally: fh.close()


def inventory(store):
    """lstat-walk; symlinks/specials are reported, never followed."""
    files, odd = {}, []
    for dp, dns, fns in os.walk(store, followlinks=False):
        for n in dns + fns:
            p = Path(dp) / n
            st = os.lstat(p)
            if os.path.islink(p) or not (p.is_dir() or p.is_file()):
                odd.append(str(p.relative_to(store)))
            elif p.is_file():
                files[str(p.relative_to(store)).replace(os.sep, "/")] = p.read_bytes()
    return files, odd


def analyze_and_export(store, out_parent, _hook=None):
    store, out_parent = Path(store), Path(out_parent)
    lockp, jp = store / "locks" / "cas.lock", store / "journal" / "journal.log"
    if not lockp.is_file() or os.path.islink(lockp): return {"verdict": "REFUSE_NOT_A_STORE"}
    lk = try_lock(lockp)
    if lk is None: return {"verdict": "REFUSE_BUSY_WRITER_ACTIVE"}
    try:
        if not jp.exists() and not os.path.islink(jp): return {"verdict": "REFUSE_JOURNAL_MISSING"}
        files1, odd = inventory(store)
        if odd: return {"verdict": "REFUSE_SYMLINK_OR_SPECIAL", "paths": sorted(odd)}
        if _hook: _hook("after_inventory")
        files2, _ = inventory(store)
        if {k: sha(v) for k, v in files1.items()} != {k: sha(v) for k, v in files2.items()}:
            return {"verdict": "REFUSE_STORE_CHANGED_DURING_EXPORT"}
        buf = files1["journal/journal.log"]
        recs, stop = strict_prefix(buf)
        hits = resync(buf, stop) if stop < len(buf) else []
        latest = {}
        for r in recs: latest[r["key"]] = r
        findings = []
        if stop < len(buf):
            findings.append("TAIL_DAMAGED_AMBIGUOUS" if hits else "TAIL_UNPARSABLE_NO_LATER_FRAME_FOUND")
        bad_keys = sorted({r["key"] for r in recs + hits if not KEY_RE.match(r["key"]) or ".." in r["key"].split("/")})
        if bad_keys: findings.append("INVALID_KEYS_NOT_MATERIALIZED")
        published = {k[5:]: v for k, v in files1.items() if k.startswith("data/")}
        diffs = sorted(k for k, v in published.items() if k not in latest or latest[k]["sha256"] != sha(v))
        diffs += sorted(k for k in latest if k not in published)
        if diffs: findings.append("PUBLISHED_DATA_DIFFERS_FROM_PREFIX")
        name = "recovery-candidate-" + sha(buf)[:12]
        final, tmp = out_parent / name, out_parent / (".incomplete-" + name)
        if final.exists(): return {"verdict": "REFUSE_OUTPUT_EXISTS", "path": str(final)}
        if tmp.exists(): shutil.rmtree(tmp)
        (tmp / "archive").mkdir(parents=True)
        manifest = {}
        for rel, b in sorted(files1.items()):
            dst = tmp / "archive" / rel; dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(b); manifest[rel] = sha(b)
            if _hook: _hook("copied:" + rel)
        for rel, h in manifest.items():
            if sha((tmp / "archive" / rel).read_bytes()) != h: return {"verdict": "REFUSE_ARCHIVE_VERIFY_FAILED"}
        for k, r in latest.items():
            if k in bad_keys: continue
            dst = (tmp / "candidate-data").joinpath(*k.split("/")); dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(r["body"])
        verdict = "CLEAN_NO_RECOVERY_NEEDED" if not findings else "INCOMPLETE_HUMAN_DECISION_REQUIRED"
        report = {"verdict": verdict, "findings": findings, "journal_size": len(buf), "prefix_end": stop,
                  "prefix_records": [{k: r[k] for k in ("offset", "end", "key", "sha256", "fence", "format")} for r in recs],
                  "ambiguous_later_frames": [{k: h[k] for k in ("offset", "end", "key", "sha256", "fence", "format", "overlaps")} for h in hits],
                  "unparsed_bytes": [stop, len(buf)] if stop < len(buf) else None,
                  "invalid_keys": bad_keys, "published_mismatch": diffs,
                  "candidate_is_authoritative": False, "archive_manifest_sha256": manifest}
        (tmp / "MANIFEST.json").write_text(json.dumps(report, indent=1, sort_keys=True))
        os.replace(tmp, final)
        report["output"] = str(final)
        return report
    finally:
        unlock(lk)


# ------------------------------- fixtures ---------------------------------

def make_store(base, journal, data=None, lock=True):
    s = Path(tempfile.mkdtemp(dir=base))
    for d in ("journal", "locks", "data", "staging"): (s / d).mkdir()
    if lock: (s / "locks" / "cas.lock").write_bytes(b"\0")
    if journal is not None: (s / "journal" / "journal.log").write_bytes(journal)
    for k, v in (data or {}).items():
        p = (s / "data").joinpath(*k.split("/")); p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(v)
    return s


def tree_hash(p):
    out = {}
    for dp, dns, fns in os.walk(p, followlinks=False):
        for n in dns + fns:
            q = Path(dp) / n
            out[str(q.relative_to(p))] = ("L:" + os.readlink(q)) if os.path.islink(q) else (sha(q.read_bytes()) if q.is_file() else "D")
    return out


def product_prefix_end(journal_path):
    """Cross-check against the real parser without constructing (mutating) a LocalBackend."""
    sys.path.insert(0, str(ROOT))
    from store.local import LocalBackend
    st = {}
    list(LocalBackend._iter_journal_records(types.SimpleNamespace(_journal_path=journal_path), st))
    return st["end"]


def main():
    base = Path(tempfile.mkdtemp(prefix="kk-q7-"))
    A, B = frame("a/one", b"#knokeep-gen:1\nA", v2=False), frame("a/two", b"#knokeep-gen:1\nB", fence=3)
    C = frame("a/one", b"#knokeep-gen:2\nA2", fence=4)
    pub = {"a/one": b"#knokeep-gen:1\nA", "a/two": b"#knokeep-gen:1\nB"}
    inner = frame("a/two", b"#knokeep-gen:9\nFORGED", fence=9)
    cases = {
        "clean": (A + B, pub, "CLEAN_NO_RECOVERY_NEEDED", []),
        "partial-final-legacy": (A + B + frame("a/x", b"xyz", v2=False)[:-5], pub, "INCOMPLETE_HUMAN_DECISION_REQUIRED", ["TAIL_UNPARSABLE_NO_LATER_FRAME_FOUND"]),
        # A v2 frame torn inside its fence trailer still contains a digest-valid LEGACY
        # frame at offset+4: resync must report it as ambiguous, not as recovered.
        "partial-final-v2-torn-fence": (A + B + frame("a/x", b"xyz")[:-3], pub, "INCOMPLETE_HUMAN_DECISION_REQUIRED", ["TAIL_DAMAGED_AMBIGUOUS"]),
        "partial-final-v2-torn-body": (A + B + frame("a/x", b"xyz")[:-45], pub, "INCOMPLETE_HUMAN_DECISION_REQUIRED", ["TAIL_UNPARSABLE_NO_LATER_FRAME_FOUND"]),
        "damaged-length-covers-later-valid": (A + frame("a/two", b"B" * 8, body_len=8 + len(C))[:-40] + C + b"\0" * 40, pub, "INCOMPLETE_HUMAN_DECISION_REQUIRED", ["TAIL_DAMAGED_AMBIGUOUS", "PUBLISHED_DATA_DIFFERS_FROM_PREFIX"]),
        "forged-frame-inside-body-after-damage": (A + b"\xff\xff" + frame("a/blob", b"xx" + inner + b"yy"), pub, "INCOMPLETE_HUMAN_DECISION_REQUIRED", ["TAIL_DAMAGED_AMBIGUOUS", "PUBLISHED_DATA_DIFFERS_FROM_PREFIX"]),
        "published-newer-than-prefix": (A + B + b"\x00\x01garbage", {"a/one": b"#knokeep-gen:2\nA2", "a/two": pub["a/two"]}, "INCOMPLETE_HUMAN_DECISION_REQUIRED", ["TAIL_UNPARSABLE_NO_LATER_FRAME_FOUND", "PUBLISHED_DATA_DIFFERS_FROM_PREFIX"]),
        "traversal-key-in-journal": (A + frame("../escape", b"E"), pub, "INCOMPLETE_HUMAN_DECISION_REQUIRED", ["INVALID_KEYS_NOT_MATERIALIZED", "PUBLISHED_DATA_DIFFERS_FROM_PREFIX"]),
        "missing-journal": (None, pub, "REFUSE_JOURNAL_MISSING", None),
    }
    results, fails = {}, 0

    def check(name, got, want_v, want_f, store, out, expect_unchanged=True, before=None):
        nonlocal fails
        ok = got["verdict"] == want_v and (want_f is None or got.get("findings") == want_f)
        ok = ok and got.get("candidate_is_authoritative", False) is False
        if expect_unchanged: ok = ok and tree_hash(store) == before
        outside = [p for p in tree_hash(base) if not (p.startswith(store.name) or p.startswith(out.name))]
        ok = ok and not [p for p in outside if "escape" in p]
        slim = {k: got[k] for k in ("verdict", "findings", "prefix_end", "journal_size", "unparsed_bytes", "published_mismatch", "invalid_keys") if k in got}
        slim["ambiguous_frames"] = [(h["offset"], h["key"], h["overlaps"]) for h in got.get("ambiguous_later_frames", [])]
        results[name] = slim
        print(("PASS " if ok else "FAIL ") + name + " -> " + json.dumps(slim))
        fails += not ok

    for name, (j, data, v, f) in cases.items():
        s = make_store(base, j, data); out = Path(tempfile.mkdtemp(dir=base)); before = tree_hash(s)
        if j is not None:
            pe = product_prefix_end(s / "journal" / "journal.log")
            if pe != strict_prefix(j)[1]:
                print("FAIL parser-mismatch", name, pe); fails += 1
        check(name, analyze_and_export(s, out), v, f, s, out, before=before)

    # concurrent writer holds cas.lock
    s = make_store(base, A + B, pub); out = Path(tempfile.mkdtemp(dir=base)); before = tree_hash(s)
    held = try_lock(s / "locks" / "cas.lock")
    try: check("concurrent-writer-holds-lock", analyze_and_export(s, out), "REFUSE_BUSY_WRITER_ACTIVE", None, s, out, before=before)
    finally: unlock(held)

    # journal growth between inventory passes (simulates a writer that bypasses cas.lock)
    s = make_store(base, A + B, pub); out = Path(tempfile.mkdtemp(dir=base))
    def grow(ev):
        if ev == "after_inventory":
            with open(s / "journal" / "journal.log", "ab") as fh: fh.write(C)
    got = analyze_and_export(s, out, _hook=grow)
    grown_ok = (s / "journal" / "journal.log").read_bytes() == A + B + C and not any(out.iterdir())
    check("journal-growth-during-export", got, "REFUSE_STORE_CHANGED_DURING_EXPORT", None, s, out, expect_unchanged=False)
    if not grown_ok: print("FAIL growth: probe wrote output or altered bytes"); fails += 1

    # interrupted export: crash while copying, then rerun
    s = make_store(base, A + B + b"\x01", pub); out = Path(tempfile.mkdtemp(dir=base)); before = tree_hash(s)
    def boom(ev):
        if ev.startswith("copied:data/"): raise KeyboardInterrupt("simulated crash")
    try: analyze_and_export(s, out, _hook=boom); print("FAIL interrupted: no crash"); fails += 1
    except KeyboardInterrupt: pass
    only_incomplete = [p.name for p in out.iterdir()] and all(p.name.startswith(".incomplete-") for p in out.iterdir())
    if not only_incomplete or tree_hash(s) != before: print("FAIL interrupted-export leftovers"); fails += 1
    check("interrupted-export-rerun", analyze_and_export(s, out), "INCOMPLETE_HUMAN_DECISION_REQUIRED", ["TAIL_UNPARSABLE_NO_LATER_FRAME_FOUND"], s, out, before=before)
    check("rerun-refuses-overwrite", analyze_and_export(s, out), "REFUSE_OUTPUT_EXISTS", None, s, out, before=before)

    # symlink fixture
    s = make_store(base, A + B, pub); out = Path(tempfile.mkdtemp(dir=base))
    target = base / "outside-secret"; target.write_bytes(b"outside")
    try:
        os.symlink(target, s / "data" / "a" / "link")
        before = tree_hash(s)
        check("symlink-in-store", analyze_and_export(s, out), "REFUSE_SYMLINK_OR_SPECIAL", None, s, out, before=before)
        if target.read_bytes() != b"outside" or any(out.iterdir()): print("FAIL symlink side effect"); fails += 1
    except (OSError, NotImplementedError) as e:
        print("SKIP symlink-in-store: symlinks unavailable:", type(e).__name__)

    shutil.rmtree(base, ignore_errors=True)
    print("RESULT", "OK" if not fails else f"{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
