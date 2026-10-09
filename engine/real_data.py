"""Real-materials trial authorization: off by default, per library, re-verified before every query.

M0 only permits libraries explicitly marked `syntheticFixture: true`. A real library has no implicit
path: its own config entry must carry an explicit trial authorization **and** the files on disk must
still match the recorded set and hashes. A missing declaration, a missing or malformed manifest, a
changed hash, an added file or a missing file all refuse the query, before any snapshot is built and
before any model client exists.

This deliberately does not fake `syntheticFixture`: the two cases stay distinguishable in config,
in diagnostics and in the result warning, so nobody can turn the real-data trial on by relabelling a
library as synthetic.
"""
import hashlib
from pathlib import Path

import evidence

#: The per-library config key that turns the trial on. Absent (or not literally true) means refused.
TRIAL_KEY = "realDataTrial"
NOT_AUTHORIZED = "REAL_DATA_TRIAL_NOT_AUTHORIZED"
MANIFEST_UNUSABLE = "REAL_DATA_TRIAL_MANIFEST_UNUSABLE"
FILES_CHANGED = "REAL_DATA_TRIAL_FILES_CHANGED"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_files(entry):
    """The declared {relative name: sha256} set, or (None, detail) when unusable."""
    trial = entry.get(TRIAL_KEY)
    if not isinstance(trial, dict):
        return None, ("%s: library %r is not marked synthetic and carries no %s authorization"
                      % (NOT_AUTHORIZED, entry.get("id"), TRIAL_KEY))
    if trial.get("authorized") is not True:
        return None, "%s: %s.authorized must be set to true" % (NOT_AUTHORIZED, TRIAL_KEY)
    files = trial.get("files")
    if not isinstance(files, dict) or not files:
        return None, "%s: %s.files must record the authorized file names and their SHA256 values" % (
            MANIFEST_UNUSABLE, TRIAL_KEY)
    cleaned = {}
    for name, digest in files.items():
        if not isinstance(name, str) or not isinstance(digest, str) or len(digest) != 64:
            return None, "%s: %s.files entries must be name -> 64-character SHA256" % (MANIFEST_UNUSABLE, TRIAL_KEY)
        cleaned[name.replace("\\", "/")] = digest.lower()
    return cleaned, ""


def actual_files(root, extensions):
    """{relative name: sha256} for the files a snapshot of `root` would use.

    This used to walk the tree on its own (`root.rglob("*")` plus an extension test) and had quietly
    drifted from the snapshot's rule: it counted hidden files and files inside `.git`, `node_modules`,
    `.venv`, `.local` and `__pycache__`, all of which the snapshot prunes. A single hidden `.md` under
    a library was therefore an "extra file" that refused every query with `REAL_DATA_TRIAL_FILES_CHANGED`
    while the snapshot itself never intended to read it - the gate disagreeing with the thing it gates.

    It now consumes the same walk the snapshot does (`evidence.selected_files`), so the authorized set
    and the searchable set are selected by one rule. `tests/real_data_test.py` pins the agreement on a
    tree containing every kind of entry that rule excludes.
    """
    root = Path(root)
    if not root.is_dir():
        return None
    found = {}
    for entry in evidence.selected_files(root, extensions)[0]:
        found[entry["relative"]] = sha256(entry["source"])
    return found


def authorize(entry, extensions):
    """Return (ok, detail, warning). `ok` False means refuse the query; `detail` is the error code.

    Counts only, never file names or content: the refusal is reported to a local client, but it also
    ends up in logs and diagnostics, and a private library's file names have no business there.
    """
    declared, detail = expected_files(entry)
    if declared is None:
        return False, detail, None
    if entry.get("sourceRoot"):
        try:
            import onboarding
            root = Path(entry["sourceRoot"])
            if any(evidence.linked(p) for p in [root, *root.parents]):
                return False, FILES_CHANGED, None
            if onboarding.original_manifest(root) != entry.get("sourceManifest"):
                return False, FILES_CHANGED, None
        except (OSError, ValueError, onboarding.Refused):
            return False, FILES_CHANGED, None
    found = actual_files(entry.get("root") or "", extensions)
    if found is None:
        return False, "%s: root %r is not a directory" % (FILES_CHANGED, entry.get("root")), None
    missing = sorted(set(declared) - set(found))
    extra = sorted(set(found) - set(declared))
    changed = sorted(name for name in set(declared) & set(found) if declared[name] != found[name])
    if missing or extra or changed:
        return False, ("%s: %d declared, %d found, %d missing, %d extra, %d changed"
                       % (FILES_CHANGED, len(declared), len(found), len(missing), len(extra), len(changed))), None
    return True, "", ("REAL_DATA_TRIAL_LIBRARY: %s: %d authorized file(s), SHA256 verified before this query"
                      % (entry.get("id"), len(declared)))
