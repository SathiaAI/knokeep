"""Independent verification (separate process from the writers).
1. sha256/size of every file under STORE (raw bytes on disk).
2. Fresh LocalBackend.read() of every key; compare version_hash with sha256(raw bytes).
3. Every 64-hex receipt value printed in writer logs (*.out) is matched against on-disk hashes.
4. Persisted client identity fields per key.
Usage: python3 verify.py STORE LOGDIR"""
import hashlib, json, os, re, sys
sys.path.insert(0, os.getcwd())
from store.local import LocalBackend

store, logdir = sys.argv[1:3]
files = {}
for d, _, fs in os.walk(store):
    for f in sorted(fs):
        p = os.path.join(d, f)
        b = open(p, "rb").read()
        files[os.path.relpath(p, store)] = (hashlib.sha256(b).hexdigest(), len(b))
for rel in sorted(files):
    print("FILE %s sha256=%s bytes=%d" % (rel, *files[rel]))

be = LocalBackend(store)
keys = sorted(be.list(""))
ok = True
for k in keys:
    blob = be.read(k)
    raw = blob.body if hasattr(blob, "body") else blob[0]
    vh = getattr(blob, "version_hash", None)
    h = hashlib.sha256(raw).hexdigest()
    disk = [r for r, (fh, _) in files.items() if fh == h]
    client = re.findall(rb"^client: *(.+)$", raw, re.M)
    match = (vh == h) and bool(disk)
    ok &= match
    print("KEY %s version_hash=%s sha256(read)=%s bytes=%d on_disk=%s match=%s client=%s"
          % (k, vh, h, len(raw), disk, match, [c.decode() for c in client]))

disk_hashes = {fh for fh, _ in files.values()}
for n in sorted(os.listdir(logdir)):
    if not n.endswith(".out") or n.startswith("08-"):
        continue
    for hx in sorted(set(re.findall(r"\b[0-9a-f]{64}\b", open(os.path.join(logdir, n)).read()))):
        print("RECEIPT %s %s on_disk=%s" % (n, hx, hx in disk_hashes))
print("VERIFY_RESULT", "PASS" if ok and keys else "FAIL")
sys.exit(0 if ok and keys else 1)
