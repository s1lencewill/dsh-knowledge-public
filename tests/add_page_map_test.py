"""The page-map enrichment: what it publishes, and what it refuses to publish.

The stake is that a map is only meaningful for the text version it came from. A wrongly published map is
worse than no map, because the service trusts it and hands a reader a confident page number. So these tests
are mostly about refusals, and about the audit distinguishing a real gap from an arithmetic artefact.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
SCRIPT = BASE / "scripts" / "add-page-map.py"

TEXT = "PDF page 1\nalpha\nbeta\nPDF page 2\ngamma\ndelta\n"


def digest(data):
    return hashlib.sha256(data).hexdigest()


class EnrichTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.texts = self.root / "texts"
        self.audits = self.root / "audit"
        self.texts.mkdir()
        self.audits.mkdir()
        self.name = "doc.txt"
        self.text_bytes = TEXT.encode("utf-8")
        (self.texts / self.name).write_bytes(self.text_bytes)
        self.text_hash = digest(self.text_bytes)
        self.pdf_hash = digest(b"pdf bytes")
        self.record = {
            "documentId": "d1", "fileName": "doc.pdf", "textFile": self.name, "pages": 2,
            "pdfSHA256": self.pdf_hash, "textSHA256": self.text_hash,
            "verification": "verified", "quality": "clean",
        }
        self.audit = {
            "text": "doc", "textFile": self.name, "textSHA256": self.text_hash,
            "pdfSHA256": self.pdf_hash, "extractor": "extract-literature.py", "extractorVersion": "2",
            "pages": [{"page": 1, "startLine": 1, "endLine": 3}, {"page": 2, "startLine": 4, "endLine": 6}],
        }
        self.write_audit()

    def write_audit(self, **overrides):
        payload = dict(self.audit)
        payload.update(overrides)
        (self.audits / "doc.json").write_bytes(json.dumps(payload).encode("utf-8"))

    def write_catalogue(self, **overrides):
        record = dict(self.record)
        record.update(overrides)
        self.catalogue = self.root / "identity.json"
        self.catalogue.write_bytes(json.dumps({"version": "2.0", "documents": {"d1": record}}).encode("utf-8"))
        return self.catalogue

    def enrich(self, *extra):
        out = self.root / "identity-pagemap.json"
        report = self.root / "report.json"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--catalogue", str(self.write_catalogue()),
             "--library-root", str(self.texts), "--audit-dir", str(self.audits),
             "--out", str(out), "--report", str(report), *extra],
            capture_output=True, text=True)
        payload = json.loads(out.read_bytes().decode("utf-8")) if out.is_file() else None
        audit = json.loads(report.read_bytes().decode("utf-8")) if report.is_file() else None
        return result, payload, audit

    def test_a_bound_map_is_published_with_its_provenance(self):
        result, payload, audit = self.enrich()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        record = payload["documents"]["d1"]
        self.assertEqual(record["pageMap"], [{"page": 1, "startLine": 1, "endLine": 3},
                                             {"page": 2, "startLine": 4, "endLine": 6}])
        self.assertEqual(record["pageMapSource"]["mappingVersion"], payload["pageMapVersion"])
        self.assertEqual(record["pageMapSource"]["auditTextSHA256"], self.text_hash)
        self.assertEqual(record["pageMapSource"]["auditFile"], "doc.json")
        self.assertEqual(audit["summary"]["mapped"], 1)
        self.assertEqual(audit["summary"]["documentsWithGaps"], 0)
        # The text's trailing newline must not be counted as a line, or every document would show a gap.
        self.assertEqual(audit["documents"][0]["coverage"]["lines"], 6)
        self.assertEqual(audit["documents"][0]["coverage"]["coveredLines"], 6)

    def test_an_edited_text_is_refused_rather_than_mapped(self):
        """The map describes a text that no longer exists: publishing it would be a confident lie."""
        (self.texts / self.name).write_bytes((TEXT + "an extra line\n").encode("utf-8"))
        result, payload, audit = self.enrich()
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("pageMap", payload["documents"]["d1"])
        self.assertEqual(audit["summary"]["mapped"], 0)
        self.assertIn("no longer matches", audit["documents"][0]["reason"])

    def test_an_audit_from_another_text_version_is_refused(self):
        self.write_audit(textSHA256="a-different-text")
        result, payload, audit = self.enrich()
        self.assertEqual(audit["summary"]["mapped"], 0)
        self.assertIn("different text version", audit["documents"][0]["reason"])

    def test_an_audit_from_another_pdf_is_refused(self):
        self.write_audit(pdfSHA256="a-different-pdf")
        _, payload, audit = self.enrich()
        self.assertEqual(audit["summary"]["mapped"], 0)
        self.assertIn("different PDF", audit["documents"][0]["reason"])

    def test_an_audit_without_page_ranges_is_refused(self):
        self.write_audit(pages=[])
        _, payload, audit = self.enrich()
        self.assertEqual(audit["summary"]["mapped"], 0)
        self.assertIn("no page line ranges", audit["documents"][0]["reason"])

    def test_a_missing_text_file_is_refused(self):
        (self.texts / self.name).unlink()
        _, payload, audit = self.enrich()
        self.assertEqual(audit["summary"]["mapped"], 0)
        self.assertIn("missing", audit["documents"][0]["reason"])

    def test_a_real_gap_in_the_map_is_reported(self):
        """A hole the extractor left is a defect to surface, not to smooth over."""
        self.write_audit(pages=[{"page": 1, "startLine": 1, "endLine": 2},
                                {"page": 2, "startLine": 5, "endLine": 6}])
        _, payload, audit = self.enrich()
        self.assertEqual(audit["summary"]["mapped"], 1)
        self.assertEqual(audit["summary"]["documentsWithGaps"], 1)
        self.assertEqual(audit["gaps"][0]["gaps"], [[3, 4]])

    def test_overlapping_ranges_are_reported(self):
        self.write_audit(pages=[{"page": 1, "startLine": 1, "endLine": 4},
                                {"page": 2, "startLine": 3, "endLine": 6}])
        _, payload, audit = self.enrich()
        self.assertEqual(audit["summary"]["documentsWithOverlaps"], ["d1"])

    def test_the_input_catalogue_is_never_modified(self):
        catalogue = self.write_catalogue()
        before = catalogue.read_bytes()
        self.enrich()
        self.assertEqual(catalogue.read_bytes(), before, "the previous version is the rollback path")

    def test_the_output_may_not_be_the_input(self):
        catalogue = self.write_catalogue()
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--catalogue", str(catalogue), "--library-root", str(self.texts),
             "--audit-dir", str(self.audits), "--out", str(catalogue)],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback path", result.stderr + result.stdout)

    def test_the_activation_record_names_both_versions_and_their_hashes(self):
        activation = self.root / "activation.json"
        result, payload, _ = self.enrich("--activation", str(activation))
        self.assertEqual(result.returncode, 0)
        record = json.loads(activation.read_bytes().decode("utf-8"))
        self.assertEqual(record["active"], "identity-pagemap.json")
        self.assertEqual(record["previous"], "identity.json")
        self.assertEqual(record["activeSHA256"], digest((self.root / "identity-pagemap.json").read_bytes()))
        self.assertEqual(record["previousSHA256"], digest(self.catalogue.read_bytes()))
        self.assertEqual(record["pageMapVersion"], 1)
        self.assertIn("rollback", record)

    def test_a_non_pdf_document_is_left_without_a_map_and_says_why(self):
        self.catalogue = None
        record = {"documentId": "d2", "fileName": "notes.md", "textFile": "notes.md", "pages": None,
                  "textSHA256": self.text_hash}
        (self.texts / "notes.md").write_bytes(self.text_bytes)
        catalogue = self.root / "identity.json"
        catalogue.write_bytes(json.dumps({"version": "2.0", "documents": {"d2": record}}).encode("utf-8"))
        out = self.root / "out.json"
        report = self.root / "report.json"
        subprocess.run(
            [sys.executable, str(SCRIPT), "--catalogue", str(catalogue), "--library-root", str(self.texts),
             "--audit-dir", str(self.audits), "--out", str(out), "--report", str(report)],
            capture_output=True, text=True)
        audit = json.loads(report.read_bytes().decode("utf-8"))
        self.assertEqual(audit["summary"]["mapped"], 0)
        self.assertEqual(audit["summary"]["unmapped"], 1)
        self.assertFalse(json.loads(out.read_bytes().decode("utf-8"))["documents"]["d2"].get("pageMap"))


if __name__ == "__main__":
    unittest.main()
