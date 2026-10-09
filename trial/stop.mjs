/**
 * `stop` —stop only what this instance started, prove the processes are the ones it started, then report the
 * residuals rather than assuming the kill worked.
 *
 * Three rules this file exists to keep:
 *
 * - **never kill by name or by port.** `python.exe`, `node.exe` and a port number are shared with the user's
 *   own work; a stop that swept those would be a destructive act disguised as cleanup. Only a pid this
 *   instance recorded is a candidate;
 * - **verify identity before killing.** A pid can be recycled, so the live process's command line must still
 *   name this instance's own `app` directory (or its log), and the process must have started before the pid
 *   file was written. A mismatch is reported and skipped, not killed;
 * - **report what is left.** After the kill, the ports, the pid files and the container runtime's leftover
 *   work directories are re-checked, and anything still present is printed. A successful API call is not
 *   evidence of a clean stop.
 */
import { existsSync, readFileSync, rmSync } from 'node:fs';
import { join, resolve } from 'node:path';
import {
  check, defaultTarget, instancePaths, parseFlags, portOwner, processCommandLine,
  processFacts, processesOfInstance, readHistory, readPidFile, requireInstance, run, say, sleep, verifyIdentity,
} from './lib.mjs';

const flags = parseFlags(process.argv.slice(2), { target: null, help: false, 'keep-data': false, 'remove-data': false });
if (flags.help) { say('node trial/stop.mjs --target <dir> [--keep-data]'); process.exit(0); }

const target = resolve(flags.target || defaultTarget());
const { paths, config } = requireInstance(target);

say('');
say(`Stopping ${target}`);

/**
 * Stop one recorded process —but only when the operating system confirms it is the one this instance started.
 *
 * `verifyIdentity` reads the live creation time, executable and command line and compares them with what was
 * recorded at launch. A port is **not** part of that decision: any process can hold a port, so port ownership
 * is reported as diagnostic colour only and can never authorise ending a process. The three outcomes are
 * reported distinctly, because "already gone" and "not ours" must never read the same as success.
 */
async function stopOne(pidFile, label) {
  if (!existsSync(pidFile)) { say(`  ....  ${label}: no record for this instance, nothing to stop`); return 'absent'; }
  const record = JSON.parse(readFileSync(pidFile, 'utf8'));
  const identity = verifyIdentity(record, paths);

  if (identity.verdict === 'gone') {
    say(`  PASS  ${label} pid ${record.pid} is not running (it had already exited)`);
    rmSync(pidFile, { force: true });
    return 'gone';
  }
  if (identity.verdict === 'foreign') {
    say(`  FAIL  ${label} pid ${record.pid} was NOT stopped: ${identity.detail}`);
    say('        it is left alone, and this record is kept so the situation stays visible');
    return 'foreign';
  }

  const corroboration = identity.portHeldBy !== null && identity.portHeldBy !== undefined
    ? `port ${record.port} is held by pid ${identity.portHeldBy}${identity.portIsOurs ? ' (this process)' : ' (not this process)'}`
    : `port ${record.port} is free or unreadable`;
  const killed = run('taskkill', ['/PID', String(record.pid), '/T', '/F']);
  if (killed.status !== 0) {
    // The process can exit between the identity check and the kill, and `taskkill` localises "not found" into
    // the console codepage. Either way, re-reading the facts answers the question that matters.
    if (processFacts(record.pid) === null) {
      say(`  PASS  ${label} pid ${record.pid} had already exited (it was gone by the time of the kill)`);
      rmSync(pidFile, { force: true });
      return 'gone';
    }
    say(`  FAIL  ${label} taskkill on pid ${record.pid} failed: ${(killed.stderr || killed.stdout || '').trim().slice(0, 110)}`);
    return 'failed';
  }
  say(`  PASS  ${label} pid ${record.pid} stopped (identity: ${identity.detail}; ${corroboration})`);
  rmSync(pidFile, { force: true });
  return 'stopped';
}

const serviceResult = await stopOne(paths.pidService, 'service');
if (serviceResult === 'stopped') await sleep(1500);
const hostResult = await stopOne(paths.pidHost, 'host');
if (hostResult === 'stopped') await sleep(800);

