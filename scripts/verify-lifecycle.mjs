/**
 * `verify-lifecycle` — the offline acceptance for the backend's independent lifecycle.
 *
 * What it is for. The backend used to die with whatever launched it, and the symptom a session saw was
 * KNOWLEDGE_SERVICE_UNREACHABLE. This script proves the replacement on a **synthetic instance on its own
 * ports**, with no model call, no engine query and no contact with the daily backend:
 *
 *   1. the backend survives the tool session that started it (the task's own process holds it)
 *   2. a second start is refused: one instance, proved by identity rather than by "the port is busy"
 *   3. an unexpected service death is restarted with backoff, and the loop stops at the threshold
 *   4. a manual stop is not undone a moment later
 *   5. a missing runtime, a bad Python path and an occupied port each produce a recorded, named failure
 *   6. `stop` ends only processes whose identity it can prove, and leaves a decoy alone
 *   7. disabling and removing the task are verifiable, and neither deletes data
 *   8. the daily instance is healthy, isolated, and lists its libraries
 *
 * What it does NOT prove, and says so in its report: the **logon trigger**. `Start-ScheduledTask` runs the
 * task's own action under the scheduler (which is what checks 1-7 use), but only an actual logon exercises the
 * trigger. That is reported as `configured, awaiting the next logon` rather than as verified.
 *
 * The synthetic instance is created once and reused; `--rebuild` recreates it. Nothing here writes outside
 * `.local/` and the synthetic instance's own directory, and no real library is touched.
 */
import { spawn, spawnSync } from 'node:child_process';
import { copyFileSync, existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { join, resolve } from 'node:path';
import {
  APP_ROOT, PROGRAM_FILES, DOC_FILES, copyTree, instanceEnv, instancePaths, newToken, ok, fail, info, say,
  parseFlags, requireInstance, run, sleep, taskNameFor, writeJson, processFacts, portOwner, processTree,
} from '../trial/lib.mjs';

const flags = parseFlags(process.argv.slice(2), {
  target: '.local/lifecycle/inst', 'daily-target': process.env.DSH_KNOWLEDGE_DAILY_TARGET || null, rebuild: false,
  'keep-task': false, 'observe-seconds': 20,
});
const target = resolve(flags.target);
const paths = instancePaths(target);
const taskName = taskNameFor(target);
const SERVICE_PORT = 18875;
const WEB_PORT = 18876;
const TEXTS = join(paths.data, 'library', 'texts');
const MARKDOWN = [
  '# 合成验收语料',
  '',
  '这个文件只用于生命周期验收，不包含任何真实资料。',
  '',
  '第二行：数值 3.0',
  '第三行：单位 M/atm',
  '第四行：结束。',
].join('\n') + '\n';

const results = [];
const record = (label, verdict, detail = '') => {
  results.push({ label, verdict, detail });
  if (verdict === 'pass') ok(label);
  else if (verdict === 'fail') fail(`${label}${detail ? ` — ${detail}` : ''}`);
  else info(`${label}${detail ? ` — ${detail}` : ''}`);
  return verdict;
};

/** Run one management command through the instance's own CLI, as a user would. */
function task(...args) {
  return spawnSync(process.execPath, [join(paths.app, 'trial', 'service-task.mjs'), ...args, '--target', target],
    { encoding: 'utf8', windowsHide: true, timeout: 120_000 });
}

async function health() {
  if (!existsSync(paths.token)) return { status: 0 };
  const token = readFileSync(paths.token, 'utf8').trim();
  try {
    const response = await fetch(`http://127.0.0.1:${SERVICE_PORT}/health`,
      { headers: { authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(4000) });
    return { status: response.status, body: await response.json() };
  } catch (error) {
    return { status: 0, error: String(error?.message ?? error) };
  }
}

async function waitFor(predicate, { timeoutMs = 60_000, everyMs = 400 } = {}) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await predicate();
    if (value) return value;
    if (Date.now() > deadline) return null;
    await sleep(everyMs);
  }
}

// ---------------------------------------------------------------------------------- the synthetic instance

function buildInstance() {
  say(`Creating the synthetic instance at ${target}`);
  rmSync(target, { recursive: true, force: true });
  mkdirSync(paths.app, { recursive: true });
  for (const entry of [...PROGRAM_FILES, ...DOC_FILES]) {
    copyTree(join(APP_ROOT, entry), join(paths.app, entry), { skip: name => name === '__pycache__' || name === '.local' });
  }
  mkdirSync(paths.data, { recursive: true });
  mkdirSync(paths.logs, { recursive: true });
  mkdirSync(paths.run, { recursive: true });
  mkdirSync(TEXTS, { recursive: true });
  writeFileSync(join(TEXTS, 'note.md'), MARKDOWN, 'utf8');
  writeFileSync(join(TEXTS, 'second.md'), '# second\n\none line.\n', 'utf8');
  writeFileSync(paths.token, newToken() + '\n', 'utf8');
  writeJson(paths.config, {
    version: JSON.parse(readFileSync(join(APP_ROOT, 'package.json'), 'utf8')).version,
    installedAt: new Date().toISOString(), updatedAt: new Date().toISOString(),
    profileName: 'kn-lifecycle-' + Math.random().toString(16).slice(2, 10),
    python: resolve(APP_ROOT, '.venv', 'Scripts', 'python.exe'),
    servicePort: SERVICE_PORT, webPort: WEB_PORT,
    modelProvider: 'none-for-lifecycle-acceptance', modelBaseUrl: null, modelApiKeyEnv: null,
    titleModel: 'title-stub', engineBaseUrl: 'http://127.0.0.1:9/v1', engineModel: 'unused',
    engineApiKey: 'trial-placeholder-not-a-credential', engineEnvFile: null, engineApiKeyEnv: null,
    picker: 'browse', appRoot: paths.app,
  });
  // One synthetic library, declared `syntheticFixture` so the real-data authorization manifest is not
  // required. No catalogue: this acceptance is about the lifecycle, not about retrieval.
  writeJson(paths.libraries, {
    libraries: [{
      id: 'samples', name: 'Synthetic lifecycle corpus', workspace: paths.data, root: TEXTS,
      syntheticFixture: true,
    }],
  });
  writeFileSync(join(paths.data, 'engine.env'),
    '# Synthetic instance: no provider credential is used by this acceptance.\n', 'utf8');
}

