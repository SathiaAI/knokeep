"""Independent follow-up tests: import target via KNOKEEP_REVIEW_ROOT."""
import os
import hashlib
import struct
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, os.environ['KNOKEEP_REVIEW_ROOT'])
from store import gate
from store.local import LocalBackend, _FileLock
from store.types import OK
from tests.ctx_helpers import create_ctx


def seed(root, **options):
    backend = LocalBackend(root, **options)
    assert isinstance(gate.persist(backend, 'p/a', b'original', ctx=create_ctx(), doc_type='system_state'), OK)
    return backend


@pytest.mark.parametrize('operation', ['read', 'list'])
def test_runtime_missing_journal_is_not_authoritative(tmp_path, operation):
    backend = seed(tmp_path)
    # Close the append handle before removal (portable Windows fixture),
    # while preserving the long-lived object's state and published view.
    backend._journal_fh.close()
    path = tmp_path / 'journal/journal.log'
    original = path.read_bytes()
    path.unlink()
    try:
        with pytest.raises(Exception):
            backend.read('p/a') if operation == 'read' else list(backend.list('p/'))
        assert not path.exists()
        assert (tmp_path / 'data/p/a').read_bytes() == b'original'
    finally:
        path.write_bytes(original)
        backend.close()


def test_missing_journal_startup_preserves_absence_and_data(tmp_path):
    backend = seed(tmp_path)
    backend.close()
    path = tmp_path / 'journal/journal.log'
    path.unlink()
    with pytest.raises(Exception):
        opened = LocalBackend(tmp_path)
        opened.close()
    assert not path.exists(), 'startup silently manufactured a new empty journal'
    assert (tmp_path / 'data/p/a').read_bytes() == b'original'


@pytest.mark.parametrize('operation', ['read', 'list'])
def test_read_list_lock_contention_bounded_and_released(tmp_path, operation):
    backend = seed(tmp_path, lock_timeout_s=0.08)
    lock = _FileLock(tmp_path / 'locks/cas.lock')
    assert lock.acquire(0.2)
    start = time.monotonic()
    try:
        with pytest.raises(Exception):
            backend.read('p/a') if operation == 'read' else list(backend.list('p/'))
        assert time.monotonic() - start < 1.5
    finally:
        lock.release()
    assert backend.read('p/a').body == b'original'
    assert list(backend.list('p/')) == ['p/a']
    backend.close()


def test_unused_list_iterator_does_not_hold_lock(tmp_path):
    backend = seed(tmp_path, lock_timeout_s=0.08)
    unused_iterator = backend.list('p/')
    # No iteration, close or garbage collection before another write.
    result = gate.persist(backend, 'p/b', b'new', ctx=create_ctx(), doc_type='system_state')
    assert isinstance(result, OK)
    assert list(unused_iterator) == ['p/a']
    backend.close()


def test_failed_constructor_releases_cas_lock(tmp_path):
    backend = seed(tmp_path)
    with backend._journal_path.open('ab') as fh:
        fh.write(b'KKJ2\x00')
    backend.close()
    try:
        opened = LocalBackend(tmp_path, lock_timeout_s=0.08)
    except Exception:
        pass
    else:
        opened.close()
    lock = _FileLock(tmp_path / 'locks/cas.lock')
    assert lock.acquire(0.2), 'failed constructor leaked cas.lock'
    lock.release()


@pytest.mark.parametrize('tail_kind', ['opaque-only', 'later-legacy', 'torn-v2-fence'])
def test_any_unparsed_suffix_is_uncertain_and_never_rolls_back(tmp_path, tail_kind):
    backend = seed(tmp_path)
    key, body = b'p/a', b'later-value'
    legacy = struct.pack('>I', len(key)) + key + struct.pack('>Q', len(body)) + body + hashlib.sha256(body).digest()
    tail = {
        'opaque-only': b'\x00\x00\x00\x05ab',
        'later-legacy': b'\x00\x00\x00\x05ab' + legacy,
        'torn-v2-fence': b'KKJ2' + legacy + b'\x00\x00',
    }[tail_kind]
    backend._journal_fh.write(tail)
    backend._journal_fh.flush()
    backend.close()
    data = tmp_path / 'data/p/a'
    data.write_bytes(b'later-value')
    journal = (tmp_path / 'journal/journal.log').read_bytes()
    reopened = None
    try:
        try:
            reopened = LocalBackend(tmp_path)
        except Exception:
            pass
        assert data.read_bytes() == b'later-value', 'incomplete scan rewrote published evidence'
        assert (tmp_path / 'journal/journal.log').read_bytes() == journal
        if reopened is not None:
            with pytest.raises(Exception):
                reopened.read('p/a')
            with pytest.raises(Exception):
                list(reopened.list('p/'))
    finally:
        if reopened is not None:
            reopened.close()


@pytest.mark.parametrize('operation', ['read', 'list'])
def test_unjournaled_published_file_is_not_authoritative(tmp_path, operation):
    backend = LocalBackend(tmp_path)
    orphan = tmp_path / 'data/p/orphan'
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b'only-in-materialized-view')
    journal = (tmp_path / 'journal/journal.log').read_bytes()
    try:
        with pytest.raises(Exception):
            backend.read('p/orphan') if operation == 'read' else list(backend.list('p/'))
        assert orphan.read_bytes() == b'only-in-materialized-view'
        assert (tmp_path / 'journal/journal.log').read_bytes() == journal
    finally:
        backend.close()


def test_ambiguous_startup_preserves_old_staging_evidence(tmp_path):
    backend = seed(tmp_path)
    backend._journal_fh.write(b'KKJ2\x00')
    backend._journal_fh.flush()
    backend.close()
    staged = tmp_path / 'staging/recoverable-evidence'
    staged.write_bytes(b'possibly-relevant-body')
    old = time.time() - 3600
    os.utime(staged, (old, old))
    reopened = None
    try:
        try:
            reopened = LocalBackend(tmp_path)
        except Exception:
            pass
        assert staged.exists(), 'ambiguous startup scavenged potential recovery evidence'
        assert staged.read_bytes() == b'possibly-relevant-body'
    finally:
        if reopened is not None:
            reopened.close()
