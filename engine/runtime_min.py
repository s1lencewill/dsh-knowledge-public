"""Minimal container runtime: the site-packages entries the engine actually loads, and the pruning.

The staged runtime used to be a copy of the whole virtual environment (1,181.8 MB / 35,732 files),
which is mostly optional machinery the container never touches: `torch` alone is 506 MB and lives
behind two function-level imports in modules the retrieval path loads but never calls.

`KEEP` was **measured, not guessed** (see docs/m0-minimal-runtime.md):

* a real payload run (`worker.run` over the fixtures) recorded every module CPython actually loaded,
  which yields 27 site-packages entries (~206 MB);
* an AST scan of only those loaded files adds anything they could import from inside a function, so a
  lazy branch cannot surprise us;
* the optional probes that run observed (`brotli`, `matplotlib`, `pyarrow`) were *not* present and the
  run still succeeded, so their absence is part of the measured baseline.

Everything else is removed. `tests/runtime_min_test.py` recomputes the loaded set from a live import
and fails when a dependency upgrade starts using a package that is not in `KEEP`, so the failure
surfaces in the test suite instead of inside the container.
"""
from importlib.metadata import distributions
from pathlib import Path
import re

#: Site-packages entries the container payload needs. Every entry was observed loading, or is a
#: packaging shim that third-party code imports for version lookups.
KEEP = (
    # Engine retrieval stack (observed loading in a real payload run).
    "sirchmunk", "duckdb", "_duckdb.cp313-win_amd64.pyd", "igraph", "leidenalg", "rapidfuzz",
    "numpy", "pandas", "loguru", "texttable.py", "six.py", "dateutil",
    # Model bridge: the child holds no URL and no key, but it still speaks HTTP to the parent proxy.
    "openai", "httpx2", "httpcore2", "h11", "anyio", "sniffio", "idna", "truststore",
    "pydantic", "pydantic_core", "annotated_types", "typing_extensions.py", "typing_inspection",
    "win32_setctime",
    # Console handling that only loads when there is no terminal to detect. The first pruned probe
    # failed with `ModuleNotFoundError: No module named 'colorama'` (loguru imports it through
    # `loguru/_colorama.py`); the container has no console, so this branch is the one that runs.
    "colorama",
    # Packaging shims: `_distutils_hack` is imported by setuptools' path hook at interpreter start-up.
    "setuptools", "packaging", "_distutils_hack",
)

#: Packages that were removed **on purpose**, with the reason. The keep-set is small, so the reason a
#: heavy package is absent matters as much as the reason a light one is present: without this, the next
#: person re-adds `torch` "just in case" and the runtime goes back to a gigabyte. The test file asserts
#: none of these can be reached by the keep-set's prefix rule.
REMOVED_ON_PURPOSE = {
    "torch": "506 MB; imported only inside sirchmunk.utils.embedding_util/learnings.compiler, which the "
             "worker disables (reuse_knowledge=False, enable_knowledge_evolution=False)",
    "scipy": "128 MB; reached only from igraph's sparse-matrix helper, which this retrieval path never uses",
    "kreuzberg": "78 MB; reached only by DocumentExtractor for typed binary/office files - M0 snapshots "
                 "contain text only",
    "modelscope": "49 MB; embedding/tokenizer download helper, unused with knowledge reuse disabled",
    "transformers": "47 MB; sentence-transformers stack behind the same disabled embedding path",
    "sentence_transformers": "5 MB; the embedding model loader, never called on this path",
    "huggingface_hub": "model download client for the same unused embedding path",
    "PIL": "15 MB; sirchmunk.schema.metadata image handling, unused without knowledge snapshots",
    "lxml": "9 MB; pandas HTML/XML readers, unused by the retrieval path",
    "pypdf": "4 MB; PDF text extraction, unused for text-only snapshots",
    "openpyxl": "Excel reader/writer for table export in the disabled knowledge compiler",
    "xlsxwriter": "Excel writer for the same disabled compiler path",
    "pptx": "PowerPoint reader, reached only from the disabled knowledge compiler",
    "fastapi": "parent-side service dependency; the container runs worker.py, not the API",
    "sirchmunk_mcp": "sirchmunk's MCP server, never imported by the worker",
}

#: What the pruned runtime can actually do. This is not documentation: `stage_manifest.json` records
#: it, the service refuses to start when the configuration asks for more, and the container payload
#: configures the engine *from* it, so the declaration cannot drift away from the behaviour.
CAPABILITIES = {
    "mediaTypes": ["text"],
    "embeddings": False,
    "knowledgeReuse": False,
    "knowledgeEvolution": False,
}

#: File types this runtime can search. The parent's snapshot filter must stay inside it: adding a
#: binary/office format needs kreuzberg/pypdf/PIL back in the image, which means re-staging, not just
#: editing evidence.py.
TEXT_EXTENSIONS = frozenset({".md", ".txt", ".py", ".ts", ".tsx", ".js", ".json", ".yaml", ".yml"})

#: Environment variables that ask for machinery the pruned runtime does not ship. Setting them would
#: otherwise fail silently - the container environment is an allow-list, so the engine would simply
#: never see them - and "silently ignored" is the failure mode this whole round exists to remove.
UNSUPPORTED_ENV = ("EMBEDDING_MODEL_ID", "EMBEDDING_CACHE_DIR", "SIRCHMUNK_WORK_PATH",
                   "SIRCHMUNK_SEARCH_PATHS")


