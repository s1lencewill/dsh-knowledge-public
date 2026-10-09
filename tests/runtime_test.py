import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from runtime import (normalize_no_proxy, normalize_proxy_environment, prepare_environment,
                     model_settings, safe_error, child_environment, executable)


class RuntimeTests(unittest.TestCase):
    def test_proxy_preserves_domains_ipv4_ipv6_and_wildcard(self):
        value = "localhost,127.0.0.1,.example.com,::1,[::1],[2001:db8::1],*"
        self.assertEqual(normalize_no_proxy(value), "localhost,127.0.0.1,.example.com,::1,::1,2001:db8::1,*")

    def test_installed_openai_client_accepts_normalized_ipv6(self):
        from openai import OpenAI
        from httpx2 import InvalidURL
        env = {"NO_PROXY": "localhost,[::1]", "no_proxy": "localhost,[::1]"}
        # Preserve Windows SystemRoot/SSL settings; only replace the proxy bypass list.
        with patch.dict(os.environ, env):
            with self.assertRaises(InvalidURL):
                OpenAI(api_key="synthetic", base_url="http://127.0.0.1:9/v1")
            os.environ.update({key:normalize_no_proxy(value) for key,value in env.items()})
            client = OpenAI(api_key="synthetic", base_url="http://127.0.0.1:9/v1")
            client.close()  # No network call was made.

    def test_empty_filename_model_uses_inert_defaults(self):
        result = model_settings("FILENAME_ONLY", {"LLM_BASE_URL":"", "LLM_MODEL_NAME":"", "LLM_API_KEY":""})
        self.assertEqual(result["base_url"], "http://127.0.0.1:9/v1")
        self.assertTrue(result["api_key"])

    def test_filename_probe_ignores_inherited_live_model(self):
        result = model_settings("FILENAME_ONLY", {"LLM_BASE_URL":"https://example.com", "LLM_MODEL_NAME":"live", "LLM_API_KEY":"secret"})
        self.assertNotIn("secret", str(result))
        self.assertEqual(result["model"], "not-configured")

    def test_fast_rejects_empty_and_whitespace_settings(self):
        with self.assertRaisesRegex(RuntimeError, "MODEL_NOT_CONFIGURED"):
            model_settings("FAST", {"LLM_BASE_URL":" ", "LLM_MODEL_NAME":"m", "LLM_API_KEY":"k"})

    def test_prepare_both_tools_without_mutating_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rg = root / "rg.exe"
            rga = root / "rga.exe"
            rg.touch()
            rga.touch()
            env = {"PATH":"", "DSH_KNOWLEDGE_RG":str(rg), "DSH_KNOWLEDGE_RGA":str(rga),
                   "NO_PROXY":"[::1]", "no_proxy":"[::1]", "HTTPS_PROXY":"http://proxy:8000"}
            with patch("runtime.shutil.which", return_value=None):
                prepared = prepare_environment(env, root, root)
            self.assertEqual(prepared["NO_PROXY"], "::1")
            self.assertEqual(prepared["no_proxy"], "::1")
            self.assertEqual(prepared["HTTPS_PROXY"], env["HTTPS_PROXY"])
            self.assertEqual(env["PATH"], "")
            self.assertEqual(env["NO_PROXY"], "[::1]")
            self.assertIn(str(root), prepared["PATH"])

    def test_missing_executable_is_actionable(self):
        with tempfile.TemporaryDirectory() as temporary, patch("runtime.shutil.which", return_value=None):
            root = Path(temporary)
            with self.assertRaisesRegex(RuntimeError, "DSH_KNOWLEDGE_RG"):
                prepare_environment({"PATH":""}, root, root)

    def test_explicit_invalid_path_does_not_fall_back(self):
        with self.assertRaisesRegex(RuntimeError, "absolute executable path"):
            prepare_environment({"DSH_KNOWLEDGE_RG":"relative.exe"})

    def test_the_projects_pinned_copy_wins_over_path(self):
        # The staged runtime is identified by tool hashes, so a PATH tool that changes outside the
        # project must not be able to redefine the runtime.
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            pinned = project / ".local/tools/rg.exe"
            pinned.parent.mkdir(parents=True)
            pinned.write_bytes(b"pinned")
            elsewhere = project / "path-dir"
            elsewhere.mkdir()
            (elsewhere / "rg.exe").write_bytes(b"from-path")
            self.assertEqual(executable("rg", {"PATH": str(elsewhere)}, project, project), pinned)

    def test_an_explicit_override_still_wins_over_the_pinned_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            pinned = project / ".local/tools/rg.exe"
            pinned.parent.mkdir(parents=True)
            pinned.write_bytes(b"pinned")
            chosen = project / "chosen-rg.exe"
            chosen.write_bytes(b"chosen")
            environment = {"PATH": "", "DSH_KNOWLEDGE_RG": str(chosen)}
            self.assertEqual(executable("rg", environment, project, project), chosen)

    def test_the_controlled_rga_build_is_preferred_over_the_official_one(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            patched = project / ".local/tools/rga-patched/rga.exe"
            patched.parent.mkdir(parents=True)
            patched.write_bytes(b"patched")
            official = project / ".local/tools/ripgrep_all-v0.10.9/rga.exe"
            official.parent.mkdir(parents=True)
            official.write_bytes(b"official")
            self.assertEqual(executable("rga", {"PATH": ""}, project, project), patched)

    def test_without_a_pinned_copy_the_search_still_falls_back(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            project.mkdir(exist_ok=True)
            elsewhere = project / "bin"
            elsewhere.mkdir()
            found = elsewhere / "rg.exe"
            found.write_bytes(b"from-path")
            self.assertEqual(executable("rg", {"PATH": str(elsewhere)}, project, project), found)

    def test_worker_child_gets_startup_utf8_mode(self):
        # Sirchmunk reads sources with the locale encoding; only start-up can enable UTF-8 mode.
        source = {"PATH": "x", "PYTHONUTF8": "0"}
        prepared = child_environment(source)
        self.assertEqual(prepared["PYTHONUTF8"], "1")
        self.assertEqual(prepared["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(source["PYTHONUTF8"], "0", "the parent environment must not be mutated")

    def test_worker_child_is_denied_user_site_packages(self):
        # A user-level site-packages can shadow the interpreter's own dependencies (see
        # appcontainer.container_environment); the worker must import only from its environment.
        self.assertEqual(child_environment({})["PYTHONNOUSERSITE"], "1")

    def test_every_proxy_spelling_is_normalised_for_http_clients(self):
        # httpx and httpx2 both fail to build a client while "[::1]" is in the bypass list.
        source = {"NO_PROXY": "localhost,[::1]", "no_proxy": "a,[2001:db8::1]", "HTTPS_PROXY": "http://proxy:8000"}
        prepared = normalize_proxy_environment(source)
        self.assertEqual(prepared["NO_PROXY"], "localhost,::1")
        self.assertEqual(prepared["no_proxy"], "a,2001:db8::1")
        self.assertEqual(prepared["HTTPS_PROXY"], "http://proxy:8000")
        self.assertEqual(source["NO_PROXY"], "localhost,[::1]", "the caller's environment is not mutated")

    def test_worker_child_environment_also_normalises_the_proxy_list(self):
        prepared = child_environment({"NO_PROXY": "[::1]", "no_proxy": "[::1]"})
        self.assertEqual(prepared["NO_PROXY"], "::1")
        self.assertEqual(prepared["no_proxy"], "::1")

    def test_proxy_error_does_not_echo_secret(self):
        from httpx2 import InvalidURL
        output = safe_error(InvalidURL("https://user:secret@example.com"))
        self.assertIn("MODEL_OR_PROXY_URL_INVALID", output)
        self.assertNotIn("secret", output)


if __name__ == "__main__":
    unittest.main()
