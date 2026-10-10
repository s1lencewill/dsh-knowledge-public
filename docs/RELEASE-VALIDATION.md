# Release validation: license, identity, cold Windows and updates

2026-10-10. Status: blocked-public-candidate. No model calls and no publication.

## License and publication identity

Original code is Apache-2.0 (LICENSE and NOTICE). Third-party terms are retained; the rga source patch is AGPL-3.0-or-later, accompanied by its upstream license and source binding. Runtime binaries are not shipped.

Public repository: s1lencewill/dsh-knowledge-public (not yet created). npm: @s1lencewill/dsh-knowledge. The author supplied the account name; that is not permission evidence. npm whoami returned E401, and the Git credential helper returned no usable credential. The unscoped dsh-knowledge registry package already exists at 0.5.1. A 404 for the scoped name is not namespace ownership or a reservation. package.json retains private:true. Do not publish until GitHub create/push rights and npm scope/access rights are authenticated and verified.

## Every one of the twelve Python skips

All twelve checks are release-required. The ten engine checks were skipped because FastEvidenceTests.setUp requires project-local .local/tools/rg.exe and .local/tools/rga-patched/rga.exe. Environment variables alone did not satisfy that precondition. We reproduced 10 skips without those files, and --release rejected them (exit 1). Explicitly placing existing local tools restored 10/10 execution with no skips; this is not a cold build. Full regression: 623 run, zero failures/errors, two skips. Previous reports are retained.

| Test ID | Original reason | Current local result |
|---|---|---|
| `fast_evidence_test.FastEvidenceTests.test_a_double_that_is_not_the_real_engine_is_left_alone` | 缺固定路径 rg/rga | 实际执行通过；工具为本机复用 |
| `fast_evidence_test.FastEvidenceTests.test_a_fabricated_path_never_becomes_a_candidate` | 缺固定路径 rg/rga | 实际执行通过；工具为本机复用 |
| `fast_evidence_test.FastEvidenceTests.test_a_small_file_still_yields_a_marked_parent_window` | 缺固定路径 rg/rga | 实际执行通过；工具为本机复用 |
| `fast_evidence_test.FastEvidenceTests.test_a_true_no_match_is_still_reported_as_no_candidates` | 缺固定路径 rg/rga | 实际执行通过；工具为本机复用 |
| `fast_evidence_test.FastEvidenceTests.test_candidates_come_only_from_files_this_run_read` | 缺固定路径 rg/rga | 实际执行通过；工具为本机复用 |
| `fast_evidence_test.FastEvidenceTests.test_evidence_the_engine_rejects_still_reaches_the_parent_with_a_warning` | 缺固定路径 rg/rga | 实际执行通过；工具为本机复用 |
| `fast_evidence_test.FastEvidenceTests.test_model_prose_is_never_offered_as_a_verbatim_snippet` | 缺固定路径 rg/rga | 实际执行通过；工具为本机复用 |
| `fast_evidence_test.FastEvidenceTests.test_the_cache_decision_does_not_decide_what_the_parent_can_verify` | 缺固定路径 rg/rga | 实际执行通过；工具为本机复用 |
| `fast_evidence_test.FastEvidenceTests.test_the_contract_guard_refuses_a_changed_upstream_shape` | 缺固定路径 rg/rga | 实际执行通过；工具为本机复用 |
| `fast_evidence_test.FastEvidenceTests.test_the_engine_never_claims_acceptance_it_did_not_give` | 缺固定路径 rg/rga | 实际执行通过；工具为本机复用 |
| `reader_test.PathBoundaryTests.test_a_file_replaced_by_a_link_is_refused` | symbolic links are not available to this user | 仍跳过；发布阻塞 |
| `real_data_test.TrialAuthorizationTests.test_a_symlink_is_not_counted_as_an_authorized_file` | symlinks are not permitted on this host | 仍跳过；发布阻塞 |

The two remaining checks cover file replacement by symlink and exclusion of symlinked files from the authorization set. Directory-junction tests do not substitute for these file-symlink cases. On the clean runner execute all of them, or block the release; do not weaken tests or label the skips optional.

Command: `.venv/Scripts/python.exe scripts/verify-python-skips.py --release --out .local/ci/results/python.json`. Exit 0 requires zero failures, errors and skips. The runner reports test IDs/reasons and keeps cleanEnvironmentVerified:false; being green does not itself prove the machine was clean.

## Clean Windows without a local VM

Use the manual .github/workflows/windows-release-validation.yml workflow. GitHub standard Windows runners provide a fresh VM; workflow source: https://docs.github.com/en/actions/reference/runners/github-hosted-runners . The workflow is prepared and syntax-checked only, not run. Private-repository Actions permissions/minutes must be checked before dispatch; no token or provider secret belongs in inputs.

Its fixed, read-only job builds a new Python 3.13 environment from Miniconda, installs the lock without caches, installs Node from the lock, builds rg/rga from pinned source and records their commits/toolchain/hashes, stages the runtime locally, then runs Python with zero skips and Node regressions. It does not copy the developer runtime. The current staging script assumes the Miniconda base layout (Library/bin); Python.org layout is not currently demonstrated and must not be advertised as equivalent.

Supply a 40-character previous source SHA and the reviewed final archive's SHA256. The previous revision must belong to the allowlisted export history, not private development history. The job builds both actual packages, requires the current archive hash to equal the reviewed package, and runs real isolated install/read/upgrade/failure/rollback acceptance. A different rebuilt hash is a failure requiring investigation, not a replacement candidate silently accepted.

Actions are pinned to commits. Workflow is manual only; permissions are contents:read, no publishing and no model keys. Only synthetic result JSON is uploaded; runtime binaries and instance data are excluded. Disk/network/dependency/AppContainer failures and missing symlink privilege remain failures. This automated runner does not demonstrate a normal user's interactive desktop UI, login trigger, market installation, or a standard non-administrator account; those are separate checks.

## Marketplace update gate: still blocked

The scoped candidate now composes a scoped bundle and installation junction, removes the old unscoped bundle during upgrade, and has regression coverage for lookup/idempotence/path escape. This is local profile compatibility, not an actual marketplace installation/update.

The service /health currently exposes protocol=1 and the Sirchmunk version, but no exact running service release-manifest/build identity. A market-updated frontend plus an old live backend cannot be proven consistent from that response. verify-install checks disk hashes/registration and runtime freshness checks engine/lock/patch/tool hashes; neither proves the running service loaded the matching build or that the market synchronized the two app locations.

Before market acceptance, expose the active service build identity (package name/version, source commit and release-manifest digest), compare it with the loaded frontend, and require staging.verify for the runtime inputs. A runtime may be reused if all required engine inputs match; its identity is the staged input manifest, not an invented npm version. Missing/mismatched identity must visibly block knowledge dispatch and offer explicit sync/restart, never quietly disable isolation.

Acceptance must use the real market update entry: same-version install, frontend-only update, backend-only update, old running service, stale runtime, successful synchronized update, failure rollback and user-data preservation. No row is passed by this document. The present final-package install/rollback experiment remains same-machine evidence only.
