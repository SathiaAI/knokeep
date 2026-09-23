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
import hashlib
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
        in the parent's tree. Thin wrapper over `_build_commit_multi` for the
        single-path case (kept as its own method so its existing signature/
        callers -- including tests/test_git_backend.py's direct calls -- are
        unaffected)."""
        return self._build_commit_multi(parent_sha, [(key, raw)])

    def _build_commit_multi(
        self, parent_sha: Optional[str], changes: List[Tuple[str, bytes]]
    ) -> str:
        """H1 Increment 2, Phase 2: like `_build_commit`, but replaces
        MULTIPLE paths in one commit -- used so a CAS-update's data key and
        its durable fence sidecar (see `_fence_sidecar_path` below) land in
        the SAME commit, published by the SAME push: both take effect
        together or neither does."""
        fd, idx_path = tempfile.mkstemp(dir=str(self._scratch_dir), prefix="index-")
        os.close(fd)
        os.remove(idx_path)  # git wants to create this file itself
        try:
            idx_env = {"GIT_INDEX_FILE": idx_path}
            if parent_sha is not None:
                self._run(["read-tree", parent_sha], env_extra=idx_env, check=True)
            for change_key, change_raw in changes:
                blob_sha = self._hash_object(change_raw)
                self._run(
                    ["update-index", "--add", "--cacheinfo", "100644", blob_sha, change_key],
                    env_extra=idx_env,
                    check=True,
                )
            tree_proc = self._run(["write-tree"], env_extra=idx_env, check=True)
            tree_sha = tree_proc.stdout.decode().strip()

            message = "knokeep: update " + ", ".join(k for k, _ in changes)
            commit_args = ["commit-tree", tree_sha, "-m", message]
            if parent_sha is not None:
                commit_args += ["-p", parent_sha]
            commit_proc = self._run(commit_args, env_extra=self._identity_env, check=True)
            return commit_proc.stdout.decode().strip()
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.remove(idx_path)

    # -- H1 Increment 2, Phase 2: durable fence-ownership sidecar -----------
    # Per-key owner_token/owner_expiry/owner_fence/last_accepted_fence,
    # committed into the repo itself (git has no side-channel storage), at a
    # reserved path never reachable by a real, gate-validated key (contract
    # keys are charset-restricted; this path uses a literal '.' segment,
    # which the gate's key validator forbids -- see store/gate.py -- so no
    # real key can ever collide with it).

    def _fence_sidecar_path(self, key: str) -> str:
        name = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f".knokeep-fence/{name}.fence"

    def _encode_fence_sidecar(
        self, owner_token: Optional[str], owner_expiry: float, owner_fence: int,
        last_accepted_fence: int,
    ) -> bytes:
        return f"{owner_token} {owner_expiry!r} {owner_fence} {last_accepted_fence}".encode("ascii")

    def _read_fence_sidecar(
        self, commit_sha: Optional[str], key: str
    ) -> Optional[Tuple[Optional[str], float, int, int]]:
        """Reads the durable fence state for `key` as of `commit_sha` (None
        if the sidecar does not exist yet -- a key that has never been
        lock()'d/written under fence enforcement)."""
        raw = self._read_blob_at(commit_sha, self._fence_sidecar_path(key))
        if raw is None:
            return None
        try:
            token_s, expiry_s, owner_fence_s, last_accepted_s = raw.decode("ascii").split(" ")
        except (UnicodeDecodeError, ValueError):
            return None
        token: Optional[str] = None if token_s == "None" else token_s
        try:
            return token, float(expiry_s), int(owner_fence_s), int(last_accepted_s)
        except ValueError:
            return None

    _FENCE_COMMIT_RETRY_ATTEMPTS = 50

    def _advance_durable_fence(self, key: str, token: str, expiry: float) -> int:
        """Durable, cross-process/-instance fence allocation: reads the
        sidecar at the current remote tip, computes the next fence, and
        commits+pushes the advance -- retried (bounded, never indefinite)
        on a non-fast-forward rejection, which here means a genuinely
        concurrent lock()/write() (by this or another process) moved the
        ref first. Raises BackendBusyError (mapped the same way lock()'s
        existing acquire-timeout is) if it cannot converge."""
        path = self._fence_sidecar_path(key)
        for _attempt in range(self._FENCE_COMMIT_RETRY_ATTEMPTS):
            try:
                head = self._fetch_head()
            except GitBackendError as e:
                raise BackendBusyError(f"lock({key!r}): fetch failed: {e}") from e
            existing = self._read_fence_sidecar(head, key)
            owner_fence = existing[2] if existing else 0
            last_accepted_fence = existing[3] if existing else 0
            new_fence = max(owner_fence, last_accepted_fence) + 1
            sidecar = self._encode_fence_sidecar(token, expiry, new_fence, last_accepted_fence)
            try:
                new_commit = self._build_commit(head, path, sidecar)
            except GitBackendError as e:
                raise BackendBusyError(f"lock({key!r}): commit build failed: {e}") from e
            scan_result = self._publish_checked(new_commit, head)
            if scan_result is not None:
                raise BackendBusyError(f"lock({key!r}): fence sidecar failed secret scan")
            ok, rejected, stderr = self._push(new_commit)
            if ok:
                return new_fence
            if not rejected:
                raise BackendBusyError(f"lock({key!r}): fence commit push failed: {stderr.strip()}")
            # Non-fast-forward: someone else advanced the ref (fence
            # contention or an unrelated key's write). Bounded retry.
            continue
        raise BackendBusyError(
            f"lock({key!r}): fence allocation did not converge after "
            f"{self._FENCE_COMMIT_RETRY_ATTEMPTS} attempts"
        )

    _FENCE_LOSS_SETTLE_POLL_S = 0.01
    _FENCE_LOSS_SETTLE_MAX_S = 20.0

    def _settle_current_hash_after_fence_loss(
        self, key: str, expected_hash: str
    ) -> Optional[str]:
        """A fence loss is PERMANENT (once superseded, WE can never become
        valid again) -- but git has no single mutex serializing every
        writer the way store/local.py's cas.lock does, so our own
        captured_head snapshot may simply predate the still-in-flight DATA
        write of whichever racer holds the current, valid fence. Without
        this, a fence-losing thread would report a stale local snapshot
        instead of the settled outcome git's own non-fast-forward rejection
        already guarantees the plain hash-CAS path below (a rejection can
        only happen AFTER a conflicting commit exists) -- breaking the
        existing "every loser's current_hash is the true winner's hash"
        guarantee the pre-fence C1 race tests rely on.

        Rather than guess a fixed wait (or an in-process "who else is
        running" heuristic, which is racy against thread-scheduling order:
        a losing thread can reach this check before a winning thread has
        even started), this reads the SAME durable signal the fence check
        itself uses: the fence sidecar's `owner_fence` vs
        `last_accepted_fence` at the freshly re-fetched head. Because
        `last_accepted_fence` is bumped ONLY atomically together with the
        data key, in the SAME commit, as the write that consumes a given
        fence (see write()'s CAS-update branch), `owner_fence >
        last_accepted_fence` is true if and only if SOME lock() has
        allocated a fence that no write has consumed yet -- i.e. a write is
        still owed and may land at any moment. When that is false (no
        pending fence, or none at all), nothing further can change the key
        on this fence's account, and the current snapshot is final -- so a
        genuinely non-racing fence rejection (e.g. an old lease replayed
        with no newer lock() pending a write) returns on its very first
        check, while a real race polls, bounded by
        `_FENCE_LOSS_SETTLE_MAX_S`, until the pending write lands."""
        deadline = time.monotonic() + self._FENCE_LOSS_SETTLE_MAX_S
        current_hash = expected_hash
        while True:
            try:
                head = self._fetch_head()
                raw = self._read_blob_at(head, key)
            except GitBackendError:
                break
            current_hash = sha256_hex(raw) if raw is not None else None
            if current_hash != expected_hash:
                return current_hash
            try:
                fence_state = self._read_fence_sidecar(head, key)
            except GitBackendError:
                break
            pending_fence = fence_state is not None and fence_state[2] > fence_state[3]
            if not pending_fence:
                return current_hash
            if time.monotonic() >= deadline:
                break
            time.sleep(self._FENCE_LOSS_SETTLE_POLL_S)
        return current_hash

    def _renew_durable_fence(self, key: str, token: str, new_expiry: float) -> None:
        """Best-effort: extend the durable sidecar's owner_expiry (fence
        unchanged) to match a renewed advisory lock. Bounded retry on
        non-fast-forward, same shape as `_advance_durable_fence`; gives up
        silently (renew() itself still reports the advisory-lock success it
        already determined) rather than raising, since a caller calling
        renew() does not expect it to fail the way lock() can."""
        path = self._fence_sidecar_path(key)
        for _attempt in range(self._FENCE_COMMIT_RETRY_ATTEMPTS):
            try:
                head = self._fetch_head()
            except GitBackendError:
                return
            existing = self._read_fence_sidecar(head, key)
            if existing is None or existing[0] != token:
                return  # nothing to extend, or already superseded
            _, _, owner_fence, last_accepted_fence = existing
            sidecar = self._encode_fence_sidecar(token, new_expiry, owner_fence, last_accepted_fence)
            try:
                new_commit = self._build_commit(head, path, sidecar)
            except GitBackendError:
                return
            scan_result = self._publish_checked(new_commit, head)
            if scan_result is not None:
                return
            ok, rejected, _stderr = self._push(new_commit)
            if ok or not rejected:
                return
            continue

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
        # H1 Increment 2, Phase 2: this backend now enforces lock()-issued
        # fence ordering on every Overwrite CAS-update via the durable
        # `.knokeep-fence/<sha256(key)>.fence` sidecar committed alongside
        # the data (see write() and _advance_durable_fence).
        return Caps(atomic=True, cas=True, lock=True, durable=True, remote=True, fence=True)

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

        # H1 Increment 2, Phase 2: ctx is required; derive expected_hash the
        # same way store/gate.py does, plus the caller's lease for fence
        # enforcement (Phase 0/1 only carried it structurally).
        precondition_lease: Optional[Lock] = (
            ctx.precondition.lease if isinstance(ctx.precondition, Overwrite) else None
        )
        expected_hash: Optional[str] = (
            ctx.precondition.expected_hash if isinstance(ctx.precondition, Overwrite) else None
        )

        k = key.key
        raw = body.body
        new_hash = sha256_hex(raw)
        new_generation = _extract_generation(raw)

        return self._write_inner(
            k, raw, new_hash, new_generation, expected_hash, precondition_lease
        )

    def _write_inner(
        self,
        k: str,
        raw: bytes,
        new_hash: str,
        new_generation: Optional[int],
        expected_hash: Optional[str],
        precondition_lease: Optional[Lock],
    ) -> WriteResult:
        try:
            captured_head = self._fetch_head()
            current_raw = self._read_blob_at(captured_head, k)
        except GitBackendError:
            return ERROR(ErrorKind.NETWORK)
        current_hash = sha256_hex(current_raw) if current_raw is not None else None

        fence_state: Optional[Tuple[Optional[str], float, int, int]] = None
        changes: List[Tuple[str, bytes]] = [(k, raw)]

        if expected_hash is None:
            # Create-only.
            if current_raw is not None:
                if current_hash == new_hash:
                    return OK(new_hash)  # idempotent create replay (§3)
                return EXISTS(current_hash)
            # else: absent -> fall through to build + publish.
        else:
            # CAS-update. Ownership/fence FIRST (H1 Increment 2, Phase 2),
            # read from the durable sidecar at the SAME captured head as the
            # data key, then the existing hash/generation CAS check.
            try:
                fence_state = self._read_fence_sidecar(captured_head, k)
            except GitBackendError:
                return ERROR(ErrorKind.NETWORK)
            owner_token = fence_state[0] if fence_state else None
            owner_expiry = fence_state[1] if fence_state else 0.0
            last_accepted_fence = fence_state[3] if fence_state else 0
            fence_ok = (
                precondition_lease is not None
                and precondition_lease.token == owner_token
                and owner_expiry > time.time()
                and precondition_lease.fence >= last_accepted_fence
            )
            if not fence_ok:
                # H1 Increment 2, Phase 2: a fence loss is PERMANENT (once
                # superseded, retrying our own check can't change that), but
                # git has no single mutex serializing every writer the way
                # local.py's cas.lock does -- our own captured_head snapshot
                # may simply predate the still-in-flight DATA write of
                # whichever racer holds the current, valid fence. Without
                # this, a fence-losing thread here would report a plain
                # local snapshot instead of the settled outcome that git's
                # own non-fast-forward rejection already guarantees for the
                # plain hash-CAS path below (a rejection can only happen
                # AFTER a conflicting commit exists) -- breaking the
                # existing "every loser's current_hash is the true winner's
                # hash" guarantee the pre-fence C1 race tests rely on.
                # Bounded settle-poll (never indefinite) closes that gap.
                settled_hash = self._settle_current_hash_after_fence_loss(k, expected_hash)
                return STALE(settled_hash, reason="FENCE")

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

            # fence_ok requires fence_state to be non-None (owner_token
            # matched precondition_lease.token, which is never None here).
            assert fence_state is not None
            sidecar = self._encode_fence_sidecar(
                fence_state[0], fence_state[1], fence_state[2], precondition_lease.fence
            )
            changes = [(k, raw), (self._fence_sidecar_path(k), sidecar)]

        try:
            new_commit = self._build_commit_multi(captured_head, changes)
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
        if expected_hash is None:
            try:
                fresh_head = self._fetch_head()
                fresh_raw = self._read_blob_at(fresh_head, k)
            except GitBackendError:
                return ERROR(ErrorKind.NETWORK)
            fresh_hash = sha256_hex(fresh_raw) if fresh_raw is not None else None
            if fresh_raw is not None:
                return EXISTS(fresh_hash)
            # Ref moved for a reason unrelated to this (still-absent) key;
            # our own create-only precondition is unaffected but we made no
            # durable write either (definitely-not-committed).
            return ERROR(ErrorKind.CONFLICT_UNKNOWN)

        # H1 Increment 2, Phase 2: for a CAS-update, the ref move that
        # rejected our push is not necessarily the conflicting DATA write
        # itself -- it can equally be another caller's fence-sidecar advance
        # (lock()) for this same key, landing first. A single immediate
        # re-read right after rejection can therefore still show OUR OWN
        # pre-race snapshot if the actual winning write has not landed yet,
        # which is exactly the gap `_settle_current_hash_after_fence_loss`
        # closes (same durable owner_fence/last_accepted_fence signal, same
        # bounded poll) -- reused here rather than a bespoke second copy.
        settled_hash = self._settle_current_hash_after_fence_loss(k, expected_hash)
        return STALE(settled_hash)

    # -- advisory lock() API (JUDGMENT CALL 1 — NOT the CAS mechanism) ------

    def lock(self, key: str, ttl_s: float) -> Lock:
        with self._lock_mutex:
            now = time.time()
            existing = self._locks.get(key)
            if existing is not None and existing[1] > now:
                raise BackendBusyError(f"key {key!r} is locked")
            token = secrets.token_hex(16)  # 128-bit CSPRNG token
            expiry = now + ttl_s
            new_fence = self._advance_durable_fence(key, token, expiry)
            self._locks[key] = (token, expiry)
            return Lock(key=key, token=token, expiry_epoch=expiry, fence=new_fence)

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
            new_expiry = time.time() + ttl_s
            self._locks[lock.key] = (token, new_expiry)
            self._renew_durable_fence(lock.key, token, new_expiry)
            return True


__all__ = ["GitBackend", "GitBackendError"]
