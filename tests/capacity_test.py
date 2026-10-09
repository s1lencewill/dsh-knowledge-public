"""Real byte/file boundaries; no mocked limits or filesystem sizes."""
import sys
import tempfile
import unittest
import asyncio
import unittest.mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import capacity
import catalog
import evidence
import reader
from evidence import snapshot, SnapshotCapacityError
from runtime import safe_error


class CapacityTests(unittest.TestCase):
    def test_parent_rejects_before_any_engine_process_starts(self):
        import pipeline

        async def runner(child_request):
            raise AssertionError("no engine process may start for an over-sized library")

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            (root / "one.md").write_text("text", encoding="utf-8")
            work = base / "job"
            request = {"mode": "FAST", "query": "q", "root": str(root), "libraryId": "fixture",
                       "cacheRoot": str(base), "workDir": str(work)}
            with unittest.mock.patch.object(pipeline, "build_snapshot",
                                            side_effect=SnapshotCapacityError("files", 1000)):
                result = asyncio.run(pipeline.run_local(request, runner))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["evidence"], [])
            self.assertIn("LIBRARY_CAPACITY_EXCEEDED", result["warnings"][0])
            self.assertFalse(work.exists() and any(work.iterdir()), "no partial snapshot may be searched")

    def test_real_file_count_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = base / "root"; root.mkdir()
            for i in range(1000):
                (root / f"{i}.txt").write_bytes(b"a")
            self.assertEqual(len(snapshot(root, base / "ok")[0]), 1000)
            (root / "1000.txt").write_bytes(b"a")
            with self.assertRaises(SnapshotCapacityError) as raised:
                snapshot(root, base / "rejected")
            self.assertEqual(raised.exception.dimension, "files")
            self.assertIn("limit=1000", safe_error(raised.exception))

    def test_real_byte_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = base / "root"; root.mkdir()
            block = b"a" * 2_000_000
            for i in range(50):
                (root / f"{i}.txt").write_bytes(block)
            manifest, _ = snapshot(root, base / "ok")
            self.assertEqual(len(manifest), 50)
            self.assertEqual(sum(Path(p).stat().st_size for p in manifest), 100_000_000)
            (root / "extra.txt").write_bytes(b"a")
            with self.assertRaises(SnapshotCapacityError) as raised:
                snapshot(root, base / "rejected-total")
            self.assertEqual(raised.exception.dimension, "totalBytes")
            (root / "extra.txt").write_bytes(block + b"a")
            with self.assertRaises(SnapshotCapacityError) as raised:
                snapshot(root, base / "rejected-file")
            self.assertEqual(raised.exception.dimension, "fileBytes")