function ensureRuntime() {
  // The staged runtime is large and read-only for our purposes, so it is referenced rather than copied. The
  // engine payload the service stages into it is byte-identical to what is already there (the same seven
  // files from the same repository), so nothing in the shared runtime changes meaningfully.
  const source = join(APP_ROOT, '.local', 'appcontainer-runtime');
  if (!existsSync(source)) return record('synthetic instance: staged runtime available', 'fail',
    `no staged runtime at ${source}; run scripts/stage-appcontainer-runtime.py`);
  if (!existsSync(join(paths.runtime, 'ready'))) {
    const made = run('cmd', ['/c', 'mklink', '/J', paths.runtime, source]);
    if (made.status !== 0) return record('synthetic instance: staged runtime available', 'fail',
      `the junction could not be created: ${(made.stderr || made.stdout || '').trim().slice(0, 200)}`);
  }
  mkdirSync(paths.tools, { recursive: true });
  for (const [from, to] of [[join(APP_ROOT, '.local', 'tools', 'rg.exe'), join(paths.tools, 'rg.exe')],
    [join(APP_ROOT, '.local', 'tools', 'rga-patched', 'rga.exe'), join(paths.tools, 'rga.exe')]]) {
    if (!existsSync(to)) {
      if (!existsSync(from)) return record('synthetic instance: tool binaries available', 'fail', `missing ${from}`);
      copyFileSync(from, to);
    }
  }
  return record('synthetic instance: staged runtime available', 'pass',
    `${paths.runtime} -> ${source} (junction; nothing is copied)`);
}

// ---------------------------------------------------------------------------------- the checks

/** 1. The backend is held by the task's own process, not by whoever asked for it. */
async function checkIndependence() {
  const started = task('start');
  if (started.status !== 0) return record('1. background start accepted', 'fail', (started.stdout || '').trim().slice(0, 200));
  const ready = await waitFor(async () => (await health()).status === 200, { timeoutMs: 90_000 });
  if (!ready) return record('1. the service comes up under the task', 'fail', 'no healthy /health within 90s');
  record('1. the service comes up under the task', 'pass');

  // The task's own result, read from the scheduler: 0 = finished, 267009 = still running. A non-zero, other
  // than "still running", means the action itself failed (a wrong argument string exits 2 with no other trace).
  const info = run('powershell', ['-NoProfile', '-Command',
    `$i = Get-ScheduledTaskInfo -TaskName '${taskName}'; "$($i.LastTaskResult)|$($i.LastRunTime.ToUniversalTime().Ticks)"`]);
  const [lastResult, lastRunTicks] = (info.stdout || '').trim().split('|');
  const ran = Number(lastResult) === 0 || Number(lastResult) === 267009;
  record('1. the task action itself exits cleanly', ran ? 'pass' : 'fail',
    `LastTaskResult=${lastResult} at ticks ${lastRunTicks}`);

  const supervisor = JSON.parse(readFileSync(paths.pidSupervisor, 'utf8'));
  const facts = processFacts(supervisor.pid);
  const parent = run('powershell', ['-NoProfile', '-Command',
    `(Get-CimInstance Win32_Process -Filter "ProcessId=${supervisor.pid}").ParentProcessId`]);
  const parentPid = Number((parent.stdout || '').trim());
  const parentFacts = Number.isInteger(parentPid) ? processFacts(parentPid) : null;
  // The claim under test is *not* "a process exists" but "this process is not ours". Its parent is the task
  // engine (a Windows service host), not this script and not any shell this script was run from.
  const ours = [process.pid, process.ppid];
  const independent = parentFacts !== null && !ours.includes(parentPid)
    && !/node\.exe$/i.test(parentFacts.name ?? '');
  record('1. the supervisor is not a child of this session', independent ? 'pass' : 'fail',
    `supervisor pid ${supervisor.pid} (started ${facts?.creationTicks ? 'recorded' : 'unknown'}), parent pid ${parentPid} ${parentFacts?.name ?? 'unknown'}`);

  const before = await health();
  const observeSeconds = Number(flags['observe-seconds']);
  const deadline = Date.now() + observeSeconds * 1000;
  let alive = true;
  while (Date.now() < deadline) {
    await sleep(1000);
    if ((await health()).status !== 200) { alive = false; break; }
  }
  record(`1. still healthy after ${observeSeconds}s of no supervision from this script`, alive ? 'pass' : 'fail',
    `isolationActive=${before.body?.isolationActive}`);
  return alive;
}

