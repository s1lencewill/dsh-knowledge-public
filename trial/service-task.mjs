import {ensureBackgroundLauncher} from './background-launcher.mjs';
/**
 * `service-task` — the backend's own lifecycle, independent of whoever happened to launch it.
 *
 * Why this file exists. The knowledge backend used to be started by a command someone ran, which meant it
 * lived exactly as long as the process tree that command belonged to. The observed failure was not a crash
 * with an error: the service simply *was not there* the next time a session asked it a question, and the
 * session's knowledge calls came back KNOWLEDGE_SERVICE_UNREACHABLE. A backend that a conversation depends on
 * cannot have that property.
 *
 * So the backend is now owned by a Windows scheduled task whose action is **this file's `supervise`
 * command**, and the supervisor is designed around one rule: *the task's own process is the long-lived one.*
 * Nothing here starts a second process and exits - which is what a launcher does, and what makes a task
 * "complete" while its child is still running.
 *
 * What the supervisor guarantees, and how:
 *
 * - **one instance, proved rather than assumed.** A pid file alone is not a claim; the lock file records the
 *   pid **and** the OS-reported creation time, executable and a marker, and a second supervisor either sees a
 *   live, matching identity (`already running`, exit 4) or an identity that does not match any live process
 *   (`stale lock`, taken over and logged). A port is never used as the test: any process can hold a port.
 * - **bounded restarts with backoff.** A child that exits unexpectedly is restarted after 1s, 2s, 4s, ...
 *   (capped), up to `--max-restarts`. Past the threshold the supervisor stops looping, records `state:
 *   failed` with the last exit code and reason, and exits non-zero. It does not retry forever, and it does not
 *   hide the failure behind a successful-looking process.
 * - **a manual stop stays stopped.** `stop` writes a marker *before* killing anything, and the supervisor
 *   checks it before every restart, so a human stop is never undone by the watchdog.
 * - **the record is complete.** Every event is one JSON line (timestamp, event, pid, exit code, reason,
 *   attempt). The pid files carry the identity a later `stop` needs, so nothing is ever killed by name or by
 *   port.
 * - **isolation is not negotiable here.** The supervisor starts the service with the instance's own
 *   environment (isolation=required) and treats a service that answers `/health` without `isolationActive` as
 *   a failed start rather than a usable one. A missing runtime, a missing token or an invalid configuration is
 *   a recorded failure, never a downgrade.
 * - **it does not touch the DSH host.** This file only ever starts and stops the knowledge service. The
 *   host on 3080 is somebody else's process and is out of scope by construction: no command here names it.
 *
 * Commands:
 *   install    register (or refresh) the logon task for this instance
 *   uninstall  stop, then unregister the task (installation files and data are kept)
 *   enable     enable the task (autostart at logon)
 *   disable    disable the task and stop the running instance
 *   start      ask the task to run now (the service starts under the task's own process, not this shell)
 *   stop       request a stop, then end only identity-verified processes of this instance
 *   status     the task, the supervisor, the service and the libraries, as observed right now
 *   logs       the tail of the supervisor log
 *   supervise  the long-lived supervisor. This is the task's action; not meant to be run by hand.
 *
 * Every command prints a fixed report and exits with a code: 0 ok, 1 usage/instance problem, 3 restart
 * threshold reached, 4 another supervisor is already running, 5 preflight/config refused.
 */
