"""A2 synthetic, offline, read-only crash-tail recovery DESIGN probe (stdlib + repo parser/gate only).

NON-SHIPPING. Design evidence only; product has no recovery/service-restoration command.
Usage (repo root): python3 docs/cloud/evidence/Q7-DEVIN-RECOVERY-DESIGN-20260928-A1/recovery_probe_a2.py
A2 changes vs recovery_probe.py (A1, kept unchanged):
  * cas.lock bytes are read through the HELD lock handle (Windows msvcrt byte lock
    blocks a second handle; A1 raised PermissionError there).
  * out_parent/store overlap (incl. resolved symlink aliases) rejected before any output.
  * no automatic deletion: every attempt uses a unique exclusively-created directory
    with an attempt manifest; prior incomplete attempts are preserved.
  * best verdict is SYNTACTICALLY_CONSISTENT_COMPLETENESS_UNPROVEN (a boundary truncation
    or rollback to an older coherent state is undetectable without an external anchor).
  * explicit byte/work budgets; keys validated with store.gate._valid_key_shape; case-fold
    collisions are not materialized.
Not a security sandbox; no fsync durability of outputs; TOCTOU/hard-link/reparse limits remain.
"""
import hashlib, json, os, secrets, struct, sys, tempfile, types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from store import gate  # noqa: E402
from store.local import LocalBackend  # noqa: E402

MAGIC, MAX_KEY = b"KKJ2", 1024
MAX_TOTAL_BYTES = 64 << 20
MAX_RESYNC_BYTES = 1 << 20
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
    hits = []
    for o in range(start + 1, len(buf)):
        r = parse_at(buf, o)
        if r: hits.append(r)
    for h in hits:
        h["overlaps"] = [x["offset"] for x in hits if x is not h and x["offset"] < h["end"] and h["offset"] < x["end"]]
    return hits


def try_lock(path):
    fh = open(path, "rb")
    try:
        if os.name == "nt": fh.seek(0); msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else: fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close(); return None


def unlock(fh):
    try:
        if os.name == "nt": fh.seek(0); msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else: fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally: fh.close()


def read_via_held(fh):
    fh.seek(0); b = fh.read(); fh.seek(0); return b


def inventory(store, lock_rel, lock_fh):
    files, odd, total = {}, [], 0
    for dp, dns, fns in os.walk(store, followlinks=False):
        for n in dns + fns:
            p = Path(dp) / n
            st = os.lstat(p)
            if os.path.islink(p) or not (p.is_dir() or p.is_file()) or (p.is_file() and st.st_nlink > 1):
                odd.append(str(p.relative_to(store)))
            elif p.is_file():
                total += st.st_size
                if total > MAX_TOTAL_BYTES: raise MemoryError("budget")
                rel = str(p.relative_to(store)).replace(os.sep, "/")
                files[rel] = read_via_held(lock_fh) if rel == lock_rel else p.read_bytes()
    return files, odd


def _inside(a, b):
    try: a.relative_to(b); return True
    except ValueError: return False


