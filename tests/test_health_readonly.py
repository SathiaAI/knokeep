#!/usr/bin/env python3
"""Read-only health inspection: corrupt stores must not report healthy; health must not mutate storage."""
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "skill", "knokeep_state.py")
sys.path.insert(0, ROOT)

from store import gate
from store import health_inspect as hi
from store.local import LocalBackend
from store.types import OK
from tests.ctx_helpers import create_ctx
import skill.knokeep_state as knokeep_state

results = []


def check(name, cond):
    results.append((name, cond))
    print(("PASS " if cond else "FAIL ") + name)


def run_health(store):
    p = subprocess.run(
        [sys.executable, STATE, "health", "--store", store],
        capture_output=True,
        text=True,
    )
    try:
        body = json.loads(p.stdout)
    except json.JSONDecodeError:
        body = {}
    return p.returncode, body, p.stdout + p.stderr


def snapshot_tree(root):
    snap = {}
    if not os.path.exists(root):
        return snap
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git",)]
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            try:
                st = os.stat(path)
                with open(path, "rb") as f:
                    snap[path] = (st.st_mtime_ns, st.st_size, f.read())
            except OSError:
                snap[path] = None
    return snap


def assert_unchanged(root, before, after):
    if not os.path.exists(root) and not before:
        return True
    return before == after


def legacy_journal_record(key: str, body: bytes) -> bytes:
    kb = key.encode("utf-8")
    return (
        struct.pack(">I", len(kb))
        + kb
        + struct.pack(">Q", len(body))
        + body
        + hashlib.sha256(body).digest()
    )


# --- valid v2 store ---------------------------------------------------------

good_root = tempfile.mkdtemp(prefix="kk_health_good_")
backend = LocalBackend(good_root)
key = "demo/system_state"
body = b"---\nrevision: 1\nupdated: 2020-01-01T00:00:00Z\n---\n# Demo\n"
r = gate.persist(
    backend, key, body, ctx=create_ctx(), doc_type="system_state"
)
assert isinstance(r, OK)
backend.close()

rc, hj, _ = run_health(good_root)
check("valid v2 store: exit 0", rc == 0 and hj.get("verdict") == "healthy")
check("valid v2 store: store ok", hj.get("store", {}).get("ok") is True)
check("valid v2 projects listed", any(p.get("project") == "demo" for p in hj.get("projects", [])))

# legacy-shaped record only (pre-KKJ2) on fresh dirs
legacy_root = tempfile.mkdtemp(prefix="kk_health_legacy_")
os.makedirs(os.path.join(legacy_root, "journal"), exist_ok=True)
os.makedirs(os.path.join(legacy_root, "data", "leg"), exist_ok=True)
leg_body = b"---\nrevision: 1\n---\nlegacy\n"
leg_key = "leg/system_state"
with open(os.path.join(legacy_root, "data", "leg", "system_state"), "wb") as f:
    f.write(leg_body)
with open(os.path.join(legacy_root, "journal", "journal.log"), "wb") as f:
    f.write(legacy_journal_record(leg_key, leg_body))
rc, hj, _ = run_health(legacy_root)
check("legacy journal + published blob: healthy", rc == 0 and hj.get("verdict") == "healthy")

# --- false healthy repro (UINT64_MAX tail) ----------------------------------

bad_root = tempfile.mkdtemp(prefix="kk_health_bad_")
b2 = LocalBackend(bad_root)
b2._journal_append("proj/system_state", b"---\nrevision: 1\n---\nok\n")
b2.close()
journal_path = os.path.join(bad_root, "journal", "journal.log")
kb = b"proj/system_state"
bad_tail = (
    b"KKJ2"
    + struct.pack(">I", len(kb))
    + kb
    + struct.pack(">Q", (1 << 64) - 1)
    + b"x" * 32
)
with open(journal_path, "ab") as f:
    f.write(bad_tail)
rc, hj, out = run_health(bad_root)
check("overflow journal: nonzero exit", rc != 0)
check("overflow journal: not healthy", hj.get("verdict") != "healthy")
check(
    "overflow journal: store reason present",
    any(p.startswith("store:") for p in hj.get("problems", [])),
)

# --- impossible / torn / digest ---------------------------------------------

imp_root = tempfile.mkdtemp(prefix="kk_health_imp_")
os.makedirs(os.path.join(imp_root, "journal"))
with open(os.path.join(imp_root, "journal", "journal.log"), "wb") as f:
    f.write(b"KKJ2" + struct.pack(">I", 3) + b"abc" + struct.pack(">Q", 9 * 1024 * 1024 + 1))
