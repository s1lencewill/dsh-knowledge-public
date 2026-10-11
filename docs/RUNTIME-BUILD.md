# Build a matching runtime locally

Runtime binaries are not distributed in the npm archive. Build on the Windows machine that will use the runtime; do not copy another machine's Python base. The tested base is Miniconda Python 3.13 with its Library/bin layout. Python.org layout, Linux and macOS are not certified by this Alpha.

Use the public source tag matching the npm version. Prerequisites: Git, Node 24+, Python 3.13, Rust/Cargo for x86_64-pc-windows-msvc, and MSVC C++ Build Tools including the Windows SDK. The script detects vcvarsall with vswhere; absent build tools are an error. It downloads upstream source/dependencies, not documents or credentials.

From a clean checkout, in a shell using the intended Miniconda Python 3.13:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --no-cache-dir -r engine\requirements-win-py313.lock
npm ci --ignore-scripts
npm run build
.venv\Scripts\python.exe scripts\build-clean-tools.py --out .local/runtime-tool-build.json
$env:DSH_KNOWLEDGE_RG = Join-Path $PWD '.local/tools/rg.exe'
$env:DSH_KNOWLEDGE_RGA = Join-Path $PWD '.local/tools/rga-patched/rga.exe'
.venv\Scripts\python.exe scripts\stage-appcontainer-runtime.py
```

The cold build requires empty generated tool/source directories; use a new checkout if they exist, rather than silently reusing artifacts. rg source is pinned to af60c2de9d85e7f3d81c78601669468cf02dabab; rga source, upstream license and raw/applied patch hashes are bound in tools/rga/SOURCE.json. Cargo uses locked source dependencies. The report records toolchain, source revisions and binary hashes. Those binary hashes can differ across toolchains; runtime freshness binds the exact locally staged binaries.

Use the matching runtime and tools together when installing the actual extracted npm archive:

```powershell
node trial\install.mjs --target <instance-directory> --python <source-checkout>\.venv\Scripts\python.exe --runtime-from <source-checkout>\.local\appcontainer-runtime --tools-from <source-checkout>\.local\tools
node trial\doctor.mjs --target <instance-directory>
node trial\start.mjs --target <instance-directory> --service-only
```

The installer places a copy in instance data; source/lock/patch/tools and the copied runtime must match the package. doctor/start fail on missing or stale inputs and do not disable isolation. Parent and container use the intended Python; the staged runtime prunes the embedding-model execution path. Read THIRD-PARTY-NOTICES.md before distributing any runtime you build; this project does not provide binary redistribution artifacts.

The clean Windows Server CI builds tools/runtime afresh and exercises isolation plus actual package installation/read/upgrade/rollback. It is not a Windows 10/11 desktop, login-trigger, non-admin or large-library/performance certification. No model calls are needed to build or run the mechanical checks.
