"""Controlled scheduling probe for issue33, with every worker outcome captured."""
import concurrent.futures,json,pathlib,subprocess,sys,threading,time
repo=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve();out.mkdir(exist_ok=False)
sys.path.insert(0,str(repo))
from store.git_backend import GitBackend
from store import gate
from tests.ctx_helpers import create_ctx,fenced_ctx
remote=out/'remote.git';subprocess.run(['git','init','--quiet','--bare','-b','main',str(remote)],check=True)
backend=GitBackend(out/'git-work',remote,git_timeout_s=10)
base=gate.persist(backend,'race',b'base',ctx=create_ctx(),doc_type='system_state')
barrier=threading.Barrier(8)
original=backend.unlock
first=threading.Event()
def delayed_unlock(lease):
    if not first.is_set():
        first.set()
        time.sleep(6) # deterministic scheduler pause between acquired lease and release
    return original(lease)
backend.unlock=delayed_unlock
def worker(i):
    start=time.monotonic()
    try:
        barrier.wait(timeout=10)
        result=gate.persist(backend,'race',f'writer-{i}'.encode(),ctx=fenced_ctx(backend,'race',base.new_hash),doc_type='system_state')
        return {'writer':i,'kind':type(result).__name__,'elapsed_s':round(time.monotonic()-start,3)}
    except Exception as e:return {'writer':i,'exception':type(e).__name__,'message':str(e),'elapsed_s':round(time.monotonic()-start,3)}
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:rows=list(pool.map(worker,range(8)))
result={'injected_pause_s':6,'helper_budget_s':5,'workers':rows,'final':backend.read('race').body.decode(),'scope':'Controlled scheduling reproduction of the helper budget failure; does not prove the exact timing cause in the original CI runner.'}
(out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))

