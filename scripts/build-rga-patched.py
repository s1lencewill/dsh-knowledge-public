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
def find_vcvars():
    configured = os.environ.get("DSH_KNOWLEDGE_VCVARS64")
    if configured:
        return Path(configured)
    vswhere = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft Visual Studio/Installer/vswhere.exe"
    if not vswhere.is_file():
        return None
    result = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"], capture_output=True, text=True)
    return Path(result.stdout.strip()) / "VC/Auxiliary/Build/vcvars64.bat" if result.returncode == 0 and result.stdout.strip() else None

VCVARS = find_vcvars()
PROXY = os.environ.get("HTTPS_PROXY")


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
    env = dict(os.environ)
    if PROXY:
        env.update(HTTPS_PROXY=PROXY, HTTP_PROXY=PROXY)
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
    if VCVARS is None or not VCVARS.is_file():
        raise SystemExit("MSVC Build Tools missing; install C++ tools or set DSH_KNOWLEDGE_VCVARS64")
    command = 'call "%s" >nul 2>&1 && cargo build --release --locked --manifest-path "%s"' % (VCVARS, SOURCE / "Cargo.toml")
    env = dict(os.environ, CARGO_TERM_COLOR="never")
    if PROXY:
        env.update(HTTPS_PROXY=PROXY, HTTP_PROXY=PROXY)
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