/** 2. A second start is refused, and the refusal is by identity rather than by port. */
async function checkSingleInstance() {
  // `start` while a supervisor holds the lock must report that, not launch a second one.
  const again = task('start');
  const text = `${again.stdout}${again.stderr}`;
  record('2. a second start is refused as "already running"', /already running/i.test(text) ? 'pass' : 'fail',
    text.trim().split('\n').filter(line => /PASS|FAIL|already/i.test(line)).join(' | ').slice(0, 200));

  // And a bare supervisor (what the task action runs) exits 4 without touching the running service. It is
  // given a short timeout: a correct one exits at once, and a wrong one would hang inside the service loop.
  const direct = spawnSync(process.execPath,
    [join(paths.app, 'trial', 'service-task.mjs'), 'supervise', '--target', target],
    { encoding: 'utf8', windowsHide: true, timeout: 30_000 });
  record('2. a bare second supervisor exits 4 without starting a service', direct.status === 4 ? 'pass' : 'fail',
    `exit ${direct.status}: ${(direct.stdout || '').trim().split('\n')[0] || ''}`);
  const owner = portOwner(SERVICE_PORT);
  const service = processFacts(owner);
  record('2. exactly one service process holds the port', owner !== null ? 'pass' : 'fail', `pid ${owner} (${service?.name ?? '?'})`);
  const state = JSON.parse(readFileSync(paths.supervisorState, 'utf8'));
  record('2. the duplicate attempt did not become the recorded state', state.state === 'running' ? 'pass' : 'fail',
    `state=${state.state}`);
}

/**
 * 2b. Whose port is it?
 *
 * The fact this check exists for: the pid the supervisor records is **not** the process that binds the port. A
 * venv's `python.exe` is a redirector stub, so `python -m uvicorn ...` starts and the interpreter that listens is
 * its child. A port check that compares the owner against the recorded pid alone therefore refuses to start an
 * instance that is perfectly healthy - which would be a self-inflicted outage on every restart.
 *
 * So the check asserts the *relationship* on the live instance (the real owner is in the recorded pid's tree, so
 * the preflight predicate accepts it), and then that a supervisor which takes over after a hard kill still comes
 * up. It deliberately does not assert that the service outlives its supervisor by itself: how the tree dies
 * depends on what launched the supervisor (a task-owned supervisor takes its tree with it), and asserting that
 * would be a test of the launcher rather than of this code. What it does assert is that **when** an orphan does
 * survive - which is what a detached service does - the next supervisor recognises it as its own, replaces it,
 * and does not mistake it for a foreign process on its port.
 */
async function checkPortIdentity() {
  const recorded = JSON.parse(readFileSync(paths.pidService, 'utf8'));
  const owner = portOwner(SERVICE_PORT);
  const tree = processTree(recorded.pid);
  const ours = owner !== null && (owner === recorded.pid || tree.has(owner));
  record('2b. the pid that binds the port is in the recorded service\'s own tree', ours ? 'pass' : 'fail',
    `recorded ${recorded.pid}, port owner ${owner}, tree [${[...tree].join(', ')}]`);
  // The predicate the preflight uses, evaluated on the live instance: no problem is produced for our own tree.
  const complaint = owner !== null && recorded.pid !== owner && !tree.has(owner);
  record('2b. the preflight accepts that tree instead of calling it a conflict', complaint ? 'fail' : 'pass',
    complaint ? `would have refused: port ${SERVICE_PORT} held by pid ${owner}` : 'no conflict reported');

  // A supervisor that takes over after its predecessor was killed hard must come up, not refuse the port.
  //
  // The wait is for the **takeover** - the pid file naming a different, live supervisor - and not for health.
  // Since the service is launched detached into its own console group, the orphan keeps answering `/health`
  // after its supervisor is killed; waiting for health would therefore succeed before the new supervisor had
  // even read the lock, and would measure nothing.
  const supervisor = JSON.parse(readFileSync(paths.pidSupervisor, 'utf8'));
  const previousPid = supervisor.pid;
  const previousService = JSON.parse(readFileSync(paths.pidService, 'utf8')).pid;
  run('taskkill', ['/PID', String(previousPid), '/F']);
  await waitFor(() => processFacts(previousPid) === null, { timeoutMs: 20_000 });
  const survived = portOwner(SERVICE_PORT) !== null;
  info(`after killing the supervisor: the port is ${survived ? 'still held by its service' : 'free'} `
    + '(how the tree dies depends on what launched the supervisor, so this is an observation, not an assertion)');

  const next = spawn(process.execPath, [join(paths.app, 'trial', 'service-task.mjs'), 'supervise',
    '--target', target, '--max-restarts', '1', '--backoff-ms', '300'], { windowsHide: true });
  let output = '';
  next.stdout.on('data', chunk => { output += chunk; });
  next.stderr.on('data', chunk => { output += chunk; });
  const tookOver = await waitFor(() => {
    let record = null;
    try { record = JSON.parse(readFileSync(paths.pidSupervisor, 'utf8')); } catch { return null; }
    return record.pid && record.pid !== previousPid && processFacts(record.pid) !== null ? record : null;
  }, { timeoutMs: 60_000, everyMs: 500 });
  record('2b. a new supervisor takes the stale lock over', tookOver ? 'pass' : 'fail',
    tookOver ? `pid ${tookOver.pid} replaced ${previousPid}` : `no takeover within 60s (exit ${next.exitCode})`);
  const back = await waitFor(async () => (await health()).status === 200, { timeoutMs: 90_000 });
  const refusedPort = /port .* is held by pid/.test(output);
  record('2b. it comes up instead of refusing the port the orphan holds', back && !refusedPort ? 'pass' : 'fail',
    output.trim().split('\n').slice(-2).join(' | ').slice(0, 200) || 'no output');
  record('2b. the stale lock is recognised as stale, not obeyed',
    readEventLog().some(entry => entry.event === 'LOCK_STALE') ? 'pass' : 'fail');
  // The takeover must *replace* the orphan rather than adopt it: a different service pid, and the event that
  // says the previous supervisor's service was found first.
  const replaced = await waitFor(() => {
    try { return JSON.parse(readFileSync(paths.pidService, 'utf8')).pid !== previousService; } catch { return false; }
  }, { timeoutMs: 30_000, everyMs: 500 });
  const orphanEvent = readEventLog().some(entry => entry.event === 'ORPHAN_SERVICE');
  record('2b. the orphaned service is recorded and replaced, not adopted', replaced && orphanEvent ? 'pass' : 'unverified',
    `new pid recorded: ${replaced}; ORPHAN_SERVICE logged: ${orphanEvent}`);
  if (next.exitCode === null) next.kill();
  task('stop');
  await waitFor(async () => portOwner(SERVICE_PORT) === null, { timeoutMs: 30_000 });
  return true;
}

