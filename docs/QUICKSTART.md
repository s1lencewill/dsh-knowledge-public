# QUICKSTART — Windows personal knowledge-base Q&A (trial)

**What this is.** *Windows 个人知识库辅助问答试用版，基于 Sirchmunk，无嵌入模型运行分支，提供可核验出处。*
A DSH plugin plus a local Python service that answers questions from **your own** PDF/Markdown library and
shows where each quoted sentence came from. It is a trial build, not a released product.

**What it is not.** It is not a search engine, not a literature reviewer, and not a claim that an answer is
correct. See [LIMITATIONS.md](LIMITATIONS.md) — read that before you rely on anything it says.

---

## 1. Prerequisites (you provide these; the installer downloads nothing)

| Requirement | Why | How to check |
|---|---|---|
| **Windows 10/11** | the isolation path uses the Windows AppContainer | — |
| **Node.js ≥ 24** | runs DSH, the plugin and the install/doctor/start/stop scripts | `node --version` |
| **DSH 0.2.0-rc.2** (`@deepseek-ai/dsh`) | the host that loads the plugin; the version is exact, because the plugin declares its SDK peers at that version and DSH checks the peer itself (0.1.7 refused a mismatch at install time; 0.2 warns and requires an explicit `allow-version` exemption) | `dsh --version` |
| **Miniconda Python 3.13** | tested base layout for the knowledge service and runtime | `python -c "import sys;print(sys.version)"` |
| **The pinned Python packages** | the service's imports | see step 2 |
| **A model provider** | answers your questions; the engine also uses it for retrieval | a DeepSeek API key in DSH's credential store |
| **The container runtime** | the service runs the engine with `isolation=required` and refuses to start without it | `doctor` tells you |

The container runtime is **not** in this package. It contains an AGPL-licensed modified `rga.exe` and two GPL
packages, so redistributing it has conditions this package does not take on — see
[THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md). The installer takes a runtime you name, or `doctor`
reports it missing. Nothing here silently disables isolation instead.

## 2. Prepare Python and the matching runtime

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r engine\requirements-win-py313.lock
```

Use the intended Miniconda Python 3.13 shell and follow [RUNTIME-BUILD](RUNTIME-BUILD.md) from the matching public source tag; the npm archive does not contain build scripts or a runtime.

The lock file pins all 96 distributions, including `sirchmunk==0.1.0`. If your Python is not 3.13 the service
will refuse to start; `doctor` says so explicitly rather than running anyway.

## 3. Install the trial instance

```powershell
node trial\install.mjs --target "$env:LOCALAPPDATA\dsh-knowledge-trial" `
    --python "$PWD\.venv\Scripts\python.exe"
```

It prints every path it will create and touch, then:

| Directory | Contents | Survives an upgrade? |
|---|---|---|
| `app\` | the program (plugin, service, browser card, docs) | replaced |
| `data\` | your libraries, catalogue, service token, logs, pid files, container runtime | **kept** |
| `data\dsh-home\` | a private DSH home with one profile, `kn-trial-<8 hex of the instance path>` (per instance, so two instances never share a session log) | kept |

It never touches your existing DSH profiles, never asks for or writes a provider credential, and never
downloads anything. Running it again refreshes `app\` and leaves your data alone.

If you have a container runtime to place:

```powershell
node trial\install.mjs --target "$env:LOCALAPPDATA\dsh-knowledge-trial" `
    --runtime-from <dir> --tools-from <dir-with-rga-and-rg>
```

`--runtime-from` copies the runtime; `--tools-from` points at the directory holding the pinned `rga.exe` and
`rg.exe` (the one `scripts\pin-tools.py` wrote a `pinned-tools.json` into). Without `--tools-from` the installer
searches beside the runtime source, and `doctor` reports the two binaries as missing if it does not find them —
the service will not start without them. `docs/RUNTIME-BUILD.md` says how the runtime and the tools are made.

## 4. Configure the model

The instance points at **DSH's own credential store**, exactly like your normal DSH profile:

