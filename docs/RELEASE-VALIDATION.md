# Alpha release validation

The release is identified by exact source commit, npm version, archive SHA256 and matching build identity. Install the actual published archive, not a newly repacked approximation. Removing private:true or editing publication metadata changes package bytes and requires renewed audit and acceptance.

Required release gates: authenticated repository/package ownership; original-code Apache-2.0 and third-party attribution; allowlisted public source with no private history or materials; pinned dependency/tool sources; a clean Windows run with zero Python skips; exact archive rebuild; actual archive mismatch rejection, isolated reads, install/upgrade/failure/rollback and installed payload agreement. Incomplete reports, false checks, missing required names or mismatched old/new hashes fail closed. No model calls are used for these checks.

The manual Windows workflow uses a fresh project environment, pinned Actions and locked dependencies, builds rg/rga from pinned upstream source and stages its runtime. Only synthetic acceptance reports are uploaded; binaries, credentials and user data are excluded. Downloaded reports must match the immutable source and archive SHA. Earlier failed or cancelled runs remain evidence, not passing runs.

Windows Server CI is not desktop interaction acceptance, a logon test, a non-admin test, live marketplace reload evidence or a retrieval-quality evaluation. Local tests are recorded separately. Actual npm-download installation must follow publication; marketplace submission follows that verification and the market contribution requirements.

The host/backend/browser compare frozen build identities and validate runtime inputs. These are consistency checks, not signatures. A process must restart after its code changes. Mismatches block business requests and retain health/cancellation; never quietly turn off AppContainer isolation.

npm account publishing may require interactive 2FA. Authentication identity alone does not prove a successful write. Use the alpha dist-tag explicitly and verify the registry's exact version and downloaded archive after publication. Do not weaken account security to publish.