rc, hj, _ = run_health(imp_root)
check(
    "impossible body length flagged",
    "store:journal_impossible_body_length" in hj.get("problems", []),
)

torn_root = tempfile.mkdtemp(prefix="kk_health_torn_")
os.makedirs(os.path.join(torn_root, "journal"))
with open(os.path.join(torn_root, "journal", "journal.log"), "wb") as f:
    f.write(b"KKJ2" + struct.pack(">I", 1) + b"x")
rc, hj, _ = run_health(torn_root)
check("torn tail flagged", "store:journal_torn_tail" in hj.get("problems", []))

digest_root = tempfile.mkdtemp(prefix="kk_health_digest_")
os.makedirs(os.path.join(digest_root, "journal"))
good = b"payload"
with open(os.path.join(digest_root, "journal", "journal.log"), "wb") as f:
    f.write(
        b"KKJ2"
        + struct.pack(">I", 3)
        + b"k/b"
        + struct.pack(">Q", len(good))
        + good
        + b"\0" * 32
        + struct.pack(">Q", 0)
    )
rc, hj, _ = run_health(digest_root)
check("bad digest flagged", "store:journal_bad_digest" in hj.get("problems", []))

# --- unpublished durable record ---------------------------------------------

unpub_root = tempfile.mkdtemp(prefix="kk_health_unpub_")
b3 = LocalBackend(unpub_root)
b3._journal_append("p/system_state", b"---\nrevision: 2\n---\nonly-journal\n")
b3.close()
rc, hj, _ = run_health(unpub_root)
check("unpublished journal record flagged", "store:data_unpublished" in hj.get("problems", []))

# --- missing store (must not create root) -----------------------------------

missing_parent = tempfile.mkdtemp(prefix="kk_health_missing_parent_")
missing = os.path.join(missing_parent, "no_such_store")
before_exists = os.path.exists(missing)
snap_before = snapshot_tree(missing_parent)
rc, hj, _ = run_health(missing)
snap_after = snapshot_tree(missing_parent)
check("missing store: exit nonzero", rc != 0)
check("store:store_missing in problems", "store:store_missing" in hj.get("problems", []))
check("missing path not created", not os.path.exists(missing) and not before_exists)
check("missing parent unchanged", assert_unchanged(missing_parent, snap_before, snap_after))

# --- data without journal (unverified) --------------------------------------

data_only_root = tempfile.mkdtemp(prefix="kk_health_data_only_")
os.makedirs(os.path.join(data_only_root, "data", "orphan"))
with open(os.path.join(data_only_root, "data", "orphan", "system_state"), "wb") as f:
    f.write(b"---\nrevision: 1\n---\norphan\n")
rc, hj, _ = run_health(data_only_root)
check(
    "data without journal: attention",
    rc != 0 and hj.get("verdict") == "attention",
)
check(
    "data without journal: journal_missing",
    "store:journal_missing" in hj.get("problems", []),
)
check(
    "data without journal: store not ok",
    hj.get("store", {}).get("ok") is False,
)

# --- journal changed during inspection (deterministic probe) ------------------

race_root = tempfile.mkdtemp(prefix="kk_health_race_")
b_race = LocalBackend(race_root)
gate.persist(
    b_race,
    "race/system_state",
    b"---\nrevision: 1\n---\nrace\n",
    ctx=create_ctx(),
    doc_type="system_state",
)
b_race.close()
real_inspect = hi._inspect_journal


def _race_inspect(journal_path):
    reasons, latest = real_inspect(journal_path)
    with open(journal_path, "ab") as fh:
        fh.write(b"x")
    return reasons, latest


with mock.patch.object(hi, "_inspect_journal", side_effect=_race_inspect):
    raced = hi.inspect_local_store(race_root)
check("race probe: indeterminate", raced.get("indeterminate") is True)
check("race probe: not ok", raced.get("ok") is False)
check(
    "race probe: inspect_indeterminate reason",
    "inspect_indeterminate" in (raced.get("reasons") or []),
)
rc_race, hj_race, _ = run_health(race_root)
check("race probe health: attention", hj_race.get("verdict") == "attention")

# --- indeterminate forces attention even if inspect reports ok:true -----------

with mock.patch.object(
    knokeep_state,
    "inspect_local_store",
    return_value={"ok": True, "reasons": [], "indeterminate": True},
):
    h_ind = knokeep_state.health(good_root)
