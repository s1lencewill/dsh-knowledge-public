"""Two libraries in one service: enumeration, locating, paging and reading must not cross.

The defect this file pins down was real. There was one process-wide catalogue, read for whichever
library asked, so with two libraries configured the second one was answered out of the first one's
index: its titles, its file names, its page counts - each entry marked `readable: false`, because none
of those files exist in the library that was asked. A listing of another library's documents presented
as this library's is worse than a refusal, so the tests below assert *both* halves: what each library
returns for itself, and that the other library's material is absent from the response, not merely
unreadable.

Offline: synthetic libraries, synthetic catalogues, no engine, no model client, no network.
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
os.environ.setdefault("DSH_KNOWLEDGE_TOKEN", "synthetic-token-for-library-tests")
os.environ["DSH_KNOWLEDGE_ISOLATION"] = "off"
from fastapi.testclient import TestClient
import capacity
import catalog
import evidence
import service


class LibraryCase(unittest.TestCase):
    """A workspace holding two libraries, each with its own root, catalogue and manifest."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.config = self.root / "libraries.json"
        self.files = {}
        self.libraries = {
            "alpha": self.build("alpha", 3, "Alpha study %02d"),
            "beta": self.build("beta", 5, "Beta study %02d"),
        }
        self.write_config()
        self.patch_config = patch.object(service, "CONFIG", self.config)
        self.patch_config.start()
        self.addCleanup(self.patch_config.stop)
        # A process-wide catalogue is present and valid for the whole file, and must be irrelevant
        # whenever the libraries declare their own.
        self.patch_catalog = patch.dict(os.environ, {catalog.CATALOG_ENV: str(self.legacy_catalogue())})
        self.patch_catalog.start()
        self.addCleanup(self.patch_catalog.stop)
        self.client = TestClient(service.app)
        self.auth = {"Authorization": "Bearer " + service.TOKEN}

    def build(self, library_id, count, title):
        directory = self.workspace / library_id
        directory.mkdir()
        files, documents = {}, {}
        for index in range(count):
            name = "%s-%02d.md" % (library_id, index)
            text = "---\ntitle: %s\n---\nBody of %s number %02d.\n" % (title % index, library_id, index)
            path = directory / name
            path.write_text(text, encoding="utf-8", newline="")
            version = evidence.digest(path.read_bytes())
            files[name] = version
            documents["md-" + name] = {
                "documentId": "md-" + name, "textFile": name, "fileName": name,
                "titleRaw": title % index, "titleNormalized": catalog.squash(title % index),
                "authors": None, "year": "2024", "doi": None, "textSHA256": version,
                "verification": "undetermined", "quality": {}, "conflicts": []}
        self.files[library_id] = files
        catalogue = self.root / ("%s-catalogue.json" % library_id)
        payload = catalog.binding_fields(library_id, directory)
        payload["documents"] = documents
        catalogue.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return {"id": library_id, "name": library_id, "workspace": str(self.workspace),
                "root": str(directory), "syntheticFixture": True, "catalogue": str(catalogue),
                "maxFileBytes": 4_000_000}

    def legacy_catalogue(self):
        """A valid, unattributed catalogue - the shape the old single-library arrangement used."""
        path = self.root / "legacy.json"
        if not path.exists():
            payload = {"documents": {"legacy": {"documentId": "legacy", "textFile": "legacy.md",
                                                "fileName": "legacy.md", "titleRaw": "Legacy title",
                                                "textSHA256": "0" * 64}}}
            path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def write_config(self, entries=None):
        self.config.write_text(json.dumps({"libraries": entries if entries is not None
                                           else list(self.libraries.values())}), encoding="utf-8")

    def page(self, library_id, **body):
        payload = {"workspace": str(self.workspace), "libraryId": library_id}
        payload.update(body)
        return self.client.post("/catalogue", json=payload, headers=self.auth)

    def find(self, library_id, **body):
        payload = {"workspace": str(self.workspace), "libraryId": library_id}
        payload.update(body)
        return self.client.post("/documents", json=payload, headers=self.auth)

    def questions(self, library_id, query="anything"):
        # A complete scope: the session is part of what makes a submitted job recoverable, and a submission
        # without one is refused before the library gate this test is about.
        return self.client.post("/queries", json={"workspace": str(self.workspace),
                                                  "libraryId": library_id, "query": query,
                                                  "session": "session-library"},
                                headers=self.auth)


