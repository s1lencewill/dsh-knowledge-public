# Build and update consistency

The browser, host plugin and Python service share a content-derived build ID. It includes the engine sources, compiled host modules, browser source and required child-runtime inputs. Runtime compatibility also requires full staging verification against source and pinned tool binaries.

Release packages fail closed when metadata is missing, when a running host/service differs from the installed package, or when the runtime is stale. Health and cancellation remain available. A cached old page cannot change settings or library scope; refresh after completing the upgrade and restarting the host/backend.

The identity is frozen when a process loads. Replacing files does not upgrade a running process. Metadata is a consistency check, not a signature or proof that a publisher is trustworthy. Source-only development without a release manifest is explicitly outside this release gate.

`npm run build` generates lib/build-identity.json and injects the identity and package registration name into lib/browser.js. Do not copy ui/source-card.js directly as a bundle. Do not replace only one half of the installation.

Required gates: zero-skipped Windows Python tests; actual-package host/backend/runtime mismatch checks; isolated install/read/upgrade/rollback; installed payload checks. Local mocks verify request refusal, not marketplace host reload or real model behavior.
