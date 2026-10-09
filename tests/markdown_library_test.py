"""The Markdown library builder: front-matter reading, records, the manifest, and the publish flow.

Synthetic corpora only, built in a temporary directory. No private material, no service, no model call,
no network: the front-matter reader is exercised on the shapes a hostile or careless note can contain,
and the claim under test is that none of them is interpreted, expanded or fetched.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
import capacity
import catalog
import evidence
import markdown_library as builder

SCRIPT = ROOT / "scripts" / "build-markdown-library.py"


def front(text):
    return builder.front_matter(text)[0]


class FrontMatterTests(unittest.TestCase):
    def test_a_leading_block_is_read_and_the_body_is_not(self):
        mapping = front("---\ntitle: A note\ndate: 2015-07-04\n---\n# Body\ntitle: not this one\n")
        self.assertEqual(mapping, {"title": "A note", "date": "2015-07-04"})

    def test_absent_unterminated_and_oversized_blocks_are_reported_and_yield_nothing(self):
        for text, reason in (("# no block\n", "no front matter"),
                             ("---\ntitle: unterminated\n", "unterminated front matter"),
                             ("---\n" + "title: x\n" * 20000, "front matter too large"),
                             ("not a delimiter\n---\ntitle: x\n---\n", "no front matter")):
            mapping, note, truncated = builder.front_matter(text)
            self.assertEqual(mapping, {}, text[:20])
            self.assertEqual(note, reason)
            self.assertFalse(truncated)

    def test_nothing_that_could_reference_another_node_is_resolved(self):
        """Anchors, aliases, tags, block scalars, flow mappings and nested blocks are skipped, not read."""
        mapping = front("---\n"
                        "title: Kept\n"
                        "anchored: &a value\n"
                        "aliased: *a\n"
                        "tagged: !!python/object/apply:os.system ['echo pwned']\n"
                        "python: !!python/name:os.system\n"
                        "block: |\n"
                        "  line one\n"
                        "folded: >\n"
                        "  folded text\n"
                        "nested:\n"
                        "  child: 1\n"
                        "  continuation: 2\n"
                        "flow: {a: 1}\n"
                        "sequence:\n"
                        "  - one\n"
                        "---\n")
        self.assertEqual(sorted(mapping), ["title"])
        self.assertEqual(mapping["title"], "Kept")
        # And the value that looked like a payload is nowhere in the result at all.
        self.assertNotIn("pwned", json.dumps(mapping))

    def test_a_url_is_text_that_is_never_fetched_validated_or_escaped_away(self):
        mapping = front("---\ntitle: T\nurl: https://example.invalid/a#frag\n---\n")
        self.assertEqual(mapping["url"], "https://example.invalid/a#frag")

    def test_scalars_lists_quotes_comments_and_empty_values(self):
        mapping = front("---\n"
                        "plain: value\n"
                        "quoted: \"a: b, c\"\n"
                        "single: 'kept as written'\n"
                        "list: [one, \"two, with comma\", three]\n"
                        "bracketed: [α, β]\n"
                        "comment: value # trailing\n"
                        "hash: https://example.invalid/p#section\n"
                        "empty:\n"
                        "emptyish:   \n"
                        "# a comment line\n"
                        "a.b-c_1: dotted key\n"
                        "---\n")
        self.assertEqual(mapping["plain"], "value")
        self.assertEqual(mapping["quoted"], "a: b, c")
        self.assertEqual(mapping["single"], "kept as written")
        self.assertEqual(mapping["list"], ["one", "two, with comma", "three"])
        self.assertEqual(mapping["bracketed"], ["α", "β"])
        self.assertEqual(mapping["comment"], "value")
        self.assertEqual(mapping["hash"], "https://example.invalid/p#section")
        self.assertNotIn("empty", mapping, "an absent field stays absent; it is not defaulted")
        self.assertNotIn("emptyish", mapping)
        self.assertEqual(mapping["a.b-c_1"], "dotted key")

    def test_a_key_that_is_not_a_plain_identifier_is_ignored(self):
        # A key this reader cannot be certain about yields nothing rather than a value under a key that
        # might not mean what the file meant. Nothing is guessed, and no key is renamed.
        mapping = front("---\ntitle: T\n- item: 1\n{flow}: 2\nbad key: 3\ncolon rich: 4\n_ok.key-1: 5\n---\n")
        self.assertEqual(sorted(mapping), ["_ok.key-1", "title"])

    def test_the_reader_is_bounded_so_a_crafted_note_cannot_allocate_without_limit(self):
        long_value = "v" * 50_000
        mapping, _, truncated = builder.front_matter("---\ntitle: T\nbig: %s\n---\n" % long_value)
        self.assertLessEqual(len(mapping["big"]), builder.FRONT_MATTER_MAX_VALUE_CHARS)
        many = "".join("k%d: %s\n" % (index, "x" * 500) for index in range(50))
        mapping, _, truncated = builder.front_matter("---\ntitle: T\n" + many + "---\n")
        self.assertTrue(truncated, "hitting the total-character bound must be reported, not hidden")
        self.assertLessEqual(len(mapping), builder.FRONT_MATTER_MAX_KEYS)
        self.assertLessEqual(sum(len(value) for value in mapping.values()),
                             builder.FRONT_MATTER_MAX_TOTAL_CHARS + builder.FRONT_MATTER_MAX_VALUE_CHARS)


class RecordTests(unittest.TestCase):
    def record(self, relative, text):
        data = text.encode("utf-8")
        mapping, note, truncated = builder.front_matter(text)
        return builder.note_record(relative, data, mapping, truncated, note)

    def test_identity_is_undetermined_even_when_the_note_names_itself(self):
        record = self.record("a/note.md", "---\ntitle: A real title\n---\nbody\n")
        self.assertEqual(record["titleRaw"], "A real title")
        self.assertEqual(record["verification"], builder.IDENTITY)
        self.assertEqual(record["verificationDetail"], builder.IDENTITY_FROM_FRONT_MATTER)
        self.assertIn("no external record", record["verificationDetail"])

    def test_a_note_without_front_matter_is_named_by_its_file_and_says_so(self):
        record = self.record("a/Some Note.md", "just a body\n")
        self.assertEqual(record["titleRaw"], "Some Note")
        self.assertEqual(record["verificationDetail"], builder.IDENTITY_FROM_FILE_NAME)
        self.assertEqual(record["frontMatterNote"], "no front matter")

    def test_a_markdown_record_never_claims_pages(self):
        record = self.record("2021/post.md", "---\ntitle: T\ndate: 2021-11-02\n---\nbody\n")
        self.assertNotIn("pages", record, "a text-line document must not offer a page number")
        self.assertFalse(__import__("source_card").is_pdf_record(record))

    def test_measured_fields_come_from_the_bytes_and_metadata_from_the_note(self):
        record = self.record("post.md", "---\ntitle: T\ndate: 2021-11-02T09:00:00Z\nauthor: A. Writer\n---\nbody\ufffd\n")
        self.assertEqual(record["textSHA256"], evidence.digest(("---\ntitle: T\ndate: 2021-11-02T09:00:00Z\n"
                                                               "author: A. Writer\n---\nbody\ufffd\n").encode("utf-8")))
        self.assertEqual(record["year"], "2021")
        self.assertEqual(record["authors"], "A. Writer")
        self.assertEqual(record["quality"]["replacementChars"], 1)
        self.assertEqual(record["quality"]["reviewPages"], [])
        self.assertIsNone(record["quality"]["searchableCjkRuns"])
        self.assertEqual(record["frontMatter"]["url"] if "url" in record["frontMatter"] else None, None)

    def test_a_title_that_is_not_a_string_is_not_used_as_one(self):
        record = self.record("fallback.md", "---\ntitle:\n---\nbody\n")
        self.assertEqual(record["titleRaw"], "fallback")


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = self.root / "library"
        self.library.mkdir()

    def note(self, relative, text):
        path = self.library / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def test_the_manifest_is_the_same_file_set_a_snapshot_would_copy(self):
        self.note("one.md", "---\ntitle: One\n---\n")
        self.note("deep/two.md", "---\ntitle: Two\n---\n")
        self.note(".hidden.md", "---\ntitle: hidden\n---\n")
        (self.library / ".git").mkdir()
        (self.library / ".git" / "config.md").write_text("x", encoding="utf-8")
        (self.library / "node_modules").mkdir()
        (self.library / "node_modules" / "pkg.md").write_text("x", encoding="utf-8")
        (self.library / "image.png").write_bytes(b"\x89PNG")
        (self.library / "data.jsonl").write_text("{}", encoding="utf-8")
        scanned = builder.scan(self.library, "lib", capacity.DEFAULT_MAX_FILE_BYTES)
        self.assertEqual(sorted(scanned["manifest"]), ["deep/two.md", "one.md"])
        manifest, _ = evidence.snapshot(self.library, self.root / "snap", capacity.DEFAULT_MAX_FILE_BYTES)
        self.assertEqual(sorted(entry["relativePath"] for entry in manifest.values()),
                         sorted(scanned["manifest"]),
                         "the authorized set and the searchable set must be one set")

    def test_a_file_that_is_not_utf8_is_authorized_but_has_no_record(self):
        self.note("good.md", "---\ntitle: Good\n---\n")
        (self.library / "bad.md").write_bytes(b"---\ntitle: bad\n---\n\xff\xfe\x00")
        scanned = builder.scan(self.library, "lib", capacity.DEFAULT_MAX_FILE_BYTES)
        self.assertIn("bad.md", scanned["manifest"])
        self.assertEqual([record["textFile"] for record in scanned["records"].values()], ["good.md"])
        self.assertEqual([item["reason"] for item in scanned["skipped"]], ["not UTF-8 text"])

    def test_an_oversized_file_is_reported_as_a_violation_and_never_dropped(self):
        self.note("small.md", "---\ntitle: Small\n---\n")
        big = self.note("big.md", "---\ntitle: Big\n---\n" + "x" * 300)
        scanned = builder.scan(self.library, "lib", 250)
        self.assertEqual([violation["dimension"] for violation in scanned["violations"]], ["fileBytes"])
        self.assertEqual(scanned["violations"][0]["path"], "big.md")
        self.assertEqual(scanned["violations"][0]["size"], big.stat().st_size)
        self.assertEqual(sorted(scanned["manifest"]), ["big.md", "small.md"],
                         "the file is still part of the library; it is the snapshot that refuses")

    def test_file_count_and_total_bytes_violations_are_reported_too(self):
        with patch.object(capacity, "MAX_FILES", 1), patch.object(capacity, "MAX_TOTAL_BYTES", 10):
            self.note("one.md", "---\ntitle: One\n---\nbody body body\n")
            self.note("two.md", "---\ntitle: Two\n---\n")
            scanned = builder.scan(self.library, "lib", capacity.DEFAULT_MAX_FILE_BYTES)
        dimensions = sorted(violation["dimension"] for violation in scanned["violations"])
        self.assertEqual(dimensions, ["files", "totalBytes"])

    def test_a_file_that_changes_while_it_is_being_read_is_reported_not_published(self):
        """A real edit inside the scan window, not a mocked limit: the manifest must refuse to publish.

        The edit happens between the moment the file's bytes were read and the moment the scan re-checks
        that the file it hashed is still the file on disk, which is exactly the gap the check exists for:
        publishing here would record a hash for bytes that are no longer there.
        """
        target = self.note("note.md", "---\ntitle: Note\n---\nbody\n")
        original = evidence.digest
        edited = []

        def editing(data):
            version = original(data)
            if not edited:
                edited.append(True)
                target.write_text("---\ntitle: Note\n---\na different, longer body\n", encoding="utf-8",
                                  newline="")
            return version

        with patch.object(builder.evidence, "digest", editing):
            scanned = builder.scan(self.library, "lib", capacity.DEFAULT_MAX_FILE_BYTES)
        self.assertTrue(edited, "the scan must have hashed the file for this test to mean anything")
        self.assertEqual(scanned["changed"], ["note.md"])

    def test_a_stable_corpus_reports_no_change(self):
        self.note("note.md", "---\ntitle: Note\n---\nbody\n")
        self.assertEqual(builder.scan(self.library, "lib", capacity.DEFAULT_MAX_FILE_BYTES)["changed"], [])

    def test_the_catalogue_payload_carries_the_binding_and_the_records(self):
        self.note("note.md", "---\ntitle: Note\n---\n")
        scanned = builder.scan(self.library, "lib", capacity.DEFAULT_MAX_FILE_BYTES)
        payload = builder.catalogue_payload(scanned, "lib", self.library)
        self.assertEqual(payload["libraryId"], "lib")
        self.assertEqual(payload["rootIdentity"], capacity.root_identity(self.library))
        self.assertEqual(sorted(payload["documents"]), ["md-note.md"])

    def test_the_diff_reports_one_edited_note_as_changed_not_as_added(self):
        first = self.note("note.md", "---\ntitle: Note\n---\none\n")
        scanned = builder.scan(self.library, "lib", capacity.DEFAULT_MAX_FILE_BYTES)
        first.write_text("---\ntitle: Note\n---\ntwo\n", encoding="utf-8", newline="")
        self.note("other.md", "---\ntitle: Other\n---\n")
        again = builder.scan(self.library, "lib", capacity.DEFAULT_MAX_FILE_BYTES)
        added, removed, changed = builder.diff(scanned["manifest"], again)
        self.assertEqual(added, ["other.md"])
        self.assertEqual(removed, [])
        self.assertEqual(changed, ["note.md"])


class CommandLineTests(unittest.TestCase):
    """The flow an operator actually runs, end to end, in a temporary instance."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.instance = Path(self.temp.name)
        self.wiki = self.instance / "wiki"
        self.wiki.mkdir()
        (self.wiki / "note.md").write_text("---\ntitle: Note\n---\nbody\n", encoding="utf-8", newline="")
        self.libraries = self.instance / "libraries.json"
        self.libraries.write_text(json.dumps({"libraries": []}), encoding="utf-8")
        self.catalogue = self.instance / "catalogue.json"
        self.python = str(Path(sys.executable))

    def run_tool(self, *arguments):
        result = subprocess.run([self.python, str(SCRIPT), *arguments], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", cwd=str(ROOT))
        return result.returncode, (result.stdout or "") + (result.stderr or "")

    def register(self, apply=True):
        arguments = ["register", "--libraries", str(self.libraries), "--id", "wiki-articles",
                     "--workspace", str(self.instance), "--root", str(self.wiki),
                     "--catalogue", str(self.catalogue), "--max-file-bytes", "4000000"]
        return self.run_tool(*arguments, *(["--apply"] if apply else []))

    def scan(self, apply=True):
        arguments = ["scan", "--libraries", str(self.libraries), "--id", "wiki-articles"]
        return self.run_tool(*arguments, *(["--apply"] if apply else []))

    def test_register_previews_first_and_an_empty_manifest_authorizes_nothing(self):
        code, output = self.register(apply=False)
        self.assertEqual(code, 0)
        self.assertIn("preview only", output)
        self.assertEqual(json.loads(self.libraries.read_text(encoding="utf-8"))["libraries"], [])
        self.register(apply=True)
        config = json.loads(self.libraries.read_text(encoding="utf-8"))
        entry = config["libraries"][0]
        self.assertEqual(entry["id"], "wiki-articles")
        self.assertEqual(entry["maxFileBytes"], 4_000_000)
        self.assertFalse(entry["syntheticFixture"])
        self.assertEqual(entry["realDataTrial"]["files"], {})
        # And that empty manifest really does refuse: no query can reach an unbuilt library.
        allowed, detail, _ = __import__("real_data").authorize(entry, evidence.EXTENSIONS)
        self.assertFalse(allowed)
        self.assertIn("MANIFEST_UNUSABLE", detail)

    def test_scan_previews_writes_nothing_then_publishes_a_loadable_pair(self):
        self.register()
        code, output = self.scan(apply=False)
        self.assertEqual(code, 0)
        self.assertIn("preview only", output)
        self.assertFalse(self.catalogue.exists())
        self.assertEqual(json.loads(self.libraries.read_text(encoding="utf-8"))["libraries"][0]
                         ["realDataTrial"]["files"], {})
        code, output = self.scan(apply=True)
        self.assertEqual(code, 0, output)
        self.assertTrue(self.catalogue.exists())
        config = json.loads(self.libraries.read_text(encoding="utf-8"))
        entry = config["libraries"][0]
        self.assertEqual(sorted(entry["realDataTrial"]["files"]), ["note.md"])
        documents, version = catalog.load_bound(self.catalogue, "wiki-articles", self.wiki)
        self.assertEqual(len(documents), 1)
        self.assertEqual(catalog.load_versioned(self.catalogue)[1], version)
        code, output = self.run_tool("check", "--libraries", str(self.libraries), "--id", "wiki-articles")
        self.assertEqual(code, 0, output)
        self.assertIn("documents    1", output)
        self.assertIn("mismatch     0", output)

    def test_the_publish_keeps_a_rollback_pair_and_changes_nothing_else(self):
        self.register()
        self.scan(apply=True)
        before = json.loads(self.libraries.read_text(encoding="utf-8"))
        backups = list((self.instance / "backups").iterdir())
        self.assertEqual(len(backups), 1, "each publish keeps the pair it replaced")
        self.assertTrue((backups[0] / "libraries.json").is_file())
        (self.wiki / "second.md").write_text("---\ntitle: Second\n---\n", encoding="utf-8", newline="")
        code, output = self.scan(apply=True)
        self.assertEqual(code, 0, output)
        self.assertIn("+1 -0 ~0", output)
        after = json.loads(self.libraries.read_text(encoding="utf-8"))
        self.assertEqual(sorted(after["libraries"][0]["realDataTrial"]["files"]), ["note.md", "second.md"])
        # Only this library's two keys moved; everything else in the file is untouched.
        self.assertEqual({key: value for key, value in before["libraries"][0].items() if key not in
                          ("realDataTrial",)}, {key: value for key, value in after["libraries"][0].items()
                                                if key not in ("realDataTrial",)})

    def test_a_corpus_over_the_library_ceiling_is_refused_and_nothing_is_published(self):
        self.register()
        (self.wiki / "huge.md").write_text("---\ntitle: Huge\n---\n" + "x" * 4_200_000, encoding="utf-8",
                                           newline="")
        code, output = self.scan(apply=True)
        self.assertEqual(code, 2, output)
        self.assertIn("REFUSED", output)
        self.assertIn("fileBytes", output)
        self.assertFalse(self.catalogue.exists(), "a refused scan publishes no catalogue")
        self.assertEqual(json.loads(self.libraries.read_text(encoding="utf-8"))["libraries"][0]
                         ["realDataTrial"]["files"], {})

    def test_bind_migrates_a_catalogue_that_predates_the_binding(self):
        legacy = self.instance / "legacy.json"
        legacy.write_text(json.dumps({"documents": {"a": {"documentId": "a", "textFile": "note.md",
                                                          "fileName": "note.md", "titleRaw": "Note",
                                                          "textSHA256": "0" * 64}}}), encoding="utf-8")
        # Unattributed: usable only where nothing could be confused, refused where attribution is required.
        catalog.load_versioned(legacy)
        with self.assertRaises(catalog.CatalogError) as raised:
            catalog.load_bound(legacy, "wiki-articles", self.wiki)
        self.assertEqual(raised.exception.code, catalog.CATALOGUE_NO_LIBRARY)
        code, output = self.run_tool("bind", "--id", "wiki-articles", "--root", str(self.wiki),
                                     "--catalogue", str(legacy))
        self.assertEqual(code, 0, output)
        self.assertIn("preview only", output)
        code, output = self.run_tool("bind", "--id", "wiki-articles", "--root", str(self.wiki),
                                     "--catalogue", str(legacy), "--apply")
        self.assertEqual(code, 0, output)
        payload = json.loads(legacy.read_text(encoding="utf-8"))
        self.assertEqual(payload["libraryId"], "wiki-articles")
        self.assertEqual(payload["rootIdentity"], capacity.root_identity(self.wiki))
        catalog.load_bound(legacy, "wiki-articles", self.wiki)
        with self.assertRaises(catalog.CatalogError) as raised:
            catalog.load_bound(legacy, "some-other-library", self.wiki)
        self.assertEqual(raised.exception.code, catalog.CATALOGUE_LIBRARY_MISMATCH)

    def test_scan_refuses_a_library_that_was_never_registered(self):
        code, output = self.run_tool("scan", "--libraries", str(self.libraries), "--id", "nope")
        self.assertEqual(code, 1)
        self.assertIn("not registered", output)


if __name__ == "__main__":
    unittest.main()
