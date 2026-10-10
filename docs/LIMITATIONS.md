# Limitations of this trial build

Read this before relying on anything the trial tells you. Each line is a real boundary of the current build,
not a hedge.

## What the answers can and cannot mean

- **Retrieval can miss the file you needed.** Sirchmunk ranks and cuts; a question whose evidence is in a
  document it did not surface gets an answer from what it did surface, or an "insufficient evidence" reply.
  Neither is a statement that your library lacks the material.
- **A located quote is not support.** The card's `locationVerified` says the offsets rebuild that text in that
  file version. It says nothing about whether the sentence supports the conclusion. `support` is always
  `not-assessed`, and it is the Agent's job to reason about it — which is a judgement, not a measurement.
- **PDF extraction has quality limits.** The page map comes from the extractor's own audit. A page is reported
  only when the map vouches for it; a scrambled extraction therefore shows a *defect* or no page, never a
  guessed page. Scanned PDFs without a text layer are not OCR'd by this build.
- **Condition, unit and negation traps are real.** An extraction frequently splits a value from its unit
  across lines ("held at 298" / "K"). A short excerpt can cut exactly there. The cards flag truncation; read on
  before concluding.
- **A document that looks like an instruction is still text.** The sample library contains one on purpose.
  The plugin's protocol says retrieved text is never instruction, and the trial treats it that way.
- **The Agent's behaviour is only partly verified.** Two real questions were run against this build (a known
  title, and a content question). The third check — what the Agent does when the evidence is genuinely
  insufficient — **was not run** and remains unverified. Do not read "it handled two questions well" as
  "it will decline correctly".

## What is not implemented

- **Opening the original file is not implemented.** The button is disabled and says so. The host can only hand
  an existing *directory* to a file manager, and doing that would expose your whole library folder, so this
  build refuses rather than widening access. Open originals yourself.
- **Desktop onboarding is separate from Server CI.** Folder preflight, consent, publication, selection and workspace defaults are implemented; desktop interaction is not certified by the server installation gate. See QUICKSTART §6.

- **No embedding-model branch runs.** The container runtime is built so that torch, transformers,
  sentence-transformers, modelscope and the embedding path are pruned. Knowledge reuse, its evolution and
  embeddings are declared off, and the engine refuses to run if the declaration and the image disagree.
- **No OCR, no image understanding, no cross-library search.** One library is in scope at a time.

## Limits you must account for yourself