class EnumerationIsolationTests(LibraryCase):
    def test_each_library_enumerates_exactly_its_own_documents(self):
        alpha, beta = self.page("alpha", limit=50).json(), self.page("beta", limit=50).json()
        self.assertEqual((alpha["total"], beta["total"]), (3, 5))
        self.assertEqual([entry["textFile"] for entry in alpha["entries"]],
                         ["alpha-00.md", "alpha-01.md", "alpha-02.md"])
        self.assertEqual([entry["textFile"] for entry in beta["entries"]],
                         ["beta-00.md", "beta-01.md", "beta-02.md", "beta-03.md", "beta-04.md"])

    def test_a_page_from_one_library_never_carries_the_other_library_s_entries(self):
        for library_id, other in (("alpha", "beta"), ("beta", "alpha")):
            body = self.page(library_id, limit=50).json()
            rendered = json.dumps(body, ensure_ascii=False)
            self.assertNotIn("%s-0" % other, rendered,
                             "another library's file names must be absent, not merely unreadable")
            self.assertNotIn("%s study" % other.capitalize(), rendered)
            self.assertNotIn(str(self.root / "workspace" / other), rendered)
            for entry in body["entries"]:
                self.assertEqual(entry["sourceCard"]["libraryId"], library_id)
                self.assertTrue(entry["readable"])

    def test_paging_stays_inside_one_library_through_every_page(self):
        seen, version = [], None
        while True:
            body = {"offset": len(seen), "limit": 2}
            if version:
                body["catalogueVersion"] = version
            payload = self.page("alpha", **body).json()
            version = payload["catalogueVersion"]
            self.assertEqual(payload["total"], 3)
            seen.extend(entry["textFile"] for entry in payload["entries"])
            if not payload["hasMore"]:
                break
        self.assertEqual(seen, ["alpha-00.md", "alpha-01.md", "alpha-02.md"])
        self.assertEqual(len(seen), len(set(seen)))


class LocatingIsolationTests(LibraryCase):
    def test_a_locator_clue_only_ever_matches_its_own_library(self):
        alpha = self.find("alpha", title="alpha study 01").json()
        # The scorer is deliberately fuzzy, so more than one of alpha's own notes may match - what must
        # never happen is a document from beta appearing in alpha's answer.
        self.assertTrue(alpha["documents"])
        for document in alpha["documents"]:
            self.assertTrue(document["textFile"].startswith("alpha-"), document["textFile"])
        self.assertIn("title-exact", alpha["documents"][0]["matched"])
        # The same clue asked of beta can only be answered out of beta's own index. The scorer will offer
        # a weak partial match on the shared word "study" - that is its documented behaviour and it is
        # beta's own note - but nothing belonging to alpha may appear, and the exact title cannot match.
        beta = self.find("beta", title="alpha study 01").json()
        rendered = json.dumps(beta, ensure_ascii=False)
        self.assertNotIn("alpha-", rendered)
        self.assertNotIn("Alpha study", rendered)
        for document in beta["documents"]:
            self.assertTrue(document["textFile"].startswith("beta-"), document["textFile"])
            self.assertNotIn("title-exact", document["matched"])

    def test_a_handle_from_one_library_does_not_read_the_other(self):
        window = self.find("alpha", title="alpha study 00").json()["documents"][0]
        read_own = self.client.post("/reads", json={"workspace": str(self.workspace), "libraryId": "alpha",
                                                    "fileHandle": window["fileHandle"],
                                                    "sourceVersion": window["sourceVersion"],
                                                    "session": "cross"}, headers=self.auth)
        self.assertEqual(read_own.status_code, 200, read_own.text)
        foreign = self.client.post("/reads", json={"workspace": str(self.workspace), "libraryId": "beta",
                                                   "fileHandle": window["fileHandle"],
                                                   "sourceVersion": window["sourceVersion"],
                                                   "session": "cross"}, headers=self.auth)
        self.assertEqual(foreign.status_code, 403)
        self.assertIn("READ_HANDLE_FOREIGN", foreign.text)


