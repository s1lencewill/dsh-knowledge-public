"""Pin the rg/rga binaries the runtime is built from, and record their hashes.

Run with the project venv:

    .\\.venv\\Scripts\\python.exe scripts\\pin-tools.py [--force]

A staged runtime is identified by the hashes of the tools it was built from (see engine/staging.py).
A tool found through PATH can change under the project's feet: on this host `rg` resolved to a
user-scoped `C:\\Users\\<user>\\.hanako\\.pi\\agent\\bin\\rg.exe`, which was updated and left the runtime
"stale" with no project change at all. So `rg` is copied to the project-local `.local/tools/rg.exe`
(never overwritten without --force) and `rga` is pinned to the controlled patched build. Everything -
staging, the service and the DSH launch environment - then uses those explicit absolute paths
(`DSH_KNOWLEDGE_RG` / `DSH_KNOWLEDGE_RGA`), which is also what the manifest hashes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / "engine"))
import runtime  # noqa: E402

PINNED = {"rg": project / ".local/tools/rg.exe", "rga": project / ".local/tools/rga-patched/rga.exe"}
RECORD = project / ".local/tools/pinned-tools.json"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def resolve(name, environment):
    """Where the tool comes from today: the patched rga build first, otherwise the usual search."""
    if name == "rga" and PINNED["rga"].is_file():
        return PINNED["rga"]
    return runtime.executable(name, environment, project, Path.home())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="replace an existing pinned copy of rg")
    arguments = parser.parse_args()

    environment = dict(os.environ)
    record = {"project": str(project), "tools": {}}
    for name in ("rg", "rga"):
        target = PINNED[name]
        source = resolve(name, environment)
        if target.is_file() and target.resolve() == Path(source).resolve():
            print("%s: already the pinned file at %s" % (name, target))
        elif target.is_file() and not arguments.force:
            print("%s: already pinned at %s (source today: %s; use --force to replace)"
                  % (name, target, source))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            print("%s: pinned %s -> %s" % (name, source, target))
        record["tools"][name] = {"path": str(target), "sha256": digest(target),
                                 "size": target.stat().st_size, "source": str(source)}
        print("    sha256=%s size=%d" % (record["tools"][name]["sha256"], record["tools"][name]["size"]))

    RECORD.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    print("recorded %s" % RECORD)
    print("use in stage/service/DSH environment:")
    print("  DSH_KNOWLEDGE_RG=%s" % PINNED["rg"])
    print("  DSH_KNOWLEDGE_RGA=%s" % PINNED["rga"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
