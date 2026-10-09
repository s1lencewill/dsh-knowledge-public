"""The source-card projection: page mapping, a measured location axis, and the three status axes.

A card is presentation, so the properties worth testing are the ones that keep it from becoming a claim:
the page comes only from the extraction's own marker, `locationVerified` is measured rather than
declared, the support axis is never anything but `not-assessed`, and no absolute path reaches a card that
a browser will render.

All offline: no model client, no engine worker, no container. Nothing here proves retrieval quality.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
os.environ.setdefault("DSH_KNOWLEDGE_TOKEN", "synthetic-token-for-source-card-test")
os.environ["DSH_KNOWLEDGE_ISOLATION"] = "off"
import evidence
import reader
import service
import source_card

#: Two pages of a synthetic PDF extraction. The marker is a whole line of its own, exactly what
#: `scripts/extract-literature.py` writes; the second page also mentions the words inside a sentence to
#: prove a passing mention is not a boundary.
PAGED = ("PDF page 1\n"
         "Intro line about the sample.\n"
         "Temperature 298 K.\n"
         "PDF page 2\n"
         "See the discussion of a PDF page layout in passing.\n"
         "Pressure 1 atm.\n"
         "PDF page 3\n"
         "Closing line.\n")

#: No markers at all: a Markdown source, or a PDF extracted without them.
UNPAGED = "Heading\n\nA note with no page markers.\nSecond line.\n"


class MarkerTests(unittest.TestCase):
    """The marker reader still exists, but as a **diagnostic** only: it can never decide a page."""

    def test_the_marker_reader_finds_the_nearest_marker_at_or_above_the_line(self):
        lines = source_card.split_lines(PAGED)
        self.assertEqual(source_card.pdf_page_for_line(lines, 1), 1)
        self.assertEqual(source_card.pdf_page_for_line(lines, 3), 1)
        self.assertEqual(source_card.pdf_page_for_line(lines, 4), 2)
        self.assertEqual(source_card.pdf_page_for_line(lines, 6), 2)
        self.assertEqual(source_card.pdf_page_for_line(lines, 8), 3)

    def test_a_passing_mention_is_not_a_page_boundary(self):
        """Only a whole-line marker counts: a sentence that says "PDF page" names no page."""
        lines = source_card.split_lines(PAGED)
        self.assertEqual(source_card.pdf_page_for_line(lines, 5), 2)

    def test_no_markers_means_nothing_to_hint_at(self):
        self.assertIsNone(source_card.pdf_page_for_line(source_card.split_lines(UNPAGED), 3))
        self.assertIsNone(source_card.pdf_page_for_line(source_card.split_lines(UNPAGED), 1))

    def test_nothing_is_guessed_for_a_missing_file_or_an_odd_line(self):
        self.assertIsNone(source_card.pdf_page_for_line(None, 1))
        self.assertIsNone(source_card.pdf_page_for_line([], 1))
        for bad in (0, -1, True, False, 1.5, "3", None):
            self.assertIsNone(source_card.pdf_page_for_line(source_card.split_lines(PAGED), bad),
                              "line %r must not resolve a page" % (bad,))

    def test_a_line_past_the_end_clamps_instead_of_inventing_one(self):
        """Past EOF there is no line to place, and the last known page is the honest answer for a line
        that does not exist: the caller's own range check rejects it first, and this must not crash."""
        lines = source_card.split_lines(PAGED)
        self.assertEqual(source_card.pdf_page_for_line(lines, 999), 3)


def pdf_record(**overrides):
    """A catalogue record for a mapped PDF, with the binding the map has to prove."""
    record = {"documentId": "d1", "fileName": "a.pdf", "textFile": "texts/a.txt", "pages": 3,
              "textSHA256": "text-hash", "pdfSHA256": "pdf-hash",
              "pageMap": [{"page": 1, "startLine": 1, "endLine": 11},
                          {"page": 2, "startLine": 12, "endLine": 18},
                          {"page": 3, "startLine": 19, "endLine": 25}],
              "pageMapSource": {"mappingVersion": 1, "source": "extraction-audit",
                                "auditTextSHA256": "text-hash", "pdfSHA256": "pdf-hash"}}
    record.update(overrides)
    return record