check(
    "indeterminate alone forces attention",
    h_ind.get("verdict") == "attention",
)
check(
    "indeterminate maps to store problem",
    "store:inspect_indeterminate" in h_ind.get("problems", []),
)

# --- journal key traversal rejected without traceback -------------------------

trav_root = tempfile.mkdtemp(prefix="kk_health_trav_")
os.makedirs(os.path.join(trav_root, "journal"))
bad_key = "../../outside"
bk = bad_key.encode("utf-8")
payload = b"escape"
digest = hashlib.sha256(payload).digest()
with open(os.path.join(trav_root, "journal", "journal.log"), "wb") as f:
    f.write(
        b"KKJ2"
        + struct.pack(">I", len(bk))
        + bk
        + struct.pack(">Q", len(payload))
        + payload
        + digest
        + struct.pack(">Q", 0)
    )
rc, hj, combined = run_health(trav_root)
check("traversal key: structured health", rc != 0 and hj.get("verdict") == "attention")
check(
    "traversal key: journal_bad_key",
    "store:journal_bad_key" in hj.get("problems", []),
)
check("traversal key: no traceback", "Traceback" not in combined)

# --- corrupt telemetry on otherwise valid store -----------------------------

tele_root = tempfile.mkdtemp(prefix="kk_health_tele_")
b6 = LocalBackend(tele_root)
gate.persist(
    b6,
    "t/system_state",
    b"---\nrevision: 1\n---\nt\n",
    ctx=create_ctx(),
    doc_type="system_state",
)
b6.close()
ev_dir = os.path.join(tele_root, ".knokeep-eval")
os.makedirs(ev_dir, exist_ok=True)
with open(os.path.join(ev_dir, "events.jsonl"), "wb") as f:
    f.write(b"\xff")
rc, hj, combined = run_health(tele_root)
check("corrupt telemetry: attention", hj.get("verdict") == "attention")
check(
    "corrupt telemetry: reason code",
    "telemetry:telemetry_unreadable" in hj.get("problems", []),
)
check("corrupt telemetry: no traceback", "Traceback" not in combined)

# --- injected journal unreadable (no chmod) -----------------------------------

inj_root = tempfile.mkdtemp(prefix="kk_health_inj_")
b7 = LocalBackend(inj_root)
gate.persist(
    b7,
    "i/system_state",
    b"---\nrevision: 1\n---\ni\n",
    ctx=create_ctx(),
    doc_type="system_state",
)
b7.close()
journal_file = os.path.join(inj_root, "journal", "journal.log")
_real_open = open


def _deny_journal_open(path, *args, **kwargs):
    if str(path).endswith("journal.log"):
        raise PermissionError(13, "injected")
    return _real_open(path, *args, **kwargs)


with mock.patch("builtins.open", side_effect=_deny_journal_open):
    inj = hi.inspect_local_store(inj_root)
check(
    "injected permission: journal unreadable",
    inj.get("ok") is False and "journal_unreadable" in (inj.get("reasons") or []),
)

# --- symlink escape via published system_state (Linux) ------------------------

symlink_roots = []
if hasattr(os, "symlink"):
    try:
        outside = tempfile.mkdtemp(prefix="kk_health_outside_")
        symlink_roots.append(outside)
        secret = os.path.join(outside, "secret.txt")
        with open(secret, "w", encoding="utf-8") as f:
            f.write("outside-bytes")
        sym_root = tempfile.mkdtemp(prefix="kk_health_sym_")
        symlink_roots.append(sym_root)
        b8 = LocalBackend(sym_root)
        body_sym = b"---\nrevision: 1\n---\nfrom-journal\n"
        gate.persist(
            b8,
            "sym/system_state",
            body_sym,
            ctx=create_ctx(),
            doc_type="system_state",
        )
        b8.close()
        sym_state = os.path.join(sym_root, "data", "sym", "system_state")
        os.remove(sym_state)
        os.symlink(secret, sym_state)
        rc, hj, _ = run_health(sym_root)
        check(
            "symlink published blob flagged",
            rc != 0
            and (
                "store:store_symlink_escape" in hj.get("problems", [])
                or "store:data_unpublished" in hj.get("problems", [])
            ),
        )
        link_parent = tempfile.mkdtemp(prefix="kk_health_rootlink_")
        symlink_roots.append(link_parent)
        link_path = os.path.join(link_parent, "linked-root")
        os.symlink(sym_root, link_path)
        linked = hi.inspect_local_store(link_path)
        check(
            "symlink store root rejected",
            linked.get("ok") is False
            and "store_symlink_escape" in (linked.get("reasons") or []),
        )
    except OSError:
        check("symlink tests skipped (privilege)", True)

