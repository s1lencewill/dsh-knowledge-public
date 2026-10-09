import sys, unittest, tempfile, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from pipeline import source_window, source_excerpt
from markdown_windows import quality_hints

class MarkdownWindowsTests(unittest.TestCase):
    def window(self, text, query):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.md"; path.write_text(text, encoding="utf-8")
            block, facts = source_window(path, query)
            self.assertIn(block, text)
            self.assertLessEqual(facts["sampledCandidates"], 128)
            return block, facts
    def legacy(self, text, query):
        lines = text.split("\n"); terms = re.findall(r"[0-9A-Za-z_-]{3,}", query.lower())
        i = next((i for i,l in enumerate(lines) if any(t in l.lower() for t in terms)), 0)
        return "\n".join(lines[max(0,i-2):max(0,i-2)+12])
    def test_body_beats_title_for_content(self):
        text="---\ntitle: sampling\nurl: https://invalid/sampling\n---\n" + "\n".join(["padding"]*20) + "\n采样方法的局限是成本高。ANSWER\n"
        self.assertNotIn("ANSWER", self.legacy(text,"采样方法局限"))
        block,facts=self.window(text,"采样方法局限");self.assertIn("ANSWER",block);self.assertEqual(facts["structureType"],"body")
    def test_ascii_title_also_matches(self):
        text="---\ntitle: umbrella\n---\n"+"\n".join(["padding"]*25)+"\numbrella cost ANSWER."
        self.assertNotIn("ANSWER",self.legacy(text,"umbrella cost"));self.assertIn("ANSWER",self.window(text,"umbrella cost")[0])
    def test_metadata_question(self):
        block,facts=self.window("---\ntitle: Example\nauthor: Ada\n---\nBody.","author")
        self.assertIn("Ada",block);self.assertEqual(facts["structureType"],"metadata")
    def test_only_metadata(self):
        block,facts=self.window("---\ntitle: Example\n---", "比较方法")
        self.assertEqual(facts["fallbackReason"],"only-nonpreferred");self.assertIsNone(facts["bodyStartLine"])
    def test_unclosed_front_matter(self):
        block,facts=self.window("---\ntitle: example\n"+"\n".join(["padding"]*20)+"\n条件限制 ANSWER", "条件限制")
        self.assertIn("ANSWER",block);self.assertEqual(facts["structureType"],"unknown")
    def test_code_table_and_references(self):
        text="# Example\n```py\ncommand ANSWER\n```\n\n| cost | 3 |\n\n# References\ncommand decoy"
        self.assertIn("ANSWER",self.window(text,"command code")[0]);self.assertIn("| cost",self.window(text,"cost table")[0])
    def test_distributed_and_no_claim(self):
        text="\n".join(["blank"]*200)+"\n条件限制 ANSWER"
        self.assertIn("ANSWER",self.window(text,"条件限制")[0]);self.assertNotIn("support", self.window(text,"不存在")[1])
    def test_quality_never_repairs(self):
        text="the only input parameter considered is for the rare event."
        self.assertEqual(quality_hints(text),["POSSIBLE_MATH_LOSS"])
        self.assertNotIn("Δ",self.window(text,"parameter")[0]);self.assertEqual(quality_hints("parameter is 3."),[])

    def test_long_line_remains_bounded_and_verbatim(self):
        text="原文"*10000
        block,facts=self.window(text,"原文")
        self.assertLessEqual(len(block),3000);self.assertEqual(block,text[:3000])

    def test_author_mentioned_in_content_question_is_not_metadata_intent(self):
        text="---\nauthor: Ada\n---\n"+"\n".join(["padding"]*20)+"\n作者讨论方法成本 ANSWER."
        block,facts=self.window(text,"作者讨论的方法成本")
        self.assertIn("ANSWER",block);self.assertEqual(facts["structureType"],"body")
