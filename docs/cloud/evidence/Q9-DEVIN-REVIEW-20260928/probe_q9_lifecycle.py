"""Independent retained-fixture process interruption probes; no cleanup or real stores."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import tempfile

repo = Path(sys.argv[1]).resolve()
output = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(repo))
from store.local import LocalBackend
from store import gate
from store.context import create_ctx
from store.types import OK
from experiments.recovery_export_v1 import export as rex

base = Path(tempfile.mkdtemp(prefix='q9-lifecycle-', dir=Path(__file__).resolve().parent))
results = []

def snap(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}

def child(script, *args):
    env = dict(os.environ, PYTHONPATH=str(repo))
    result = subprocess.run([sys.executable, '-c', script, *map(str, args)], cwd=repo,
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 73, (result.returncode, result.stderr)
    return result.returncode

def record(name, ok, **kw):
    results.append(dict(case=name, passed=bool(ok), **kw))

def setup(name):
    case = base / name
    case.mkdir()
    root = case / 'store'
    backend = LocalBackend(root)
    assert isinstance(gate.persist(backend, 'p/old', b'old', ctx=create_ctx(), doc_type='system_state'), OK)
    backend.close()
    out = case / 'out'
    out.mkdir()
    return root, out

root, out = setup('writer-crash')
child(r'''
import os,sys
from store.local import LocalBackend
from store import gate
from store.context import create_ctx
b=LocalBackend(sys.argv[1]); original=b._journal_append
def crash(*a,**k):
    original(*a,**k)
    os._exit(73)
b._journal_append=crash
gate.persist(b,'p/new',b'durable-new',ctx=create_ctx(),doc_type='system_state')
''', root)
before = snap(root)
r = rex.export_store(root, out)
archive = Path(r['output'])
record('fsynced-writer-process-crash-export',
       r['code'] == 'EXPORT_INCOMPLETE_HUMAN_DECISION_REQUIRED'
       and (archive/'candidate-data/p/new').read_bytes() == b'durable-new'
       and rex.verify_export(archive)['code'] == 'VERIFY_OK'
       and 'PUBLISHED_DATA_DIFFERS_FROM_PREFIX' in r['findings'], code=r['code'], child_exit=73)
record('writer-crash-source-byte-preservation', before == snap(root))

root, out = setup('injected-torn-append')
child(r'''
import os,sys
from pathlib import Path
p=Path(sys.argv[1])/'journal/journal.log'
with p.open('ab') as f:
    f.write(b'KKJ2\x00\x00\x00\x05ab')
    f.flush();os.fsync(f.fileno());os._exit(73)
''', root)
before = snap(root)
r = rex.export_store(root, out)
archive = Path(r['output'])
record('injected-partial-append-process-exit',
       r['code'] == 'EXPORT_INCOMPLETE_HUMAN_DECISION_REQUIRED'
       and 'TAIL_UNEXAMINED_AMBIGUOUS' in r['findings']
       and (archive/'archive/journal/journal.log').read_bytes() == (root/'journal/journal.log').read_bytes()
       and rex.verify_export(archive)['code'] == 'VERIFY_OK', code=r['code'], child_exit=73,
       limitation='Fault-injected partial append, not a randomized OS kill or power-loss test')
record('partial-append-source-byte-preservation', before == snap(root))

root, out = setup('exporter-crash')
before = snap(root)
child(r'''
import os,sys
from experiments.recovery_export_v1 import export as rex
def die(point):
    if point == 'manifest_written':os._exit(73)
rex.export_store(sys.argv[1],sys.argv[2],hook=die)
''', root, out)
attempts = list(out.glob('.incomplete-*'))
assert len(attempts) == 1
attempt = attempts[0]
attempt_before = snap(attempt)
v = rex.verify_export(attempt)
record('exporter-process-crash-incomplete-rejected', v['code'] == 'REJECT_INCOMPLETE_OR_ALTERED'
       and v.get('reason') == 'incomplete_attempt_root', code=v['code'], child_exit=73)
r = rex.export_store(root, out)
record('retry-preserves-original-incomplete-attempt',
       snap(attempt) == attempt_before and rex.verify_export(r['output'])['code'] == 'VERIFY_OK'
       and len(list(out.glob('.incomplete-*'))) == 1)
final_before = snap(Path(r['output']))
r2 = rex.export_store(root, out)
record('second-export-refuses-existing-final', r2['code'] == 'REFUSE_OUTPUT_EXISTS'
       and snap(Path(r['output'])) == final_before)
record('exporter-crash-and-retries-source-byte-preservation', before == snap(root))

report = {'platform': sys.platform, 'cases': results, 'passed': sum(x['passed'] for x in results),
          'total': len(results), 'service_restored': False,
          'limits': 'Synthetic stores only; actual process exits, not power loss or hostile concurrent filesystem.'}
output.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
print(json.dumps(report, indent=2))
sys.exit(0 if all(x['passed'] for x in results) else 1)