class LocationTests(unittest.TestCase):
    def test_a_mapped_pdf_reports_the_pdf_coordinate(self):
        located = source_card.location(12, 13, 10, 20, record=pdf_record())
        self.assertEqual(located["coordinateSpace"], "extracted-text")
        self.assertEqual(located["kind"], "pdf-page")
        self.assertEqual(located["pdfPage"], 2)
        self.assertEqual(located["pageSource"], source_card.PAGE_MAP)
        self.assertEqual((located["startLine"], located["endLine"]), (12, 13))
        self.assertEqual((located["startChar"], located["endChar"]), (10, 20))

    def test_a_non_pdf_source_gives_line_numbers_only(self):
        located = source_card.location(2, 2, 0, 5, record={"fileName": "notes.md", "pages": None}, hint=9)
        self.assertEqual(located["kind"], "text-line")
        self.assertIsNone(located["pdfPage"])
        self.assertEqual(located["pageSource"], source_card.NOT_PDF)
        # Not even a hint: a `PDF page N` line in a text file is not a page at all.
        self.assertIsNone(located["pageHint"])

    def test_offsets_are_omitted_rather_than_invented(self):
        located = source_card.location(1, 1, None, None, record=pdf_record())
        self.assertNotIn("startChar", located)
        self.assertNotIn("endChar", located)

    def test_a_pdf_without_a_map_says_the_mapping_was_never_established(self):
        located = source_card.location(3, 3, record={"fileName": "a.pdf", "pages": 3}, hint=9)
        self.assertIsNone(located["pdfPage"], "the text must never supply the page")
        self.assertEqual(located["pageSource"], source_card.UNMAPPED)
        self.assertEqual(located["pageHint"], 9, "the marker is kept, but only as a diagnostic hint")
        self.assertEqual(located["kind"], "text-line")