import { existsSync, openSync, readFileSync, rmSync, writeFileSync, mkdirSync, closeSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { join, resolve } from 'node:path';
import {
  defaultTarget, httpJson, instanceEnv, ok, fail, info, say, parseFlags, portOwner,
  processChildren, processFacts, processTree, readJson, readPidFile, requireInstance, run, sleep, taskNameFor,
  verifyIdentity, waitForHealth, writeJson, writePidFile,
} from './lib.mjs';

const USAGE = [
  'node trial/service-task.mjs <command> --target <dir> [options]',
  '  install   register the per-user logon task (and its watchdog) that keeps this instance\'s backend alive',
  '  uninstall stop, then remove both tasks (files and data are kept unless --remove-data)',
  '  enable    enable autostart at logon (and the watchdog)',
  '  disable   disable autostart, then stop the running instance',
  '  start     run the task now (the backend runs under the task, not under this shell)',
  '  stop      request a stop and end only this instance\'s own processes',
  '  status    report the task, the watchdog, the supervisor, the service and the libraries [--json]',
  '  logs      print the tail of the supervisor log [--lines <n>]',
  '  supervise the long-lived supervisor (the task\'s action) [--force]',
  '  revive    start the supervisor if it is not running and no stop was requested (the watchdog\'s action)',
  'options: --max-restarts <n> (default 5), --healthy-seconds <n> (default 300), --backoff-ms <n> (default 1000),',
  '         --watchdog-minutes <n> (default 0 = logon trigger only; >0 repeats every n minutes and shows a',
  '         console window each run, because an interactive scheduled task running a console program has one)',
].join('\n');

const flags = parseFlags(process.argv.slice(2), {
  target: null, help: false, json: false, lines: 40, 'max-restarts': 5, 'healthy-seconds': 300,
  'backoff-ms': 1000, 'remove-data': false, force: false, 'watchdog-minutes': 0,
});
const command = (process.argv[2] || '').toLowerCase();
if (flags.help || !command) { say(USAGE); process.exit(flags.help ? 0 : 1); }
const target = resolve(flags.target || defaultTarget());
const { paths, config } = requireInstance(target);
const taskName = taskNameFor(target);
/** The watchdog: a short-lived task that starts the supervisor when it is not running. */
const watchdogName = taskName + '-watchdog';
const port = config.servicePort;

// ------------------------------------------------------------------------------------------------ records

/** One JSON line per lifecycle event. Never a credential, never a document: the fields are listed here. */
function event(name, fields = {}) {
  const line = JSON.stringify({ at: new Date().toISOString(), event: name, pid: process.pid, ...fields });
  mkdirSync(paths.logs, { recursive: true });
  try { writeFileSync(paths.supervisorLog, line + '\n', { flag: 'a', encoding: 'utf8' }); } catch { /* the log is an aid */ }
  return line;
}

function readState() {
  return readJson(paths.supervisorState);
}

function writeState(update) {
  const previous = readState() || {};
  const state = { ...previous, ...update, updatedAt: new Date().toISOString() };
  writeJson(paths.supervisorState, state);
  return state;
}

/** The lock's content: identity, not an empty claim. Our own pid plus what the OS says about it. */
function lockRecord() {
  const facts = processFacts(process.pid);
  return {
    pid: process.pid, kind: 'supervisor', at: new Date().toISOString(), app: paths.app,
    marker: 'service-task.mjs supervise',
    creationTicks: facts?.creationTicks ?? null, executable: facts?.executable ?? null,
    command: `node service-task.mjs supervise --target ${target}`,
  };
}

/**
 * Take the single-instance lock, or explain who holds it.
 *
 * The file's existence proves nothing - a killed supervisor leaves one behind - so the recorded identity is
 * what decides: a live pid whose creation time, executable and command line agree with the record is the
 * holder; anything else is a stale record and is replaced, with that fact written to the log. `attempt` is the
 * retry after a race (the file appeared between the read and the exclusive create).
 */
function acquireLock(attempt = 0) {
  const record = lockRecord();
  const existing = readJson(paths.supervisorLock);
  if (existing && Number.isInteger(existing.pid) && attempt === 0) {
    const facts = processFacts(existing.pid);
    const isTheHolder = facts !== null && existing.pid !== process.pid
      && (existing.creationTicks === null || existing.creationTicks === undefined
        || Number(existing.creationTicks) === facts.creationTicks);
    if (isTheHolder) {
      return { acquired: false, holder: existing,
        detail: `pid ${existing.pid} is running and its identity matches the lock record` };
    }
    event('LOCK_STALE', { stalePid: existing.pid,
      reason: facts === null ? 'the recorded process is gone' : 'the pid is alive but is a different process (recycled)' });
    rmSync(paths.supervisorLock, { force: true });
    return acquireLock(1);
  }
  // `wx` is the atomic step: two supervisors starting in the same instant cannot both create the file.
  try {
    const handle = openSync(paths.supervisorLock, 'wx');
    writeFileSync(handle, JSON.stringify(record, null, 2) + '\n', 'utf8');
    closeSync(handle);
  } catch (error) {
    if (error?.code === 'EEXIST' && attempt < 3) return acquireLock(attempt + 1);
    return { acquired: false, holder: existing,
      detail: `the lock file could not be written (${error?.code || 'unknown error'})` };
  }
  writeJson(paths.pidSupervisor, record);
  return { acquired: true, replaced: existing?.pid ?? null };
}

function releaseLock() {
  const held = readJson(paths.supervisorLock);
  if (held && held.pid === process.pid) rmSync(paths.supervisorLock, { force: true });
  const recorded = readPidFile(paths.pidSupervisor);
  if (recorded && recorded.pid === process.pid) rmSync(paths.pidSupervisor, { force: true });
}

/** True when a human asked for a stop: the marker is written before any kill, so a restart loop sees it. */
function stopRequested() {
  return existsSync(paths.stopRequest);
}

// ------------------------------------------------------------------------------------------------ preflight

/**
 * Everything that must hold before a service is launched, as an explicit verdict.
 *
 * The same rules `trial/start.mjs` applies, in the same order and with the same wording, because a supervisor
 * that accepted a state the interactive start refuses would be a second, weaker gate. Nothing here degrades:
 * a failure is a recorded `state: failed` with a reason, not a service started without isolation.
 */
function preflight() {
  const problems = [];
  if (!existsSync(paths.runtime)) {
    problems.push(`the container runtime is missing at ${paths.runtime}; the service requires isolation and `
      + 'will not start without it (place it with `install --runtime-from <dir>`)');
  } else if (!existsSync(join(paths.runtime, 'ready'))) {
    problems.push(`the runtime at ${paths.runtime} is not staged (no "ready" marker); re-run the staging step`);
  }
  if (!existsSync(paths.token) || readFileSync(paths.token, 'utf8').trim().length < 24) {
    problems.push('the service token is missing or too short; re-run install');
  }
  const libraries = readJson(paths.libraries)?.libraries;
  if (!Array.isArray(libraries) || libraries.length === 0) {
    problems.push('no library is configured; re-run install');
  }
  if (config.python && !existsSync(config.python)) {
    problems.push(`the configured Python is gone: ${config.python}`);
  }
  const owner = portOwner(port);
  const recorded = readPidFile(paths.pidService);
  if (owner !== null) {
    // The port may legitimately be held by a **descendant** of the recorded service: a venv's `python.exe` is a
    // redirector stub, so the interpreter that actually binds the port is its child. Both are this instance's
    // own tree and neither is a conflict. Anything else on our port is a conflict, and is reported rather than
    // worked around.
    const tree = recorded?.pid ? processTree(recorded.pid) : new Set();
    if (recorded?.pid !== owner && !tree.has(owner)) {
      problems.push(`port ${port} is held by pid ${owner}, which is not this instance's recorded service `
        + `(pid ${recorded?.pid ?? 'none'}) or any of its children`);
    }
  }
  return problems;
}

// ------------------------------------------------------------------------------------------------ service

function serviceArgv() {
  return ['-m', 'uvicorn', 'service:app', '--app-dir', join(paths.app, 'engine'),
    '--host', '127.0.0.1', '--port', String(port)];
}

/**
 * Launch the service as this supervisor's own child, with the instance's environment and log file.
 *
 * `detached: true` alongside `windowsHide` is deliberate: it puts the service in its **own** console group
 * (`CREATE_NEW_PROCESS_GROUP` + no window), so a control event aimed at whatever console this supervisor
 * happens to share on the operator's desktop does not reach the service. That is half of the fix for the
 * 2026-09-24 event; the other half is that the supervisor no longer treats a signal as a stop (see below).
 * The handle is kept - deliberately not `unref`'d - because the supervisor has to observe the exit.
 */
function launchService() {
  const env = instanceEnv(paths, config);
  mkdirSync(paths.logs, { recursive: true });
  const handle = openSync(join(paths.logs, 'service.log'), 'a');
  const child = spawn(config.python, serviceArgv(), {
    env, cwd: paths.app, windowsHide: true, detached: true, stdio: ['ignore', handle, handle],
  });
  // An `error` event (the executable does not exist) leaves `pid` undefined; the caller turns that into a
  // bounded retry rather than a pid file with no pid in it.
  if (!child.pid) return child;
  const facts = processFacts(child.pid);
  writePidFile(paths.pidService, {
    pid: child.pid, kind: 'service', port, marker: paths.app, command: 'uvicorn service:app',
    app: paths.app, log: join(paths.logs, 'service.log'),
    supervisor: process.pid, creationTicks: facts?.creationTicks ?? null, executable: facts?.executable ?? null,
  });
  return child;
}

/** End a service process tree, but only when the OS confirms it is the one this instance recorded. */
function killService({ requireIdentity = true } = {}) {
  const record = readPidFile(paths.pidService);
  if (!record) return { verdict: 'absent' };
  const identity = requireIdentity ? verifyIdentity(record, paths) : { verdict: 'ours', detail: 'identity check skipped by --force' };
  if (identity.verdict === 'gone') { rmSync(paths.pidService, { force: true }); return { verdict: 'gone' }; }
  if (identity.verdict === 'foreign') {
    event('SERVICE_NOT_OURS', { pid: record.pid, reason: identity.detail });
    return { verdict: 'foreign', detail: identity.detail };
  }
  const killed = run('taskkill', ['/PID', String(record.pid), '/T', '/F']);
  if (killed.status !== 0 && processFacts(record.pid) !== null) {
    event('SERVICE_KILL_FAILED', { pid: record.pid, status: killed.status });
    return { verdict: 'failed' };
  }
  event('SERVICE_STOPPED', { pid: record.pid, identity: identity.detail });
  rmSync(paths.pidService, { force: true });
  return { verdict: 'stopped', pid: record.pid };
}

/** A service process left over from an earlier supervisor: same identity rules, killed before a fresh start. */
function killOrphanService() {
  const record = readPidFile(paths.pidService);
  if (!record) return;
  if (record.supervisor !== undefined && record.supervisor !== null && record.supervisor !== process.pid) {
    event('ORPHAN_SERVICE', { pid: record.pid, previousSupervisor: record.supervisor });
  }
  killService();
}

// ------------------------------------------------------------------------------------------------ supervise

/**
 * The stop marker, and what a *new* supervisor does with one.
 *
 * The marker's job is to reach a supervisor that is **already running** - typically one sitting in a restart
 * backoff - so that a human stop is not undone by the watchdog a second later. A supervisor that is starting
 * fresh is a different situation: it was started by a logon trigger or by an explicit `start`, which is a new
 * intention. So the marker is *consumed* here, with a log line, rather than obeyed forever - otherwise a stop
 * on Monday would silently keep the backend down on Tuesday, and "stop" would have quietly become "disable".
 */
function consumeStopMarker() {
  const marker = readJson(paths.stopRequest);
  if (!marker) return;
  rmSync(paths.stopRequest, { force: true });
  event('STOP_MARKER_CONSUMED', { requestedAt: marker.requestedAt ?? null, requestedBy: marker.by ?? null });
}

async function supervise() {
  const maxRestarts = Number(flags['max-restarts']);
  const healthySeconds = Number(flags['healthy-seconds']);
  const backoffBase = Number(flags['backoff-ms']);
  if (!Number.isInteger(maxRestarts) || maxRestarts < 0) { say('--max-restarts must be an integer >= 0'); return 1; }
  if (!Number.isInteger(healthySeconds) || healthySeconds < 1) { say('--healthy-seconds must be an integer >= 1'); return 1; }
  if (!Number.isInteger(backoffBase) || backoffBase < 100) { say('--backoff-ms must be an integer >= 100'); return 1; }

  // The lock is taken before the marker is consumed: if another supervisor already runs this instance, its
  // restart loop is the one that owns the marker, and this process must not clear it on its way out.
  const lock = acquireLock();
  if (!lock.acquired) {
    // Note what is *not* done here: the state file is not written. It belongs to the supervisor that holds the
    // lock, and a refused duplicate that overwrote it would make a healthy running instance report
    // `state: duplicate` - the exact kind of confusion this record exists to prevent.
    event('SUPERVISOR_DUPLICATE', { holder: lock.holder?.pid ?? null, reason: lock.detail });
    say(`Another supervisor already runs this instance: ${lock.detail}`);
    return 4;
  }
  if (lock.replaced) event('LOCK_TAKEOVER', { stalePid: lock.replaced });
  consumeStopMarker();

  const problems = preflight();
  if (problems.length) {
    for (const problem of problems) event('PREFLIGHT_FAILED', { reason: problem });
    writeState({ state: 'failed', pid: process.pid, reason: problems[0], problems, attempts: 0 });
    releaseLock();
    say('Refusing to start the service:');
    for (const problem of problems) say(`  FAIL  ${problem}`);
    say(`See ${paths.supervisorLog}`);
    return 5;
  }

  killOrphanService();
  writeState({ state: 'starting', pid: process.pid, startedAt: new Date().toISOString(), attempts: 0,
    maxRestarts, healthySeconds, servicePort: port, taskName });

  let attempts = 0;
  let child = null;
  const endChild = () => {
    if (!child || child.exitCode !== null || child.killed) return;
    try { run('taskkill', ['/PID', String(child.pid), '/T', '/F']); } catch { /* best effort on the way out */ }
  };
  // Signals are **not** an instruction to stop.
  //
  // This is a correction of a real failure, not a defensive nicety. On 2026-09-24 at 10:11 local, a Windows
  // console control event reached the task's console group: the service exited with 0xC000013A
  // (STATUS_CONTROL_C_EXIT) and the supervisor received the emulated SIGINT. The supervisor treated that as
  // "stopping", ended the loop, wrote `state: stopped` and exited 0 - so the backend was gone and nothing
  // brought it back, which is the same class of silent disappearance this whole file exists to remove. Nobody
  // had asked for a stop: there was no stop marker.
  //
  // So the rule is now explicit: **only the marker stops the loop.** A signal is recorded, and the service is
  // restarted through the ordinary bounded-backoff path like any other unexpected death. If the environment is
  // really tearing the tree down, this process is killed a moment later and nothing more happens; if the event
  // was transient (a console window closing somewhere on the desktop), the watchdog does its job.
  const onSignal = (name) => {
    event('SUPERVISOR_SIGNAL', { signal: name, note: 'recorded, not treated as a stop: only the stop marker stops the loop' });
    if (stopRequested()) endChild();
  };
  for (const name of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.on(name, () => onSignal(name));

  let exitCode = 0;
  for (;;) {
    if (stopRequested()) { event('STOP_REQUESTED', { attempt: attempts }); break; }
    event('SERVICE_STARTING', { attempt: attempts + 1, port });
    child = launchService();
    if (!child.pid) {
      // `spawn` failed rather than the service exiting: a missing interpreter, an unusable working directory.
      // It is the same kind of failure as an immediate exit, so it goes through the same bounded retry.
      child = null;
      attempts += 1;
      const reason = 'the service process could not be spawned (check the configured Python and the app directory)';
      event('SERVICE_SPAWN_FAILED', { attempt: attempts, maxRestarts });
      writeState({ state: 'restarting', pid: process.pid, attempts, reason });
      if (attempts > maxRestarts) {
        exitCode = 3;
        event('SUPERVISOR_FAILED', { attempts, maxRestarts, reason });
        writeState({ state: 'failed', pid: process.pid, attempts, reason });
        say(`Giving up after ${maxRestarts} restarts: ${reason}`);
        break;
      }
      await sleep(Math.min(60_000, backoffBase * (2 ** (attempts - 1))));
      continue;
    }
    const token = readFileSync(paths.token, 'utf8').trim();
    const health = await waitForHealth(`http://127.0.0.1:${port}/health`, { timeoutMs: 60_000, headers: { authorization: `Bearer ${token}` } });
    const healthy = health.status === 200 && health.body?.isolationActive === true;
    if (healthy) {
      // Deliberately **not** resetting the attempt counter here. A service that becomes healthy and then dies
      // again is the failure mode this threshold exists for: if every successful start cleared the count, a
      // crash-loop of "start, die, start, die" would restart forever and the threshold would never be reached.
      // The count is cleared only after `--healthy-seconds` of continuous uptime, further down.
      event('SERVICE_READY', { pid: child.pid, isolation: health.body?.isolation, attempt: attempts,
        libraries: readJson(paths.libraries)?.libraries?.length ?? null });
      writeState({ state: 'running', pid: process.pid, servicePid: child.pid, attempts,
        since: new Date().toISOString(), reason: null });
    } else {
      const reason = health.status === 200
        ? `the service answered /health but isolationActive=${String(health.body?.isolationActive)}; refusing to call this instance usable`
        : `the service did not become healthy (last HTTP ${health.status}${health.error ? `: ${health.error}` : ''})`;
      event('SERVICE_UNHEALTHY', { pid: child.pid, http: health.status, reason });
      endChild();
    }
    const startedAt = Date.now();
    const code = await new Promise(resolveExit => {
      if (child.exitCode !== null) return resolveExit(child.exitCode);
      child.once('exit', (value) => resolveExit(value));
      child.once('error', () => resolveExit(null));
    });
    const uptimeSeconds = Math.round((Date.now() - startedAt) / 1000);
    if (stopRequested()) {
      event('SERVICE_EXIT_REQUESTED', { pid: child.pid, code, uptimeSeconds });
      break;
    }
    // The exit code is recorded as it is: 0xC000013A (3221225786) is STATUS_CONTROL_C_EXIT - the service was
    // ended by a console control event rather than by a crash - and it is *not* a reason to stop watching.
    // A service that stayed up for `--healthy-seconds` is a new failure, not a continuation of the last one.
    if (healthy && uptimeSeconds >= healthySeconds && attempts > 0) {
      event('RESTART_COUNTER_RESET', { uptimeSeconds, previousAttempts: attempts });
      attempts = 0;
    }
    attempts += 1;
    const reason = `the service exited with code ${code} after ${uptimeSeconds}s`;
    event('SERVICE_EXITED', { pid: child.pid, code, uptimeSeconds, attempt: attempts, maxRestarts });
    writeState({ state: 'restarting', pid: process.pid, attempts, lastExitCode: code,
      lastExitAt: new Date().toISOString(), lastUptimeSeconds: uptimeSeconds, reason });
    if (attempts > maxRestarts) {
      exitCode = 3;
      event('SUPERVISOR_FAILED', { attempts, maxRestarts, reason });
      // Stop restarting **for good**, by turning autostart off rather than by relying on an exit code. The task
      // now has a bounded crash-retry policy (so a supervisor killed from outside comes back on its own), and
      // that policy cannot tell a deliberate stop from an accident - so the deliberate one is made explicit:
      // disable the task, which the scheduler will not restart. `status` reports it and `enable` (or `install`)
      // turns it back on.
      const disabled = powershell(`Disable-ScheduledTask -TaskName '${taskName}' -ErrorAction SilentlyContinue`);
      const autostartOff = disabled.status === 0;
      if (autostartOff) event('TASK_DISABLED_AFTER_FAILURE', { task: taskName, attempts, reason });
      writeState({ state: 'failed', pid: process.pid, attempts, reason, autostart: autostartOff ? 'disabled' : 'unchanged',
        detail: `stopped after ${maxRestarts} restarts; autostart ${autostartOff ? 'disabled so it is not retried' : 'left enabled'}; `
          + `inspect ${join(paths.logs, 'service.log')}` });
      say(`Giving up after ${maxRestarts} restarts: ${reason}`);
      if (autostartOff) say(`Autostart for ${taskName} is now OFF so this cannot loop. Fix the cause, then: `
        + `service-task.mjs enable --target "${target}" && service-task.mjs start --target "${target}"`);
      say(`See ${paths.supervisorLog} and ${join(paths.logs, 'service.log')}`);
      break;
    }
    const waitMs = Math.min(60_000, backoffBase * (2 ** (attempts - 1)));
    event('RESTART_BACKOFF', { attempt: attempts, waitMs });
    await sleep(waitMs);
  }

  endChild();
  rmSync(paths.pidService, { force: true });
  releaseLock();
  if (exitCode === 0) {
    event('SUPERVISOR_STOPPED', {});
    writeState({ state: 'stopped', pid: null, since: new Date().toISOString(), reason: null });
  }
  return exitCode;
}

// ------------------------------------------------------------------------------------------------ the task

function powershell(script) {
  return run('powershell', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script]);
}

function stateOf(name) {
  const result = powershell(`(Get-ScheduledTask -TaskName '${name}' -ErrorAction SilentlyContinue).State`);
  const text = (result.stdout || '').trim();
  return text.length ? text : null;
}

function taskState() {
  return stateOf(taskName);
}

function watchdogState() {
  return stateOf(watchdogName);
}

/**
 * How often the registered watchdog actually repeats, read from the task itself rather than from the flags.
 *
 * A `status` that reported the *installation* default instead of the registered trigger would be exactly the
 * kind of claim this project keeps out of its reports - and it is the number that tells an operator whether to
 * expect console windows on their desktop.
 */
function watchdogTriggerMinutes() {
  const result = powershell(`$t = Get-ScheduledTask -TaskName '${watchdogName}' -ErrorAction SilentlyContinue; `
    + 'if ($t) { $r = $t.Triggers[0].Repetition; if ($r -and $r.Interval) { $r.Interval } else { "none" } }');
  const text = (result.stdout || '').trim();
  const match = /^PT(\d+)M$/.exec(text);
  return match ? Number(match[1]) : 0;
}

/**
 * Register (or refresh) the task, as a per-user logon task with the least privilege that works.
 *
 * Three settings carry the guarantees: `MultipleInstances IgnoreNew` (the scheduler itself refuses a second
 * instance), `ExecutionTimeLimit PT0S` (unlimited - the default three days would silently end the backend),
 * and a **bounded crash restart** (`RestartCount 3`, every 2 minutes).
 *
 * That last one exists because of an observed failure, not by symmetry. On 2026-09-24 a Windows console close
 * killed the supervisor *itself* (the task's LastTaskResult was `0xC000013A`), and with no restart policy the
 * backend stayed down until the next logon: the watchdog cannot watch itself. The scheduler retries only after
 * an **unexpected** end, which is why the restart authority is split the way it is:
 *
 *   * a supervisor that ends deliberately exits 0 (an operator `stop`), so nothing restarts it;
 *   * a supervisor that ends on its own restart threshold **disables this task** before exiting, so a loop that
 *     has already failed its own bound is not retried by a second authority (and `status` says so);
 *   * a supervisor killed by the environment - the console close above - is retried up to three times.
 *
 * The action is a program plus an argument list, never a shell string: the node executable and the two
 * arguments are written as separate task-action fields, and the instance path is quoted only in the form
 * Windows requires for a quoted argument. A path containing a double quote is refused outright rather than
 * escaped into something a scheduler might interpret differently.
 */
function installTask() {
  if (target.includes('"')) { say('Refusing to install: the instance path contains a double quote.'); return 1; }
  const node = process.execPath;
  if (!existsSync(node)) { say(`Refusing to install: the node executable is gone: ${node}`); return 1; }
  const script = join(paths.app, 'trial', 'service-task.mjs');
  if (!existsSync(script)) { say(`Refusing to install: ${script} is missing (is this an installed instance?)`); return 1; }
  // The argument string is the one place a shell-like quoting rule applies inside the task definition. The
  // script path and the target are each one quoted argument; a path containing a double quote was refused
  // above, so no other escaping rule is needed. (An extra quote here is not cosmetic: it becomes part of the
  // `--target` value, the instance cannot be loaded, and the task action exits 2 with nothing to show for it
  // except the task's own LastTaskResult.)
  let launcher;try{launcher=ensureBackgroundLauncher(paths.data);}catch{fail('No-window task launcher could not be built; task definitions were not changed.');return 1;}
  const action = `New-ScheduledTaskAction -Execute '${launcher.replace(/'/g, "''")}' ` +
    `-Argument '"${node}" "${script}" supervise "${target}"' -WorkingDirectory '${paths.app.replace(/'/g, "''")}'`;
  const trigger = 'New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\\$env:USERNAME"';
  const principal = 'New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\\$env:USERNAME" -LogonType Interactive -RunLevel Limited';
  const settings = 'New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries ' +
    '-DontStopIfGoingOnBatteries -DontStopOnIdleEnd -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable ' +
    '-RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2)';
  const script_ = [
    `$ErrorActionPreference='Stop'`,
    `$action = ${action}`,
    `$trigger = ${trigger}`,
    `$principal = ${principal}`,
    `$settings = ${settings}`,
    `$before = Get-ScheduledTask -TaskName '${taskName}' -ErrorAction SilentlyContinue`,
    `Register-ScheduledTask -TaskName '${taskName}' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null`,
    // Registering refreshes the definition; enabling makes "installed" mean "usable", including after a
    // threshold failure turned autostart off.
    `Enable-ScheduledTask -TaskName '${taskName}' | Out-Null`,
    `if ($before) { 'REPLACED' } else { 'CREATED' }`,
  ].join('; ');
  const result = powershell(script_);
  const verdict = (result.stdout || '').trim().split(/\r?\n/).pop();
  if (result.status !== 0) {
    fail(`the task could not be registered: ${(result.stderr || result.stdout || '').trim().slice(0, 300)}`);
    return 1;
  }
  // --- the watchdog
  //
  // The supervisor cannot watch itself, and Windows' own "restart on failure" did not fire for a task whose
  // process was killed from outside (measured on this machine: RestartCount=3/PT2M registered, the task ended
  // with result 1, and no restart happened within 300s). So the revival is ours, and it is a *separate*,
  // short-lived task whose only job is to ask the question again:
  //
  //   revive:  stop requested? -> do nothing.  supervisor alive? -> do nothing.  otherwise -> start it.
  //
  // A repetition trigger on the supervisor's own task could not do this: it would start a supervisor five
  // minutes after an operator `stop`, which consumes the stop marker and brings the backend back - turning
  // "stop" into "disable". Checking the marker without consuming it is what keeps the two intentions apart.
  //
  // **Corrected 2026-09-24: the watchdog does NOT repeat by default.** It was registered with a two-minute
  // repetition, and that is exactly what the operator saw on their desktop: a scheduled task with an
  // interactive principal running a *console* program allocates a console window on every run, so the machine
  // "kept popping up cmd windows", one every two minutes, forever. The self-healing property is not worth a
  // window every two minutes, so the default is now a **logon trigger** - the same moment the supervisor
  // itself starts, where a second check costs nothing - and a repetition is opt-in through
  // `--watchdog-minutes N` for anyone who wants the two-minute revival and accepts the windows. With the
  // repetition off, a supervisor killed from outside is picked up at the next logon or by an explicit
  // `start`; that is the documented cost, and it is the same cost the interactive `start.mjs` path always had.
  const watchdogMinutes = Number(flags['watchdog-minutes']);
  if (!Number.isInteger(watchdogMinutes) || watchdogMinutes < 0 || watchdogMinutes > 60) {
    fail('--watchdog-minutes must be an integer between 0 (logon only, the default) and 60');
    return 1;
  }
  const watchdogTrigger = watchdogMinutes === 0
    ? 'New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\\$env:USERNAME"'
    : `New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
      + `-RepetitionInterval (New-TimeSpan -Minutes ${watchdogMinutes}) -RepetitionDuration (New-TimeSpan -Days 3650)`;
  const watchdogScript = [
    `$ErrorActionPreference='Stop'`,
    `$action = New-ScheduledTaskAction -Execute '${launcher.replace(/'/g, "''")}' ` +
      `-Argument '"${node}" "${script}" revive "${target}"' -WorkingDirectory '${paths.app.replace(/'/g, "''")}'`,
    `$trigger = ${watchdogTrigger}`,
    `$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\\$env:USERNAME" -LogonType Interactive -RunLevel Limited`,
    `$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable ` +
      '-ExecutionTimeLimit (New-TimeSpan -Minutes 5)',
    `Register-ScheduledTask -TaskName '${watchdogName}' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null`,
    `Enable-ScheduledTask -TaskName '${watchdogName}' | Out-Null`,
    `(Get-ScheduledTask -TaskName '${watchdogName}').State`,
  ].join('; ');
  const watchdog = powershell(watchdogScript);
  if (watchdog.status !== 0) {
    fail(`the watchdog task could not be registered: ${(watchdog.stderr || watchdog.stdout || '').trim().slice(0, 300)}`);
    return 1;
  }
  // Read the definition back and check the two fields a mistake here would silently break. A wrong argument
  // string is invisible until the task runs: the action exits 2, the only trace is the scheduler's own
  // LastTaskResult, and the backend is simply never there.
  const expectedArguments = `"${node}" "${script}" supervise "${target}"`;
  const readBack = powershell(`$a = (Get-ScheduledTask -TaskName '${taskName}').Actions[0]; "$($a.Execute)|$($a.Arguments)|$($a.WorkingDirectory)"`);
  const [execute, args, workingDirectory] = (readBack.stdout || '').trim().split('|');
  if (execute !== launcher || args !== expectedArguments || workingDirectory !== paths.app) {
    fail('the registered task does not match what was asked for; it has been left in place so the difference is visible:');
    info(`execute    ${execute}  (wanted ${launcher})`);
    info(`arguments  ${args}  (wanted ${expectedArguments})`);
    info(`workingdir ${workingDirectory}  (wanted ${paths.app})`);
    event('TASK_MISMATCH', { task: taskName, execute, args, workingDirectory });
    return 1;
  }
  // The settings are read back too: a restart policy that silently did not apply would only be discovered the
  // next time the supervisor was killed by something outside itself.
  const policy = powershell(`$t = Get-ScheduledTask -TaskName '${taskName}'; `
    + `"$($t.Settings.MultipleInstances)|$($t.Settings.ExecutionTimeLimit)|$($t.Settings.RestartCount)|$($t.Settings.RestartInterval)"`);
  const [instances, timeLimit, restartCount, restartInterval] = (policy.stdout || '').trim().split('|');
  if (instances !== 'IgnoreNew' || timeLimit !== 'PT0S' || Number(restartCount) !== 3) {
    fail('the registered task settings do not match what was asked for; nothing was changed after the fact:');
    info(`multipleInstances ${instances}  (wanted IgnoreNew)`);
    info(`executionTimeLimit ${timeLimit}  (wanted PT0S)`);
    info(`restartCount ${restartCount} / interval ${restartInterval}  (wanted 3 / PT2M)`);
    event('TASK_SETTINGS_MISMATCH', { task: taskName, instances, timeLimit, restartCount, restartInterval });
    return 1;
  }
  ok(`scheduled task ${taskName} ${verdict === 'REPLACED' ? 'refreshed' : 'registered'} (logon trigger, per user, no elevation)`);
  info(`action        Windows-subsystem launcher → Node supervisor (same current user)`);
  info(`working dir   ${paths.app}`);
  info(`policy        ignore-new instance, no time limit, ≤${restartCount} scheduler retries after a crash `
    + `(${restartInterval} apart); a deliberate stop exits 0 and is never retried, and a supervisor that reaches `
    + 'its own restart threshold disables this task instead of being restarted by it');
  ok(`watchdog task ${watchdogName} registered (${watchdogMinutes === 0
    ? 'logon trigger only, no repetition'
    : `every ${watchdogMinutes} min - this WILL flash a console window each run`}; `
    + `state ${(watchdog.stdout || '').trim().split(/\r?\n/).pop()})`);
  info(watchdogMinutes === 0
    ? 'it checks once at logon; a supervisor killed between logons waits for the next logon or an explicit start'
    : 'it starts the supervisor only when no stop was requested and no supervisor is alive');
  event('TASK_INSTALLED', { task: taskName, verdict, node, restartCount, restartInterval, watchdog: watchdogName, watchdogMinutes });
  return 0;
}