# --- pass 3 regressions -----------------------------------------------------

malformed_root = tempfile.mkdtemp(prefix="kk_health_malform_")
b_m = LocalBackend(malformed_root)
gate.persist(
    b_m,
    "mf/system_state",
    b"---\nrevision: 1\n---\nm\n",
    ctx=create_ctx(),
    doc_type="system_state",
)
b_m.close()
ev_d = os.path.join(malformed_root, ".knokeep-eval")
os.makedirs(ev_d, exist_ok=True)
with open(os.path.join(ev_d, "events.jsonl"), "w", encoding="utf-8") as f:
    f.write("[]\n")
    f.write("null\n")
    f.write('{"decision":"block","findings":[null]}\n')
    f.write('{"decision":"block","reasons":7}\n')
rc, hj, combined = run_health(malformed_root)
check("malformed telemetry lines: attention", hj.get("verdict") == "attention")
check(
    "malformed telemetry: unreadable code",
    "telemetry:telemetry_unreadable" in hj.get("problems", []),
)
check("malformed telemetry: no traceback", "Traceback" not in combined)

dangle_ev_root = tempfile.mkdtemp(prefix="kk_health_dangle_ev_")
b_de = LocalBackend(dangle_ev_root)
gate.persist(
    b_de,
    "de/system_state",
    b"---\nrevision: 1\n---\nde\n",
    ctx=create_ctx(),
    doc_type="system_state",
)
b_de.close()
ev_d2 = os.path.join(dangle_ev_root, ".knokeep-eval")
os.makedirs(ev_d2, exist_ok=True)
if hasattr(os, "symlink"):
    try:
        os.symlink(
            os.path.join(ev_d2, "missing-target-never-created"),
            os.path.join(ev_d2, "events.jsonl"),
        )
        rc, hj, _ = run_health(dangle_ev_root)
        check(
            "dangling events symlink: attention",
            hj.get("verdict") == "attention",
        )
        check(
            "dangling events symlink: telemetry unreadable",
            "telemetry:telemetry_unreadable" in hj.get("problems", []),
        )
    except OSError:
        check("dangling events symlink skipped", True)
else:
    check("dangling events symlink skipped (no symlink)", True)

empty_journal_root = tempfile.mkdtemp(prefix="kk_health_empty_journal_")
os.makedirs(os.path.join(empty_journal_root, "journal"))
open(os.path.join(empty_journal_root, "journal", "journal.log"), "wb").close()
rc, hj, _ = run_health(empty_journal_root)
check(
    "empty journal missing data dir: attention",
    hj.get("verdict") == "attention",
)
check(
    "empty journal missing data: layout reason",
    "store:data_layout_missing" in hj.get("problems", []),
)

sys.path.insert(0, os.path.join(ROOT, "tools"))
import knokeep_audit

with mock.patch.object(
    knokeep_audit,
    "audit",
    return_value={"scanner": "gitleaks:none", "leaks": 0},
):
    h_audit = knokeep_state.health(good_root)
check(
    "gitleaks none not treated as clean scan",
    h_audit.get("audit", {}).get("scanner") == "unavailable"
    and h_audit.get("audit", {}).get("leaks") is None,
)
check(
    "gitleaks none notes unavailable",
    any("audit_unavailable" in n for n in h_audit.get("notes", [])),
)

