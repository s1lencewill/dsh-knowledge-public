# Third-party components, licences and obligations

Inventory produced from installed artifacts on the build machine (Python: `<dist-info>/METADATA`, licence
texts under `<dist-info>/licenses/`; Node: `package.json`; binaries: their pinned build records and
`--version`). Read it as evidence, not as legal advice; it is a factual list with the obligations that attach
to each licence family.

**Original project code is Apache-2.0 (author decision 2026-10-10); see LICENSE and NOTICE. Third-party materials retain their own licenses.**

## What actually ships in this package

The package excludes runtime binaries and dependency code. It includes a source modification of rga, its upstream AGPL license and attribution; these are third-party-governed materials, not Apache-2.0 code. The lock file records dependency versions but is not dependency code.

| Group | Redistributed here? | Evidence |
|---|---|---|
| rga source patch and upstream license | yes in the public candidate; AGPL-3.0-or-later | `tools/rga/cache-path-fallback.patch`, `tools/rga/LICENSE.md`, `tools/rga/SOURCE.json` |
| the plugin's own built JS | yes (it is the project's code) | `lib/index.js`, `lib/browser.js` |
| `@deepseek-ai/dsh-tools`, `@deepseek-ai/dsh-llm` (MIT) | **no** — resolved from the installed DSH host | `package.json` peerDependencies; `lib/index.js` imports |
| `react` | **no** — supplied by the DSH web shell's module table | `lib/browser.js` `require("react")` |
| the 96 pinned Python distributions | **no code**, only the lock file | `engine/requirements-win-py313.lock` |
| the container runtime (`rga.exe`, `rg.exe`, `igraph`, `leidenalg`, `sirchmunk`, …) | **no** — placed by the user, explicitly | `docs/QUICKSTART.md` §3, `trial/install.mjs --runtime-from` |

That boundary is deliberate: it keeps every copyleft obligation out of the distributed artifact.

## The three components that need a decision before any redistribution

| Component | Version | Licence | Why it matters |
|---|---|---|---|
| **rga** (ripgrep-all), **modified** | 0.10.9 + `tools/rga/cache-path-fallback.patch` | **AGPL-3.0-or-later** | the pinned `rga.exe` is a *modified* version, so AGPL §5 (mark modified, date) and §6 (convey Corresponding Source with the object code) attach to anything that conveys it. Today it lives only in the user's own runtime directory, which the package does not build or ship. the local subprocess architecture alone is not a blanket conclusion about AGPL network-use obligations; any hosted or redistributed deployment requires a separate review |
| **igraph** | 1.0.0 | **GPL v2** | real code in the staged runtime. GPL permits redistribution under conditions: licence text, notices retained, Corresponding Source conveyed |
| **leidenalg** | 0.12.0 | **GPL-3.0-or-later** | same, and it combines with `igraph` under GPLv3 |

Latent, and currently pruned out of the runtime:

| **kreuzberg** | 5.0.0rc3 | **Elastic-2.0 (ELv2)** | not open source. ELv2 forbids offering it to third parties as a hosted/managed service and forbids removing its notices. Its code is pruned today (`engine/runtime_min.py` `REMOVED_ON_PURPOSE`), so it is not conveyed — re-enabling the DocumentExtractor path would put it back into the bundle |

## Python dependency set (96 distributions, pinned by the lock)

Licence counts, read per package:

| Count | Licence |
|---|---|
| 35 | MIT |
| 25 | BSD-3-Clause |
| 15 | Apache-2.0 |
| 2 | BSD-2-Clause |
| 1 each | MIT-0 (`cffi`); MIT-CMU (`pillow`); ISC (`shellingham`); PSF-2.0 (`typing_extensions`); MPL-2.0 (`certifi`); MPL-2.0 AND MIT (`tqdm`); Apache-2.0 OR BSD-3-Clause (`cryptography`); Apache-2.0 OR BSD-2-Clause (`packaging`); MIT OR Apache-2.0 (`sniffio`); Apache-2.0 AND CNRI-Python (`regex`); BSD-3-Clause AND ISC (`ijson`); BSD-3-Clause AND Apache-2.0 (`python-dateutil`); BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 (`numpy`); multi-expression (`torch`); **GPL v2 (`igraph`)**; **GPL-3.0-or-later (`leidenalg`)**; **Elastic-2.0 (`kreuzberg`)**; BSD variant unstated (`ply`) |