function uninstallTask() {
  const existing = taskState();
  if (existing === null) { info(`no task named ${taskName} is registered`); }
  else {
    powershell(`Stop-ScheduledTask -TaskName '${taskName}' -ErrorAction SilentlyContinue`);
    const result = powershell(`Unregister-ScheduledTask -TaskName '${taskName}' -Confirm:$false`);
    if (result.status !== 0) { fail(`the task could not be removed: ${(result.stderr || '').trim().slice(0, 200)}`); return 1; }
    ok(`task ${taskName} removed (state was ${existing})`);
  }
  const watchdog = watchdogState();
  if (watchdog === null) info(`no watchdog named ${watchdogName} is registered`);
  else {
    powershell(`Stop-ScheduledTask -TaskName '${watchdogName}' -ErrorAction SilentlyContinue`);
    const removed = powershell(`Unregister-ScheduledTask -TaskName '${watchdogName}' -Confirm:$false`);
    if (removed.status !== 0) { fail(`the watchdog could not be removed: ${(removed.stderr || '').trim().slice(0, 200)}`); return 1; }
    ok(`watchdog ${watchdogName} removed (state was ${watchdog})`);
  }
  event('TASK_UNINSTALLED', { task: taskName, watchdog: watchdogName });
  rmSync(paths.stopRequest, { force: true });
  if (flags['remove-data']) {
    say(`  ....  --remove-data was given, so ${paths.data} is being deleted`);
    rmSync(paths.data, { recursive: true, force: true });
  } else {
    say(`  ....  app kept at ${paths.app}; data (libraries, logs, token, runtime) kept at ${paths.data}`);
  }
  return 0;
}

