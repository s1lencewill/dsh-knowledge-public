# DRAFT — licence for this project has NOT been chosen

**Nothing in this file is a decision, and nothing in this repository may be described as publicly releasable
yet.** The author has not chosen a licence, so this file records the options and their consequences rather
than asserting terms on the author's behalf. Replace it with the chosen licence (as `LICENSE`) before any
public release.

**Current distribution status, stated plainly:** this package is for **authorised private trial use only**. It
is not open source and not a public release, and the absence of a `LICENSE` file is the reason rather than an
oversight: nothing here grants a right to redistribute it, and the third-party components it touches carry
their own conditions (`THIRD-PARTY-NOTICES.md`).

## Why a licence is needed at all

The project's own code (everything under `src/`, `engine/`, `ui/`, `trial/`, `scripts/`, `tests/`) currently
carries **no licence at all**. Under default copyright, that means all rights are reserved: nobody may copy,
modify or redistribute it, which also makes the current private trial bundle a permission-only arrangement
between the author and whoever receives it.

## What the project's own code is, and what it is not

- **Its own code:** the DSH plugin (host half and browser half), the local knowledge service, the trial
  tooling, the extractor helpers, the tests and the docs. This is what a licence would cover.
- **Not its own code:** `sirchmunk` (Apache-2.0) and the rest of the pinned Python set, DSH itself (MIT), the
  `rga`/`rg` binaries, and the browser `react` runtime (supplied by the DSH shell, never redistributed here).
  Those keep their own terms; see `THIRD-PARTY-NOTICES.md`.

## Options, with what each implies for this project

| Option | Effect on the project's own code | Compatibility with what it depends on |
|---|---|---|
| **MIT** or **BSD-3-Clause** | permissive; anyone may reuse it, including in closed products | compatible with all permissive dependencies; **does not** remove the copyleft obligations that attach to the container runtime, because those are separate works |
| **Apache-2.0** | permissive, adds an explicit patent grant and a NOTICE mechanism | matches `sirchmunk`, `openai`, `transformers` and most of the Python set; still leaves the runtime's copyleft obligations untouched |
| **GPL-3.0-or-later** | copyleft for the project's own code | would be the simplest posture if the *shipped* product must include the container runtime, because that runtime already contains GPL code (`igraph` GPL-2.0, `leidenalg` GPL-3.0-or-later) |
| **AGPL-3.0-or-later** | copyleft plus the network-use clause | only forced if the modified `rga` were conveyed *and* the combined work were treated as one work; the current design calls `rga` as a separate local subprocess, so this is not forced today |

**The decision that actually matters first** is not which permissive licence to pick — it is whether the
product will ever *convey* the container runtime. Today it does not: the runtime is built or placed by the
user, and the package ships neither `rga.exe` nor `igraph`/`leidenalg`. Keeping that boundary keeps the
obligations at arm's length.

## Suggested default (for the author to accept or reject)

If the intent is the widest reuse with the least friction, **Apache-2.0** is the closest match to the
ecosystem this project sits in (it aligns with `sirchmunk` and with most of the pinned set, and adds a patent
grant that MIT lacks). If the intent is to keep the container runtime inside the distributed product, the
copyleft question has to be answered first, because GPL code is already in that runtime.

## What must happen before any public release

1. Choose the licence and put the real text in this file (and record the choice in `package.json`).
2. Add the required attribution for the permissive dependencies: a `THIRD-PARTY-NOTICES.md` that carries the
   copyright notices and licence texts for the MIT/BSD/Apache-2.0/MPL-2.0 components (the audit lists them).
3. Decide, in writing, about the copyleft components: either keep them out of every distributed artifact (the
   current state), or take on the Corresponding Source obligations for `rga` (AGPL, modified — notices plus
   source plus the patch) and for `igraph`/`leidenalg` (GPL source obligations).
4. Resolve the two open attribution questions in the audit: `ply`'s unstated BSD variant, and the provenance
   of the pinned `rg.exe` build (no licence file accompanies it on this machine).
5. Only then describe anything as a release.