class CatalogueResolutionTests(LibraryCase):
    def test_a_library_without_a_declared_catalogue_refuses_instead_of_borrowing_one(self):
        entries = [dict(self.libraries["alpha"]), dict(self.libraries["beta"])]
        entries[0].pop("catalogue")
        self.write_config(entries)
        response = self.find("alpha", title="alpha study 01")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn(catalog.CATALOG_UNAVAILABLE, response.text)
        self.assertNotIn("Legacy title", response.text,
                         "the process-wide catalogue must not answer for a library that declared none")
        self.assertNotIn("Beta study", response.text)
        # The other library still works: one unusable declaration does not take the service down.
        self.assertEqual(self.page("beta", limit=1).status_code, 200)

    def test_a_declared_catalogue_without_a_binding_is_refused_in_a_multi_library_configuration(self):
        entries = [dict(self.libraries["alpha"]), dict(self.libraries["beta"])]
        entries[0]["catalogue"] = str(self.legacy_catalogue())
        self.write_config(entries)
        response = self.find("alpha", title="legacy title")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn(catalog.CATALOGUE_NO_LIBRARY, response.text)
        self.assertEqual(response.json()["documents"] if "documents" in response.json() else [], [])

    def test_a_catalogue_bound_to_another_library_is_refused_before_a_record_is_read(self):
        entries = [dict(self.libraries["alpha"]), dict(self.libraries["beta"])]
        entries[0]["catalogue"] = self.libraries["beta"]["catalogue"]
        self.write_config(entries)
        for call in (self.page("alpha", limit=50), self.find("alpha", title="alpha study 01")):
            self.assertEqual(call.status_code, 503, call.text)
            self.assertIn(catalog.CATALOGUE_LIBRARY_MISMATCH, call.text)
            self.assertNotIn("Beta study", call.text)

    def test_a_catalogue_bound_to_another_root_is_refused(self):
        entries = [dict(self.libraries["alpha"]), dict(self.libraries["beta"])]
        other = self.workspace / "elsewhere"
        other.mkdir(exist_ok=True)
        path = self.root / "bound-elsewhere.json"
        payload = catalog.binding_fields("alpha", other)
        payload["documents"] = json.loads(
            Path(self.libraries["alpha"]["catalogue"]).read_text(encoding="utf-8"))["documents"]
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        entries[0]["catalogue"] = str(path)
        self.write_config(entries)
        response = self.page("alpha", limit=1)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn(catalog.CATALOGUE_LIBRARY_MISMATCH, response.text)

    def test_a_single_library_configuration_keeps_the_process_wide_catalogue(self):
        """The compatibility path: one library, an unattributed catalogue, exactly as before.

        Nothing in the library entry points at a catalogue, so the older arrangement - one process-wide
        `DSH_KNOWLEDGE_CATALOG` - is what answers. That is only safe because there is exactly one library
        for it to be confused with, which is precisely the condition this path is gated on.
        """
        self.write_config([dict(self.libraries["alpha"], catalogue="")])
        response = self.page("alpha", limit=50)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual([entry["title"] for entry in body["entries"]], ["Legacy title"])

    def test_the_same_shared_catalogue_is_refused_once_a_second_library_exists(self):
        entries = [dict(self.libraries["alpha"], catalogue=""), dict(self.libraries["beta"], catalogue="")]
        self.write_config(entries)
        for library_id in ("alpha", "beta"):
            response = self.page(library_id, limit=50)
            self.assertEqual(response.status_code, 503, response.text)
            self.assertIn(catalog.CATALOG_UNAVAILABLE, response.text)
            self.assertNotIn("Legacy title", response.text)

    def test_an_attributed_process_wide_catalogue_still_serves_the_library_it_names(self):
        """The migration path in its other direction: a shared file is fine once it is attributed."""
        path = self.legacy_catalogue()
        record = json.loads(path.read_text(encoding="utf-8"))["documents"]["legacy"]
        name = record["textFile"]
        (self.workspace / "alpha" / name).write_text("---\ntitle: Legacy\n---\nbody\n", encoding="utf-8",
                                                     newline="")
        payload = catalog.binding_fields("alpha", self.workspace / "alpha")
        payload["documents"] = {name: dict(record, textSHA256=evidence.digest(
            (self.workspace / "alpha" / name).read_bytes()))}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.write_config([dict(self.libraries["alpha"], catalogue="")])
        response = self.find("alpha", title="legacy title")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([document["textFile"] for document in response.json()["documents"]], [name])