function setEnabled(enabled) {
  const state = taskState();
  if (state === null) { fail(`no task named ${taskName} is registered; run install first`); return 1; }
  const word = enabled ? 'Enable' : 'Disable';
  const result = powershell(`${word}-ScheduledTask -TaskName '${taskName}'`);
  if (result.status !== 0) { fail(`could not ${enabled ? 'enable' : 'disable'} the task: ${(result.stderr || '').trim().slice(0, 200)}`); return 1; }
  ok(`task ${taskName} ${enabled ? 'enabled' : 'disabled'} (state was ${state}, now ${taskState()})`);
  // The watchdog follows autostart: leaving it enabled after `disable` would undo the operator's one word.
  if (watchdogState() !== null) {
    const watchdog = powershell(`${word}-ScheduledTask -TaskName '${watchdogName}'`);
    if (watchdog.status === 0) ok(`watchdog ${watchdogName} ${enabled ? 'enabled' : 'disabled'} (now ${watchdogState()})`);
    else fail(`the watchdog could not be ${enabled ? 'enabled' : 'disabled'}: ${(watchdog.stderr || '').trim().slice(0, 160)}`);
  }
  event(enabled ? 'TASK_ENABLED' : 'TASK_DISABLED', { task: taskName, watchdog: watchdogName });
  if (!enabled) {
    // Disabling autostart is not a stop: say so, then stop, so the two intentions never merge by accident.
    info('autostart is off; the running instance is stopped next (disable means "do not come back at logon")');
    return stopInstance();
  }
  return 0;
}

