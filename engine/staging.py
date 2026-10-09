"""Runtime staleness detection: what the container image was built from, and whether it still matches.

The runtime is a copy of the interpreter, the venv's site-packages, the engine payload and the
rg/rga binaries. Rebuilding it by hand is easy to forget, so staging records a manifest of the
build inputs - the dependency lock, the rga patch, the engine sources and the tool binaries - and
the service refuses to start when any of them has moved on since.

Hashing site-packages itself is deliberately not attempted: the pinned lock file is what the
dependencies are built from, and hashing tens of thousands of files at every startup would cost
more than it protects.
"""
import hashlib
import json
import os
from pathlib import Path
import sys

# Engine files the container actually runs; the same list the service stages into the runtime.
ENGINE_FILES = ("worker.py", "runtime.py", "sirchmunk_compat.py", "ipc.py", "llm_bridge.py",
                "container_entry.py", "runtime_min.py")
BUILD_INPUTS = ("engine/requirements-win-py313.lock", "tools/rga/cache-path-fallback.patch")
MANIFEST_NAME = "stage-manifest.json"


def prune_spec():
    """The pruning the runtime was built with: the site-packages keep-set and the Library/bin drops.

    Read from the pruning code beside this module, not from the project being described: the spec is
    a property of the pruner, and a test project must not be able to redefine it. Recorded in the
    manifest so editing it invalidates an already staged runtime instead of leaving a container that
    is missing a package the new spec promised to provide.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import runtime_min
    return {"sitePackages": sorted(runtime_min.KEEP),
            "libraryBin": list(runtime_min.LIBRARY_BIN_DROP),
            "capabilities": dict(runtime_min.CAPABILITIES)}


def digest(path):
    """sha256 of a build input, or None when it is missing or unreadable.

    A deleted engine source or patch is itself a staleness signal, so it is recorded as a hole
    instead of raising out of `verify()` - a startup check must always return a verdict.
    """
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def resolve_tools(project):
    """The rg/rga source binaries the runtime is built from (patched rga preferred)."""
    sys.path.insert(0, str(Path(project) / "engine"))
    from runtime import executable
    project = Path(project)
    patched = project / ".local/tools/rga-patched/rga.exe"
    rga = patched if patched.is_file() else executable("rga", os.environ, project, Path.home())
    return {"rg": executable("rg", os.environ, project, Path.home()), "rga": rga}


def describe(project, tools=None):
    """Manifest of every input the staged runtime depends on."""
    project = Path(project)
    tools = tools or resolve_tools(project)
    inputs = {}
    for name in ENGINE_FILES:
        inputs["engine/" + name] = digest(project / "engine" / name)
    for name in BUILD_INPUTS:
        inputs[name] = digest(project / name)
    for name, path in sorted(tools.items()):
        inputs["tool:" + name] = digest(path)
    return {"python": "%d.%d.%d" % sys.version_info[:3], "inputs": inputs,
            "prune": prune_spec()}


def write(runtime, manifest):
    path = Path(runtime) / MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read(runtime):
    path = Path(runtime) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Torn or unreadable manifest: treat it exactly like a missing one.
        return None


def verify(runtime, project, tools=None):
    """Return (ok, detail). `detail` names the first drifted inputs, or the missing manifest."""
    recorded = read(runtime)
    if recorded is None:
        return False, "no %s in %s - re-run scripts/stage-appcontainer-runtime.py" % (MANIFEST_NAME, runtime)
    current = describe(project, tools)
    if current["python"] != recorded.get("python"):
        return False, "staged for Python %s, running Python %s" % (recorded.get("python"), current["python"])
    recorded_inputs = recorded.get("inputs") or {}
    drifted = []
    for name, value in sorted(current["inputs"].items()):
        if value is None or recorded_inputs.get(name) != value:
            drifted.append(name)
    if drifted:
        return False, "changed since staging: " + ", ".join(drifted)
    if (recorded.get("prune") or {}) != current["prune"]:
        return False, "the runtime prune spec changed since staging"
    return True, ""
