"""Independent review cases: limits must not create an unreadable accepted head."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from experiments.checkpoint_v1 import checkpoint as cp
from store.local import LocalBackend


def proposal(mid, receipt=None, **kwargs):
    pred = None if receipt is None else {'milestone_id': receipt['milestone_id'], 'sha256': receipt['checkpoint_sha256']}
    return cp.build_proposal(milestone_id=mid, writer='reviewer', predecessor=pred,
                             state=kwargs.pop('state', 'state'), log='log', **kwargs)


def test_history_limit_preserves_last_readable_head(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, 'MAX_HISTORY', 2)
    be = LocalBackend(tmp_path)
    one = cp.save(be, 'p', proposal('one'))
    two = cp.save(be, 'p', proposal('two', one))
    with pytest.raises(cp.CheckpointError, match='history'):
        cp.save(be, 'p', proposal('three', two))
    assert cp.resume(be, 'p')['milestone_id'] == 'two'


@pytest.mark.parametrize('changes,gen', [({'version': True}, 1), ({'version':2},1), ({'depth':True},1), ({'depth':0},1), ({'depth':2},2), ({},99), ({'sha256':False},1)])
def test_corrupt_head_metadata_fails_closed(tmp_path, changes, gen):
    be = LocalBackend(tmp_path)
    cp.save(be, 'p', proposal('one'))
    path = tmp_path/'data/p/checkpoints_v1/HEAD'
    head = json.loads(path.read_bytes().split(b'\n',1)[1])
    head.update(changes)
    path.write_bytes(f'#knokeep-gen:{gen}\n'.encode()+cp._canon(head))
    with pytest.raises(cp.CheckpointError):
        cp.resume(be, 'p')


@pytest.mark.parametrize('text', ['nul\x00hidden', 'format\u202ehidden', 'sk-ant-' + 'A'*20])
def test_decoded_text_cannot_bypass_gate_with_json_escapes(tmp_path, text):
    be = LocalBackend(tmp_path)
    payload = json.dumps(proposal('bad', state=text),ensure_ascii=True).encode()
    with pytest.raises(cp.CheckpointError):
        cp.save(be, 'p', cp._strict_loads(payload))
    assert cp.lookup(be, 'p', 'bad')['status'] == 'not_found'


def test_duplicate_open_question_ids_rejected_before_staging(tmp_path):
    be = LocalBackend(tmp_path)
    q = {'id':'q','question':'Which policy?','affected_action_ids':['next']}
    with pytest.raises(cp.CheckpointError):
        cp.save(be, 'p', proposal('bad', open_questions=[q,copy.deepcopy(q)]))
    assert cp.lookup(be, 'p', 'bad')['status'] == 'not_found'


def test_resolution_must_reference_retained_staged_proposal(tmp_path):
    be = LocalBackend(tmp_path)
    one = cp.save(be, 'p', proposal('one'))
    with pytest.raises(cp.CheckpointError):
        cp.save(be, 'p', proposal('invalid', one, resolves_staged=[{'milestone_id':'missing','sha256':'0'*64}]))
    assert cp.lookup(be, 'p', 'invalid')['status'] == 'not_found'
    stale = cp.save(be, 'p', proposal('loser'))
    assert stale['status'] == 'staged_stale'
    cp.save(be, 'p', proposal('resolution', one, resolves_staged=[{'milestone_id':'loser','sha256':stale['checkpoint_sha256']}]))
    resumed = cp.resume(be, 'p')
    assert 'loser' not in resumed['staged']
    assert resumed['resolved_staged'] == ['loser']
    assert cp.lookup(be, 'p', 'loser')['status'] == 'resolved_staged'


def test_cli_save_resume_lookup(tmp_path):
    pin = tmp_path/'proposal.json'; pin.write_text(json.dumps(proposal('one')))
    prefix = [sys.executable,'-m','experiments.checkpoint_v1.checkpoint','--store',str(tmp_path/'store'),'--project','p']
    for args, expected in ((['save','--input',str(pin)],'accepted'),(['resume'],'ok'),(['lookup','--milestone-id','one'],'accepted')):
        r = subprocess.run(prefix+args,capture_output=True,text=True,timeout=15)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)['status'] == expected
