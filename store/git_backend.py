"""GitBackend — git-based StoreBackend adapter (contract v1.4, §3, §4.3).

"git (solo / fallback)" per contract §4.3. Each key is a blob at a path in a
git tree; a write is a new commit built on the CAPTURED HEAD sha of a target
ref, published by an atomic, fast-forward-only ref update against a
`remote` (a path/URL git already knows how to fetch/push to — in this
environment always a local `git init --bare` repo; the real GitHub remote is
deferred, per the task, until a token is rotated, but nothing here is
GitHub-specific).

Stdlib-only; this module shells out to the `git` CLI (explicitly allowed by
the task) rather than reimplementing git's object model. No network beyond
the `git fetch`/`git push` calls to `remote`, which in every test here is a
local filesystem path — no real network egress occurs.

Architecture
------------
`work_dir` is a small local git repository this backend fully owns (created
via `git init`, never checked out — it holds no tracked working-tree files,
only an object database, refs, and scratch index files used purely as
plumbing scratch space). It plays the role of the "working clone" the task
describes, without literally being created via `git clone`: instead of a
persistent `origin` remote, every fetch/push names `remote` explicitly by
path, which keeps the implementation transport-agnostic (the same code will
work against a real `git@github.com:...` remote later) and avoids relying on
any shared, mutable local state (like `FETCH_HEAD`) that would race across
concurrent callers sharing one `work_dir` (see JUDGMENT CALL 2 below).

Per-write flow (`write()`):
  1. Fetch the remote's current `ref` tip into a uniquely-named local ref
     (race-free — see JUDGMENT CALL 2) and read it back: this is the
     "captured HEAD SHA" the contract requires as the commit's base.
  2. Read the target key's CURRENT blob content at that captured head (via
     `git cat-file -p <head>:<key>`) and compare its sha256 to
     `expected_hash` — explicitly, independent of anything git's own
     fast-forward check will later verify. This is what makes "reject a
     stale per-key hash even if the push would fast-forward" (contract
     §4.3) hold: git's ref-level fast-forward check only catches DAG-level
     divergence, not "this specific key's content moved since the caller
     last read it" — those are different failure classes and only the
     second one is this explicit comparison.
  3. Build a NEW commit, child of the captured head, whose tree differs
     from the captured head's tree in EXACTLY the one changed path — via a
     scratch index (`GIT_INDEX_FILE`) + `read-tree`/`update-index
     --cacheinfo`/`write-tree`/`commit-tree` plumbing, never touching a
     working tree and never re-hashing any blob but the changed one, so
     every other key's blob object id is byte-identical to what it was in
     the captured head's tree ("only the target key's blob mutated").
  4. Scan every BLOB object that is reachable from the new commit but NOT
     already reachable from the captured head (`git rev-list --objects
     new ^captured`) through `store.gate.secret_scan` — this is the "to-be-
     uploaded object set including history" the contract mandates, computed
     the same way `git push` itself would compute what it needs to send.
     If anything scans positive, STOP: no push, no ref update. The
     new commit/tree/blob objects the failed scan found remain only as
     unreferenced loose objects in `work_dir`'s own local object database
     (never pushed, never reachable from any ref anyone else can see, and
     eligible for local `git gc --prune` cleanup) — nothing durable or
     reachable from the published ref carries the unscanned bytes.
  5. Publish with a single, plain (never `--force`, never `git rebase`)
     `git push remote <new-commit>:<ref>`. Because the new commit's parent
     is exactly the captured head, a plain push succeeds if and only if the
     remote ref is UNCHANGED since step 1 (a true git fast-forward) — this
     IS "git push --force-with-lease=<ref>:<captured> semantics" without
     needing the flag, since we never intend to overwrite a diverged tip:
     a diverged tip is exactly what we want rejected. Rejection (non-fast-
     forward) is reported as STALE (CAS-update) or EXISTS/CONFLICT_UNKNOWN
     (create-only) after a fresh re-read, never retried, never rebased.

JUDGMENT CALLS (numbered, each also called out inline at its point of use):

  1. Advisory lock()/unlock()/renew() are kept in-memory (one dict guarded
     by a `threading.Lock`), the same shape as `FakeBackend`'s, rather than
     durable-in-git (e.g. a lease object under `refs/knokeep/locks/<key>`).
     Contract §3 is explicit that "Advisory lock() is never the CAS
     mechanism" for any backend, and §4.3 does not mandate a specific lock
     storage for git — only the load-bearing CAS-via-ref-update matters for
     git specifically. An in-memory advisory lock satisfies the documented
     contract (§2's token/TTL semantics, §9's C7) without adding a second,
     independent durability mechanism (a git ref-based lock) that nothing
     in §4.3 calls for and that would itself need its own CAS story.

  2. Race-free head-capture without `FETCH_HEAD`. A naive
     `git fetch remote ref` followed by `git rev-parse FETCH_HEAD` is NOT
     safe when multiple callers share one `work_dir` (conformance/suite.py's
     C1/C2 races call `gate.persist()` on the SAME backend instance from
     many threads): `FETCH_HEAD` is one shared mutable file, and one
     thread's fetch can overwrite it before another thread reads it back,
     handing that thread the WRONG "captured head". Instead, every fetch
     lands under a freshly `uuid4()`-named local ref
     (`refs/knokeep/fetch/<uuid>`), which this call and only this call ever
     reads or writes; the SHA is read back from THAT ref, then the
     scratch ref is deleted. Concurrent fetches into the same object
     database are safe (git objects are content-addressed and idempotent;
     distinct ref names are distinct files/entries with no shared mutable
     state), so this removes the race entirely rather than narrowing it.

  3. Adapter-level secret scan is necessarily a defense-in-depth no-op on
     the sanctioned path. `ScannedBody` is constructible only inside
     `store/gate.py`, and `gate.persist()` always runs `secret_scan()`
     BEFORE wrapping bytes into one — so a `ScannedBody` carrying secret
     content can never reach `GitBackend.write()` through the public API at
     all. The step-4 scan above is still implemented in full (per contract
     §4.3, unconditionally, on every write) because it is the layer that
     would catch content this backend's OWN commit-construction pulled in
     from git history that did NOT go through gate.persist() for THIS call
     (the literal "including history" in "scan the entire to-be-uploaded
     object set including history") — e.g. a prior write's commit built
     directly against this backend's plumbing (bypassing the gate, as a
     test or a bug might do) that never got scanned. Because this can't be
     reached from `gate.persist()`, `tests/test_git_backend.py` exercises
     it directly through the backend's own private commit-building/publish
     helpers (`_build_commit`/`_publish_checked`), the same way
     `tests/test_local_backend.py` reaches into `LocalBackend`'s private
     journal primitives to simulate states the public API cannot produce.

  4. Non-fast-forward push rejection always maps to STALE for a CAS-update
     (never CONFLICT_UNKNOWN, never an automatic retry/rebase), even in the
     case where the intervening commit that moved the ref did not touch
     the key being written (a benign, unrelated-key race). Contract §4.3
     states plainly "Non-FF → STALE" with no carve-out for this case, and
     "NEVER force-push or rebase" rules out the alternative of silently
     re-basing our one-key change onto the new tip and re-attempting —
     that would require re-running the step-2 per-key hash check against
     the new tip before re-committing, which is exactly a retry loop the
     contract's wording does not ask for. A caller that wants resilience
     against benign unrelated-key contention retries the whole CAS write
     (re-read, re-persist) exactly as it would against any other backend's
     STALE result.

  5. Generation monotonicity (residual from T1, same as `LocalBackend`):
     `store/gate.py persist()` only validates that a non-STATE doc type's
     body carries a well-formed `#knokeep-gen:<uint64>` header; it cannot
     check monotonicity (stateless per call). This adapter derives the
     stored generation from the CURRENT blob at the captured head and
     rejects a CAS-update whose new generation is not strictly greater,
     returning STALE(current_hash) — identical logic and identical
     rationale to `store/local.py`'s judgment call 1.

  6. `capabilities().remote = True`. Even though every test here points
     `remote` at a local filesystem path, the adapter is architecturally a
     "remote-style" backend (§4.3 explicitly files git under "fallback",
     alongside object-store, and the whole point of the design is that the
     same code later targets a real GitHub remote) — unlike `LocalBackend`
     (`remote=False`, a bare filesystem with no independent durability
     authority), this backend's `OK` is contingent on a `git push`
     accepting the ref update, i.e. an external acknowledgment, matching
     the contract's own framing in §3: "remote backends derive durability
     from their server ACK; git from the ref update."

  7. A push failure that does NOT look like a fast-forward rejection (git
     not reachable, remote path missing, permission denied, etc.) is
     mapped to `ERROR(NETWORK)` (or `ERROR(PERMISSION)` when the stderr
     text plainly says so) rather than raised, since `write()` — unlike
     `read()` — is documented to report failures as `WriteResult`, and
     `NETWORK` is the ErrorKind the contract's own §1 commit-classification
     table already uses for "failed before the request was fully sent".
"""
from __future__ import annotations

