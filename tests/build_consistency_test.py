import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import release_identity as identity
import os
os.environ.setdefault('DSH_KNOWLEDGE_TOKEN','synthetic-token-for-service-test')
os.environ['DSH_KNOWLEDGE_ISOLATION']='off'
import service
from fastapi import HTTPException
from starlette.requests import Request
class BuildConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_header_refuses_before_handler_and_cleanup_remains_available(self):
        build={'id':'a'*64,'valid':True}
        def req(path,method='POST',header=None):
            return Request({'type':'http','path':path,'method':method,'headers':([] if header is None else [(b'x-knowledge-build',header.encode())]),'query_string':b''})
        with patch.object(service,'TOKEN','synthetic-token-with-at-least-24-characters'),patch.object(service,'RUNNING_BUILD',build),patch.object(identity,'runtime_status',return_value={'status':'verified'}) as runtime:
            auth='Bearer '+service.TOKEN
            with self.assertRaises(HTTPException) as e:await service.authenticated(req('/reads'),auth)
            self.assertEqual(e.exception.detail,'BUILD_MISMATCH');runtime.assert_not_called()
            await service.authenticated(req('/reads',header='a'*64),auth)
            await service.authenticated(req('/queries/owned','DELETE'),auth)
            await service.authenticated(req('/queries/owned','GET'),auth)
            await service.authenticated(req('/health','GET'),auth)
    async def test_stale_runtime_blocks_matching_host(self):
        with patch.object(service,'TOKEN','synthetic-token-with-at-least-24-characters'),patch.object(service,'RUNNING_BUILD',{'id':'a'*64,'valid':True}),patch.object(identity,'runtime_status',return_value={'status':'stale'}):
            request=Request({'type':'http','path':'/queries','method':'POST','headers':[(b'x-knowledge-build',b'a'*64)],'query_string':b''})
            with self.assertRaises(HTTPException) as e:await service.authenticated(request,'Bearer '+service.TOKEN)
            self.assertEqual(e.exception.detail,'RUNTIME_BUILD_MISMATCH')
    async def test_release_identity_missing_is_invalid_not_development(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);self.assertIsNone(identity.capture(p));(p/'RELEASE-MANIFEST.json').write_text('{}');self.assertEqual(identity.capture(p),{'valid':False})
    async def test_runtime_inputs_must_match_and_full_stage_verification_is_required(self):
        b={'valid':True,'runtimeInputs':{'engine/worker.py':'a'*64}}
        with patch.object(identity.staging,'read',return_value={'inputs':{'engine/worker.py':'b'*64}}),patch.object(identity.staging,'verify') as verify:
            self.assertEqual(identity.runtime_status(b,'runtime','project',True)['status'],'stale');verify.assert_not_called()
        with patch.object(identity.staging,'read',return_value={'inputs':b['runtimeInputs']}),patch.object(identity.staging,'verify',return_value=(False,'stale tool')):
            self.assertEqual(identity.runtime_status(b,'runtime','project',True)['status'],'stale')
