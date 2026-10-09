"""Zero-model verification of one library at real scale, read-only against a running service.

    python scripts/verify-library.py --url http://127.0.0.1:18765 \\
        --token-file <instance>/data/service-token.txt \\
        --libraries <instance>/data/libraries.json \\
        --workspace "<the library's declared workspace>" --library-id <library id> \\
        --instance <instance> --corpus "<the library's root>"

No model call and no engine run: every section either reads the catalogue over HTTP or reads the
originals locally, and the one query-shaped check that would need a worker asserts the worker is never
reached. The corpus is opened read-only and its whole-tree digest is taken before and after the run, so
"the verification did not touch the library" is measured rather than asserted.

Items 2 and 9 need a catalogue they are allowed to modify, so they run against a **copy** of the corpus
in a temporary instance, in-process, with no service and no network. Item 11 checks the live instance's
job directory and service log around a set of refusals.

Timings are raw samples from a handful of runs on one machine: they are not percentiles and nothing
here claims to be one. Output is for an operator's console and for a gitignored report file; it prints
counts and library-relative paths, never document bodies.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))

import capacity
import catalog
import evidence
import markdown_library
import real_data

RESULTS = []


def record(item, state, detail, evidence_=None):
    RESULTS.append({"item": item, "state": state, "detail": detail, "evidence": evidence_ or {}})
    print("  %-4s %-36s %s" % (state, item, detail), flush=True)


def tree_digest(root):
    """One digest over the whole tree: paths, sizes and the SHA256 of every entry, sorted."""
    root = Path(root)
    lines = []
    for directory, children, names in os.walk(root, topdown=True, followlinks=False):
        children[:] = sorted(children)
        for name in sorted(names):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            try:
                if path.is_symlink() or path.is_junction():
                    lines.append("%s\0link" % relative)
                    continue
                data = path.read_bytes()
            except OSError as error:
                lines.append("%s\0unreadable\0%s" % (relative, type(error).__name__))
                continue
            lines.append("%s\0%d\0%s" % (relative, len(data), hashlib.sha256(data).hexdigest()))
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest(), len(lines)


class Service:
    """The instance's own HTTP surface, authenticated with the token file the host uses."""

    def __init__(self, url, token_file):
        self.url = url.rstrip("/")
        self.token = Path(token_file).read_text(encoding="utf-8").strip()
        if len(self.token) < 24:
            raise SystemExit("refused: %s does not look like a service token" % token_file)

    def call(self, path, body=None, method=None):
        request = urllib.request.Request(self.url + path,
                                         method=method or ("POST" if body is not None else "GET"))
        request.add_header("Authorization", "Bearer " + self.token)
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, payload, timeout=180) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, raw

    def libraries(self, workspace):
        return self.call("/libraries?workspace=" + urllib.parse.quote(str(workspace)))

    def catalogue(self, workspace, library_id, **body):
        return self.call("/catalogue", dict(body, workspace=str(workspace), libraryId=library_id))

    def documents(self, workspace, library_id, **body):
        return self.call("/documents", dict(body, workspace=str(workspace), libraryId=library_id))

    def read(self, workspace, library_id, **body):
        return self.call("/reads", dict(body, workspace=str(workspace), libraryId=library_id))


def paginate(service, workspace, library_id, page_size):
    """Every page, following the version the previous page reported."""
    entries, versions, offsets, start = [], set(), [], time.monotonic()
    version, total = None, None
    while True:
        body = {"offset": len(entries), "limit": page_size}
        if version:
            body["catalogueVersion"] = version
        status, payload = service.catalogue(workspace, library_id, **body)
        if status != 200 or not isinstance(payload, dict):
            return {"error": "%s %s" % (status, str(payload)[:200])}
        if payload.get("status") != "ok":
            return {"error": "status=%r warnings=%s" % (payload.get("status"), payload.get("warnings"))}
        version = payload["catalogueVersion"]
        versions.add(version)
        total = payload["total"]
        offsets.append(payload["offset"])
        entries.extend(payload["entries"])
        if not payload["hasMore"]:
            break
        if len(entries) > 50000:
            return {"error": "traversal did not terminate"}
    return {"entries": entries, "total": total, "versions": versions, "offsets": offsets,
            "pages": len(offsets), "elapsedMs": int((time.monotonic() - start) * 1000)}


