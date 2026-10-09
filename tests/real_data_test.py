"""Real-materials trial authorization: off by default, per library, verified against the files.

The mechanism exists because M0 refuses real libraries. It must never become a way to smuggle one in:
no declaration, a declaration that is not literally authorized, an unusable manifest, a changed hash,
an added file or a missing file all refuse - and a synthetic library never reaches this code path.
"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import evidence
import real_data

TEXT = (".md", ".txt")


class TrialAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "texts"
        self.root.mkdir()
        for name in ("a.txt", "b.txt", "c.txt"):
            (self.root / name).write_text("content of %s\n" % name, encoding="utf-8")

    def entry(self, **overrides):
        files = {name: real_data.sha256(self.root / name) for name in ("a.txt", "b.txt", "c.txt")}
        library = {"id": "trial", "root": str(self.root), "syntheticFixture": False,
                   "realDataTrial": {"authorized": True, "files": files}}
        library.update(overrides)
        return library

    def test_a_matching_file_set_is_authorized_and_warns(self):
        ok, detail, warning = real_data.authorize(self.entry(), TEXT)
        self.assertTrue(ok, detail)
        self.assertEqual(detail, "")
        self.assertIn("REAL_DATA_TRIAL_LIBRARY", warning)
        self.assertIn("3 authorized file(s)", warning)

    def test_a_library_without_the_declaration_is_refused(self):
        library = self.entry()
        del library["realDataTrial"]
        ok, detail, warning = real_data.authorize(library, TEXT)
        self.assertFalse(ok)
        self.assertIn(real_data.NOT_AUTHORIZED, detail)
        self.assertIsNone(warning)

    def test_the_declaration_must_be_literally_authorized(self):
        for value in (False, "true", 1, None):
            library = self.entry(realDataTrial={"authorized": value,
                                                "files": {"a.txt": "0" * 64}})
            ok, detail, _ = real_data.authorize(library, TEXT)
            self.assertFalse(ok, value)
            self.assertIn(real_data.NOT_AUTHORIZED, detail)

    def test_an_unusable_manifest_is_refused(self):
        for files in (None, {}, {"a.txt": "short"}, {3: "0" * 64}):
            library = self.entry(realDataTrial={"authorized": True, "files": files})
            ok, detail, _ = real_data.authorize(library, TEXT)
            self.assertFalse(ok, files)
            self.assertIn(real_data.MANIFEST_UNUSABLE, detail)

    def test_a_changed_extra_or_missing_file_is_refused(self):
        library = self.entry()
        (self.root / "a.txt").write_text("tampered\n", encoding="utf-8")
        ok, detail, _ = real_data.authorize(library, TEXT)
        self.assertFalse(ok)
        self.assertIn(real_data.FILES_CHANGED, detail)
        self.assertIn("1 changed", detail)

        (self.root / "a.txt").write_text("content of a.txt\n", encoding="utf-8")
        (self.root / "d.txt").write_text("new\n", encoding="utf-8")
        ok, detail, _ = real_data.authorize(library, TEXT)
        self.assertFalse(ok)
        self.assertIn("1 extra", detail)

        (self.root / "d.txt").unlink()
        (self.root / "c.txt").unlink()
        ok, detail, _ = real_data.authorize(library, TEXT)
        self.assertFalse(ok)
        self.assertIn("1 missing", detail)

    def test_the_refusal_reports_counts_not_names_or_content(self):
        library = self.entry()
        (self.root / "a.txt").write_text("the literature sentinel sentence\n", encoding="utf-8")
        ok, detail, _ = real_data.authorize(library, TEXT)
        self.assertFalse(ok)
        self.assertNotIn("a.txt", detail)
        self.assertNotIn("sentinel", detail)

    def test_files_outside_the_snapshot_filter_are_ignored(self):
        # A stray PDF next to the authorized texts is neither searched nor a trial violation.
        (self.root / "paper.pdf").write_bytes(b"%PDF-1.4 not text")
        ok, detail, _ = real_data.authorize(self.entry(), TEXT)
        self.assertTrue(ok, detail)

    def test_a_symlink_is_not_counted_as_an_authorized_file(self):
        try:
            (self.root / "link.txt").symlink_to(self.root / "a.txt")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not permitted on this host")
        ok, detail, _ = real_data.authorize(self.entry(), TEXT)
        self.assertTrue(ok, detail)

    def test_a_missing_root_is_refused(self):
        library = self.entry(root=str(Path(self.temp.name) / "absent"))
        ok, detail, _ = real_data.authorize(library, TEXT)
        self.assertFalse(ok)
        self.assertIn(real_data.FILES_CHANGED, detail)

    def test_the_declared_hashes_match_what_the_loader_computes(self):
        # Guard against a manifest that "verifies" only because both sides are wrong.
        library = self.entry()
        declared = library["realDataTrial"]["files"]
        self.assertEqual(declared["a.txt"], hashlib.sha256((self.root / "a.txt").read_bytes()).hexdigest())
        self.assertEqual(real_data.actual_files(self.root, TEXT), declared)


class SelectionAgreementTests(unittest.TestCase):
    """The gate and the snapshot must select files by the same rule, or the gate gates the wrong thing.

    The authorization check used to walk the tree on its own (`rglob("*")` plus an extension test) while
    the snapshot pruned hidden names and five excluded directories. One hidden note under a library was
    therefore an "extra file": every query refused with `REAL_DATA_TRIAL_FILES_CHANGED`, while the
    snapshot had never intended to read it. These tests pin the agreement, entry kind by entry kind.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "texts"
        self.root.mkdir()
        for name in ("a.txt", "b.txt", "c.txt"):
            (self.root / name).write_text("content of %s\n" % name, encoding="utf-8")

    def entry(self, **overrides):
        files = {name: real_data.sha256(self.root / name) for name in ("a.txt", "b.txt", "c.txt")}
        library = {"id": "trial", "root": str(self.root), "syntheticFixture": False,
                   "realDataTrial": {"authorized": True, "files": files}}
        library.update(overrides)
        return library

    def noise(self):
        (self.root / ".hidden.md").write_text("hidden\n", encoding="utf-8")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config.md").write_text("x\n", encoding="utf-8")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "pkg.md").write_text("x\n", encoding="utf-8")
        (self.root / ".venv").mkdir()
        (self.root / ".venv" / "v.md").write_text("x\n", encoding="utf-8")
        (self.root / "__pycache__").mkdir()
        (self.root / "__pycache__" / "c.md").write_text("x\n", encoding="utf-8")
        (self.root / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (self.root / "notes.jsonl").write_text("{}\n", encoding="utf-8")

    def test_hidden_and_excluded_entries_are_neither_searchable_nor_a_trial_violation(self):
        self.noise()
        files = {name: real_data.sha256(self.root / name) for name in ("a.txt", "b.txt", "c.txt")}
        library = self.entry(realDataTrial={"authorized": True, "files": files})
        ok, detail, _ = real_data.authorize(library, TEXT)
        self.assertTrue(ok, detail)

    def test_the_authorized_set_is_exactly_what_a_snapshot_would_copy(self):
        self.noise()
        (self.root / "sub").mkdir()
        (self.root / "sub" / "nested.md").write_text("nested\n", encoding="utf-8")
        found = real_data.actual_files(self.root, TEXT)
        manifest, _ = evidence.snapshot(self.root, Path(self.temp.name) / "snap", 2_000_000)
        self.assertEqual(sorted(found), sorted(entry["relativePath"] for entry in manifest.values()))
        self.assertIn("sub/nested.md", found)
        self.assertNotIn(".hidden.md", found)

    def test_the_tool_that_builds_a_manifest_selects_the_same_way(self):
        """The builder, the gate and the snapshot are three consumers of one rule, so all three agree."""
        import capacity
        import markdown_library
        self.noise()
        scanned = markdown_library.scan(self.root, "trial", capacity.DEFAULT_MAX_FILE_BYTES, TEXT)
        self.assertEqual(sorted(scanned["manifest"]), sorted(real_data.actual_files(self.root, TEXT)))
        library = self.entry(realDataTrial={"authorized": True, "files": dict(scanned["manifest"])})
        ok, detail, _ = real_data.authorize(library, TEXT)
        self.assertTrue(ok, detail)

    def test_a_file_the_snapshot_skips_for_encoding_is_still_part_of_the_authorized_set(self):
        """A known and deliberate difference, written down so it is not mistaken for an oversight.

        The gate answers "are these the files this library declares?" - so a non-UTF-8 text file is
        declared like any other. The snapshot answers "what text can be searched?" - so it warns and
        leaves that file out. The authorized set is therefore a superset of the searchable set, and the
        builder produces the same set the gate compares against, which is what keeps them consistent.
        """
        (self.root / "latin.md").write_bytes(b"---\ntitle: x\n---\n\xff\xfe not utf-8\n")
        self.noise()
        files = real_data.actual_files(self.root, TEXT)
        self.assertIn("latin.md", files)
        library = self.entry(realDataTrial={"authorized": True, "files": files})
        ok, detail, _ = real_data.authorize(library, TEXT)
        self.assertTrue(ok, detail)
        manifest, warnings = evidence.snapshot(self.root, Path(self.temp.name) / "snap2", 2_000_000)
        self.assertNotIn("latin.md", [entry["relativePath"] for entry in manifest.values()])
        self.assertTrue(any("Non-UTF8 file: latin.md" == warning for warning in warnings), warnings)


if __name__ == "__main__":
    unittest.main()