- `data\dsh-home\` starts empty, so its credential store is empty too. Copy the entry from your own
  `%USERPROFILE%\.dsh\.credentials.yaml`, or run DSH once with this home and add the key through the Models
  page. `doctor` does not check for it (that would mean reading a credential), and `start` does not need it —
  the first model call will fail with a clear error if it is missing.
- To point the instance at a different provider or a local stub, re-run `install` with
  `--model-base-url`, `--model-api-key-env` and `--model-provider`. The trial never forwards to a provider it
  was not configured for. **`--model-base-url` is the exception you usually want to leave alone:** without it the
  profile writes no `baseURL` at all and DSH's own adapter default applies (0.2's `llm-deepseek` defaults to
  `https://api.deepseek.com/anthropic`), whereas a value written here replaces that default — this installer's
  old default (`https://api.deepseek.com`) did exactly that and pointed the adapter at a 404.

## 5. Start and stop

```powershell
node trial\doctor.mjs --target "$env:LOCALAPPDATA\dsh-knowledge-trial"   # pass/fail/unverified, no model call
node trial\start.mjs  --target "$env:LOCALAPPDATA\dsh-knowledge-trial"   # verify → launch → wait for health
node trial\stop.mjs   --target "$env:LOCALAPPDATA\dsh-knowledge-trial"   # verified kill + residual report
```

**First run needs one click.** The instance has its own DSH home, so it has no workspace yet: the page opens on
"选择一个工作区开始". Click **添加工作区…** and an **in-page "Select Workspace Directory" dialog** opens: navigate
with the folder list or type an absolute path into its path field, then press **Open**. Paths with spaces and
non-ASCII characters work; a path that does not exist, a path that is a file, and closing the dialog without
picking all get their own answer instead of a silent no-op, and adding the same directory twice re-selects the
existing workspace rather than creating a second one. The choice survives a refresh. After that one step the
instance remembers it.

That dialog is the `browse` interaction, which this trial's profile pins by default because it is the one that
works for every operator — including one who only sees the page (a remote, headless or automated browser). The
alternative is DSH's OS folder chooser, which opens on the **host's desktop**: re-run `install` with
`--picker native` for it, or `--picker auto` to leave DSH's own per-boot choice alone. The installer deliberately
does **not** pre-write DSH's workspace store — that store is DSH's own internal format, and guessing at it would
be a way to hand you a broken instance.

`start` refuses to launch if the runtime, the Python dependencies, the token or a port is wrong, and it only
reports the instance as usable **after** `/health` answers and reports `isolationActive: true`. Logs are
appended to `data\logs\service.log` and `data\logs\host.log`.

`stop` stops the pids this instance recorded (after checking that each live process still names this
instance, so a recycled pid is never killed), then **sweeps for any other process whose own command line names
this instance** — that is how a helper left behind by an earlier failed start is found — and finally reports
what is left: ports, pid files, runtime work directories. A kill that reports success is not treated as a
clean stop; the port checks decide.

### Attaching the backend to a DSH host that is already running

You do not have to replace your own DSH to use this: the plugin can be attached to the host you already run, with
no restart, and the backend started on its own. Four steps, all reversible.

1. **Install the instance** somewhere of its own (program in `app/`, user data in `data/`):
   `node trial\install.mjs --target <dir> --python <python.exe> --service-port <port> [--runtime-from <dir> --tools-from <dir>]`
2. **Point your profile at it.** In your profile directory (`%USERPROFILE%\.dsh\profiles\<name>`), create
   `node_modules\dsh-knowledge` as a junction to `<dir>\app`, and add a row to the profile's `cordis.patch.yml`:
   ```yaml
   - insert:
       - id: knowledge
         name: dsh-knowledge
         config:
           url: http://127.0.0.1:<port>
           tokenFile: <dir>\data\service-token.txt
   ```
   Back the patch file up first. `patchReload: live` makes the profile reload the row without restarting DSH; the
   credential is named by path and never written into the configuration file.
3. **Start only the backend**: `node trial\start.mjs --target <dir> --service-only [--catalog <identity.json>]`.
   It runs with `isolation=required` and refuses to start if the runtime or the token is missing.
4. **Stop it again** with `node trial\stop.mjs --target <dir>`; to detach from the host, remove the patch row and
   the junction (that is the whole rollback — nothing in your profile is rewritten, and your other plugins,
   workspaces, sessions and model settings are untouched).

