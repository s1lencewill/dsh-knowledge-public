import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import container_entry
os.environ.setdefault("DSH_KNOWLEDGE_TOKEN","synthetic-token-for-service-test")
import service
import diagnostics
class EntryCheckpointTests(unittest.TestCase):
 def test_unknown_or_forged_marker_is_never_log_text(self):
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'entry-progress.json'
   self.assertEqual(service.worker_checkpoint(folder),'UNKNOWN')
   for value in ['private source text','RESULT_WRITE',{},['ENGINE_RUN']]:
    path.write_text(json.dumps({'stage':value}),encoding='utf-8')
    self.assertEqual(service.worker_checkpoint(folder),'RESULT_WRITE' if value=='RESULT_WRITE' else 'UNKNOWN')
 def test_unserializable_engine_return_is_explicit_failure_and_checkpoint_survives(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);config=root/'request.json';config.write_text(json.dumps({'stateDir':folder,'request':{}}),encoding='utf-8')
   async def run(_):return {'status':'partial','unsafe':object()}
   import asyncio.windows_utils as windows_utils
   with patch.object(windows_utils,'_winapi',windows_utils._winapi),patch.object(sys,'argv',['container_entry.py',str(config)]),patch.dict(sys.modules,{'worker':types.SimpleNamespace(run=run)}),patch.dict(container_entry.os.environ):
    self.assertEqual(container_entry.main(),0)
   result=json.loads((root/'result.json').read_text());self.assertEqual(result['status'],'failed')
   self.assertEqual(result['warnings'],['CONTAINER_RESULT_SERIALIZE_FAILED'])
   self.assertEqual(service.worker_checkpoint(root),'RESULT_WRITTEN')
   sink=diagnostics.Diagnostics(root/'diagnostics.jsonl');sink.stage('worker_checkpoint','job-test',status=service.worker_checkpoint(root))
   row=json.loads((root/'diagnostics.jsonl').read_text());self.assertEqual(row['status'],'RESULT_WRITTEN')

 def test_pipe_namespace_adapter_handles_new_and_old_name_generators(self):
  from unittest.mock import Mock
  api=Mock(); module=types.SimpleNamespace(_winapi=api)
  container_entry.install_local_pipe_namespace(module)
  prefix='\\\\.\\pipe\\'
  for name in ['python-pipe-123-0-random','tmp-old-generator']:
   module._winapi.CreateNamedPipe(prefix+name,1)
   module._winapi.CreateFile(prefix+name,2)
   api.CreateNamedPipe.assert_called_with(prefix+'LOCAL\\'+name,1)
   api.CreateFile.assert_called_with(prefix+'LOCAL\\'+name,2)
  module._winapi.CreateFile(prefix+'LOCAL\\existing',3)
  api.CreateFile.assert_called_with(prefix+'LOCAL\\existing',3)
  self.assertIs(module._winapi.CloseHandle,api.CloseHandle)
 def test_real_asyncio_subprocess_works_after_namespace_adapter(self):
  import asyncio.windows_utils as windows_utils
  async def run():
   proc=await asyncio.create_subprocess_exec(sys.executable,'-c','print(42)',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
   out,err=await asyncio.wait_for(proc.communicate(),15)
   self.assertEqual(proc.returncode,0,err);self.assertEqual(out.strip(),b'42')
  with patch.object(windows_utils,'_winapi',windows_utils._winapi):
   container_entry.install_local_pipe_namespace(windows_utils)
   asyncio.run(run())
if __name__=='__main__':unittest.main()