class PerLibraryCeilingTests(LibraryCase):
    """`maxFileBytes` is per library, and the query path must honour the library it was asked about."""

    def large(self):
        path = self.workspace / "alpha" / "large.md"
        path.write_bytes(b"---\ntitle: Large\n---\n" + b"Z" * 2_500_000)
        version = evidence.digest(path.read_bytes())
        catalogue_path = Path(self.libraries["alpha"]["catalogue"])
        payload = json.loads(catalogue_path.read_text(encoding="utf-8"))
        payload["documents"]["md-large.md"] = {
            "documentId": "md-large.md", "textFile": "large.md", "fileName": "large.md",
            "titleRaw": "Large", "titleNormalized": "large", "textSHA256": version,
            "verification": "undetermined", "quality": {}, "conflicts": []}
        catalogue_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_the_same_note_is_offered_by_the_library_that_raised_its_ceiling(self):
        self.large()
        response = self.find("alpha", fileName="large.md")
        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()["documents"][0]
        self.assertTrue(document["readable"], "the library declared 4 MB and the file is 2.5 MB")
        read = self.client.post("/reads", json={"workspace": str(self.workspace), "libraryId": "alpha",
                                                "fileHandle": document["fileHandle"],
                                                "sourceVersion": document["sourceVersion"],
                                                "session": "big"}, headers=self.auth)
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual(read.json()["sourceVersion"], document["sourceVersion"])

    def test_a_library_at_the_default_ceiling_refuses_the_same_note_and_says_why(self):
        self.large()
        entries = [dict(self.libraries["alpha"]), dict(self.libraries["beta"])]
        beta_catalogue = Path(self.libraries["beta"]["catalogue"])
        payload = json.loads(beta_catalogue.read_text(encoding="utf-8"))
        payload["documents"]["md-large.md"] = {
            "documentId": "md-large.md", "textFile": "large.md", "fileName": "large.md",
            "titleRaw": "Large", "titleNormalized": "large", "textSHA256": "0" * 64,
            "verification": "undetermined", "quality": {}, "conflicts": []}
        beta_catalogue.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        entries[1]["catalogue"] = str(beta_catalogue)
        self.write_config(entries)
        response = self.find("beta", fileName="large.md")
        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()["documents"][0]
        self.assertFalse(document["readable"], "beta kept the default 2 MB ceiling")
        self.assertEqual(document["refusal"], "READ_FILE_UNAVAILABLE")
        self.assertIn("READ_FILE_UNAVAILABLE", response.json()["warnings"][0])

    def test_a_declaration_that_cannot_be_honoured_refuses_that_library_by_name(self):
        entries = [dict(self.libraries["alpha"]), dict(self.libraries["beta"])]
        entries[0]["maxFileBytes"] = "4000000"
        self.write_config(entries)
        for call in (self.page("alpha", limit=1), self.find("alpha", title="alpha study 01"),
                     self.questions("alpha")):
            self.assertEqual(call.status_code, 503, call.text)
            self.assertIn(capacity.CONFIG_INVALID, call.text)
            self.assertIn("alpha", call.text)
            self.assertNotIn("LIBRARY_CAPACITY_EXCEEDED", call.text,
                             "a bad declaration is not a capacity rejection and must not read as one")