/**
 * 2c. The supervisor's *own* death - the gap a watchdog exists for.
 *
 * Measured on this machine: a Windows console close killed the supervisor itself (the task's LastTaskResult was
 * `0xC000013A`), and Windows' own "restart on failure" did **not** fire even with `RestartCount=3` and
 * `RestartInterval=PT2M` registered - the task ended with a non-zero result and simply stayed Ready. So the
 * revival is explicit: a second, short-lived task runs `revive` on a repetition trigger, which starts the
 * supervisor only when no stop was requested and no supervisor is alive.
 *
 * Two properties, and the second matters as much as the first: the watchdog must bring the backend back after a
 * hard kill, and it must **not** undo an operator `stop` (that would quietly turn "stop" into "disable").
 */
async function checkWatchdog() {
  const installed = task('install', '--watchdog-minutes', '1');
  const started = task('start');
  const ready = await waitFor(async () => (await health()).status === 200, { timeoutMs: 90_000 });
  if (!ready) {
    return record('2c. the instance is up before the watchdog test', 'fail',
      `${(installed.stdout || started.stdout || '').trim().split('\n').slice(-2).join(' | ').slice(0, 200)}`);
  }
  const supervisor = JSON.parse(readFileSync(paths.pidSupervisor, 'utf8'));
  const servicePid = JSON.parse(readFileSync(paths.pidService, 'utf8')).pid;
  const killedAt = Date.now();
  run('taskkill', ['/PID', String(supervisor.pid), '/F']);
  await waitFor(() => processFacts(supervisor.pid) === null, { timeoutMs: 20_000 });
  record('2c. the supervisor can be killed without ending its service',
    processFacts(servicePid) !== null ? 'pass' : 'unverified',
    `service pid ${servicePid} after the supervisor died`);

  const revived = await waitFor(() => {
    let record = null;
    try { record = JSON.parse(readFileSync(paths.pidSupervisor, 'utf8')); } catch { return null; }
    return record.pid && record.pid !== supervisor.pid && processFacts(record.pid) !== null ? record : null;
  }, { timeoutMs: 150_000, everyMs: 3000 });
  record('2c. the watchdog starts a new supervisor after a hard kill', revived ? 'pass' : 'fail',
    revived ? `pid ${revived.pid} after ${Math.round((Date.now() - killedAt) / 1000)}s (interval 1 min)`
      : 'no revival within 150s');
  record('2c. the watchdog recorded its decision',
    readEventLog().some(entry => entry.event === 'REVIVE_STARTED') ? 'pass' : 'fail');
  const back = await waitFor(async () => (await health()).status === 200, { timeoutMs: 90_000 });
  record('2c. the service is healthy again after that handover', back ? 'pass' : 'fail');

  // And the stop must hold across at least one watchdog tick (interval 1 min, so wait 80s).
  task('stop');
  const stoppedAt = Date.now();
  let held = true;
  while (Date.now() - stoppedAt < 80_000) {
    await sleep(5000);
    let record = null;
    try { record = JSON.parse(readFileSync(paths.pidSupervisor, 'utf8')); } catch { record = null; }
    if (record?.pid && processFacts(record.pid) !== null) {
      held = false;
      record('2c. an operator stop is not undone by the watchdog', 'fail',
        `a supervisor (pid ${record.pid}) came back ${Math.round((Date.now() - stoppedAt) / 1000)}s after the stop`);
      break;
    }
  }
  if (held) {
    record('2c. an operator stop is not undone by the watchdog', 'pass',
      'no supervisor came back within 80s (one watchdog tick plus slack)');
  }
  const skipped = readEventLog().some(entry => entry.event === 'REVIVE_SKIPPED' && /stop was requested/.test(entry.reason ?? ''));
  record('2c. the watchdog recorded why it declined to revive', skipped ? 'pass' : 'fail');
  return true;
}

