"""`knowledge_find_documents` over HTTP: authorization, catalogue hygiene, and read handoff.

Offline: a synthetic library and a synthetic catalogue, no engine worker and no model client. A hit
locates a file; nothing here says the file answers anything.
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
import reader
import service

TEXT_ONE = "Alpha study on interfacial water\nsecond line of alpha\n"
TEXT_TWO = "Beta study on aerosol acidity\nsecond line of beta\n"


class FindDocumentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.workspace = self.root
        self.files = {}
        for name, text in (("alpha.txt", TEXT_ONE), ("beta.txt", TEXT_TWO)):
            path = self.library / name
            path.write_text(text, encoding="utf-8")
            self.files[name] = evidence.digest(path.read_bytes())
        self.other_workspace = Path(tempfile.mkdtemp(prefix="find-other-"))
        self.addCleanup(shutil.rmtree, self.other_workspace, True)
        (self.other_workspace / "library").mkdir()

        self.config = self.root / "config.json"
        self.catalog_path = self.root / "identity.json"
        self.trial_catalogue = self.root / "trial-identity.json"
        self.write_config()

        self.write_catalog()

        self.patch_config = patch.object(service, "CONFIG", self.config)
        self.patch_config.start()
        self.addCleanup(self.patch_config.stop)
        # Still set, and deliberately so: this fixture now declares a catalogue per library, and the
        # process-wide variable must have no effect on a request once that is true.
        self.patch_catalog = patch.dict(os.environ, {catalog.CATALOG_ENV: str(self.catalog_path)})
        self.patch_catalog.start()
        self.addCleanup(self.patch_catalog.stop)
        self.client = TestClient(service.app)
        self.auth = {"Authorization": "Bearer " + service.TOKEN}

    def write_config(self, fixture_catalogue=None, trial_catalogue=None):
        """Two libraries, each naming its own catalogue.

        The fixture used to carry neither, which only worked while a single process-wide catalogue was
        read for whichever library asked. That arrangement is exactly what a second library breaks: the
        answer would be the *other* library's titles and file names. So the config now declares one
        catalogue per library, and each of those files is bound to the library it was built for.
        """
        self.config.write_text(json.dumps({"libraries": [
            {"id": "fixture", "name": "fixture", "workspace": str(self.workspace),
             "root": str(self.library), "syntheticFixture": True,
             "catalogue": str(self.catalog_path if fixture_catalogue is None else fixture_catalogue)},
            {"id": "trial", "name": "trial", "workspace": str(self.workspace),
             "root": str(self.library), "syntheticFixture": False,
             "catalogue": str(self.trial_catalogue if trial_catalogue is None else trial_catalogue),
             "realDataTrial": {"authorized": True, "files": {"alpha.txt": self.files["alpha.txt"]}}},
        ]}), encoding="utf-8")

    def write_catalog(self, documents=None, library_id="fixture", root=None, path=None):
        payload = documents if documents is not None else {
            "alpha-doc": {"documentId": "alpha-doc", "textFile": "alpha.txt", "fileName": "alpha.txt",
                          "titleRaw": "Alpha study on interfacial water",
                          "titleNormalized": catalog.squash("Alpha study on interfacial water"),
                          "authors": "A. Author and B. Writer", "year": "2024", "doi": "10.1000/alpha",
                          "textSHA256": self.files["alpha.txt"], "verification": "verified", "quality": {},
                          "conflicts": [], "pages": 3},
            "beta-doc": {"documentId": "beta-doc", "textFile": "beta.txt", "fileName": "beta.txt",
                         "titleRaw": "Beta study on aerosol acidity",
                         "titleNormalized": catalog.squash("Beta study on aerosol acidity"),
                         "authors": "C. Chemist", "year": "2022", "doi": "10.1000/beta",
                         "textSHA256": self.files["beta.txt"], "verification": "partial", "quality": {},
                         "conflicts": [], "pages": 2},
        }
        body = catalog.binding_fields(library_id, root or self.library)
        body["documents"] = payload
        (path or self.catalog_path).write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

    def find(self, **body):
        payload = {"workspace": str(self.workspace), "libraryId": "fixture"}
        payload.update(body)
        return self.client.post("/documents", json=payload, headers=self.auth)

    def test_authentication_and_library_scope(self):
        self.assertEqual(self.client.post("/documents", json={"workspace": str(self.workspace),
                                                              "libraryId": "fixture", "title": "x"}).status_code, 401)
        response = self.client.post("/documents", json={"workspace": str(self.other_workspace),
                                                        "libraryId": "fixture", "title": "x"}, headers=self.auth)
        self.assertEqual(response.status_code, 403)
        self.assertIn("Unknown library for workspace", response.text)

    def test_a_title_clue_returns_a_ranked_list_and_a_readable_handle(self):
        response = self.find(title="alpha study on interfacial water")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(len(body["documents"]), 1)
        document = body["documents"][0]
        self.assertEqual(document["documentId"], "alpha-doc")
        self.assertIn("title-exact", document["matched"])
        self.assertTrue(document["readable"])
        window = self.client.post("/reads", json={"workspace": str(self.workspace), "libraryId": "fixture",
                                                  "fileHandle": document["fileHandle"],
                                                  "sourceVersion": document["sourceVersion"],
                                                  "session": "find-test"}, headers=self.auth)
        self.assertEqual(window.status_code, 200, window.text)
        payload = window.json()
        text = evidence.normalised_text((self.library / "alpha.txt").read_bytes())
        self.assertEqual(evidence.excerpt_from_offsets(text, payload), payload["excerpt"])

    def test_two_matches_come_back_as_a_list_and_zero_hits_say_so(self):
        self.write_catalog({
            "one": {"documentId": "one", "textFile": "alpha.txt", "fileName": "alpha.txt",
                    "titleRaw": "Shared title", "titleNormalized": catalog.squash("Shared title"),
                    "textSHA256": self.files["alpha.txt"], "verification": "partial", "quality": {}, "conflicts": []},
            "two": {"documentId": "two", "textFile": "beta.txt", "fileName": "beta.txt",
                    "titleRaw": "Shared title", "titleNormalized": catalog.squash("Shared title"),
                    "textSHA256": self.files["beta.txt"], "verification": "partial", "quality": {}, "conflicts": []},
        })
        body = self.find(title="shared title").json()
        self.assertEqual([item["documentId"] for item in body["documents"]], ["one", "two"],
                         "a tie must not be silently resolved to the first entry")
        self.assertEqual(len(self.find(title="shared title", limit=1).json()["documents"]), 1)
        missing = self.find(title="nothing in this library").json()
        self.assertEqual(missing["documents"], [])
        self.assertTrue(any(item.startswith("NO_CATALOG_MATCH") for item in missing["warnings"]))

    def test_the_limit_and_the_query_shape_are_enforced(self):
        self.assertEqual(self.find(title="x" * 400).status_code, 422)
        self.assertEqual(self.find().status_code, 400)
        self.assertEqual(self.find(title="alpha", limit=0).status_code, 422)
        self.assertEqual(self.find(title="alpha", limit=99).status_code, 422)

    def test_a_missing_catalogue_is_reported_not_guessed(self):
        # Declared but absent: refused. And the process-wide catalogue - which is present, readable and
        # bound to the other library - must NOT be quietly substituted for it.
        absent = self.root / "absent.json"
        self.write_config(fixture_catalogue=absent)
        response = self.find(title="alpha")
        self.assertEqual(response.status_code, 503)
        self.assertIn(catalog.CATALOG_UNAVAILABLE, response.text)
        # Absent because nothing declares one: also refused, with the same code the caller can act on.
        with patch.dict(os.environ, {catalog.CATALOG_ENV: ""}):
            self.write_config(fixture_catalogue="")
            response = self.find(title="alpha")
        self.assertEqual(response.status_code, 503)
        self.assertIn(catalog.CATALOG_UNAVAILABLE, response.text)

    def test_a_catalogue_of_another_library_is_refused_not_answered_from(self):
        # The defect this binding exists for: library B asked, library A's catalogue loaded, and B was
        # handed A's titles and file names. Both are visible to this workspace, so nothing but the
        # binding distinguishes them.
        foreign = self.root / "foreign.json"
        self.write_catalog(library_id="some-other-library", path=foreign)
        self.write_config(fixture_catalogue=foreign)
        response = self.find(title="alpha study on interfacial water")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn(catalog.CATALOGUE_LIBRARY_MISMATCH, response.text)
        self.assertNotIn("Alpha study", response.text, "no other library's title may be returned")

    def test_a_catalogue_built_for_another_root_is_refused(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(exist_ok=True)
        self.write_catalog(library_id="fixture", root=elsewhere)
        response = self.find(title="alpha study on interfacial water")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn(catalog.CATALOGUE_LIBRARY_MISMATCH, response.text)

    def test_a_hostile_or_stale_catalogue_entry_is_listed_but_gets_no_handle(self):
        outside = self.root / "outside.txt"
        outside.write_text("outside the library\n", encoding="utf-8")
        self.write_catalog({
            "traversal": {"documentId": "traversal", "textFile": "../outside.txt", "fileName": "outside.txt",
                          "titleRaw": "Outside", "titleNormalized": catalog.squash("Outside"),
                          "textSHA256": evidence.digest(outside.read_bytes()), "verification": "partial",
                          "quality": {}, "conflicts": []},
            "stale": {"documentId": "stale", "textFile": "alpha.txt", "fileName": "alpha.txt",
                      "titleRaw": "Stale", "titleNormalized": catalog.squash("Stale"),
                      "textSHA256": "0" * 64, "verification": "partial", "quality": {}, "conflicts": []},
            "deleted": {"documentId": "deleted", "textFile": "gone.txt", "fileName": "gone.txt",
                        "titleRaw": "Gone", "titleNormalized": catalog.squash("Gone"),
                        "textSHA256": "0" * 64, "verification": "partial", "quality": {}, "conflicts": []},
        })
        for clue, expected in (("outside", reader.READ_FILE_UNAVAILABLE),
                               ("stale", "DOCUMENT_CHANGED"),
                               ("gone", reader.READ_FILE_UNAVAILABLE)):
            body = self.find(title=clue).json()
            self.assertEqual(len(body["documents"]), 1, clue)
            document = body["documents"][0]
            self.assertFalse(document["readable"], clue)
            self.assertNotIn("fileHandle", document, clue)
            self.assertEqual(document["refusal"], expected, clue)

    def test_a_real_library_still_checks_authorization_and_the_authorized_file_set(self):
        trial_root = self.root / "trial"
        trial_root.mkdir(exist_ok=True)
        (trial_root / "alpha.txt").write_text(TEXT_ONE, encoding="utf-8")
        version = evidence.digest((trial_root / "alpha.txt").read_bytes())
        self.write_config()
        self.config.write_text(json.dumps({"libraries": [
            {"id": "fixture", "name": "fixture", "workspace": str(self.workspace),
             "root": str(self.library), "syntheticFixture": True, "catalogue": str(self.catalog_path)},
            {"id": "trial", "name": "trial", "workspace": str(self.workspace),
             "root": str(trial_root), "syntheticFixture": False, "catalogue": str(self.trial_catalogue),
             "realDataTrial": {"authorized": True, "files": {"alpha.txt": version}}},
        ]}), encoding="utf-8")
        self.write_catalog({
            "alpha": {"documentId": "alpha", "textFile": "alpha.txt", "fileName": "alpha.txt",
                      "titleRaw": "Alpha", "titleNormalized": catalog.squash("Alpha"),
                      "textSHA256": version, "verification": "verified", "quality": {}, "conflicts": []},
        }, library_id="trial", root=trial_root, path=self.trial_catalogue)
        good = self.client.post("/documents", json={"workspace": str(self.workspace), "libraryId": "trial",
                                                    "fileName": "alpha.txt"}, headers=self.auth)
        self.assertEqual(good.status_code, 200, good.text)
        self.assertTrue(good.json()["documents"][0]["readable"])
        # A trial library that no longer matches its authorized set refuses the lookup as well.
        (trial_root / "unauthorized.txt").write_text("not authorized\n", encoding="utf-8")
        revoked = self.client.post("/documents", json={"workspace": str(self.workspace), "libraryId": "trial",
                                                       "fileName": "alpha.txt"}, headers=self.auth)
        self.assertEqual(revoked.status_code, 403)
        self.assertIn("REAL_DATA_TRIAL_FILES_CHANGED", revoked.text)

    def test_locating_builds_no_model_client_and_starts_no_engine(self):
        def forbidden(*_args, **_kwargs):
            raise AssertionError("locating a document must not touch the model or the engine")

        with patch.object(service.model_proxy, "provider_from_environment", forbidden), \
             patch.object(service, "run_isolated_async", forbidden), \
             patch.object(service.asyncio, "create_subprocess_exec", forbidden):
            response = self.find(title="alpha study on interfacial water")
        self.assertEqual(response.status_code, 200, response.text)

    def test_locating_writes_no_query_and_no_document_text_anywhere(self):
        sink = Path(tempfile.mkdtemp(prefix="find-diagnostics-"))
        self.addCleanup(shutil.rmtree, sink, True)
        with patch.object(service.stage_diagnostics, "DEFAULT_DIR", sink):
            response = self.find(title="alpha study on interfacial water")
        self.assertEqual(response.status_code, 200)
        written = "".join(path.read_text(encoding="utf-8") for path in sorted(sink.iterdir()))
        self.assertEqual(written, "", "a catalogue lookup must not write a diagnostics record")
        self.assertNotIn("alpha.txt", response.headers.get("x-anything", ""))


if __name__ == "__main__":
    unittest.main()
