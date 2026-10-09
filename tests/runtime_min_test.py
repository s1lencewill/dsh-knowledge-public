"""The minimal runtime: the keep-set must cover what the payload loads, and pruning must be surgical.

The failure this guards against is silent: a dependency upgrade starts loading a package the prune
removed, and the engine dies inside the AppContainer where the traceback is hard to reach. The
drift check therefore runs the payload's imports in a fresh interpreter and compares them to KEEP.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
ENGINE = PROJECT / "engine"
sys.path.insert(0, str(ENGINE))
import runtime_min

# Imports the container payload performs. Kept in sync with engine/container_entry.py + worker.py.
PAYLOAD = ("worker", "llm_bridge", "runtime", "sirchmunk_compat", "ipc", "sirchmunk", "sirchmunk.llm")

PROBE = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
for name in sys.argv[2:]:
    __import__(name)
site = Path(sys.prefix).resolve() / "Lib" / "site-packages"
tops, outside = set(), []
for name, module in sys.modules.items():
    origin = getattr(module, "__file__", None)
    if not origin:
        spec = getattr(module, "__spec__", None)
        origin = getattr(spec, "origin", None)
    if not origin or not str(origin).endswith((".py", ".pyd", ".so", ".dll")):
        continue
    path = Path(origin).resolve()
    if site in path.parents:
        tops.add(path.relative_to(site).parts[0])
    elif "site-packages" in path.parts:
        outside.append("%s -> %s" % (name, path))
print("RUNTIME_MIN_PROBE=" + json.dumps({"tops": sorted(tops), "outside": sorted(outside)}))
"""


def measured_payload():
    """Every site-packages entry a fresh interpreter loads for the payload's imports."""
    environment = dict(os.environ, PYTHONNOUSERSITE="1")
    probe = subprocess.run([sys.executable, "-c", PROBE, str(ENGINE), *PAYLOAD],
                           capture_output=True, text=True, timeout=300, env=environment)
    if probe.returncode != 0:
        raise AssertionError("payload import probe failed:\n" + (probe.stdout + probe.stderr)[-2000:])
    lines = [line for line in probe.stdout.splitlines() if line.startswith("RUNTIME_MIN_PROBE=")]
    return json.loads(lines[-1].split("=", 1)[1])


class KeepSetTests(unittest.TestCase):
    def test_the_payload_needs_nothing_outside_the_keep_set(self):
        facts = measured_payload()
        missing = sorted(set(facts["tops"]) - set(runtime_min.KEEP))
        self.assertEqual(missing, [], "the payload now loads %s; add it to KEEP (and re-stage) "
                                     "instead of letting the container fail an import" % missing)

    def test_the_payload_loads_nothing_from_a_foreign_site_packages(self):
        facts = measured_payload()
        self.assertEqual(facts["outside"], [], "PYTHONNOUSERSITE must keep the user site out")

    def test_every_kept_entry_exists_in_this_environment(self):
        site = Path(sys.prefix) / "Lib" / "site-packages"
        absent = [name for name in runtime_min.KEEP if not list(site.glob(name if "." in name else name + "*"))]
        self.assertEqual(absent, [], "KEEP names packages that are not installed")

    def test_the_keep_set_has_no_duplicates_and_stays_small(self):
        self.assertEqual(len(runtime_min.KEEP), len(set(runtime_min.KEEP)))
        megabytes, _ = runtime_min.size(Path(sys.prefix) / "Lib" / "site-packages")
        self.assertLess(megabytes, 300, "the point of the keep-set is a small runtime")


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.site = Path(self.temp.name) / "site-packages"
        self.site.mkdir()
        self.keep = ("sirchmunk", "numpy", "six.py")
        for name in self.keep:
            if name.endswith(".py"):
                (self.site / name).write_text("# module\n", encoding="utf-8")
            else:
                (self.site / name).mkdir()
                (self.site / name / "core.py").write_text("x = 1\n" * 100, encoding="utf-8")
        for name in ("torch", "transformers", "modelscope"):
            (self.site / name).mkdir()
            (self.site / name / "big.py").write_text("x = 1\n" * 1000, encoding="utf-8")
        (self.site / "torch-2.0.dist-info").mkdir()
        (self.site / "sirchmunk-0.1.0.dist-info").mkdir()
        (self.site / "_distutils_hack.pth").write_text("import _distutils_hack\n", encoding="utf-8")
        (self.site / "loose.pyd").write_bytes(b"extension")

    def test_prune_removes_only_the_unkept_entries(self):
        removed, freed = runtime_min.prune(self.site, self.keep)
        self.assertEqual(sorted(removed), ["loose.pyd", "modelscope", "torch", "transformers"])
        self.assertGreater(freed, 0)
        for name in self.keep:
            self.assertTrue((self.site / name).exists(), name)
        for name in ("torch", "transformers", "modelscope"):
            self.assertFalse((self.site / name).exists(), name)

    def test_hooks_and_distribution_metadata_survive(self):
        runtime_min.prune(self.site, self.keep)
        self.assertTrue((self.site / "_distutils_hack.pth").is_file(), "site.py loads .pth hooks")
        self.assertTrue((self.site / "sirchmunk-0.1.0.dist-info").is_dir())
        self.assertTrue((self.site / "torch-2.0.dist-info").is_dir(),
                        "distribution metadata is name-mismatched (python-dateutil) and must stay")

    def test_companion_lib_directories_follow_their_package(self):
        # numpy.libs holds the BLAS DLLs numpy imports; a prefix match must keep it.
        (self.site / "numpy.libs").mkdir()
        (self.site / "numpy.libs" / "openblas.dll").write_bytes(b"dll")
        (self.site / "numpy_testsuite").mkdir()
        names = runtime_min.removable(self.site, self.keep)
        self.assertNotIn("numpy.libs", names)
        self.assertIn("numpy_testsuite", names)
        removed, _ = runtime_min.prune(self.site, self.keep)
        self.assertNotIn("numpy.libs", removed)
        self.assertTrue((self.site / "numpy.libs" / "openblas.dll").is_file())

    def test_deliberately_removed_packages_stay_removed(self):
        # The reason a heavy package is absent matters as much as the reason a light one is present:
        # this catches a "just in case" re-add, including one that would arrive through the prefix rule.
        for name, reason in runtime_min.REMOVED_ON_PURPOSE.items():
            self.assertNotIn(name, runtime_min.KEEP, reason)
            self.assertFalse(runtime_min.is_kept(name, runtime_min.KEEP), "%s: %s" % (name, reason))
        for heavy in ("torch", "scipy", "kreuzberg", "transformers", "modelscope", "sentence_transformers"):
            self.assertIn(heavy, runtime_min.REMOVED_ON_PURPOSE, "%s needs a written reason" % heavy)

    def test_prune_is_idempotent_and_reports_what_it_kept(self):
        runtime_min.prune(self.site, self.keep)
        removed, freed = runtime_min.prune(self.site, self.keep)
        self.assertEqual((removed, freed), ([], 0))
        megabytes, files = runtime_min.size(self.site, self.keep)
        self.assertLess(megabytes, 1)
        self.assertEqual(files, 3, "two package modules and one loose module")