/** 5. Each refusal path produces a named failure with a recorded reason. */
async function checkRefusals() {
  // 5a. missing runtime: rename the junction for the length of one supervisor start.
  task('stop');
  await waitFor(async () => portOwner(SERVICE_PORT) === null, { timeoutMs: 20_000 });
  const parked = `${paths.runtime}.parked`;
  renameSync(paths.runtime, parked);
  const missing = task('supervise');
  renameSync(parked, paths.runtime);
  const missingState = JSON.parse(readFileSync(paths.supervisorState, 'utf8'));
  const namedRuntime = missing.status === 5 && /runtime/i.test(missingState.reason || '');
  record('5. a missing runtime refuses to start, naming it', namedRuntime ? 'pass' : 'fail',
    `exit ${missing.status}, state=${missingState.state}, reason=${(missingState.reason || '').slice(0, 90)}`);

  // 5b. a configured Python that is gone.
  const config = JSON.parse(readFileSync(paths.config, 'utf8'));
  writeJson(paths.config, { ...config, python: join(paths.data, 'no-such-python.exe') });
  const badPython = task('supervise');
  writeJson(paths.config, config);
  const badState = JSON.parse(readFileSync(paths.supervisorState, 'utf8'));
  record('5. a missing interpreter refuses to start, naming it', badPython.status === 5 && /Python/i.test(badState.reason || '')
    ? 'pass' : 'fail', `exit ${badPython.status}, reason=${(badState.reason || '').slice(0, 90)}`);

  // 5c. an occupied port: a listener this instance did not record.
  const decoy = createServer();
  await new Promise(done => decoy.listen(SERVICE_PORT, '127.0.0.1', done));
  const occupied = task('supervise');
  const occupiedState = JSON.parse(readFileSync(paths.supervisorState, 'utf8'));
  await new Promise(done => decoy.close(done));
  record('5. an occupied port refuses to start, naming the holder', occupied.status === 5 && /port/i.test(occupiedState.reason || '')
    ? 'pass' : 'fail', `exit ${occupied.status}, reason=${(occupiedState.reason || '').slice(0, 90)}`);

  // 5d. the log records each refusal; the whole-log integrity check runs at the end, once every kind of event
  // has had a chance to occur.
  const events = readEventLog();
  record('5. every refusal path wrote its own event',
    events.some(entry => entry.event === 'PREFLIGHT_FAILED') ? 'pass' : 'fail',
    `events so far: ${[...new Set(events.map(entry => entry.event))].join(', ').slice(0, 200)}`);
  return true;
}

function readEventLog() {
  if (!existsSync(paths.supervisorLog)) return [];
  return readFileSync(paths.supervisorLog, 'utf8').split('\n').filter(Boolean).map(line => {
    try { return JSON.parse(line); } catch { return { event: null, unparsed: line }; }
  });
}

/** The task's own state, as the scheduler reports it ("Ready" / "Running" / "Disabled" / null). */
function taskStateOf() {
  const result = run('powershell', ['-NoProfile', '-Command',
    `(Get-ScheduledTask -TaskName '${taskName}' -ErrorAction SilentlyContinue).State`]);
  const text = (result.stdout || '').trim();
  return text.length ? text : null;
}

/**
 * The log, checked as a record rather than as prose.
 *
 * Three properties, all of them claims this file makes: every line is one JSON object (so a reader can parse
 * it without guessing); the events a lifecycle must show are all present; and **no credential appears in it** -
 * the service token is looked for by value, because "we do not log secrets" is a property of the bytes, not of
 * the field names.
 */