- **Three budgets, counted separately — and none of them is the others.** A test window is bounded by three
  different quantities and they are not interchangeable:
  1. **knowledge-tool dispatches per turn** — 6. Calls 1–6 are dispatchable; the 7th is refused locally, without
     an HTTP request or a container spawn. The ceiling is independent of the failure breaker (4 identical failures /
     8 consecutive). It bounds this plugin's five tools only — it is not a host-wide tool limit.
     - Every result reports the accounting as `budget` (`used`/`max`/`remaining`/`countedAs`), and `used` counts
       **attempts**, including attempts the plugin refused, so a refusal spends the turn's number.
     - A budget stop asks for the final answer through the host's own `ToolRunContext.deferContext()`, then calls
       `concludeTurn()`. **`concludeTurn()` alone produces no answer at all**: the host's agent loop returns
       `{kind: 'completed'}` at that step, so the model never gets a step in which to write prose. That was the
       observed defect this behaviour exists for (three consecutive turns with material returned and no text).
     - **The host has no primitive that forbids tool dispatch.** Searched rather than assumed: `dsh-agent-loop`
       exposes `maxParallelToolCalls` and an `agent/request` waterfall carrying provider/model, and nothing that
       narrows a step's tool catalogue or sets a tool choice. So the closing step is **bounded, not enforced**: it is
       granted once per turn, a further call in it is refused locally at zero service cost, and the turn then closes.
     A model that ignores the instruction still produces no prose, and the deterministic summary on the refusal
     card is what the reader gets. The plugin cannot promise a text answer; it can promise that no service work
     happens after the ceiling and that the closing step is granted exactly once.
  2. **Sirchmunk provider requests** — bounded in the service parent (`engine/model_proxy.py`) *before* the proxy
     sends, and persistable across restarts through `DSH_KNOWLEDGE_QUOTA_FILE` / `DSH_KNOWLEDGE_QUOTA_MAX`. The slot
     is reserved, and spent, at the moment of dispatch: a request that then times out, fails or is cancelled has
     already spent it.
  3. **main-Agent provider requests** — bounded in the plugin (`src/modellimit.ts`) on the host's own `llm/stream`
     waterfall. A listener that does not call `next()` costs no provider request, so the refusal is pre-dispatch.
     - **Its scope is the host process, not a session, and that is a real hazard.** The `llm/stream` payload is
       the request; it carries no session and no agent. So a ceiling armed here applies to **every session that
       host serves**, and spending it refuses model requests in all of them — including conversations that have
       nothing to do with whatever window armed it. That happened once, from a ceiling armed in a profile row, and
       the only repair was to edit a ledger file by hand.
     - Therefore it is armed **only** through the process environment, and takes **two deliberate keys**:
       `DSH_KNOWLEDGE_ARM_AGENT_QUOTA=1` plus a ledger path (`DSH_KNOWLEDGE_AGENT_QUOTA_FILE`) or a number
       (`DSH_KNOWLEDGE_MAX_AGENT_REQUESTS`). With only one key the whole budget is inert — it does not read,
       write or refuse anything — because an inherited variable is a thin interlock for a decision whose blast
       radius is every session the host serves. There is deliberately **no plugin-config spelling** of it: a
       profile row is the one thing an operator edits without thinking about scope. Arm it in a **dedicated
       process** (an isolated host), where the window's scope and the process's scope are the same thing.
     - **Off unless armed**, and it says so: `budget.model.enforced: false` means no ceiling is in force, and
       `budget.model.source` (`ledger` | `configured` | `none`) says where the effective number came from.
     - **Cheap to disarm**: the ledger is re-read on every dispatch, so raising `max`, zeroing `used`, or deleting
       the file takes effect on the next request with no host restart.
     - One `knowledge_search` may make the engine issue several provider requests, and the calling Agent spends
       provider requests that this plugin never sees — so the tool-call number is not a model-request number.
     - **What it cannot see:** a retry performed by a listener *beneath* it (`dsh-llm-retry` wraps the adapter)
       does not re-enter this waterfall, so `dispatched` is a **floor** on provider requests, not a ceiling on them.
       A dispatched request whose terminal frame was never observed is counted as `usageUnknown` — never as zero.
  - By contrast, budget 2 (Sirchmunk) is armed by the *service* process, which serves knowledge queries only: its
    scope and the thing it affects are the same, so it can be armed without putting unrelated work at risk.
  - Per-request token usage is **not** parsed by the trial's own accounting; when you see a token number in the
    interface it is the host's own figure, and where it is absent the trial reports it as *unknown*.
- **A read window is not an outbound boundary.** `maxChars` (default 6000, maximum 20000) bounds only the text
  handed back to the calling Agent. It does not bound what Sirchmunk samples and sends to its provider, and it does
  not bound the history the Agent already carries into its own requests. What *is* measurable is recorded where the
  requests actually happen: per job, `modelCalls` (requests started) and `modelSentBytes` (bytes this parent
  forwarded), plus `requestBytes` on every `provider_call_started` diagnostic; and on the host side
  `budget.model.requestChars`, which is a **measurement of the request image this plugin was shown, not a cap**.
  The Agent's carried context cannot be bounded by this plugin at all.
- **Short references are process-local.** A `readRef` lives in the plugin instance: a host restart, a plugin
  reload or eviction of the bounded table (500 entries) makes it resolve to nothing, and it is then reported as
  `KNOWLEDGE_READ_REF_UNKNOWN` — never re-pointed at a file that looks similar. A reference issued for another
  session, workspace or library is refused with `KNOWLEDGE_READ_REF_FOREIGN`. Recovery is always "locate the
  document again", which costs one of the turn's calls and is stated in the refusal.