Notable individual components: `sirchmunk` 0.1.0 **Apache-2.0**; `duckdb` 1.5.5 MIT; `fastapi` 0.141.1 MIT;
`uvicorn` 0.52.4 BSD-3-Clause; `openai` 3.13.0 Apache-2.0; `transformers` 5.17.0 Apache-2.0;
`sentence-transformers` 6.0.1 Apache-2.0; `torch` 2.14.0 Apache-2.0 (with a large bundled third-party tree);
`pypdf` 6.18.1 BSD-3-Clause; `pdfminer.six` 20260107 MIT.

## Binaries

| Component | Version | Licence | Notes |
|---|---|---|---|
| `rga.exe` (pinned build) | 0.10.9, sha256 `8e384360…` | AGPL-3.0-or-later (modified) | built by `scripts/build-rga-patched.py` from tag `v0.10.9` + the local patch |
| `rg.exe` (pinned build) | ripgrep 15.1.0 (rev af60c2de9d), sha256 `decdd499…` | Unlicense OR MIT (upstream) | **provenance of this particular Windows build is not recorded in the repo** and no licence file accompanies it on this machine — an open attribution question |

`rga` shells out to `rg` as a separate process, so ripgrep's permissive terms apply to `rg` alone; the two
binaries are an aggregate, not a combined work.

## Obligations by licence family (what a redistribution would have to carry)

- **MIT / BSD-3-Clause / BSD-2-Clause / MIT-0 / ISC / PSF-2.0 / MIT-CMU** — include the copyright notice and
  the licence text with any binary redistribution. Covers the large majority of the Python set.
- **Apache-2.0 (§4)** — give recipients a copy of the licence, retain any `NOTICE` file, and state significant
  modifications. Affects `sirchmunk`, `openai`, `transformers`, `sentence-transformers`, `modelscope`,
  `huggingface_hub`, `requests` (ships a `NOTICE`), `msgpack`, `parquet`, `safetensors`, `sentencepiece`,
  `tokenizers`, `tzdata`, `hf-xet`, `torch`.
- **MPL-2.0 (§3.2)** — `certifi`, and the MPL half of `tqdm`: make the licence text available and tell
  recipients how to obtain the MPL-covered source. No effect on the rest of a bundle.
- **GPL-2.0 / GPL-3.0** — `igraph`, `leidenalg`: licence texts, notices retained, Corresponding Source
  conveyed with the binaries.
- **AGPL-3.0** — `rga` as shipped in a runtime: licence text, a prominent "modified" notice with the date,
  the whole work licensed under the AGPL, and the Corresponding Source for the conveyed object code (or a
  written offer / equivalent network access).
- **Elastic-2.0** — `kreuzberg`, if ever bundled: keep its notices, and never offer it to third parties as a
  hosted or managed service.

## Open questions (do not guess these)

1. **`ply` 3.11** states only "BSD"; the wheel ships no licence file, so the clause count (2- vs 3-clause)
   cannot be read from the installed artifact. It is not in the runtime's code set today.
2. **`rg.exe` provenance** — the pinned binary was copied from a host path recorded by `scripts/pin-tools.py`;
   who built that Windows binary is not recorded, and no licence file accompanies it locally.
3. **`kreuzberg`'s 725 bundled Rust components** are known only from the SBOM the wheel ships. Aggregating it
   shows no GPL/AGPL/SSPL/BUSL/CC-BY-NC component, but the entries were not each verified against upstream
   texts. Relevant only if that path is re-enabled.
4. **`fontTools` 4.65.0** (MIT) is installed outside the lock, so the PDF-extraction dependency set is not
   fully reproducible from `engine/requirements-win-py313.lock`.