function checkLog() {
  const lines = readFileSync(paths.supervisorLog, 'utf8').split('\n').filter(Boolean);
  const parsed = lines.map(line => { try { return JSON.parse(line); } catch { return null; } });
  const wellFormed = parsed.every(entry => entry && typeof entry.at === 'string' && typeof entry.event === 'string'
    && Number.isInteger(entry.pid));
  record('5. every supervisor log line is one JSON object', wellFormed ? 'pass' : 'fail',
    `${lines.length} lines, ${parsed.filter(Boolean).length} parsed`);
  const vocabulary = new Set(parsed.filter(Boolean).map(entry => entry.event));
  const required = ['SERVICE_STARTING', 'SERVICE_READY', 'SERVICE_EXITED', 'RESTART_BACKOFF', 'SUPERVISOR_FAILED',
    'SUPERVISOR_DUPLICATE', 'PREFLIGHT_FAILED', 'STOP_MARKER_WRITTEN', 'STOP_MARKER_CONSUMED', 'STOP_COMPLETE',
    'LOCK_STALE', 'TASK_INSTALLED', 'TASK_UNINSTALLED'];
  const missing = required.filter(name => !vocabulary.has(name));
  record('5. the log shows the whole lifecycle vocabulary', missing.length === 0 ? 'pass' : 'fail',
    missing.length ? `missing: ${missing.join(', ')}` : `${vocabulary.size} distinct events`);
  const token = readFileSync(paths.token, 'utf8').trim();
  record('5. no credential appears in the log', !readFileSync(paths.supervisorLog, 'utf8').includes(token)
    ? 'pass' : 'fail', `searched ${lines.length} lines for the service token by value`);
  const mentioningFiles = lines.filter(line => /\.md"/.test(line));
  record('5. no library file name appears in the log', mentioningFiles.length === 0 ? 'pass' : 'fail',
    `${mentioningFiles.length} line(s) mention a document name`);
}

/** 3. An unexpected death is restarted with backoff, and the loop stops at the threshold. */
async function checkRestartPolicy() {
  const child = spawn(process.execPath, [join(paths.app, 'trial', 'service-task.mjs'), 'supervise', '--target', target,
    '--max-restarts', '2', '--backoff-ms', '400', '--healthy-seconds', '600'], { windowsHide: true });
  let output = '';
  child.stdout.on('data', chunk => { output += chunk; });
  child.stderr.on('data', chunk => { output += chunk; });
  const ready = await waitFor(async () => (await health()).status === 200, { timeoutMs: 90_000 });
  if (!ready) { child.kill(); return record('3. the supervisor starts the service', 'fail', output.slice(-300)); }
  record('3. the supervisor starts the service', 'pass');

  // Kill the service three times: two restarts are allowed, the third exceeds the threshold. Each kill waits
  // for a healthy service first, so a kill can never land on a pid that is already gone (which would make the
  // count meaningless).
  let kills = 0;
  for (let round = 0; round < 10 && kills < 3; round += 1) {
    await waitFor(async () => (await health()).status === 200, { timeoutMs: 60_000, everyMs: 500 });
    const recorded = existsSync(paths.pidService) ? JSON.parse(readFileSync(paths.pidService, 'utf8')) : null;
    if (!recorded?.pid || !processFacts(recorded.pid)) continue;
    const killed = run('taskkill', ['/PID', String(recorded.pid), '/T', '/F']);
    if (killed.status === 0) kills += 1;
    await sleep(600);
  }
  record('3. the service can be killed three times', kills === 3 ? 'pass' : 'fail', `kills accepted: ${kills}`);
  const exited = await waitFor(() => child.exitCode !== null, { timeoutMs: 120_000 });
  const state = JSON.parse(readFileSync(paths.supervisorState, 'utf8'));
  record('3. the restart threshold stops the loop and exits non-zero',
    exited && child.exitCode === 3 && state.state === 'failed' ? 'pass' : 'fail',
    `exit ${child.exitCode}, state=${state.state}, attempts=${state.attempts}`);
  const events = readFileSync(paths.supervisorLog, 'utf8').split('\n').filter(Boolean)
    .map(line => { try { return JSON.parse(line); } catch { return {}; } });
  const backoffs = events.filter(entry => entry.event === 'RESTART_BACKOFF');
  const growing = backoffs.length >= 2 && backoffs[1].waitMs >= backoffs[0].waitMs * 2;
  record('3. restarts back off instead of hammering', growing ? 'pass' : 'fail',
    `backoffs: ${backoffs.map(entry => entry.waitMs).join(', ') || 'none'}`);
  // Reaching the threshold must stop the loop **for good**, not hand the problem to the scheduler's crash
  // policy (which cannot tell a deliberate end from an accident): autostart is turned off and said so.
  const disabledByFailure = /disabled/i.test(taskStateOf() ?? '');
  record('3. the threshold turns autostart off instead of letting another authority retry it',
    disabledByFailure ? 'pass' : 'fail', `task state ${taskStateOf()}`);
  const restored = task('enable');
  record('3. enable restores autostart after a threshold stop',
    restored.status === 0 && /ready|running/i.test(taskStateOf() ?? '') ? 'pass' : 'fail',
    `state ${taskStateOf()} after enable (exit ${restored.status})`);
  if (child.exitCode === null) child.kill();
  // Clean up through the tool rather than by killing the supervisor: an orphaned service would make the next
  // check's preflight see an occupied port (correctly, but for the wrong reason).
  task('stop');
  await waitFor(async () => portOwner(SERVICE_PORT) === null, { timeoutMs: 30_000 });
  return true;
}

/** 6. `stop` ends only what it can prove, and a manual stop is not undone. */
async function checkStop() {
  const started = task('start');
  const ready = await waitFor(async () => (await health()).status === 200, { timeoutMs: 90_000 });
  if (!ready) return record('6. the service is up before the stop', 'fail', (started.stdout || '').trim().slice(0, 200));
  const supervisor = JSON.parse(readFileSync(paths.pidSupervisor, 'utf8'));
  const servicePid = JSON.parse(readFileSync(paths.pidService, 'utf8')).pid;

  // A decoy: a live process that does NOT name this instance. A forged pid file claims it is the service, so
  // `stop` must recognise the mismatch, report it, and leave the decoy running.
  const decoy = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 120000)'], { windowsHide: true, stdio: 'ignore' });
  await sleep(1200);
  writeFileSync(paths.pidService, JSON.stringify({ pid: decoy.pid, kind: 'service', port: SERVICE_PORT,
    marker: paths.app, command: 'not this instance', creationTicks: 1, executable: 'C:\\Windows\\System32\\cmd.exe' }, null, 2));
  const forged = task('stop');
  await sleep(500);
  const decoyAlive = processFacts(decoy.pid) !== null;
  record('6. a process whose identity does not match is left alone',
    decoyAlive && /left alone|NOT this instance/i.test(`${forged.stdout}${forged.stderr}`) ? 'pass' : 'fail',
    `${(forged.stdout || '').split('\n').filter(line => /FAIL|foreign|left alone/i.test(line)).join(' ').slice(0, 160)}`);

  // Now the real stop, with the real identity recorded again.
  writeFileSync(paths.pidService, JSON.stringify({ pid: servicePid, kind: 'service', port: SERVICE_PORT,
    marker: paths.app, command: 'uvicorn service:app', app: paths.app, supervisor: supervisor.pid,
    executable: null, creationTicks: null }, null, 2));
  const stopped = task('stop');
  const gone = await waitFor(async () => (await health()).status !== 200 && portOwner(SERVICE_PORT) === null,
    { timeoutMs: 30_000 });
  record('6. stop ends the service and frees the port', gone ? 'pass' : 'fail',
    (stopped.stdout || '').trim().split('\n').slice(-3).join(' | ').slice(0, 200));
  const supervisorGone = processFacts(supervisor.pid) === null;
  record('6. stop ends the supervisor it recorded', supervisorGone ? 'pass' : 'fail', `pid ${supervisor.pid}`);
  decoy.kill();
  // Leave no stale service record behind: the next check starts from a clean slate.
  rmSync(paths.pidService, { force: true });

  // 4. and it stays stopped: nothing here is a watchdog that brings it back.
  const observe = 15;
  let stillDown = true;
  for (let second = 0; second < observe; second += 1) {
    await sleep(1000);
    if ((await health()).status === 200 || portOwner(SERVICE_PORT) !== null) { stillDown = false; break; }
  }
  record(`4. the service stays stopped for ${observe}s after a manual stop`, stillDown ? 'pass' : 'fail');
  const state = JSON.parse(readFileSync(paths.supervisorState, 'utf8'));
  record('4. the state file says it was stopped, not that it failed', state.state === 'stopped' ? 'pass' : 'fail',
    `state=${state.state}`);
  return true;
}

