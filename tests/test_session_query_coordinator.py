"""Independent regressions first run against the original Q11 delivery."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'skill')]
import knokeep_state as ks
from application.identifiers import valid_id
from application.session_queries import SessionQueryError, list_sessions, read_session
from store.backend import BackendCorruptionError
from store.fake import FakeBackend


@pytest.mark.parametrize('identifier', ['p\n', 'p\r', 'p\r\n', 'p ', '\tp', 'p\t'])
def test_identifier_rejects_whitespace(identifier):
    assert not valid_id(identifier)


def test_bool_limit_rejected():
    with pytest.raises(SessionQueryError):
        list_sessions(FakeBackend(), 'p', limit=True)


@pytest.mark.parametrize('arguments', [
    ['session-read', '--project', 'p'],
    ['session-read', '--project', 'p', '--session-id'],
    ['session-list', '--project', 'p', '--list-limit', 'no'],
    ['session-list', '--project', 'p', '--unexpected'],
])
def test_query_argument_errors_are_json_without_creating_store(tmp_path, arguments):
    store = tmp_path / 'must-not-exist'
    result = subprocess.run([sys.executable, str(ROOT/'skill/knokeep_state.py'),
                             *arguments, '--store', str(store)], capture_output=True, timeout=15)
    assert result.returncode != 0
    assert json.loads(result.stdout or result.stderr)['blocked'] is True
    assert not store.exists()


@pytest.mark.parametrize('query', [
    lambda: ks.session_read('unused', 'p', '../bad'),
    lambda: ks.session_list('unused', 'p', limit=0),
    lambda: ks.session_list('unused', 'p', after='../bad'),
])
def test_invalid_query_does_not_construct_backend(monkeypatch, query):
    constructed = []
    def factory(_):
        constructed.append(True)
        return FakeBackend()
    monkeypatch.setattr(ks, '_backend', factory)
    with pytest.raises((SessionQueryError, SystemExit)):
        query()
    assert constructed == []


@pytest.mark.parametrize('command', ['list', 'read'])
@pytest.mark.parametrize('fail', [False, True])
def test_cli_adapter_closes_owned_backend(monkeypatch, command, fail):
    class OwnedBackend(FakeBackend):
        closed = False
        def close(self):
            self.closed = True
        def read(self, key):
            if fail:
                raise PermissionError('not allowed')
            return super().read(key)
        def list(self, prefix):
            if fail:
                raise BackendCorruptionError('bad journal')
            yield from super().list(prefix)
    backend = OwnedBackend()
    monkeypatch.setattr(ks, '_backend', lambda _: backend)
    try:
        if command == 'list':
            ks.session_list('unused', 'p')
        else:
            ks.session_read('unused', 'p', 's')
    except (PermissionError, BackendCorruptionError):
        assert fail
    assert backend.closed


@pytest.mark.parametrize('error', [PermissionError, BackendCorruptionError])
def test_query_does_not_turn_backend_failure_into_absence(error):
    class BadBackend(FakeBackend):
        def read(self, key):
            raise error('failure')
        def list(self, prefix):
            yield 'p/sessions/a'
            raise error('failure after first key')
    with pytest.raises(error):
        read_session(BadBackend(), 'p', 's')
    with pytest.raises(error):
        list_sessions(BadBackend(), 'p')


@pytest.mark.parametrize('before', [True, False])
@pytest.mark.parametrize('command', ['session-list', 'session-read'])
def test_common_options_may_precede_query_command(tmp_path, before, command):
    common = ['--store', str(tmp_path/'store'), '--project', 'p']
    query = [command] + (['--session-id', 'missing'] if command == 'session-read' else [])
    result = subprocess.run([sys.executable, str(ROOT/'skill/knokeep_state.py'),
                             *(common + query if before else query + common)], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout)
    assert answer['ok'] is True
    if command == 'session-read':
        assert answer['found'] is False
    else:
        assert answer['session_ids'] == [] and answer['truncated'] is False


def test_out_of_range_list_rejects_before_store(tmp_path):
    store = tmp_path/'must-not-exist'
    result = subprocess.run([sys.executable, str(ROOT/'skill/knokeep_state.py'),
                             '--store', str(store), '--project', 'p', 'session-list',
                             '--list-limit', '0'], capture_output=True, timeout=15)
    assert result.returncode == 2
    assert json.loads(result.stdout)['reason'] == 'invalid_limit'
    assert not store.exists()