### Keep the backend alive without keeping a shell open (recommended)

`start` launches the service as a child of whatever ran it, so closing that shell (or ending the tool session
that started it) eventually takes the backend with it — and the next session's questions come back
`KNOWLEDGE_SERVICE_UNREACHABLE`. `trial\service-task.mjs` fixes that by handing the backend to a **per-user
scheduled task** whose action is the supervisor itself, so the backend's life does not depend on yours:

```powershell
# 1. register the logon task (per user, no elevation, no password, nothing admin-level)
node C:\path\to\instance\app\trial\service-task.mjs install --target C:\path\to\instance

# 2. run it now; the service starts under the task, not under this shell
node ...\service-task.mjs start  --target C:\path\to\instance
node ...\service-task.mjs status --target C:\path\to\instance

# 3. the three different intentions, kept different on purpose
node ...\service-task.mjs stop    --target C:\path\to\instance   # stop now; the task stays enabled
node ...\service-task.mjs disable --target C:\path\to\instance   # do not start at the next logon (and stop now)
node ...\service-task.mjs uninstall --target C:\path\to\instance # stop, remove the task; app\ and data\ are kept
```

`status` prints the task state, the supervisor pid and its state file, the service pid, the **actual port
owner** (a venv `python.exe` is a redirector stub, so the process that binds the port is a child of the one
recorded), `/health` with `isolation`, and each declared workspace's libraries:

```
  task        dsh-knowledge-backend-<name> (Running)
  watchdog    dsh-knowledge-backend-<name>-watchdog (Ready)
  autostart   enabled
  supervisor  pid 19268 running  state=running
  service     pid 13584 running on 127.0.0.1:18765
  port owner  pid 38396 (this service's own process tree)
  health      isolation=required isolationActive=true
  libraries   wiki-articles (Personal wiki articles (Markdown))
              C:\work\kb -> example-library
```

Two tasks are registered, and they do different jobs: the **supervisor** task (logon trigger) holds the backend,
and a **watchdog** task (`revive`, logon trigger by default) starts the supervisor if it is not running and no
stop was requested. The watchdog deliberately **does not repeat**: a scheduled task with an interactive principal
running a console program opens a console window on every run, so a repeating watchdog is a console window every
few minutes on your desktop — the default was corrected on 2026-09-24 after exactly that was reported. If you
want the two-minute self-healing back and accept the windows, register with `--watchdog-minutes 2`; measured with
the repetition on, a hard-killed supervisor came back in 53 s while an operator `stop` was still honoured 180 s
later. With the default (logon only), a supervisor killed between logons waits for the next logon or an explicit
`start`. `status` prints which mode is actually registered.

What the supervisor does, in one paragraph: it holds a single-instance lock whose value is the OS-reported
identity of its own process (a stale lock is recognised as stale and taken over, and a bare second supervisor
exits 4 without touching the running service); it starts the service with the instance's own environment
(`isolation=required`) and treats a service that answers `/health` without `isolationActive` as a **failed**
start rather than a usable one; a service that exits unexpectedly is restarted with exponential backoff up to
`--max-restarts` (default 5), after which it stops looping, turns autostart **off** so nothing retries it in a
loop, records `state: failed` with the exit code and reason, and exits 3; and a `stop` writes its marker
**before** killing anything, so neither the supervisor nor the watchdog undoes a human stop. Every event is one
JSON line in `data\logs\supervisor.log`. Nothing in this path starts, stops or restarts your DSH host.