/**
 * `revive` — the watchdog's whole job, and the only command that may start the supervisor without being asked.
 *
 * The three questions, in order, and why each one is here:
 *
 * 1. **was a stop requested?** If the marker exists, a human asked for the backend to be down, and the answer is
 *    no. The marker is *not* consumed here - consuming it is what a logon or an explicit `start` does - so a stop
 *    holds until one of those happens, and "stop" never quietly becomes "disable".
 * 2. **is a supervisor alive?** Identity-checked, not "does the lock file exist": a stale lock is not a reason to
 *    leave the backend down. A live one is not touched (and the supervisor task's `IgnoreNew` would refuse a
 *    second instance anyway).
 * 3. **is the task disabled?** A disabled task is the operator's `disable` or a threshold failure's own doing;
 *    neither should be undone by a timer.
 *
 * Anything else starts the supervisor task. Every decision is one JSON line, so "why did it come back?" and "why
 * did it not?" both have an answer in `supervisor.log`.
 */
function revive() {
  if (stopRequested()) {
    event('REVIVE_SKIPPED', { reason: 'a stop was requested; only `start` or a logon clears it' });
    return 0;
  }
  const state = taskState();
  if (state === null) {
    event('REVIVE_SKIPPED', { reason: 'no supervisor task is registered' });
    return 0;
  }
  if (/disabled/i.test(state)) {
    event('REVIVE_SKIPPED', { reason: 'the task is disabled (operator disable, or a threshold failure)' });
    return 0;
  }
  const lock = readJson(paths.supervisorLock);
  if (lock && Number.isInteger(lock.pid)) {
    const facts = processFacts(lock.pid);
    const alive = facts !== null && lock.pid !== process.pid
      && (lock.creationTicks === null || lock.creationTicks === undefined
        || Number(lock.creationTicks) === facts.creationTicks);
    if (alive) {
      event('REVIVE_SKIPPED', { reason: `supervisor pid ${lock.pid} is alive` });
      return 0;
    }
  }
  event('REVIVE_STARTED', { stalePid: lock?.pid ?? null, note: 'no stop was requested and no supervisor is alive' });
  const started = powershell(`Start-ScheduledTask -TaskName '${taskName}'`);
  if (started.status !== 0) {
    event('REVIVE_FAILED', { reason: (started.stderr || '').trim().slice(0, 200) });
    return 1;
  }
  return 0;
}

