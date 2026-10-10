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
