"""Bounded, disposable local fault probes. No production data or source edits."""
import hashlib,io,json,os,pathlib,struct,subprocess,sys,time
ROOT=pathlib.Path(__file__).resolve().parent
PRODUCT=pathlib.Path(__file__).resolve().parents[4]
sys.path[:0]=[str(PRODUCT),str(PRODUCT/'skill')]
import knokeep_state as ks
from store import gate
from store.context import create_ctx
from store.local import LocalBackend
from store.types import OK

if len(sys.argv)>1:
    mode,folder=sys.argv[1:3]
    if mode in ('crash-create','crash-append'):
        original=LocalBackend._journal_append
        def commit_then_die(self,*a,**kw):
            original(self,*a,**kw)
            os._exit(73)
        LocalBackend._journal_append=commit_then_die
        if mode=='crash-create':
            gate.persist(LocalBackend(folder),'probe/state',b'durable before acknowledgement',ctx=create_ctx(),doc_type='system_state')
        else:
            ks._LEASE_TTL_S=0.1
            ks.session_append(folder,'probe','session-a','test','verified task Alpha')
    elif mode=='writer':
        ident,expected,trigger=sys.argv[3:]
        end=time.monotonic()+8
        while not pathlib.Path(trigger).exists():
            if time.monotonic()>end:raise RuntimeError('barrier timeout')
            time.sleep(.01)
        try:
            print(json.dumps(ks.flush_log(folder,'race',f'## Completed & Verified\nwriter {ident}\n## Active State\nwriter {ident}\n## Next Step\nnext {ident}\n',expected,session=ident,client='test')))
        except SystemExit as exc:
            print(str(exc));sys.exit(1)
    sys.exit(0)

out=pathlib.Path.cwd()/'q3-probe-output';out.mkdir(exist_ok=False)
results={}
def child(mode,folder):
    t=time.monotonic();p=subprocess.run([sys.executable,__file__,mode,str(folder)],capture_output=True,timeout=12)
    return {'os_exit_code':p.returncode,'elapsed_s':round(time.monotonic()-t,3),'stdout':p.stdout.decode(),'stderr':p.stderr.decode()}

raw=(ROOT/'source-journal.bin').read_bytes()
stream=io.BytesIO(raw);prefix_end=None;target='68c736be709c57740c6343b976e8f7961ac879ac86f6167528d216c633b495e8'
while stream.tell()<len(raw):
    marker=stream.read(4);v2=marker==b'KKJ2'; klen=struct.unpack('>I',stream.read(4) if v2 else marker)[0]
    key=stream.read(klen).decode();blen=struct.unpack('>Q',stream.read(8))[0];body=stream.read(blen);digest=stream.read(32).hex()
    if v2:stream.read(8)
    assert hashlib.sha256(body).hexdigest()==digest
    if digest==target:prefix_end=stream.tell();break
assert prefix_end
store=out/'intermediate-store';(store/'journal').mkdir(parents=True)
(store/'journal/journal.log').write_bytes(raw[:prefix_end])
b=LocalBackend(store);state=b.read('q1-cc-a2/system_state');assert state.version_hash==target;b.close()
boot=ks.bootstrap(str(store),'q1-cc-a2')
results['intermediate']={'accepted_hash':target,'prefix_bytes':prefix_end,'bootstrap':boot,'state':state.body.decode(),'semantic_warning_present':bool(boot.get('section_warnings')),'scope':'Exact accepted-prefix replay, not an actual process interruption at this source milestone.'}

folder=out/'crash-create';cr=child('crash-create',folder);assert cr['os_exit_code']==73
b=LocalBackend(folder);blob=b.read('probe/state');before=(folder/'journal/journal.log').read_bytes();retry=gate.persist(b,'probe/state',b'durable before acknowledgement',ctx=create_ctx(),doc_type='system_state');after=(folder/'journal/journal.log').read_bytes();b.close()
results['crash_after_commit_create']={**cr,'recovered_body':blob.body.decode(),'retry_result':type(retry).__name__,'journal_unchanged_on_retry':before==after}
assert isinstance(retry,OK) and before==after

folder=out/'crash-append';cr=child('crash-append',folder);assert cr['os_exit_code']==73
time.sleep(.2)
first=LocalBackend(folder);before=first.read('probe/sessions/session-a').body;first.close()
retry=ks.session_append(str(folder),'probe','session-a','test','verified task Alpha')
second=LocalBackend(folder);after=second.read('probe/sessions/session-a').body;second.close()
results['crash_after_commit_append']={**cr,'retry':retry,'before_count':before.count(b'verified task Alpha'),'after_count':after.count(b'verified task Alpha'),'before':before.decode(),'after':after.decode(),'test_lease_ttl_s':.1,'duplicate_observed':after.count(b'verified task Alpha')==2}
assert after.count(b'verified task Alpha')==2

folder=out/'two-writers';ks.init(str(folder),'race',client='test');expected=ks.bootstrap(str(folder),'race')['log_hash'];trigger=out/'go'
procs=[subprocess.Popen([sys.executable,__file__,'writer',str(folder),ident,expected,str(trigger)],stdout=subprocess.PIPE,stderr=subprocess.PIPE) for ident in ('a','b')]
trigger.write_text('go')
rows=[]
for p in procs:
    try:so,se=p.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        p.kill();so,se=p.communicate(timeout=3)
    rows.append({'os_exit_code':p.returncode,'stdout':so.decode(),'stderr':se.decode()})
boot=ks.bootstrap(str(folder),'race')
backend=LocalBackend(folder);bodies={k:backend.read(k).body.decode() for k in backend.list('race/')};backend.close()
results['two_process_stale_writer']={'workers':rows,'bootstrap':boot,'documents':bodies}
(out/'results.json').write_text(json.dumps(results,indent=2)+'\n')
print(json.dumps({'crash_create_recovered':True,'create_retry_idempotent':True,'append_retry_duplicate':True,'intermediate_bootstrap_warned':results['intermediate']['semantic_warning_present'],'race_conflicts':boot['conflict_count'],'worker_exit_codes':[r['os_exit_code'] for r in rows]}))

