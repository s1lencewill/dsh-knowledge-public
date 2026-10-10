"""Build the patched rga 0.10.9 used by the isolated engine.

Pinned source tag, pinned lockfile, one small patch (see tools/rga/README.md). The artifact stays
in the gitignored .local tree; only the patch and this script are committed.

    python scripts/build-rga-patched.py [--force]
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

BASE = Path(__file__).resolve().parents[1]
TAG = "v0.10.9"
REPO = "https://github.com/phiresky/ripgrep-all.git"
SOURCE = BASE / ".local/rga-src"
PATCH = BASE / "tools/rga/cache-path-fallback.patch"
OUTPUT = BASE / ".local/tools/rga-patched/rga.exe"
VCVARS = Path(r"C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat")
PROXY = os.environ.get("HTTPS_PROXY") or "http://127.0.0.1:7897"


def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        print((result.stdout or "")[-2000:])
        print((result.stderr or "")[-2000:], file=sys.stderr)
        raise SystemExit("failed: %s" % " ".join(str(part) for part in command))
    return result


def fetch(force):
    if SOURCE.is_dir() and not force:
        return
    if SOURCE.exists():
        shutil.rmtree(SOURCE)
    env = dict(os.environ, HTTPS_PROXY=PROXY, HTTP_PROXY=PROXY)
    run(["git", "-c", "http.sslbackend=openssl", "clone", "--depth", "1", "--branch", TAG, REPO, str(SOURCE)], env=env)


def apply_patch():
    check = subprocess.run(["git", "apply", "--check", str(PATCH)], cwd=str(SOURCE), capture_output=True, text=True)
    if check.returncode == 0:
        run(["git", "apply", str(PATCH)], cwd=str(SOURCE))
        print("patch applied")
        return
    applied = subprocess.run(["git", "apply", "--reverse", "--check", str(PATCH)], cwd=str(SOURCE),
                             capture_output=True, text=True)
    if applied.returncode == 0:
        print("patch already applied")
        return
    print(check.stderr, file=sys.stderr)
    raise SystemExit("patch does not apply cleanly; re-verify the pinned tag")


def build():
    if not VCVARS.is_file():
        raise SystemExit("vcvars64.bat not found at %s" % VCVARS)
    command = 'call "%s" >nul 2>&1 && cargo build --release --locked --manifest-path "%s"' % (VCVARS, SOURCE / "Cargo.toml")
    env = dict(os.environ, HTTPS_PROXY=PROXY, HTTP_PROXY=PROXY, CARGO_TERM_COLOR="never")
    print("building (this takes minutes on a cold registry) ...")
    run(["cmd", "/c", command], env=env)


def install():
    built = SOURCE / "target/release/rga.exe"
    if not built.is_file():
        raise SystemExit("build produced no rga.exe")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built, OUTPUT)
    digest = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    manifest = {"tag": TAG, "lockfile": "Cargo.lock", "patch": str(PATCH.relative_to(BASE)),
                "sha256": digest, "size": OUTPUT.stat().st_size}
    (OUTPUT.parent / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("installed %s" % OUTPUT.relative_to(BASE))
    print("sha256 %s" % digest)
    version = subprocess.run([str(OUTPUT), "--version"], capture_output=True, text=True)
    print("version: %s" % (version.stdout or version.stderr).strip())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="re-clone the source tree")
    parser.add_argument("--skip-build", action="store_true")
    arguments = parser.parse_args()
    fetch(arguments.force)
    apply_patch()
    if not arguments.skip_build:
        build()
    install()


if __name__ == "__main__":
    main()