import contextlib
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from . import gate
from .backend import BackendBusyError, Lock
from .context import Overwrite
from .gate import ScannedBody, ScannedKey
from .types import (
    BackendHealth,
    Blob,
    Caps,
    EXISTS,
    ERROR,
    ErrorKind,
    OK,
    STALE,
    WriteResult,
    sha256_hex,
)


class GitBackendError(Exception):
    """An unexpected git/plumbing failure (not a normal CAS outcome).

    Raised out of internal helpers; the public StoreBackend methods either
    map it to a WriteResult (write()) or let it propagate (read()/list(),
    per contract §2: "PERMISSION/NETWORK/CORRUPTION RAISE").
    """


# ---------------------------------------------------------------------------
# Generation header (contract §6; same convention as store/gate.py's
# `make_generation_header` / store/local.py's local copy — re-implemented
# here rather than importing gate's private helper, same rationale as local.py).
# ---------------------------------------------------------------------------

_GEN_HEADER_RE = re.compile(rb"^#knokeep-gen:(0|[1-9][0-9]{0,19})\n")
_UINT64_MAX = (1 << 64) - 1


def _extract_generation(raw: bytes) -> Optional[int]:
    m = _GEN_HEADER_RE.match(raw)
    if not m:
        return None
    value = int(m.group(1))
    if value > _UINT64_MAX:
        return None
    return value


