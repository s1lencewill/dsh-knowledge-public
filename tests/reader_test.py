"""The on-demand reader: identifiers, path boundaries, windows, budgets and consistency.

Pure module-level tests: no HTTP service, no engine worker and no model client are involved. The
service-level behaviour (authorization, lifecycle and limits over the wire) lives in
`tests/service_read_test.py`.
"""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import evidence
import pipeline
import reader

TOKEN = "synthetic-token-for-reader-tests"
SECRET = reader.derive_secret(TOKEN)
LIBRARY = "fixture"

#: Mixed content: CJK, emoji, a non-BMP ideograph, a line longer than one read, a unit on the line
#: after its value, a negation that continues onto the next line, and a two-line range.
MIXED = ("开头 😀 𠀀 证据：体相主导，界面约 1%\n"
         "第二行尾巴\n"
         + "长行 " + ("很长" * 400) + "\n"
         "数值 3.0\n"
         "单位 M/atm\n"
         "否定：界面水解并不主导，\n"
         "而是体相过程主导。\n"
         "区间 10-40\n"
         "nm\n"
         "结尾一行\n")


class Fixture:
    """One temporary library root with a helper for minting handles into it."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="reader-test-"))

    def write(self, name, text, encoding="utf-8", newline="\n"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # Explicit newline control: CRLF has to reach the file unchanged for the folding test.
        with path.open("w", encoding=encoding, newline="") as handle:
            handle.write(text.replace("\n", newline))
        return path

    def handle(self, name="a.txt", version=None, library=LIBRARY, workspace=None):
        data = (self.root / name).read_bytes()
        return reader.issue_handle(SECRET, str(workspace or self.root), library, name,
                                   version or evidence.digest(data))

    def service_handle(self, name, recorded_path, library=LIBRARY, version="0" * 64):
        """A handle whose recorded path is deliberately not the real one (for boundary tests)."""
        return reader.issue_handle(SECRET, str(self.root), library, recorded_path, version)

    def read(self, **kwargs):
        workspace = str(kwargs.pop("workspace", self.root))
        library = kwargs.pop("library", LIBRARY)
        if "handle" not in kwargs and kwargs.get("cursor") is None:
            kwargs["handle"] = self.handle()
        return reader.read(SECRET, workspace, library, self.root, **kwargs)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


class ReadTestCase(unittest.TestCase):
    def setUp(self):
        self.fixtures = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for fixture in self.fixtures:
            fixture.close()

    def fixture(self, text=MIXED, name="a.txt", **kwargs):
        fixture = Fixture()
        self.fixtures.append(fixture)
        fixture.write(name, text, **kwargs)
        return fixture

    def refusal(self, code, callable_, *args, **kwargs):
        with self.assertRaises(reader.ReadError) as caught:
            callable_(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception)
        return caught.exception


class RefusalFactsTests(ReadTestCase):
    """The structured half of a recoverable read refusal.

    A refusal that names *which* part of the request was wrong, and the bound it violated, is the difference
    between a caller correcting itself and a caller guessing: the observed session received the generic
    "check the library id and the parameters" and concluded the reference must be stale, abandoning a file it
    could have read. These tests pin both halves of that contract - what the facts say, and what they must
    never say.
    """

    def facts(self, exception):
        self.assertIsNotNone(exception.facts, "a recoverable refusal must carry its facts")
        return exception.facts

    def test_a_start_line_past_the_end_names_the_real_line_count(self):
        fixture = self.fixture("一行\n二行\n三行")  # 3 lines, no trailing newline
        error = self.refusal(reader.READ_RANGE_INVALID, fixture.read, start_line=480)
        facts = self.facts(error)
        self.assertEqual(facts["reason"], reader.READ_REASON_START_LINE_PAST_END)
        self.assertEqual(facts["requestedStartLine"], 480)
        self.assertEqual(facts["fileLines"], 3, "the bound must be the file's real line count")
        self.assertEqual(facts["suggestedAction"], reader.READ_ACTION_CORRECT_START_LINE)
        self.assertEqual(set(facts), {"reason", "requestedStartLine", "fileLines", "suggestedAction"},
                         "the facts are a closed shape: no extra field can appear")

    def test_the_line_count_is_the_same_one_a_window_reports(self):
        # One definition of "how many lines", used by the refusal and by the window it refuses. If these two
        # ever disagreed, a caller could be told a bound it cannot satisfy.
        for source in ("一行\n二行\n三行\n", "一行\n二行\n三行", "", "只有一行"):
            fixture = self.fixture(source)
            window = fixture.read()
            error = self.refusal(reader.READ_RANGE_INVALID, fixture.read, start_line=window["fileLines"] + 2)
            self.assertEqual(self.facts(error)["fileLines"], window["fileLines"], repr(source))

    def test_the_last_line_is_readable_and_one_past_it_is_not(self):
        fixture = self.fixture("一行\n二行\n三行")  # 3 lines, no trailing newline
        last = fixture.read(start_line=3)
        self.assertEqual(last["startLine"], 3)
        self.assertEqual(last["excerpt"], "三行")
        self.assertEqual(last["fileLines"], 3)
        error = self.refusal(reader.READ_RANGE_INVALID, fixture.read, start_line=4)
        facts = self.facts(error)
        self.assertEqual(facts["fileLines"], 3)
        self.assertEqual(facts["requestedStartLine"], 4)
        # A file that ends with a newline has one extra readable position: the (empty) end of the file. That
        # is a successful read with a warning, not a refusal, and it is why the advice says "at most fileLines"
        # rather than "exactly the last line".
        trailing = self.fixture("一行\n二行\n三行\n")
        end = trailing.read(start_line=4)
        self.assertEqual(end["excerpt"], "")
        self.assertTrue(any(w.startswith("READ_AT_END") for w in end["warnings"]))

    def test_a_bad_start_line_is_refused_without_repeating_the_value(self):
        fixture = self.fixture("一行\n二行\n")
        for value in (0, -3, "480", 1.5, True):
            error = self.refusal(reader.READ_REQUEST_INVALID, fixture.read, start_line=value)
            facts = self.facts(error)
            self.assertEqual(facts["reason"], reader.READ_REASON_START_LINE_INVALID, repr(value))
            self.assertIsNone(facts["requestedStartLine"],
                              "a value that was never a valid line number must not be echoed: %r" % (value,))
            self.assertEqual(facts["fileLines"], 2)
            self.assertEqual(facts["suggestedAction"], reader.READ_ACTION_CORRECT_START_LINE)
        # 1 is a valid start line, and the parameter is optional, so neither may be refused.
        self.assertEqual(fixture.read(start_line=1)["startLine"], 1)
        self.assertEqual(fixture.read()["startLine"], 1, "an omitted startLine means the beginning of the file")

    def test_an_empty_file_is_readable_and_reports_zero_lines(self):
        fixture = self.fixture("")
        window = fixture.read()
        self.assertEqual(window["excerpt"], "")
        self.assertEqual(window["fileLines"], 0)
        self.assertTrue(any(w.startswith("READ_EMPTY_FILE") for w in window["warnings"]))
        error = self.refusal(reader.READ_RANGE_INVALID, fixture.read, start_line=2)
        facts = self.facts(error)
        self.assertEqual(facts["fileLines"], 0)
        self.assertEqual(facts["requestedStartLine"], 2)

    def test_facts_never_describe_a_file_the_caller_was_not_allowed_to_open(self):
        # The whole point of the leak rule: `fileLines` is a fact about an authorised file. Everything that is
        # refused *before* the text is read must therefore carry no line count at all.
        fixture = self.fixture("一行\n二行\n")
        with self.assertRaises(reader.ReadError) as caught:
            fixture.read(handle=None)
        self.assertIsNone(caught.exception.facts["fileLines"],
                          "an identifier-less refusal must not measure any file")
        self.assertEqual(caught.exception.facts["reason"], reader.READ_REASON_IDENTIFIER_MISSING)

        elsewhere = tempfile.mkdtemp(prefix="reader-other-")
        self.addCleanup(shutil.rmtree, elsewhere, True)
        foreign = self.refusal(reader.READ_HANDLE_FOREIGN, fixture.read,
                               handle=fixture.handle(workspace=elsewhere))
        self.assertIsNone(foreign.facts, "a foreign handle is not a request-shape refusal at all")

        # A mismatched sourceVersion is refused before the file is read as well.
        mismatch = self.refusal(reader.READ_SOURCE_VERSION_MISMATCH, fixture.read, source_version="0" * 64)
        self.assertIsNone(mismatch.facts)

    def test_a_file_that_is_not_library_content_is_refused_without_facts(self):
        # A recorded path that resolves to nothing. Refusing it with "your start line was too large" would be
        # a statement about a file the reader never opened.
        fixture = self.fixture("一行\n二行\n")
        error = self.refusal(reader.READ_FILE_UNAVAILABLE, fixture.read,
                             handle=fixture.service_handle("a.txt", "missing.txt"))
        self.assertIsNone(error.facts)

    def test_a_changed_version_is_refused_before_the_line_count_is_known(self):
        fixture = self.fixture("一行\n二行\n")
        handle = fixture.handle()
        fixture.write("a.txt", "一行\n二行\n三行\n四行\n")  # the file changed under the handle
        with self.assertRaises(reader.ReadError) as caught:
            reader.read(SECRET, str(fixture.root), LIBRARY, fixture.root, handle=handle, start_line=99)
        # The version check runs before the range check, and the version failure is not a range failure.
        self.assertEqual(caught.exception.code, reader.SOURCE_VERSION_CHANGED)
        self.assertIsNone(caught.exception.facts)

    def test_a_conflicting_identifier_pair_is_refused_as_a_request_problem(self):
        # A long fixture, so the first read really is cut and returns a cursor to disagree with.
        fixture = self.fixture()
        other = fixture.write("b.txt", "另一个文件\n")
        cursor = fixture.read(handle=fixture.handle("a.txt"), max_chars=250)["nextCursor"]
        self.assertIsNotNone(cursor, "the fixture must need more than one read")
        error = self.refusal(reader.READ_REQUEST_INVALID, fixture.read,
                             handle=reader.issue_handle(SECRET, str(fixture.root), LIBRARY, "b.txt",
                                                        evidence.digest(other.read_bytes())),
                             cursor=cursor)
        self.assertEqual(error.facts["reason"], reader.READ_REASON_IDENTIFIER_CONFLICT)
        self.assertIsNone(error.facts["fileLines"], "no file was read, so no line count is reported")

    def test_only_the_two_recoverable_codes_carry_facts(self):
        # A budget refusal is not repairable by editing an argument, so it must not carry a suggestion.
        fixture = self.fixture("一行\n二行\n")
        sessions = reader.Sessions(1, 400_000)
        state, _ = sessions.reserve("s", 4000)
        with self.assertRaises(reader.ReadError) as caught:
            sessions.reserve("s", 4000)
        self.assertEqual(caught.exception.code, reader.READ_LIMIT_EXCEEDED)
        self.assertIsNone(caught.exception.facts)

    def test_facts_cannot_be_attached_to_a_non_recoverable_code(self):
        # Defence in depth: even a caller that builds the exception by hand cannot put a line count on a
        # refusal whose code says the request was not the problem.
        error = reader.ReadError(reader.READ_HANDLE_INVALID, 404, "nope", facts=reader.read_facts("START_OUTSIDE_FILE"))
        self.assertIsNone(error.facts)


class IdentifierTests(ReadTestCase):
    def test_a_handle_reads_the_file_it_was_minted_for(self):
        fixture = self.fixture("第一行\n第二行\n")
        window = fixture.read(max_chars=400)
        self.assertEqual(window["excerpt"], "第一行\n第二行")
        self.assertEqual(window["sourceVersion"], evidence.digest((fixture.root / "a.txt").read_bytes()))
        self.assertEqual(window["relativePath"], "a.txt")
        self.assertEqual(window["libraryId"], LIBRARY)
        self.assertFalse(window["hasMore"])
        self.assertIsNone(window["nextCursor"])
        self.assertEqual(window["fileHandle"], fixture.handle(), "a read returns a usable identifier again")

    def test_a_handle_is_opaque_and_carries_no_plaintext_path(self):
        fixture = self.fixture()
        handle = fixture.handle()
        self.assertTrue(handle.startswith(reader.HANDLE_PREFIX + "."))
        self.assertNotIn("a.txt", handle)
        self.assertNotIn(str(fixture.root), handle)
        self.assertRegex(handle, r"^krh1\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+$")

    def test_a_tampered_handle_is_refused_without_naming_anything(self):
        fixture = self.fixture()
        handle = fixture.handle()
        prefix, body, signature = handle.split(".")
        flipped = signature[:-1] + ("A" if signature[-1] != "A" else "B")
        self.refusal(reader.READ_HANDLE_INVALID, fixture.read, handle="%s.%s.%s" % (prefix, body, flipped))
        self.refusal(reader.READ_HANDLE_INVALID, fixture.read, handle=handle[:-1])
        refusal = self.refusal(reader.READ_HANDLE_INVALID, fixture.read, handle="nonsense")
        self.assertNotIn(str(fixture.root), str(refusal))
        self.assertIn("knowledge_search", refusal.detail)

    def test_a_handle_signed_with_another_token_is_refused(self):
        fixture = self.fixture()
        foreign = reader.issue_handle(reader.derive_secret("y" * 24), str(fixture.root), LIBRARY, "a.txt",
                                      evidence.digest((fixture.root / "a.txt").read_bytes()))
        self.refusal(reader.READ_HANDLE_INVALID, fixture.read, handle=foreign)

    def test_a_cursor_cannot_be_presented_as_a_handle(self):
        fixture = self.fixture()
        cursor = fixture.read(max_chars=250)["nextCursor"]
        self.assertIsNotNone(cursor, "the fixture must need more than one read")
        self.refusal(reader.READ_HANDLE_INVALID, fixture.read, handle=cursor)

    def test_a_handle_from_another_workspace_or_library_is_refused(self):
        fixture = self.fixture()
        elsewheres = tempfile.mkdtemp(prefix="reader-other-")
        self.addCleanup(shutil.rmtree, elsewheres, True)
        self.refusal(reader.READ_HANDLE_FOREIGN, fixture.read, handle=fixture.handle(workspace=elsewheres))
        self.refusal(reader.READ_HANDLE_FOREIGN, fixture.read, handle=fixture.handle(library="other"))

    def test_a_cursor_and_a_handle_for_different_files_are_refused(self):
        fixture = self.fixture()
        other = fixture.write("b.txt", "另一个文件的内容\n" * 5)
        cursor = fixture.read(handle=fixture.handle("a.txt"), max_chars=250)["nextCursor"]
        self.refusal(reader.READ_REQUEST_INVALID, fixture.read,
                     handle=reader.issue_handle(SECRET, str(fixture.root), LIBRARY, "b.txt",
                                                evidence.digest(other.read_bytes())), cursor=cursor)

    def test_a_read_without_an_identifier_is_refused(self):
        fixture = self.fixture()
        self.refusal(reader.READ_REQUEST_INVALID, fixture.read, handle=None)

    def test_a_mismatched_source_version_is_refused(self):
        fixture = self.fixture()
        self.refusal(reader.READ_SOURCE_VERSION_MISMATCH, fixture.read, source_version="0" * 64)
        window = fixture.read(source_version=evidence.digest((fixture.root / "a.txt").read_bytes()))
        self.assertEqual(window["sourceVersion"], evidence.digest((fixture.root / "a.txt").read_bytes()))

    def test_a_cursor_with_a_matching_handle_is_accepted(self):
        fixture = self.fixture()
        first = fixture.read(max_chars=250)
        both = fixture.read(handle=fixture.handle(), cursor=first["nextCursor"], max_chars=250)
        self.assertEqual(both["startChar"], first["nextStartChar"])
        self.assertFalse(any(w.startswith("READ_CURSOR") for w in both["warnings"]))
        ignored = fixture.read(handle=fixture.handle(), cursor=first["nextCursor"], start_line=1, max_chars=250)
        self.assertTrue(any(w.startswith("READ_CURSOR_PRECEDENCE") for w in ignored["warnings"]),
                        "a cursor wins over startLine, and the read says so instead of silently ignoring it")

    def test_rotating_the_token_invalidates_outstanding_handles(self):
        fixture = self.fixture()
        rotated = reader.derive_secret("z" * 24)
        self.refusal(reader.READ_HANDLE_INVALID, reader.read, rotated, str(fixture.root), LIBRARY,
                     fixture.root, handle=fixture.handle())


class PathBoundaryTests(ReadTestCase):
    def test_a_recorded_absolute_path_is_refused(self):
        fixture = self.fixture()
        self.refusal(reader.READ_FILE_UNAVAILABLE, fixture.read,
                     handle=fixture.service_handle("a.txt", str(fixture.root / "a.txt")))

    def test_traversal_and_escapes_are_refused(self):
        fixture = self.fixture()
        for recorded in ("../a.txt", "sub/../../a.txt", "..\\a.txt", "C:/windows/win.ini", "/etc/passwd",
                         ".", "sub/..", "/absolute"):
            self.refusal(reader.READ_FILE_UNAVAILABLE, fixture.read,
                         handle=fixture.service_handle("a.txt", recorded))

    def test_a_file_replaced_by_a_link_is_refused(self):
        fixture = self.fixture()
        outside = fixture.root.parent / (fixture.root.name + "-outside.txt")
        outside.write_text("outside content\n", encoding="utf-8")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        target = fixture.root / "a.txt"
        target.unlink()
        try:
            target.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are not available to this user")
        self.refusal(reader.READ_FILE_UNAVAILABLE, fixture.read)

    def test_a_linked_directory_is_refused(self):
        fixture = self.fixture()
        outside = Path(tempfile.mkdtemp(prefix="reader-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        secret = outside / "secret.txt"
        secret.write_text("outside content\n", encoding="utf-8")
        if not _make_directory_link(fixture.root / "linked", outside):
            self.skipTest("directory junctions are not available to this user")
        self.refusal(reader.READ_FILE_UNAVAILABLE, fixture.read,
                     handle=fixture.service_handle("a.txt", "linked/secret.txt",
                                                   version=evidence.digest(secret.read_bytes())))

    def test_unsupported_types_and_oversized_files_are_refused(self):
        fixture = self.fixture()
        (fixture.root / "binary.png").write_bytes(b"\x89PNG\r\n")
        self.refusal(reader.READ_FILE_UNAVAILABLE, fixture.read,
                     handle=fixture.service_handle("a.txt", "binary.png"))
        (fixture.root / "big.txt").write_bytes(b"a" * (reader.MAX_FILE_BYTES + 1))
        self.refusal(reader.READ_FILE_UNAVAILABLE, fixture.read,
                     handle=fixture.service_handle("a.txt", "big.txt"))

    def test_a_deleted_file_is_refused(self):
        fixture = self.fixture()
        handle = fixture.handle()
        (fixture.root / "a.txt").unlink()
        self.refusal(reader.READ_FILE_UNAVAILABLE, fixture.read, handle=handle)

    def test_a_non_utf8_file_is_reported_as_not_text(self):
        fixture = self.fixture()
        path = fixture.root / "latin1.txt"
        path.write_bytes("caf\xe9 latin-1\n".encode("latin-1"))
        self.refusal(reader.READ_FILE_NOT_TEXT, fixture.read,
                     handle=fixture.service_handle("a.txt", "latin1.txt", version=evidence.digest(path.read_bytes())))

    def test_a_changed_file_demands_a_new_search(self):
        fixture = self.fixture()
        handle = fixture.handle()
        fixture.write("a.txt", "已替换的内容\n")
        error = self.refusal(reader.SOURCE_VERSION_CHANGED, fixture.read, handle=handle)
        self.assertEqual(error.status, 409)
        self.assertIn("knowledge_search", error.detail)


class WindowTests(ReadTestCase):
    def test_windows_are_contiguous_and_reproduce_the_file_exactly(self):
        fixture = self.fixture()
        rebuilt, cursor, windows = "", None, 0
        while True:
            window = fixture.read(cursor=cursor, max_chars=120)
            self.assertEqual(window["startChar"], len(rebuilt), "each window continues exactly where the last ended")
            rebuilt += window["excerpt"]
            windows += 1
            if window["nextStartChar"] is None:
                break
            gap = MIXED[window["endChar"]:window["nextStartChar"]]
            self.assertIn(gap, ("", "\n"), "only the line separator may be skipped")
            rebuilt += gap
            cursor = window["nextCursor"]
        self.assertGreater(windows, 3, "the fixture must actually need several reads")
        # The only characters never returned are line separators; nothing else is dropped or repeated.
        self.assertTrue(MIXED.startswith(rebuilt))
        self.assertLessEqual(set(MIXED[len(rebuilt):]), {"\n"})
        self.assertLessEqual(len(MIXED) - len(rebuilt), 1, "at most the file's final newline")

    def test_the_end_of_the_file_ends_cleanly(self):
        fixture = self.fixture("只有一行\n")
        window = fixture.read()
        self.assertEqual(window["excerpt"], "只有一行")
        self.assertEqual(window["endChar"], len("只有一行"))
        self.assertTrue(window["endsAtLineBoundary"])
        self.assertFalse(window["hasMore"])
        self.assertIsNone(window["nextCursor"])

    def test_a_long_line_is_cut_and_flagged_then_continued_inside(self):
        source = "短行\n" + "长" * 900 + "\n尾行\n"
        fixture = self.fixture(source)
        first = fixture.read(max_chars=reader.MIN_READ_CHARS)
        self.assertTrue(first["midLineStop"])
        self.assertTrue(any(w.startswith("READ_LONG_LINE") for w in first["warnings"]))
        self.assertEqual(first["nextStartChar"], first["endChar"], "a mid-line cut resumes at the same offset")
        second = fixture.read(cursor=first["nextCursor"], max_chars=reader.MIN_READ_CHARS)
        self.assertEqual(second["startChar"], first["endChar"])
        self.assertTrue(second["midLineStart"])
        self.assertEqual(first["excerpt"] + second["excerpt"], source[:400])

    def test_overlap_is_opt_in_and_marked(self):
        source = "\n".join("第%d行 内容" % index for index in range(1, 61)) + "\n"
        fixture = self.fixture(source)
        first = fixture.read(max_chars=60)
        second = fixture.read(cursor=first["nextCursor"], max_chars=60)
        self.assertEqual(second["startChar"], first["nextStartChar"])
        self.assertFalse(second["overlapsPrevious"])
        overlapped = fixture.read(cursor=first["nextCursor"], max_chars=60, overlap_lines=1)
        self.assertTrue(overlapped["overlapsPrevious"])
        self.assertTrue(any(w.startswith("READ_OVERLAP") for w in overlapped["warnings"]))
        self.assertEqual(overlapped["startChar"], reader._line_start_before(source, second["startChar"]))
        # The repeated context is the previous window's last line, verbatim.
        self.assertEqual(overlapped["excerpt"].split("\n")[0], first["excerpt"].split("\n")[-1])
        self.assertLess(overlapped["startChar"], second["startChar"])

    def test_the_line_cap_is_enforced_and_reported(self):
        fixture = self.fixture("\n".join("第%d行" % index for index in range(50)) + "\n")
        window = fixture.read(max_chars=reader.MAX_READ_CHARS, max_lines=3)
        self.assertTrue(window["lineLimitReached"])
        self.assertEqual(window["excerpt"].count("\n") + 1, 3)
        self.assertTrue(any(w.startswith("READ_LINE_LIMIT") for w in window["warnings"]))

    def test_a_start_past_the_end_is_refused_and_the_last_position_is_not(self):
        fixture = self.fixture("一行\n二行\n")
        self.refusal(reader.READ_RANGE_INVALID, fixture.read, start_line=4)
        empty = fixture.read(start_line=3)
        self.assertEqual(empty["excerpt"], "")
        self.assertTrue(any(w.startswith("READ_AT_END") for w in empty["warnings"]))

    def test_an_empty_file_returns_nothing_and_says_so(self):
        fixture = self.fixture("")
        window = fixture.read()
        self.assertEqual(window["excerpt"], "")
        self.assertEqual(window["fileChars"], 0)
        self.assertFalse(window["hasMore"])
        self.assertTrue(any(w.startswith("READ_EMPTY_FILE") for w in window["warnings"]))

    def test_offsets_are_code_points_into_bom_stripped_crlf_folded_text(self):
        fixture = self.fixture("第一行\n第二行 emoji 😀 后面\n第三行\n", newline="\r\n")
        path = fixture.root / "a.txt"
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
        window = fixture.read()
        self.assertEqual(window["excerpt"], "第一行\n第二行 emoji 😀 后面\n第三行")
        normalised = evidence.normalised_text(path.read_bytes())
        self.assertEqual(evidence.excerpt_from_offsets(normalised, window), window["excerpt"])
        self.assertEqual(len([character for character in window["excerpt"]]), window["endChar"] - window["startChar"])
        self.assertGreater(len(window["excerpt"].encode("utf-16-le")) // 2, window["endChar"] - window["startChar"],
                           "the fixture must contain characters whose UTF-16 width exceeds one unit")

    def test_a_request_larger_than_the_response_ceiling_is_clamped(self):
        fixture = self.fixture("x" * 1000 + "\n")
        window = fixture.read(max_chars=10 ** 9)
        self.assertLessEqual(window["endChar"] - window["startChar"], reader.MAX_READ_CHARS)

    def test_the_boundary_check_agrees_with_the_citation_contract(self):
        for sample in ("以句号结束。", "end of statement.", "断了 frac-", "没有标点", "", "带括号 (yes).",
                       "问号？", "quoted \"stop.\"", "数字 3.0"):
            self.assertEqual(reader._closes_a_sentence(sample), pipeline._closes_a_sentence(sample), sample)


class EndOfFileWarningTests(ReadTestCase):
    """The three "how did the window end" cases, which one sentence used to cover.

    The recorded session met the third one: a window that reached line 423 of 423 with `hasMore: false` and
    `nextCursor: null` still carried "read on with nextCursor before concluding anything from it" - advice
    pointing at a cursor the same response reports as absent. The mechanical boundary flag is unchanged here;
    only the reason and the advice differ.
    """

    def warning(self, window, prefix):
        return next((w for w in window["warnings"] if w.startswith(prefix)), None)

    def test_more_content_after_the_window_still_says_read_on(self):
        fixture = self.fixture("第一行没有句号\n" * 40)
        window = fixture.read(max_chars=120, max_lines=3)
        self.assertTrue(window["hasMore"], "the fixture must leave content after the window")
        self.assertFalse(window["excerptComplete"])
        notice = self.warning(window, "EXCERPT_TRUNCATED")
        self.assertIsNotNone(notice)
        self.assertIn("continues after it", notice)
        self.assertIn("read on", notice)
        self.assertIsNone(self.warning(window, "EXCERPT_INCOMPLETE_AT_EOF"))

    def test_the_end_of_the_file_does_not_advise_reading_on(self):
        # Ends mid-sentence, and it is the end: no next window exists, and the warning says exactly that.
        fixture = self.fixture("第一行有句号。\n第二行没有句号")
        window = fixture.read()
        self.assertFalse(window["hasMore"])
        self.assertIsNone(window["nextStartChar"])
        self.assertFalse(window["excerptComplete"])
        notice = self.warning(window, "EXCERPT_INCOMPLETE_AT_EOF")
        self.assertIsNotNone(notice)
        self.assertIn("end of the file", notice)
        self.assertIn("no next window", notice)
        self.assertIn("mechanical", notice, "the boundary check must not be presented as a semantic judgement")
        self.assertNotIn("read on", notice)
        self.assertIsNone(self.warning(window, "EXCERPT_TRUNCATED"),
                          "a window at the end of the file is not a truncated window")

    def test_the_end_of_the_file_with_a_closed_sentence_carries_no_such_notice(self):
        # "Closed" means what the mechanical check recognizes, which is an ASCII terminator - see the fixture
        # note below for what that does and does not say about Chinese text.
        fixture = self.fixture("first line ends here.\nsecond line does too.\n")
        window = fixture.read()
        self.assertFalse(window["hasMore"])
        self.assertTrue(window["excerptComplete"])
        self.assertIsNone(self.warning(window, "EXCERPT_TRUNCATED"))
        self.assertIsNone(self.warning(window, "EXCERPT_INCOMPLETE_AT_EOF"))

    def test_the_boundary_check_is_ascii_and_the_warning_says_so(self):
        # A fact about this build, pinned so nobody "fixes" it silently: `_closes_a_sentence` looks for an ASCII
        # `.`/`!`/`?`, so a Chinese sentence ending in `。` is reported as not-complete. That is the mechanical
        # rule the protocol tells the model not to read as semantic completeness - and the EOF warning names the
        # rule instead of implying that the text is unfinished. Changing the rule would change the citation
        # contract (pipeline shares it), which is why the warning describes it rather than the check bending.
        fixture = self.fixture("第一行有句号。\n第二行也有句号。\n")
        window = fixture.read()
        self.assertFalse(window["excerptComplete"], "the ASCII-only check does not recognize 。")
        notice = self.warning(window, "EXCERPT_INCOMPLETE_AT_EOF")
        self.assertIn("ASCII", notice)
        self.assertIn("full-width CJK stop", notice)

    def test_the_warning_does_not_name_a_field_the_result_renames(self):
        # `nextCursor` is the service's own spelling and the plugin renames it to `nextRef` in the model-facing
        # result. A warning naming the service's field while that field has been renamed reads as advice about
        # something that is not there, so the wording stays field-name-agnostic - asserted so it cannot drift
        # back into naming one of them.
        fixture = self.fixture("第一行没有句号\n" * 40)
        window = fixture.read(max_chars=120, max_lines=3)
        text = " ".join(window["warnings"])
        self.assertNotIn("nextCursor", text)
        self.assertNotIn("nextRef", text)

    def test_offsets_still_rebuild_the_excerpt_at_the_end_of_a_unicode_file(self):
        # The EOF split must not touch the offset contract: the same reconstruction check as the general
        # fixture, on a file that ends without a newline and mixes CJK, an emoji and CRLF.
        fixture = self.fixture("第一行有句号。\n第二行 emoji 😀 没有句号", newline="\r\n")
        window = fixture.read()
        self.assertFalse(window["hasMore"])
        normalised = evidence.normalised_text((fixture.root / "a.txt").read_bytes())
        self.assertEqual(evidence.excerpt_from_offsets(normalised, window), window["excerpt"])
        self.assertEqual(window["endChar"], window["fileChars"], "the window reaches the end of the text")
        self.assertIsNotNone(self.warning(window, "EXCERPT_INCOMPLETE_AT_EOF"))


class ConsistencyTests(ReadTestCase):
    def test_one_read_supplies_the_version_and_the_text(self):
        fixture = self.fixture("原始内容\n第二行\n")
        handle = fixture.handle()
        original = Path.read_bytes
        calls = []

        def flaky(self):
            calls.append(self)
            return original(self) if len(calls) == 1 else b"changed meanwhile\n"

        with patch.object(Path, "read_bytes", flaky):
            window = fixture.read(handle=handle)
        self.assertEqual(len(calls), 1, "a read must not look at the file twice")
        self.assertEqual(window["excerpt"], "原始内容\n第二行")
        self.assertEqual(window["sourceVersion"], evidence.digest("原始内容\n第二行\n".encode("utf-8")))

    def test_an_unreadable_path_is_refused(self):
        fixture = self.fixture()
        handle = fixture.handle()
        with patch.object(Path, "read_bytes", side_effect=OSError("denied")):
            self.refusal(reader.READ_FILE_UNAVAILABLE, fixture.read, handle=handle)


class SessionBudgetTests(unittest.TestCase):
    def test_the_read_count_is_capped(self):
        sessions = reader.Sessions(max_reads=2)
        state = None
        for _ in range(2):
            state, budget = sessions.reserve("key", 500)
            self.assertEqual(budget, 500)
        error = self.refusal(reader.READ_LIMIT_EXCEEDED, sessions.reserve, "key", 500)
        self.assertEqual(error.status, 429)
        self.assertEqual(sessions.report(state)["readsUsed"], 2)

    def test_the_character_budget_shrinks_a_window_and_then_exhausts(self):
        sessions = reader.Sessions(max_reads=10, max_chars=1000, min_chars=200)
        state, budget = sessions.reserve("key", 900)
        self.assertEqual(budget, 900)
        sessions.charge(state, 500)
        state, budget = sessions.reserve("key", 900)
        self.assertEqual(budget, 500, "clamped to what the session has left, never below the minimum")
        sessions.charge(state, 500)
        self.refusal(reader.READ_LIMIT_EXCEEDED, sessions.reserve, "key", 900)

    def test_sessions_are_independent_and_the_table_is_bounded(self):
        sessions = reader.Sessions(max_reads=1, max_entries=2)
        sessions.reserve("a", 500)
        sessions.reserve("b", 500)
        sessions.reserve("c", 500)  # evicts the oldest entry, "a"
        self.refusal(reader.READ_LIMIT_EXCEEDED, sessions.reserve, "b", 500)
        sessions.reserve("a", 500)  # "a" was evicted, so it starts over instead of being refused
        self.assertEqual(len(sessions._state), 2)

    def refusal(self, code, callable_, *args, **kwargs):
        with self.assertRaises(reader.ReadError) as caught:
            callable_(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception)
        return caught.exception


def _make_directory_link(link, target):
    """A directory junction where symbolic links need privileges this user may not have."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    try:
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                capture_output=True, text=True, timeout=30)
        return result.returncode == 0 and link.exists()
    except (OSError, subprocess.SubprocessError):
        return False


if __name__ == "__main__":
    unittest.main()