def verify_pagination(service, workspace, library_id):
    traversal = paginate(service, workspace, library_id, 50)
    if "error" in traversal:
        record("1 full pagination", "FAIL", traversal["error"])
        return None
    entries = traversal["entries"]
    ids = [entry["documentId"] for entry in entries]
    unique = len(set(ids)) == len(ids)
    contiguous = [entry["index"] for entry in entries] == list(range(traversal["total"]))
    one_version = len(traversal["versions"]) == 1
    inside = all(not str(entry["textFile"]).startswith(("/", "\\"))
                 and ".." not in str(entry["textFile"]).replace("\\", "/").split("/") for entry in entries)
    readable = sum(1 for entry in entries if entry.get("readable"))
    ok = unique and contiguous and one_version and inside
    record("1 full pagination", "PASS" if ok else "FAIL",
           "total=%d returned=%d pages=%d unique=%s contiguous=%s one-version=%s all-inside=%s readable=%d"
           % (traversal["total"], len(entries), traversal["pages"], unique, contiguous, one_version, inside,
              readable),
           {"pages": traversal["pages"], "elapsedMs": traversal["elapsedMs"]})
    return {"entries": entries, "traversal": traversal, "readable": readable}


def pick_targets(entries, root):
    """The awkward paths: the deepest, the largest, a non-ASCII name and one with a space."""
    def depth(entry):
        return str(entry["textFile"]).count("/")

    def size(entry):
        try:
            return (root / str(entry["textFile"])).stat().st_size
        except OSError:
            return 0

    targets = {"deepest": max(entries, key=depth), "largest": max(entries, key=size)}
    for label, predicate in (("non-ascii", lambda text: any(ord(character) > 127 for character in text)),
                             ("spaced", lambda text: " " in text)):
        candidates = [entry for entry in entries if predicate(str(entry["textFile"]))]
        if candidates:
            targets[label] = max(candidates, key=depth)
    return targets


def verify_locate_and_read(service, workspace, library_id, root, targets):
    """Locate by file name, read it back, and re-check the offsets against the original bytes."""
    root = Path(root)
    positions, consistency, failures = [], [], []
    handle = None
    for label, entry in targets.items():
        relative = str(entry["textFile"])
        name = os.path.basename(relative.replace("/", os.sep))
        status, found = service.documents(workspace, library_id, fileName=name, limit=10)
        documents = (found.get("documents") if isinstance(found, dict) else None) or []
        match = next((document for document in documents if document.get("textFile") == relative), None)
        if status != 200 or match is None:
            failures.append("%s: locate -> %s" % (label, status))
            continue
        if not match.get("readable"):
            failures.append("%s: not readable (%s)" % (label, match.get("refusal")))
            continue
        if handle is None:
            handle = (match["fileHandle"], match["sourceVersion"])
        status, window = service.read(workspace, library_id, fileHandle=match["fileHandle"],
                                      sourceVersion=match["sourceVersion"], maxChars=4000)
        if status != 200 or not isinstance(window, dict):
            failures.append("%s: read -> %s" % (label, status))
            continue
        positions.append({"label": label, "relativePath": relative})
        raw = (root / relative).read_bytes()
        text = raw.decode("utf-8-sig").replace("\r\n", "\n")
        checks = {
            "sourceVersion": window["sourceVersion"] == hashlib.sha256(raw).hexdigest()
            == match["sourceVersion"],
            "offsetsRebuild": text[window["startChar"]:window["endChar"]] == window["excerpt"],
            "startLine": text[:window["startChar"]].count("\n") + 1 == window["startLine"],
            "endLine": window["endLine"] == window["startLine"] + window["excerpt"].count("\n"),
            "locationVerified": bool(((window.get("sourceCard") or {}).get("status") or {})
                                     .get("locationVerified")),
            "codePoints": all(isinstance(window[field], int)
                              for field in ("startChar", "endChar", "startLine", "endLine")),
        }
        consistency.append(dict(checks, label=label, relativePath=relative,
                                fileChars=len(text), windowChars=len(window["excerpt"])))
        if not all(checks.values()):
            failures.append("%s: %s" % (label, [name for name, value in checks.items() if not value]))
    record("3 locate/read awkward paths", "PASS" if positions and not failures else "FAIL",
           "%d path(s) located and read: %s%s"
           % (len(positions), ", ".join(item["label"] for item in positions),
              "" if not failures else "; " + "; ".join(failures)),
           {"paths": positions})
    record("5 line/offset/version consistency", "PASS" if consistency and not failures else "FAIL",
           "%d window(s) re-checked against the original bytes%s"
           % (len(consistency), "" if not failures else "; " + "; ".join(failures)),
           {"windows": consistency})
    return handle


