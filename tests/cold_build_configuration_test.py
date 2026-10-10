import os,runpy,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
class ColdBuildConfigurationTests(unittest.TestCase):
    def load(self):
        return runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/build-rga-patched.py'))
    def test_no_implicit_developer_loopback_proxy(self):
        with patch.dict(os.environ,{},clear=True):
            builder=self.load();env=builder['build_environment']()
            self.assertNotIn('HTTPS_PROXY',env);self.assertNotIn('HTTP_PROXY',env)
    def test_explicit_proxy_is_respected(self):
        with patch.dict(os.environ,{'HTTPS_PROXY':'http://synthetic-proxy.invalid:8888'},clear=True):
            env=self.load()['build_environment']();self.assertEqual(env['HTTP_PROXY'],'http://synthetic-proxy.invalid:8888')
    def test_explicit_toolchain_is_checked_and_missing_one_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'vcvars64.bat';path.write_text('synthetic',encoding='utf-8')
            with patch.dict(os.environ,{'DSH_KNOWLEDGE_VCVARS':str(path)},clear=True):self.assertEqual(self.load()['find_vcvars'](),path)
            with patch.dict(os.environ,{'DSH_KNOWLEDGE_VCVARS':str(path)+'-missing'},clear=True):self.assertIsNone(self.load()['find_vcvars']())
    def test_cargo_uses_native_cmd_form_and_paths_are_environment_data(self):
        calls=[]
        with patch.dict(os.environ,{},clear=True):
            builder=self.load();run=builder['run_cargo'];g=run.__globals__;g['run']=lambda *a,**kw:calls.append((a,kw));run(r'C:\VS space\vcvars64.bat',r'C:\source space\Cargo.toml',('pcre2',))
        command=calls[0][0][0];env=calls[0][1]['env'];self.assertIsInstance(command,str);self.assertTrue(command.startswith('cmd /d /s /v:off /c '));self.assertNotIn('VS space',command);self.assertEqual(env['DSH_KNOWLEDGE_BUILD_MANIFEST'],r'C:\source space\Cargo.toml');self.assertNotIn('HTTPS_PROXY',env)
    def test_recorded_license_and_effective_patch_bytes_are_distinct_and_bound(self):
        import hashlib,json
        root=Path(__file__).resolve().parents[1];record=json.loads((root/'tools/rga/SOURCE.json').read_text(encoding='utf-8'))
        license=(root/'tools/rga/LICENSE.md').read_bytes();patch_bytes=(root/'tools/rga/cache-path-fallback.patch').read_bytes()
        self.assertEqual(hashlib.sha256(license).hexdigest(),record['licenseSHA256']);self.assertEqual(hashlib.sha256(license.replace(b'\r\n',b'\n')).hexdigest(),record['upstreamLicenseSHA256'])
        self.assertEqual(hashlib.sha256(patch_bytes).hexdigest(),record['patchSHA256']);self.assertEqual(hashlib.sha256(patch_bytes.replace(b'\r\n',b'\n')).hexdigest(),record['appliedPatchSHA256'])