- **Parameter ranges are not in the tool schema.** The installed SDK's parameter DSL (`ValueSchemaAnnotations`)
  carries `description`, `title`, `default` and `examples` and **no** `minimum`/`maximum`, so a range is prose
  plus a local check. The check is real — an out-of-range value is refused before any request, never clamped —
  but a client reading the schema alone sees only the description.
- **A page number is never inferred.** If a PDF has no map, the card says so and gives line numbers.
- **The result annotation states the posture of *that* job, not a policy.** Every successful query result carries
  a line saying this service does not compose the answer (the calling Agent does, from the excerpts) and a line
  reporting the isolation that job actually ran under — `ENGINE_ISOLATION: AppContainer enforced for this query`,
  or `disabled …` when the service runs with `DSH_KNOWLEDGE_ISOLATION=off`, or `not applied` from a harness entry
  point. It is derived from the code path that ran, so it cannot be right about one deployment and wrong about the
  other. An earlier version asserted a fixed sentence in every result that called answer synthesis and path
  isolation "pending"; both halves were false.
- **Enumeration order is the catalogue's, not a relevance order.** `knowledge_list_documents` lists by document
  id so `offset` means the same thing twice; it is complete coverage of the catalogue, which is not the same as
  "every file in the library" (a file with no catalogue record is not listed) and not a ranking of importance.
- **The catalogue version detects change, it does not prevent it.** A page built from a catalogue that changed
  under a paged read is refused (`status: "catalogue-changed"`) with an instruction to restart; if the caller
  never sends the `catalogueVersion` it was given, that check cannot run and the page says so
  (`CATALOGUE_UNVERIFIED_CONTINUATION`).
- **The per-file ceiling is per library, and it is the only capacity dimension that is.** `maxFileBytes` is
  optional (default 2,000,000 bytes, the value every library already had) and may be declared up to 8,000,000;
  a value that is not a whole number of bytes, is below 1, or is above that ceiling is a **refusal of that
  library** (`LIBRARY_CONFIG_INVALID`, naming the library and the value), never a silent fall back to the
  default. The other two dimensions — 1,000 files and 100,000,000 bytes in total — remain service-wide and are
  not configurable. Exceeding any of them rejects the **whole library**: no partial snapshot is searched, and an
  oversized file is never quietly dropped. A declared ceiling is a promise about memory, which is why there is a
  maximum at all: the snapshot, the read and the catalogue's readability check each hold that file's bytes.
- **Crossing the per-file ceiling does not make a file invisible.** Both the tool and the service *report*
  violations instead of skipping them, so the number of files a corpus has and the number a snapshot will accept
  are two different, separately stated facts (`scripts/build-markdown-library.py scan` prints both).
- **The authorization manifest is re-hashed on every call, and it must match exactly.** A real-data library
  declares its file set with SHA256 values; any added, removed or edited file refuses **every** call for that
  library with `REAL_DATA_TRIAL_FILES_CHANGED` until the manifest is explicitly rebuilt. That cost is real and
  measurable: on the 608-file, 17.9 MB wiki it is roughly 0.5 s per call, paid before any search starts. It was
  deliberately **not** cached or weakened, because the gate's whole value is that it compares the bytes now.
- **A library must declare its own catalogue once there are two of them.** With one library configured, the
  older process-wide `DSH_KNOWLEDGE_CATALOG` still answers, unattributed, exactly as before. With two or more,
  a library that declares no `catalogue` is **refused** rather than served from the process-wide file — because
  that file would answer with whichever library asked first, and the other library would be handed titles and
  file names belonging to material it does not contain. A catalogue must also carry the `libraryId` and normalised
  root identity it was built for; a mismatch, a missing file and a half-written binding are all refusals with no
  fallback. `scripts/build-markdown-library.py bind` is the migration step for a catalogue that predates this.
