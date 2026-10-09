"""`knowledge_list_documents` over HTTP: pure-local enumeration, paging, and change handling.

Offline: a synthetic library and a synthetic catalogue with more entries than one page, no engine worker and no
model client. The point of these tests is that "which documents does this library contain" is answerable
*completely* — which no keyword search can do — and that paging over it cannot silently skip or repeat an entry.

A listed entry is a location, not evidence: nothing here claims a document answers anything.
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
os.environ.setdefault("DSH_KNOWLEDGE_TOKEN", "synthetic-token-for-service-test")
os.environ["DSH_KNOWLEDGE_ISOLATION"] = "off"
from fastapi.testclient import TestClient
import catalog
import evidence
import service

#: More than one page of the plugin's default page size (20) and more than `MAX_CANDIDATES` (10): the count is
#: deliberately past both, because "a keyword search cannot enumerate" is exactly what ten candidates hides.
DOCUMENT_COUNT = 23


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.workspace = self.root
        self.other_workspace = Path(tempfile.mkdtemp(prefix="catalogue-other-"))
        self.addCleanup(shutil.rmtree, self.other_workspace, True)
        (self.other_workspace / "library").mkdir()

        self.files = {}
        self.documents = {}
        for index in range(DOCUMENT_COUNT):
            name = "doc-%02d.txt" % index
            text = "Document %02d body.\nsecond line of %02d\n" % (index, index)
            path = self.library / name
            path.write_text(text, encoding="utf-8")
            self.files[name] = evidence.digest(path.read_bytes())
            key = "doc-%02d" % index
            self.documents[key] = {
                "documentId": key, "textFile": name, "fileName": name,
                "titleRaw": "Synthetic study %02d" % index,
                "titleNormalized": catalog.squash("Synthetic study %02d" % index),
                "authors": "A. Author", "year": "2024", "doi": "10.1000/%s" % key,
                "textSHA256": self.files[name], "verification": "verified", "quality": {}, "conflicts": [],
                "pages": 2,
            }

        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"libraries": [
            {"id": "fixture", "name": "fixture", "workspace": str(self.workspace),
             "root": str(self.library), "syntheticFixture": True},
        ]}), encoding="utf-8")
        self.catalog_path = self.root / "identity.json"
        self.write_catalog()

        self.patch_config = patch.object(service, "CONFIG", self.config)
        self.patch_config.start()
        self.addCleanup(self.patch_config.stop)
        self.patch_catalog = patch.dict(os.environ, {catalog.CATALOG_ENV: str(self.catalog_path)})
        self.patch_catalog.start()
        self.addCleanup(self.patch_catalog.stop)
        self.client = TestClient(service.app)
        self.auth = {"Authorization": "Bearer " + service.TOKEN}

    def write_catalog(self, documents=None):
        payload = documents if documents is not None else self.documents
        self.catalog_path.write_text(json.dumps({"documents": payload}, ensure_ascii=False), encoding="utf-8")

    def page(self, **body):
        request = {"workspace": str(self.workspace), "libraryId": "fixture"}
        request.update(body)
        return self.client.post("/catalogue", json=request, headers=self.auth)

    def test_authentication_and_library_scope(self):
        self.assertEqual(self.client.post("/catalogue", json={"workspace": str(self.workspace),
                                                              "libraryId": "fixture"}).status_code, 401)
        response = self.client.post("/catalogue", json={"workspace": str(self.other_workspace),
                                                        "libraryId": "fixture"}, headers=self.auth)
        self.assertEqual(response.status_code, 403)
        self.assertIn("Unknown library for workspace", response.text)

    def test_the_catalogue_is_enumerated_completely_and_never_by_keyword(self):
        # One page big enough for everything: the counts must describe the whole library.
        response = self.page(limit=50)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["total"], DOCUMENT_COUNT)
        self.assertEqual(body["returned"], DOCUMENT_COUNT)
        self.assertFalse(body["hasMore"])
        self.assertIsNone(body["nextOffset"])
        self.assertEqual([entry["index"] for entry in body["entries"]], list(range(DOCUMENT_COUNT)))
        # No query is involved and none is accepted: this endpoint has no keyword parameter at all.
        schema = self.client.post("/catalogue", json={"workspace": str(self.workspace), "libraryId": "fixture",
                                                      "query": "anything"}, headers=self.auth)
        self.assertEqual(schema.status_code, 200, "an unknown field must not silently become a search")
        self.assertEqual(schema.json()["total"], DOCUMENT_COUNT)

    def test_paging_covers_every_entry_exactly_once(self):
        seen, offsets, version = [], [], None
        while True:
            body = {"offset": len(seen), "limit": 7}
            if version is not None:
                body["catalogueVersion"] = version
            payload = self.page(**body).json()
            version = payload["catalogueVersion"]
            offsets.append(payload["offset"])
            seen.extend(entry["documentId"] for entry in payload["entries"])
            if not payload["hasMore"]:
                self.assertIsNone(payload["nextOffset"])
                break
            self.assertEqual(payload["nextOffset"], len(seen))
        self.assertEqual(len(seen), DOCUMENT_COUNT)
        self.assertEqual(len(set(seen)), DOCUMENT_COUNT, "no page may repeat an entry")
        self.assertEqual(sorted(seen), sorted(self.documents.keys()), "no page may skip an entry")
        self.assertEqual(offsets, [0, 7, 14, 21])

    def test_every_listed_entry_reports_its_identity_and_is_verified_against_the_library(self):
        body = self.page(limit=50).json()
        entry = body["entries"][0]
        self.assertEqual(entry["verification"], "verified")
        self.assertTrue(entry["readable"], "a readable entry carries a handle verified just now")
        self.assertIn("sourceVersion", entry)
        card = entry["sourceCard"]
        self.assertEqual(card["citationOrigin"], "catalogue")
        # A catalogue entry is not evidence: there is no excerpt and no location, and support is never assessed.
        self.assertIsNone(card["excerpt"])
        self.assertIsNone(card["location"])
        self.assertEqual(card["status"]["support"], "not-assessed")
        self.assertFalse(card["status"]["locationVerified"])

    def test_a_listed_handle_reads_the_document_it_named(self):
        body = self.page(limit=50).json()
        entry = body["entries"][3]
        window = self.client.post("/reads", json={"workspace": str(self.workspace), "libraryId": "fixture",
                                                  "fileHandle": entry["fileHandle"],
                                                  "sourceVersion": entry["sourceVersion"],
                                                  "session": "catalogue-read"}, headers=self.auth)
        self.assertEqual(window.status_code, 200, window.text)
        payload = window.json()
        self.assertEqual(payload["relativePath"], entry["textFile"])
        text = evidence.normalised_text((self.library / entry["textFile"]).read_bytes())
        self.assertEqual(evidence.excerpt_from_offsets(text, payload), payload["excerpt"])

    def test_a_catalogue_entry_that_no_longer_verifies_is_listed_as_unreadable(self):
        self.documents["doc-05"]["textFile"] = "outside.txt"
        (self.root / "outside.txt").write_text("outside the library\n", encoding="utf-8")
        self.write_catalog()
        body = self.page(limit=50).json()
        entry = next(item for item in body["entries"] if item["documentId"] == "doc-05")
        self.assertFalse(entry["readable"])
        self.assertNotIn("fileHandle", entry)
        self.assertIsNotNone(entry["refusal"])
        self.assertIn("DOCUMENT_NOT_READABLE", entry["sourceCard"]["truncationNotices"])
        self.assertEqual(len(body["entries"]), DOCUMENT_COUNT, "a refused entry is still listed")

    def test_a_catalogue_that_changed_between_pages_is_reported_instead_of_skipped(self):
        first = self.page(limit=10).json()
        version = first["catalogueVersion"]
        self.assertEqual(first["returned"], 10)
        # The catalogue gains a document underneath the reader, and one is renamed: a silent page-2 would now
        # skip or repeat entries, so the service must refuse to produce the page at all.
        self.documents["doc-new"] = dict(self.documents["doc-00"], documentId="doc-new", textFile="doc-00.txt")
        del self.documents["doc-01"]
        self.write_catalog()
        second = self.page(offset=10, limit=10, catalogueVersion=version).json()
        self.assertEqual(second["status"], "catalogue-changed")
        self.assertEqual(second["entries"], [], "no entries may be produced from a different listing")
        self.assertNotEqual(second["catalogueVersion"], version)
        self.assertEqual(second["nextOffset"], 0, "the caller is told to restart, not to guess an offset")
        self.assertTrue(any(item.startswith(catalog.CATALOGUE_CHANGED) for item in second["warnings"]))
        # Restarting with the new version walks the new listing completely.
        restart, seen = None, []
        while True:
            body = {"offset": len(seen), "limit": 10, "catalogueVersion": second["catalogueVersion"]}
            restart = self.page(**body).json()
            seen.extend(entry["documentId"] for entry in restart["entries"])
            if not restart["hasMore"]:
                break
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(sorted(seen), sorted(self.documents.keys()))

    def test_a_continuation_without_a_version_says_so_rather_than_implying_the_pages_join(self):
        body = self.page(offset=5, limit=5).json()
        self.assertEqual(body["returned"], 5)
        self.assertTrue(any(item.startswith("CATALOGUE_UNVERIFIED_CONTINUATION") for item in body["warnings"]))
        # With the version, the same page is produced without the warning.
        version = self.page(limit=50).json()["catalogueVersion"]
        verified = self.page(offset=5, limit=5, catalogueVersion=version).json()
        self.assertFalse(any(item.startswith("CATALOGUE_UNVERIFIED_CONTINUATION") for item in verified["warnings"]))

    def test_the_page_shape_is_enforced_at_the_boundary(self):
        self.assertEqual(self.page(limit=0).status_code, 422)
        self.assertEqual(self.page(limit=catalog.MAX_PAGE_SIZE + 1).status_code, 422)
        self.assertEqual(self.page(offset=-1).status_code, 422)
        self.assertEqual(self.page(offset=1.5).status_code, 422)
        self.assertEqual(self.page(limit=catalog.MAX_PAGE_SIZE).status_code, 200)
        # An offset past the end is an empty page, not an error: it is a position, not a document request.
        past = self.page(offset=DOCUMENT_COUNT + 5, limit=5).json()
        self.assertEqual(past["entries"], [])
        self.assertFalse(past["hasMore"])

    def test_the_default_page_size_is_a_page_and_not_the_whole_library(self):
        body = self.page().json()
        self.assertEqual(body["limit"], catalog.DEFAULT_PAGE_SIZE)
        self.assertEqual(body["returned"], catalog.DEFAULT_PAGE_SIZE)
        self.assertTrue(body["hasMore"])
        self.assertEqual(body["total"], DOCUMENT_COUNT)

    def test_a_missing_catalogue_is_reported_not_guessed(self):
        with patch.dict(os.environ, {catalog.CATALOG_ENV: ""}):
            response = self.page()
        self.assertEqual(response.status_code, 503)
        self.assertIn(catalog.CATALOG_UNAVAILABLE, response.text)
        with patch.dict(os.environ, {catalog.CATALOG_ENV: str(self.root / "absent.json")}):
            response = self.page()
        self.assertEqual(response.status_code, 503)
        self.assertIn(catalog.CATALOG_UNAVAILABLE, response.text)

    def test_an_empty_catalogue_says_it_is_empty(self):
        self.write_catalog({})
        body = self.page().json()
        self.assertEqual(body["total"], 0)
        self.assertEqual(body["entries"], [])
        self.assertFalse(body["hasMore"])
        self.assertTrue(any(item.startswith("CATALOGUE_EMPTY") for item in body["warnings"]))

    def test_enumeration_starts_no_engine_and_builds_no_model_client(self):
        # The whole point: a listing is a local catalogue read. Any engine or model work here would be a defect
        # that paging tests could not see, so the two entry points are asserted to be untouched.
        with patch.object(service, "run_isolated_async") as isolated, \
                patch.object(service, "execute") as execute, \
                patch.object(service, "start_isolation") as isolation:
            response = self.page(limit=50)
        self.assertEqual(response.status_code, 200, response.text)
        isolated.assert_not_called()
        execute.assert_not_called()
        isolation.assert_not_called()
