import sys
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from evidence import snapshot, verify, normalised_text, excerpt_from_offsets


class CitationLocatorContractTests(unittest.TestCase):
    """A citation must be rebuildable from its returned fields, not only from its line numbers.

    The real failure: two citations stopped before the end of their last line, so the line range alone
    produced more text than the excerpt. Line numbers stay (they locate), the offsets make the quote exact.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "library"
        self.root.mkdir()
        self.file = self.root / "source.txt"

    def manifest_for(self, data: bytes):
        self.file.write_bytes(data)
        manifest, _warnings = snapshot(self.root, self.base / "snapshot")
        return manifest, next(iter(manifest))

    def check(self, excerpt):
        item = verify(self.manifest, self.key, excerpt, "test")
        self.assertIsNotNone(item, "the excerpt must be locatable")
        text = normalised_text((self.base / "snapshot").rglob("source.txt").__next__().read_bytes())
        self.assertEqual(excerpt_from_offsets(text, item), item["excerpt"])
        # line numbers still locate; the offsets pin the exact quote inside them
        lines = text.split("\n")
        if item["endLine"] == item["startLine"]:
            rebuilt = lines[item["startLine"] - 1][item["startOffset"]:item["endOffset"]]
        else:
            first = lines[item["startLine"] - 1][item["startOffset"]:]
            middle = lines[item["startLine"]:item["endLine"] - 1]
            last = lines[item["endLine"] - 1][:item["endOffset"]]
            rebuilt = "\n".join([first] + middle + [last])
        self.assertEqual(rebuilt, item["excerpt"])
        return item

    def test_a_quote_that_stops_before_the_end_of_its_last_line(self):
        # The shape the real run produced: the range is right, the tail of the last line is missing.
        manifest, key = self.manifest_for("第一行\n证据段：前一半然后\n第二段继续\n".encode())
        self.manifest, self.key = manifest, key
        item = self.check("证据段：前一半然后")
        self.assertEqual(item["startLine"], 2)
        self.assertEqual(item["startOffset"], 0)
        self.assertEqual(item["endOffset"], len("证据段：前一半然后"))
        self.assertFalse(item["excerpt"].endswith("然后\n"))

    def test_a_quote_that_starts_and_ends_inside_its_lines(self):
        self.manifest, self.key = self.manifest_for("alpha beta WALNUT gamma\ndelta\n".encode())
        item = self.check("WALNUT")
        self.assertEqual((item["startLine"], item["startOffset"], item["endOffset"]), (1, 11, 17))

    def test_a_wrapped_sentence_across_lines_is_exact(self):
        self.manifest, self.key = self.manifest_for(
            "the uptake coefficient which quantifies the\nfrac-\ntion of collisions\n".encode())
        item = self.check("the uptake coefficient which quantifies the\nfrac-")
        self.assertEqual((item["startLine"], item["endLine"]), (1, 2))
        self.assertEqual(item["endOffset"], 5)

    def test_crlf_source_and_unicode_width_do_not_shift_the_offsets(self):
        self.manifest, self.key = self.manifest_for(
            "标题\r\n中文证据：体相主导\r\n尾行\r\n".encode("utf-8"))
        item = self.check("中文证据：体相主导")
        self.assertEqual(item["startLine"], 2)
        self.assertEqual(item["startOffset"], 0)
        self.assertEqual(item["endOffset"], len("中文证据：体相主导"))
        self.assertEqual(item["startChar"], len("标题\n"))

    def test_a_bom_does_not_count_as_part_of_the_first_line(self):
        self.manifest, self.key = self.manifest_for("\ufeff第一行\n第二行证据\n".encode("utf-8"))
        item = self.check("第二行证据")
        self.assertEqual((item["startLine"], item["startOffset"]), (2, 0))

    def test_a_long_quote_is_still_exactly_rebuildable(self):
        body = "\n".join("第 %02d 行：证据内容 with ASCII tail" % index for index in range(60))
        self.manifest, self.key = self.manifest_for((body + "\n").encode("utf-8"))
        excerpt = "\n".join("第 %02d 行：证据内容 with ASCII tail" % index for index in range(10, 24))
        item = self.check(excerpt)
        self.assertEqual(item["startLine"], 11)
        self.assertEqual(item["endLine"], 24)
        self.assertEqual(item["endChar"] - item["startChar"], len(excerpt))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "资料 with spaces"
        self.root.mkdir()
        self.file = self.root / "source.md"
        self.file.write_bytes("# 文档\r\n证据：WALNUT\r\n尾行\r\n".encode())
        (self.root / ".env").write_text("SECRET=synthetic")
        (self.root / "bad.txt").write_bytes(b"\xff\xfe\x80")
        self.manifest, self.warnings = snapshot(self.root, self.base / "snapshot")
        self.key = next(iter(self.manifest))

    def test_exact_citation(self):
        evidence = verify(self.manifest, self.key, "证据：WALNUT", "test")
        self.assertEqual(evidence["startLine"], 2)
        self.assertEqual(evidence["endLine"], 2)

    def test_exclusions_and_encoding(self):
        self.assertEqual(len(self.manifest), 1)
        self.assertTrue(any("Non-UTF8" in warning for warning in self.warnings))

    def test_reject_invented_quote(self):
        self.assertIsNone(verify(self.manifest, self.key, "Invented", "test"))

    def test_reject_changed_source(self):
        self.file.write_text("New version")
        self.assertIsNone(verify(self.manifest, self.key, "证据：WALNUT", "test"))

    def test_reject_deleted_source(self):
        self.file.unlink()
        self.assertIsNone(verify(self.manifest, self.key, "证据：WALNUT", "test"))

    def test_reject_unlisted_path(self):
        self.assertIsNone(verify(self.manifest, self.root / ".env", "SECRET=synthetic", "test"))

    def test_reject_nested_snapshot(self):
        with self.assertRaises(ValueError):
            snapshot(self.root, self.root / "snapshot")

    def test_bom_and_repeated_quote_use_first_line(self):
        self.file.write_bytes(b"\xef\xbb\xbf" + "标题\r\n重复\r\n重复\r\n".encode())
        manifest, _ = snapshot(self.root, self.base / "bom-snapshot")
        first = verify(manifest, next(iter(manifest)), "重复", "test")
        second = verify(manifest, next(iter(manifest)), "重复", "test")
        self.assertEqual(first["startLine"], 2)
        self.assertEqual(first["id"], second["id"])

    def test_excluded_directories_are_pruned_before_descent(self):
        ignored = self.root / "node_modules" / "nested"
        ignored.mkdir(parents=True)
        (ignored / "hidden.md").write_text("must not be visited")
        original = os.walk
        visited = []
        def recording(*args, **kwargs):
            for entry in original(*args, **kwargs):
                visited.append(Path(entry[0]))
                yield entry
        with patch("evidence.os.walk", recording):
            manifest, _ = snapshot(self.root, self.base / "pruned")
        self.assertEqual(visited, [self.root.resolve()])
        self.assertEqual(len(manifest), 1)

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_junction_outside_dangling_and_loop_are_pruned_with_warnings(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "outside.md").write_text("outside")
        vanished = self.base / "vanished"
        vanished.mkdir()
        links = [(self.root / "escape", outside), (self.root / "dangling", vanished), (self.root / "loop", self.root)]
        try:
            for link, target in links:
                result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
                self.assertEqual(result.returncode, 0, "Could not create regression junction")
            vanished.rmdir()
            manifest, warnings = snapshot(self.root, self.base / "junction-snapshot")
            self.assertEqual(len(manifest), 1)
            for link, _ in links:
                self.assertTrue(any(link.name in warning and "linked" in warning for warning in warnings))
        finally:
            # Remove only verified junction entries, never recurse through their targets.
            for link, _ in links:
                if link.is_junction():
                    link.rmdir()


if __name__ == "__main__":
    unittest.main()