def _check_git_available() -> None:
    """Contract task requirement: confirm `git` exists before doing anything."""
    try:
        proc = subprocess.run(
            ["git", "--version"], capture_output=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"GitBackend requires the `git` CLI on PATH: {e}") from e
    if proc.returncode != 0:
        raise RuntimeError(
            "GitBackend requires the `git` CLI on PATH: "
            f"`git --version` exited {proc.returncode}"
        )


_REJECTED_MARKERS = (
    "[rejected]",
    "non-fast-forward",
    "fetch first",
    "stale info",
    "already exists",
    "[remote rejected]",
    "cannot lock ref",
    "failed to update ref",
)


class GitBackend:
    """git-backed StoreBackend adapter. See module docstring."""

    def __init__(
        self,
        work_dir,
        remote,
        *,
        ref: str = "refs/heads/main",
        author_name: str = "knokeep-store",
        author_email: str = "store@knokeep.local",
        git_timeout_s: float = 30.0,
    ) -> None:
        _check_git_available()

        self._work_dir = Path(work_dir).resolve()
        # JUDGMENT CALL: every git subprocess below runs with `-C work_dir`,
        # which changes git's effective cwd and therefore how it resolves a
        # RELATIVE remote path (relative to work_dir, not to whatever the
        # caller's own process cwd was). A local-filesystem remote is
        # resolved to an absolute path once, here, so callers can pass a
        # relative path naturally; a real URL remote (git@host:..., ssh://,
        # https://, ...) is left untouched since Path(...).exists() is
        # False for it and it has no such relative-resolution ambiguity.
        self._remote = str(remote)
        if os.path.exists(self._remote):
            self._remote = str(Path(self._remote).resolve())
        self._ref = ref
        self._git_timeout_s = git_timeout_s
        self._identity_env = {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }

        self._work_dir.mkdir(parents=True, exist_ok=True)
        if not (self._work_dir / ".git").exists():
            init = subprocess.run(
                ["git", "init", "--quiet", str(self._work_dir)],
                capture_output=True,
                timeout=self._git_timeout_s,
            )
            if init.returncode != 0:
                raise GitBackendError(
                    f"git init failed: {init.stderr.decode('utf-8', 'replace')}"
                )
        # Defensive: never let ambient config silently mutate blob bytes
        # (line-ending conversion) or require local GPG signing.
        self._run(["config", "core.autocrlf", "false"], check=True)
        self._run(["config", "commit.gpgsign", "false"], check=True)

        self._scratch_dir = (self._work_dir / ".knokeep-scratch").resolve()
        self._scratch_dir.mkdir(parents=True, exist_ok=True)

        # -- advisory lock() bookkeeping (JUDGMENT CALL 1: in-memory only) --
        self._lock_mutex = threading.Lock()
        self._locks: Dict[str, Tuple[str, float]] = {}

    # -- subprocess plumbing -------------------------------------------------

    def _run(
        self,
        args,
        *,
        input_bytes: Optional[bytes] = None,
        env_extra: Optional[dict] = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        if env_extra:
            env.update(env_extra)
        try:
            proc = subprocess.run(
                ["git", "-C", str(self._work_dir), *args],
                input=input_bytes,
                stdin=subprocess.DEVNULL if input_bytes is None else None,
                capture_output=True,
                env=env,
                timeout=self._git_timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            raise GitBackendError(f"git {' '.join(args)} timed out") from e
        except OSError as e:
            raise GitBackendError(f"git {' '.join(args)} failed to start: {e}") from e
        if check and proc.returncode != 0:
            raise GitBackendError(
                f"git {' '.join(args)} failed (exit {proc.returncode}): "
                f"{proc.stderr.decode('utf-8', 'replace').strip()}"
            )
        return proc

    # -- head capture (JUDGMENT CALL 2: race-free, no shared FETCH_HEAD) ----

    def _fetch_head(self) -> Optional[str]:
        """Fetch remote's current `ref` tip into a uniquely-named local ref
        and return its SHA, or None if the ref does not exist on the remote
        yet (unborn branch / freshly `git init --bare`'d remote)."""
        tmp_ref = f"refs/knokeep/fetch/{uuid.uuid4().hex}"
        proc = self._run(
            ["fetch", "--quiet", self._remote, f"{self._ref}:{tmp_ref}"], check=False
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace").lower()
            # git phrases this as "couldn't find remote ref" (recent git) or
            # "couldn't find remote ref ..." with a trailing branch name; a
            # simple substring check on the stable prefix covers both.
            if "couldn't find remote ref" in stderr:
                return None
            raise GitBackendError(f"git fetch failed: {stderr.strip()}")
        try:
            rp = self._run(["rev-parse", tmp_ref], check=True)
            return rp.stdout.decode().strip()
        finally:
            self._run(["update-ref", "-d", tmp_ref], check=False)

    def _read_blob_at(self, commit_sha: Optional[str], key: str) -> Optional[bytes]:
        if commit_sha is None:
            return None
        proc = self._run(["cat-file", "-p", f"{commit_sha}:{key}"], check=False)
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace").lower()
            if "does not exist" in stderr or "bad revision" in stderr or "not found" in stderr or "not a valid object" in stderr:
                return None
            raise GitBackendError(f"git cat-file failed: {stderr.strip()}")
        return proc.stdout

    # -- commit construction: only the target key's blob mutated ------------

    def _hash_object(self, raw: bytes) -> str:
        proc = self._run(
            ["hash-object", "-w", "--no-filters", "--stdin"], input_bytes=raw, check=True
        )
        return proc.stdout.decode().strip()

    def _build_commit(self, parent_sha: Optional[str], key: str, raw: bytes) -> str:
        """Build (but do not publish) a commit, child of `parent_sha`, whose
        tree is `parent_sha`'s tree with exactly `key` replaced by a new blob
        of `raw`. Every other path keeps the exact same blob object id it had
        in the parent's tree."""
        blob_sha = self._hash_object(raw)
        fd, idx_path = tempfile.mkstemp(dir=str(self._scratch_dir), prefix="index-")
        os.close(fd)
        os.remove(idx_path)  # git wants to create this file itself
        try:
            idx_env = {"GIT_INDEX_FILE": idx_path}
            if parent_sha is not None:
                self._run(["read-tree", parent_sha], env_extra=idx_env, check=True)
            self._run(
                ["update-index", "--add", "--cacheinfo", "100644", blob_sha, key],
                env_extra=idx_env,
                check=True,
            )
            tree_proc = self._run(["write-tree"], env_extra=idx_env, check=True)
            tree_sha = tree_proc.stdout.decode().strip()

            commit_args = ["commit-tree", tree_sha, "-m", f"knokeep: update {key}"]
            if parent_sha is not None:
                commit_args += ["-p", parent_sha]
            commit_proc = self._run(commit_args, env_extra=self._identity_env, check=True)
            return commit_proc.stdout.decode().strip()
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.remove(idx_path)

    # -- pre-publish scan: the entire to-be-uploaded object set (§4.3) ------

    def _objects_to_upload(
        self, new_commit_sha: str, base_sha: Optional[str]
    ) -> List[Tuple[str, bytes]]:
        """Every BLOB reachable from `new_commit_sha` but not already
        reachable from `base_sha` — exactly the object closure `git push`
        itself would need to transfer (§4.3: "the entire to-be-uploaded
        object set including history")."""
        args = ["rev-list", "--objects", new_commit_sha]
        if base_sha:
            args.append(f"^{base_sha}")
        proc = self._run(args, check=True)
        shas = [
            line.split(" ", 1)[0]
            for line in proc.stdout.decode().splitlines()
            if line.strip()
        ]
        if not shas:
            return []
        check_input = ("\n".join(shas) + "\n").encode()
        check_proc = self._run(
            ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
            input_bytes=check_input,
            check=True,
        )
        blob_shas = []
        for line in check_proc.stdout.decode().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == "blob":
                blob_shas.append(parts[0])
        results: List[Tuple[str, bytes]] = []
        for sha in blob_shas:
            content_proc = self._run(["cat-file", "-p", sha], check=True)
            results.append((sha, content_proc.stdout))
        return results

    def _scan_for_secrets(self, objects: List[Tuple[str, bytes]]) -> List[str]:
        labels: List[str] = []
        for _sha, content in objects:
            labels.extend(gate.secret_scan(content))
        return labels

    def _publish_checked(
        self, new_commit_sha: str, base_sha: Optional[str]
    ) -> WriteResult | None:
        """Run the mandatory pre-publish secret scan (§4.3) and, if clean,
        push. Returns an ERROR WriteResult if the scan blocked the publish
        (and, in that case, NOTHING was pushed / no ref was updated); returns
        None if the scan passed and the caller should proceed to interpret
        the push outcome itself (kept separate from push() so tests can call
        this exact step directly — JUDGMENT CALL 3)."""
        try:
            new_objects = self._objects_to_upload(new_commit_sha, base_sha)
        except GitBackendError:
            return ERROR(ErrorKind.CORRUPTION)
        try:
            labels = self._scan_for_secrets(new_objects)
        except Exception:
            return ERROR(ErrorKind.SCAN_FAILURE)
        if labels:
            # Fail closed BEFORE any network/ref write: no push, no
            # update-ref. The scanned-positive objects remain only as
            # unreferenced loose objects in this local work_dir, never
            # reachable from any ref anyone else can see.
            return ERROR(ErrorKind.SECRET_BLOCKED, labels=tuple(sorted(set(labels))))
        return None

    def _push(self, new_commit_sha: str) -> Tuple[bool, bool, str]:
        """Plain (never --force) push. Returns (ok, looked_like_rejection, stderr)."""
        proc = self._run(
            ["push", "--no-verify", self._remote, f"{new_commit_sha}:{self._ref}"],
            check=False,
        )
        stderr = proc.stderr.decode("utf-8", "replace")
        if proc.returncode == 0:
            return True, False, stderr
        low = stderr.lower()
        rejected = any(marker in low for marker in _REJECTED_MARKERS)
        return False, rejected, stderr

    # -- StoreBackend protocol ---------------------------------------------

    def capabilities(self) -> Caps:
        # JUDGMENT CALL 6: remote=True (see module docstring).
        return Caps(atomic=True, cas=True, lock=True, durable=True, remote=True)

    def health(self) -> BackendHealth:
        try:
            self._fetch_head()
            return BackendHealth(
                ok=True, detail=f"git backend remote={self._remote} ref={self._ref}"
            )
        except GitBackendError as e:
            return BackendHealth(ok=False, detail=str(e))

    def read(self, key: str) -> Optional[Blob]:
        # PERMISSION/NETWORK/CORRUPTION raise (contract §2); GitBackendError
        # covers all three here (no finer classification is derivable from
        # a git subprocess's stderr in general).
        head = self._fetch_head()
        raw = self._read_blob_at(head, key)
        if raw is None:
            return None
        return Blob(body=raw, version_hash=sha256_hex(raw))

    def list(self, prefix: str) -> Iterator[str]:
        head = self._fetch_head()
        if head is None:
            return iter([])
        proc = self._run(["ls-tree", "-r", "--name-only", head], check=True)
        names = [n for n in proc.stdout.decode("utf-8", "replace").splitlines() if n]
        return iter(sorted(n for n in names if n.startswith(prefix)))

    def write(
        self,
        key: ScannedKey,
        body: ScannedBody,
        *,
        ctx,
    ) -> WriteResult:
        # Adapter accepts only gate-issued values; raw bytes/str are a
        # TypeError before any I/O (contract §1/§5).
        if not isinstance(key, ScannedKey) or not isinstance(body, ScannedBody):
            raise TypeError(
                "GitBackend.write requires ScannedKey/ScannedBody from store.gate.persist()"
            )
        if not gate.verify(key) or not gate.verify(body):
            raise TypeError("GitBackend.write: gate marker verification failed")

        # H1 Increment 2, Phase 0: ctx is required; derive expected_hash the
        # same way store/gate.py does. No fence enforcement yet.
        expected_hash: Optional[str] = (
            ctx.precondition.expected_hash if isinstance(ctx.precondition, Overwrite) else None
        )

        k = key.key
        raw = body.body
        new_hash = sha256_hex(raw)
        new_generation = _extract_generation(raw)

        try:
            captured_head = self._fetch_head()
            current_raw = self._read_blob_at(captured_head, k)
        except GitBackendError:
            return ERROR(ErrorKind.NETWORK)
        current_hash = sha256_hex(current_raw) if current_raw is not None else None

        if expected_hash is None:
            # Create-only.
            if current_raw is not None:
                if current_hash == new_hash:
                    return OK(new_hash)  # idempotent create replay (§3)
                return EXISTS(current_hash)
            # else: absent -> fall through to build + publish.
        else:
            # CAS-update.
            if current_raw is None:
                return STALE(None)
            if current_hash != expected_hash:
                # JUDGMENT CALL: this explicit per-key comparison, evaluated
                # BEFORE any commit is even built, is what makes "reject a
                # stale per-key hash even if the push would fast-forward"
                # hold (§4.3) — it does not depend on git's own
                # fast-forward check at all.
                return STALE(current_hash)
            if new_generation is not None:
                stored_generation = _extract_generation(current_raw)
                if stored_generation is not None and new_generation <= stored_generation:
                    return STALE(current_hash)  # JUDGMENT CALL 5

        try:
            new_commit = self._build_commit(captured_head, k, raw)
        except GitBackendError:
            return ERROR(ErrorKind.CORRUPTION)

        scan_result = self._publish_checked(new_commit, captured_head)
        if scan_result is not None:
            return scan_result

        ok, rejected, _stderr = self._push(new_commit)
        if ok:
            return OK(new_hash)
        if not rejected:
            return ERROR(ErrorKind.NETWORK)  # JUDGMENT CALL 7

        # Non-fast-forward: the ref moved between our fetch and our push.
        # Re-read fresh state to classify + report accurately, per §4.3
        # "Non-FF -> STALE" (never retried, never rebased — JUDGMENT CALL 4).
        try:
            fresh_head = self._fetch_head()
            fresh_raw = self._read_blob_at(fresh_head, k)
        except GitBackendError:
            return ERROR(ErrorKind.NETWORK)
        fresh_hash = sha256_hex(fresh_raw) if fresh_raw is not None else None

        if expected_hash is None:
            if fresh_raw is not None:
                return EXISTS(fresh_hash)
            # Ref moved for a reason unrelated to this (still-absent) key;
            # our own create-only precondition is unaffected but we made no
            # durable write either (definitely-not-committed).
            return ERROR(ErrorKind.CONFLICT_UNKNOWN)
        return STALE(fresh_hash)

    # -- advisory lock() API (JUDGMENT CALL 1 — NOT the CAS mechanism) ------

    def lock(self, key: str, ttl_s: float) -> Lock:
        with self._lock_mutex:
            now = time.time()
            existing = self._locks.get(key)
            if existing is not None and existing[1] > now:
                raise BackendBusyError(f"key {key!r} is locked")
            token = secrets.token_hex(16)  # 128-bit CSPRNG token
            expiry = now + ttl_s
            self._locks[key] = (token, expiry)
            return Lock(key=key, token=token, expiry_epoch=expiry)

    def unlock(self, lock: Lock) -> bool:
        with self._lock_mutex:
            existing = self._locks.get(lock.key)
            if existing is None:
                return False
            token, expiry = existing
            if token != lock.token or expiry <= time.time():
                return False
            del self._locks[lock.key]
            return True

    def renew(self, lock: Lock, ttl_s: float) -> bool:
        with self._lock_mutex:
            existing = self._locks.get(lock.key)
            if existing is None:
                return False
            token, expiry = existing
            if token != lock.token or expiry <= time.time():
                return False
            self._locks[lock.key] = (token, time.time() + ttl_s)
            return True


__all__ = ["GitBackend", "GitBackendError"]