Two boundaries worth knowing. The **logon trigger is configured, not proven**, until an actual logon is
observed; `Start-ScheduledTask` exercises the same action but not the trigger. And `uninstall` keeps the
installation and the data by default (`--remove-data` is the only thing that deletes `data\`).

## 6. Add your own library

The workbench now supports adding a folder: local preflight, explicit scope/provider consent, build, then select the library. Session selection and workspace defaults are separate; switching affects subsequent calls only. Check updates and confirm differences before changing authorization. Removing registration keeps originals. Windows Server CI does not verify this desktop workflow. The configuration fallback remains available below.

1. **Extract your PDFs to text with the page map.** The extractor is in this repository, not in the instance:
   `scripts/extract-literature.py` writes a text file plus an audit per PDF, and
   `scripts/add-page-map.py` folds the audit into a catalogue. Run them with the same Python as the service.
2. **Write a library config** — a JSON file listing your libraries:

```json
{ "libraries": [ { "id": "mine", "name": "My papers",
                   "workspace": "C:\\work\\kb",
                   "root": "C:\\work\\kb\\texts",
                   "syntheticFixture": false } ] }
```

`workspace` is the directory you open a session in; `root` is where that library's files are. A library is
visible to a session when **both** hold, compared per path component on the resolved paths (so `kb` never matches
`kb2`, while on Windows `C:\work\kb` and `c:\work\kb\` are the same directory):

1. the session directory is at or under the library's `workspace`, and
2. the library's `root` is at or under the session directory.

In practice: open a session anywhere inside `C:\work\kb` — the workspace itself, `C:\work\kb\texts`, or a
subdirectory of it — and the library is listed; open a session outside `C:\work\kb`, or in a sibling that does not
contain the library's files, and it is not. A library whose `root` sits outside its own `workspace` is refused
rather than offered.

Then point the instance at it. A non-synthetic library is treated as real user data: the service requires an
explicit authorisation for it and re-checks file hashes at query time, so a mistyped path fails loudly:

```powershell
node trial\install.mjs --target "$env:LOCALAPPDATA\dsh-knowledge-trial"   # rewrites nothing
# then edit data\config.json and data\libraries.json; restart with stop + start.
```

Markdown works the same way, except it has no page map, so its cards report line numbers and no page.

### 6a. A library of Markdown notes, without hand-writing a manifest

`scripts/build-markdown-library.py` builds the two things a real library needs — an identity catalogue and the
authorisation manifest — and does it as four previewable commands. Run it with the same Python as the service.
**Without `--apply` it prints what it would do and writes nothing.**

```powershell
$K = "$env:LOCALAPPDATA\dsh-knowledge-trial"
$PY = "<this repository>\.venv\Scripts\python.exe"

# 1. register the library (no manifest yet — so it authorises nothing and refuses every call until step 2)
& $PY scripts\build-markdown-library.py register --libraries "$K\data\libraries.json" --id notes `
  --workspace "C:\work\kb" --root "C:\work\kb\notes" `
  --catalogue "$K\data\catalogue-notes.json" --max-file-bytes 4000000 --apply

# 2. build it: counts, capacity, what changed, and what would be published
& $PY scripts\build-markdown-library.py scan --libraries "$K\data\libraries.json" --id notes
& $PY scripts\build-markdown-library.py scan --libraries "$K\data\libraries.json" --id notes --apply

# 3. check what was published, through the service's own loader and binding check
& $PY scripts\build-markdown-library.py check --libraries "$K\data\libraries.json" --id notes
```

What it reads, and what it refuses to:

| | |
|---|---|
| front matter | `title`, `date`, `url`, `author`, topic keys — as **text**. No YAML engine: nested mappings, anchors, aliases, tags, block scalars and flow mappings are skipped rather than interpreted, the block is bounded in bytes/keys/value length/total characters, and **no URL is ever fetched or validated**. |
| identity | `undetermined`, always. A title the note asserts about itself is not corroboration, and the record says so in `verificationDetail`. |
| the corpus | read-only. Every published file goes to the instance's `data\`; nothing is ever written inside your notes. |
| capacity | `[--max-file-bytes N]` defaults to 2,000,000 and may go up to 8,000,000. Files over it are **reported**, not skipped: a scan that finds one refuses to publish rather than shipping a listing that looks complete. |
| change | the manifest is the authorisation, and it must match the files exactly. Adding, editing or deleting a note refuses every call for that library (`REAL_DATA_TRIAL_FILES_CHANGED`) until you run `scan --apply` again. **A query never refreshes the manifest.** |
| publishing | the catalogue and the manifest are written together, the previous pair is copied to `data\backups\<stamp>-<id>\`, and the rollback commands are printed. |
| an existing catalogue | if you already have one from an earlier version, `bind --id <id> --root <root> --catalogue <file> --apply` attaches the library binding it now needs. |

With two or more libraries configured, **each one must name its own `catalogue`**: the process-wide
`DSH_KNOWLEDGE_CATALOG` is only used while there is exactly one library, because with two it would answer for
whichever library asked first.

## 7. Ask a question

1. Open the URL `start` printed (it carries this instance's own login token).
2. **Scope the conversation to a library first**: type `/knowledge-use samples` (or your library id). The
   scope is *your* decision; the model cannot set, widen or bypass it, and a call naming another library is
   refused before any request is sent.

   Type it in the **conversation**, after your first message. The command itself runs either way — the host
   records `command/run` and its result in the session log even on the empty start screen — but the host does
   not surface that result on the empty screen, so on the empty screen it looks as if nothing happened. The
   instance still needs a session before a question can be asked, so the reliable order is: add the workspace →
   send one message → `/knowledge-use <libraryId>` → ask. If the command answers that no library is configured,
   read the message: it names the directory the session is in and the rule that would have matched.
3. Ask in plain language. Two shapes work well:
   - *"《Thermal Stability of the Synthetic Sample》这篇文档里，样品被保持在什么温度、多长时间、什么容器条件？"*
     — you know the title, so the Agent locates the document and reads it.
   - *"我的资料里记录的平衡常数是多少、单位是什么？"* — a content question, so the Agent searches first.

## 8. Read the evidence

Each answer is followed by a **source card**:

| Field | Meaning |
|---|---|
| 文件 / 知识库 | which file, in which library |
| 来源 | how the quote was obtained: engine excerpt, catalogue hit, or this call's own read |
| 版本 | the file version the quote belongs to. The engine, the card and the read must agree; if the file changed since, the old handle is refused with `SOURCE_VERSION_CHANGED` |
| 位置 | line numbers, and a PDF page **only when the extraction map vouches for it** |
| 身份已核验 / 位置已核验 / 支持：未评估 | three separate statements. "位置已核验" means the offsets rebuild the quote in that version — it says nothing about whether the quote supports the conclusion |
| 展开已返回内容 | shows what this call already returned. It is **not** a read and issues no request |
| 查看候选差异 | when a locate returned several candidates, shows why each matched |

To read more of the original, **the Agent** calls `knowledge_read`; the panel never does. If an excerpt stops
mid-sentence or a unit is cut off, ask again and the Agent reads on.

## 9. Common failures

| What you see | What it means | What to do |
|---|---|---|
| `库范围不匹配` | the call named a library the conversation is not scoped to | `/knowledge-use <id>`, then retry |
| `原文已变更` | the file changed since the handle was issued | search again; do not reuse the handle |
| `额度已用尽` | the service's per-session read budget is spent | wait, or raise the limit |
| `重复失败已熔断` | the same call failed repeatedly and the plugin stopped it | answer from what you have; repeating cannot help |
| `本轮工具调用已达上限` | the per-turn ceiling (default 6; adjustable in the knowledge panel) was reached | answer in a **new** turn |
| `知识库服务不可达` / `超时` | the service is not running, or too slow | `doctor`, then `start`; check `data\logs\service.log` |
| `未知错误` | the plugin could not classify it | quote the 关联 ID and the steps that produced it |
| `打开原件（宿主不支持，未接入）` | opening the original file is **not implemented** | open it yourself from your library folder |

Failure cards never show a path, a handle, a key or an upstream message; they show a fixed category, a
reason, an action, and a correlation id.

## 10. Upgrade, roll back, uninstall

```powershell
# upgrade: install over the same target; app\ is replaced, data\ is kept
node trial\install.mjs --target "$env:LOCALAPPDATA\dsh-knowledge-trial" --python <python>

# roll back: point the same target at the previous package directory
node trial\install.mjs --target "$env:LOCALAPPDATA\dsh-knowledge-trial" --from <previous-package>

# uninstall: stop first, then remove the program and (optionally) the data
node trial\stop.mjs --target "$env:LOCALAPPDATA\dsh-knowledge-trial"
Remove-Item "$env:LOCALAPPDATA\dsh-knowledge-trial\app" -Recurse     # program only
Remove-Item "$env:LOCALAPPDATA\dsh-knowledge-trial\data" -Recurse    # your data, only if you mean it
```

Deleting `app\` alone leaves your libraries, token and logs intact; re-installing restores the program.
Nothing else on the machine is touched — the instance owns only what is under its target directory.

## 11. 服务状态、调用上限与来源树

进入会话后右栏“知识库”自动显示，来源按知识库 → 文档 → 摘录展开，不必打开思考或工具组。这里只显示会话中已返回且已加载的来源，不代表完整库目录。

服务状态每 10 秒检查一次并显示时间；“不可达”可能是未启动、已关闭或连接失败。展开“启动 / 关闭服务”可看到本实例的明确命令，当前没有网页直接启停按钮。关闭共享知识后端影响所有会话。

“每轮知识工具调用上限”默认 6，可设 1–30；是搜索、定位、列库、读取合计，不是模型调用或 token 配额。点击“保存上限”，每轮首次知识调用时锁定该轮值；正在进行的调用计数不会重置，修改对之后新轮生效。配置提供 tokenFile 时保存到其同目录 panel-settings.json；否则页面说明仅本次宿主运行保留。

参见 [面板修复记录](m0-panel-controls-round.md)。


### 日常问答控制面板

右栏支持启动服务、停止服务（确认共享影响）和重新检查。仅受控安装实例且端点绑定一致时提供直接启停；任务 Disabled 时仅单次启动，不 enable 自启动。停止使用实例身份核验，取消在途查询，未完成提供方用量保持未知。轮询不会清除停止标记。

每轮知识工具调用上限默认 6，可为本会话设置 1–30；第一次调用时锁定本轮上限，修改在下一轮生效，失败不退还。尝试数、HTTP fetch 分派（含轮询和清理）、搜索与读取尝试、宿主进程模型统计不是同一口径。

Markdown 回退窗口按结构和中英文查询特征选择原文；metadata 或 POSSIBLE_MATH_LOSS 只是提示，不能证明支持结论。主 Agent 应先读已有正文，再补缺失维度。来源卡 citationId 随工具结果持久化；kref 仍是有生命周期的工具引用。当前宿主没有开放回答引用的点击桥接，因此回答须写文档名、工具行范围及版本，可在来源树核对；不宣称永久链接已接入。


## 从右栏添加或切换知识库

手动打开右侧边栏 → 知识库 → **添加知识库**。输入当前会话工作区内的文件夹及名称，再点**本地预检**。资料目录不能包含插件自身的 data 目录，以免把生成物再导入自己。预检不外发、不调用模型、不写原件；确认目录、可见工作区及片段外发授权后，才会建立并校验知识库。建库后从顶部下拉选择用于本会话，或设为工作区默认。选库与 `/knowledge-use` 共用作用域；切换仅影响后续调用，在途查询及历史来源保持原库。

默认仅对之后新建的会话生效，恢复旧会话保留其选择。实例 profile 配置 `tokenFile` 时，选择和默认保存在 token 文件同目录的 `library-selection.json`；仅配置环境变量 token 的开发 profile 没有持久路径，界面会明确提示仅在本次宿主运行期间保留。文件不可读时不会覆盖它或套用其他默认。

统一导入 `.md` 与具有文本层的 `.pdf`。图片和扫描 PDF 不做 OCR，会列在排除项；坏编码、抽取失败或超限会阻止发布。默认可引用文本单文件 2,000,000 字节、1000 文件、合计 100 MB 不变；导入另有 PDF 原件单文件 32 MB、所选原件合计 100 MB 的本地预检保护，不会自动提高上限。PDF 的公式、表格及复杂行序仍需回原件核对，身份不凭文件名改为已确认。

原件不改动，衍生文本、审计和绑定目录写在实例 `data/managed-libraries/<id>/<version>/`。修改原件后原授权不再适用：点**检查更新**看新增/修改/删除差异，再重新确认并发布。仅预览或取消不会刷新授权。更新生成新版本，保留旧版本与配置备份；**移除登记**只注销本库，不删原件或衍生版本。已有脚本注册的库继续可用，若没有向导的 `sourceRoot`/原件清单，更新仍用原来的脚本流程。

回答中的资料总数只表示目录枚举量；读过部分段落、读完全文以及支持某个结论是不同状态。检索执行失败不能解释为库内没有答案。
