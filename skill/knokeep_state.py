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
out-of-band store audit walks published files read-only via health_inspect (including
unjournaled bypass blobs LocalBackend read/list refuse) and re-scans raw bytes through
store.gate (content-based: reject non-utf-8/secret-bearing blobs). knokeep_secretgate retired."""
import sys, os, re, json, datetime, argparse
import hashlib, random, time, contextlib

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)   # store package (parent) — the shared V2 engine
from store import gate
from store.local import LocalBackend
from store.health_inspect import (
    inspect_local_store,
    read_project_summaries,
    store_aux_reads_allowed,
    safe_data_dir_for_audit,
    read_telemetry_bytes,
    resolve_store_root,
    enumerate_project_published_files_for_audit,
    is_os_metadata_basename,
)
from store.backend import BackendBusyError
from store.types import OK, STALE, EXISTS, ERROR, ErrorKind
from store.config import default_store_root
from store.context import create_ctx, overwrite_ctx
from application.identifiers import valid_id
from application.session_queries import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    SessionQueryError,
    list_sessions as query_list_sessions,
    read_session as query_read_session,
    session_store_key,
    validate_list_arguments,
)
from application.raw_sessions import (
    RawSessionError,
    append_raw_session as app_append_raw_session,
    read_raw_session as app_read_raw_session,
    read_source_bytes,
    validate_append_arguments,
)
from application.knowledge_validation import (
    KnowledgeValidationError,
    read_proposal_file,
    validate_proposal,
    validate_proposal_structure,
    parse_proposal_bytes,
    validation_exit_code,
)

_QUERY_CMDS = frozenset({"session-list", "session-read"})
_RAW_SESSION_CMDS = frozenset({"raw-session-append", "raw-session-read"})
_VALIDATE_CMDS = frozenset({"knowledge-validate"})

SCHEMA_VERSION = 1

def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _auto_sid():                                              # session-append without --session-id: generate a valid one
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M") + "-" + ("%04d" % (os.getpid() % 10000))

def die(**kw):
    kw["blocked"] = True
    raise SystemExit(json.dumps(kw))

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

def _validate_client(client):
    if not valid_id(client):
        die(reason="invalid client", value=client)

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

_DOC_TYPE = {"state": "system_state", "log": "session_log", "journal": "journal", "conflict": "conflict"}

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

_LEASE_TTL_S = 30  # advisory-lease TTL for the read->compute->persist critical
                   # section (D-006). That section is milliseconds — well under
                   # the TTL — so the lease is never held across I/O or a user
                   # turn and needs no renewal/heartbeat (panel refinement:
                   # "hold only for that key's persist critical section").


@contextlib.contextmanager
def _hold(backend, key):
    """Acquire this key's advisory lease and HOLD it across the caller's
    read->compute->persist critical section (D-006 / checklist #16), releasing
    it in a finally. LocalBackend.unlock is ownership-conditional (it releases
    only if OUR token still owns and the TTL has not elapsed), so an expired
    writer can never release a successor's lease. Raises BackendBusyError if the
    key is currently held by another writer; the caller treats that as one
    bounded retry, never an unbounded spin (checklist #11)."""
    lease = backend.lock(key, ttl_s=_LEASE_TTL_S)
    try:
        yield lease
    finally:
        try:
            backend.unlock(lease)
        except Exception:
            pass


def _persist(backend, key, kind, raw_bytes, expected_hash, lease=None):
    """Single write door (store.gate.persist) with the required OperationContext.

    - expected_hash is None -> CreateOnly: a create physically cannot clobber, so
      no lease is needed (D-005; C2 first-writer-wins).
    - expected_hash set + `lease` given -> Overwrite carrying the HELD lease
      (D-006 / checklist #16): the runtime caller acquired the lease, read UNDER
      it, and derived expected_hash from that under-lease read, so the backend
      enforces the monotonic fence (token + fence) against a lease that was live
      BEFORE the read — this closes the stale-writer-with-matching-hash hole that
      the old acquire-then-unlock-then-write pattern left open.
    - expected_hash set + lease None -> legacy acquire-and-release compatibility
      for direct callers/tests only. The runtime skill paths
      (flush_state/flush_log/session_append) always pass a held lease now.

    `lease` is a trailing keyword arg so existing positional 5-arg calls keep
    working; monkeypatch stubs must accept it (tests/test_h1_conflict.py)."""
    if expected_hash is None:
        ctx = create_ctx()
    else:
        if lease is None:
            lease = backend.lock(key, ttl_s=_LEASE_TTL_S)
            backend.unlock(lease)
        ctx = overwrite_ctx(expected_hash, lease)
    return gate.persist(backend, key, raw_bytes, ctx=ctx, doc_type=_DOC_TYPE[kind])

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

def init(store, project, client="cowork"):
    _validate_project(project)
    _validate_client(client)
    backend = _backend(store)
    skey, lkey = _key(project, "state"), _key(project, "log")
    if backend.read(skey) is None:
        body = "## Architecture\n(tbd)\n\n## Path & Variable Directory\n(tbd)\n\n## Hard Constraints\n(tbd)"
        fm = {"schema_version": SCHEMA_VERSION, "project_id": project, "client": client, "revision": 1, "updated": now()}
        _require_ok(_persist(backend, skey, "state", _bytes(fm, body), None))
    if backend.read(lkey) is None:
        fm = {"schema_version": SCHEMA_VERSION, "project_id": project, "client": client, "revision": 1, "updated": now()}
        _require_ok(_persist(backend, lkey, "log", _bytes(fm, "## Completed & Verified\n\n## Active State\n\n## Next Step\n"), None))
    return {"ok": True, "base": _data_dir(store, project)}

def _flush_doc(kind, store, project, new_content, expect_hash=None, section=None, session=None, client="cowork"):
    """Shared bounded_cas_reread_reapply implementation for flush_state/flush_log.

    section=None: whole-document replace, unchanged legacy semantics, EXCEPT
        that a CAS conflict now parks the losing body + records it instead of
        silently dying with reason="stale" (single attempt only — never
        blindly resubmits the whole document on STALE/EXISTS).
    section=<heading>: replace only that '## <heading>' block. Up to
        _MAX_CAS_ATTEMPTS attempts: on STALE/EXISTS, re-read the fresh body;
        if the target section is unchanged from what this writer first saw
        (or is absent both times), reapply and retry; if it changed, or the
        heading is duplicated/unparseable, park + fail closed immediately.
    """
    _validate_project(project)
    _validate_client(client)
    backend = _backend(store)
    key = _key(project, kind)
    sess = session if (session and valid_id(str(session))) else "unknown"

    # Validate the expected-hash SHAPE up front. A malformed hash (truncated,
    # uppercase, non-hex) is a caller input error, not a concurrency conflict:
    # fail loud as invalid_expect_hash BEFORE any read/reapply/park, so a typo
    # never produces a durable park write or pollutes the conflict list.
    if expect_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", str(expect_hash)):
        die(reason="invalid_expect_hash", value=str(expect_hash))

    # Deferral A (SAT-1158): a section BODY line that looks like a level-2
    # heading ("## ...") would shift section boundaries on reparse, letting a
    # later writer mis-target a section or bleed content across sections.
    # Increment 1 flattened the section NAME only; here we fail closed on
    # body-level heading-injection for SECTION-scoped writes (a leaf section's
    # content is not itself sectioned). Whole-doc writes are the caller's own
    # full structure (intentional headings), so they are exempt. Fails loud
    # BEFORE any read/lock/park so a bad body never produces a durable write.
    # The section NAME is interpolated into "## {name}" by _replace_section:
    # a name carrying a line break would persist a second level-2 heading
    # (the very boundary ambiguity the body guard below prevents), and an
    # empty name is not a heading at all. Refuse both before any read/lock.
    if section is not None and (
        not isinstance(section, str) or not section.strip() or "\n" in section or "\r" in section
    ):
        die(reason="invalid_section_name", section=str(section).replace("\r", " ").replace("\n", " "))
    # Scan the body on NORMALIZED line boundaries: a bare CR is a line ending
    # for Markdown consumers (CommonMark: LF, CR or CRLF), so "x\r## y" would
    # otherwise slip past a LF-only `^` and land a second level-2 heading.
    if section is not None and _ANY_H2_RE.search(
        (new_content or "").replace("\r\n", "\n").replace("\r", "\n")
    ):
        die(reason="section_body_heading_injection", section=str(section))

    base_section = None
    base_hash = None
    base_established = False
    new_body = new_content
    res = None

    attempt = 0
    busy_waits = 0
    busy_deadline = time.monotonic() + _BUSY_WAIT_S
    while attempt < _MAX_CAS_ATTEMPTS:
        attempt += 1
        # D-006 / checklist #16: HOLD this key's lease across read->compute->
        # persist so the backend fence protects us from a stale writer whose
        # content-hash still matches. A fresh lease per attempt (a higher fence)
        # is the "re-acquire on retry" path. `park` defers any conflict-parking
        # until AFTER the lease is released, so only ONE key is ever locked at a
        # time (checklist #5 — no nested locks, no self-deadlock).
        park = None
        try:
            with _hold(backend, key) as lease:
                blob = backend.read(key)
                if blob is not None:
                    fm, cur_body = parse(blob.body.decode("utf-8"))
                    if not fm:
                        die(reason=f"malformed {kind} frontmatter")
                    cur_hash = blob.version_hash
                else:
                    fm, cur_body, cur_hash = None, "", None

                if blob is not None and not expect_hash and attempt == 1:
                    die(reason=("expect_hash required for log update" if kind == "log"
                                else "expect_hash required for update"), current_hash=cur_hash)

                if section is not None:
                    try:
                        got = _get_section(cur_body, section)
                    except ValueError:
                        park = (new_content, section, base_hash, cur_hash, "conflict_parked")
                        break
                    cur_section = got[2] if got else None

                    if not base_established:
                        # F1 fix: the reapply baseline is valid ONLY against the
                        # caller's known version. expect_hash is None only on a
                        # first-ever create (no prior winner to clobber). If the
                        # store already advanced past expect_hash we never observed
                        # the caller's true section, so reapplying could silently
                        # overwrite whoever advanced it -> PARK.
                        if expect_hash is None or cur_hash == expect_hash:
                            base_section = cur_section
                            base_hash = cur_hash
                            base_established = True
                        else:
                            park = (new_content, section, expect_hash, cur_hash, "conflict_parked")
                            break
                    elif not _section_eq(cur_section, base_section):
                        park = (new_content, section, base_hash, cur_hash, "conflict_parked")
                        break

                    try:
                        new_body = _replace_section(cur_body, section, new_content)
                    except ValueError:
                        park = (new_content, section, base_hash, cur_hash, "conflict_parked")
                        break

                    persist_expect = cur_hash   # always CAS against the version we built upon
                else:
                    # Whole-doc base is the caller's DECLARED version (what the
                    # losing body was written against), not the winner we happened
                    # to read — preserves conflict lineage so base != current.
                    base_hash = expect_hash
                    new_body = new_content
                    persist_expect = expect_hash if blob is not None else None

                if blob is not None:
                    out_fm = dict(fm)
                    out_fm["revision"] = int(fm.get("revision", 0)) + 1
                    out_fm["updated"] = now()
                else:
                    out_fm = {"schema_version": SCHEMA_VERSION, "project_id": project}
                    out_fm["revision"] = 1
                    out_fm["updated"] = now()
                # Declared latest writer, not authenticated identity. A failed
                # persist leaves the winner's stored metadata untouched.
                out_fm["client"] = client

                # CreateOnly (persist_expect is None) needs no lease; a fenced
                # Overwrite carries the HELD lease so write() enforces the fence.
                res = _persist(backend, key, kind, _bytes(out_fm, new_body),
                               persist_expect,
                               lease=(lease if persist_expect is not None else None))
        except BackendBusyError:
            # Another writer holds this key's lease across ITS read->compute->
            # persist section. Waiting it out is not a CAS conflict, so it
            # must not consume the _MAX_CAS_ATTEMPTS reapply budget: five
            # 10-50ms backoffs (< 1s in total) are shorter than one fsync-heavy
            # critical section on a slow disk, which parked perfectly good
            # writes as "conflict_retry_exhausted". Bounded by wall-clock
            # instead (_BUSY_WAIT_S, never an unbounded spin -- checklist
            # #11); the backoff multiplier is capped.
            if time.monotonic() >= busy_deadline:
                break
            busy_waits += 1
            attempt -= 1
            time.sleep(random.uniform(0.01, 0.05) * min(busy_waits, 10))
            continue

        # --- lease released; act on the outcome outside any critical section ---
        if isinstance(res, OK):
            return {"ok": True, "revision": out_fm["revision"], "version_hash": res.new_hash}

        if isinstance(res, ERROR):
            if res.kind == ErrorKind.SECRET_BLOCKED:
                die(reasons=list(res.labels))
            die(reason="write_error", kind=res.kind.value)

        # STALE or EXISTS from here on.
        if section is None:
            # Whole-doc mode: exactly one CAS attempt, never blindly resubmit.
            _park_conflict(backend, store, project, sess, new_content, None,
                            base_hash, getattr(res, "current_hash", None),
                            reason="conflict_parked", doc_kind=kind)

        if attempt >= _MAX_CAS_ATTEMPTS:
            break
        time.sleep(random.uniform(0.01, 0.05) * attempt)

    # A section-mode conflict detected under the lease parks here, AFTER the
    # lease is released (one key at a time).
    if park is not None:
        losing_content, park_section, park_base, park_current, park_reason = park
        _park_conflict(backend, store, project, sess, losing_content, park_section,
                        park_base, park_current, reason=park_reason, doc_kind=kind)

    _park_conflict(backend, store, project, sess, new_content, section,
                    base_hash, getattr(res, "current_hash", None),
                    reason="conflict_retry_exhausted", doc_kind=kind)


def flush_state(store, project, new_body, expect_hash=None, section=None, session=None, client="cowork"):
    return _flush_doc("state", store, project, new_body, expect_hash=expect_hash,
                       section=section, session=session, client=client)

def flush_log(store, project, new_body, expect_hash=None, section=None, session=None, client="cowork"):
    return _flush_doc("log", store, project, new_body, expect_hash=expect_hash,
                       section=section, session=session, client=client)

def session_append(store, project, session_id, client, entry, operation_id=None):
    _validate_project(project)
    if not valid_id(session_id):
        die(reason="invalid session id", value=session_id)
    if not valid_id(client):
        die(reason="invalid client", value=client)
    if operation_id is not None and not valid_id(operation_id):
        die(reason="invalid operation id")
    # Scope is this project's session journal. Bind both text and declared writer;
    # the ledger and entry must be persisted as ONE document under the same lease.
    payload_hash = hashlib.sha256(json.dumps(
        {"client": client, "entry": entry}, sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    backend = _backend(store)
    jkey = _key(project, "journal", session_id)
    for attempt in range(1, 51):                              # bounded CAS retry: same-session concurrent appends
        # D-006 / checklist #16: read the journal and append UNDER a held lease
        # (a fresh lease per iteration = the re-acquire-on-retry path) so the
        # fence rejects a stale appender whose content-hash still matches.
        try:
            with _hold(backend, jkey) as lease:
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
                if operation_id is not None:
                    try:
                        operations = json.loads(fm.get("append_operations", "{}"))
                    except (TypeError, ValueError):
                        die(reason="invalid append operation ledger")
                    if (not isinstance(operations, dict)
                            or any(not valid_id(k) or not isinstance(v, str)
                                   or not re.fullmatch(r"[0-9a-f]{64}", v)
                                   for k, v in operations.items())):
                        die(reason="invalid append operation ledger")
                    previous = operations.get(operation_id)
                    if previous is not None:
                        if previous != payload_hash:
                            die(reason="operation id reused with different content")
                        return {"ok": True, "log": jkey,
                                "operation_id": operation_id, "duplicate": True,
                                "current_version_hash": blob.version_hash}
                    operations[operation_id] = payload_hash
                    fm["append_operations"] = json.dumps(operations, sort_keys=True, separators=(",", ":"))
                body = body + f"[{now()}] {entry}\n"
                # CreateOnly (expect is None) needs no lease; an Overwrite append
                # carries the HELD lease. The whole-doc version_hash stays the CAS
                # token: under the held lease the fence + hash re-check happen
                # atomically at write time, which already prevents a lost append,
                # so no separate tail-hash/sequence precondition is needed (see
                # decision note in the Phase-3 handoff).
                res = _persist(backend, jkey, "journal", _bytes(fm, body),
                               expect, lease=(lease if expect is not None else None))
        except BackendBusyError:
            # Another writer holds the journal lease across ITS read->append
            # critical section. lock() refuses immediately (never waits), so
            # back off briefly before retrying -- otherwise all 50 attempts
            # can burn through in a few milliseconds while the holder is
            # still inside its section, exhausting the budget spuriously.
            # Bounded (capped multiplier), same shape as _flush_doc's retry.
            time.sleep(random.uniform(0.01, 0.05) * min(attempt, 10))
            continue                                          # bounded retry
        if isinstance(res, OK):
            if operation_id is not None:
                return {"ok": True, "log": jkey, "operation_id": operation_id,
                        "duplicate": False, "current_version_hash": res.new_hash}
            return {"ok": True, "log": jkey}
        if isinstance(res, ERROR) and res.kind == ErrorKind.SECRET_BLOCKED:
            die(reasons=list(res.labels))
        if isinstance(res, (STALE, EXISTS)):
            continue                                          # concurrent append/create race — re-read and retry
        if isinstance(res, ERROR):
            die(reason="write_error", kind=res.kind.value)
    die(reason="append_retry_exhausted")

def _section(b, h, warnings=None, limit=400):
    """Read-only resume view of one '## <h>' section (issue #20).

    Uses the same line-anchored, CRLF-tolerant heading rules as the writer
    (_get_section): an exact level-2 heading ("### Sub" and "# Title" lines are
    content, not boundaries), and the section ends at the next level-2 heading.
    An empty section returns "" and never the next heading's text. A duplicated
    heading is ambiguous: return "" (never guess) and record a warning.
    """
    try:
        got = _get_section(b, h)
    except ValueError:
        if warnings is not None:
            warnings.append(f"duplicate heading: {h}")
        return ""
    if got is None:
        return ""
    content = got[2].replace("\r\n", "\n").strip()
    if limit is not None and len(content) > limit:
        if warnings is not None:
            warnings.append(f"truncated preview: {h}; read the full section before acting")
        return content[:limit]
    return content

# --- H1 increment 1: bounded_cas_reread_reapply conflict protocol -----------
# A "## <Heading>" line must be a real level-2 heading: "##" immediately
# followed by whitespace (so "### Sub" never matches — after the 2nd '#' a
# 3rd '#' is not whitespace), anchored to the start of a line so "##" text
# appearing mid-paragraph is never mistaken for a heading.
# CommonMark allows an ATX heading to be indented by up to three spaces, so
# the guard and _get_section agree on that too (a body line "   ## X" is a
# heading to every Markdown consumer and must be treated as one here).
_ANY_H2_RE = re.compile(r"(?m)^[ ]{0,3}##(?=[ \t]|\r?$)")


def _get_section(body, heading):
    """Locate the '## <heading>' block in body (CRLF-tolerant).

    Returns (start, end, content) where content is everything between the
    heading line and the next level-2 heading (or end of doc), or None if
    the heading is absent. Raises ValueError if the heading appears more
    than once (ambiguous — caller must not guess which one to touch).
    The heading match tolerates a trailing CR (CRLF docs) so it never fails
    to find an existing heading and blindly appends a duplicate.
    """
    hre = re.compile(rf"(?m)^[ ]{{0,3}}##[ \t]+{re.escape(heading)}[ \t]*\r?$")
    matches = list(hre.finditer(body))
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(f"duplicate heading: {heading!r}")
    m = matches[0]
    start = m.start()
    content_start = m.end() + 1 if m.end() < len(body) and body[m.end()] == "\n" else m.end()
    nxt = _ANY_H2_RE.search(body, content_start)
    end = nxt.start() if nxt else len(body)
    return start, end, body[content_start:end]


def _replace_section(body, heading, new_content):
    """Return body with the '## <heading>' section's content replaced by
    new_content (appended as a new section at the end if the heading is
    absent). Raises ValueError if the heading is duplicated in body."""
    new_content = (new_content or "").strip("\n")
    block = f"## {heading}\n{new_content}\n"
    got = _get_section(body, heading)  # raises ValueError on duplicate
    if got is None:
        if not body.strip():
            return block
        sep = "\n" if body.endswith("\n") else "\n\n"
        return body + sep + block
    start, end, _old = got
    return body[:start] + block + body[end:]


def _section_eq(a, b):
    """Compare section contents ignoring trailing separator newlines, but keep an
    absent section (None) distinct from an empty one (""). A competing edit to a
    different section can add/remove a trailing newline on the last section's
    content without the target text actually changing — that must not false-park.
    Strips trailing CR as well as LF: a CRLF-stored doc can leave the baseline
    ending in a CR while dump() rewrote the current doc without one, so stripping
    only '\\n' would compare 'old\\r' with 'old' and wrongly park."""
    if (a is None) != (b is None):
        return False
    if a is None:
        return True
    return a.rstrip("\r\n") == b.rstrip("\r\n")


_MAX_CAS_ATTEMPTS = 5
# Total wall-clock a writer will wait for a competitor's held lease before
# giving up (parking as retry-exhausted). A held section is milliseconds to
# a second even on a slow disk; the advisory TTL (_LEASE_TTL_S) is the hard
# ceiling behind it.
_BUSY_WAIT_S = 5.0


def _park_conflict(backend, store, project, session, losing_content, section,
                    base_hash, current_hash, reason, doc_kind=None):
    """Never drop a losing writer's work: park it verbatim under
    {project}/conflicts/..., record a hash/key-only note in the session
    journal, then fail closed. Does not return."""
    raw = (losing_content or "").encode("utf-8")
    shorthash = hashlib.sha256(raw).hexdigest()[:12]
    # Gate-safe hex digest of the session id, never the raw id: a valid but
    # random-looking session id embedded in the key can trip the gate's
    # high-entropy KEY scanner (SECRET_BLOCKED) and wrongly fail the park even
    # for a clean body. Pure hex is exempt from the entropy heuristic.
    sess_label = hashlib.sha256((session or "unknown").encode("utf-8")).hexdigest()[:8]

    conflict_key = None
    for i in range(3):
        # Key segments kept short (<32 chars) and lowercase so the gate's
        # high-entropy KEY scanner never mistakes a conflict key (which embeds
        # a hash) for a secret and blocks the park — that would be silent loss.
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        candidate = f"{project}/conflicts/{ts}/{sess_label}-{shorthash}" + (f"-{i}" if i else "")
        cres = _persist(backend, candidate, "conflict", raw, None)
        if isinstance(cres, OK):
            conflict_key = candidate
            break
        # A secret inside the losing body must NOT be stored: fail loud and
        # distinctly (never a silent overwrite, never a generic park_failed).
        if isinstance(cres, ERROR) and cres.kind == ErrorKind.SECRET_BLOCKED:
            die(reason="conflict_body_secret_blocked", reasons=list(cres.labels),
                base_hash=base_hash, current_hash=current_hash)
    if conflict_key is None:
        # Could not even park a copy — fail closed without pretending success.
        die(reason="conflict_park_failed", base_hash=base_hash, current_hash=current_hash)

    # Journal note carries keys, hashes and the gate-safe session label only —
    # never body bytes or the raw session id (a high-entropy id would make the
    # journal key itself gate-rejected). It is written to a FIXED "conflicts"
    # journal. The section name is caller text: flatten any CR/LF so it cannot
    # break the note line (it still passes through the secret gate on write).
    note = (f"conflict_parked key={conflict_key} sess={sess_label} "
            f"doc_kind={doc_kind or 'unknown'} "
            f"base_hash={base_hash or 'none'} current_hash={current_hash or 'none'}")
    if section:
        note += " section=" + str(section).replace("\r", " ").replace("\n", " ")
    journal_recorded = True
    try:
        session_append(store, project, "conflicts", "system", note)
    except SystemExit:
        journal_recorded = False  # surfaced in the die payload — never silently swallowed

    # Deferral B (SAT-1158): park-noise telemetry. Emit a dedicated durable
    # event so the dogfood soak can measure how often — and at what scope —
    # writers park, and decide whether a tighter whole-doc merge is warranted.
    # decision="park" is its own bucket (never "block"), so it does not pollute
    # the secret/concurrency block counters in evaluate(). Fail-open (never
    # blocks or alters the park). No body bytes — labels/keys/hashes only.
    _event(store, project, "park", "park",
           {"reason": reason, "doc_kind": doc_kind or "unknown",
            "scope": "whole_doc" if section is None else "section",
            "conflict_key": conflict_key})
    die(reason=reason, conflict_key=conflict_key, current_hash=current_hash,
        base_hash=base_hash, doc_kind=doc_kind, journal_recorded=journal_recorded)
# --- end H1 increment 1 helpers ---------------------------------------------

# --- Deferral C (SAT-1158): conflict resolve / tombstone lifecycle ----------
# Parked conflict keys accumulate monotonically; bootstrap() kept counting a key
# even after its edit was reviewed + reapplied, and a raw file delete is undone
# by LocalBackend's append-only journal replay. A "resolved" marker is therefore
# itself a durable gate.persist WRITE (survives replay) under a SEPARATE prefix
# {project}/conflict-resolved/ (which never matches the {project}/conflicts/
# listing prefix), named by a hex digest of the conflict key so the marker key
# is short + pure-hex (never entropy-flagged by the gate's KEY scanner).

def _resolved_marker_name(conflict_key):
    return hashlib.sha256(conflict_key.encode("utf-8")).hexdigest()[:32]

def _resolved_key(project, conflict_key):
    return f"{project}/conflict-resolved/{_resolved_marker_name(conflict_key)}"

def _is_resolved(backend, project, conflict_key):
    """A conflict counts as settled only if its marker exists AND reads back
    as a resolve_conflict() marker bound to this exact conflict key."""
    try:
        blob = backend.read(_resolved_key(project, conflict_key))
    except Exception:
        return False
    if blob is None:
        return False
    try:
        fm, _body = parse(blob.body.decode("utf-8"))
    except Exception:
        return False
    return bool(fm) and fm.get("conflict_key") == conflict_key and fm.get("project_id") == project

def _audit_store(store, project):
    """Out-of-band store audit (single gate, T-2/T-3). Every in-helper write is
    already gate-scanned, so this catches a BYPASS: a secret or non-text blob
    written straight to data/ (including unjournaled orphans that LocalBackend
    read/list refuse). Uses health_inspect's read-only published-file walk — not
    backend.list/read — and re-scans raw bytes through store.gate (content-based).
    Every file, including OS metadata names (.DS_Store, Thumbs.db, desktop.ini),
    is secret-scanned first; those names are exempt ONLY from the non-text/binary
    check. secret_scan is heuristic, not a guarantee. Walk budget, layout and
    journal uncertainty are reported as findings, not a silent all-clear."""
    findings = []
    entries, block_reasons, uncertainty_reasons = enumerate_project_published_files_for_audit(
        store, project
    )
    for code in block_reasons:
        findings.append({"key": project + "/", "reason": f"audit blocked: {code}"})
    for code in uncertainty_reasons:
        findings.append({"key": project + "/", "reason": f"store audit uncertain: {code}"})
    for key, raw in entries:
        if raw is None:
            findings.append({"key": key, "reason": "unreadable blob in store"})
            continue
        hits = gate.secret_scan(raw)
        if hits:
            findings.append({"key": key, "reasons": hits})
            continue
        if not gate._is_acceptable_text(raw) and not is_os_metadata_basename(key):
            findings.append({"key": key, "reason": "non-text/binary content in store"})
    return findings


def bootstrap(store, project):
    _validate_project(project)
    backend = _backend(store)
    findings = _audit_store(store, project)
    if findings:
        die(reason="store contains secrets - refusing to resume", findings=findings[:10])
    sblob = backend.read(_key(project, "state"))
    lblob = backend.read(_key(project, "log"))
    fm, _ = parse(sblob.body.decode("utf-8")) if sblob else ({}, "")
    lfm, lbody = parse(lblob.body.decode("utf-8")) if lblob else ({}, "")
    section_warnings = []
    active_full = _section(lbody, "Active State", section_warnings, limit=None)
    next_full = _section(lbody, "Next Step", section_warnings, limit=None)
    active, nxt = active_full[:400], next_full[:400]
    truncated_fields = []
    for field, heading, full in (("active", "Active State", active_full),
                                 ("next", "Next Step", next_full)):
        if len(full) > 400:
            truncated_fields.append(field)
            section_warnings.append(
                f"truncated preview: {heading}; read {field}_full before acting")
    vh = sblob.version_hash if sblob else None
    lh = lblob.version_hash if lblob else None
    rev = int(fm["revision"]) if fm.get("revision") else None
    # Surface parked conflicts so a resuming session cannot silently miss a
    # losing writer's work. Deferral C: subtract those explicitly RESOLVED (a
    # durable marker under {project}/conflict-resolved/ that survives journal
    # replay), so conflict_count now means OUTSTANDING (unreviewed) conflicts —
    # a reviewed+reapplied key stops being counted. Each key under
    # {project}/conflicts/ is one parked body; ASCII marker only (no emoji).
    parked = list(backend.list(project + "/conflicts/"))
    conflicts = [k for k in parked if not _is_resolved(backend, project, k)]
    conflict_count = len(conflicts)
    settled_count = len(parked) - conflict_count
    resume = f"resuming: {active or '(none)'} / next: {nxt or '(none)'} / v{(vh or '?')[:12]}"
    if conflict_count:
        resume += (f"  [!] {conflict_count} parked conflict(s) - "
                   f"review {project}/conflicts/")
    if section_warnings:                                    # ambiguous resume sections: say so, never guess
        resume += "  [!] " + "; ".join(section_warnings)
    out = {"version_hash": vh, "revision": rev, "log_hash": lh,
           "state_client": fm.get("client"), "log_client": lfm.get("client"),
           "client_labels_verified": False,
           "active": active, "next": nxt,
           "active_full": active_full, "next_full": next_full,
           "truncated_fields": truncated_fields,
           "conflicts": conflicts, "conflict_count": conflict_count,
           "settled_count": settled_count,
           "resume_line": resume}
    if section_warnings:
        out["section_warnings"] = section_warnings
    return out

def resolve_conflict(store, project, conflict_key):
    """Deferral C: mark a parked conflict RESOLVED with a durable, append-only
    marker that SURVIVES LocalBackend journal replay (a raw file delete would be
    undone by replay; a gate.persist write is not). After this, bootstrap()
    counts the conflict as settled, not outstanding. Idempotent: resolving an
    already-resolved key is a no-op OK (CreateOnly -> EXISTS)."""
    _validate_project(project)
    backend = _backend(store)
    prefix = project + "/conflicts/"
    if not isinstance(conflict_key, str) or not conflict_key.startswith(prefix):
        die(reason="invalid_conflict_key", value=str(conflict_key))
    # The marker is named from the key's spelling, and bootstrap() matches
    # markers against the CANONICAL listing -- so the supplied key must be
    # exactly one of the listed parked keys. A non-canonical alias (".."
    # segments, doubled separators) can read the same file through the
    # backend but would mint a marker bootstrap() never matches.
    if not gate._valid_key_shape(conflict_key) or conflict_key not in set(backend.list(prefix)):
        die(reason="unknown_conflict_key", value=conflict_key)
    marker_key = _resolved_key(project, conflict_key)
    # The marker names the FULL conflict key it resolves; bootstrap() reads it
    # back and honours it only when that field matches the parked key exactly,
    # so a marker-shaped value that merely has the right digest name (planted
    # or written out of band) never silently settles a conflict.
    fm = {"schema_version": SCHEMA_VERSION, "project_id": project, "resolved_at": now(),
          "conflict_key": conflict_key}
    # CreateOnly: first resolve wins; a repeat is EXISTS -> already resolved (OK).
    res = _persist(backend, marker_key, "conflict", _bytes(fm, "resolved"), None)
    if isinstance(res, EXISTS):
        # A value already sits at this name. It is an idempotent success ONLY
        # if it is a marker bound to this exact conflict. Anything else is a
        # NAMESPACE COLLISION: {project}/conflict-resolved/ is not reserved
        # by the gate and was legal user storage, so the existing bytes are
        # never replaced -- fail closed and leave the conflict outstanding.
        if _is_resolved(backend, project, conflict_key):
            return {"ok": True, "resolved": conflict_key, "marker": marker_key}
        die(reason="conflict_marker_collision", marker=marker_key, conflict_key=conflict_key)
    if isinstance(res, OK):
        return {"ok": True, "resolved": conflict_key, "marker": marker_key}
    if isinstance(res, ERROR) and res.kind == ErrorKind.SECRET_BLOCKED:
        die(reasons=list(res.labels))
    die(reason="resolve_failed",
        kind=(res.kind.value if isinstance(res, ERROR) else type(res).__name__))

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

def _addressing_note():
    return "project selects store namespace only; not authorization or tenancy"

def session_list(store, project, limit=DEFAULT_LIST_LIMIT, after=None):
    """CLI adapter: bounded session id list (StoreBackend list/read only).

    Opens LocalBackend (constructor side effects apply); always closes it before return.
    Injected backends used by application helpers remain caller-owned.
    """
    validate_list_arguments(project, limit=limit, after=after)
    backend = _backend(store)
    try:
        listed = query_list_sessions(backend, project, limit=limit, after=after)
        out = {
            "ok": True,
            "addressing": _addressing_note(),
            "session_ids": list(listed.session_ids),
            "truncated": listed.truncated,
        }
        if listed.next_after is not None:
            out["next_after"] = listed.next_after
        return out
    finally:
        backend.close()

def raw_session_append(store, project, session_id, client, operation_id, source: bytes):
    validate_append_arguments(project, session_id, client, operation_id, source)
    backend = _backend(store)
    try:
        try:
            receipt = app_append_raw_session(
                backend, project, session_id, client, operation_id, source
            )
        except RawSessionError as e:
            if e.reason == "secret_blocked":
                die(reasons=list(e.detail.get("labels", ())))
            if e.reason == "operation_id_conflict":
                die(reason="operation id reused with different content")
            if e.reason == "outcome_uncertain":
                die(reason="outcome_uncertain", kind=e.detail.get("kind"))
            if e.reason in ("invalid_project_id", "invalid_session_id", "invalid_client", "invalid_operation_id"):
                die(reason=e.reason.replace("_", " "), **{k: v for k, v in e.detail.items()})
            if e.reason == "source_too_large":
                die(reason="source too large")
            if e.reason == "source_file_error":
                die(reason="source_file_error", error=e.detail.get("error"))
            if e.reason in ("source_rejected", "envelope_rejected"):
                die(reason="invalid source", kind=e.detail.get("kind"))
            if e.reason == "append_retry_exhausted":
                die(reason="append_retry_exhausted")
            die(reason=e.reason, **e.detail)
        return {
            "ok": True,
            "operation_id": receipt.operation_id,
            "source_sha256": receipt.source_sha256,
            "source_length": receipt.source_length,
            "captured": receipt.captured,
            "record_id": receipt.record_id,
            "current_version_hash": receipt.current_version_hash,
            "duplicate": receipt.duplicate,
        }
    finally:
        backend.close()


def knowledge_validate(store, project, proposal_path):
    """CLI adapter: validate a source-linked proposal (read-only store access)."""
    if not project:
        raise KnowledgeValidationError("missing_project")
    if not valid_id(project):
        raise KnowledgeValidationError("invalid_project_id")
    raw = read_proposal_file(proposal_path)
    obj = parse_proposal_bytes(raw)
    validate_proposal_structure(obj)
    backend = _backend(store)
    try:
        return validate_proposal(backend, project, raw)
    finally:
        backend.close()


def _knowledge_validate_fail(exc: KnowledgeValidationError) -> None:
    print(json.dumps(exc.to_payload()))
    sys.exit(validation_exit_code(exc))


def raw_session_read(store, project, session_id, operation_id):
    _validate_project(project)
    if not valid_id(session_id):
        die(reason="invalid session id", value=session_id)
    if not valid_id(operation_id):
        die(reason="invalid operation id")
    backend = _backend(store)
    try:
        try:
            got = app_read_raw_session(backend, project, session_id, operation_id)
        except RawSessionError as e:
            if e.reason == "secret_blocked":
                die(reasons=list(e.detail.get("labels", ())))
            _raw_session_fail(e)
        out = {
            "ok": True,
            "found": got.found,
            "operation_id": got.operation_id,
        }
        if got.found:
            out["source_sha256"] = got.source_sha256
            out["source_length"] = got.source_length
            out["captured"] = got.captured
            out["record_id"] = got.record_id
            out["current_version_hash"] = got.current_version_hash
            out["source_base64"] = got.source_base64
        return out
    finally:
        backend.close()


def _raw_session_fail(exc: RawSessionError) -> None:
    payload = {"blocked": True, "reason": exc.reason}
    payload.update(exc.detail)
    print(json.dumps(payload))
    validation = exc.reason.startswith("invalid") or exc.reason in (
        "source_too_large",
        "missing_session_id",
        "missing_operation_id",
        "missing_source_file",
        "source_file_error",
    )
    sys.exit(2 if validation else 1)


def session_read(store, project, session_id):
    """CLI adapter: read one session journal blob (bytes + hash; no summarization).

    Opens LocalBackend (constructor side effects apply); always closes it before return.
    """
    session_store_key(project, session_id)
    backend = _backend(store)
    try:
        got = query_read_session(backend, project, session_id)
        out = {
            "ok": True,
            "addressing": _addressing_note(),
            "found": got.found,
            "session_id": got.session_id,
        }
        if got.found:
            out["content_hash"] = got.content_hash
            out["body_base64"] = got.body_base64
            if got.body_utf8 is not None:
                out["body_utf8"] = got.body_utf8
        return out
    finally:
        backend.close()

def _query_json_fail(reason, code=2, **detail):
    payload = {"blocked": True, "reason": reason}
    payload.update(detail)
    print(json.dumps(payload))
    sys.exit(code)

def _query_exit_from_session_error(exc: SessionQueryError) -> None:
    payload = {"blocked": True, "reason": exc.reason}
    payload.update(exc.detail)
    print(json.dumps(payload))
    validation = exc.reason.startswith("invalid") or exc.reason in (
        "missing_session_id",
        "missing_project",
    )
    sys.exit(2 if validation else 1)

def _coerce_query_list_limit(raw):
    if raw is None:
        return DEFAULT_LIST_LIMIT
    if isinstance(raw, bool):
        raise SessionQueryError("invalid_limit", limit=raw, max_limit=MAX_LIST_LIMIT)
    if type(raw) is int:
        value = raw
    else:
        text = str(raw).strip()
        if not re.fullmatch(r"[0-9]+", text):
            raise SessionQueryError("invalid_limit", limit=raw, max_limit=MAX_LIST_LIMIT)
        try:
            value = int(text)
        except ValueError:
            raise SessionQueryError("invalid_limit", limit=raw, max_limit=MAX_LIST_LIMIT) from None
    if type(value) is not int:
        raise SessionQueryError("invalid_limit", limit=raw, max_limit=MAX_LIST_LIMIT)
    return value

def _run_query_command(ns):
    """Validate parsed query arguments before opening LocalBackend; emit JSON."""
    if not ns.project:
        _query_json_fail("missing_project")
    if not valid_id(ns.project):
        _query_json_fail("invalid_project_id", value=ns.project)
    store = ns.store or default_store_root()
    try:
        if ns.cmd == "session-read":
            if not ns.session_id:
                _query_json_fail("missing_session_id")
            if ns.list_limit is not None or ns.after is not None:
                _query_json_fail("invalid_argument", message="list options require session-list")
            result = session_read(store, ns.project, ns.session_id)
        else:
            if ns.session_id is not None:
                _query_json_fail("invalid_argument", message="--session-id requires session-read")
            limit = _coerce_query_list_limit(ns.list_limit)
            if ns.after is not None and not valid_id(ns.after):
                _query_json_fail("invalid_after", value=ns.after)
            result = session_list(store, ns.project, limit=limit, after=ns.after)
    except SessionQueryError as exc:
        _query_exit_from_session_error(exc)
    except Exception as exc:
        _query_json_fail("internal_error", code=1, error=type(exc).__name__)
    print(json.dumps(result))

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
    park = [r for r in rows if r.get("op") == "park"]          # Deferral B: park-noise
    sc = {
        "events": len(rows),
        "writes_allowed": len(allow),
        "blocks_total": len(block),
        "parks_total": len(park),
        "whole_doc_parks": sum(1 for r in park if r.get("scope") == "whole_doc"),
        "section_parks": sum(1 for r in park if r.get("scope") == "section"),
        "secret_blocks": sum(1 for r in block if _has(r, "key") or _has(r, "secret")
                             or _has(r, "token") or _has(r, "private key") or _has(r, "URI")),
        "concurrency_blocks": sum(1 for r in block if _has(r, "stale") or _has(r, "lock_timeout")
                                  or _has(r, "BUSY") or _has(r, "conflict")),
        "bootstrap_refusals": sum(1 for r in boot if r.get("decision") == "block"),
        "clean_resumes": sum(1 for r in boot if r.get("decision") == "allow"),
        "errors": len(error),
        "note": "leaks past the gate are NOT measurable here - run the Layer-2 audit scan",
    }
    return sc

def _valid_telemetry_event(obj) -> bool:
    if not isinstance(obj, dict):
        return False
    for key in ("decision", "op", "scope", "reason", "ts", "project"):
        val = obj.get(key)
        if val is not None and not isinstance(val, str):
            return False
    rs = obj.get("reasons")
    if rs is not None:
        if not isinstance(rs, list) or not all(isinstance(x, str) for x in rs):
            return False
    findings = obj.get("findings")
    if findings is not None:
        if not isinstance(findings, list):
            return False
        for f in findings:
            if f is None or not isinstance(f, dict):
                return False
            r = f.get("reason")
            if r is not None and not isinstance(r, str):
                return False
    return True


def _scorecard_from_events_raw(raw: bytes, cutoff=None):
    """Parse telemetry bytes into the Layer-3 scorecard (health-only safe reader)."""
    try:
        rows = []
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, ["telemetry_unreadable"], 0
        for ln in text.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                obj = json.loads(ln)
            except Exception:
                return None, ["telemetry_unreadable"], 0
            if not _valid_telemetry_event(obj):
                return None, ["telemetry_unreadable"], 0
            rows.append(obj)

        def _has(r, label):
            rs = r.get("reasons") or []
            fs = []
            for f in (r.get("findings") or []):
                if isinstance(f, dict):
                    fs.append(f.get("reason", ""))
            return any(label in str(x) for x in rs + fs) or label in str(r.get("reason", ""))

        allow = [r for r in rows if r.get("decision") == "allow"]
        block = [r for r in rows if r.get("decision") == "block"]
        error = [r for r in rows if r.get("decision") == "error"]
        boot = [r for r in rows if r.get("op") == "bootstrap"]
        park = [r for r in rows if r.get("op") == "park"]
        sc = {
            "events": len(rows),
            "writes_allowed": len(allow),
            "blocks_total": len(block),
            "parks_total": len(park),
            "whole_doc_parks": sum(1 for r in park if r.get("scope") == "whole_doc"),
            "section_parks": sum(1 for r in park if r.get("scope") == "section"),
            "secret_blocks": sum(1 for r in block if _has(r, "key") or _has(r, "secret")
                                 or _has(r, "token") or _has(r, "private key") or _has(r, "URI")),
            "concurrency_blocks": sum(1 for r in block if _has(r, "stale") or _has(r, "lock_timeout")
                                      or _has(r, "BUSY") or _has(r, "conflict")),
            "bootstrap_refusals": sum(1 for r in boot if r.get("decision") == "block"),
            "clean_resumes": sum(1 for r in boot if r.get("decision") == "allow"),
            "errors": len(error),
            "note": "leaks past the gate are NOT measurable here - run the Layer-2 audit scan",
        }
        if cutoff is not None:
            recent = 0
            for r in error:
                try:
                    ts = datetime.datetime.strptime(
                        r.get("ts", ""), "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=datetime.timezone.utc)
                    if ts >= cutoff:
                        recent += 1
                except Exception:
                    return None, ["telemetry_unreadable"], 0
            return sc, [], recent
        return sc, [], 0
    except (TypeError, AttributeError, ValueError):
        return None, ["telemetry_unreadable"], 0

def _empty_scorecard():
    return {
        "events": 0,
        "writes_allowed": 0,
        "blocks_total": 0,
        "parks_total": 0,
        "whole_doc_parks": 0,
        "section_parks": 0,
        "secret_blocks": 0,
        "concurrency_blocks": 0,
        "bootstrap_refusals": 0,
        "clean_resumes": 0,
        "errors": 0,
        "note": "leaks past the gate are NOT measurable here - run the Layer-2 audit scan",
    }

def health(store, window_hours=24):
    """One-shot health verdict: scorecard + independent audit + per-project freshness.
    Verdict reflects RECENT activity (default 24h): 'attention' (exit 1) if the gate
    errored in the window or a leak is present now; else 'healthy'. Missing gitleaks is a note."""
    store_check = inspect_local_store(store)
    root, _root_reasons = resolve_store_root(store)
    aux_ok = store_aux_reads_allowed(store_check)
    telemetry_problems = []
    sc = _empty_scorecard()
    recent_errors = 0
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=window_hours)
    if aux_ok:
        raw, t_reasons = read_telemetry_bytes(store, root=root)
        telemetry_problems.extend(t_reasons)
        if raw is not None and not t_reasons:
            sc_parsed, parse_reasons, recent_errors = _scorecard_from_events_raw(raw, cutoff)
            telemetry_problems.extend(parse_reasons)
            if sc_parsed is not None:
                sc = sc_parsed
    elif root is not None:
        raw, t_reasons = read_telemetry_bytes(store, root=root)
        if t_reasons:
            telemetry_problems.extend(t_reasons)
        elif raw is not None:
            _, parse_reasons, _recent = _scorecard_from_events_raw(raw)
            telemetry_problems.extend(parse_reasons)

    leaks = None
    scanner = "unavailable"
    audit_problems: list = []
    if aux_ok:
        try:
            sys.path.insert(0, os.path.join(_ROOT, "tools"))
            import knokeep_audit
            audit_dir, layout_reasons = safe_data_dir_for_audit(store, root=root)
            for code in layout_reasons:
                audit_problems.append(code)
            if audit_dir and not layout_reasons:
                a = knokeep_audit.audit(audit_dir)
                scanner = a.get("scanner") or "unavailable"
                if scanner in (None, "gitleaks:none") or str(scanner).endswith(":none"):
                    leaks = None
                    scanner = "unavailable"
                else:
                    leaks = a.get("leaks")
        except Exception:
            pass
    projects = read_project_summaries(store, root=root) if aux_ok else []
    problems, notes = [], []
    for code in store_check.get("reasons") or []:
        problems.append("store:" + code)
    if store_check.get("indeterminate") and not any(
        p == "store:inspect_indeterminate" for p in problems
    ):
        problems.append("store:inspect_indeterminate")
    for code in telemetry_problems:
        problems.append("telemetry:" + code)
    for code in audit_problems:
        problems.append("audit:" + code)
    if store_check.get("indeterminate"):
        notes.append(
            "store_inspect_indeterminate (journal changed during read; not a linearizability guarantee)"
        )
    if recent_errors > 0: problems.append("errors_last_%dh=%d" % (window_hours, recent_errors))
    if leaks: problems.append("leaks>0")
    if sc.get("errors", 0) > 0 and recent_errors == 0:
        notes.append("%d historical error(s) outside the %dh window (resolved)" % (sc.get("errors", 0), window_hours))
    if leaks is None: notes.append("audit_unavailable (gitleaks not found)")
    verdict = "healthy" if not problems else "attention"
    return {"verdict": verdict, "window_hours": window_hours, "recent_errors": recent_errors,
            "problems": problems, "notes": notes, "store": store_check,
            "scorecard": sc, "audit": {"scanner": scanner, "leaks": leaks}, "projects": projects}

def _bodyfile(path, option="--body-file"):
    if not path or not os.path.exists(path):
        die(reason=f"missing {option}", path=path)         # empty file is allowed; missing is an error
    return _readfile(path)

def _entry(a):
    if a.entry_file is None:
        return a.entry or ""
    entry = sys.stdin.read().rstrip("\n") if a.entry_file == "-" else _bodyfile(a.entry_file, "--entry-file")
    if not entry.strip():
        die(reason='empty --entry-file input; use --entry "" for an intentional empty entry')
    return entry

def main():
    class CommandParser(argparse.ArgumentParser):
        def error(self, message):
            # On malformed query invocations no namespace is available yet.
            # Detect the requested command while skipping known option values.
            takes_value = {"--store", "--project", "--session-id", "--client",
                           "--list-limit", "--after", "--operation-id", "--body-file",
                           "--entry", "--entry-file", "--expect-hash", "--section",
                           "--conflict-key", "--source-file", "--proposal-file"}
            args = iter(sys.argv[1:])
            for arg in args:
                if arg.startswith("--"):
                    # argparse accepts unambiguous long-option abbreviations.
                    # Their following values are not command names either.
                    matches = [option for option in takes_value if option.startswith(arg)]
                    if len(matches) == 1:
                        next(args, None)
                elif arg in {"init", "flush-state", "flush-log", "session-append",
                             "raw-session-append", "raw-session-read",
                             "bootstrap", "rollup", "resolve", "eval", "health"}:
                    break
                elif arg in _RAW_SESSION_CMDS:
                    _raw_session_fail(RawSessionError("invalid_argument", message=message))
                elif arg in _QUERY_CMDS:
                    _query_json_fail("invalid_argument", message=message)
                elif arg in _VALIDATE_CMDS:
                    _knowledge_validate_fail(KnowledgeValidationError("invalid_argument"))
            super().error(message)
    ap = CommandParser()
    ap.add_argument("cmd", choices=["init", "flush-state", "flush-log", "session-append",
                                    "session-list", "session-read", "raw-session-append",
                                    "raw-session-read", "knowledge-validate", "bootstrap",
                                    "rollup", "resolve", "eval", "health"])
    ap.add_argument("--store"); ap.add_argument("--project")   # --store optional: defaults to the shared cross-tool root
    ap.add_argument("--session-id"); ap.add_argument("--client", default="cowork")
    ap.add_argument("--list-limit")
    ap.add_argument("--after")
    ap.add_argument("--operation-id")
    ap.add_argument("--source-file")
    ap.add_argument("--proposal-file")
    ap.add_argument("--body-file"); ap.add_argument("--entry"); ap.add_argument("--entry-file"); ap.add_argument("--expect-hash"); ap.add_argument("--section"); ap.add_argument("--conflict-key")
    a = ap.parse_args()
    if a.proposal_file is not None and a.cmd != "knowledge-validate":
        if a.cmd in _QUERY_CMDS:
            _query_json_fail("invalid_argument", message="--proposal-file is not supported by queries")
        if a.cmd in _RAW_SESSION_CMDS:
            _raw_session_fail(RawSessionError("invalid_argument", message="write options are not supported"))
        ap.error("--proposal-file is only supported by knowledge-validate")
    if a.cmd == "knowledge-validate":
        if a.proposal_file is None:
            _knowledge_validate_fail(KnowledgeValidationError("missing_proposal_file"))
        if any(arg == "--client" or arg.startswith("--client=") for arg in sys.argv[1:]):
            _knowledge_validate_fail(KnowledgeValidationError("invalid_argument"))
        if any(
            getattr(a, name) is not None
            for name in (
                "session_id",
                "operation_id",
                "source_file",
                "body_file",
                "entry",
                "entry_file",
                "expect_hash",
                "section",
                "conflict_key",
                "list_limit",
                "after",
            )
        ):
            _knowledge_validate_fail(KnowledgeValidationError("invalid_argument"))
        if not a.store:
            a.store = default_store_root()
        try:
            result = knowledge_validate(a.store, a.project, a.proposal_file)
        except KnowledgeValidationError as exc:
            _knowledge_validate_fail(exc)
        except Exception:
            print(
                json.dumps(
                    {"blocked": True, "reason": "internal_error"}
                )
            )
            sys.exit(1)
        print(json.dumps(result))
        return
    if a.cmd == "raw-session-read":
        if not a.store:
            a.store = default_store_root()
        if not a.project:
            _raw_session_fail(RawSessionError("missing_project"))
        if not a.session_id:
            _raw_session_fail(RawSessionError("missing_session_id"))
        if not a.operation_id:
            _raw_session_fail(RawSessionError("missing_operation_id"))
        if any(getattr(a, name) is not None for name in
               ("body_file", "entry", "entry_file", "expect_hash", "section",
                "conflict_key", "source_file", "list_limit", "after")):
            _raw_session_fail(RawSessionError("invalid_argument", message="write options are not supported"))
        print(json.dumps(raw_session_read(a.store, a.project, a.session_id, a.operation_id)))
        return
    if a.cmd in _QUERY_CMDS:
        if a.source_file is not None:
            _query_json_fail("invalid_argument", message="--source-file is not supported by queries")
        if any(getattr(a, name) is not None for name in
               ("operation_id", "body_file", "entry", "entry_file", "expect_hash", "section", "conflict_key")):
            _query_json_fail("invalid_argument", message="write options are not supported by queries")
        _run_query_command(a)
        return
    if a.cmd not in _RAW_SESSION_CMDS and a.source_file is not None:
        ap.error("--source-file is only supported by raw-session-append")
    if a.operation_id is not None:
        if a.cmd not in ("session-append", "raw-session-append"):
            ap.error("--operation-id is only supported by session-append, raw-session-append, or raw-session-read")
        if not a.session_id:
            ap.error("--operation-id requires an explicit --session-id reused across retries")
    if a.cmd == "raw-session-append":
        if a.source_file is None:
            _raw_session_fail(RawSessionError("missing_source_file"))
        if not a.operation_id:
            _raw_session_fail(RawSessionError("missing_operation_id"))
        if not a.project:
            _raw_session_fail(RawSessionError("missing_project"))
        if not a.session_id:
            _raw_session_fail(RawSessionError("missing_session_id"))
        _validate_project(a.project)
        _validate_client(a.client)
        if not valid_id(a.session_id):
            die(reason="invalid session id", value=a.session_id)
        if not valid_id(a.operation_id):
            die(reason="invalid operation id")
        if any(getattr(a, name) is not None for name in
               ("body_file", "entry", "entry_file", "expect_hash", "section", "conflict_key",
                "list_limit", "after")):
            _raw_session_fail(RawSessionError("invalid_argument", message="unsupported options for raw-session-append"))
        if not a.store:
            a.store = default_store_root()
        try:
            src = read_source_bytes(a.source_file, sys.stdin.buffer)
        except RawSessionError as e:
            _raw_session_fail(e)
        try:
            validate_append_arguments(a.project, a.session_id, a.client, a.operation_id, src)
        except RawSessionError as e:
            _raw_session_fail(e)
        try:
            result = raw_session_append(a.store, a.project, a.session_id, a.client, a.operation_id, src)
        except SystemExit:
            raise
        except Exception as e:
            print(json.dumps({"blocked": True, "reason": "internal error", "error": type(e).__name__}))
            sys.exit(1)
        _event(a.store, a.project, a.cmd, "allow", {"ok": result.get("ok")})
        print(json.dumps(result))
        return
    if a.cmd == "session-append":
        if a.body_file is not None:
            ap.error("session-append does not accept --body-file; use --entry-file (or --entry-file - for stdin)")
        if (a.entry is None) == (a.entry_file is None):
            ap.error("session-append requires exactly one of --entry or --entry-file")
    if not a.store:
        a.store = default_store_root()                     # shared cross-tool default (T-4)
    if a.cmd == "eval":                                     # read-only scorecard, no project needed
        print(json.dumps(evaluate(a.store))); return
    if a.cmd == "health":                                   # read-only verdict; exit 1 on attention
        h = health(a.store); print(json.dumps(h, indent=1)); sys.exit(0 if h["verdict"] == "healthy" else 1)
    if not a.project:
        print(json.dumps({"blocked": True, "reason": "--project required"})); sys.exit(2)
    try:
        if a.cmd == "init": result = init(a.store, a.project, client=a.client)
        elif a.cmd == "flush-state": result = flush_state(a.store, a.project, _bodyfile(a.body_file), a.expect_hash, section=a.section, session=a.session_id, client=a.client)
        elif a.cmd == "flush-log": result = flush_log(a.store, a.project, _bodyfile(a.body_file), a.expect_hash, section=a.section, session=a.session_id, client=a.client)
        elif a.cmd == "session-append": result = session_append(a.store, a.project, a.session_id or _auto_sid(), a.client, _entry(a), operation_id=a.operation_id)
        elif a.cmd == "bootstrap": result = bootstrap(a.store, a.project)
        elif a.cmd == "rollup": result = rollup(a.store, a.project)
        elif a.cmd == "resolve": result = resolve_conflict(a.store, a.project, a.conflict_key)
    except SystemExit as e:
        if a.cmd not in _QUERY_CMDS:
            _event(a.store, a.project, a.cmd, "block", _labels(e.code))   # observe the block; never alter it
        raise
    except SessionQueryError as e:
        payload = {"blocked": True, "reason": e.reason}
        payload.update(e.detail)
        print(json.dumps(payload))
        sys.exit(2 if e.reason.startswith("invalid") else 1)
    except Exception as e:
        if a.cmd not in _QUERY_CMDS:
            _event(a.store, a.project, a.cmd, "error", {"error": type(e).__name__})
        print(json.dumps({"blocked": True, "reason": "internal error", "error": type(e).__name__})); sys.exit(1)
    if a.cmd not in _QUERY_CMDS:
        _event(a.store, a.project, a.cmd, "allow", {"ok": result.get("ok")})
    print(json.dumps(result))

if __name__ == "__main__":
    main()
