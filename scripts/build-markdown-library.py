"""Register, build, refresh and verify a Markdown library. Zero model calls, zero network.

    register   add a library entry to libraries.json (no catalogue, no manifest yet)
    scan       read the corpus and show what a catalogue + manifest would say; `--apply` publishes
    bind       attach the library binding to an existing catalogue (the migration for a legacy one)
    check      load the published catalogue through the service's own loader and report what it says

Everything is local: this tool never starts the engine, never builds a model client, never fetches a
`url:` out of front matter and never writes inside the corpus. `scan` is read-only until `--apply`.

The publish is two files - the catalogue and the `realDataTrial.files` manifest inside `libraries.json` -
and they are derived from one scan, so they are written together:

  1. both are rendered and re-parsed before anything is replaced, so a half-written file is never
     published;
  2. the catalogue is replaced **first**. In the moment between the two renames the manifest on disk is
     the previous one, so `real_data.authorize` refuses every call for this library
     (`REAL_DATA_TRIAL_FILES_CHANGED`). That is the safe half of the window: the alternative order would
     leave the *new* manifest with the *old* catalogue, which lists documents the manifest no longer
     authorizes - each one handed back as `readable: false`. A blanket refusal for a few milliseconds
     beats a listing that is wrong;
  3. the previous pair is copied to `backups/<stamp>-<library-id>/` first, and the exact rollback command
     is printed. Nothing else in `libraries.json` - no other library, no token, no path - is touched.
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

import capacity
import catalog
import markdown_library

EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 2, 3


def load_config(path):
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise SystemExit("refused: %s is not readable JSON (%s)" % (path, error))
    if not isinstance(config, dict) or not isinstance(config.get("libraries"), list):
        raise SystemExit("refused: %s carries no libraries list" % path)
    return config


def entry_for(config, library_id):
    for entry in config["libraries"]:
        if entry.get("id") == library_id:
            return entry
    return None


def dump(payload):
    """The on-disk shape. `ensure_ascii=False` keeps CJK names as names rather than escape soup."""
    return json.dumps(payload, ensure_ascii=False, indent=2)


def write_atomic(path, text):
    """Replace `path` in one step. A reader sees either the old file or the new one, never a torn one."""
    path = Path(path)
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(text, encoding="utf-8", newline="")
    os.replace(temporary, path)


def backup(directory, stamp, library_id, catalogue, config):
    """Copy the pair that is about to be replaced. A first publish has no catalogue to keep."""
    target = Path(directory) / ("%s-%s" % (stamp, library_id))
    target.mkdir(parents=True, exist_ok=True)
    if Path(catalogue).is_file():
        shutil.copy2(catalogue, target / "catalogue.json")
    else:
        (target / "NO-PREVIOUS-CATALOGUE").write_text("first publish\n", encoding="utf-8")
    shutil.copy2(config, target / "libraries.json")
    return target


def report_scan(scanned, limit, root, library_id):
    print("library      %s" % library_id)
    print("root         %s" % root)
    print("maxFileBytes %d" % limit)
    print("text files   %d  (limit %d)" % (scanned["files"], capacity.MAX_FILES))
    print("total bytes  %d  (limit %d)" % (scanned["totalBytes"], capacity.MAX_TOTAL_BYTES))
    print("records      %d" % len(scanned["records"]))
    print("manifest     %d entries, %d bytes of JSON" % (len(scanned["manifest"]),
                                                         markdown_library.manifest_bytes(scanned["manifest"])))
    print("hash cost    %d bytes re-hashed on every authorize() call" % markdown_library.hash_cost(scanned))
    print("scan time    %d ms" % scanned["elapsedMs"])
    if scanned["skipped"]:
        print("skipped      %d" % len(scanned["skipped"]))
        for item in scanned["skipped"][:10]:
            print("  %s: %s" % (item["reason"], item["path"]))
    if scanned["warnings"]:
        print("warnings     %d" % len(scanned["warnings"]))
        for warning in scanned["warnings"][:10]:
            print("  %s" % warning)


def publish(args, config, entry, scanned, library_id, root, catalogue_path):
    added, removed, changed = markdown_library.diff(
        ((entry.get("realDataTrial") or {}).get("files")), scanned)
    print("change       +%d -%d ~%d (added/removed/changed vs the authorized set)"
          % (len(added), len(removed), len(changed)))
    for label, names in (("added", added), ("removed", removed), ("changed", changed)):
        for name in names[:10]:
            print("  %s %s" % (label, name))
    if scanned["violations"]:
        print("capacity     REFUSED: %d violation(s)" % len(scanned["violations"]))
        for violation in scanned["violations"][:10]:
            print("  %s %s size=%s limit=%s" % (violation["dimension"], violation.get("path", "-"),
                                                 violation["size"], violation["limit"]))
        return EXIT_REFUSED
    if scanned["changed"]:
        print("stability    REFUSED: %d file(s) changed while they were being read" % len(scanned["changed"]))
        for name in scanned["changed"][:10]:
            print("  %s" % name)
        return EXIT_REFUSED
    payload = markdown_library.catalogue_payload(scanned, library_id, root)
    rendered = dump(payload)
    if len(rendered.encode("utf-8")) > catalog.MAX_CATALOG_BYTES:
        print("catalogue    REFUSED: %d bytes exceeds the %d the service accepts"
              % (len(rendered.encode("utf-8")), catalog.MAX_CATALOG_BYTES))
        return EXIT_REFUSED
    if not args.apply:
        print("preview only: nothing written. Re-run with --apply to publish.")
        return EXIT_OK
    config_before = dump(config)
    config_path = Path(args.libraries)
    update = {"realDataTrial": dict(entry.get("realDataTrial") or {}, authorized=True,
                                    files=dict(sorted(scanned["manifest"].items()))),
              "catalogue": str(catalogue_path)}
    if args.max_file_bytes is not None:
        update["maxFileBytes"] = args.max_file_bytes
    entry.update(update)
    config_after = dump(config)
    # Both documents are rendered and re-parsed before either is replaced.
    json.loads(rendered)
    json.loads(config_after)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    saved = backup(Path(args.backup_dir), stamp, library_id, catalogue_path, config_path)
    write_atomic(catalogue_path, rendered)
    write_atomic(config_path, config_after)
    print("published    %s" % catalogue_path)
    print("published    %s (manifest and catalogue field)" % config_path)
    print("backup       %s" % saved)
    print("rollback     copy %s\\catalogue.json over %s and %s\\libraries.json over %s"
          % (saved, catalogue_path, saved, config_path))
    print("previous     %d bytes of libraries.json" % len(config_before.encode("utf-8")))
    return EXIT_OK


def command_register(args):
    config_path = Path(args.libraries)
    config = load_config(config_path)
    if entry_for(config, args.id):
        raise SystemExit("refused: library %r is already registered in %s" % (args.id, config_path))
    root = Path(args.root).resolve(strict=True)
    workspace = Path(args.workspace).resolve(strict=True)
    if not root.is_relative_to(workspace):
        raise SystemExit("refused: root %s is not inside workspace %s" % (root, workspace))
    entry = {"id": args.id, "name": args.name, "workspace": str(workspace), "root": str(root),
             "syntheticFixture": False, "catalogue": str(Path(args.catalogue).resolve()),
             "realDataTrial": {"authorized": True, "files": {}}}
    if args.max_file_bytes is not None:
        value, detail = capacity.valid(args.max_file_bytes)
        if detail:
            raise SystemExit("refused: %s" % detail)
        entry["maxFileBytes"] = value
    if not args.apply:
        print("preview only. Would append to %s:" % config_path)
        print(dump(entry))
        print("NOTE an empty manifest authorizes nothing: every call for this library refuses "
              "(REAL_DATA_TRIAL_MANIFEST_UNUSABLE) until `scan --apply` fills it.")
        return EXIT_OK
    config["libraries"].append(entry)
    write_atomic(config_path, dump(config))
    print("registered   %s in %s" % (args.id, config_path))
    print("next         scan --libraries %s --id %s --apply" % (config_path, args.id))
    return EXIT_OK


def command_scan(args):
    config = load_config(args.libraries)
    entry = entry_for(config, args.id)
    if entry is None:
        raise SystemExit("refused: library %r is not registered in %s (run `register` first)"
                         % (args.id, args.libraries))
    root = Path(entry["root"])
    declared = args.catalogue or entry.get("catalogue") or ""
    if not str(declared).strip():
        raise SystemExit("refused: library %r declares no catalogue; pass --catalogue" % args.id)
    catalogue_path = Path(declared)
    effective = args.max_file_bytes if args.max_file_bytes is not None else entry.get("maxFileBytes")
    scanned = markdown_library.scan(root, args.id, effective)
    report_scan(scanned, capacity.resolve({"id": args.id, "maxFileBytes": effective}), root, args.id)
    return publish(args, config, entry, scanned, args.id, root, catalogue_path)


def command_bind(args):
    """Attach a binding to a catalogue that predates them - the migration path for the first library."""
    path = Path(args.catalogue)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise SystemExit("refused: %s is not readable JSON (%s)" % (path, error))
    if not isinstance(payload, dict) or not isinstance(payload.get("documents"), dict):
        raise SystemExit("refused: %s carries no documents mapping" % path)
    root = Path(args.root).resolve(strict=True)
    existing = (payload.get(catalog.BINDING_LIBRARY), payload.get(catalog.BINDING_ROOT))
    if existing != (None, None) and not args.force:
        raise SystemExit("refused: %s is already bound to %r (pass --force to rebind)" % (path, existing[0]))
    binding = catalog.binding_fields(args.id, root)
    if not args.apply:
        print("preview only. Would add to %s:" % path)
        print(dump(binding))
        print("documents    %d" % len(payload["documents"]))
        return EXIT_OK
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = Path(args.backup_dir) / ("%s-%s-bind" % (stamp, args.id))
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target / "catalogue.json")
    # The pair, not just the half this command edits. A catalogue binding is half of a *state*: rolling an
    # upgrade back needs the config and the catalogue that were in force together, and a backup that kept
    # only the file this command happened to write left the other half to be reconstructed by hand. That is
    # exactly what went wrong once, so the config is copied here even though this action does not touch it.
    if args.libraries and Path(args.libraries).is_file():
        shutil.copy2(args.libraries, target / "libraries.json")
    else:
        (target / "NO-LIBRARIES-JSON").write_text(
            "this bind was run without --libraries, so the config in force beside this catalogue was not "
            "captured\n", encoding="utf-8")
    payload.update(binding)
    write_atomic(path, dump(payload))
    print("bound        %s -> %s" % (path, binding))
    print("backup       %s" % target)
    return EXIT_OK


def command_check(args):
    """Load the catalogue the way the service does, and report what it says. Reads nothing else."""
    config = load_config(args.libraries)
    entry = entry_for(config, args.id)
    if entry is None:
        raise SystemExit("refused: library %r is not registered in %s" % (args.id, args.libraries))
    root = Path(entry["root"])
    declared = args.catalogue or entry.get("catalogue") or ""
    path = Path(declared)
    limit = capacity.resolve(entry)
    print("library      %s" % args.id)
    print("root         %s" % root)
    print("rootIdentity %s" % capacity.root_identity(root))
    print("catalogue    %s" % path)
    print("maxFileBytes %d" % limit)
    authorized = (entry.get("realDataTrial") or {}).get("files") or {}
    print("authorized   %d file(s)" % len(authorized))
    try:
        documents, version = catalog.load_bound(path, args.id, root, require_attribution=True)
    except catalog.CatalogError as error:
        print("refused      %s: %s" % (error.code, error.detail))
        return EXIT_REFUSED
    print("documents    %d" % len(documents))
    print("version      %s" % version)
    limits = []
    for record in documents.values():
        text_file = record.get("textFile")
        if isinstance(text_file, str) and text_file not in authorized:
            limits.append("UNAUTHORIZED %s" % text_file)
    print("mismatch     %d document(s) not in the authorized set" % len(limits))
    for item in limits[:10]:
        print("  %s" % item)
    return EXIT_OK if not limits else EXIT_REFUSED


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(target):
        target.add_argument("--libraries", required=True, help="the libraries.json to read")
        target.add_argument("--apply", action="store_true", help="write; without it nothing changes")
        target.add_argument("--backup-dir", default=None, help="where the previous pair is copied")

    register = sub.add_parser("register", help="add a library entry, with no manifest yet")
    common(register)
    register.add_argument("--id", required=True)
    register.add_argument("--name", default=None)
    register.add_argument("--workspace", required=True)
    register.add_argument("--root", required=True)
    register.add_argument("--catalogue", required=True)
    register.add_argument("--max-file-bytes", type=int, default=None)
    register.set_defaults(handler=command_register)

    scan = sub.add_parser("scan", help="build and (with --apply) publish the catalogue and manifest")
    common(scan)
    scan.add_argument("--id", required=True)
    scan.add_argument("--catalogue", default=None)
    scan.add_argument("--max-file-bytes", type=int, default=None)
    scan.set_defaults(handler=command_scan)

    bind = sub.add_parser("bind", help="attach a library binding to an existing catalogue")
    # `bind` touches one catalogue file: it neither reads nor needs libraries.json, so it must not
    # require one. `--libraries` stays accepted for a uniform command line, and is ignored.
    bind.add_argument("--libraries", required=False, default=None, help=argparse.SUPPRESS)
    bind.add_argument("--apply", action="store_true", help="write; without it nothing changes")
    bind.add_argument("--backup-dir", default=None, help="where the previous catalogue is copied")
    bind.add_argument("--id", required=True)
    bind.add_argument("--root", required=True)
    bind.add_argument("--catalogue", required=True)
    bind.add_argument("--force", action="store_true")
    bind.set_defaults(handler=command_bind)

    check = sub.add_parser("check", help="load the published catalogue through the service loader")
    common(check)
    check.add_argument("--id", required=True)
    check.add_argument("--catalogue", default=None)
    check.set_defaults(handler=command_check)

    args = parser.parse_args(argv)
    if args.backup_dir is None:
        anchor = args.libraries or getattr(args, "catalogue", None)
        args.backup_dir = str(Path(anchor).resolve().parent / "backups")
    if args.command == "register" and args.name is None:
        args.name = args.id
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
