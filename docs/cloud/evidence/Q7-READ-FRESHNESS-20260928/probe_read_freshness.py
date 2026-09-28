"""Independent process-crash probes. JSON only; no private paths in output."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

repo = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(repo))
from store import gate
from store.local import LocalBackend
from store.types import OK
from tests.ctx_helpers import create_ctx
from tests.test_local_backend import _run_crash_child, _CRASH_EXIT_CODE

results = []
def report(name, passed, **evidence):
    results.append(dict(case=name, passed=bool(passed), **evidence))

with tempfile.TemporaryDirectory(prefix='knokeep-freshness-') as td:
    root = Path(td) / 'cas'
    parent = LocalBackend(root)
    r = gate.persist(parent, 'p/head', b'old', ctx=create_ctx(), doc_type='system_state')
    assert isinstance(r, OK)
    child = _run_crash_child('cas', root, 'p/head', 'durable-new', r.new_hash)
    assert child.returncode == _CRASH_EXIT_CODE
    journal = root / 'journal/journal.log'
    original = journal.read_bytes()
    got = parent.read('p/head')
    report('read_after_durable_child_crash', got.body == b'durable-new', observed=got.body.decode(), child_exit=child.returncode)
    report('read_preserves_journal', journal.read_bytes() == original)
    parent.close()

    root = Path(td) / 'create'
    parent = LocalBackend(root)
    child = _run_crash_child('create', root, 'p/new', 'durable-create')
    assert child.returncode == _CRASH_EXIT_CODE
    original = (root / 'journal/journal.log').read_bytes()
    listed = list(parent.list('p/'))
    report('list_after_durable_child_crash', 'p/new' in listed, observed=listed)
    got = parent.read('p/new')
    report('read_unpublished_creation', got is not None and got.body == b'durable-create', observed=None if got is None else got.body.decode())
    report('list_and_read_preserve_journal', (root / 'journal/journal.log').read_bytes() == original)
    parent.close()

    root = Path(td) / 'ambiguous'
    backend = LocalBackend(root)
    gate.persist(backend, 'p/head', b'old', ctx=create_ctx(), doc_type='system_state')
    # Historical-store fixture: opaque broken bytes, then a complete later
    # record plus its published view. This is not a new writer behavior.
    backend._journal_fh.write(b'\x00\x00\x00\x05ab')
    backend._journal_fh.flush()
    backend._journal_append('p/head', b'later-durable')
    (root / 'data/p/head').write_bytes(b'later-durable')
    backend.close()
    original = (root / 'journal/journal.log').read_bytes()
    before = (root / 'data/p/head').read_bytes()
    startup_error = None
    try:
        reopened = LocalBackend(root)
    except Exception as exc:
        startup_error = type(exc).__name__
    else:
        try:
            reopened.read('p/head')
        except Exception as exc:
            startup_error = type(exc).__name__
        reopened.close()
    after = (root / 'data/p/head').read_bytes()
    report('ambiguous_restart_does_not_rollback_view', after == before, before=before.decode(), after=after.decode(), error=startup_error)
    report('ambiguous_restart_reports_uncertainty', startup_error is not None, error=startup_error)
    report('ambiguous_restart_preserves_journal', (root / 'journal/journal.log').read_bytes() == original, sha256=hashlib.sha256(original).hexdigest())

print(json.dumps({'cases': results, 'pass': sum(x['passed'] for x in results), 'total': len(results)}, indent=2))
sys.exit(0 if all(x['passed'] for x in results) else 1)
