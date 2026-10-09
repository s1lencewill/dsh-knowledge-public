/**
 * Keep the acceptance stand-in provider alive, and record every time it was not.
 *
 * The stand-in is the one process whose absence silently ruins a run: with it gone, every model request fails,
 * the host retries, and the operator sees a retry loop instead of a first answer. It is also the one process that
 * can be restarted without invalidating anything the operator holds (the login link belongs to the *host*), so
 * this supervisor restarts it and logs the event rather than only reporting it later.
 *
 * It never starts, stops or restarts the host or the service: those hold the operator's single-use link.
 *
 *   node scripts/accept/supervise-stub.mjs [--conv-port 18794] [--engine-port 18793] [--report-port 18795]
 *                                          [--log .local/accept/stub-watch.log]
 */
import { spawn, spawnSync } from 'node:child_process';
import { appendFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';

const argv = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = argv.indexOf(name);
  return index > -1 ? argv[index + 1] : fallback;
};
const CONV_PORT = flag('--conv-port', '18794');
const ENGINE_PORT = flag('--engine-port', '18793');
const REPORT_PORT = flag('--report-port', '18795');
const LOG = resolve(flag('--log', '.local/accept/stub-watch.log'));
const STUB = resolve(import.meta.dirname, 'stub-provider.mjs');
const log = (message) => appendFileSync(LOG, `${new Date().toISOString()} ${message}\n`);

const listening = (port) => spawnSync('powershell', ['-NoProfile', '-Command',
  `(Get-NetTCPConnection -State Listen -LocalPort ${port} -ErrorAction SilentlyContinue | Measure-Object).Count`],
{ encoding: 'utf8' }).stdout.trim() !== '0';

const start = () => {
  const child = spawn(process.execPath, [STUB, '--conv-port', CONV_PORT,
    '--engine-port', ENGINE_PORT, '--report-port', REPORT_PORT],
  { cwd: process.cwd(), detached: true, stdio: 'ignore', windowsHide: true });
  child.unref();
  return child.pid;
};

const ports = [CONV_PORT, ENGINE_PORT, REPORT_PORT];
let restarts = 0;
log(`supervisor started (stub ${existsSync(STUB) ? 'present' : 'MISSING'} at ${STUB})`);
while (true) {
  const missing = ports.filter(port => !listening(port));
  if (missing.length) {
    restarts += 1;
    const pid = start();
    log(`ports ${missing.join(',')} were not listening — restarted the stand-in (pid ${pid}, restart #${restarts})`);
    await new Promise(r => setTimeout(r, 5000));
  } else {
    log(`conversation=1 engine=1 report=1 (restarts so far: ${restarts})`);
  }
  await new Promise(r => setTimeout(r, 10_000));
}