- **Publishing a catalogue and its manifest is two files, not one transaction.** They are rendered and re-parsed
  before either is replaced, the previous pair is copied aside, and the catalogue is renamed **first** so the
  moment between the two renames is a blanket refusal from the authorization gate rather than a listing whose
  entries cannot be read. The residual window is the time between two `os.replace` calls; it is not zero, and it
  is stated here rather than described as an atomic switch.
- **A Markdown note's identity is `undetermined`, on purpose.** The builder records the note's own front-matter
  title, and says in `verificationDetail` that nothing external corroborates it. Metadata in a note is *data the
  builder read*, not a claim the service verified, so a wiki catalogue can never show `verified` identity.
- **A wiki's images are not searchable.** The engine's extension filter does not include image types and no OCR is
  performed, so image content is excluded rather than indexed — the counts of what was excluded are reported, and
  no claim is made that text inside an image can be found.
- **Enumerating a large library does not fit in one turn.** The per-turn ceiling is 6 calls and one page holds at
  most 50 entries, so a 608-document catalogue needs 13 calls — three turns at least — to list completely, and
  `find_documents` (10 candidates per call) is not an enumeration at all. The measured traversal is reported by
  `scripts/verify-library.py`, and the intended answer for "summarise this whole library" is that the coverage is
  partial and says so.

## The backend's own lifecycle

- **The backend is held by a scheduled task, not by whoever started it.** `trial/service-task.mjs` registers one
  per-user logon task whose action is `supervise`; the supervisor is the task's *own* long-lived process, so
  there is no "launcher exited, task complete, tree torn down" gap. Verified with the synthetic acceptance
  (`scripts/verify-lifecycle.mjs`): the supervisor's parent is the task engine, not the shell that asked for the
  start, and the service stays healthy across separate tool calls.
- **A watchdog brings the supervisor itself back — at logon, not on a timer.** A second, short-lived per-user
  task (`…-watchdog`) runs `revive`: it starts the supervisor only when **no stop was requested** and **no
  supervisor is alive** (identity-checked, not "the lock file exists"). Measured on the live instance: after a
  hard kill of the supervisor the backend was back in **53 s**; after an operator `stop` no supervisor returned
  within 180 s and the log recorded `REVIVE_SKIPPED: a stop was requested`. The watchdog asks the marker's
  question without consuming it, which is what keeps `stop` from turning into `disable`.
- **The watchdog no longer repeats, because a repetition shows a console window every time.** It was registered
  with a two-minute repetition, and on 2026-09-24 the operator reported the machine "kept popping up cmd
  windows" (measured: `LastRunTime 19:05:05`, `NextRunTime 19:07:07`). A scheduled task with an *interactive*
  principal running a **console** program allocates a console window on every run, so a two-minute watchdog is a
  window every two minutes, forever, on the desktop. The default is now a **logon trigger** - the moment the
  supervisor itself starts, where the check costs nothing - and `--watchdog-minutes N` re-enables the repetition
  for anyone who wants the two-minute revival and accepts the windows. `status` reports which mode is
  registered (read back from the task, not from the flags). The cost of the default is stated plainly: a
  supervisor killed between logons is picked up at the next logon or by an explicit `start` - the same cost the
  manual `trial/start.mjs` path always had.
- **A console-program task has a window whichever authority starts it.** That is a property of Windows, not of
  this design: an interactive task running `node.exe` gets a console. Removing the repetition removes the
  *repeated* windows; the single window at each start (logon, or an explicit `start`) remains. Suppressing it
  would mean running the backend non-interactively (a task-principal change with its own consequences), not a
  flag on this one.
- **The logon trigger itself is configured but not verified.** `Start-ScheduledTask` runs the action under the
  scheduler, which is what every check uses; only an actual logon exercises the trigger. Treat "starts at logon"
  as configured-and-pending, not as proven, until the next logon is observed.
