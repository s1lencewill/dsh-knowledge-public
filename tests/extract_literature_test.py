"""Reusable rules of the layout-aware extractor, on synthetic geometry.

No PDF is needed: the extractor's decisions are pure functions of fragment positions, so the rules that
matter - line reconstruction, super/subscript attachment, region boundaries, paragraph boundaries,
normalisation and the version diff - are tested directly.
"""
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("extract_literature", ROOT / "scripts" / "extract-literature.py")
extractor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extractor)


def fragment(x, y, text, size=10.0, advance=None):
    return (x, y, text, size, advance if advance is not None else extractor.glyph_advance(text, None, size))


def segments_of(rows):
    return extractor.split_rows(rows, page_width=600.0)


class LineReconstructionTests(unittest.TestCase):
    def test_each_glyph_on_its_own_operation_becomes_one_visual_line(self):
        # The Chinese-PDF defect: one text operation per character. Coordinates rebuild the line.
        fragments = [fragment(50 + 12 * index, 700.0, character, 12.0) for index, character in
                     enumerate("界面反应动力学研究")]
        rows = extractor.cluster_rows(fragments)
        self.assertEqual(len(rows), 1)
        self.assertEqual("".join(text for _, text, _, _ in rows[0]["fragments"]), "界面反应动力学研究")
        segments = extractor.split_rows(rows, page_width=600.0)
        self.assertEqual([segment["text"] for segment in segments], ["界面反应动力学研究"])

    def test_a_superscript_is_attached_to_its_sentence(self):
        text = "The measured rate is slow"
        size = 10.0
        end = 50 + extractor.glyph_advance(text, None, size)
        rows = extractor.cluster_rows([fragment(50, 700, text, size),
                                       fragment(end - 2, 704, "12", 6.0)])
        segments = extractor.split_rows(rows, page_width=600.0)
        merged, count = extractor.merge_inline_markup(segments)
        self.assertEqual(count, 1)
        self.assertEqual(len(merged), 1)
        self.assertIn("12", merged[0]["text"])
        self.assertTrue(merged[0]["text"].startswith("The measured rate is slow"))

    def test_a_wide_gap_starts_a_new_region_but_a_word_space_does_not(self):
        fragments = [fragment(50, 700, "left column ends here"), fragment(150, 700, " continue"),
                     fragment(380, 700, "right column text")]
        rows = extractor.cluster_rows(fragments)
        segments = extractor.split_rows(rows, page_width=600.0)
        self.assertEqual([segment["text"] for segment in segments],
                         ["left column ends here continue", "right column text"],
                         "a word space must not split, a column gutter must")

    def test_a_multi_line_operation_is_split_into_separate_lines(self):
        pieces, extra = extractor.split_fragment("Supporting\nInformation is available", 50, 700, 10.0, 100.0)
        self.assertEqual(extra, 1)
        self.assertEqual([piece[2] for piece in pieces], ["Supporting", "Information is available"])
        self.assertLess(pieces[1][1], pieces[0][1])

    def test_a_line_never_contains_a_line_break(self):
        text, breaks = extractor.clean_line("coupled to\nthe interface")
        self.assertEqual(text, "coupled to the interface")
        self.assertEqual(breaks, 1)
        cjk, _ = extractor.clean_line("界面\n反应")
        self.assertEqual(cjk, "界面反应", "no invented space between CJK characters")


class OrderingTests(unittest.TestCase):
    def test_a_gutter_orders_columns_left_then_right(self):
        segments = [{"y": 700, "size": 10, "text": "left one", "x0": 50, "x1": 150, "fragments": []},
                    {"y": 700, "size": 10, "text": "right one", "x0": 350, "x1": 450, "fragments": []},
                    {"y": 680, "size": 10, "text": "left two", "x0": 50, "x1": 150, "fragments": []},
                    {"y": 680, "size": 10, "text": "right two", "x0": 350, "x1": 450, "fragments": []}]
        ordered = extractor.order_region(segments, 600.0)
        self.assertEqual([segment["text"] for segment in ordered],
                         ["left one", "left two", "right one", "right two"])

    def test_a_full_width_title_is_read_before_the_side_by_side_parts(self):
        segments = [{"y": 740, "size": 16, "text": "A full width title", "x0": 50, "x1": 550, "fragments": []},
                    {"y": 700, "size": 10, "text": "abstract left", "x0": 50, "x1": 250, "fragments": []},
                    {"y": 700, "size": 10, "text": "significance right", "x0": 350, "x1": 550, "fragments": []},
                    {"y": 682, "size": 10, "text": "abstract continues", "x0": 50, "x1": 250, "fragments": []}]
        ordered = extractor.order_region(segments, 600.0)
        texts = [segment["text"] for segment in ordered]
        self.assertEqual(texts[0], "A full width title")
        self.assertLess(texts.index("abstract left"), texts.index("significance right"),
                        "the left region is read before the right one, not interleaved")

    def test_headings_captions_and_columns_are_not_joined_into_paragraphs(self):
        lines = [
            {"text": "Results", "y": 700, "size": 12, "isHeading": True, "isCaption": False,
             "isHeaderFooter": False, "column": 1},
            {"text": "First paragraph sentence one", "y": 686, "size": 10, "isHeading": False,
             "isCaption": False, "isHeaderFooter": False, "column": 1},
            {"text": "wrapped continuation", "y": 676, "size": 10, "isHeading": False, "isCaption": False,
             "isHeaderFooter": False, "column": 1},
            {"text": "Figure 1. A caption", "y": 660, "size": 9, "isHeading": False, "isCaption": True,
             "isHeaderFooter": False, "column": 1},
            {"text": "Body after the caption", "y": 646, "size": 10, "isHeading": False, "isCaption": False,
             "isHeaderFooter": False, "column": 1},
            {"text": "right column text", "y": 640, "size": 10, "isHeading": False, "isCaption": False,
             "isHeaderFooter": False, "column": 2},
        ]
        paragraphs = extractor.paragraph_join(lines)
        self.assertEqual(paragraphs[0], "Results")
        self.assertEqual(paragraphs[1], "First paragraph sentence one wrapped continuation")
        self.assertEqual(paragraphs[2], "Figure 1. A caption")
        self.assertEqual(paragraphs[3], "Body after the caption")
        self.assertEqual(paragraphs[4], "right column text")


class NormalisationAndDiffTests(unittest.TestCase):
    def test_normalisation_is_matching_only(self):
        self.assertEqual(extractor.normalize_for_match("wrapped hy-\nphen"), "wrapped hyphen")
        self.assertEqual(extractor.squash("Howthe Acidity"), extractor.squash("How the Acidity"))

    def test_the_version_diff_reports_missing_and_recovered_content(self):
        old = "PDF page 1\nabcdef\n"
        new = "PDF page 1\nabcde\n"
        report = extractor.compare(old, new)
        self.assertFalse(report["sameContent"])
        self.assertGreater(report["contentCharsMissingFromNew"], 0)
        same = extractor.compare("PDF page 1\nabcdef\n", "PDF page 1\na\nb\nc\nd\ne\nf\n")
        self.assertTrue(same["sameContent"], "re-wrapping is not content loss")
        self.assertEqual(same["newSingleCharLines"], 6)

    def test_strip_markers_ignores_the_page_header_lines(self):
        self.assertEqual(extractor.strip_markers("PDF page 1\ntext\nPDF page 2\nmore\n").strip(), "text\n\nmore")


if __name__ == "__main__":
    unittest.main()
