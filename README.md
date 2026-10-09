# DSH Knowledge — Windows Developer Alpha

Sirchmunk-backed personal knowledge workspace for DSH. Search PDF/Markdown content with an ordinary LLM, read source windows, and inspect answer citations in the sidebar. No embedding model is required.

**Local release preparation only. License confirmation, clean Windows installation and marketplace update acceptance are pending. This snapshot is not publicly released.**

## Requirements and setup

Windows 10/11 with AppContainer, Node 24+, Python 3.13 and DSH 0.2.0-rc.2. A local runtime and your own model provider configuration are required. Read [QUICKSTART](docs/QUICKSTART.md), [runtime preparation](docs/RUNTIME-BUILD.md) and [limitations](docs/LIMITATIONS.md) before installing. There is no bundled runtime or shared API key.

Build the JavaScript with `npm ci --ignore-scripts` and `npm run build`. This command does not create a Python environment or container runtime. Use `node trial/install.mjs --help` for explicit instance setup. Keep app code separate from data.

The workbench provides library setup, per-session selection and workspace defaults, previewed library updates, precise source excerpts, answer citation navigation, service state and per-turn limits. Selecting a folder does not itself grant permission to send content to model providers.

[Privacy](docs/PRIVACY.md) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [Release status](docs/ALPHA-READINESS.md)

## Limits

Retrieval may miss a document or one side of a comparison. Exact source coordinates do not prove a claim. The historical DEEP NO_RESULT_FILE failure remains unresolved. Scanned PDFs/images have no OCR path; formulas/layout require human checks. Answer numbers are plain text; use the answer sources action to navigate. Original-file opening remains unavailable.

## Licensing

The author has not yet confirmed the original-code license. This preparation is marked UNLICENSED/private to prevent accidental npm publication. Third-party sources keep their own terms. See LICENSE-DRAFT.md and THIRD-PARTY-NOTICES.md; the rga patch is governed by its upstream terms.

## Source validation

Generate only synthetic local fixtures with `.venv/Scripts/python.exe scripts/setup-fixtures.py` before the Python suite. Then run `.venv/Scripts/python.exe -m unittest discover -s tests -p "*_test.py"`. Engine/AppContainer tests require an explicitly prepared runtime and tools; report their skips separately.