bad_ts_root = tempfile.mkdtemp(prefix="kk_health_bad_ts_")
b_ts = LocalBackend(bad_ts_root)
gate.persist(
    b_ts,
    "ts/system_state",
    b"---\nrevision: 1\n---\nts\n",
    ctx=create_ctx(),
    doc_type="system_state",
)
b_ts.close()
os.makedirs(os.path.join(bad_ts_root, ".knokeep-eval"), exist_ok=True)
# Exercise each row alone: a second invalid row can otherwise short-circuit
# validation before the malformed timestamp reaches the formerly broken path.
for label, event in (
    ("malformed", {"decision": "error", "ts": "not-a-timestamp"}),
    ("missing", {"decision": "error"}),
    ("numeric", {"decision": "error", "ts": 12345}),
    ("null", {"decision": "error", "ts": None}),
):
    with open(os.path.join(bad_ts_root, ".knokeep-eval", "events.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
    rc, hj, combined = run_health(bad_ts_root)
    check(f"{label} error timestamp: attention", hj.get("verdict") == "attention")
    check(
        f"{label} error timestamp: telemetry unreadable",
        "telemetry:telemetry_unreadable" in hj.get("problems", []),
    )
    check(f"{label} error timestamp: no traceback", "Traceback" not in combined)

if hasattr(os, "symlink"):
    try:
        audit_out = tempfile.mkdtemp(prefix="kk_health_audit_out_")
        nested_root = tempfile.mkdtemp(prefix="kk_health_audit_nested_")
        b_n = LocalBackend(nested_root)
        gate.persist(
            b_n,
            "n/system_state",
            b"---\nrevision: 1\n---\nn\n",
            ctx=create_ctx(),
            doc_type="system_state",
        )
        b_n.close()
        trap = os.path.join(nested_root, "data", "n", "trap")
        os.symlink(audit_out, trap)
        rc, hj, _ = run_health(nested_root)
        check(
            "nested data symlink blocks audit path",
            rc != 0
            and any(p.startswith("audit:") for p in hj.get("problems", [])),
        )
    except OSError:
        check("nested audit symlink skipped", True)

# --- bootstrap audit: raw published walk (orphan bypass) --------------------

bootstrap_audit_root = tempfile.mkdtemp(prefix="kk_health_bootstrap_audit_")
b_ba = LocalBackend(bootstrap_audit_root)
gate.persist(
    b_ba,
    "ba/system_state",
    b"---\nrevision: 1\n---\nok\n",
    ctx=create_ctx(),
    doc_type="system_state",
)
gate.persist(
    b_ba,
    "ba/session_log",
    b"---\nrevision: 1\n---\n## Active State\n\n## Next Step\n\n",
    ctx=create_ctx(),
    doc_type="session_log",
)
b_ba.close()
with open(
    os.path.join(bootstrap_audit_root, "data", "ba", "leak.md"),
    "w",
    encoding="utf-8",
) as _leak:
    _leak.write("ghp_EXAMPLE0000000000000000000000")
entries_ba, block_ba, _unc_ba = hi.enumerate_project_published_files_for_audit(
    bootstrap_audit_root, "ba"
)
check(
    "bootstrap audit walk lists orphan bypass blob",
    not block_ba and any(k == "ba/leak.md" for k, _ in entries_ba),
)
findings_ba = knokeep_state._audit_store(bootstrap_audit_root, "ba")
check(
    "bootstrap audit flags orphan secret",
    any(f.get("key") == "ba/leak.md" and f.get("reasons") for f in findings_ba),
)
p_ba = subprocess.run(
    [sys.executable, STATE, "bootstrap", "--store", bootstrap_audit_root, "--project", "ba"],
    capture_output=True,
    text=True,
)
check(
    "bootstrap CLI refuses orphan bypass secret",
    p_ba.returncode != 0 and "store contains secrets" in p_ba.stderr,
)

# --- redaction --------------------------------------------------------------

secret_key = "sekret/proj/system_state"
redact_root = tempfile.mkdtemp(prefix="kk_health_redact_")
b4 = LocalBackend(redact_root)
b4._journal_append(secret_key, b"---\nrevision: 1\n---\nbody\n")
b4.close()
with open(os.path.join(redact_root, "journal", "journal.log"), "ab") as f:
    f.write(b"\xff\xff\xff\xff")
_, _, combined = run_health(redact_root)
check("diagnostics omit absolute store path", redact_root not in combined)
check("diagnostics omit journal key string", secret_key not in combined)

# --- no mutation on synthetic store -----------------------------------------

mut_root = tempfile.mkdtemp(prefix="kk_health_mut_")
b5 = LocalBackend(mut_root)
gate.persist(
    b5,
    "m/system_state",
    b"---\nrevision: 1\n---\nstable\n",
    ctx=create_ctx(),
    doc_type="system_state",
)
b5.close()
before = snapshot_tree(mut_root)
time.sleep(0.05)
run_health(mut_root)
after = snapshot_tree(mut_root)
check("health does not mutate store bytes/mtimes", before == after)

for d in (
    good_root,
    legacy_root,
    bad_root,
    imp_root,
    torn_root,
    digest_root,
    unpub_root,
    missing_parent,
    redact_root,
    mut_root,
    data_only_root,
    race_root,
    trav_root,
    tele_root,
    inj_root,
    malformed_root,
    dangle_ev_root,
    empty_journal_root,
    bad_ts_root,
    bootstrap_audit_root,
) + tuple(symlink_roots):
    if os.path.exists(d):
        import shutil

        shutil.rmtree(d, ignore_errors=True)

passed = sum(1 for _, c in results if c)
print(f"\n{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