def probe_default_ceiling(original):
    """Copy one file into a throwaway library at the DEFAULT ceiling and query it offline.

    The runner raises if it is ever called, so the assertion is not only that the library is refused but
    that the refusal happens before any worker exists - which is what makes it a capacity refusal rather
    than a failed query.
    """
    import asyncio
    import pipeline

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no engine process may start for an over-sized library")

    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        library = base / "library"
        library.mkdir()
        (library / original.name).write_bytes(original.read_bytes())
        request = {"mode": "FAST", "query": "q", "root": str(library), "libraryId": "probe",
                   "cacheRoot": str(base), "workDir": str(base / "job"),
                   "maxFileBytes": capacity.DEFAULT_MAX_FILE_BYTES}
        result = asyncio.run(pipeline.run_local(request, forbidden))
        return "LIBRARY_CAPACITY_EXCEEDED: fileBytes" in " ".join(result.get("warnings") or [])


def verify_large_file(service, workspace, library_id, root, entries):
    root = Path(root)
    over = []
    for entry in entries:
        try:
            size = (root / str(entry["textFile"])).stat().st_size
        except OSError:
            continue
        if size > capacity.DEFAULT_MAX_FILE_BYTES:
            over.append((size, entry))
    if not over:
        record("4 over-default file", "SKIP", "no file exceeds the default 2,000,000-byte ceiling")
        return
    size, entry = max(over, key=lambda item: item[0])
    relative = str(entry["textFile"])
    name = os.path.basename(relative.replace("/", os.sep))
    status, found = service.documents(workspace, library_id, fileName=name, limit=5)
    documents = (found.get("documents") if isinstance(found, dict) else None) or []
    match = next((document for document in documents if document.get("textFile") == relative), None)
    read_ok = False
    if match and match.get("readable"):
        status, window = service.read(workspace, library_id, fileHandle=match["fileHandle"],
                                      sourceVersion=match["sourceVersion"], maxChars=6000)
        read_ok = (status == 200 and isinstance(window, dict)
                   and window.get("sourceVersion") == hashlib.sha256((root / relative).read_bytes()).hexdigest())
    refused = probe_default_ceiling(root / relative)
    ok = bool(match and match.get("readable")) and read_ok and refused
    record("4 over-default file", "PASS" if ok else "FAIL",
           "%s is %d bytes: offered=%s read=%s; the same bytes at a 2,000,000-byte ceiling refused=%s"
           % (os.path.basename(relative), size, bool(match and match.get("readable")), read_ok, refused),
           {"relativePath": relative, "size": size})


