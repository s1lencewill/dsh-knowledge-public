"""The document catalogue: matching modes, catalogue hygiene, and verification before a handle.

Pure module tests with synthetic catalogues and synthetic libraries. No private material, no service,
no engine.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import catalog
import evidence
import reader


def record(document_id, text_file, title, **extra):
    payload = {"documentId": document_id, "textFile": text_file, "fileName": text_file,
               "titleRaw": title, "titleNormalized": catalog.squash(title),
               "textSHA256": extra.pop("textSHA256", None),
               "authors": None, "year": None, "doi": None, "verification": "partial",
               "quality": {}, "conflicts": []}
    payload.update(extra)
    return payload


class CatalogueCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="catalog-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.library = self.root / "library"
        self.library.mkdir()

    def write(self, name, text="synthetic literature text\nsecond line\n"):
        path = self.library / name
        path.write_text(text, encoding="utf-8")
        return path, evidence.digest(path.read_bytes())

    def write_catalog(self, documents, name="identity.json"):
        path = self.root / name
        path.write_text(json.dumps({"documents": documents}), encoding="utf-8")
        return path

    def rewrite(self, path, payload, **dump):
        """Write `payload` over `path` with a modification time that cannot be the previous one."""
        path.write_text(json.dumps(payload, **dump), encoding="utf-8")
        later = path.stat().st_mtime + 5
        os.utime(path, (later, later))
        return path

    def selected(self, **overrides):
        library = {"id": "fixture", "root": str(self.library), "syntheticFixture": True}
        library.update(overrides)
        return library

    def find(self, documents, **query):
        entries = {"d%d" % index: item for index, item in enumerate(documents)}
        return catalog.find(entries, query)


class MatchingTests(CatalogueCase):
    def test_doi_matching_ignores_case_and_resolver_prefixes(self):
        documents = [record("a", "a.txt", "One", doi="10.1021/jacs.2c03099"),
                     record("b", "b.txt", "Two", doi="10.1038/s41467-022-28697-8")]
        for query in ({"doi": "10.1021/JACS.2c03099"}, {"doi": "https://doi.org/10.1021/jacs.2c03099"},
                      {"doi": "doi:10.1021/jacs.2c03099"}):
            ranked = self.find(documents, **query)
            self.assertEqual(len(ranked), 1, query)
            self.assertEqual(ranked[0]["record"]["documentId"], "a")
            self.assertIn("doi-exact", ranked[0]["matched"])

    def test_title_matching_is_space_insensitive_and_reports_fuzzy(self):
        documents = [record("a", "a.txt", "How the Acidity of Water Droplets Is Controlled By the Interface"),
                     record("b", "b.txt", "Charge transfer at air-water interfaces")]
        exact = self.find(documents, title="howtheacidityof water droplets is controlled by the interface")
        self.assertEqual(exact[0]["record"]["documentId"], "a")
        self.assertIn("title-exact", exact[0]["matched"])
        fuzzy = self.find(documents, title="air water interfaces charge transfer study")
        self.assertTrue(fuzzy)
        self.assertTrue({"title-partial", "title-weak"} & set(fuzzy[0]["matched"]), fuzzy[0]["matched"])

    def test_file_name_and_author_year_clues(self):
        documents = [record("a", "JACS2023,145,10631SO3.txt", "Sulfur trioxide hydrolysis",
                            authors="Yajuan Feng and Chao Wang", year="2023"),
                     record("b", "Langmuir2025,41,5,3675H2O.txt", "Water self ions",
                            authors="Pengchao Zhang and Xuefei Xu", year="2025")]
        by_name = self.find(documents, fileName="JACS2023,145,10631SO3.txt")
        self.assertEqual(by_name[0]["matched"], ["filename-exact"])
        by_author = self.find(documents, authors="Zhang", year="2025")
        self.assertEqual(by_author[0]["record"]["documentId"], "b")
        self.assertIn("authors-match", by_author[0]["matched"])
        self.assertIn("year-match", by_author[0]["matched"])
        mismatch = self.find(documents, authors="Zhang", year="1999")
        self.assertIn("year-mismatch", mismatch[0]["mismatched"])

    def test_several_matches_come_back_as_a_list_and_zero_hits_stay_empty(self):
        documents = [record("a", "a.txt", "Bulk phase dominates sulfur dioxide hydrolysis"),
                     record("b", "b.txt", "Bulk phase dominates sulfur dioxide hydrolysis")]
        ranked = self.find(documents, title="bulk phase dominates sulfur dioxide hydrolysis")
        self.assertEqual(len(ranked), 2, "a tie must be a list, not a silent first pick")
        self.assertEqual(self.find(documents, title="nothing whatever matches this"), [])
        self.assertEqual(self.find(documents, doi="10.9999/absent"), [])

    def test_the_limit_is_bounded(self):
        documents = [record("d%d" % index, "d%d.txt" % index, "Bulk phase hydrolysis") for index in range(15)]
        self.assertEqual(len(self.find(documents, title="bulk phase hydrolysis", limit=3)), 3)
        self.assertEqual(len(self.find(documents, title="bulk phase hydrolysis", limit=999)),
                         catalog.MAX_CANDIDATES)

    def test_an_empty_query_and_an_overlong_query_are_refused(self):
        with self.assertRaises(catalog.CatalogError) as caught:
            self.find([record("a", "a.txt", "One")])
        self.assertEqual(caught.exception.code, catalog.FIND_REQUEST_INVALID)
        with self.assertRaises(catalog.CatalogError):
            self.find([record("a", "a.txt", "One")], title="x" * (catalog.MAX_QUERY_CHARS + 1))


class EnumerationTests(CatalogueCase):
    """The local listing: deterministic order, complete coverage, and a version that moves only on content."""

    def entries(self, count=23):
        return {"k%02d" % index: record("d%02d" % index, "d%02d.txt" % index, "Synthetic study %02d" % index)
                for index in range(count)}

    def test_the_order_is_deterministic_and_independent_of_mapping_order(self):
        forward = self.entries()
        reverse = dict(reversed(list(forward.items())))
        self.assertEqual([item["documentId"] for item in catalog.ordered_entries(forward)],
                         [item["documentId"] for item in catalog.ordered_entries(reverse)],
                         "the listing order is a property of the library, not of a dict's insertion order")
        self.assertEqual([item["documentId"] for item in catalog.ordered_entries(forward)],
                         sorted(item["documentId"] for item in catalog.ordered_entries(forward)))

    def test_paging_covers_every_entry_exactly_once(self):
        payload = self.entries()
        seen, offset = [], 0
        while True:
            page = catalog.enumerate_catalog(payload, offset, 7)
            self.assertEqual(page["offset"], offset)
            self.assertEqual(page["total"], 23)
            seen.extend(item["documentId"] for item in page["entries"])
            if not page["hasMore"]:
                self.assertIsNone(page["nextOffset"])
                break
            self.assertEqual(page["nextOffset"], len(seen))
            offset = page["nextOffset"]
        self.assertEqual(len(seen), 23)
        self.assertEqual(len(set(seen)), 23)

    def test_the_page_size_defaults_and_is_capped(self):
        payload = self.entries(80)
        self.assertEqual(catalog.enumerate_catalog(payload)["returned"], catalog.DEFAULT_PAGE_SIZE)
        self.assertEqual(catalog.enumerate_catalog(payload, 0, 999)["returned"], catalog.MAX_PAGE_SIZE)

    def test_an_offset_past_the_end_is_an_empty_page_not_an_error(self):
        page = catalog.enumerate_catalog(self.entries(3), 10, 5)
        self.assertEqual(page["entries"], [])
        self.assertFalse(page["hasMore"])
        self.assertEqual(page["total"], 3)

    def test_the_shape_is_refused_rather_than_coerced(self):
        for offset in (-1, 1.5, "0", True):
            with self.assertRaises(catalog.CatalogError) as caught:
                catalog.enumerate_catalog(self.entries(2), offset, 5)
            self.assertEqual(caught.exception.code, catalog.CATALOGUE_REQUEST_INVALID)
        for limit in (0, -1, 2.5, "5"):
            with self.assertRaises(catalog.CatalogError) as caught:
                catalog.enumerate_catalog(self.entries(2), 0, limit)
            self.assertEqual(caught.exception.code, catalog.CATALOGUE_REQUEST_INVALID)

    def test_the_version_moves_when_an_entry_changes_and_not_when_the_file_is_rewritten_identically(self):
        documents = self.entries(3)
        path = self.write_catalog(documents)
        _, first = catalog.load_versioned(path)
        # A rewrite that changes the formatting but not the content must not move the version: a caller paging
        # through a listing would otherwise be interrupted by a no-op save. The mtime is advanced explicitly
        # after every write: otherwise the service's cache would answer from the previous parse and this test
        # would be asserting that a cache hit returns the same object, not that the version is stable.
        self.rewrite(path, {"documents": documents}, indent=2, sort_keys=True)
        _, same = catalog.load_versioned(path)
        self.assertEqual(same, first)
        # Adding, removing or re-pointing an entry must move it.
        documents["k99"] = record("d99", "d99.txt", "Another study")
        self.rewrite(path, {"documents": documents})
        _, added = catalog.load_versioned(path)
        self.assertNotEqual(added, first)
        del documents["k99"]
        documents["k00"]["textSHA256"] = "0" * 64
        self.rewrite(path, {"documents": documents})
        _, repointed = catalog.load_versioned(path)
        self.assertNotEqual(repointed, first)
        self.assertNotEqual(repointed, added)

    def test_a_replacement_inside_one_mtime_tick_is_not_served_from_the_cache(self):
        # Publishing a refresh replaces the catalogue; if the parse were cached on the modification time
        # alone, a replacement within the same tick would hand the caller the listing that no longer exists.
        path = self.write_catalog({"a": record("a", "a.txt", "One")})
        first, _ = catalog.load_versioned(path)
        replacement = self.root / "replacement.json"
        replacement.write_text(json.dumps({"documents": {"b": record("b", "b.txt", "Two")}}), encoding="utf-8")
        os.replace(replacement, path)
        second, _ = catalog.load_versioned(path)
        self.assertNotEqual(sorted(first), sorted(second))
        self.assertEqual(sorted(second), ["b"])

    def test_loading_returns_the_payload_and_the_version_that_describes_it(self):
        path = self.write_catalog(self.entries(2))
        payload, version = catalog.load_versioned(path)
        self.assertEqual(len(payload), 2)
        self.assertEqual(version, catalog.catalogue_version(payload))
        self.assertEqual(catalog.load(path), payload, "the single-value form is the same payload")


class CatalogueHygieneTests(CatalogueCase):
    def test_a_missing_malformed_or_oversized_catalogue_is_a_refusal(self):
        with self.assertRaises(catalog.CatalogError) as caught:
            catalog.load(self.root / "absent.json")
        self.assertEqual(caught.exception.code, catalog.CATALOG_UNAVAILABLE)
        bad = self.root / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with self.assertRaises(catalog.CatalogError) as caught:
            catalog.load(bad)
        self.assertEqual(caught.exception.code, catalog.CATALOG_UNREADABLE)
        empty = self.root / "empty.json"
        empty.write_text('{"nope": true}', encoding="utf-8")
        with self.assertRaises(catalog.CatalogError):
            catalog.load(empty)

    def test_entries_without_a_text_file_are_dropped(self):
        path = self.write_catalog({"good": record("a", "a.txt", "One"), "bad": {"documentId": "b"}})
        loaded = catalog.load(path)
        self.assertEqual(sorted(loaded), ["good"])


class VerificationTests(CatalogueCase):
    def issuer(self):
        return reader.Issuer(reader.derive_secret("synthetic-token-for-catalogue-tests"), str(self.root), "fixture")

    def test_a_matching_catalogue_entry_yields_a_readable_handle(self):
        path, version = self.write("a.txt")
        item = record("a", "a.txt", "One", textSHA256=version)
        window, refusal = catalog.verify_text_file(self.selected(), item, self.issuer())
        self.assertIsNone(refusal)
        self.assertEqual(window["sourceVersion"], version)
        self.assertTrue(window["fileHandle"].startswith(reader.HANDLE_PREFIX))
        read_back = reader.read(reader.derive_secret("synthetic-token-for-catalogue-tests"), str(self.root),
                                "fixture", self.library, handle=window["fileHandle"])
        self.assertEqual(read_back["excerpt"].split("\n")[0], "synthetic literature text")

    def test_traversal_absolute_paths_and_out_of_library_entries_are_refused(self):
        outside = self.root / "outside.txt"
        outside.write_text("outside the library\n", encoding="utf-8")
        for text_file in ("../outside.txt", str(outside), "sub/../../outside.txt"):
            item = record("a", text_file, "One", textSHA256=evidence.digest(outside.read_bytes()))
            window, refusal = catalog.verify_text_file(self.selected(), item, self.issuer())
            self.assertIsNone(window, text_file)
            self.assertEqual(refusal["code"], reader.READ_FILE_UNAVAILABLE, text_file)

    def test_a_stale_hash_or_a_changed_file_is_refused(self):
        self.write("a.txt", "original text\n")
        item = record("a", "a.txt", "One", textSHA256="0" * 64)
        window, refusal = catalog.verify_text_file(self.selected(), item, self.issuer())
        self.assertIsNone(window)
        self.assertEqual(refusal["code"], "DOCUMENT_CHANGED")

    def test_a_real_library_must_also_authorize_the_mapped_file(self):
        path, version = self.write("a.txt")
        authorized = {"id": "trial", "root": str(self.library), "syntheticFixture": False,
                      "realDataTrial": {"authorized": True, "files": {"other.txt": version}}}
        window, refusal = catalog.verify_text_file(authorized, record("a", "a.txt", "One", textSHA256=version),
                                                   self.issuer())
        self.assertIsNone(window)
        self.assertEqual(refusal["code"], catalog.DOCUMENT_NOT_READABLE)
        good = dict(authorized, realDataTrial={"authorized": True, "files": {"a.txt": version}})
        window, refusal = catalog.verify_text_file(good, record("a", "a.txt", "One", textSHA256=version),
                                                   self.issuer())
        self.assertIsNone(refusal)
        self.assertEqual(window["sourceVersion"], version)

    def test_a_missing_file_is_refused(self):
        window, refusal = catalog.verify_text_file(self.selected(), record("a", "gone.txt", "One",
                                                                          textSHA256="0" * 64), self.issuer())
        self.assertIsNone(window)
        self.assertEqual(refusal["code"], reader.READ_FILE_UNAVAILABLE)


class LoadingTests(CatalogueCase):
    def test_the_catalogue_is_cached_by_path_and_mtime(self):
        path = self.write_catalog({"a": record("a", "a.txt", "One")})
        first = catalog.load(path)
        second = catalog.load(path)
        self.assertIs(first, second)
        os.utime(path, (10, 10))
        third = catalog.load(path)
        self.assertIsNot(first, third)


if __name__ == "__main__":
    unittest.main()
