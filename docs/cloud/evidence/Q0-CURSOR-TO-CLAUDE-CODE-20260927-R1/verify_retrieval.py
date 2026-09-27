"""Q0-CURSOR-TO-CLAUDE-CODE-20260927-R1: read-only retrieval check of the Cursor Cloud export.

Verifies the committed export (manifest hash + size), runs the strict verifier, copies the
export to a disposable scratch dir, runs the existing CLI bootstrap and an independent
LocalBackend read against that copy, compares returned hashes with the export bytes, and
confirms the committed fixture is unchanged. Never writes to the committed fixture.
Writes logs/command_log.md and logs/results.json next to this script; paths are
sanitized to $REPO / $SCRATCH. Exit 0 only if every check passes."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
SOURCE_ID = 'Q0-CURSOR-CLOUD-20260927-A1'
EXPECTED_HEAD = 'be48e86408ff93290e2e750ac04ee2a5bd49e094'
PROJECT = 'q0cursor'
FIXTURE = REPO / 'tests/fixtures/q0' / SOURCE_ID
STORE = FIXTURE / 'store-export'
FIXTURE_REL = FIXTURE.relative_to(REPO).as_posix()
LOGS = HERE / 'logs'
GIT = ['git', '-c', 'safe.directory=' + REPO.as_posix()]

scratch = None
log = []
checks = {}


def sanitize(text):
    pairs = [(str(REPO), '$REPO'), (REPO.as_posix(), '$REPO')]
    if scratch:
        pairs = [(str(scratch), '$SCRATCH'), (scratch.as_posix(), '$SCRATCH'),
                 (os.path.realpath(scratch), '$SCRATCH')] + pairs
    for raw, token in pairs:
        text = text.replace(raw, token).replace(raw.replace('\\', '\\\\'), token)
    return text


def run(argv, display):
    proc = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, encoding='utf-8')
    log.append({'command': display, 'exit_code': proc.returncode,
                'stdout': sanitize(proc.stdout), 'stderr': sanitize(proc.stderr)})
    return proc


def manifest_check():
    failures, rows = [], []
    for line in (FIXTURE / 'MANIFEST.sha256').read_text(encoding='utf-8').splitlines():
        if not line or line.startswith('#'):
            continue
        m = re.fullmatch(r'([0-9a-f]{64})  (.+)  bytes=(\d+)', line)
        if not m:
            failures.append('invalid manifest line')
            continue
        digest, rel, size = m.groups()
        path = STORE / rel
        if not path.is_file():
            failures.append(rel + ': missing')
            continue
        data = path.read_bytes()
        ok = len(data) == int(size) and hashlib.sha256(data).hexdigest() == digest
        rows.append({'member': rel, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'ok': ok})
        if not ok:
            failures.append(rel + ': size/hash mismatch')
    listed = {r['member'] for r in rows}
    actual = {p.relative_to(STORE).as_posix() for p in STORE.rglob('*') if p.is_file()}
    failures += ['unmanifested: ' + p for p in sorted(actual - listed)]
    return rows, failures


def fixture_hashes():
    return {p.relative_to(STORE).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(STORE.rglob('*')) if p.is_file()}


def main():
    global scratch
    head = run(GIT + ['rev-parse', 'HEAD'], 'git rev-parse HEAD').stdout.strip()
    checks['head_matches'] = head == EXPECTED_HEAD
    if not checks['head_matches']:
        return finish({'head': head})
    run(['python', '--version'], 'python --version')

    rows, failures = manifest_check()
    checks['manifest_hash_and_size'] = not failures
    before = fixture_hashes()

    strict = run([sys.executable, f'docs/cloud/evidence/{SOURCE_ID}/verify_export.py'],
                 f'python docs/cloud/evidence/{SOURCE_ID}/verify_export.py')
    checks['strict_verifier_exit_0'] = strict.returncode == 0

    scratch = Path(tempfile.mkdtemp(prefix='.q0-r1-scratch-', dir=REPO))  # disposable, removed below
    try:
        copy = scratch / 'store'
        shutil.copytree(STORE, copy)
        boot = run([sys.executable, 'skill/knokeep_state.py', 'bootstrap', '--store', str(copy), '--project', PROJECT],
                   f'python skill/knokeep_state.py bootstrap --store $SCRATCH/store --project {PROJECT}')
        checks['bootstrap_exit_0'] = boot.returncode == 0
        try:
            boot_json = json.loads(boot.stdout)
        except ValueError:
            boot_json = None

        sys.path.insert(0, str(REPO))
        from store.local import LocalBackend
        backend = LocalBackend(os.path.realpath(copy))
        reads = {}
        for key in (f'{PROJECT}/system_state', f'{PROJECT}/session_log', f'{PROJECT}/sessions/q0harness'):
            blob = backend.read(key)
            export_bytes = (STORE / 'data' / key).read_bytes()
            reads[key] = None if blob is None else {
                'version_hash': blob.version_hash,
                'export_sha256': hashlib.sha256(export_bytes).hexdigest(),
                'bytes_equal_export': blob.body == export_bytes,
                'body': blob.body.decode('utf-8'),
            }
        listed = sorted(backend.list(PROJECT + '/'))
        backend._journal_fh.close()
        log.append({'command': 'python: LocalBackend($SCRATCH/store).read/list (in-process)', 'exit_code': 0,
                    'stdout': sanitize(json.dumps({'list': listed, 'reads': {k: (v and {x: v[x] for x in v if x != 'body'})
                                                                              for k, v in reads.items()}}, indent=1)),
                    'stderr': ''})
        state = reads[f'{PROJECT}/system_state']
        checks['independent_read_bytes_equal_export'] = all(v and v['bytes_equal_export'] for v in reads.values())
        checks['bootstrap_version_hash_equals_export_sha256'] = bool(
            boot_json and state and boot_json.get('version_hash') == state['export_sha256'])
        log_read = reads[f'{PROJECT}/session_log']
        checks['bootstrap_log_hash_equals_export_sha256'] = bool(
            boot_json and log_read and boot_json.get('log_hash') == log_read['export_sha256'])
        scratch_events = (copy / '.knokeep-eval/events.jsonl').read_bytes()
        telemetry_appended = scratch_events != (STORE / '.knokeep-eval/events.jsonl').read_bytes()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    after = fixture_hashes()
    checks['fixture_bytes_unchanged'] = before == after
    diff = run(GIT + ['diff', '--exit-code', '--', FIXTURE_REL], f'git diff --exit-code -- {FIXTURE_REL}')
    status = run(GIT + ['status', '--porcelain', '--', FIXTURE_REL], f'git status --porcelain -- {FIXTURE_REL}')
    checks['fixture_git_diff_clean'] = diff.returncode == 0 and not diff.stdout and not status.stdout
    return finish({'head': head, 'manifest': rows, 'manifest_failures': failures,
                   'bootstrap_result': boot_json, 'reads': reads, 'listed_keys': listed,
                   'scratch_telemetry_appended': telemetry_appended})


def finish(result):
    LOGS.mkdir(exist_ok=True)
    result['checks'] = checks
    result['all_passed'] = bool(checks) and all(checks.values())
    (LOGS / 'results.json').write_text(json.dumps(result, indent=1) + '\n', encoding='utf-8')
    out = ['# Command log (generated by verify_retrieval.py; paths sanitized)', '']
    for entry in log:
        out += [f"## `{entry['command']}`", '', f"exit code: {entry['exit_code']}", '',
                'stdout:', '```', entry['stdout'].rstrip(), '```', '',
                'stderr:', '```', entry['stderr'].rstrip(), '```', '']
    (LOGS / 'command_log.md').write_text('\n'.join(out), encoding='utf-8')
    print(json.dumps({'checks': checks, 'all_passed': result['all_passed']}, indent=1))
    return 0 if result['all_passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
