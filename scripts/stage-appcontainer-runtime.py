"""Build the container runtime as a new directory, validate it, then swap it in.

Run with the project's Python 3.13 venv. Re-run after dependency changes: the service compares the
staged manifest against the current lock, patch, engine sources and tool binaries and refuses to
start when they have drifted.

Staging into a fresh directory and swapping means a stale package can never survive inside a
runtime that reported success.
"""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / "engine"))
import runtime_min
import staging

root = project / ".local/appcontainer-runtime"
staged = project / ".local/appcontainer-runtime.new"
previous = project / ".local/appcontainer-runtime.old"

if sys.version_info[:2] != (3, 13) or Path(sys.prefix).resolve() != (project / ".venv").resolve():
    raise SystemExit("Run with this project's Python 3.13 venv")

if staged.exists():
    shutil.rmtree(staged)
staged.mkdir(parents=True)
base = Path(sys.base_prefix)
for pattern in ("python*.exe", "*.dll"):
    for file in base.glob(pattern):
        shutil.copy2(file, staged / file.name)
shutil.copytree(base / "DLLs", staged / "DLLs", dirs_exist_ok=True)
shutil.copytree(base / "Library/bin", staged / "Library/bin", dirs_exist_ok=True)
shutil.copytree(base / "Lib", staged / "Lib", ignore=shutil.ignore_patterns("site-packages", "__pycache__"),
                dirs_exist_ok=True)
# Site-packages is pruned while it is copied. The keep-set is measured (engine/runtime_min.py):
# copying the whole environment first would move ~1.3 GB only to delete ~1.1 GB of it again.
source_site = project / ".venv/Lib/site-packages"
dropped = set(runtime_min.removable(source_site))
dropped_size = runtime_min.removable_size(source_site)


def ignore_site(directory, names):
    if Path(directory).name == "site-packages":
        return [name for name in names if name == "__pycache__" or name in dropped]
    return [name for name in names if name == "__pycache__"]


shutil.copytree(source_site, staged / "Lib/site-packages", ignore=ignore_site, dirs_exist_ok=True)

# Validate the new copy before it replaces anything: the interpreter must run, and every module the
# container payload imports must resolve *inside* the staged directory. The staged prefix is a copy of
# the base interpreter rather than a venv, so without PYTHONNOUSERSITE the user's own site-packages is
# searched first and can silently stand in for a pruned dependency.
CHECK = (
    "import importlib, json, pathlib, sys\n"
    "names = %r\n"
    "modules = [importlib.import_module(name) for name in names]\n"
    "print('DSH_KNOWLEDGE_PROBE=' + json.dumps({\n"
    "    'prefix': sys.prefix,\n"
    "    'modules': {m.__name__: str(pathlib.Path(m.__file__).resolve()) for m in modules}}))\n"
) % (list(runtime_min.VALIDATE_IMPORTS),)
probe = subprocess.run([str(staged / "python.exe"), "-c", CHECK], capture_output=True, text=True,
                       timeout=300, env=dict(os.environ, PYTHONNOUSERSITE="1"))


def fail(reason):
    shutil.rmtree(staged, ignore_errors=True)
    raise SystemExit("new runtime failed validation: %s\n%s" % (reason, (probe.stdout + probe.stderr)[-2000:]))


if probe.returncode != 0:
    fail("the interpreter could not import the engine dependencies")
lines = [line for line in probe.stdout.splitlines() if line.startswith("DSH_KNOWLEDGE_PROBE=")]
if not lines:
    fail("the validation probe produced no result")
facts = json.loads(lines[-1].split("=", 1)[1])
if Path(facts["prefix"]).resolve() != staged.resolve():
    fail("the staged interpreter reports prefix %r" % facts["prefix"])
outside = {name: path for name, path in facts["modules"].items()
           if staged.resolve() not in Path(path).parents}
if outside:
    fail("these imports resolved outside the staged runtime: %s" % json.dumps(outside))
print("imports ok from " + staged.name + " (all inside the runtime)")

# Library/bin is where the base interpreter keeps its native dependencies. Only debug symbols and
# conda's own machinery are dropped (see runtime_min.LIBRARY_BIN_DROP); the DLLs Python links against
# stay, because the validation above cannot exercise every stdlib extension.
dropped_bin, bin_mb = runtime_min.prune_library_bin(staged)
print("Library/bin pruned: %d files (%s MB)" % (len(dropped_bin), bin_mb))

manifest = staging.describe(project)
staging.write(staged, manifest)
(staged / "ready").write_text("Python " + sys.version, encoding="utf-8")
kept_mb, kept_files = runtime_min.size(staged / "Lib/site-packages")
print("site-packages kept: %s MB / %d files; pruned %d entries (%s MB)" % (kept_mb, kept_files,
                                                                          len(dropped), dropped_size))

if previous.exists():
    shutil.rmtree(previous)
if root.exists():
    root.rename(previous)
staged.rename(root)
shutil.rmtree(previous, ignore_errors=True)
total_mb, total_files = 0, 0
for item in root.rglob("*"):
    if item.is_file():
        total_mb += item.stat().st_size
        total_files += 1
print("Runtime swapped in: %s MB / %d files, %d recorded inputs; this is not an isolation certification"
      % (round(total_mb / 1024 / 1024, 1), total_files, len(manifest["inputs"])))