def verify_cross_library(service, workspace, entries, libraries_path, library_id):
    failures = []
    listed = service.libraries(workspace)[1]
    ids = [entry["id"] for entry in listed] if isinstance(listed, list) else []
    if ids != [library_id]:
        failures.append("this workspace lists %r" % (ids,))
    status, _payload = service.catalogue(workspace, "literature-full-v2", limit=50)
    if status != 403:
        failures.append("naming another library returned %s, expected 403" % status)
    for entry in entries:
        if not str(entry["documentId"]).startswith("md-"):
            failures.append("foreign document id %r" % entry["documentId"])
    config = json.loads(Path(libraries_path).read_text(encoding="utf-8-sig"))
    bindings, id_sets = {}, {}
    for library in config["libraries"]:
        path = library.get("catalogue")
        if not path:
            continue
        documents, _ = catalog.load_bound(path, library["id"], library["root"], require_attribution=True)
        bindings[library["id"]] = {"documents": len(documents),
                                   "rootIdentity": capacity.root_identity(library["root"])}
        id_sets[library["id"]] = set(documents)
    keys = sorted(id_sets)
    shared = sum(len(id_sets[left] & id_sets[right]) for left, right in zip(keys, keys[1:]))
    if shared:
        failures.append("%d shared document id(s) across catalogues" % shared)
    record("6 cross-library separation", "PASS" if not failures else "FAIL",
           "workspace sees %r; naming the other library is 403; %d shared document id(s) across %d catalogue(s)"
           % (ids, shared, len(bindings)) + ("" if not failures else "; " + "; ".join(failures)),
           {"bindings": bindings, "sharedIds": shared})


def verify_visibility(service, workspace, library_id, root):
    """The two conditions, as the four cases they produce - counted, not paraphrased.

    A library is visible only when the session directory is at or under the declared workspace **and** it
    contains the library's root. So "anywhere under the workspace" is wrong, and the case that catches it
    is a subdirectory of the workspace that holds no library root: it must see nothing. That case is
    checked here because the earlier version of this script only walked *up* from the workspace and would
    have called a genuine two-condition rule "any descendant".
    """
    workspace = Path(workspace)
    root = Path(root)
    below = workspace / "raw"
    # A real sibling folder where it exists, so the "holds no library root" case is exercised on the
    # actual corpus rather than on a directory this script invented.
    sibling = next((child for child in sorted(workspace.iterdir())
                    if child.is_dir() and not _inside(root, child) and child != workspace), workspace)
    cases = {}
    for label, directory in (("at declared workspace", workspace),
                             ("below it, containing the root", below if below.is_dir() else workspace),
                             ("below it, holding no root", sibling),
                             ("above it", workspace.parent)):
        listed = service.libraries(directory)[1]
        cases[label] = [entry["id"] for entry in listed] if isinstance(listed, list) else listed
    ok = (cases["at declared workspace"] == [library_id] and cases["below it, containing the root"] == [library_id]
          and cases["below it, holding no root"] == [] and cases["above it"] == [])
    record("7 workspace visibility", "PASS" if ok else "FAIL",
           "at=%r below+root=%r below-no-root=%r above=%r"
           % (cases["at declared workspace"], cases["below it, containing the root"],
              cases["below it, holding no root"], cases["above it"]), cases)


def _inside(root, child):
    """Whether `root` is `child` or sits under it - the same component-wise test the service makes."""
    try:
        root, child = Path(root).resolve(), Path(child).resolve()
    except OSError:
        return False
    return root == child or child in root.parents


