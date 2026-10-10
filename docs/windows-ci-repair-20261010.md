# Windows CI repair — 2026-10-10

Status: blocked-public-candidate. No publication or daily installation changes.

Run 38055062101 (source 2d843b4) failed: 632 Python tests, 6 failures, 6 errors, zero skips. Fresh dependency installation, cold rg/rga build, runtime stage and symbolic-link probe succeeded. Archive/install steps were skipped and are not accepted.

Confirmed corrections:
- Onboarding preview stores a resolved workspace. Publish/cancel now resolve the supplied workspace before the same owner/session comparison. Foreign workspace and session remain rejected. Original sources and authorization scope are unchanged.
- New CPython 3.13 windows_utils no longer exposes tempfile. Redirect the module-local _winapi pipe creation/opening functions to the LOCAL namespace instead of depending on the name generator. Never mutate the shared _winapi module. Tests cover old/new pipe-name forms and actual asyncio subprocess I/O.
- Two tests compared Windows short-path spelling with resolved long paths. Their expected paths now use resolve(), preserving the exact traversal/PATH assertions.

The forbidden-options pipe timeout is not yet explained. Its complete local module rerun passed 43/43 without changing the timeout or proxy implementation; this does not prove the cloud failure fixed. Keep the original failed run and require a new full CI result.

The container entry changed, so this candidate requires renewed runtime staging. No reuse of the previous freshness claim. No tests are removed or skipped to obtain a passing gate.

Local follow-up: the full suite reproduced the forbidden-options timeout. The test fixture closed pipe handles without joining the serving thread, allowing stale readers to outlive handle ownership. Cleanup now terminates clients, stops and joins each serving thread, then closes the handle; an alive thread is a test failure. Production deadlines and proxy rules are unchanged. Verification remains required.

The first local full run also had missing pinned rg and a stale generated build identity (3 failures, 2 errors, 12 skips total). Those preparation failures are retained, not classified as new cloud failures. Pinning the local tool and rebuilding identity corrected preparation; the local tool reuse is not cold-build evidence.

## Follow-up, 2026-10-11

Correction: the four local service capacity failures were LIBRARY_CONFIG_UNAVAILABLE, not build-identity rejection. Capacity tests mocked libraries() but not the newly explicit library_config() read. They now supply a synthetic empty config in addition to their existing library selection stub; no production gate changed. The full fixture preparation command is also run before the final local suite.

After pipe cleanup correction the second and third local full runs did not reproduce the forbidden-options timeout. This is regression evidence, not proof of the unobserved cloud thread ordering. The original failure remains archived.

Final local regression: Python 636 tests, zero failures/errors, two local symlink skips; Node 369/369. Candidate runtime restaged and staging.verify returned True. These are same-machine checks, not a clean Windows release pass.