def analyze_and_export(store, out_parent, _hook=None):
    store, out_parent = Path(store), Path(out_parent)
    if not out_parent.is_dir(): return {"verdict": "REFUSE_OUTPUT_PARENT_MISSING"}
    rs, ro = Path(os.path.realpath(store)), Path(os.path.realpath(out_parent))
    if os.path.normcase(str(rs)) == os.path.normcase(str(ro)) or _inside(ro, rs) or _inside(rs, ro):
        return {"verdict": "REFUSE_OUTPUT_OVERLAPS_STORE"}
    lock_rel = "locks/cas.lock"
    lockp, jp = store / "locks" / "cas.lock", store / "journal" / "journal.log"
    if not lockp.is_file() or os.path.islink(lockp): return {"verdict": "REFUSE_NOT_A_STORE"}
    lk = try_lock(lockp)
    if lk is None: return {"verdict": "REFUSE_BUSY_WRITER_ACTIVE"}
    try:
        if not jp.exists() and not os.path.islink(jp): return {"verdict": "REFUSE_JOURNAL_MISSING"}
        try:
            files1, odd = inventory(store, lock_rel, lk)
        except MemoryError:
            return {"verdict": "REFUSE_BUDGET_EXCEEDED"}
        if odd: return {"verdict": "REFUSE_SYMLINK_HARDLINK_OR_SPECIAL", "paths": sorted(odd)}
        if _hook: _hook("after_inventory")
        files2, _ = inventory(store, lock_rel, lk)
        if {k: sha(v) for k, v in files1.items()} != {k: sha(v) for k, v in files2.items()}:
            return {"verdict": "REFUSE_STORE_CHANGED_DURING_EXPORT"}
        buf = files1["journal/journal.log"]
        recs, stop = strict_prefix(buf)
        if len(buf) - stop > MAX_RESYNC_BYTES: return {"verdict": "REFUSE_BUDGET_EXCEEDED"}
        hits = resync(buf, stop) if stop < len(buf) else []
        latest = {}
        for r in recs: latest[r["key"]] = r
        findings = []
        if stop < len(buf):
            findings.append("TAIL_DAMAGED_AMBIGUOUS" if hits else "TAIL_UNPARSABLE_NO_LATER_FRAME_FOUND")
        bad_keys = sorted({r["key"] for r in recs + hits if not gate._valid_key_shape(r["key"])})
        folded = {}
        for k in latest: folded.setdefault(k.casefold(), []).append(k)
        case_coll = sorted(k for ks in folded.values() if len(ks) > 1 for k in ks)
        if bad_keys: findings.append("INVALID_KEYS_NOT_MATERIALIZED")
        if case_coll: findings.append("CASE_FOLD_COLLISION_NOT_MATERIALIZED")
        published = {k[5:]: v for k, v in files1.items() if k.startswith("data/")}
        diffs = sorted(k for k, v in published.items() if k not in latest or latest[k]["sha256"] != sha(v))
        diffs += sorted(k for k in latest if k not in published)
        if diffs: findings.append("PUBLISHED_DATA_DIFFERS_FROM_PREFIX")
        name = "recovery-candidate-" + sha(buf)[:12]
        final = out_parent / name
        if final.exists(): return {"verdict": "REFUSE_OUTPUT_EXISTS", "path": final.name}
        attempt = out_parent / (".incomplete-" + name + "-" + secrets.token_hex(8))
        os.mkdir(attempt)  # exclusive: FileExistsError if it exists; never reused, never deleted
        (attempt / "ATTEMPT.json").write_text(json.dumps({"status": "incomplete", "journal_sha256": sha(buf)}))
        manifest = {}
        for rel, b in sorted(files1.items()):
            dst = attempt / "archive" / rel; dst.parent.mkdir(parents=True, exist_ok=True)
            with open(dst, "xb") as fh: fh.write(b)
            manifest[rel] = sha(b)
            if _hook: _hook("copied:" + rel)
        for rel, h in manifest.items():
            if sha((attempt / "archive" / rel).read_bytes()) != h: return {"verdict": "REFUSE_ARCHIVE_VERIFY_FAILED"}
        for k, r in latest.items():
            if k in bad_keys or k in case_coll: continue
            dst = (attempt / "candidate-data").joinpath(*k.split("/")); dst.parent.mkdir(parents=True, exist_ok=True)
            with open(dst, "xb") as fh: fh.write(r["body"])
        verdict = "SYNTACTICALLY_CONSISTENT_COMPLETENESS_UNPROVEN" if not findings else "INCOMPLETE_HUMAN_DECISION_REQUIRED"
        report = {"verdict": verdict, "findings": findings, "journal_size": len(buf), "prefix_end": stop,
                  "prefix_records": [{k: r[k] for k in ("offset", "end", "key", "sha256", "fence", "format")} for r in recs],
                  "ambiguous_later_frames": [{k: h[k] for k in ("offset", "end", "key", "sha256", "fence", "format", "overlaps")} for h in hits],
                  "unparsed_bytes": [stop, len(buf)] if stop < len(buf) else None,
                  "invalid_keys": bad_keys, "case_fold_collisions": case_coll, "published_mismatch": diffs,
                  "candidate_is_authoritative": False, "completeness_proven": False,
                  "archive_manifest_sha256": manifest}
        with open(attempt / "MANIFEST.json", "x") as fh: fh.write(json.dumps(report, indent=1, sort_keys=True))
        os.rename(attempt, final)  # fails if final appeared meanwhile on Windows; POSIX: final must not exist (checked)
        report["output"] = final.name
        return report
    finally:
        unlock(lk)


def make_store(base, journal, data=None):
    s = Path(tempfile.mkdtemp(dir=base))
    for d in ("journal", "locks", "data", "staging"): (s / d).mkdir()
    (s / "locks" / "cas.lock").write_bytes(b"\0")
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
    st = {}
    list(LocalBackend._iter_journal_records(types.SimpleNamespace(_journal_path=journal_path), st))
    return st["end"]