class PageMapTests(unittest.TestCase):
    """Only a map bound to this exact version may produce a page. Everything else resolves none.

    This is the difference between "the quote is on page 3" and "a line above the quote says page 3".
    """

    def test_a_usable_map_resolves_pages_and_reports_its_source(self):
        self.assertEqual(source_card.page_for_line(5, page_map=pdf_record()["pageMap"]),
                         (1, source_card.PAGE_MAP))
        self.assertEqual(source_card.page_for_line(18, page_map=pdf_record()["pageMap"]),
                         (2, source_card.PAGE_MAP))

    def test_a_document_that_prints_a_marker_cannot_move_the_page(self):
        """The exact spoof the map exists to defeat: a body line reading `PDF page 9`.

        The text scan is fooled, which is why it is not authoritative and no longer even a fallback.
        """
        text = "\n".join(["PDF page 1", "real page one line", "PDF page 9", "a line that pretends to be page nine"])
        self.assertEqual(source_card.pdf_page_for_line(source_card.split_lines(text), 4), 9,
                         "the naive scan is fooled, as expected")
        located = source_card.location(4, 4, record=pdf_record(
            pageMap=[{"page": 1, "startLine": 1, "endLine": 4}]))
        self.assertEqual(located["pdfPage"], 1)
        self.assertEqual(located["pageSource"], source_card.PAGE_MAP)

    def test_a_map_that_does_not_cover_the_line_reports_a_gap_and_never_the_text(self):
        located = source_card.location(99, 99, record=pdf_record(), hint=9)
        self.assertIsNone(located["pdfPage"])
        self.assertEqual(located["pageSource"], source_card.MAP_GAP)
        self.assertIsNone(located["pageHint"], "a gap must not reopen the text as a source of truth")

    def test_a_stale_binding_makes_the_map_unusable_and_the_text_is_not_consulted(self):
        stale = pdf_record(textSHA256="a-newer-text-version")
        located = source_card.location(12, 12, record=stale, hint=9)
        self.assertIsNone(located["pdfPage"])
        self.assertEqual(located["pageSource"], source_card.MAP_UNUSABLE)
        self.assertIsNone(located["pageHint"])

    def test_a_pdf_binding_mismatch_also_makes_the_map_unusable(self):
        located = source_card.location(12, 12, record=pdf_record(pdfSHA256="another-pdf"), hint=9)
        self.assertEqual(located["pageSource"], source_card.MAP_UNUSABLE)

    def test_a_map_recorded_under_another_layout_version_is_not_read(self):
        record = pdf_record()
        record["pageMapSource"] = dict(record["pageMapSource"], mappingVersion=99)
        self.assertEqual(source_card.location(12, 12, record=record, hint=9)["pageSource"],
                         source_card.MAP_UNUSABLE)

    def test_a_map_without_a_recorded_binding_is_not_trusted(self):
        record = pdf_record()
        del record["pageMapSource"]
        self.assertEqual(source_card.location(12, 12, record=record, hint=9)["pageSource"],
                         source_card.MAP_UNUSABLE)

    def test_malformed_maps_are_refused_rather_than_partly_read(self):
        for bad in ("nope", {}, [], [{"page": "1", "startLine": 1, "endLine": 9}], ["x", None]):
            record = pdf_record(pageMap=bad)
            located = source_card.location(12, 12, record=record, hint=9)
            self.assertEqual(located["pageSource"], source_card.MAP_UNUSABLE,
                             "pageMap=%r must not resolve a page" % (bad,))
            self.assertIsNone(located["pdfPage"])

    def test_the_binding_is_checked_with_the_records_own_hashes(self):
        usable, problem = source_card.usable_page_map(pdf_record())
        self.assertIsNone(problem)
        self.assertEqual(len(usable), 3)
        self.assertEqual(source_card.usable_page_map({}) , (None, None))

    def test_a_pdf_is_recognised_from_its_page_count_or_its_name(self):
        self.assertTrue(source_card.is_pdf_record({"pages": 3, "fileName": "x.txt"}))
        self.assertTrue(source_card.is_pdf_record({"fileName": "X.PDF"}))
        self.assertFalse(source_card.is_pdf_record({"pages": None, "fileName": "n.md"}))
        self.assertFalse(source_card.is_pdf_record({"pages": 0, "fileName": "n.md"}))
        self.assertFalse(source_card.is_pdf_record(None))

    def test_a_window_inside_one_page_reports_one_page(self):
        located = source_card.location(13, 18, record=pdf_record())
        self.assertEqual((located["pdfPage"], located["pdfPageEnd"]), (2, 2))

    def test_a_window_that_crosses_a_page_boundary_says_so(self):
        """One number would describe the first line while reading as if it covered the range."""
        located = source_card.location(18, 21, record=pdf_record())
        self.assertEqual((located["pdfPage"], located["pdfPageEnd"]), (2, 3))
        self.assertEqual(located["pageSource"], source_card.PAGE_MAP)


class ReproducesTests(unittest.TestCase):
    def test_a_matching_range_is_verified(self):
        text = "abcdefgh"
        self.assertTrue(source_card.reproduces(text, "cde", 2, 5))

    def test_a_range_that_does_not_rebuild_the_excerpt_is_refused(self):
        text = "abcdefgh"
        self.assertFalse(source_card.reproduces(text, "cde", 2, 6))
        self.assertFalse(source_card.reproduces(text, "cde", 3, 5))

    def test_out_of_range_and_malformed_input_are_refused_not_clamped(self):
        text = "abcdefgh"
        for excerpt, start, end in [("cde", -1, 5), ("cde", 5, 2), ("cde", 0, 99),
                                    ("cde", None, 5), (None, 2, 5), ("cde", 2.0, 5)]:
            self.assertFalse(source_card.reproduces(text, excerpt, start, end),
                             "(%r, %r, %r) must not verify" % (excerpt, start, end))

    def test_a_non_string_text_is_refused(self):
        self.assertFalse(source_card.reproduces(None, "cde", 2, 5))