def verify_exclusions(root, entries):
    """What the extension filter leaves out, counted rather than assumed."""
    root = Path(root)
    inventory = {}
    for directory, _children, names in os.walk(root, topdown=True, followlinks=False):
        for name in names:
            path = Path(directory) / name
            bucket = inventory.setdefault(path.suffix.lower(), {"files": 0, "bytes": 0})
            try:
                bucket["files"] += 1
                bucket["bytes"] += path.stat().st_size
            except OSError:
                pass
    selected, _warnings = evidence.selected_files(root)
    catalogued = {str(entry["textFile"]) for entry in entries}
    searchable = {entry["relative"] for entry in selected}
    images = {suffix: bucket for suffix, bucket in inventory.items()
              if suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")}
    others = {suffix: bucket for suffix, bucket in inventory.items()
              if suffix not in evidence.EXTENSIONS and suffix not in images}
    ok = catalogued == searchable
    record("8 extension exclusion", "PASS" if ok else "FAIL",
           "catalogued=%d searchable-text=%d; images excluded=%d file(s)/%d bytes; other excluded=%s; "
           "image text is not searchable (no OCR)"
           % (len(catalogued), len(searchable), sum(b["files"] for b in images.values()),
              sum(b["bytes"] for b in images.values()), {s: b["files"] for s, b in others.items()}),
           {"inventory": dict(sorted(inventory.items())), "images": images})


def verify_refusals_start_nothing(service, workspace, library_id, instance):
    """A refusal must create no job and reach no worker. This section makes no query at all."""
    cache = Path(instance, "data", "query-cache")
    log = Path(instance, "data", "logs", "service.log")
    before_jobs = {path.name for path in cache.iterdir() if path.is_dir()} if cache.is_dir() else set()
    before_log = log.stat().st_size if log.is_file() else 0
    refusals = [("bad handle", service.read(workspace, library_id, fileHandle="krh1.not-a-real-token.x",
                                            maxChars=200)[0]),
                ("unknown library", service.documents(workspace, "no-such-library", title="x")[0]),
                ("foreign library", service.catalogue(workspace, "literature-full-v2", limit=1)[0]),
                ("bad offset", service.catalogue(workspace, library_id, offset=-1, limit=1)[0])]
    time.sleep(0.5)
    after_jobs = {path.name for path in cache.iterdir() if path.is_dir()} if cache.is_dir() else set()
    after_log = log.stat().st_size if log.is_file() else 0
    new = ""
    if after_log > before_log:
        with log.open("rb") as stream:
            stream.seek(before_log)
            new = stream.read().decode("utf-8", "replace")
    started = new.count("container_started") + new.count("ISOLATION_JOB") + new.count("JOB_BUDGET")
    ok = not (after_jobs - before_jobs) and started == 0
    record("11 refusals start nothing", "PASS" if ok else "FAIL",
           "4 refusal(s) -> %s; new job dirs=%d; new worker/deadline lines=%d"
           % ([code for _label, code in refusals], len(after_jobs - before_jobs), started))


def verify_timings(service, workspace, library_id, instance, root, handle):
    """Raw samples of each stage on one machine. No percentile is claimed."""
    config = json.loads(Path(instance, "data", "libraries.json").read_text(encoding="utf-8-sig"))
    entry = next(library for library in config["libraries"] if library["id"] == library_id)
    catalogue_path = entry["catalogue"]
    timings = {}

    def hash_cost():
        real_data.authorize(entry, evidence.EXTENSIONS)

    def catalogue_cost():
        import catalog as module
        documents, _ = module.load_bound(catalogue_path, library_id, root, require_attribution=True)
        module.enumerate_catalog(documents, 0, 50)

    def snapshot_cost():
        import pipeline
        with tempfile.TemporaryDirectory() as directory:
            pipeline.build_snapshot(str(root), directory, capacity.resolve(entry))

    def read_cost():
        if handle:
            service.read(workspace, library_id, fileHandle=handle[0], sourceVersion=handle[1], maxChars=6000)

    for label, function, samples in (("authorize: hash the whole authorized set", hash_cost, 3),
                                     ("catalogue: read + one page of 50", catalogue_cost, 3),
                                     ("snapshot: copy the whole library", snapshot_cost, 1),
                                     ("read: one window over HTTP", read_cost, 3)):
        durations = []
        for _ in range(samples):
            start = time.monotonic()
            function()
            durations.append(int((time.monotonic() - start) * 1000))
        timings[label] = durations
    record("10 stage timings (raw samples)", "PASS",
           "; ".join("%s = %s ms" % (label, durations) for label, durations in timings.items()),
           {"note": "raw samples on one machine; no percentile is claimed", "timingsMs": timings})


def copy_text_only(source, destination):
    """A read-only copy of a corpus's text files, preserving relative paths. For the offline sections."""
    source = Path(source)
    for entry in evidence.selected_files(source)[0]:
        target = Path(destination) / entry["relative"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(entry["source"], target)
    return destination


def offline_sections(corpus):
    """Items 2 and 9 need a catalogue they may modify: a temporary instance, never the live one.

    Both run in-process against the service application with a throwaway libraries.json, a throwaway
    catalogue and a throwaway copy of the corpus. No engine, no model client, no network, and the live
    instance's files are never opened.
    """
    from unittest.mock import patch
    # This process is not the service: it needs a token of its own to call the app in-process, and it
    # never wants isolation (nothing here spawns a container). Neither setting reaches the live service.
    os.environ.setdefault("DSH_KNOWLEDGE_TOKEN", "synthetic-token-for-library-verification")
    os.environ["DSH_KNOWLEDGE_ISOLATION"] = "off"
    import service as service_app
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        library = copy_text_only(corpus, base / "wiki")
        config = base / "libraries.json"
        catalogue = base / "catalogue.json"
        entry = {"id": "temp-wiki", "name": "temp copy", "workspace": str(base), "root": str(library),
                 "syntheticFixture": True, "catalogue": str(catalogue), "maxFileBytes": 4_000_000}
        config.write_text(json.dumps({"libraries": [entry]}), encoding="utf-8")
        scanned = markdown_library.scan(library, "temp-wiki", 4_000_000)
        catalogue.write_text(json.dumps(markdown_library.catalogue_payload(scanned, "temp-wiki", library),
                                        ensure_ascii=False), encoding="utf-8")
        with patch.object(service_app, "CONFIG", config):
            client = TestClient(service_app.app)
            auth = {"Authorization": "Bearer " + service_app.TOKEN}
            body = {"workspace": str(base), "libraryId": "temp-wiki"}
            first = client.post("/catalogue", json=dict(body, limit=5), headers=auth).json()
            version = first["catalogueVersion"]
            payload = json.loads(catalogue.read_text(encoding="utf-8"))
            first_id = sorted(scanned["records"])[0]
            extra_path = library / "hidden-extra.md"
            extra_path.write_text((library / scanned["records"][first_id]["textFile"]).read_text(encoding="utf-8"),
                                  encoding="utf-8", newline="")
            payload["documents"]["md-hidden-extra.md"] = dict(
                scanned["records"][first_id], documentId="md-hidden-extra.md", textFile="hidden-extra.md",
                fileName="hidden-extra.md", textSHA256=evidence.digest(extra_path.read_bytes()))
            catalogue.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.utime(catalogue, None)
            moved = client.post("/catalogue", json=dict(body, limit=5, offset=0, catalogueVersion=version),
                                headers=auth).json()
            detected = moved.get("status") == "catalogue-changed" and moved.get("returned") == 0
            record("2 catalogue change while paging", "PASS" if detected else "FAIL",
                   "a mid-listing change is reported as %r with returned=%s and no page produced"
                   % (moved.get("status"), moved.get("returned")), {"warnings": moved.get("warnings")})

            # Item 2 left an extra note behind, so the authorization set is rebuilt from the copy as it
            # now stands. That is the same explicit refresh an operator would run, and it is what makes
            # the next block about *changes* rather than about the state the previous block created.
            scanned = markdown_library.scan(library, "temp-wiki", 4_000_000)

            def authorize_now():
                return real_data.authorize({"id": "temp-wiki", "root": str(library), "syntheticFixture": False,
                                            "realDataTrial": {"authorized": True,
                                                              "files": dict(scanned["manifest"])}},
                                           evidence.EXTENSIONS)[0]

            name = sorted(scanned["manifest"])[0]
            text = (library / name).read_text(encoding="utf-8")
            states = {"clean": authorize_now()}
            (library / "added.md").write_text("---\ntitle: Added\n---\n", encoding="utf-8", newline="")
            states["after add"] = authorize_now()
            os.remove(library / "added.md")
            (library / name).write_text(text + "\nan edit\n", encoding="utf-8", newline="")
            states["after edit"] = authorize_now()
            (library / name).write_text(text, encoding="utf-8", newline="")
            states["after restore"] = authorize_now()
            ok = (states["clean"] is True and states["after add"] is False and states["after edit"] is False
                  and states["after restore"] is True)
            record("9 change refuses, restore recovers", "PASS" if ok else "FAIL",
                   "authorize() over %d copied file(s): %s (the refresh is the explicit "
                   "`scan --apply`, never automatic)" % (len(scanned["manifest"]), states))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:18765")
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--libraries", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--library-id", required=True)
    parser.add_argument("--instance", default=None, help="instance root, for the log and job dirs")
    parser.add_argument("--corpus", default=None, help="corpus to copy for the offline sections")
    parser.add_argument("--json", default=None)
    parser.add_argument("--skip-integrity", action="store_true")
    args = parser.parse_args(argv)

    config = json.loads(Path(args.libraries).read_text(encoding="utf-8-sig"))
    entry = next((library for library in config["libraries"] if library["id"] == args.library_id), None)
    if entry is None:
        raise SystemExit("refused: %r is not registered in %s" % (args.library_id, args.libraries))
    root = Path(entry["root"])
    instance = args.instance or str(Path(args.libraries).resolve().parent.parent)
    print("verifying %s at %s (library ceiling %d bytes)" % (args.library_id, root, capacity.resolve(entry)))

    before = None
    if not args.skip_integrity:
        before = tree_digest(root)
        print("  corpus digest before: %s over %d entries" % (before[0][:16], before[1]))

    service = Service(args.url, args.token_file)
    status, health = service.call("/health")
    print("  health: %s isolation=%s active=%s" % (status, health.get("isolation"), health.get("isolationActive")))

    verify_visibility(service, args.workspace, args.library_id, root)
    pagination = verify_pagination(service, args.workspace, args.library_id)
    handle = None
    if pagination:
        entries = pagination["entries"]
        verify_exclusions(root, entries)
        handle = verify_locate_and_read(service, args.workspace, args.library_id, root,
                                        pick_targets(entries, root))
        verify_large_file(service, args.workspace, args.library_id, root, entries)
        verify_cross_library(service, args.workspace, entries, args.libraries, args.library_id)
    verify_refusals_start_nothing(service, args.workspace, args.library_id, instance)
    verify_timings(service, args.workspace, args.library_id, instance, root, handle)
    if args.corpus:
        offline_sections(Path(args.corpus))

    if before is not None:
        after = tree_digest(root)
        record("12 corpus unchanged", "PASS" if after == before else "FAIL",
               "digest %s -> %s over %d entries" % (before[0][:16], after[0][:16], after[1]))

    failed = [item for item in RESULTS if item["state"] == "FAIL"]
    print("\n%d item(s): %d pass, %d fail, %d skip"
          % (len(RESULTS), sum(1 for item in RESULTS if item["state"] == "PASS"), len(failed),
             sum(1 for item in RESULTS if item["state"] == "SKIP")))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(
            {"libraryId": args.library_id, "root": str(root),
             "recordedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "items": RESULTS},
            ensure_ascii=False, indent=2), encoding="utf-8", newline="")
        print("report: %s" % args.json)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