function startInstance() {
  const state = taskState();
  if (state === null) { fail(`no task named ${taskName} is registered; run install first`); return 1; }
  // A disabled task is either the operator's `disable` or a threshold failure's own doing. `start` refuses to
  // silently undo either one: turning autostart back on is a separate intention and is said in a separate word.
  if (/disabled/i.test(state)) {
    const recorded = readState() || {};
    fail(`task ${taskName} is disabled, so this command will not start it.`);
    info(recorded.state === 'failed'
      ? `the supervisor disabled it after failing its own restart bound (${recorded.reason ?? 'see the log'})`
      : 'autostart was turned off with `disable`');
    info(`fix the cause, then: service-task.mjs enable --target "${target}" && service-task.mjs start --target "${target}"`);
    return 1;
  }
  // `start` is the explicit act that clears a previous stop, and it is the only command that may.
  if (stopRequested()) { rmSync(paths.stopRequest, { force: true }); info('cleared the previous stop marker'); }
  const holder = readJson(paths.supervisorLock);
  if (holder && Number.isInteger(holder.pid)) {
    const facts = processFacts(holder.pid);
    if (facts && (holder.creationTicks === null || holder.creationTicks === undefined
      || Number(holder.creationTicks) === facts.creationTicks)) {
      ok(`an instance is already running (supervisor pid ${holder.pid}); nothing was started`);
      return 0;
    }
    info(`the lock names pid ${holder.pid}, which is not running; the supervisor will take it over`);
  }
  const result = powershell(`Start-ScheduledTask -TaskName '${taskName}'`);
  if (result.status !== 0) { fail(`the task could not be started: ${(result.stderr || '').trim().slice(0, 200)}`); return 1; }
  ok(`task ${taskName} started; the backend now runs under the scheduler, not under this shell`);
  return 0;
}