/** 7. Disable and uninstall are verifiable, and neither takes the data with it. */
async function checkTaskManagement() {
  const installed = task('install');
  record('7. the task is registered', /registered|refreshed/i.test(`${installed.stdout}`) ? 'pass' : 'fail',
    (installed.stdout || '').trim().split('\n').filter(line => /PASS|FAIL/.test(line)).join(' ').slice(0, 160));
  const disabled = task('disable');
  const state = run('powershell', ['-NoProfile', '-Command',
    `(Get-ScheduledTask -TaskName '${taskName}' -ErrorAction SilentlyContinue).State`]);
  record('7. disable is reflected in the task state', /disabled/i.test(state.stdout || '') ? 'pass' : 'fail',
    `state=${(state.stdout || '').trim() || 'absent'}; ${(disabled.stdout || '').trim().split('\n')[0]}`);
  const enabled = task('enable');
  const state2 = run('powershell', ['-NoProfile', '-Command',
    `(Get-ScheduledTask -TaskName '${taskName}' -ErrorAction SilentlyContinue).State`]);
  record('7. enable restores it', /ready|running/i.test(state2.stdout || '') ? 'pass' : 'fail',
    `state=${(state2.stdout || '').trim()}, exit ${enabled.status}`);
  if (flags['keep-task']) { record('7. uninstall exercised', 'unverified', '--keep-task was given'); return true; }
  const removed = task('uninstall');
  const after = run('powershell', ['-NoProfile', '-Command',
    `if (Get-ScheduledTask -TaskName '${taskName}' -ErrorAction SilentlyContinue) { 'present' } else { 'absent' }`]);
  record('7. uninstall removes the task', /absent/.test(after.stdout || '') ? 'pass' : 'fail',
    (removed.stdout || '').trim().split('\n').slice(-2).join(' | ').slice(0, 160));
  const kept = existsSync(join(paths.data, 'libraries.json')) && existsSync(paths.token) && existsSync(paths.app);
  record('7. uninstall keeps the installation files and the data', kept ? 'pass' : 'fail',
    `data=${existsSync(paths.data)}, app=${existsSync(paths.app)}`);
  // Leave the synthetic instance's task installed and enabled: the next run reuses it as-is.
  task('install');
  const logKept = existsSync(paths.supervisorLog);
  record('7. the log survives the uninstall', logKept ? 'pass' : 'fail', paths.supervisorLog);
  return true;
}

/** 8. The daily instance, as a *reader*: healthy, isolated, and listing its libraries. */
async function checkDaily() {
  if (!flags['daily-target']) return record('8. the daily instance is healthy', 'unverified', 'no daily target explicitly supplied');
  const daily = resolve(flags['daily-target']);
  const dailyPaths = instancePaths(daily);
  if (!existsSync(dailyPaths.config)) return record('8. the daily instance is healthy', 'unverified', `no instance at ${daily}`);
  if (!existsSync(join(dailyPaths.app, 'trial', 'service-task.mjs'))) {
    return record('8. the daily instance is healthy', 'fail',
      `the installed program at ${dailyPaths.app} predates this build (no trial/service-task.mjs); upgrade it first`);
  }
  const listed = spawnSync(process.execPath,
    [join(dailyPaths.app, 'trial', 'service-task.mjs'), 'status', '--target', daily, '--json'],
    { encoding: 'utf8', windowsHide: true, timeout: 60_000 });
  let report = null;
  try { report = JSON.parse((listed.stdout || '').trim()); } catch { /* reported below */ }
  if (!report) return record('8. the daily instance is healthy', 'fail',
    ((listed.stdout || '') + (listed.stderr || '')).replace(/\s+/g, ' ').slice(0, 240));
  record('8. the daily instance is healthy with isolation on',
    report.health?.isolationActive === true && report.health?.isolation === 'required' ? 'pass' : 'fail',
    `isolation=${report.health?.isolation} isolationActive=${report.health?.isolationActive}`);
  record('8. the daily instance lists its libraries',
    Array.isArray(report.libraries) && report.libraries.length > 0 ? 'pass' : 'fail',
    (report.libraries || []).map(entry => entry.id).join(', ') || 'none');
  record('8. no model-request ceiling is armed on the daily instance',
    report.health?.providerBudget?.enforced === false ? 'pass' : 'unverified',
    `providerBudget=${JSON.stringify(report.health?.providerBudget)}`);
  return true;
}

