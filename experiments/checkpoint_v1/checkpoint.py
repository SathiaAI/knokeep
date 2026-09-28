"""EXPERIMENTAL checkpoint_v1 prototype -- not production, not wired into the CLI.

Immutable proposal objects + one authoritative CAS head, over the EXISTING
LocalBackend and store.gate.persist. A proposal is STAGED until the head
journal event references it; that fenced CAS head write is the logical commit.
Nothing is deleted or compacted. Legacy system_state/session_log are only read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from typing import Any, Dict, List, Optional

from store import gate
from store.backend import BackendBusyError
from store.context import AuthContext, CreateOnly, OperationContext, Overwrite
from store.local import LocalBackend
from store.types import EXISTS, OK, STALE

VERSION = 1
MAX_OBJECT_BYTES = 256 * 1024
MAX_LIST = 256
MAX_HISTORY = 256
DEFAULT_LEASE_TTL_S = 30.0
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = {
    "version", "milestone_id", "writer", "predecessor", "state", "state_sha256",
    "log", "log_sha256", "open_questions", "completed_actions", "resolves_staged",
    "legacy_state_sha256", "legacy_log_sha256",
}


class CheckpointError(Exception):
    pass


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def proposal_key(project: str, mid: str) -> str:
    return f"{project}/checkpoints_v1/proposals/{mid}"


def head_key(project: str) -> str:
    return f"{project}/checkpoints_v1/HEAD"


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _strict_loads(raw: bytes) -> Any:
    _need(len(raw) <= MAX_OBJECT_BYTES, 'JSON exceeds checkpoint size limit')
    def hook(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                raise CheckpointError(f"duplicate key: {k}")
            d[k] = v
        return d

    def bad_const(c):
        raise CheckpointError(f"non-finite value: {c}")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=hook, parse_constant=bad_const)
    except CheckpointError:
        raise
    except Exception as e:
        raise CheckpointError(f"malformed JSON: {e}") from None


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise CheckpointError(msg)


def _is_id(v: Any) -> bool:
    return (isinstance(v, str) and bool(_ID_RE.fullmatch(v))
            and gate.validate_write(v, b'', doc_type='journal') is None)


def _reference(ref):
    _need(isinstance(ref, dict) and set(ref) == {'milestone_id','sha256'}, 'reference shape')
    _need(_is_id(ref['milestone_id']), 'reference milestone invalid')
    _need(isinstance(ref['sha256'], str) and bool(_HEX64.fullmatch(ref['sha256'])), 'reference hash invalid')


def _id_list(v: Any, name: str) -> List[str]:
    _need(isinstance(v, list) and len(v) <= MAX_LIST, f"{name}: list of <= {MAX_LIST}")
    _need(all(_is_id(x) for x in v), f"{name}: invalid id")
    _need(len(set(v)) == len(v), f"{name}: duplicate id")
    return v


def validate_proposal(p: Any) -> None:
    """Strict v1 structural validation + secret scan of decoded user text."""
    _need(isinstance(p, dict), "proposal must be an object")
    _need(set(p) == _FIELDS, f"fields must be exactly {sorted(_FIELDS)}")
    _need(type(p["version"]) is int and p["version"] == VERSION, "version must be 1")
    _need(_is_id(p["milestone_id"]), "milestone_id invalid")
    _need(_is_id(p["writer"]), "writer invalid")
    pred = p["predecessor"]
    if pred is not None:
        _reference(pred)
    for f in ("state", "log"):
        _need(isinstance(p[f], str), f"{f} must be a string")
        _need(p[f + "_sha256"] == _sha(p[f].encode("utf-8")), f"{f}_sha256 mismatch")
    for f in ("legacy_state_sha256", "legacy_log_sha256"):
        _need(p[f] is None or (isinstance(p[f], str) and bool(_HEX64.match(p[f]))), f"{f} invalid")
    oq = p["open_questions"]
    _need(isinstance(oq, list) and len(oq) <= MAX_LIST, "open_questions: bounded list")
    texts = [p["state"], p["log"], p["writer"]]
    affected = set()
    question_ids = set()
    for q in oq:
        _need(isinstance(q, dict) and set(q) == {"id", "question", "affected_action_ids"},
              "open_question shape")
        _need(_is_id(q["id"]), "open_question.id invalid")
        _need(q['id'] not in question_ids, 'duplicate open question id')
        question_ids.add(q['id'])
        _need(isinstance(q["question"], str) and bool(q['question'].strip()), "open_question.question must be nonempty string")
        texts.append(q["question"])
        affected.update(_id_list(q["affected_action_ids"], "affected_action_ids"))
    done = set(_id_list(p["completed_actions"], "completed_actions"))
    refs = p['resolves_staged']
    _need(isinstance(refs, list) and len(refs) <= MAX_LIST, 'resolves_staged must be bounded references')
    for ref in refs:
        _reference(ref)
    _need(len({r['milestone_id'] for r in refs}) == len(refs), 'duplicate resolved reference')
    clash = sorted(done & affected)
    _need(not clash, f"completed action(s) with unresolved open question: {clash}")
    _need(len(_canon(p)) <= MAX_OBJECT_BYTES, f"proposal exceeds {MAX_OBJECT_BYTES} bytes")
    # Existing scanner rules on each DECODED text (JSON escaping cannot hide it);
    # gate.persist additionally scans the canonical envelope.
    for t in texts:
        rejection = gate.validate_write('checkpoint-text', t.encode('utf-8'), doc_type='journal')
        _need(rejection is None, f'decoded text refused: {rejection}')


def build_proposal(*, milestone_id, writer, predecessor, state, log, open_questions=(),
                   completed_actions=(), resolves_staged=(), legacy_state_sha256=None,
                   legacy_log_sha256=None) -> Dict[str, Any]:
    return {
        "version": VERSION, "milestone_id": milestone_id, "writer": writer,
        "predecessor": predecessor, "state": state, "state_sha256": _sha(state.encode("utf-8")),
        "log": log, "log_sha256": _sha(log.encode("utf-8")),
        "open_questions": list(open_questions), "completed_actions": list(completed_actions),
        "resolves_staged": list(resolves_staged),
        "legacy_state_sha256": legacy_state_sha256, "legacy_log_sha256": legacy_log_sha256,
    }


def _read_proposal(backend, project: str, mid: str, expect_sha: Optional[str] = None):
    blob = backend.read(proposal_key(project, mid))
    if blob is None:
        return None
    raw = blob.body
    if expect_sha is not None and _sha(raw) != expect_sha:
        raise CheckpointError(f"proposal {mid} hash mismatch (corrupt)")
    p = _strict_loads(raw)
    validate_proposal(p)
    _need(p["milestone_id"] == mid, f"proposal {mid} id mismatch (corrupt)")
    _need(_canon(p) == raw, f"proposal {mid} not canonical (corrupt)")
    return p, _sha(raw)


def _read_head(backend, project: str):
    blob = backend.read(head_key(project))
    if blob is None:
        return None, None
    raw = blob.body
    nl = raw.find(b"\n")
    _need(raw.startswith(b"#knokeep-gen:") and nl > 0, "HEAD corrupt")
    h = _strict_loads(raw[nl + 1:])
    _need(isinstance(h, dict) and set(h) == {"version", "milestone_id", "sha256", "depth"},
          "HEAD corrupt")
    _need(type(h['version']) is int and h['version'] == VERSION, 'HEAD version corrupt')
    _need(type(h['depth']) is int and 1 <= h['depth'] <= MAX_HISTORY, 'HEAD depth corrupt')
    _reference({'milestone_id':h['milestone_id'],'sha256':h['sha256']})
    _need(raw == gate.make_generation_header(h['depth']) + _canon(h), 'HEAD generation or encoding corrupt')
    return h, blob.version_hash


def _chain(backend, project: str, head) -> List[Dict[str, Any]]:
    """Walk from head target to root, verifying every hash. Fails closed."""
    out = []
    ref = {"milestone_id": head["milestone_id"], "sha256": head["sha256"]}
    while ref is not None:
        _need(len(out) < MAX_HISTORY, f"history walk exceeds {MAX_HISTORY} checkpoints")
        got = _read_proposal(backend, project, ref["milestone_id"], ref["sha256"])
        _need(got is not None, f"checkpoint target {ref['milestone_id']} missing")
        p, h = got
        _need(p['milestone_id'] not in {c['milestone_id'] for c in out}, 'checkpoint cycle')
        out.append({"milestone_id": p["milestone_id"], "sha256": h, "proposal": p})
        ref = p["predecessor"]
    _need(len(out) == head['depth'], 'HEAD depth disagrees with chain')
    accepted = {c['milestone_id'] for c in out}
    for c in out:
        _check_resolutions(backend, project, c['proposal'], accepted)
    return out


def _check_resolutions(backend, project, proposal, accepted):
    for ref in proposal['resolves_staged']:
        _need(ref['milestone_id'] not in accepted, 'resolution target was accepted, not staged')
        _need(ref['milestone_id'] != proposal['milestone_id'], 'self resolution')
        got = _read_proposal(backend, project, ref['milestone_id'], ref['sha256'])
        _need(got is not None, 'resolved staged proposal missing')


def _resolved_ids(chain):
    return {r['milestone_id'] for c in chain for r in c['proposal']['resolves_staged']}


def _legacy_hashes(backend, project: str):
    res = {}
    for name, k in (("state", f"{project}/system_state"), ("log", f"{project}/session_log")):
        b = backend.read(k)
        res[name] = b.version_hash if b is not None else None
    return res


def _ctx(pre) -> OperationContext:
    return OperationContext(auth=AuthContext(), precondition=pre)


def save(backend, project: str, proposal: Dict[str, Any], *,
         lease_ttl_s: float = DEFAULT_LEASE_TTL_S) -> Dict[str, Any]:
    _need(_is_id(project), 'project invalid')
    proposal = _strict_loads(_canon(proposal))  # freeze caller-owned mutable input
    validate_proposal(proposal)  # before staging: bad input reserves no ID
    old_head, _ = _read_head(backend, project)
    old_chain = _chain(backend, project, old_head) if old_head else []
    _check_resolutions(backend, project, proposal, {c['milestone_id'] for c in old_chain})
    raw = _canon(proposal)
    mid = proposal["milestone_id"]
    my_sha = _sha(raw)
    r = gate.persist(backend, proposal_key(project, mid), raw, ctx=_ctx(CreateOnly()),
                     doc_type="journal")
    if isinstance(r, EXISTS):
        raise CheckpointError(f"milestone_id {mid} already used with different bytes")
    if not isinstance(r, OK):
        raise CheckpointError(f"staging failed: {r}")
    # Proposal is durable but only STAGED. Publish head under a lease.
    hk = head_key(project)
    lease = backend.lock(hk, lease_ttl_s)
    try:
        head, head_ver = _read_head(backend, project)
        if head is not None:
            chain = _chain(backend, project, head)
            for c in chain:
                if c["milestone_id"] == mid and c["sha256"] == my_sha:
                    return {"status": "accepted", "duplicate": True, "milestone_id": mid,
                            "checkpoint_sha256": my_sha,
                            "current_head_sha256": head["sha256"],
                            "current_head_milestone_id": head["milestone_id"]}
            cur = {"milestone_id": head["milestone_id"], "sha256": head["sha256"]}
            depth = head["depth"] + 1
        else:
            cur, depth = None, 1
        stale = {"status": "staged_stale", "duplicate": False, "milestone_id": mid,
                 "checkpoint_sha256": my_sha,
                 "current_head_sha256": cur["sha256"] if cur else None,
                 "current_head_milestone_id": cur["milestone_id"] if cur else None}
        if proposal["predecessor"] != cur:
            return stale  # never silently rebase
        _need(depth <= MAX_HISTORY, 'history limit reached; staged proposal retained, head unchanged')
        body = gate.make_generation_header(depth) + _canon(
            {"version": VERSION, "milestone_id": mid, "sha256": my_sha, "depth": depth})
        pre = CreateOnly() if head is None else Overwrite(expected_hash=head_ver, lease=lease)
        w = gate.persist(backend, hk, body, ctx=_ctx(pre), doc_type="checkpoint_head_v1")
        if isinstance(w, (STALE, EXISTS)):
            return stale
        if not isinstance(w, OK):
            raise CheckpointError(f"head publish failed: {w}")
        readback = lookup(backend, project, mid)
        _need(readback['status'] == 'accepted' and readback['checkpoint_sha256'] == my_sha,
              'head write acknowledged but checkpoint readback failed; outcome uncertain')
        return {"status": "accepted", "duplicate": False, "milestone_id": mid,
                "checkpoint_sha256": my_sha, "current_head_sha256": my_sha,
                "current_head_milestone_id": mid}
    finally:
        backend.unlock(lease)


def _staged(backend, project: str, accepted) -> List[str]:
    pre = f"{project}/checkpoints_v1/proposals/"
    return sorted(k[len(pre):] for k in backend.list(pre) if k[len(pre):] not in accepted)


def resume(backend, project: str) -> Dict[str, Any]:
    _need(_is_id(project), 'project invalid')
    head, _ = _read_head(backend, project)
    if head is None:
        return {"status": "no_checkpoint", "staged": _staged(backend, project, set())}
    chain = _chain(backend, project, head)
    tgt = chain[0]["proposal"]
    accepted = {c["milestone_id"] for c in chain}
    resolved = _resolved_ids(chain)
    legacy = _legacy_hashes(backend, project)
    unsealed = [n for n, f in (("system_state", "state"), ("session_log", "log"))
                if legacy[f] != tgt[f"legacy_{f}_sha256"]]
    return {"status": "ok", "milestone_id": tgt["milestone_id"],
            "checkpoint_sha256": head["sha256"], "state": tgt["state"], "log": tgt["log"],
            "open_questions": tgt["open_questions"],
            "completed_actions": tgt["completed_actions"],
            "unsealed_legacy_changes": unsealed, "staged": _staged(backend, project, accepted | resolved),
            "resolved_staged": sorted(resolved)}


def lookup(backend, project: str, mid: str) -> Dict[str, Any]:
    _need(_is_id(project) and _is_id(mid), 'project or milestone invalid')
    got = _read_proposal(backend, project, mid)
    if got is None:
        return {"status": "not_found", "milestone_id": mid}
    _, sha = got
    head, _ = _read_head(backend, project)
    if head is not None:
        chain = _chain(backend, project, head)
        for c in chain:
            if c["milestone_id"] == mid:
                return {"status": "accepted", "milestone_id": mid, "checkpoint_sha256": sha,
                        "current_head_sha256": head["sha256"]}
        if mid in _resolved_ids(chain):
            return {'status':'resolved_staged','milestone_id':mid,'checkpoint_sha256':sha,
                    'current_head_sha256':head['sha256']}
    return {"status": "staged", "milestone_id": mid, "checkpoint_sha256": sha,
            "current_head_sha256": head["sha256"] if head else None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="EXPERIMENTAL checkpoint_v1 (not production)")
    ap.add_argument("--store", required=True)
    ap.add_argument("--project", required=True)
    sub = ap.add_subparsers(dest="op", required=True)
    s = sub.add_parser("save")
    s.add_argument("--input", required=True)
    sub.add_parser("resume")
    lk = sub.add_parser("lookup")
    lk.add_argument("--milestone-id", required=True)
    a = ap.parse_args(argv)
    _need(_is_id(a.project), 'project invalid')
    be = LocalBackend(a.store)
    try:
        if a.op == "save":
            with open(a.input, "rb") as f:
                data = f.read(MAX_OBJECT_BYTES + 1)
            _need(len(data) <= MAX_OBJECT_BYTES, "input exceeds size limit")
            out = save(be, a.project, _strict_loads(data))
        elif a.op == "resume":
            out = resume(be, a.project)
        else:
            out = lookup(be, a.project, a.milestone_id)
    except CheckpointError as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        return 2
    except BackendBusyError:
        print(json.dumps({'status':'busy','error':'lease unavailable; proposal may be staged; retry exact payload'}))
        return 3
    finally:
        be.close()
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("status") in ("ok", "accepted", "staged", "resolved_staged", "no_checkpoint") else 1


if __name__ == "__main__":
    sys.exit(main())