class CapabilityTests(unittest.TestCase):
    """The prune premise is enforced, not merely documented."""

    def test_the_snapshot_filter_cannot_outgrow_what_the_runtime_ships(self):
        sys.path.insert(0, str(ENGINE))
        import evidence
        outside = sorted(extension for extension in evidence.EXTENSIONS
                         if extension not in runtime_min.TEXT_EXTENSIONS)
        self.assertEqual(outside, [], "evidence.py now accepts %s: widen the runtime and declare it, "
                                      "or the engine will be handed a file it cannot parse" % outside)

    def test_a_clean_configuration_is_compatible(self):
        self.assertEqual(runtime_min.incompatibilities({}, runtime_min.TEXT_EXTENSIONS), [])

    def test_embedding_and_out_of_snapshot_settings_are_refused(self):
        for name in ("EMBEDDING_MODEL_ID", "EMBEDDING_CACHE_DIR", "SIRCHMUNK_WORK_PATH",
                     "SIRCHMUNK_SEARCH_PATHS"):
            problems = runtime_min.incompatibilities({name: "something"}, runtime_min.TEXT_EXTENSIONS)
            self.assertEqual(len(problems), 1, name)
            self.assertIn(name, problems[0])
            self.assertEqual(runtime_min.incompatibilities({name: "   "}, runtime_min.TEXT_EXTENSIONS), [],
                             "%s set to blanks is not a configuration" % name)

    def test_a_binary_extension_in_the_filter_is_refused(self):
        problems = runtime_min.incompatibilities({}, set(runtime_min.TEXT_EXTENSIONS) | {".pdf"})
        self.assertEqual(len(problems), 1)
        self.assertIn(".pdf", problems[0])

    def test_a_declaration_that_enables_pruned_machinery_is_refused(self):
        for capability in ("embeddings", "knowledgeReuse", "knowledgeEvolution"):
            with patch.dict(runtime_min.CAPABILITIES, {capability: True}):
                problems = runtime_min.incompatibilities({}, runtime_min.TEXT_EXTENSIONS)
        self.assertTrue(problems, "a declaration that cannot be honoured must be refused")

    def test_the_declaration_says_text_only_without_the_pruned_machinery(self):
        self.assertEqual(runtime_min.CAPABILITIES["mediaTypes"], ["text"])
        for capability in ("embeddings", "knowledgeReuse", "knowledgeEvolution"):
            self.assertIs(runtime_min.CAPABILITIES[capability], False, capability)
        for heavy in ("torch", "sentence_transformers", "modelscope", "kreuzberg"):
            self.assertIn(heavy, runtime_min.REMOVED_ON_PURPOSE)


if __name__ == "__main__":
    unittest.main()
