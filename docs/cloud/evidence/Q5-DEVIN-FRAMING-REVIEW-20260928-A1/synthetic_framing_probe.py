"""Independent synthetic probe for LocalBackend journal framing.

Run from repo root: python3 docs/cloud/evidence/Q5-DEVIN-FRAMING-REVIEW-20260928-A1/synthetic_framing_probe.py
Never allocates huge buffers: every journal read(n) is spied and n is
checked against the real file size; tails are at most a few dozen bytes.
Exit 0 iff all cases behave as expected.
"""
import builtins, hashlib, struct, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from store.local import LocalBackend  # noqa: E402

MAGIC = b"KKJ2"
real_open = builtins.open
MAXREAD = {"n": 0}


class Spy:
    def __init__(self, fh, limit):
        self.fh, self.limit = fh, limit
    def __enter__(self): return self
    def __exit__(self, *a): self.fh.close()
    def __getattr__(self, k): return getattr(self.fh, k)
    def read(self, n=-1):
        MAXREAD["n"] = max(MAXREAD["n"], n)
        if not (0 <= n <= self.limit):
            raise AssertionError(f"untrusted length reached read(): {n}")
        return self.fh.read(n)


def rec(key, body, v2, fence=0, key_len=None, body_len=None, digest=None):
    kb = key.encode()
    out = (MAGIC if v2 else b"") + struct.pack(">I", len(kb) if key_len is None else key_len) + kb
    out += struct.pack(">Q", len(body) if body_len is None else body_len) + body
    out += hashlib.sha256(body).digest() if digest is None else digest
    if v2:
        out += struct.pack(">Q", fence)
    return out


def scan(journal: bytes):
    d = Path(tempfile.mkdtemp())
    b = LocalBackend(d)
    b.close()
    jp = b._journal_path
    jp.write_bytes(journal)
    limit = len(journal)
    def g(p, mode="r", *a, **k):
        fh = real_open(p, mode, *a, **k)
        return Spy(fh, limit) if str(p) == str(jp) and mode == "rb" else fh
    builtins.open = g
    try:
        st = {}
        recs = list(b._iter_journal_records(st))
    finally:
        builtins.open = real_open
    assert jp.read_bytes() == journal, "journal bytes mutated by scan"
    return recs, st


good_l = rec("a/one", b"#knokeep-gen:1\nL", False)
good_v = rec("a/two", b"#knokeep-gen:1\nV", True, fence=7)
prefix = good_l + good_v
UINT32, UINT64 = 2**32 - 1, 2**64 - 1
cases = {
    # name: (tail, expected yielded count)
    "valid-mixed-only": (b"", 2),
    "legacy-uint32-keylen": (struct.pack(">I", UINT32), 2),
    "v2-uint32-keylen": (MAGIC + struct.pack(">I", UINT32), 2),
    "legacy-keylen-1025": (struct.pack(">I", 1025) + b"k" * 1100, 2),
    "legacy-keylen-0": (struct.pack(">I", 0) + b"\0" * 60, 2),
    "legacy-uint64-body": (struct.pack(">I", 1) + b"k" + struct.pack(">Q", UINT64), 2),
    "v2-uint64-body": (MAGIC + struct.pack(">I", 1) + b"k" + struct.pack(">Q", UINT64), 2),
    "v2-body-2^63": (MAGIC + struct.pack(">I", 1) + b"k" + struct.pack(">Q", 2**63) + b"x" * 40, 2),
    "v2-body-len-off-by-one": (rec("k", b"xy", True, body_len=3), 2),
    "legacy-truncated-digest": (rec("k", b"x", False)[:-1], 2),
    "v2-truncated-fence": (rec("k", b"x", True)[:-1], 2),
    "v2-magic-only": (MAGIC, 2),
    "v2-magic-partial-keylen": (MAGIC + b"\0\0", 2),
    "bad-digest": (rec("k", b"x", True, digest=b"\0" * 32), 2),
    "valid-trailing-legacy": (rec("k/z", b"z", False), 3),
    "valid-trailing-v2-maxkey": (rec("k" * 1024, b"z", True, fence=2**63 - 1), 3),
    "valid-empty-body-v2": (rec("k/e", b"", True), 3),
}
fail = 0
for name, (tail, want) in cases.items():
    MAXREAD["n"] = 0
    try:
        recs, st = scan(prefix + tail)
        ok = len(recs) == want and recs[0][0] == "a/one" and recs[1][2] == 7
        clean = st["end"] == st["size"]
        exp_clean = want == 3 or tail == b""
        ok = ok and clean == exp_clean
        print(f"{'PASS' if ok else 'FAIL'} {name}: yielded={len(recs)} end={st['end']} size={st['size']} maxread={MAXREAD['n']}")
    except Exception as e:  # crash or allocation-sized read
        ok = False
        print(f"FAIL {name}: {type(e).__name__}: {e}")
    fail += not ok
print("RESULT", "OK" if not fail else f"{fail} FAILED")
sys.exit(1 if fail else 0)
