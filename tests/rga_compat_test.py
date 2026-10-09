"""rga cache compatibility: every Sirchmunk rga invocation must carry a writable cache path.

Sirchmunk 0.1.0 only passes `--rga-cache-path` on the keyword path; the async filename path calls
rga bare. Inside an AppContainer rga then dies with "could not get cache path: no home directory
found" (exit 101) and retrieval silently degrades to the rg fallback.
"""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import sirchmunk_compat
from sirchmunk.retrieve.text_retriever import GrepRetriever


class RgaCacheCompatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / "rga-cache"
        self.saved = {name: GrepRetriever.__dict__.get(name) for name in
                      ("_run_rga", "_run_search_process", "_dsh_rga_cache_flag")}

        def restore():
            for name, value in self.saved.items():
                if value is None:
                    if hasattr(GrepRetriever, name):
                        delattr(GrepRetriever, name)
                else:
                    setattr(GrepRetriever, name, value)
        self.addCleanup(restore)

    def test_helper_injects_once_and_respects_an_explicit_policy(self):
        flag = sirchmunk_compat.RGA_CACHE_PREFIX + "C:\\cache"
        self.assertEqual(sirchmunk_compat.with_rga_cache(["--files", "."], flag), ["--files", ".", flag])
        for existing in ([flag], ["--rga-no-cache"]):
            self.assertEqual(sirchmunk_compat.with_rga_cache(list(existing), flag), existing,
                             "an explicit cache policy must win over the default")

    def test_installation_is_idempotent_and_creates_the_directory(self):
        first = sirchmunk_compat.install_rga_cache_path(self.cache)
        wrapper = GrepRetriever._run_search_process
        second = sirchmunk_compat.install_rga_cache_path(self.cache)
        self.assertEqual(first, second)
        self.assertTrue(self.cache.is_dir())
        self.assertIs(GrepRetriever._run_search_process, wrapper, "re-install must not double-wrap")

    def test_sync_rga_call_carries_the_cache_flag(self):
        sirchmunk_compat.install_rga_cache_path(self.cache)
        captured = {}

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return Result()

        with patch("sirchmunk.retrieve.text_retriever.subprocess.run", fake_run):
            GrepRetriever._run_rga(["--files", "."])
        command = captured["cmd"]
        self.assertIn("--no-config", command)
        self.assertTrue(any(argument.startswith(sirchmunk_compat.RGA_CACHE_PREFIX) for argument in command), command)
        self.assertEqual(sum(argument.startswith(sirchmunk_compat.RGA_CACHE_PREFIX) for argument in command), 1, command)

    def test_async_rga_path_gets_the_flag_but_the_rg_fallback_does_not(self):
        calls = []

        async def fake_search_process(command, args, **kwargs):
            calls.append((command, list(args)))
            return {"returncode": 0, "stdout": "", "stderr": ""}

        with patch.object(GrepRetriever, "_run_search_process", staticmethod(fake_search_process)):
            sirchmunk_compat.install_rga_cache_path(self.cache)  # wraps the stub
            asyncio.run(GrepRetriever._run_search_process("rga", ["--files", "."]))
            asyncio.run(GrepRetriever._run_search_process("rg", ["--files", "."]))
        rga_command, rg_command = calls
        self.assertEqual(rga_command[0], "rga")
        self.assertTrue(any(argument.startswith(sirchmunk_compat.RGA_CACHE_PREFIX) for argument in rga_command[1]),
                        rga_command)
        self.assertEqual(rg_command[1], ["--files", "."], "native rg must not receive an rga-only flag")

    def test_upstream_shape_change_fails_loudly(self):
        delattr(GrepRetriever, "_run_search_process")  # setUp restores it afterwards
        with self.assertRaisesRegex(RuntimeError, "re-verify the pin"):
            sirchmunk_compat.install_rga_cache_path(self.cache)


if __name__ == "__main__":
    unittest.main()