def incompatibilities(environment, extensions=TEXT_EXTENSIONS):
    """Reasons the current configuration cannot run on this runtime. Empty means compatible."""
    problems = []
    for name in UNSUPPORTED_ENV:
        if (environment.get(name) or "").strip():
            problems.append("%s is set, but the minimal runtime ships no embedding or "
                            "out-of-snapshot knowledge machinery" % name)
    binary = sorted(extension for extension in extensions if extension not in TEXT_EXTENSIONS)
    if binary:
        problems.append("the snapshot filter accepts %s, which needs removed parsers; re-stage with a "
                        "wider keep-set and declare it in CAPABILITIES" % ", ".join(binary))
    if CAPABILITIES.get("mediaTypes") != ["text"]:
        problems.append("CAPABILITIES declares %r, but the pruned keep-set was measured for text only"
                        % (CAPABILITIES.get("mediaTypes"),))
    if CAPABILITIES.get("embeddings") or CAPABILITIES.get("knowledgeReuse") \
            or CAPABILITIES.get("knowledgeEvolution"):
        problems.append("CAPABILITIES enables machinery whose packages are pruned: %s" % CAPABILITIES)
    return problems


#: Needed by the interpreter itself (`site.py` loads the .pth hooks) or by setuptools' shim.
PROTECTED_SUFFIXES = (".pth", ".egg-link")
#: Distribution metadata is tiny and its directory name does not always match the import name
#: (`python-dateutil` provides `dateutil`), so a name-based guess would break importlib.metadata.
PROTECTED_SUFFIXES_DIST = (".dist-info", ".egg-info")

#: Files under `Library/bin` that a running Python never loads. Measured: the whole directory is
#: 99.6 MB, and `.pdb` debug symbols (34 MB), conda's rename leftovers (`*.conda_trash`, 8 MB) and
#: `libmamba.dll` (16.6 MB, conda's own solver, loaded only by the conda CLI) are the bulk of it.
#: The remaining DLLs stay: Python's stdlib extensions do link against libcrypto/libssl/sqlite3/etc.
LIBRARY_BIN_DROP = ("*.pdb", "*.conda_trash*", "libmamba.dll")

_LOOSE = re.compile(r"\.(py|pyd|so|dll)$")

#: Modules the staged interpreter must import from inside the runtime. These live in site-packages;
#: the engine payload itself (worker/llm_bridge/ipc/...) is copied in by the service at startup, so
#: scripts/stage-appcontainer-runtime.py can only check this subset.
VALIDATE_IMPORTS = ("sirchmunk", "sirchmunk.llm", "openai", "httpx2", "duckdb", "igraph", "leidenalg",
                    "numpy", "pandas", "pydantic", "truststore")


def local_top_level(site):
    """Top-level site-packages entries worth considering: packages, extension modules, loose files."""
    site = Path(site)
    return sorted(path.name for path in site.iterdir() if path.name not in ("__pycache__",))


def is_kept(name, keep=KEEP):
    """True when a top-level entry belongs to a kept package.

    Matching is not exact on purpose: `numpy.libs` (the OpenBLAS DLLs numpy 2.x loads at import) is a
    separate top-level directory that only shares a prefix with `numpy`, and the same holds for
    `scipy.libs` and friends. Dropping those leaves an intact-looking package that cannot import.
    """
    return any(name == item or name.startswith(item + ".") or name.startswith(item + "-")
               for item in keep)


def removable(site, keep=KEEP):
    """The entries to delete: everything not kept, not a .pth hook and not distribution metadata."""
    names = []
    for name in local_top_level(site):
        if is_kept(name, keep) or name.endswith(PROTECTED_SUFFIXES + PROTECTED_SUFFIXES_DIST):
            continue
        names.append(name)
    return names


def prune(site, keep=KEEP):
    """Delete every non-kept entry. Returns (removed names, bytes freed)."""
    import shutil

    site = Path(site)
    removed, freed = [], 0
    for name in removable(site, keep):
        path = site / name
        try:
            if path.is_dir():
                freed += sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
                shutil.rmtree(path)
            else:
                freed += path.stat().st_size
                path.unlink()
        except OSError:
            continue  # a locked file must not fail staging: the import check still runs after
        removed.append(name)
    return removed, freed


def size(site, keep=KEEP):
    """(megabytes kept, files kept) for the entries in `keep` that exist."""
    site = Path(site)
    total = files = 0
    for path, amount, count in _walk(site, keep):
        total += amount
        files += count
    return round(total / 1024 / 1024, 1), files


def _walk(site, names):
    """Yield (path, bytes, files) for each top-level name, tolerating missing entries."""
    for name in names:
        for path in site.glob(name if "." in name else name + "*"):
            if path.is_dir():
                items = [item for item in path.rglob("*") if item.is_file()]
                yield path, sum(item.stat().st_size for item in items), len(items)
            elif path.is_file():
                yield path, path.stat().st_size, 1


def removable_size(site, keep=KEEP):
    """Bytes the prune will free, computed before anything is deleted."""
    return round(sum(amount for _, amount, _ in _walk(site, removable(site, keep))) / 1024 / 1024, 1)


def prune_library_bin(runtime):
    """Drop the Library/bin files in LIBRARY_BIN_DROP. Returns (removed names, megabytes freed)."""
    library = Path(runtime) / "Library" / "bin"
    removed, freed = [], 0
    for pattern in LIBRARY_BIN_DROP:
        for path in sorted(library.glob(pattern)):
            if not path.is_file():
                continue
            try:
                freed += path.stat().st_size
                path.unlink()
            except OSError:
                continue
            removed.append(path.name)
    return removed, round(freed / 1024 / 1024, 1)


def installed_distributions(site):
    """Distribution names importable from `site`, for the closure test."""
    return sorted({distribution.metadata["Name"] for distribution in distributions(path=[str(site)])
                   if distribution.metadata.get("Name")})