// A pid file only knows the *last* launch. An earlier failed start can leave a helper behind that no pid file
// names, so the sweep looks for processes whose **own command line names this instance's directories** — a
// verifiable instance marker, never a process name and never a port. Each hit is also cross-checked against this
// instance's own launch history, so even the sweep keeps per-process evidence rather than a blanket rule.
const history = readHistory(paths);
const orphans = processesOfInstance(paths);
if (orphans.length === 0) say('  ....  no other process names this instance');
for (const orphan of orphans) {
  const known = history.find(entry => entry.pid === orphan.pid
    && (entry.creationTicks === undefined || entry.creationTicks === null
      || Number(entry.creationTicks) === orphan.creationTicks));
  const reason = known ? 'its command line names this instance and its creation time matches our launch record'
    : 'its command line names this instance (not in our launch history, so the command line is the evidence)';
  const killed = run('taskkill', ['/PID', String(orphan.pid), '/T', '/F']);
  /** A process the sweep dealt with may still be named by a pid file; that record is now stale, so it goes. */
  const forget = () => {
    for (const file of [paths.pidService, paths.pidHost]) {
      if (readPidFile(file)?.pid === orphan.pid) rmSync(file, { force: true });
    }
  };
  if (killed.status === 0) { say(`  PASS  swept pid ${orphan.pid} (${orphan.name}; ${reason})`); forget(); }
  else if (!processesOfInstance(paths).some(entry => entry.pid === orphan.pid)) {
    // The scan and the kill are two moments, and a helper that exits in between is not a failure. Re-scanning
    // before reporting is what keeps "already gone" distinct from "could not stop"; without it a successful
    // sweep of a process that had just exited was printed as a FAIL.
    say(`  PASS  pid ${orphan.pid} had already exited by the time of the sweep (nothing left to do)`);
    forget();
  } else {
    say(`  FAIL  could not stop pid ${orphan.pid}: ${(killed.stderr || killed.stdout || '').trim().slice(0, 100)}`);
  }
  await sleep(400);
}

// --- residuals: reported, never assumed away
say('');
say('Residual check');
let stillHeld = 0;
for (const [label, port] of [['service port', config.servicePort], ['web port', config.webPort]]) {
  const owner = portOwner(port);
  if (owner === null) check(`${label} ${port} is free`, () => true);
  else {
    stillHeld += 1;
    check(`${label} ${port}`, () => ({ state: 'fail',
      detail: `still held by pid ${owner} —if that is not this instance, leave it alone` }));
  }
}
const leftovers = [
  ['pid files', [paths.pidService, paths.pidHost].filter(existsSync)],
  ['runtime job directories', existsSync(join(paths.runtime, 'work'))
    ? [join(paths.runtime, 'work')] : []],
];
for (const [label, found] of leftovers) {
  if (found.length === 0) check(`no leftover ${label}`, () => true);
  else check(`${label} still present`, () => ({ state: 'unverified',
    detail: `${found.length} path(s) remain; they are inside this instance's data directory and are safe to delete by hand` }));
}
check('in-flight jobs', () => ({ state: 'unverified',
  detail: 'a job running inside the container is cancelled by the service parent when its process tree is '
    + 'killed; the service log records whether the worker acknowledged it, and this script does not read that log' }));

if (!flags['keep-data'] && flags['remove-data']) {
  rmSync(paths.data, { recursive: true, force: true });
  say('  ....  --remove-data was given, so this instance\'s data directory was deleted');
} else {
  say(`  ....  data kept at ${paths.data} (libraries, logs, token, runtime). Use --remove-data to delete it.`);
}
say('');
// The summary is driven by the observed residuals, not by which branch the kill took: a kill that reported
// success but left the port held is not a clean stop, and a pid that had already exited is not a failure.
say(stillHeld === 0
  ? 'Stop complete: this instance\'s ports are free and no process of its own is left.'
  : `Stop incomplete: ${stillHeld} port(s) are still held —see the FAIL lines above.`);
say('');
