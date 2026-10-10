import hashlib
import json
from pathlib import Path
import staging

HEX = set('0123456789abcdef')
def _hash(value):
    return isinstance(value,str) and len(value)==64 and set(value)<=HEX

def capture(project):
    """Freeze running code's identity. A digest is a consistency check, not a signature."""
    project=Path(project)
    if not (project/'RELEASE-MANIFEST.json').is_file(): return None
    try:
        b=json.loads((project/'lib/build-identity.json').read_text(encoding='utf-8'))
        if b['schemaVersion']!=1 or b['protocol']!=1 or not _hash(b['id']): raise ValueError()
        if not isinstance(b['runtimeInputs'],dict) or not b['runtimeInputs']:raise ValueError()
        if not all(isinstance(p,str) and _hash(h) for p,h in b['runtimeInputs'].items()):raise ValueError()
        if not isinstance(b['files'],dict) or not b['files']:raise ValueError()
        for path,wanted in b['files'].items():
            if not isinstance(path,str) or not _hash(wanted):raise ValueError()
            if path.startswith('engine/') and (Path(path).name!=path.removeprefix('engine/') or hashlib.sha256((project/path).read_bytes()).hexdigest()!=wanted):raise ValueError()
        return {'id':b['id'],'version':b['version'],'protocol':b['protocol'],'runtimeInputs':dict(b['runtimeInputs']),'valid':True}
    except (OSError,ValueError,KeyError,TypeError): return {'valid':False}

def runtime_status(build,runtime,project,active):
    if build is None:return {'status':'development'}
    if not build.get('valid'):return {'status':'build-invalid'}
    recorded=staging.read(runtime)
    if recorded is None:return {'status':'missing'}
    if not active:return {'status':'not-active'}
    expected=build['runtimeInputs']
    if any(recorded.get('inputs',{}).get(p)!=h for p,h in expected.items()):return {'status':'stale'}
    try:ok,_=staging.verify(runtime,project)
    except Exception:ok=False
    return {'status':'verified' if ok else 'stale','sourceInputs':expected}
