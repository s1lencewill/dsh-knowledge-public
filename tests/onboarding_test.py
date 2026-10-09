"""Local import publication tests; neither model nor original-file mutation is allowed."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import onboarding
import evidence
import real_data
os.environ.setdefault("DSH_KNOWLEDGE_TOKEN","synthetic-token-for-service-test")
import service
import source_card
from fastapi.testclient import TestClient

class OnboardingTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.space=Path(self.tmp.name)/'workspace';self.space.mkdir()
  self.root=self.space/'资料 with space';self.root.mkdir()
  (self.root/'note.md').write_text('---\ntitle: Sample note\n---\n条件 298 K 😀。\n',encoding='utf-8')
  self.data=Path(self.tmp.name)/'data';self.data.mkdir()
  self.config=self.data/'libraries.json';self.config.write_text('{"libraries":[]}',encoding='utf-8')
  self.manager=onboarding.Manager(self.config)
  token=patch.object(service,"TOKEN","synthetic-onboarding-token-abcdefghijklmnopqrstuvwxyz");token.start();self.addCleanup(token.stop)
 def preview(self): return self.manager.preview(str(self.space),'session-a',str(self.root))
 def publish(self,p): return self.manager.publish(p['previewId'],str(self.space),'session-a',True)
 def test_preview_cancel_writes_nothing(self):
  before=self.config.read_bytes();p=self.preview()
  self.assertTrue(p['canPublish']);self.assertEqual(p['documents'],1)
  self.manager.cancel(p['previewId'],str(self.space),'session-a')
  self.assertEqual(list(self.data.iterdir()),[self.config]);self.assertEqual(before,self.config.read_bytes())
  with self.assertRaises(onboarding.Refused):self.publish(p)
 def test_publication_bound_read_and_update_originals_unchanged(self):
  raw=(self.root/'note.md').read_bytes();r=self.publish(self.preview());lib=json.loads(self.config.read_text(encoding='utf-8'))['libraries'][0]
  self.assertEqual(raw,(self.root/'note.md').read_bytes());self.assertEqual(real_data.authorize(lib,evidence.EXTENSIONS)[0],True,real_data.authorize(lib,evidence.EXTENSIONS)[1])
  with patch.object(service,'CONFIG',self.config):
   self.assertEqual(service.libraries(str(self.space))[0]['id'],r['libraryId'])
   sibling=self.space/'sibling';sibling.mkdir();self.assertEqual(service.libraries(str(sibling)),[])
  old_root=Path(lib['root']);old_bytes=next(old_root.iterdir()).read_bytes()
  (self.root/'note.md').write_text('updated text。',encoding='utf-8');self.assertFalse(real_data.authorize(lib,evidence.EXTENSIONS)[0])
  p=self.manager.preview(str(self.space),'session-a',library_id=r['libraryId']);self.assertEqual(p['diff']['changed'],['note.md'])
  self.publish(p);new=json.loads(self.config.read_text(encoding='utf-8'))['libraries'][0]
  self.assertNotEqual(new['root'],lib['root']);self.assertEqual(next(old_root.iterdir()).read_bytes(),old_bytes)
  self.assertTrue(real_data.authorize(new,evidence.EXTENSIONS)[0])
 def test_changed_source_and_config_require_new_preview(self):
  p=self.preview();(self.root/'new.md').write_text('new')
  with self.assertRaisesRegex(onboarding.Refused,'SOURCE_CHANGED'):self.publish(p)
  p=self.preview();self.config.write_text('{"libraries":[],"other":true}')
  with self.assertRaisesRegex(onboarding.Refused,'CONFIG_CHANGED'):self.publish(p)
 def test_consent_owner_and_publish_lock_fail_closed(self):
  p=self.preview()
  with self.assertRaisesRegex(onboarding.Refused,'CONSENT_REQUIRED'):self.manager.publish(p['previewId'],str(self.space),'session-a',False)
  with self.assertRaisesRegex(onboarding.Refused,'PREVIEW_FOREIGN'):self.manager.publish(p['previewId'],str(self.space),'session-b',True)
  lock=self.config.with_name(self.config.name+'.publish-lock');lock.write_text('held')
  with self.assertRaisesRegex(onboarding.Refused,'PUBLISH_BUSY'):self.publish(p)
 def test_limit_encoding_and_outside_workspace(self):
  (self.root/'large.md').write_bytes(b'a'*2_000_001)
  (self.root/'bad.md').write_bytes(b'\xff')
  p=self.preview();self.assertFalse(p['canPublish'])
  self.assertEqual({v['reason'] for v in p['problems']},{'FILE_TOO_LARGE','NOT_UTF8'})
  with self.assertRaisesRegex(onboarding.Refused,'PREFLIGHT_FAILED'):self.publish(p)
  with self.assertRaisesRegex(onboarding.Refused,'FOLDER_OUTSIDE_WORKSPACE'):self.manager.preview(str(self.root),'session-a',str(self.space))
 def test_mid_publish_failure_does_not_replace_previous_config(self):
  self.publish(self.preview());before=self.config.read_bytes();lib=json.loads(before)['libraries'][0]
  p=self.manager.preview(str(self.space),'session-a',library_id=lib['id'])
  with patch.object(onboarding.os,'replace',side_effect=OSError('synthetic failure')):
   with self.assertRaises(OSError):self.publish(p)
  self.assertEqual(self.config.read_bytes(),before);self.assertTrue(real_data.authorize(lib,evidence.EXTENSIONS)[0])
 def test_http_import_list_catalogue_read_without_model(self):
  with patch.object(service,'CONFIG',self.config),patch.object(service,'ONBOARDING',self.manager),patch.object(service.model_proxy,'provider_from_environment',side_effect=AssertionError('model forbidden')):
   client=TestClient(service.app);headers={'Authorization':'Bearer '+service.TOKEN}
   body={'workspace':str(self.space),'session':'session-a'}
   self.assertEqual(client.post('/library-admin',json={**body,'action':'list'}).status_code,401)
   p=client.post('/library-admin',headers=headers,json={**body,'action':'preview','root':str(self.root)}).json()
   r=client.post('/library-admin',headers=headers,json={**body,'action':'publish','previewId':p['previewId'],'consent':True}).json()
   listed=client.post('/library-admin',headers=headers,json={**body,'action':'list'}).json();self.assertEqual(listed['libraries'][0]['documents'],1)
   catalogue=client.post('/catalogue',headers=headers,json={**body,'libraryId':r['libraryId']}).json()
   entry=catalogue['entries'][0];self.assertTrue(entry['readable'])
   read=client.post('/reads',headers=headers,json={**body,'libraryId':r['libraryId'],'fileHandle':entry['fileHandle'],'sourceVersion':entry['sourceVersion'],'startLine':1}).json()
   text=next(Path(json.loads(self.config.read_text(encoding='utf-8'))['libraries'][0]['root']).iterdir()).read_bytes().decode('utf-8-sig').replace('\r\n','\n')
   self.assertEqual(text[read['startChar']:read['endChar']],read['excerpt'])
 def test_mixed_pdf_mapping_and_scanned_pdf_exclusion(self):
  from pypdf import PdfWriter
  from pypdf.generic import NameObject,DictionaryObject,DecodedStreamObject
  writer=PdfWriter();page=writer.add_blank_page(600,800)
  font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
  page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
  content=DecodedStreamObject();content.set_data(b'BT /F1 12 Tf 50 700 Td (Temperature 298 K.) Tj ET');page[NameObject('/Contents')]=writer._add_object(content)
  writer.write(self.root/'sample.pdf')
  blank=PdfWriter();blank.add_blank_page(600,800);blank.write(self.root/'scan.pdf')
  before=onboarding.original_manifest(self.root);p=self.preview()
  self.assertEqual(p['documents'],2);self.assertIn('SCANNED_PDF_NO_OCR',{v['reason'] for v in p['excluded']})
  self.publish(p);lib=json.loads(self.config.read_text(encoding='utf-8'))['libraries'][0]
  records=json.loads(Path(lib['catalogue']).read_text(encoding='utf-8'))['documents'];record=next(v for v in records.values() if v['fileName']=='sample.pdf')
  self.assertIsNone(source_card.usable_page_map(record)[1]);self.assertEqual(record['pages'],1)
  self.assertEqual(before,onboarding.original_manifest(self.root))
 def test_unregister_preserves_originals_versions_and_backup(self):
  result=self.publish(self.preview());lib=json.loads(self.config.read_text(encoding='utf-8'))['libraries'][0]
  before=onboarding.original_manifest(self.root)
  self.manager.unregister(result['libraryId'],str(self.space),True)
  self.assertEqual(json.loads(self.config.read_text(encoding='utf-8'))['libraries'],[])
  self.assertTrue(Path(lib['root']).is_dir());self.assertEqual(onboarding.original_manifest(self.root),before)
  self.assertGreaterEqual(len(list((self.data/'backups').glob('*/libraries.json'))),2)
 def test_expired_preview_and_linked_root_refused(self):
  clock=[0];self.manager.clock=lambda:clock[0];p=self.preview();clock[0]=901
  with self.assertRaisesRegex(onboarding.Refused,'PREVIEW_EXPIRED'):self.publish(p)
  with patch.object(onboarding.evidence,'linked',return_value=True):
   with self.assertRaisesRegex(onboarding.Refused,'LINK_NOT_ALLOWED'):self.preview()
 def test_import_cannot_scan_its_own_managed_storage(self):
  with self.assertRaisesRegex(onboarding.Refused,'FOLDER_OVERLAPS_STORAGE'):
   self.manager.preview(str(Path(self.tmp.name)),'session-a',str(Path(self.tmp.name)))
if __name__=='__main__':unittest.main()
