# Reproducing the container runtime

The container runtime is **not** in the trial package. It contains a modified AGPL-licensed `rga.exe` and two
GPL packages, so shipping it would attach obligations the package does not take on (see
`THIRD-PARTY-NOTICES.md`). The trial's `install --runtime-from <dir>` places a runtime you have produced
yourself, and `doctor` fails loudly when it is missing.

This file records how to produce one. **Steps marked *verified* were exercised on the build machine; steps
marked *to verify* are described from the code and have not been run end to end on a clean machine.**

## What the runtime is

A self-contained Python 3.13 installation plus a pruned `site-packages` and two pinned tool binaries, which
the service runs the extraction engine inside (Windows AppContainer, no network, no user profile). It is built
on **the machine that will use it**: it copies that machine's Python base, so it is not a portable artifact.
`--runtime-from` is therefore a *local trial* path, **not** a way to move a runtime between machines.

## Inputs, pinned

| Input | Version / source | Where it is recorded |
|---|---|---|
| Python base | **3.13**, from the interpreter you pass to `install --python` | the manifest's `pythonVersion` |
| Python dependency set | `engine/requirements-win-py313.lock` (96 distributions, `sirchmunk==0.1.0` among them) | the lock file itself; the manifest hashes it |
| `rg` (ripgrep) | 15.1.0, rev `af60c2de9d`, sha256 `decdd499…` | `scripts/pin-tools.py` → `.local/tools/pinned-tools.json` |
| `rga` (ripgrep-all) | **0.10.9 + `tools/rga/cache-path-fallback.patch`** (a modified build) | `scripts/build-rga-patched.py` → `.local/tools/rga-patched/manifest.json` |
| Keep/prune decision | `engine/runtime_min.py` (`KEEP`, `REMOVED_ON_PURPOSE`, `CAPABILITIES`) | in the repository |

Pinned **by hash**, not by name: `engine/staging.py` refuses to start when the tools' hashes drift, which is
deliberate — a PATH-resolved `rg` was once updated under this project and silently invalidated a runtime.

## Steps

1. **Python base and dependencies** *(verified)*
   ```powershell
   py -3.13 -m venv .venv
   .venv\Scripts\python.exe -m pip install -r engine\requirements-win-py313.lock
   ```
   The lock includes the heavy packages the runtime then prunes (`torch`, `transformers`,
   `sentence-transformers`, `modelscope`, `scikit-learn`, `scipy`, …). They are installed because the lock is
   the resolution of `sirchmunk`, and pruned because this build declares the embedding path off.

2. **The pinned tools** *(verified)*
   ```powershell
   .venv\Scripts\python.exe scripts\pin-tools.py            # copies/records rg.exe
   .venv\Scripts\python.exe scripts\install-rga.py         # or: the controlled build below
   ```
   For the **modified** rga the project actually ships into the runtime:
   ```powershell
   .venv\Scripts\python.exe scripts\build-rga-patched.py
   ```
   which clones `phiresky/ripgrep-all` at tag `v0.10.9`, applies `tools/rga/cache-path-fallback.patch`, builds
   with `cargo build --release --locked`, and records the result in `.local/tools/rga-patched/manifest.json`.
   Requires a Rust toolchain and the MSVC build tools; *the toolchain versions were not recorded, so this step
   is **to verify** on a machine that does not already have them.*

3. **Stage the runtime** *(verified)*
   ```powershell
   .venv\Scripts\python.exe scripts\stage-appcontainer-runtime.py
   ```
   It copies the interpreter, `DLLs`, `Library/bin`, `Lib` and a **pruned** `site-packages`, then writes
   `stage-manifest.json` describing every input. It stages into a new directory and swaps it in, so a stale
   package cannot survive inside a runtime that reported success.

4. **Place it in a trial instance** *(verified)*
   ```powershell
   node trial\install.mjs --target <dir> --runtime-from .local\appcontainer-runtime --tools-from .local\tools
   ```
   The installer copies the runtime into `<dir>\data\appcontainer-runtime` and the two tool binaries into
   `<dir>\data\tools` **beside** it — the service copies those into the runtime at startup, and a copy whose
   source is its own destination fails with "file in use".

   `--tools-from` names the directory the pinned binaries live in; `pin-tools.json`'s recorded paths and hashes
   are read from it first, and the hashes are compared. When the flag is omitted the installer searches beside
   the runtime source (`<dir>`, `<dir>\tools`, `<dir>\rga-patched`) — a freshly staged runtime does **not**
   contain `rga.exe`/`rg.exe` itself, so omitting the flag only works when they happen to sit next to it.

5. **Freshness check** *(verified)*
   `doctor` runs `engine/staging.verify(runtime, app)`, which recomputes the input hashes and reports drift as a
   failure with the reason. `start` refuses on a stale runtime; nothing silently runs with a mismatched one.

## Verification of the download/build

- **Hashes**: the runtime's `stage-manifest.json` records sha256 for the lock, the patch, the engine sources and
  both tool binaries. `staging.verify` is the check, and it is the check the service itself runs at startup.
- **Versions**: `rg --version` and `rga --version` are the source of the version strings in
  `THIRD-PARTY-NOTICES.md`; neither file records a version string, so the binaries are the record.
- **Provenance**: the pinned `rg.exe` came from a host path recorded by `scripts/pin-tools.py`; **who built that
  Windows binary is not recorded**, and no licence file accompanies it. Treated as an open attribution item.

## Third-party components this step brings in

| Component | Licence | Consequence |
|---|---|---|
| `rga` (modified) | AGPL-3.0-or-later | modified-version notices with a date, and Corresponding Source for the conveyed object code, attach to anyone who *conveys* a runtime |
| `igraph` | GPL v2 | licence text, notices, Corresponding Source |
| `leidenalg` | GPL-3.0-or-later | same, combined under GPLv3 |
| `kreuzberg` | Elastic-2.0 | pruned today; re-enabling the extractor path puts it back |

**Nothing in this document resolves those obligations, and the package does not claim that keeping the runtime
out settles them.** It records what the step pulls in so the decision can be made deliberately.

## Not verified

- building rga on a machine without a pre-existing Rust + MSVC toolchain;
- producing a runtime on one machine and using it on another (it copies a machine's Python base and is expected
  **not** to be portable);
- OCR or any path that would re-enable `kreuzberg`.