- **`stop` is a stop, `disable` is an intention.** `stop` writes a marker, ends the supervisor and the service by
  identity, and leaves the tasks enabled: the backend comes back at the next logon or the next explicit `start`.
  `disable` turns autostart off **including the watchdog** (leaving it on would undo the operator's one word).
  `uninstall` removes both tasks and keeps `app/` and `data/`.
- **A hard task stop cannot be caught.** Task Scheduler ends the task's whole tree, so neither the supervisor nor
  the service runs a shutdown path; `stop` therefore re-verifies the service pid and the port itself, and writes
  `STOP_COMPLETE` with what it observed.
- **Only the stop marker stops the loop — a signal does not.** A Windows console control event reaching the
  task's console group (a console window closing somewhere on the desktop) once made the supervisor read the
  service's `0xC000013A` (STATUS_CONTROL_C_EXIT) exit as an intended stop: it wrote `state: stopped`, exited 0,
  and the backend stayed down with nothing to bring it back — the same class of silent disappearance this
  lifecycle exists to remove. A signal is now recorded as `SUPERVISOR_SIGNAL` and the service is restarted
  through the ordinary bounded-backoff path, and the service is launched in its **own console group**
  (`detached` + `windowsHide`) so such an event does not reach it. `service-task stop` is the only way to stop
  the backend.
- **A supervisor that reaches its own restart threshold disables its task.** The task also carries a bounded
  crash-retry policy (`RestartCount 3`, every 2 minutes) because the supervisor cannot watch itself — but on this
  machine Windows did **not** act on it for a process killed from outside (registered `RestartCount=3`/`PT2M`,
  task ended with a non-zero result, no retry within 300 s), which is why the watchdog exists. Because that
  scheduler policy cannot tell a deliberate end from an accident, the deliberate end is made explicit: a
  threshold stop turns autostart off, `status` says so, and `enable` (or `install`) turns it back on.
- **The supervisor does not supervise the DSH host.** Nothing in the lifecycle scripts starts, stops or restarts
  the host on 3080, and no command accepts it as a target.
- **A venv `python.exe` is a redirector stub.** The process that actually binds the service port is a *child* of
  the pid the supervisor records. Port ownership is therefore checked against the recorded pid **and its
  descendants**; a port held by anything else is reported as a conflict and never taken over.

## The read refusal contract

- **Two codes name a bound; the rest do not.** `READ_RANGE_INVALID` and `READ_REQUEST_INVALID` carry the
  structured facts (`reason`, `requestedStartLine`, `fileLines`, `suggestedAction`, `correlationId`), and the
  plugin maps them to `KNOWLEDGE_READ_RANGE_INVALID` / `KNOWLEDGE_READ_REQUEST_INVALID`. Every other refusal
  carries no facts at all, so an error can never be used to measure a file the caller was not allowed to open.
- **`fileLines` is a fact about an authorised file.** It is computed after the identifier, workspace, library,
  path, size and version checks, and a refusal that happens before any of them reports `null`.
- **The suggested action is a fixed token, not a line number.** The message names the bound the file imposes
  ("it has 12 line(s), so startLine must be at most 12") and never echoes the value that was sent; the plugin
  does not silently clamp an out-of-range request to the end of the file.
- **One correction, not a loop.** The protocol asks the model to retry *once* with a corrected value inside the
  bound, and that retry is an ordinary tool call that spends the turn's budget. A second refusal on the same file
  means answering from what is already held.
- **An older plugin still sees a recognised failure.** The response keeps `detail` as `"CODE: sentence"`, so an
  older caller folds these into its generic refusal rather than losing them; the structured half is additive.

## References: two kinds, and what each is for

- **A document reference and a continuation reference are different objects.** `readRef`
  (`readRefKind: "document"`) identifies one file and its version and may be combined with `startLine` to jump
  inside it. `nextRef` (`nextRefKind: "continuation"`) is bound to the position its window stopped at and must be
  sent alone. Every result that carries any reference also carries the kind fields and a fixed `referenceGuide`
  sentence saying the same thing.
- **Combining a continuation reference with `startLine` is refused locally** (`KNOWLEDGE_REF_KIND_CONFLICT`), with
  no request sent. The recorded session shows why: three calls asked for lines 200/335/105 through a `nextRef` and
  were silently given 88/177/229, each with a `READ_CURSOR_PRECEDENCE` warning the model had to read, interpret
  and recover from inside the turn's budget.
- **The refusal offers the document reference for the same file when it still holds one**, and says so plainly
  when it does not (`suggestedAction: DROP_START_LINE`) instead of naming a reference that has been evicted. The
  pairing is process-local and forgotten with the references themselves.
- **The long spellings are unchanged.** `fileHandle`/`cursor` still work exactly as before, including the
  service's own "the cursor wins and `READ_CURSOR_PRECEDENCE` is reported" rule - that path is the legacy one and
  is deliberately not given the plugin's stricter check.
- **A reference is minted only for a value shaped like a service token** (`krh1…`/`krc1…`, two signed segments).
  A missing or malformed value is left exactly as the service sent it and gets no pointer: handing out a
  reference that resolves to a token the service will reject would make the plugin the author of the copy error
  this module exists to prevent. In the model-facing result, a long token that *was* replaced is removed; the
  service protocol itself is untouched, so any other client still receives `fileHandle`/`nextCursor`.

## How a window ended, and how a card says it

- **A truncated window is one with more file after it.** `EXCERPT_TRUNCATED` (and the card's `truncated: true`)
  means the window stopped before a sentence boundary, or at a line/character limit, while the file continues.
- **The end of the extracted text is not a truncation.** `EXCERPT_INCOMPLETE_AT_EOF` says the window reaches the
  end of the file and the sentence-boundary check found no ending. There is no next window, nothing advises
  reading on, and the card reports `truncated: false` **with the notice still rendered** - the incompleteness is
  the text's, and hiding it would be worse than reporting it under the right name.
- **The boundary check is mechanical and ASCII-only.** It looks for `.`/`!`/`?` at the end and does not look past
  trailing markup, so a bold marker (`!**`), a table cell or a full-width CJK stop (`。`) all read the same to it.
  A `true` from it is not a claim that the excerpt carries the units, conditions or the answer; a `false` is not a
  claim that the text is semantically unfinished. This is why the warning **names the rule** instead of asserting
  that the text is incomplete.
- **The wording never names a field the plugin renames.** The service's own spelling is `nextCursor`; the
  model-facing result exposes it as `nextRef`. Warnings therefore say "the continuation this response returns"
  rather than either name, so the advice cannot point at a field that is not there.

## What the counters do and do not measure

- **`budget.model` is process-wide, not per-session.** `llm/stream` is a host-process waterfall whose payload
  carries no session identity, so the plugin keeps **one** counter per host process: it counts every model request
  that passed through the hook since that process started, in **every** session and from every plugin that uses
  the hook. A value that starts in the hundreds does not mean the current conversation made hundreds of calls, and
  it cannot be attributed to one knowledge call. The scope is stated on the field itself
  (`countedAs`), and an attempt whose end was never observed is reported as `usageUnknown`, never as zero.
- **There is no per-tool duration and no host-scheduling timing.** Tool results carry no `elapsedMs`; the only
  timestamped budgets are the query jobs' own `JOB_BUDGET` lines in the service log, which do not cover the local
  routes (`/reads`, `/documents`, `/catalogue`). `budget.model.requestChars` is currently always `null`.
  Adding any of these is a separate piece of work; the trial does not infer "this was all reasoning time" from a
  total duration, because nothing in the build measures that distinction.

## Platform and packaging scope

- **Verified on this machine only.** The install acceptance was run in a fresh directory with a clean DSH home
  on the development machine: *本机独立目录安装验收*. It is **not** a clean Windows VM, not a second machine,
  and not a cross-platform verification.
- **The first run needs one click in DSH's onboarding**: an instance has its own DSH home, so it has no
  workspace yet, and the installer deliberately does not pre-write DSH's internal workspace store. The click goes
  to the trial's pinned `browse` interaction — an in-page directory dialog. DSH's own default in this situation
  is the `native` interaction: a renderless browser half plus an **OS folder chooser that opens on the host's
  desktop**, which an operator who only sees the page cannot use (and which produces no browser event and no
  console error when it is missed). `--picker native` or `--picker auto` restore that behaviour; see QUICKSTART §5.
- **A command's result is not surfaced on DSH's empty start screen** (host-side presentation, recorded here
  because the plugin has no UI channel to fix it from its side). Typing `/knowledge-use <id>` before a
  conversation exists does run the handler and the host does record it — one acceptance run left three
  `command/run` + `command/done` pairs with distinct sequence numbers in the session log — but nothing appears on
  the empty screen, and the operator only meets the recorded outcomes after a conversation exists, which reads as
  "the command was dropped". The handler is a value-returning registration (`{kind, text}`); the minimal host-side
  fix is to render `command/done` outcomes on the empty surface too, or to refuse argument-bearing commands until
  a session exists. Until then, QUICKSTART §7 gives the order that works.
- **The end-to-end card flow was not re-verified inside the installed instance.** The installer, doctor,
  start/stop and the plugin composition were verified there (the plugin row composes into the instance's own
  boot graph), but the locate → read → source-card path was not driven in that instance, because the fresh
  home stops at the workspace step above. That path is verified in the development profile
  (`docs/m0-host-integration.md`, `docs/m0-host-failure-cards.md`), not in the installed copy.
- **The host still has to provide:** Windows 10/11 with AppContainer support, Node ≥ 24, DSH **0.2.0-rc.2**
  (exact: the SDK peers are declared at that version, DSH checks the peer itself — 0.1.7 rejected a mismatch
  outright, 0.2 warns and offers an exact-version exemption — and `trial/host-dependencies.mjs` refuses to bind
  an SDK the build was not compiled against), Python 3.13, and a reachable model provider.
- **A profile rewrite can unregister the plugin, and nothing in the plugin can prevent that.** DSH 0.1.7
  composes a profile from `dsh.profile.bundles` in the profile's own `package.json`; the 0.1.7 upgrade rewrote
  the daily profile and the plugin simply vanished from the composed tree (198 entries, no `knowledge`), which
  looks identical to a healthy install until somebody asks a knowledge question. Registration is now the 0.1.7
  shape (bundle + `link:` dependency + a `- id: knowledge` config row) and `doctor` has a
  `plugin composed into the profile` check that fails loudly when any of the three is missing - but the check
  only runs when somebody runs `doctor`, so an upgrade that rewrites the profile is still something to verify
  after the fact.
- **The container runtime is not in the package** (AGPL/GPL payload — see THIRD-PARTY-NOTICES.md). Placing it
  is an explicit step, and a missing runtime is a hard failure, never a silent downgrade of isolation.
- **The runtime step is machine-specific**: it copies a runtime staged on the machine that produced it. A
  different machine needs `scripts/stage-appcontainer-runtime.py` run there first, from the pinned lock, the
  patch and the pinned tool binaries.

## Not proven by this build

- Retrieval quality in general, and anything about real literature.
- Cross-machine, cross-platform or clean-VM installation.
- Protocol conformance by the Agent as a whole.
- The container runtime's own isolation guarantees beyond what the service reports as `isolationActive`.
- Anything about licenses: this repository has **no chosen licence yet**, and the files here are drafts for
  the author to decide on (see `LICENSE-DRAFT.md` and `THIRD-PARTY-NOTICES.md`). The package is for authorised
  private trial use only; nothing here grants a right to redistribute it.

Developer Alpha: Windows only; Sirchmunk primary engine; no embedding-model branch. Retrieval coverage gaps and historical DEEP failures remain unresolved by installation tests. Runtime binaries are built locally, not bundled.