def main():
    base = Path(tempfile.mkdtemp(prefix="kk-q7a2-"))
    A, B = frame("a/one", b"#knokeep-gen:1\nA", v2=False), frame("a/two", b"#knokeep-gen:1\nB", fence=3)
    C = frame("a/one", b"#knokeep-gen:2\nA2", fence=4)
    pub = {"a/one": b"#knokeep-gen:1\nA", "a/two": b"#knokeep-gen:1\nB"}
    inner = frame("a/two", b"#knokeep-gen:9\nFORGED", fence=9)
    INC, SC = "INCOMPLETE_HUMAN_DECISION_REQUIRED", "SYNTACTICALLY_CONSISTENT_COMPLETENESS_UNPROVEN"
    cases = {
        "clean": (A + B, pub, SC, []),
        "partial-final-legacy": (A + B + frame("a/x", b"xyz", v2=False)[:-5], pub, INC, ["TAIL_UNPARSABLE_NO_LATER_FRAME_FOUND"]),
        "partial-final-v2-torn-fence": (A + B + frame("a/x", b"xyz")[:-3], pub, INC, ["TAIL_DAMAGED_AMBIGUOUS"]),
        "partial-final-v2-torn-body": (A + B + frame("a/x", b"xyz")[:-45], pub, INC, ["TAIL_UNPARSABLE_NO_LATER_FRAME_FOUND"]),
        "damaged-length-covers-later-valid": (A + frame("a/two", b"B" * 8, body_len=8 + len(C))[:-40] + C + b"\0" * 40, pub, INC, ["TAIL_DAMAGED_AMBIGUOUS", "PUBLISHED_DATA_DIFFERS_FROM_PREFIX"]),
        "forged-frame-inside-body-after-damage": (A + b"\xff\xff" + frame("a/blob", b"xx" + inner + b"yy"), pub, INC, ["TAIL_DAMAGED_AMBIGUOUS", "PUBLISHED_DATA_DIFFERS_FROM_PREFIX"]),
        "published-newer-than-prefix": (A + B + b"\x00\x01garbage", {"a/one": b"#knokeep-gen:2\nA2", "a/two": pub["a/two"]}, INC, ["TAIL_UNPARSABLE_NO_LATER_FRAME_FOUND", "PUBLISHED_DATA_DIFFERS_FROM_PREFIX"]),
        # record-boundary truncation of the LAST record: journal parses fully; only published data hints at loss
        "boundary-truncation-published-hint": (A + B, {"a/one": b"#knokeep-gen:2\nA2", "a/two": pub["a/two"]}, INC, ["PUBLISHED_DATA_DIFFERS_FROM_PREFIX"]),
        # boundary truncation + publish rollback (coherent older state): UNDETECTABLE, verdict never claims completeness
        "boundary-truncation-coherent-rollback": (A + B, pub, SC, []),
        "traversal-key": (A + B + frame("../escape", b"E"), pub, INC, ["INVALID_KEYS_NOT_MATERIALIZED", "PUBLISHED_DATA_DIFFERS_FROM_PREFIX"]),
        "dot-keys": (A + B + frame("a/./b", b"D") + frame("a/b.", b"D") + frame(".", b"D"), pub, INC, ["INVALID_KEYS_NOT_MATERIALIZED", "PUBLISHED_DATA_DIFFERS_FROM_PREFIX"]),
        "case-fold-collision": (A + B + frame("a/Readme", b"x") + frame("a/README", b"y"), {**pub, "a/Readme": b"x"} if os.name != "nt" else pub, INC, ["CASE_FOLD_COLLISION_NOT_MATERIALIZED", "PUBLISHED_DATA_DIFFERS_FROM_PREFIX"]),
        "missing-journal": (None, pub, "REFUSE_JOURNAL_MISSING", None),
    }
    fails = 0

    def check(name, got, want_v, want_f, store, out, before=None):
        nonlocal fails
        ok = got["verdict"] == want_v and (want_f is None or got.get("findings") == want_f)
        ok = ok and got.get("candidate_is_authoritative", False) is False and got.get("completeness_proven", False) is False
        if before is not None: ok = ok and tree_hash(store) == before
        ok = ok and not any("escape" in p for p in tree_hash(base))
        slim = {k: got[k] for k in ("verdict", "findings", "prefix_end", "journal_size", "published_mismatch", "invalid_keys", "case_fold_collisions") if k in got}
        slim["ambiguous_frames"] = [(h["offset"], h["key"], h["overlaps"]) for h in got.get("ambiguous_later_frames", [])]
        print(("PASS " if ok else "FAIL ") + name + " -> " + json.dumps(slim))
        fails += not ok

    for name, (j, data, v, f) in cases.items():
        s = make_store(base, j, data); out = Path(tempfile.mkdtemp(dir=base)); before = tree_hash(s)
        if j is not None and product_prefix_end(s / "journal" / "journal.log") != strict_prefix(j)[1]:
            print("FAIL parser-mismatch", name); fails += 1
        check(name, analyze_and_export(s, out), v, f, s, out, before=before)

    s = make_store(base, A + B, pub); before = tree_hash(s)
    check("output-inside-store", analyze_and_export(s, s / "staging"), "REFUSE_OUTPUT_OVERLAPS_STORE", None, s, None, before=before)
    check("output-equals-store", analyze_and_export(s, s), "REFUSE_OUTPUT_OVERLAPS_STORE", None, s, None, before=before)
    outer = Path(tempfile.mkdtemp(dir=base)); inner_store = make_store(outer, A + B, pub); before_o = tree_hash(outer)
    check("store-inside-output", analyze_and_export(inner_store, outer), "REFUSE_OUTPUT_OVERLAPS_STORE", None, outer, None, before=before_o)
    try:
        alias = base / "alias-to-store-data"; os.symlink(s / "data", alias, target_is_directory=True)
        check("output-alias-resolves-into-store", analyze_and_export(s, alias), "REFUSE_OUTPUT_OVERLAPS_STORE", None, s, None, before=before)
        alias.unlink()
    except (OSError, NotImplementedError) as e:
        print("SKIP output-alias-resolves-into-store:", type(e).__name__)

    s = make_store(base, A + B, pub); out = Path(tempfile.mkdtemp(dir=base)); before = tree_hash(s)
    held = try_lock(s / "locks" / "cas.lock")
    try: check("concurrent-writer-holds-lock", analyze_and_export(s, out), "REFUSE_BUSY_WRITER_ACTIVE", None, s, out, before=before)
    finally: unlock(held)

    s = make_store(base, A + B, pub); out = Path(tempfile.mkdtemp(dir=base))
    def grow(ev):
        if ev == "after_inventory":
            with open(s / "journal" / "journal.log", "ab") as fh: fh.write(C)
    check("journal-growth-during-export", analyze_and_export(s, out, _hook=grow), "REFUSE_STORE_CHANGED_DURING_EXPORT", None, s, out)
    if any(out.iterdir()): print("FAIL growth wrote output"); fails += 1

    s = make_store(base, A + B + b"\x01", pub); out = Path(tempfile.mkdtemp(dir=base)); before = tree_hash(s)
    def boom(ev):
        if ev.startswith("copied:data/"): raise KeyboardInterrupt("simulated crash")
    try: analyze_and_export(s, out, _hook=boom); fails += 1; print("FAIL no crash")
    except KeyboardInterrupt: pass
    first = sorted(p.name for p in out.iterdir()); first_hash = tree_hash(out)
    (out / first[0] / "UNEXPECTED-USER-FILE").write_bytes(b"not ours")
    first_hash = tree_hash(out)
    check("interrupted-export-rerun", analyze_and_export(s, out), INC, ["TAIL_UNPARSABLE_NO_LATER_FRAME_FOUND"], s, out, before=before)
    after = tree_hash(out)
    preserved = all(after.get(k) == v for k, v in first_hash.items()) and len([n for n in os.listdir(out) if n.startswith(".incomplete-")]) == 1
    print(("PASS " if preserved else "FAIL ") + "prior-incomplete-attempt-preserved -> " + json.dumps(sorted(os.listdir(out))[:1] and [".incomplete-* kept", "recovery-candidate-* created"]))
    fails += not preserved
    check("rerun-refuses-overwrite", analyze_and_export(s, out), "REFUSE_OUTPUT_EXISTS", None, s, out, before=before)

    s = make_store(base, A + B, pub); out = Path(tempfile.mkdtemp(dir=base))
    target = base / "outside-secret"; target.write_bytes(b"outside")
    try:
        os.symlink(target, s / "data" / "a" / "link"); before = tree_hash(s)
        check("symlink-in-store", analyze_and_export(s, out), "REFUSE_SYMLINK_HARDLINK_OR_SPECIAL", None, s, out, before=before)
    except (OSError, NotImplementedError) as e:
        print("SKIP symlink-in-store:", type(e).__name__)
    s = make_store(base, A + B, pub); out = Path(tempfile.mkdtemp(dir=base))
    try:
        os.link(s / "data" / "a" / "one", base / "hardlink-outside"); before = tree_hash(s)
        check("hardlink-in-store", analyze_and_export(s, out), "REFUSE_SYMLINK_HARDLINK_OR_SPECIAL", None, s, out, before=before)
    except (OSError, NotImplementedError) as e:
        print("SKIP hardlink-in-store:", type(e).__name__)

    print("PLATFORM", os.name, sys.platform)
    print("RESULT", "OK" if not fails else f"{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