class CardTests(unittest.TestCase):
    def test_support_is_never_assessed_by_this_projection(self):
        """The one invariant a card must never break, whatever the caller passes."""
        card = source_card.card("lib", "a.pdf", "quote", None, "v1", "engine",
                                identity="verified", location_verified=True)
        self.assertEqual(card["status"]["support"], "not-assessed")
        self.assertEqual(source_card.SUPPORT_NOT_ASSESSED, "not-assessed")

    def test_identity_and_parse_quality_are_carried_through_untouched(self):
        card = source_card.card("lib", "a.pdf", None, None, None, "catalogue",
                                parse_quality="layout-ambiguous", identity="conflict",
                                identity_detail="two files disagree on the year")
        self.assertEqual(card["status"]["identity"], "conflict")
        self.assertEqual(card["status"]["identityDetail"], "two files disagree on the year")
        self.assertEqual(card["parseQuality"], "layout-ambiguous")
        self.assertFalse(card["status"]["locationVerified"])

    def test_a_notice_is_not_by_itself_a_truncation_claim(self):
        # These two fields used to be collapsed (`truncated = flag or notices`), which made "the quoted range
        # stops mid-statement" and "the extracted text ends here without a recognized sentence boundary" the
        # same claim on the card. They are different facts about different things, and the caller is the only
        # one that knows which one it has: the notice is always rendered, the flag says what it means.
        card = source_card.card("lib", "a.pdf", "quote", None, "v1", "engine",
                                truncation_notices=["EXCERPT_INCOMPLETE_AT_EOF"])
        self.assertFalse(card["truncated"], "a text-end notice is not a truncation of the window")
        self.assertEqual(card["truncationNotices"], ["EXCERPT_INCOMPLETE_AT_EOF"])

        truncated = source_card.card("lib", "a.pdf", "quote", None, "v1", "engine",
                                     truncated=True, truncation_notices=["EXCERPT_TRUNCATED"])
        self.assertTrue(truncated["truncated"])
        self.assertEqual(truncated["truncationNotices"], ["EXCERPT_TRUNCATED"])

    def test_an_empty_notice_list_does_not_by_itself_mark_truncation(self):
        card = source_card.card("lib", "a.pdf", "quote", None, "v1", "engine", truncation_notices=[""])
        self.assertFalse(card["truncated"])
        self.assertEqual(card["truncationNotices"], [])

    def test_conflicts_are_reported_only_when_there_are_any(self):
        self.assertNotIn("conflicts", source_card.card("l", "a", None, None, None, None, conflicts=[]))
        self.assertIn("conflicts", source_card.card("l", "a", None, None, None, None, conflicts=[{"a": 1}]))

    def test_a_card_carries_a_relative_path_and_never_an_absolute_one(self):
        """A card is rendered in a browser: a filesystem path there is both a leak and a temptation."""
        root = str(Path(tempfile.gettempdir()).resolve())
        card = source_card.card("lib", "a.pdf", "quote", None, "v1", "engine",
                                relative_path="texts/a.txt")
        self.assertEqual(card["relativePath"], "texts/a.txt")
        self.assertNotIn(root, json.dumps(card))


