# Version and dependencies

**Version:** `0.1.0-alpha.10` — Windows developer Alpha, based on Sirchmunk, no embedding-model execution branch, verifiable source coordinates. Original code is Apache-2.0; third-party terms remain separate. Installation CI is not retrieval or desktop interaction certification.

## What is in the package

| Path | What it is | Runtime-required |
|---|---|---|
| `lib/index.js`, `lib/*.js` | the built DSH plugin (host half) | yes |
| `lib/browser.js` | the built browser bundle (source card) | yes |
| `ui/source-card.js` | the hand-written source of the browser bundle | for review |
| `engine/*.py` | the local knowledge service and its worker | yes |
| `engine/requirements.in`, `engine/requirements-win-py313.lock` | the pinned Python set (96 distributions) | yes |
| `cordis.patch.yml` | the DSH bundle patch that inserts the plugin | yes |
| `RELEASE-MANIFEST.json` | exact file list, hashes, dependency metadata and source provenance; integrity, not a signature | yes |
| `trial/*.mjs` | `install` / `doctor` / `start` / `stop` | yes |
| `tools/rga/` | the AGPL patch applied to rga, and its build notes | for the runtime step |
| `samples/` | the self-made synthetic sample library | yes (default library) |
| `docs/QUICKSTART.md`, `docs/LIMITATIONS.md`, `docs/RUNTIME-BUILD.md`, this file | user documentation | yes |
| `THIRD-PARTY-NOTICES.md`, `LICENSE`, `NOTICE`, `VERSION` | licence inventory, original-code license, attribution, and the contents note | yes |

**Not in the package, on purpose:** the container runtime (it carries an AGPL-licensed modified `rga.exe` and
the GPL packages `igraph` and `leidenalg` — see `THIRD-PARTY-NOTICES.md`), any credential, any session log,
any `.local/` development data, the development `.venv`, and the test suite.

## Runtime requirements

| Component | Version | Provided by |
|---|---|---|
| Windows | 10/11 with AppContainer support | the user |
| Node.js | ≥ 24 | the user |
| DSH (`@deepseek-ai/dsh`) | **0.2.0-rc.2** (exact) | the user |
| Python | **3.13** exactly | the user |
| Python packages | the pinned lock (96 distributions, `sirchmunk==0.1.0` among them) | the user, from the lock |
| Model provider | any OpenAI-compatible route DSH is configured for | the user's credential store |

The DSH version is **exact, not a floor**, and that is deliberate: `package.json` declares
`@deepseek-ai/dsh-tools` / `@deepseek-ai/dsh-llm` at `0.2.0-rc.2`, `trial/host-dependencies.mjs` refuses to bind a
host whose installed peer differs (a `link:` into an SDK the build was not compiled against is how a plugin
appears to load and then fails in one call), and DSH runs the same check on its own: 0.1.7 **refused** the
installation (`dsh plugin add` → *incompatible with dsh 0.1.7-rc.2*), while 0.2 evaluates the peer with
`semver.satisfies(runtime, range, { includePrerelease: true })` and reports a **warning plus an exact-version
exemption** (`dsh plugin allow-version`) rather than silently substituting anything. Adapting to a new host
therefore means **changing the declaration and re-running the acceptance**, not granting an exemption
(`dsh plugin allow-version` records a mismatch as accepted risk; that is the wrong answer for a build that has
not been tested against the host).

## Package contents, checked against the runtime

Every file the running system reads is inside the package. There is no dependency on the directory the
package was built in: `engine/service.py` resolves its own project root from its own location, the DSH profile
points at the instance's `app/` directory through a junction that is created at install time, and the
instance's own `data/` holds the runtime and the libraries. `doctor` checks this list by presence:

- protocol text — compiled into `lib/index.js`, so it cannot be "missing" separately;
- browser card — `lib/browser.js`, declared as `exports["./client"].default`;
- service modules — `engine/service.py` plus its siblings;
- configuration template — `cordis.patch.yml`, copied and rewritten per instance;
- build patch — `tools/rga/cache-path-fallback.patch`, needed only by the runtime step.

## Third-party components

See `THIRD-PARTY-NOTICES.md` for the full inventory (component, version, source, licence, evidence path) and
for the obligations that attach to each copyleft component. Summary of the licences among the 96 pinned
Python distributions: 35 MIT, 25 BSD-3-Clause, 15 Apache-2.0, 2 BSD-2-Clause, and one each of MIT-0,
MIT-CMU, ISC, PSF-2.0, MPL-2.0, MPL-2.0 AND MIT, and several dual/multi expressions — plus three components
that need a decision before any redistribution:

| Component | Version | Licence | Where it ends up |
|---|---|---|---|
| `rga` (ripgrep-all), with a local patch | 0.10.9 | **AGPL-3.0-or-later** | container runtime only (not shipped) |
| `igraph` | 1.0.0 | **GPL v2** | container runtime only (not shipped) |
| `leidenalg` | 0.12.0 | **GPL-3.0-or-later** | container runtime only (not shipped) |
| `kreuzberg` | 5.0.0rc3 | **Elastic-2.0 (ELv2, not open source)** | pruned from the runtime today; would enter it if the DocumentExtractor path were re-enabled |

`kreuzberg` is the latent one: its code is pruned now, so it is not conveyed, but re-enabling that path puts
an ELv2 dependency back into the bundle.

## Build and verification

The package ships built artifacts. To rebuild from source: `npm run build` (TypeScript + the browser bundle)
and `npm run verify:all` (the full offline gate). Neither is needed to run the trial.