class VisibilityTests(LibraryCase):
    def test_a_session_outside_the_declared_workspace_sees_nothing(self):
        outside = self.root / "outside"
        outside.mkdir()
        response = self.client.get("/libraries", params={"workspace": str(outside)}, headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        for library_id in ("alpha", "beta"):
            refused = self.client.post("/queries", json={"workspace": str(outside),
                                                         "libraryId": library_id, "query": "q"},
                                       headers=self.auth)
            self.assertEqual(refused.status_code, 403)

    def test_a_parent_of_the_workspace_does_not_see_the_libraries(self):
        """The declared workspace is a hard gate, and it is not widened by being a parent of it."""
        response = self.client.get("/libraries", params={"workspace": str(self.root)}, headers=self.auth)
        self.assertEqual(response.json(), [], "a session above the declared workspace must see nothing")

    def test_a_session_below_the_workspace_sees_the_libraries_it_contains(self):
        """Both conditions, stated as the three cases they produce.

        A session at the declared workspace sees every library under it. A session opened *inside* one
        library sees that library only, because the other library's root is not at or under the
        directory this session was opened in. And a subdirectory of the workspace that contains **no**
        library root sees nothing at all - "anywhere under the workspace" was never the rule, and a user
        who opens a session in a sibling folder must not read the empty answer as a broken install.
        """
        at_workspace = self.client.get("/libraries", params={"workspace": str(self.workspace)},
                                       headers=self.auth)
        self.assertEqual([entry["id"] for entry in at_workspace.json()], ["alpha", "beta"])
        inside = self.client.get("/libraries", params={"workspace": str(self.workspace / "alpha")},
                                 headers=self.auth)
        self.assertEqual([entry["id"] for entry in inside.json()], ["alpha"])
        sibling = self.workspace / "notes"
        sibling.mkdir(exist_ok=True)
        elsewhere = self.client.get("/libraries", params={"workspace": str(sibling)}, headers=self.auth)
        self.assertEqual(elsewhere.json(), [], "a subdirectory holding no library root sees nothing")


class RealDataIsolationTests(LibraryCase):
    """The authorization gate and the catalogue are per library as well, and agree with each other."""

    def test_two_real_libraries_authorize_and_locate_without_mixing(self):
        entries = []
        for library_id in ("alpha", "beta"):
            entry = dict(self.libraries[library_id], syntheticFixture=False)
            entry["realDataTrial"] = {"authorized": True,
                                      "files": dict(self.files[library_id])}
            entries.append(entry)
        self.write_config(entries)
        for library_id, count in (("alpha", 3), ("beta", 5)):
            body = self.page(library_id, limit=50).json()
            self.assertEqual(body["total"], count)
            for entry in body["entries"]:
                self.assertTrue(entry["readable"], entry)
        # Removing one file from one library's manifest refuses that library only.
        entries[0]["realDataTrial"]["files"].pop("alpha-00.md")
        self.write_config(entries)
        self.assertEqual(self.page("alpha", limit=1).status_code, 403)
        self.assertEqual(self.page("beta", limit=1).status_code, 200)

    def test_a_catalogue_entry_for_a_file_that_is_gone_is_listed_but_gets_no_handle(self):
        """A location miss is reported as one; it never becomes an unreadable entry with a title on it."""
        catalogue_path = Path(self.libraries["alpha"]["catalogue"])
        payload = json.loads(catalogue_path.read_text(encoding="utf-8"))
        payload["documents"]["md-deleted.md"] = {
            "documentId": "md-deleted.md", "textFile": "deleted.md", "fileName": "deleted.md",
            "titleRaw": "Deleted note", "titleNormalized": "deleted note", "textSHA256": "0" * 64,
            "verification": "undetermined", "quality": {}, "conflicts": []}
        catalogue_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        response = self.find("alpha", title="deleted note")
        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()["documents"][0]
        self.assertFalse(document["readable"])
        self.assertNotIn("fileHandle", document)
        self.assertEqual(document["refusal"], "READ_FILE_UNAVAILABLE")
        self.assertIn("DOCUMENT_NOT_READABLE", document["sourceCard"]["truncationNotices"])


if __name__ == "__main__":
    unittest.main()