class ReadWindowTests(unittest.TestCase):
    """`reader.read` derives the page from the bytes it verified, on every read."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "texts").mkdir()
        self.relative = "texts/paged.txt"
        self.written = PAGED.replace("\n", "\r\n").encode("utf-8")
        (self.root / self.relative).write_bytes(self.written)
        self.version = evidence.digest(self.written)
        self.workspace = str(self.root)
        self.secret = reader.derive_secret("synthetic-token-for-source-card-test")
        self.issuer = reader.Issuer(self.secret, self.workspace, "lib")

    def read(self, **kwargs):
        handle = self.issuer.issue(self.relative, self.version)
        return reader.read(self.secret, self.workspace, "lib", self.root, handle=handle, **kwargs)

    def test_the_window_carries_its_page_from_the_verified_bytes(self):
        window = self.read(max_chars=200, max_lines=200)
        self.assertEqual(window["pdfPage"], 1)
        self.assertTrue(window["locationVerified"])

    def test_a_window_starting_on_another_page_reports_that_page(self):
        window = self.read(start_line=4, max_chars=200, max_lines=200)
        self.assertEqual(window["pdfPage"], 2)
        self.assertEqual(window["startLine"], 4)

    def test_an_unpaged_source_reports_no_page_and_still_verifies_its_location(self):
        self.relative = "texts/unpaged.txt"
        (self.root / self.relative).write_bytes(UNPAGED.encode("utf-8"))
        self.version = evidence.digest(UNPAGED.encode("utf-8"))
        window = self.read(max_chars=200, max_lines=200)
        self.assertIsNone(window["pdfPage"])
        self.assertTrue(window["locationVerified"])


class RouteCardTests(unittest.TestCase):
    """The two local routes attach a card built from the real file, with no engine and no model."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "texts").mkdir()
        self.written = PAGED.encode("utf-8")
        (self.root / "texts" / "paged.txt").write_bytes(self.written)
        self.version = evidence.digest(self.written)
        self.workspace = str(self.root)
        self.library = {"id": "synthetic", "name": "synthetic", "root": str(self.root),
                        "syntheticFixture": True}
        self.issuer = reader.Issuer(reader.derive_secret(service.TOKEN), self.workspace, "synthetic")
        # The catalogue the routes read: a bound page map for the PDF, nothing for the Markdown note.
        self.catalogue = self.root / "catalogue.json"
        self.write_catalogue({
            "documentId": "d1", "fileName": "paged.pdf", "textFile": "texts/paged.txt", "pages": 3,
            "textSHA256": self.version,
            "pageMap": [{"page": 1, "startLine": 1, "endLine": 3},
                        {"page": 2, "startLine": 4, "endLine": 5},
                        {"page": 3, "startLine": 6, "endLine": 8}],
            "pageMapSource": {"mappingVersion": 1, "auditTextSHA256": self.version},
        }, {"documentId": "d2", "fileName": "notes.md", "textFile": "texts/notes.md", "pages": None,
            "textSHA256": self.version})
        self.previous_catalog = os.environ.get("DSH_KNOWLEDGE_CATALOG")
        os.environ["DSH_KNOWLEDGE_CATALOG"] = str(self.catalogue)
        self.addCleanup(self.restore_catalog)

    def restore_catalog(self):
        if self.previous_catalog is None:
            os.environ.pop("DSH_KNOWLEDGE_CATALOG", None)
        else:
            os.environ["DSH_KNOWLEDGE_CATALOG"] = self.previous_catalog

    def write_catalogue(self, *records):
        self.catalogue.write_bytes(json.dumps(
            {"documents": {record["textFile"]: record for record in records}}).encode("utf-8"))

    def test_a_read_route_returns_a_card_with_the_three_axes_separate(self):
        handle = self.issuer.issue("texts/paged.txt", self.version)
        body = service.Read(workspace=self.workspace, libraryId="synthetic", fileHandle=handle,
                            startLine=1, maxChars=200, maxLines=200, session="s1")
        result = service.perform_read(self.library, body)
        card = result["sourceCard"]
        self.assertEqual(card["libraryId"], "synthetic")
        self.assertEqual(card["citationOrigin"], "read")
        self.assertEqual(card["sourceVersion"], self.version)
        self.assertEqual(card["location"]["pdfPage"], 1)
        self.assertEqual(card["location"]["pageSource"], source_card.PAGE_MAP)
        self.assertTrue(card["status"]["locationVerified"])
        self.assertEqual(card["status"]["support"], "not-assessed")
        self.assertEqual(card["status"]["identity"], "not-assessed")
        # The original file's name comes from the record; the citable text file is the fallback only.
        self.assertEqual(card["fileName"], "paged.pdf")
        self.assertEqual(card["relativePath"], "texts/paged.txt")
        self.assertNotIn(str(self.root), json.dumps(card))

    def test_a_read_route_card_reports_the_page_of_its_own_start_line(self):
        handle = self.issuer.issue("texts/paged.txt", self.version)
        body = service.Read(workspace=self.workspace, libraryId="synthetic", fileHandle=handle,
                            startLine=4, maxChars=200, maxLines=200, session="s1")
        result = service.perform_read(self.library, body)
        self.assertEqual(result["sourceCard"]["location"]["pdfPage"], 2)

    def test_a_read_of_an_unmapped_pdf_reports_no_page_and_keeps_the_marker_as_a_hint(self):
        """The reader's marker scan is real text scanning; it must not become the reported page."""
        self.write_catalogue({"documentId": "d1", "fileName": "paged.pdf", "textFile": "texts/paged.txt",
                              "pages": 3, "textSHA256": self.version})
        handle = self.issuer.issue("texts/paged.txt", self.version)
        body = service.Read(workspace=self.workspace, libraryId="synthetic", fileHandle=handle,
                            startLine=4, maxChars=200, maxLines=200, session="s1")
        result = service.perform_read(self.library, body)
        self.assertIsNone(result["pdfPage"])
        self.assertEqual(result["pageSource"], source_card.UNMAPPED)
        self.assertEqual(result["pageHint"], 2, "the marker is kept, but only as a hint")
        card = result["sourceCard"]
        self.assertIsNone(card["location"]["pdfPage"])
        self.assertEqual(card["location"]["pageSource"], source_card.UNMAPPED)
        self.assertEqual(card["location"]["pageHint"], 2)

    def test_a_read_of_a_markdown_source_never_reports_a_page(self):
        (self.root / "texts" / "notes.md").write_bytes(UNPAGED.encode("utf-8"))
        version = evidence.digest(UNPAGED.encode("utf-8"))
        handle = self.issuer.issue("texts/notes.md", version)
        body = service.Read(workspace=self.workspace, libraryId="synthetic", fileHandle=handle,
                            startLine=3, maxChars=200, maxLines=200, session="s1")
        result = service.perform_read(self.library, body)
        self.assertIsNone(result["pdfPage"])
        self.assertEqual(result["pageSource"], source_card.NOT_PDF)
        self.assertIsNone(result["pageHint"])

        self.assertEqual(result["sourceCard"]["status"]["support"], "not-assessed")

    def test_a_refused_mapping_still_gets_a_card_with_no_location(self):
        """A refused mapping is still worth a card - identity and provenance are known without a read -
        but it must not look like something that was read."""
        import catalog

        window, refusal = catalog.verify_text_file(
            self.library,
            {"documentId": "d1", "fileName": "paged.pdf", "textFile": "texts/missing.txt"},
            self.issuer, {})
        self.assertIsNone(window)
        self.assertTrue(refusal["code"])
        card = source_card.card(library_id="synthetic", file_name="paged.pdf", excerpt=None,
                                location_fields=None, source_version=None, citation_origin="catalogue",
                                identity=source_card.IDENTITY_NOT_ASSESSED, location_verified=False)
        card["truncationNotices"] = ["DOCUMENT_NOT_READABLE"]
        card["truncated"] = True
        self.assertTrue(card["truncated"])
        self.assertIsNone(card["location"])
        self.assertIsNone(card["excerpt"])
        self.assertEqual(card["status"]["support"], "not-assessed")