class PerLibraryCeilingTests(unittest.TestCase):
    """`maxFileBytes` is optional, validated, and the same number for every path that judges a file."""

    def test_the_default_is_the_limit_every_library_already_had(self):
        self.assertEqual(capacity.resolve({}), 2_000_000)
        self.assertEqual(capacity.resolve({"id": "x"}), 2_000_000)
        self.assertEqual(capacity.DEFAULT_MAX_FILE_BYTES, reader.MAX_FILE_BYTES)
        self.assertEqual(capacity.resolve({"id": "x", "maxFileBytes": 4_000_000}), 4_000_000)

    def test_a_declaration_that_cannot_be_honoured_is_refused_not_defaulted(self):
        for value in (0, -1, 1.5, 4_000_000.0, True, False, "4000000", "", [], {}, float("inf"),
                      capacity.CEILING_MAX_FILE_BYTES + 1):
            with self.assertRaises(capacity.CapacityConfigError) as raised:
                capacity.resolve({"id": "wiki", "maxFileBytes": value})
            self.assertEqual(raised.exception.code, capacity.CONFIG_INVALID)
            self.assertIn("wiki", str(raised.exception))
        # The ceiling itself is usable; one byte more is not.
        self.assertEqual(capacity.resolve({"id": "wiki", "maxFileBytes": capacity.CEILING_MAX_FILE_BYTES}),
                         capacity.CEILING_MAX_FILE_BYTES)

    def test_the_boundary_is_the_limit_itself_and_not_one_byte_below_it(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            exact = root / "exact.md"
            exact.write_bytes(b"a" * 3_000_000)
            # At the library's ceiling the file is in; the default ceiling refuses it.
            manifest, _ = snapshot(root, base / "out", 3_000_000)
            self.assertEqual(len(manifest), 1)
            with self.assertRaises(SnapshotCapacityError) as raised:
                snapshot(root, base / "out2", capacity.DEFAULT_MAX_FILE_BYTES)
            self.assertEqual((raised.exception.dimension, raised.exception.limit), ("fileBytes", 2_000_000))
            (root / "over.md").write_bytes(b"a" * 3_000_001)
            with self.assertRaises(SnapshotCapacityError) as raised:
                snapshot(root, base / "out3", 3_000_000)
            self.assertEqual(raised.exception.limit, 3_000_000)

    def test_a_nonsense_limit_passed_to_the_snapshot_raises_instead_of_being_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            (root / "a.md").write_text("x", encoding="utf-8")
            for value in (0, -5, 1.5, "big", None, True):
                with self.assertRaises(ValueError):
                    snapshot(root, base / ("out-%s" % value), value)

    def test_every_path_that_judges_a_file_agrees_on_the_same_bytes(self):
        """The one property the whole change exists for: no path may disagree with another.

        A 2.5 MB note is *in* the library at a 4 MB ceiling and *out* of it at the default. Snapshot,
        read and the catalogue's readability check must all say the same thing, for the same file, in
        both configurations - nothing else is acceptable, because a disagreement is either a document
        offered and then unreadable, or a searchable library that the reader claims is empty.
        """
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "library"
            root.mkdir()
            path = root / "large.md"
            path.write_bytes(b"Z" * 2_500_000)
            version = evidence.digest(path.read_bytes())
            issuer = reader.Issuer(reader.derive_secret("synthetic-token-for-capacity-tests"), str(base), "lib")

            wide = {"id": "lib", "root": str(root), "syntheticFixture": True, "maxFileBytes": 4_000_000}
            narrow = {"id": "lib", "root": str(root), "syntheticFixture": True}
            record = {"documentId": "d", "textFile": "large.md", "fileName": "large.md",
                      "titleRaw": "Large", "titleNormalized": "large", "textSHA256": version,
                      "verification": "undetermined", "quality": {}, "conflicts": []}

            # Snapshot: in at 4 MB, refused at the default.
            manifest, _ = snapshot(root, base / "out", capacity.resolve(wide))
            self.assertEqual(len(manifest), 1)
            with self.assertRaises(SnapshotCapacityError):
                snapshot(root, base / "out2", capacity.resolve(narrow))

            # Read: the same verdict, both ways.
            self.assertEqual(reader.resolve(root, "large.md", capacity.resolve(wide)).name, "large.md")
            with self.assertRaises(reader.ReadError) as raised:
                reader.resolve(root, "large.md", capacity.resolve(narrow))
            self.assertEqual(raised.exception.code, reader.READ_FILE_UNAVAILABLE)

            # Catalogue readability, and therefore handle issuance: the same verdict again.
            window, refusal = catalog.verify_text_file(wide, record, issuer)
            self.assertIsNone(refusal)
            self.assertTrue(window["fileHandle"].startswith(reader.HANDLE_PREFIX))
            window, refusal = catalog.verify_text_file(narrow, record, issuer)
            self.assertIsNone(window, "a narrowed library must not mint a handle for the file")
            self.assertEqual(refusal["code"], reader.READ_FILE_UNAVAILABLE)

    def test_an_oversized_file_is_never_silently_dropped_from_a_snapshot(self):
        """Reject-not-partial: the whole library is refused, so no result can describe a partial one."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            for index in range(5):
                (root / ("small-%d.md" % index)).write_text("fine\n", encoding="utf-8")
            (root / "huge.md").write_bytes(b"a" * 2_000_001)
            with self.assertRaises(SnapshotCapacityError):
                snapshot(root, base / "out", capacity.DEFAULT_MAX_FILE_BYTES)
            self.assertFalse((base / "out" / "small-0.md").exists(),
                             "a refused snapshot must leave no searchable fragment behind")