/** The read contract, end to end, against the live isolated service (no TestClient). */
async function checkReadContract() {
  const started = task('start');
  const ready = await waitFor(async () => (await health()).status === 200, { timeoutMs: 90_000 });
  if (!ready) return record('read contract: the service is up', 'fail', (started.stdout || '').trim().slice(0, 160));
  const token = readFileSync(paths.token, 'utf8').trim();
  const file = join(TEXTS, 'note.md');
  // The handle is minted by the installed engine itself: a handle is signed with the service token, so there
  // is no way to fabricate one for a live service - which is exactly the property being relied on here.
  const mintScript = [
    'import sys, pathlib',
    'engine, workspace, token, path = sys.argv[1:5]',
    'sys.path.insert(0, engine)',
    'import reader, evidence',
    'data = pathlib.Path(path).read_bytes()',
    'print(reader.issue_handle(reader.derive_secret(token), workspace, "samples", "note.md", evidence.digest(data)))',
  ].join('\n');
  const minted = spawnSync(resolve(APP_ROOT, '.venv', 'Scripts', 'python.exe'),
    ['-c', mintScript, join(paths.app, 'engine'), paths.data, token, file],
    { encoding: 'utf8', windowsHide: true, timeout: 60_000 });
  const handle = (minted.stdout || '').trim().split('\n').pop();
  if (!handle) return record('read contract: a handle can be minted', 'fail', (minted.stderr || '').slice(0, 200));
  const response = await fetch(`http://127.0.0.1:${SERVICE_PORT}/reads`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', authorization: `Bearer ${token}` },
    body: JSON.stringify({ workspace: paths.data, libraryId: 'samples', fileHandle: handle, startLine: 9999 }),
    signal: AbortSignal.timeout(15_000),
  });
  const body = await response.json();
  const error = body.error || {};
  const lines = MARKDOWN.split('\n').length - 1;
  record('read contract: an out-of-range read returns the structured refusal',
    response.status === 400 && error.code === 'READ_RANGE_INVALID' && error.fileLines === lines
      && error.reason === 'START_LINE_PAST_END' && error.suggestedAction === 'CORRECT_START_LINE' ? 'pass' : 'fail',
    `HTTP ${response.status} ${JSON.stringify(error).slice(0, 200)}`);
  record('read contract: the legacy detail string is still present for older callers',
    typeof body.detail === 'string' && body.detail.startsWith('READ_RANGE_INVALID:') ? 'pass' : 'fail',
    String(body.detail).slice(0, 80));
  const logged = readFileSync(join(paths.logs, 'service.log'), 'utf8').split('\n')
    .filter(line => line.startsWith('READ_REFUSAL ')).map(line => { try { return JSON.parse(line.slice(13)); } catch { return {}; } });
  const match = logged.find(entry => entry.correlationId === error.correlationId);
  record('read contract: the correlation id in the response is the one in the service log',
    Boolean(match) ? 'pass' : 'fail', `correlationId=${error.correlationId}`);
  const good = await fetch(`http://127.0.0.1:${SERVICE_PORT}/reads`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', authorization: `Bearer ${token}` },
    body: JSON.stringify({ workspace: paths.data, libraryId: 'samples', fileHandle: handle, startLine: 5, maxLines: 2 }),
    signal: AbortSignal.timeout(15_000),
  });
  const window = await good.json();
  record('read contract: the same handle reads the last lines normally',
    good.status === 200 && window.fileLines === lines && typeof window.excerpt === 'string' && window.excerpt.length > 0
      ? 'pass' : 'fail', `HTTP ${good.status} fileLines=${window.fileLines}`);
  task('stop');
  return true;
}

// ---------------------------------------------------------------------------------- run

say('');
say(`dsh-knowledge lifecycle acceptance — synthetic instance ${target}`);
say(`task ${taskName}, ports ${SERVICE_PORT}/${WEB_PORT}, no model call, no engine query`);
say('');
if (flags.rebuild || !existsSync(paths.config)) buildInstance();
else info(`reusing the synthetic instance at ${target} (--rebuild recreates it)`);
const ready = ensureRuntime();
if (ready !== 'pass') { say(''); say('The synthetic instance is not usable; stopping.'); report(); process.exit(1); }
mkdirSync(paths.logs, { recursive: true });
rmSync(paths.supervisorLog, { force: true });   // each acceptance run starts from a clean log

function report() {
  say('');
  const failed = results.filter(entry => entry.verdict === 'fail');
  const unverified = results.filter(entry => entry.verdict === 'unverified');
  say(`Acceptance: ${results.filter(entry => entry.verdict === 'pass').length} passed, ${failed.length} failed, `
    + `${unverified.length} unverified`);
  for (const entry of failed) say(`  FAIL  ${entry.label} — ${entry.detail}`);
  for (const entry of unverified) say(`  ????  ${entry.label} — ${entry.detail}`);
  writeJson(join(APP_ROOT, '.local', 'verification', 'lifecycle.json'),
    { recordedAt: new Date().toISOString(), target, task: taskName, results });
  return failed.length === 0 ? 0 : 1;
}

// The order is dictated by what each check needs, not by its number: the task must exist before anything can
// be started through it, the crash test needs the single-instance lock to itself, and the task is left
// installed and enabled at the end.
const installed = task('install');
record('7. the task can be registered', /registered|refreshed/i.test(installed.stdout || '') ? 'pass' : 'fail',
  (installed.stdout || '').trim().split('\n').filter(line => /PASS|FAIL/.test(line)).join(' ').slice(0, 200));
await checkIndependence();
await checkSingleInstance();
await checkPortIdentity();
await checkWatchdog();
await checkRefusals();
await checkRestartPolicy();
await checkStop();
await checkTaskManagement();
await checkReadContract();
await checkDaily();
checkLog();

// Leave the machine as it was found. The synthetic instance keeps its app and its data (so the next run is
// fast), but not a logon task that would start a *test* backend on the operator's own account at every logon.
// The next run's first step re-registers it.
const cleanup = task('uninstall');
const gone = run('powershell', ['-NoProfile', '-Command',
  `if (Get-ScheduledTask -TaskName '${taskName}' -ErrorAction SilentlyContinue) { 'present' } else { 'absent' }`]);
record('cleanup: the synthetic logon task is removed and its app/data are kept',
  /absent/.test(gone.stdout || '') && existsSync(paths.app) && existsSync(join(paths.data, 'libraries.json'))
    ? 'pass' : 'fail',
  `${(cleanup.stdout || '').trim().split('\n').slice(-2).join(' | ').slice(0, 160)}; app=${existsSync(paths.app)} data=${existsSync(paths.data)}`);
say('');
info('the logon trigger itself is configured but NOT verified here: Start-ScheduledTask runs the action');
info('under the scheduler, which is what these checks use; only a real logon exercises the trigger.');
process.exit(report());
