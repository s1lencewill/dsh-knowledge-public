"""Runtime staleness: staging records what the image was built from, and the service checks it.

Without this, a runtime copied before a dependency or engine change keeps running the old code and
reports success - the failure mode the manifest exists to prevent.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ENGINE = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ENGINE))
import staging


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / "engine").mkdir()
        (self.project / "tools/rga").mkdir(parents=True)
        for name in staging.ENGINE_FILES:
            (self.project / "engine" / name).write_text("# %s\n" % name, encoding="utf-8")
        for name in staging.BUILD_INPUTS:
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("locked\n", encoding="utf-8")
        self.tools = {"rg": self.project / "tools/rg.exe", "rga": self.project / "tools/rga.exe"}
        for path in self.tools.values():
            path.write_bytes(b"synthetic tool")
        self.runtime = self.project / ".local/appcontainer-runtime"
        self.runtime.mkdir(parents=True)

    def test_manifest_covers_lock_patch_engine_sources_and_tools(self):
        manifest = staging.describe(self.project, self.tools)
        inputs = manifest["inputs"]
        self.assertIn("engine/worker.py", inputs)
        self.assertIn("engine/requirements-win-py313.lock", inputs)
        self.assertIn("tools/rga/cache-path-fallback.patch", inputs)
        self.assertIn("tool:rg", inputs)
        self.assertIn("tool:rga", inputs)
        self.assertEqual(len(inputs), len(staging.ENGINE_FILES) + len(staging.BUILD_INPUTS) + 2)

    def test_fresh_runtime_verifies_and_drift_is_named(self):
        staging.write(self.runtime, staging.describe(self.project, self.tools))
        ok, detail = staging.verify(self.runtime, self.project, self.tools)
        self.assertTrue(ok, detail)
        self.assertEqual(detail, "")

        (self.project / "engine/worker.py").write_text("# changed\n", encoding="utf-8")
        ok, detail = staging.verify(self.runtime, self.project, self.tools)
        self.assertFalse(ok)
        self.assertIn("engine/worker.py", detail)

    def test_lock_and_tool_drift_are_detected(self):
        staging.write(self.runtime, staging.describe(self.project, self.tools))
        (self.project / "engine/requirements-win-py313.lock").write_text("newer==1\n", encoding="utf-8")
        ok, detail = staging.verify(self.runtime, self.project, self.tools)
        self.assertFalse(ok)
        self.assertIn("requirements-win-py313.lock", detail)

        staging.write(self.runtime, staging.describe(self.project, self.tools))
        self.tools["rga"].write_bytes(b"synthetic tool v2")
        ok, detail = staging.verify(self.runtime, self.project, self.tools)
        self.assertFalse(ok)
        self.assertIn("tool:rga", detail)

    def test_missing_manifest_tells_the_user_to_stage(self):
        ok, detail = staging.verify(self.runtime, self.project, self.tools)
        self.assertFalse(ok)
        self.assertIn("stage-appcontainer-runtime.py", detail)

    def test_python_version_change_is_reported(self):
        manifest = staging.describe(self.project, self.tools)
        manifest["python"] = "3.12.0"
        staging.write(self.runtime, manifest)
        ok, detail = staging.verify(self.runtime, self.project, self.tools)
        self.assertFalse(ok)
        self.assertIn("3.12.0", detail)

    def test_the_pinned_tool_paths_are_what_gets_hashed(self):
        # Stage, service and the DSH launch environment must agree on the same two absolute paths;
        # these are what the manifest records, so a drifting PATH tool can no longer stale the runtime.
        import os
        from unittest.mock import patch
        pinned_rg = self.project / "pinned-rg.exe"
        pinned_rga = self.project / "pinned-rga.exe"
        for path in (pinned_rg, pinned_rga):
            path.write_bytes(b"pinned tool")
        environment = {"DSH_KNOWLEDGE_RG": str(pinned_rg), "DSH_KNOWLEDGE_RGA": str(pinned_rga)}
        with patch.dict(os.environ, environment):
            tools = staging.resolve_tools(self.project)
        self.assertEqual(tools, {"rg": pinned_rg, "rga": pinned_rga})
        manifest = staging.describe(self.project, tools)
        self.assertEqual(manifest["inputs"]["tool:rg"], staging.digest(pinned_rg))
        self.assertEqual(manifest["inputs"]["tool:rga"], staging.digest(pinned_rga))

    def test_a_missing_build_input_is_drift_not_a_crash(self):
        # A deleted engine source or patch must produce a verdict, not an exception out of startup.
        staging.write(self.runtime, staging.describe(self.project, self.tools))
        (self.project / "engine/container_entry.py").unlink()
        ok, detail = staging.verify(self.runtime, self.project, self.tools)
        self.assertFalse(ok)
        self.assertIn("engine/container_entry.py", detail)

        manifest = staging.describe(self.project, self.tools)
        self.assertIsNone(manifest["inputs"]["engine/container_entry.py"])
        staging.write(self.runtime, manifest)
        ok, detail = staging.verify(self.runtime, self.project, self.tools)
        self.assertFalse(ok, "a staged hole is still a hole")
        self.assertIn("engine/container_entry.py", detail)

    def test_a_changed_prune_spec_requires_a_restage(self):
        # The prune decision is part of the image: widening the keep-set must not leave the old,
        # narrower runtime in place, and narrowing it must not leave removed packages behind.
        staging.write(self.runtime, staging.describe(self.project, self.tools))
        recorded = staging.read(self.runtime)
        recorded["prune"]["sitePackages"] = sorted(set(recorded["prune"]["sitePackages"]) - {"numpy"})
        staging.write(self.runtime, recorded)
        ok, detail = staging.verify(self.runtime, self.project, self.tools)
        self.assertFalse(ok)
        self.assertIn("prune spec", detail)

        staging.write(self.runtime, staging.describe(self.project, self.tools))
        recorded = staging.read(self.runtime)
        recorded["prune"]["libraryBin"] = ["*.dll"]
        staging.write(self.runtime, recorded)
        ok, detail = staging.verify(self.runtime, self.project, self.tools)
        self.assertFalse(ok)
        self.assertIn("prune spec", detail)

    def test_the_declared_capabilities_are_part_of_the_image(self):
        # If the declaration widens, the staged runtime no longer matches what the service promises.
        staging.write(self.runtime, staging.describe(self.project, self.tools))
        recorded = staging.read(self.runtime)
        self.assertEqual(recorded["prune"]["capabilities"], staging.prune_spec()["capabilities"])
        recorded["prune"]["capabilities"] = dict(recorded["prune"]["capabilities"], embeddings=True)
        staging.write(self.runtime, recorded)
        ok, detail = staging.verify(self.runtime, self.project, self.tools)
        self.assertFalse(ok)
        self.assertIn("prune spec", detail)

    def test_a_torn_manifest_reads_as_missing(self):
        (self.runtime / staging.MANIFEST_NAME).write_text("{not json", encoding="utf-8")
        ok, detail = staging.verify(self.runtime, self.project, self.tools)
        self.assertFalse(ok)
        self.assertIn("stage-appcontainer-runtime.py", detail)

        (self.runtime / staging.MANIFEST_NAME).unlink()
        (self.runtime / staging.MANIFEST_NAME).mkdir()  # present but unreadable as a file
        ok, detail = staging.verify(self.runtime, self.project, self.tools)
        self.assertFalse(ok)
        self.assertIn("stage-appcontainer-runtime.py", detail)


if __name__ == "__main__":
    unittest.main()
