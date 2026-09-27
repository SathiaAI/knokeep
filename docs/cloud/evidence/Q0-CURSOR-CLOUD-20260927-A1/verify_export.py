"""Verify the published original export, including byte counts; fail closed."""
import hashlib
import json
from pathlib import Path
import re

REPO = Path(__file__).resolve().parents[4]
FIXTURE = REPO / 'tests/fixtures/q0/Q0-CURSOR-CLOUD-20260927-A1'
STORE = FIXTURE / 'store-export'
expected = set()
failures = []
for line in (FIXTURE / 'MANIFEST.sha256').read_text(encoding='utf-8').splitlines():
    if not line or line.startswith('#'):
        continue
    match = re.fullmatch(r'([0-9a-f]{64})  (.+)  bytes=(\d+)', line)
    if not match:
        raise SystemExit('Invalid manifest line')
    digest, relative, size = match.groups()
    path = STORE / relative
    if not path.resolve().is_relative_to(STORE.resolve()) or relative in expected:
        raise SystemExit('Unsafe or duplicate manifest member')
    expected.add(relative)
    if not path.is_file():
        failures.append(relative + ': missing')
        continue
    data = path.read_bytes()
    if len(data) != int(size) or hashlib.sha256(data).hexdigest() != digest:
        failures.append(relative + ': size/hash mismatch')
actual = {p.relative_to(STORE).as_posix() for p in STORE.rglob('*') if p.is_file()}
failures.extend('Unmanifested: ' + p for p in sorted(actual - expected))
receipt = json.loads((Path(__file__).parent / 'logs/probe_summary.json').read_text(encoding='utf-8'))
state = (STORE / 'data/q0cursor/system_state').read_bytes()
if hashlib.sha256(state).hexdigest() != receipt['cli_system_state_hash']:
    failures.append('CLI receipt disagrees with original exported state')
print(json.dumps({'checked_members': len(expected), 'failures': failures}, indent=2))
raise SystemExit(bool(failures))
