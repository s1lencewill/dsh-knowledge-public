"""Freeze, or verify, the code-and-test baseline a real-test window runs against.

    python scripts/freeze-baseline.py --write     record the SHA256 of every load-bearing file
    python scripts/freeze-baseline.py --verify    compare the worktree with the recorded baseline

Why this exists: a real-model test window produces a result *about a particular version of this code*. If the
code moves while the window is open, the result describes no version at all - and "we only changed a comment"
is not checkable by memory. So the file set is enumerated from an explicit list, hashed, and written to a
gitignored manifest beside the other verification output. `--verify` says exactly which files moved.

Deliberately *not* a git operation: the same information is wanted whether or not the change is committed, and
a manifest that only works on a clean tree would be useless for the window this project actually runs.
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / ".local" / "verification" / "frozen-baseline.json"

#: Every file a real-test result would depend on. Directories are expanded, not listed twice.
SOURCES = [
    ("engine/*.py", "the service the queries actually run in"),
    ("scripts/build-markdown-library.py", "the tool that builds and refreshes the library"),
    ("scripts/verify-library.py", "the zero-model scale acceptance"),
    ("scripts/verify-install.mjs", "the installed-artifact check"),
    ("scripts/verify-all.mjs", "the gate that decides what passed"),
    ("src/*.ts", "the plugin the host loads"),
    ("tests/*", "the regressions that stand behind every claim in the protocol"),
    ("trial/*.mjs", "install / doctor / start / stop"),
    ("docs/m0-wiki-library-round.md", "the round record"),
    ("docs/m0-wiki-real-test-protocol.md", "the frozen protocol itself"),
    ("docs/LIMITATIONS.md", "what this build does not do"),
    ("docs/QUICKSTART.md", "how an operator drives it"),
    ("README.md", "the entry point"),
]
#: Skipped: generated, private, or not load-bearing. `lib/` is generated from `src/` by the build, and the
#: installed copy is hashed by `verify-install.mjs` against it - listing it here would double the question.
EXCLUDE = {"__pycache__", ".local", ".git", "node_modules"}


def files():
    seen = {}
    for pattern, _why in SOURCES:
        for path in sorted(ROOT.glob(pattern)):
            if not path.is_file() or path.suffix in (".pyc",):
                continue
            if any(part in EXCLUDE for part in path.parts):
                continue
            seen[path.relative_to(ROOT).as_posix()] = path
    return seen


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build():
    return {name: digest(path) for name, path in sorted(files().items())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="record the baseline")
    parser.add_argument("--verify", action="store_true", help="compare with the recorded baseline")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    args = parser.parse_args(argv)
    if not (args.write or args.verify):
        parser.error("give --write or --verify")
    manifest = Path(args.manifest)
    current = build()

    if args.write:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(
            {"recordedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "root": str(ROOT),
             "files": current}, indent=2, sort_keys=True), encoding="utf-8", newline="")
        print("frozen: %d file(s) -> %s" % (len(current), manifest))
        return 0

    if not manifest.is_file():
        print("refused: no baseline at %s; run --write first" % manifest)
        return 2
    recorded = json.loads(manifest.read_text(encoding="utf-8")).get("files") or {}
    added = sorted(set(current) - set(recorded))
    removed = sorted(set(recorded) - set(current))
    changed = sorted(name for name in set(current) & set(recorded) if current[name] != recorded[name])
    for label, names in (("added", added), ("removed", removed), ("changed", changed)):
        for name in names:
            print("  %s %s" % (label, name))
    if added or removed or changed:
        print("BASELINE MOVED: %d added, %d removed, %d changed of %d recorded"
              % (len(added), len(removed), len(changed), len(recorded)))
        print("Re-freeze deliberately (--write) and re-run the gate before trusting a window result.")
        return 1
    print("baseline intact: %d file(s) match the frozen manifest" % len(recorded))
    return 0


if __name__ == "__main__":
    sys.exit(main())
