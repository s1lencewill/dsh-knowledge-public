# Private candidate: packaging and acceptance

Version: **0.1.0-alpha.3**. This is preparation for a private Beta, not an accepted Beta or public release.

## Actual archive audit

`node scripts/package-release.mjs pack --out <new-directory>` builds from a clean working tree. A development
candidate requires explicit `--allow-dirty`; its manifest records that fact. Build first with `npm run build`.
The command inspects the actual npm archive, creates a full file/hash/dependency manifest, repacks it and
checks the final archive. `SHA256SUMS` identifies its exact bytes. Audit again with:

```powershell
node scripts/package-release.mjs audit <candidate.tgz>
```

The package excludes the AppContainer runtime, credentials, sessions, development environments and Python
bytecode. `RELEASE-MANIFEST.json` covers every payload file except itself. It is an integrity inventory,
**not a signature or proof of publisher identity**. Pattern scanning is a bounded check, not a guarantee
that every possible secret has been detected. SDK dependencies and the user-provided runtime are external.
The installer validates an incoming manifest before staging and validates the copied payload before swapping.
Installed SDK junctions are excluded from that payload inventory and checked by the host-dependency verifier.

## Interaction prototype

Source cards offer **在右栏查看来源**. The official `sidebarRightTabs` registry and `sidebar.right.pane.tab`
slot hold a session-scoped source panel. The panel displays only a source explicitly clicked in that session;
it does not send HTTP, launch a model, read more text, hold service credentials or insert text into the chat.
List cards similarly display the clicked list result; this is **a past result, not current service health**.

The snapshot is memory-only, bounded to 100 sessions. After refresh or plugin reload, click a persisted card
again. Unload unregisters the tab and discards snapshots; stale callbacks cannot reopen it. A hidden session's
callback cannot navigate the currently mounted session. Without the optional rightbar services the existing
cards remain usable. This is a card-to-panel prototype. Inline answer citation linking, library editing,
import controls, current backend monitoring and original-file opening are outside this implementation.

### Page acceptance, still required

Use an isolated DSH 0.2.0-rc.2 profile, a synthetic library and local stubs. Keep the user's daily host separate.

1. List libraries, expand the tool group and click the library-panel button. Check the past-result label.
2. Locate/read a synthetic document, expand its card and click the source-panel button. Verify title,
   sourceVersion, line range, excerpt and trusted page-map page range agree with the clicked card.
3. Change to another conversation. The first conversation's selected text must not appear there. Click a
   different source in the second conversation, then switch back and verify each session's own selection.
4. Record service/model request counters before and after panel clicks; they must not increase.
5. Refresh. The panel has no retained excerpt; re-click the persisted card and verify the same source returns.
6. Unload/reload the plugin through an isolated profile; no duplicate tab/slot/tool and no surviving buttons.
7. Check both normal and narrow layouts, keyboard focus and long excerpts. Capture screenshots and console errors.

Node component/lifecycle tests cover mechanics; they do **not** replace this real-page acceptance.

## Clean Windows acceptance

Use a disposable Windows 10/11 VM or another clean Windows machine. A new directory on the developer's
machine is not a clean environment. Record OS/architecture, Node >=24, DSH **0.2.0-rc.2**, Python **3.13**,
and how each prerequisite was supplied. Do not reference the developer checkout, venv or runtime.
Install the Python lock from the archive into a target-local environment. Supply a runtime built/validated
on the target using `RUNTIME-BUILD.md`; do not assume the developer's staged runtime is portable.

The private kit contains the candidate, a prior in-round alpha.2 archive, the standalone acceptance runner
and its offline archive reader. It intentionally contains no runtime or credentials. From the kit root:

```powershell
node scripts/accept-release.mjs --archive dsh-knowledge-0.1.0-alpha.3.tgz `
  --previous-archive dsh-knowledge-0.1.0-alpha.2.tgz `
  --work C:\test\kb-acceptance --python C:\test\py313\python.exe `
  --runtime-from C:\test\runtime --tools-from C:\test\tools
```

The work directory must not exist. The runner uses free local ports and retains logs/reports/rollback copies;
it performs no real-model search and registers no scheduled task. It checks damaged upgrade refusal,
program/config rollback, unchanged user settings/library/token, explicit missing-runtime refusal,
isolation-required health, catalogue/read/version/offset flow and owned-service stop. Configuration version
and update timestamp may change on successful upgrade; user settings must not. The prior archive is a
pre-panel candidate from this round, not evidence of every historical upgrade path.

The runner always labels its automated report `same-machine-independent-directory`. A reviewer must provide
separate clean-machine provenance and page evidence; there is no flag that can turn a local run into a
verified clean-Windows result. Complete first-run workspace onboarding through the real DSH page, run the
page checklist above, then verify the rolled-back and upgraded hosts both load their expected tools. Validate
logon/autostart only if explicitly enabled for that disposable instance. No test should alter a daily host.

## Beta exit gates

- Exact archive/file manifest and repeat-build archive identity.
- Clean-checkout source provenance and full local checks on that exact candidate.
- Real DSH page acceptance, two-conversation isolation and no extra requests.
- Clean Windows prerequisites, first-run onboarding, install/upgrade/rollback/start/stop.
- Explicit support limits and private corpus/provider authorization for any later real-model window.

Until these pass, the kit says **blocked-candidate**, not Beta accepted. Public release additionally needs an
the confirmed Apache-2.0 original-code license, resolved third-party/binary provenance and completed publication gates.
Frozen historical tags are never moved to represent this candidate.