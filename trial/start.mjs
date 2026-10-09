/**
 * `start` —verify, launch, wait for health, and only then report the instance as usable.
 *
 * Order matters and is the whole point: the runtime and the config are checked *before* anything is launched,
 * so a missing runtime produces a clear refusal instead of a service that starts and then silently cannot do
 * isolation. The service and the DSH host are detached with their output appended to the instance's own log
 * files, and their pids are recorded with enough identity to be verified later by `stop`.
 */
import { existsSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import {
  check, defaultTarget, httpJson, instanceEnv, instancePaths, launchDetached, parseFlags, portOwner, pythonOf,
  readJson, appendHistory, processFacts, requireInstance, resolveDshLaunch, run, say, sleep, waitForHealth,
  writePidFile,
} from './lib.mjs';

const flags = parseFlags(process.argv.slice(2), { target: null, help: false, 'service-only': false, catalog: null });
if (flags.help) {
  say('node trial/start.mjs --target <dir> [--service-only] [--catalog <identity.json>]');
  say('  --service-only   start only the knowledge backend, for a DSH host that is already running');
  say('  --catalog <p>    use this catalogue/identity file instead of the instance\'s own');
  process.exit(0);
}

const target = resolve(flags.target || defaultTarget());
const { paths, config } = requireInstance(target);
const python = pythonOf(config);
// A DSH host that is already running cannot be given new environment variables, so the backend is pointed at the
// real library's catalogue by flag here rather than by editing the host's environment.
const catalog = typeof flags.catalog === 'string' ? resolve(flags.catalog) : null;
const serviceOnly = Boolean(flags['service-only']);

say('');
say(`Starting ${target}${serviceOnly ? ' (service only: no DSH host is launched)' : ''}`);
if (catalog) say(`  catalogue: ${catalog}`);

// --- preflight: the facts that must hold before anything is launched
let blocked = false;
const need = (label, condition, detail) => {
  if (condition) { check(label, () => true); return true; }
  blocked = true;
  check(label, () => ({ state: 'fail', detail }));
  return false;
};

need('container runtime present', existsSync(paths.runtime),
  'the service requires isolation and will not start without it; place it with '
  + '`install --runtime-from <dir>` (see THIRD-PARTY-NOTICES.md for why it is not shipped)');
need('service dependencies importable', run(python, ['-c', 'import fastapi, uvicorn, pydantic']).status === 0,
  `install the pinned set into your Python: ${python} -m pip install -r `
  + `"${join(paths.app, 'engine', 'requirements-win-py313.lock')}"`);
need('token present', existsSync(paths.token) && readFileSync(paths.token, 'utf8').trim().length >= 24,
  're-run install');
need('libraries configured', (readJson(paths.libraries)?.libraries ?? []).length > 0, 're-run install');

const serviceOwner = portOwner(config.servicePort);
const hostOwner = serviceOnly ? null : portOwner(config.webPort);
need(`service port ${config.servicePort} is free`,
  serviceOwner === null,
  `port ${config.servicePort} is held by pid ${serviceOwner}; this instance will not take over another service`);
if (!serviceOnly) need(`web port ${config.webPort} is free`,
  hostOwner === null || readJson(paths.pidHost)?.pid === hostOwner,
  `port ${config.webPort} is held by pid ${hostOwner}; choose another with install --web-port`);

if (blocked) {
  say('');
  say('Refusing to start: a preflight check failed. Nothing was launched.');
  say(`Fix the failure above, then re-run. \`node trial/doctor.mjs --target "${target}"\` lists everything.`);
  process.exit(1);
}

/** Record a launched process with the identity the OS reports, so `stop` can re-verify it later. */
function recordLaunch(file, launched, { command, marker }) {
  const facts = processFacts(launched.pid);
  writePidFile(file, { pid: launched.pid, kind: launched.kind, port: launched.port, marker,
    command, app: paths.app, log: launched.logPath,
    creationTicks: facts?.creationTicks ?? null, executable: facts?.executable ?? null });
  appendHistory(paths, { pid: launched.pid, kind: launched.kind, port: launched.port, marker,
    creationTicks: facts?.creationTicks ?? null, executable: facts?.executable ?? null });
}

// --- service
const env = instanceEnv(paths, config, catalog ? { DSH_KNOWLEDGE_CATALOG: catalog } : {});
const serviceLog = join(paths.logs, 'service.log');
const service = launchDetached(python, ['-m', 'uvicorn', 'service:app', '--app-dir',
  join(paths.app, 'engine'), '--host', '127.0.0.1', '--port', String(config.servicePort)],
{ env, logPath: serviceLog, cwd: paths.app, kind: 'service', marker: paths.app, port: config.servicePort });
recordLaunch(paths.pidService, service, { command: 'uvicorn service:app', marker: paths.app });
say(`  ....  service launched (pid ${service.pid}), log ${serviceLog}`);

const token = readFileSync(paths.token, 'utf8').trim();
const health = await waitForHealth(`http://127.0.0.1:${config.servicePort}/health`,
  { timeoutMs: 45_000, headers: { authorization: `Bearer ${token}` } });
if (health.status !== 200) {
  say('');
  say(`Service did not become healthy (last HTTP ${health.status}${health.error ? `: ${health.error}` : ''}).`);
  say(`Read ${serviceLog}, then stop this instance with: node trial/stop.mjs --target "${target}"`);
  process.exit(1);
}
const isolated = check(`service healthy (isolationActive=${health.body?.isolationActive})`,
  () => health.body?.isolationActive === true ? true
    : { state: 'fail', detail: 'the service answered but reports isolation is NOT active; refusing to call '
      + 'this instance usable' });
if (isolated === 'fail') {
  say('');
  say(`Read ${serviceLog}, then stop this instance with: node trial/stop.mjs --target "${target}"`);
  process.exit(1);
}

// --- host
if (serviceOnly) {
  say('');
  say('This instance\'s knowledge backend is up; no DSH host was launched (--service-only).');
  say(`  service     http://127.0.0.1:${config.servicePort} (isolation=required, bearer token from ${paths.token})`);
  if (catalog) say(`  catalogue   ${catalog}`);
  say(`  logs        ${paths.logs}`);
  say(`  stop        node trial/stop.mjs --target "${target}"`);
  say('');
  say('Point a host at it with a profile patch row:');
  say('  - insert:');
  say('      - id: knowledge');
  say('        name: dsh-knowledge');
  say('        config:');
  say(`          url: http://127.0.0.1:${config.servicePort}`);
  say(`          tokenFile: ${paths.token}`);
  say('');
  process.exit(0);
}
const hostLog = join(paths.logs, 'host.log');
const launch = resolveDshLaunch();
// Only the part of the log this launch appends is read. The first version searched the whole file for the first
// URL it could find, and `host.log` is appended to across runs - so a second `start` printed the *previous*
// run's token, which is exactly the link the user would then open. The offset is what makes the printed URL
// this run's URL.
const hostLogOffset = existsSync(hostLog) ? readFileSync(hostLog, 'utf8').length : 0;
const host = launchDetached(launch.command, [...launch.args, '--profile', config.profileName, '--no-open',
  '--port', String(config.webPort)], { env, logPath: hostLog, cwd: paths.app, kind: 'host',
  marker: `--profile ${config.profileName}`, port: config.webPort });
recordLaunch(paths.pidHost, host, { command: `${launch.how} --profile ${config.profileName}`,
  marker: `--profile ${config.profileName}` });
say(`  ....  host launched (pid ${host.pid}) via ${launch.how}, log ${hostLog}`);

const URL_PATTERN = /http:\/\/127\.0\.0\.1:\d+\/\?token=[A-Za-z0-9_-]+/g;
let url = null;
let served = false;
for (let attempt = 0; attempt < 40 && !url; attempt += 1) {
  await sleep(750);
  if (!existsSync(hostLog)) continue;
  const appended = readFileSync(hostLog, 'utf8').slice(hostLogOffset);
  const found = [...appended.matchAll(URL_PATTERN)].map(match => match[0]);
  if (!found.length) continue;
  // The last URL in this run's own output is the one it is serving.
  url = found[found.length - 1];
  // Liveness is checked **without touching the URL**. DSH's `?token=` is exchanged once for a session cookie, so
  // fetching it to see whether it works consumes the very link the user is about to open - the first version of
  // this check turned a working instance into a 401. Port ownership is the fact that answers the question here.
  served = portOwner(config.webPort) === host.pid;
}
if (!url) {
  say('');
  say('The host did not print a URL.');
  say(`Read ${hostLog}.`);
  say(`Stop this instance with: node trial/stop.mjs --target "${target}"`);
  process.exit(1);
}
check(`host is serving on ${config.webPort} (pid ${host.pid})`, () => served ? true
  : { state: 'unverified', detail: `the port is not owned by pid ${host.pid}; the URL below may not answer` });

say('');
say('This instance is up.');
say(`  open        ${url}`);
say(`  services    service 127.0.0.1:${config.servicePort} (isolation=required), web 127.0.0.1:${config.webPort}`);
say(`  logs        ${paths.logs}`);
say(`  stop        node trial/stop.mjs --target "${target}"`);
say('');
say("The URL carries this instance's own login token: it is not a provider credential, and no other command");
say('prints it.');
say('');
