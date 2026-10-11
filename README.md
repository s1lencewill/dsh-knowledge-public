# DSH Knowledge — Windows Developer Alpha

A personal PDF/Markdown knowledge workspace for DSH. Sirchmunk is the primary retrieval engine; it uses an ordinary LLM without an embedding-model execution branch. Read source windows and navigate answer citations in the sidebar.

This is a developer Alpha for Windows. It requires Node 24+, Python 3.13, DSH 0.2.0-rc.2, a locally built AppContainer runtime, and your own model configuration. No runtime binaries or shared API key are bundled. Retrieval may miss documents, conditions or one side of a comparison. Historical DEEP NO_RESULT_FILE failures remain a known limitation. Scanned PDFs/images have no OCR path. Exact quote coordinates do not establish that a claim is supported.

## Install and verify

1. Download the exact npm Alpha archive or the matching GitHub pre-release asset; check SHA256 against the published checksum.
2. Extract it into an independent directory. Prepare Python dependencies and a runtime from the matching public source revision, following [RUNTIME-BUILD](docs/RUNTIME-BUILD.md).
3. Run the instance installer with explicit `--target`, `--python`, `--runtime-from` and matching `--tools-from`. Run `doctor` before `start`; missing/stale runtime is a failure, never a reason to disable isolation.
4. Open the instance's login link and add an existing DSH workspace. This is not yet a knowledge library. In the workbench choose **添加知识库**, preview a folder under that workspace, review issues and provider-consent scope, build, then select it for the session. Workspace defaults are separate.

See [QUICKSTART](docs/QUICKSTART.md) for exact commands, configuration, start/stop, updates and data-preserving uninstall. Source development uses `npm ci --ignore-scripts` and `npm run build`; this does not prepare Python or a runtime. Switching library affects subsequent calls only; source updates require a preview and renewed authorization.

## Upgrades and evidence

Upgrade both frontend and backend from the same audited package. A file replacement does not reload a running process. Compare build identities, restart the matching host/backend, and require runtime staging freshness. Mismatch blocks knowledge dispatch while preserving health/cancellation. Keep a previous app generation and user data for rollback.

The release's CI reports cover a fresh Windows Server runner, cold source/tool/runtime preparation, zero-skip Python checks and actual archive install/read/upgrade/rollback. They do not certify Windows 10/11 desktop interaction, logon triggers, live marketplace updates, ordinary non-admin accounts, or retrieval/answer quality. Never infer those from the installation checks.

Original code is Apache-2.0; third-party terms remain separate, including the AGPL rga source patch. Read [notices](THIRD-PARTY-NOTICES.md), [limitations](docs/LIMITATIONS.md), [privacy](docs/PRIVACY.md), [security](SECURITY.md), and [build consistency](docs/BUILD-CONSISTENCY.md). Do not attach private documents, credentials or raw sessions to public issues.