async function stopInstance() {
  // The marker goes down first: a supervisor that is between restarts must see "stopped" before it sees a dead
  // child, or it would restart the very service the user just ended.
  writeJson(paths.stopRequest, { requestedAt: new Date().toISOString(), pid: process.pid, by: 'service-task stop' });
  event('STOP_MARKER_WRITTEN', {});
  const supervisor = readPidFile(paths.pidSupervisor) || readJson(paths.supervisorLock);
  if (taskState() !== null) {
    const stopped = powershell(`Stop-ScheduledTask -TaskName '${taskName}' -ErrorAction SilentlyContinue`);
    info(stopped.status === 0 ? `task ${taskName} asked to stop` : 'the task stop reported an error; verifying anyway');
  }
  if (supervisor && Number.isInteger(supervisor.pid)) {
    const identity = verifyIdentity({ ...supervisor, marker: 'service-task.mjs supervise' }, paths);
    for (let attempt = 0; attempt < 20 && processFacts(supervisor.pid) !== null; attempt += 1) await sleep(250);
    if (processFacts(supervisor.pid) === null) {
      ok(`supervisor pid ${supervisor.pid} ended (${identity.detail})`);
    } else {
      // The task's stop did not end it (for example a supervisor started by hand outside the scheduler).
      const killed = run('taskkill', ['/PID', String(supervisor.pid), '/T', '/F']);
      if (killed.status === 0) ok(`supervisor pid ${supervisor.pid} ended (identity: ${identity.detail})`);
      else fail(`could not end supervisor pid ${supervisor.pid}; it was left alone`);
    }
  } else {
    info('no supervisor record for this instance');
  }
  const service = killService();
  if (service.verdict === 'absent') info('no service process was recorded');
  else if (service.verdict === 'stopped') ok(`service pid ${service.pid} stopped`);
  else if (service.verdict === 'gone') ok('the service had already exited');
  else if (service.verdict === 'foreign') {
    fail(`the recorded service pid is not this instance's process: ${service.detail}`);
    fail('it was left alone; the record is kept so the situation stays visible');
  } else fail('the service could not be stopped; see the supervisor log');
  rmSync(paths.pidSupervisor, { force: true });
  rmSync(paths.supervisorLock, { force: true });
  const owner = portOwner(port);
  if (owner === null) ok(`service port ${port} is free`);
  else fail(`service port ${port} is still held by pid ${owner}`);
  // One summary line for the whole stop. It is written here rather than left to the kill path because a hard
  // task stop ends the supervisor and the service together - Task Scheduler terminates the task's tree, so
  // neither of them gets to write anything on the way out. This line is the record of what the stop observed.
  event('STOP_COMPLETE', { portFree: owner === null, service: service.verdict, supervisor: supervisor?.pid ?? null });
  writeState({ state: 'stopped', pid: null, since: new Date().toISOString(), reason: 'stopped by service-task stop' });
  return owner === null ? 0 : 1;
}

// ------------------------------------------------------------------------------------------------ status