class AttachTests(unittest.TestCase):
    """`attach_source_cards` enriches a finished query result without changing what it claims."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "texts").mkdir()
        self.text = PAGED
        (self.root / "texts" / "paged.txt").write_bytes(self.text.encode("utf-8"))
        self.catalogue = self.root / "catalogue.json"
        self.text_version = evidence.digest(self.text.encode("utf-8"))
        self.record = {
            "documentId": "d1", "titleRaw": "Synthetic study", "fileName": "paged.pdf",
            "textFile": "texts/paged.txt", "pages": 3, "quality": "clean", "verification": "verified",
            "textSHA256": self.text_version,
            "pageMap": [{"page": 1, "startLine": 1, "endLine": 3},
                        {"page": 2, "startLine": 4, "endLine": 5},
                        {"page": 3, "startLine": 6, "endLine": 8}],
            "pageMapSource": {"mappingVersion": 1, "auditTextSHA256": self.text_version},
        }
        self.write_catalogue()
        self.previous = os.environ.get("DSH_KNOWLEDGE_CATALOG")
        os.environ["DSH_KNOWLEDGE_CATALOG"] = str(self.catalogue)
        self.addCleanup(self.restore)

    def write_catalogue(self):
        self.catalogue.write_bytes(json.dumps(
            {"documents": {self.record["textFile"]: self.record}}).encode("utf-8"))

    def restore(self):
        if self.previous is None:
            os.environ.pop("DSH_KNOWLEDGE_CATALOG", None)
        else:
            os.environ["DSH_KNOWLEDGE_CATALOG"] = self.previous

    def result(self):
        return {"status": "complete", "answer": "", "warnings": [], "evidence": [{
            "id": "e1", "libraryId": "synthetic", "relativePath": "texts/paged.txt",
            "sourceVersion": "v" * 64, "startLine": 4, "endLine": 5, "excerpt": "See the discussion",
            "excerptSource": "engine", "excerptComplete": True,
            # The pipeline's marker scan, which the card must treat as a hint only.
            "location": {"pdfPage": 2, "startLine": 4, "endLine": 5},
            "locationVerified": True,
        }]}

    def test_the_card_picks_up_identity_and_parse_quality_from_the_catalogue(self):
        card = self.result()
        service.attach_source_cards(card)
        attached = card["evidence"][0]["sourceCard"]
        self.assertEqual(attached["title"], "Synthetic study")
        self.assertEqual(attached["fileName"], "paged.pdf")
        self.assertEqual(attached["parseQuality"], "clean")
        self.assertEqual(attached["status"]["identity"], "verified")
        self.assertEqual(attached["status"]["support"], "not-assessed")
        self.assertTrue(attached["status"]["locationVerified"])
        self.assertEqual(attached["location"]["pdfPage"], 2)
        self.assertEqual(attached["location"]["pageSource"], source_card.PAGE_MAP)

    def test_the_pipelines_marker_page_is_only_a_hint_when_the_map_cannot_be_used(self):
        """The pipeline's location came from scanning the text; without a bound map it decides nothing."""
        del self.record["pageMapSource"]
        self.write_catalogue()
        card = self.result()
        service.attach_source_cards(card)
        attached = card["evidence"][0]["sourceCard"]
        self.assertIsNone(attached["location"]["pdfPage"])
        self.assertEqual(attached["location"]["pageSource"], source_card.MAP_UNUSABLE)
        self.assertIsNone(attached["location"]["pageHint"])
        self.assertEqual(attached["status"]["support"], "not-assessed")

    def test_an_unreadable_excerpt_is_flagged_rather_than_silently_softened(self):
        card = self.result()
        card["evidence"][0]["excerptComplete"] = False
        service.attach_source_cards(card)
        attached = card["evidence"][0]["sourceCard"]
        self.assertTrue(attached["truncated"])
        self.assertEqual(attached["truncationNotices"], ["EXCERPT_TRUNCATED"])

    def test_a_missing_catalogue_leaves_identity_unassessed_and_adds_no_claim(self):
        os.environ["DSH_KNOWLEDGE_CATALOG"] = str(self.root / "absent.json")
        card = self.result()
        service.attach_source_cards(card)
        attached = card["evidence"][0]["sourceCard"]
        self.assertEqual(attached["status"]["identity"], "not-assessed")
        self.assertIsNone(attached["title"])
        self.assertEqual(attached["status"]["support"], "not-assessed")

    def test_an_empty_result_is_left_alone(self):
        payload = {"status": "partial", "answer": "", "evidence": [], "warnings": ["NO_CANDIDATES"]}
        self.assertIs(service.attach_source_cards(payload), payload)
        self.assertEqual(payload["evidence"], [])


if __name__ == "__main__":
    unittest.main()
