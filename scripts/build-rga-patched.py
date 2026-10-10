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
PROXY = os.environ.get("HTTPS_PROXY")


def find_vcvars():
    """Find the installed MSVC toolchain without assuming a local Community edition."""
    override = os.environ.get("DSH_KNOWLEDGE_VCVARS")
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None
    program = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    vswhere = program / "Microsoft Visual Studio/Installer/vswhere.exe"
    if vswhere.is_file():
        found = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires",
                                "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-find",
                                "VC/Auxiliary/Build/vcvars64.bat"], capture_output=True, text=True)
        if found.returncode == 0:
            for line in found.stdout.splitlines():
                candidate = Path(line.strip())
                if candidate.is_file(): return candidate
    base = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft Visual Studio"
    for edition in ("Enterprise", "Professional", "Community", "BuildTools"):
        candidate = base / "2022" / edition / "VC/Auxiliary/Build/vcvars64.bat"
        if candidate.is_file(): return candidate
    return None


def build_environment():
    # Honour explicitly configured proxies; never assume a proxy on the developer's loopback port.
    env = dict(os.environ)
    if PROXY: env.update(HTTPS_PROXY=PROXY, HTTP_PROXY=PROXY)
    return env


def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        print((result.stdout or "")[-2000:])
        print((result.stderr or "")[-2000:], file=sys.stderr)
        raise SystemExit("failed: %s" % (command if isinstance(command,str) else " ".join(str(part) for part in command)))
    return result


def fetch(force):
    if SOURCE.is_dir() and not force:
        return
    if SOURCE.exists():
        if SOURCE.is_symlink() or SOURCE.is_junction() or SOURCE.resolve() != (BASE / ".local/rga-src").absolute():
            raise SystemExit("SOURCE_DIRECTORY_UNSAFE")
        shutil.rmtree(SOURCE)
    env = build_environment()
    run(["git", "-c", "http.sslbackend=openssl", "clone", "--config", "core.autocrlf=false", "--depth", "1", "--branch", TAG, REPO, str(SOURCE)], env=env)


def verify_source():
    expected = json.loads((BASE / "tools/rga/SOURCE.json").read_text(encoding="utf-8"))
    commit = run(["git", "rev-parse", "HEAD"], cwd=str(SOURCE)).stdout.strip()
    if commit != expected["commit"]: raise SystemExit("RGA_SOURCE_COMMIT_CHANGED")
    if hashlib.sha256((SOURCE / "LICENSE.md").read_bytes()).hexdigest() != expected["upstreamLicenseSHA256"]:
        raise SystemExit("RGA_LICENSE_CHANGED")
    if hashlib.sha256(PATCH.read_bytes()).hexdigest() != expected["patchSHA256"]:
        raise SystemExit("RGA_PATCH_CHANGED")


def apply_patch():
    effective = BASE / ".local/rga-patch-lf.patch"
    patch_bytes = PATCH.read_bytes().replace(b"\r\n", b"\n")
    expected = json.loads((BASE / "tools/rga/SOURCE.json").read_text(encoding="utf-8"))
    if hashlib.sha256(patch_bytes).hexdigest() != expected["appliedPatchSHA256"]:
        raise SystemExit("RGA_APPLIED_PATCH_CHANGED")
    effective.write_bytes(patch_bytes)
    check = subprocess.run(["git", "apply", "--check", str(effective)], cwd=str(SOURCE), capture_output=True, text=True)
    if check.returncode == 0:
        run(["git", "apply", str(effective)], cwd=str(SOURCE))
        print("patch applied")
        return
    applied = subprocess.run(["git", "apply", "--reverse", "--check", str(effective)], cwd=str(SOURCE),
                             capture_output=True, text=True)
    if applied.returncode == 0:
        print("patch already applied")
        return
    print(check.stderr, file=sys.stderr)
    raise SystemExit("patch does not apply cleanly; re-verify the pinned tag")


def run_cargo(vcvars, manifest, features=()):
    # cmd does not understand list2cmdline's backslash-escaped inner quotes. Use its explicit
    # /s /c command form; paths remain environment data, never interpolated shell source.
    if any(not feature.replace("-", "").isalnum() for feature in features): raise ValueError("CARGO_FEATURE_INVALID")
    env = dict(build_environment(), CARGO_TERM_COLOR="never",
               DSH_KNOWLEDGE_BUILD_VCVARS=str(vcvars), DSH_KNOWLEDGE_BUILD_MANIFEST=str(manifest))
    flags = " --features " + ",".join(features) if features else ""
    command = ('cmd /d /s /v:off /c "call "%DSH_KNOWLEDGE_BUILD_VCVARS%" >nul 2>&1 && '
               'cargo build --release --locked' + flags + ' --manifest-path "%DSH_KNOWLEDGE_BUILD_MANIFEST%""')
    return run(command, env=env)


def build():
    vcvars = find_vcvars()
    if vcvars is None:
        raise SystemExit("MSVC_REQUIRED")
    print("building (this takes minutes on a cold registry) ...")
    run_cargo(vcvars, SOURCE / "Cargo.toml")


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
    verify_source()
    apply_patch()
    if not arguments.skip_build:
        build()
    install()


if __name__ == "__main__":
    main()