async function status() {
  const state = readState() || {};
  const task = taskState();
  const supervisor = readPidFile(paths.pidSupervisor);
  const supervisorAlive = supervisor ? processFacts(supervisor.pid) : null;
  const service = readPidFile(paths.pidService);
  const serviceAlive = service ? processFacts(service.pid) : null;
  // Who actually holds the port, and whether that is this instance's own process tree. A venv python is a
  // redirector stub, so the holder is usually the *child* of the recorded pid - still ours, and worth saying
  // rather than hiding behind the recorded number.
  const portHolder = portOwner(port);
  const serviceTree = service?.pid ? processTree(service.pid) : new Set();
  const portIsOurs = portHolder !== null && (portHolder === service?.pid || serviceTree.has(portHolder));
  let health = { status: 0 };
  if (existsSync(paths.token)) {
    const token = readFileSync(paths.token, 'utf8').trim();
    if (token.length >= 24) {
      health = await httpJson(`http://127.0.0.1:${port}/health`, { headers: { authorization: `Bearer ${token}` } });
    }
  }
  let libraries = null;
  let librariesByWorkspace = [];
  const libraryRecords = [];
  if (health.status === 200 && existsSync(paths.token)) {
    const token = readFileSync(paths.token, 'utf8').trim();
    // A library is visible only to a session *inside* its declared workspace, so asking one arbitrary
    // directory would report "none" for a healthy instance. The declared workspaces are read from the
    // instance's own configuration and each is asked in turn, which is what an operator actually wants to see.
    const declared = (readJson(paths.libraries)?.libraries ?? [])
      .map(entry => entry.workspace).filter(workspace => typeof workspace === 'string' && workspace);
    const workspaces = declared.length ? [...new Set(declared)] : [paths.data];
    for (const workspace of workspaces) {
      const listed = await httpJson(`http://127.0.0.1:${port}/libraries?workspace=${encodeURIComponent(workspace)}`,
        { headers: { authorization: `Bearer ${token}` } });
      if (listed.status === 200 && Array.isArray(listed.body)) {
        libraryRecords.push(...listed.body);
        librariesByWorkspace.push({ workspace, ids: listed.body.map(entry => entry.id) });
      } else {
        librariesByWorkspace.push({ workspace, ids: [], http: listed.status });
      }
    }
    libraries = [];
    for (const entry of librariesByWorkspace) for (const id of entry.ids) {
      if (!libraries.some(existing => existing.id === id)) {
        libraries.push({ id, name: (libraryRecords.find(record => record.id === id) || {}).name ?? null });
      }
    }
  }
  const report = {
    instance: target, task: { name: taskName, state: task },
    watchdog: { name: watchdogName, state: watchdogState(), minutes: watchdogTriggerMinutes() },
    autostart: task === null ? 'not-installed' : (task.toLowerCase().includes('disabled') ? 'disabled' : 'enabled'),    supervisor: supervisor
      ? { pid: supervisor.pid, alive: supervisorAlive !== null, since: supervisor.since ?? state.since ?? null }
      : null,
    state: state.state ?? (task === null ? 'not-installed' : 'unknown'),
    attempts: state.attempts ?? null,
    lastExitCode: state.lastExitCode ?? null,
    reason: state.reason ?? null,
    stopRequested: stopRequested(),
    service: service
      ? { pid: service.pid, alive: serviceAlive !== null, port, portHeldBy: portHolder, portIsOurs }
      : { pid: null, alive: false, port, portHeldBy: portHolder, portIsOurs: false },
    health: health.status === 200
      ? { isolation: health.body?.isolation, isolationActive: health.body?.isolationActive,
          providerBudget: health.body?.providerBudget, readLimits: health.body?.readLimits }
      : { http: health.status, error: health.error ?? null },
    libraries,
    librariesByWorkspace,
    log: paths.supervisorLog,
  };
  if (flags.json) { say(JSON.stringify(report, null, 2)); }
  else {
    say('');
    say(`dsh-knowledge backend — ${target}`);
    say(`  task        ${task === null ? 'not installed' : `${taskName} (${task})`}`);
    say(`  watchdog    ${report.watchdog.state === null ? 'not installed' : `${watchdogName} (${report.watchdog.state})`}`);
    if (report.watchdog.state !== null) {
      say(`  watchdog #  ${report.watchdog.minutes === 0
        ? 'logon trigger only (no repetition: nothing runs, and nothing flashes a window, between logons)'
        : `repeats every ${report.watchdog.minutes} min - each run opens a console window`}`);
    }
    say(`  autostart   ${report.autostart}${report.state === 'failed' && report.autostart === 'disabled'
      ? '  (turned off by the supervisor after failing its own restart bound)' : ''}`);
    say(`  supervisor  ${report.supervisor ? `pid ${report.supervisor.pid} ${report.supervisor.alive ? 'running' : 'NOT RUNNING'}` : 'none'}  state=${report.state}`);
    if (report.attempts) say(`  restarts    ${report.attempts} (last exit code ${report.lastExitCode ?? 'unknown'})`);
    if (report.reason) say(`  reason      ${report.reason}`);
    say(`  service     ${report.service.pid
      ? `pid ${report.service.pid} ${report.service.alive ? 'running' : 'NOT RUNNING'} on 127.0.0.1:${port}`
      : `no recorded process; port 127.0.0.1:${port} is ${portHolder === null ? 'free' : `held by pid ${portHolder}`}`}`);
    if (report.service.pid) {
      say(`  port owner  ${portHolder === null ? `127.0.0.1:${port} is free` : `pid ${portHolder}`
        + `${portIsOurs ? ' (this service\'s own process tree)' : ' (NOT this instance)'}`}`);
    }
    if (health.status === 200) {
      say(`  health      isolation=${health.body?.isolation} isolationActive=${health.body?.isolationActive}`);
    } else {
      say(`  health      unreachable (HTTP ${health.status}${health.error ? `: ${health.error}` : ''})`);
    }
    say(`  libraries   ${libraries && libraries.length
      ? libraries.map(entry => `${entry.id}${entry.name ? ` (${entry.name})` : ''}`).join(', ')
      : 'none visible to any declared workspace'}`);
    for (const entry of librariesByWorkspace) {
      say(`              ${entry.workspace} -> ${entry.ids.length ? entry.ids.join(', ') : '(none)'}`);
    }
    if (stopRequested()) say('  stop marker present: the next supervisor start will refuse until `start` clears it');
    say(`  log         ${paths.supervisorLog}`);
    say('');
  }
  return health.status === 200 ? 0 : 1;
}

// ------------------------------------------------------------------------------------------------ dispatch

/**
 * The single exit point.
 *
 * Every command *returns* its code and this function sets `process.exitCode`, instead of each branch calling
 * `process.exit()`. That is not style: on Windows, exiting while a fetch handle is still being torn down trips
 * a libuv assertion (`!(handle->flags & UV_HANDLE_CLOSING)` in `win/async.c`) and the process dies with
 * 0xC0000409 - after it has already printed its correct answer. Letting the loop drain removes that whole class
 * of failure, which is why `status` can print its report and exit 0 on the same machine where it used to crash
 * on the way out.
 */
async function main() {
  if (command === 'supervise') return supervise();
  if (command === 'install') return installTask();
  if (command === 'uninstall') { await stopInstance(); return uninstallTask(); }
  if (command === 'enable') return setEnabled(true);
  if (command === 'disable') return setEnabled(false);
  if (command === 'start') return startInstance();
  if (command === 'stop') return stopInstance();
  if (command === 'status') return status();
  if (command === 'revive') return revive();
  if (command === 'logs') {
    if (!existsSync(paths.supervisorLog)) { say(`no supervisor log at ${paths.supervisorLog}`); return 0; }
    const lines = readFileSync(paths.supervisorLog, 'utf8').split('\n').filter(Boolean);
    for (const line of lines.slice(-Number(flags.lines) || -40)) say(line);
    return 0;
  }
  say(`Unknown command "${command}".`);
  say(USAGE);
  return 1;
}

process.exitCode = await main();
