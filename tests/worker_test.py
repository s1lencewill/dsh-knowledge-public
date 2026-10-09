"""Worker contract: the engine-side process produces candidates and never touches the library.

Verification lives in engine/pipeline.py (see pipeline_test.py); here we pin what the untrusted
process is allowed to receive and what it is allowed to return.
"""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import worker

SOURCE = "# Fixture\n\nThe codename is WALNUT-01.\n"
SNIPPET = "WALNUT-01"


class FakeStorage:
    def __init__(self):
        self._shutdown_parquet_sync = lambda: None
        self.db = None
        self.closed = False

    def close(self):
        self.closed = True


class FakeAsyncClient:
    async def close(self):
        return None


class FakeLLM:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._client = types.SimpleNamespace(close=lambda: None)
        self._async_client = FakeAsyncClient()


class FakeSearcher:
    """Returns both upstream snippet shapes, one not-found unit and one invented unit."""

    _FAST_MAX_EVIDENCE_CHARS = 40000
    _FAST_SMALL_FILE_THRESHOLD = 100000

    async def _try_direct_doc_analysis(self, query, paths):
        raise AssertionError("direct answers must be disabled")

    def __init__(self, **kwargs):
        self._background_tasks = []
        self.knowledge_storage = FakeStorage()
        self.paths = None
        self.mode = None

    async def search(self, query, paths, **kwargs):
        self.paths = paths
        self.mode = kwargs.get("mode")
        target = next(Path(paths[0]).rglob("fixture.md"))
        if kwargs.get("mode") == "FILENAME_ONLY":
            return [{"file_path": str(target)}]
        return {"cluster": {"evidences": [
            {"file_or_url": str(target), "is_found": True, "snippets": [{"snippet": SNIPPET}]},
            {"file_or_url": str(target), "is_found": True, "snippets": ["[fixture.md]\n" + SOURCE.strip()]},
            {"file_or_url": str(target), "is_found": False, "snippets": [SNIPPET]},
            {"file_or_url": "/outside/never/cited.md", "is_found": True, "snippets": [SNIPPET]},
        ]}, "llm_usages": [{"total_tokens": 7}]}


class WorkerContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.snapshot = base / "job" / "files"
        self.snapshot.mkdir(parents=True)
        (self.snapshot / "fixture.md").write_text(SOURCE, encoding="utf-8")
        self.work = base / "job"
        self.searcher = FakeSearcher()
        module = types.ModuleType("sirchmunk")
        module.AgenticSearch = lambda **kwargs: self.searcher
        llm_module = types.ModuleType("sirchmunk.llm")
        llm_module.OpenAIChat = FakeLLM
        module.llm = llm_module
        # Import the real submodules the worker needs before stubbing the parent package: a stub
        # without __path__ would otherwise hide sirchmunk.retrieve.text_retriever (rga compat).
        import sirchmunk.retrieve.text_retriever  # noqa: F401  (keeps the module in sys.modules)
        self.modules = patch.dict(sys.modules, {"sirchmunk": module, "sirchmunk.llm": llm_module})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        tools = base / "tools"
        tools.mkdir()
        (tools / "rg.exe").touch()
        (tools / "rga.exe").touch()
        self.environment = patch.dict(os.environ, {
            "DSH_KNOWLEDGE_RG": str(tools / "rg.exe"), "DSH_KNOWLEDGE_RGA": str(tools / "rga.exe"),
            "LLM_BASE_URL": "https://example.invalid/v1", "LLM_MODEL_NAME": "stub", "LLM_API_KEY": "stub-key"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def request(self, mode="FAST"):
        return {"mode": mode, "query": "walnut", "libraryId": "fixture", "cacheRoot": str(self.temp.name),
                "workDir": str(self.work), "snapshotDir": str(self.snapshot),
                "stateDir": str(self.work / "state")}

    async def test_returns_candidates_and_never_reads_the_library(self):
        result = await worker.run(self.request())
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("evidence", result, "verification belongs to the parent")
        self.assertEqual(len(result["candidates"]), 4)
        self.assertIn("snippets", result["candidates"][0])
        self.assertEqual(result["usage"], [{"total_tokens": 7}])
        self.assertEqual(self.searcher.paths, [str(self.snapshot)], "the engine only sees the snapshot")

    async def test_engine_never_receives_a_library_path(self):
        request = self.request()
        request["root"] = "C:\\does-not-exist\\library"
        result = await worker.run(request)
        self.assertEqual(result["status"], "partial", "the worker must not need the library at all")

    async def test_missing_snapshot_is_refused(self):
        request = self.request()
        request["snapshotDir"] = str(self.work / "absent")
        with self.assertRaisesRegex(RuntimeError, "SNAPSHOT_MISSING"):
            await worker.run(request)

    async def test_filename_only_returns_matches_without_candidates(self):
        result = await worker.run(self.request("FILENAME_ONLY"))
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["matches"][0]["name"], "fixture.md")
        self.assertTrue(any("Filename-only" in warning for warning in result["warnings"]))

    async def test_the_engine_is_configured_from_the_declared_capabilities(self):
        # The minimal runtime has no torch/modelscope, so the flags cannot be independent literals
        # that drift away from what the image declares.
        seen = {}
        module = sys.modules["sirchmunk"]
        original = module.AgenticSearch
        module.AgenticSearch = lambda **kwargs: (seen.update(kwargs), self.searcher)[1]
        self.addCleanup(setattr, module, "AgenticSearch", original)
        await worker.run(self.request())
        self.assertIs(seen["reuse_knowledge"], False)
        self.assertIs(seen["enable_knowledge_evolution"], False)

    async def test_an_incompatible_capability_declaration_refuses_to_run(self):
        import runtime_min
        with patch.dict(runtime_min.CAPABILITIES, {"embeddings": True}):
            with self.assertRaisesRegex(RuntimeError, "RUNTIME_INCOMPATIBLE"):
                await worker.run(self.request())


if __name__ == "__main__":
    unittest.main()
