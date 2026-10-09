/**
 * `doctor` — say what is ready, what is missing, and what could not be checked. It never runs a model call.
 *
 * Every check here is local and offline: file presence, versions, dependency imports, runtime freshness,
 * config validity, port availability. Nothing in this file contacts a provider or the knowledge service, so
 * "the doctor passes" cannot be mistaken for "the model works".
 */
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { probePlugin } from './host-dependencies.mjs';
import {
  check, defaultTarget, httpJson, instancePaths, parseFlags, portOwner, pythonOf, readJson, requireInstance,
  run, say, ok, unverified,
} from './lib.mjs';

const flags = parseFlags(process.argv.slice(2), { target: null, help: false });
if (flags.help) { say('node trial/doctor.mjs --target <dir>'); process.exit(0); }

const target = resolve(flags.target || defaultTarget());
const { paths, config } = requireInstance(target);
const states = [];
const record = (state) => { states.push(state); return state; };

say('');
say(`Doctor for ${target} (version ${config.version})`);
say('');

// --- toolchain
record(check(`Node ${process.versions.node}`, () => Number(process.versions.node.split('.')[0]) >= 24
  ? true : { state: 'fail', detail: 'Node 24 or newer is required' }));
const python = pythonOf(config);
record(check(`Python: ${python}`, () => {
  const result = run(python, ['-c', 'import sys; print("%d.%d" % sys.version_info[:2])']);
  if (result.status !== 0) return { state: 'fail', detail: (result.stderr || 'python did not run').trim().slice(0, 120) };
  const version = (result.stdout || '').trim();
  return version === '3.13' ? version : { state: 'fail', detail: `engine needs 3.13, got ${version}` };
}));

// --- the package's own files
for (const relative of ['package.json', 'lib/index.js', 'lib/browser.js', 'engine/service.py', 'ui/source-card.js']) {
  record(check(`app/${relative}`, () => existsSync(join(paths.app, relative)) ? true
    : { state: 'fail', detail: 'missing; re-run install' }));
}
record(check('client entry declared in package.json', () => {
  const pkg = readJson(join(paths.app, 'package.json'));
  const entry = pkg?.exports?.['./client']?.default;
  if (!entry) return { state: 'fail', detail: 'exports["./client"] is missing, so no browser card can load' };
  return existsSync(join(paths.app, entry)) ? entry : { state: 'fail', detail: `${entry} does not exist` };
}));

// --- service dependencies (imports, not versions: this is what actually has to load)
record(check('installed plugin and host SDK importable', () => {
  const probe = probePlugin(paths.app);
  return probe.ok ? true : { state: 'fail', detail: `${probe.code}; run node app/trial/host-dependencies.mjs "${paths.app}" with the intended DSH host installed` };
}));
record(check('service dependencies importable', () => {
  const probe = 'import fastapi, uvicorn, pydantic; print("ok")';
  const result = run(python, ['-c', probe]);
  return result.status === 0 ? true
    : { state: 'fail', detail: 'run: ' + python + ' -m pip install -r app/engine/requirements-win-py313.lock' };
}));
record(check('engine lock file present', () => existsSync(join(paths.app, 'engine', 'requirements-win-py313.lock'))
  ? true : { state: 'fail', detail: 'the pinned dependency list is missing from the package' }));

// --- the container runtime, and its freshness
let runtimeState = 'unverified';
if (!existsSync(paths.runtime)) {
  runtimeState = record(check('container runtime', () => ({ state: 'fail',
    detail: 'not placed. The service runs with isolation=required by design and will refuse to start without '
      + 'it. The runtime contains an AGPL-licensed modified rga and two GPL packages, so it is not shipped in '
      + 'the package: place it yourself with `install --runtime-from <dir>` (see THIRD-PARTY-NOTICES.md)' })));
} else {
  const manifest = ['stage-manifest.json', 'Manifest.json', 'manifest.json']
    .map(name => join(paths.runtime, name)).find(existsSync);
  runtimeState = record(check('container runtime', () => manifest ? true
    : { state: 'fail', detail: 'the runtime directory has no manifest, so freshness cannot be checked' }));
  if (manifest) {
    record(check('runtime freshness', () => {
      // `staging.verify` compares the staged manifest against the current lock, patch, engine sources and tool
      // binaries. The tool paths come from the environment at run time (`engine/appcontainer.py` sets them the
      // same way), so the probe has to provide them too - without this the check reported a missing tool as an
      // unverified traceback instead of the truth.
      //
      // They are looked for where this instance actually keeps them first. `install` puts them in
      // `data/tools`; only a service run copies them into the runtime directory itself. Pointing the probe at
      // the runtime directory alone therefore reported a fresh, correctly installed runtime as drifted
      // (DEPENDENCY_MISSING) until the service had been started once.
      const toolPath = (name) => [join(paths.tools, name), join(paths.runtime, name)].find(existsSync)
        ?? join(paths.tools, name);
      const probeEnv = {
        ...process.env,
        DSH_KNOWLEDGE_RGA: toolPath('rga.exe'),
        DSH_KNOWLEDGE_RG: toolPath('rg.exe'),
      };
      const result = run(python, ['-c', [
        'import json, sys',
        'sys.path.insert(0, r"' + join(paths.app, 'engine') + '")',
        'import staging',
        'from pathlib import Path',
        'try:',
        '    ok, detail = staging.verify(Path(r"' + paths.runtime + '"), Path(r"' + paths.app + '"))',
        '    print(json.dumps({"ok": bool(ok), "detail": str(detail)}))',
        'except Exception as error:',
        '    print(json.dumps({"ok": False, "detail": type(error).__name__ + ": " + str(error)}))',
      ].join('\n')], { env: probeEnv, timeoutMs: 120_000 });
      if (result.status !== 0) return { state: 'unverified', detail: (result.stderr || '').trim().slice(0, 160) };
      const parsed = JSON.parse((result.stdout || '{}').trim().split('\n').pop() || '{}');
      return parsed.ok ? true
        : { state: 'fail', detail: `stale or drifted runtime: ${String(parsed.detail).slice(0, 140)}. Re-stage it.` };
    }));
  }
  // The two pinned binaries are a hard prerequisite: the service refuses to start without them, and it is the
  // one install step a user can get wrong by pointing `--runtime-from` at a directory that does not carry them.
  record(check('pinned tool binaries (rga, rg)', () => {
    const found = ['rga.exe', 'rg.exe'].map(name =>
      [join(paths.tools, name), join(paths.runtime, name)].find(existsSync));
    const missing = ['rga.exe', 'rg.exe'].filter((_, index) => !found[index]);
    return missing.length
      ? { state: 'fail', detail: `${missing.join(' and ')} not found in ${paths.tools} or the runtime; re-run `
        + 'install with --tools-from <dir> (see docs/RUNTIME-BUILD.md)' }
      : found.join(' and ');
  }));
  // Isolation is a capability of this machine, and it is reported rather than assumed.
  record(check('isolation capability (AppContainer)', () => {
    const result = run('powershell', ['-NoProfile', '-Command',
      '(Get-Command New-AppContainerSandbox -ErrorAction SilentlyContinue) -ne $null']);
    return (result.stdout || '').trim().toLowerCase() === 'true' ? true
      : { state: 'unverified', detail: 'no direct probe for this; the service reports isolationActive at start' };
  }));
}

// --- configuration
record(check('instance config', () => {
  // `modelBaseUrl` is deliberately not in this list: absent means "leave the adapter's own default base URL
  // alone", which is the correct state for DSH 0.2 (whose `llm-deepseek` defaults to the Anthropic endpoint).
  // A value here is an override, so its presence is reported rather than required.
  for (const key of ['servicePort', 'webPort', 'modelApiKeyEnv', 'engineBaseUrl']) {
    if (config[key] === undefined || config[key] === null) return { state: 'fail', detail: `config.${key} is missing` };
  }
  const base = typeof config.modelBaseUrl === 'string' && config.modelBaseUrl.trim() !== ''
    ? `model base URL override: ${config.modelBaseUrl}` : 'model base URL: adapter default';
  return `service :${config.servicePort}, web :${config.webPort}; ${base}`;
}));
record(check('library config', () => {
  const libraries = readJson(paths.libraries);
  if (!libraries?.libraries?.length) return { state: 'fail', detail: 'no library is configured' };
  for (const library of libraries.libraries) {
    if (!existsSync(library.root)) return { state: 'fail', detail: `library root does not exist: ${library.root}` };
  }
  return `${libraries.libraries.length} libraries, roots exist`;
}));
record(check('catalogue', () => {
  const catalogue = readJson(paths.catalogue);
  const documents = catalogue?.documents ? Object.keys(catalogue.documents) : [];
  return documents.length ? `${documents.length} documents` : { state: 'fail', detail: 'the catalogue has no documents' };
}));
record(check('service token', () => {
  const token = existsSync(paths.token) ? readFileSync(paths.token, 'utf8').trim() : '';
  return token.length >= 24 ? `present (${token.length} characters, not shown)` 
    : { state: 'fail', detail: 'missing or too short; re-run install' };
}));
record(check('DSH profile', () => existsSync(join(paths.profilesDir, config.profileName, 'cordis.patch.yml')) ? true
  : { state: 'fail', detail: 'no profile patch; re-run install' }));
// A profile patch that no longer *composes* is the failure this check exists for, and it happened for real: the
// DSH 0.1.7 upgrade rewrote a profile and dropped the plugin from the composed tree entirely (198 entries, no
// `knowledge`), which looks exactly like a working install until somebody asks a knowledge question. Since
// 0.1.7 a profile is composed from `dsh.profile.bundles`, so the entry has to be named there *and* resolvable
// from the profile's own node_modules - the patch row alone is no longer enough.
record(check('plugin composed into the profile', () => {
  const profileDir = join(paths.profilesDir, config.profileName);
  const manifestPath = join(profileDir, 'package.json');
  const patchPath = join(profileDir, 'cordis.patch.yml');
  const manifest = existsSync(manifestPath) ? readJson(manifestPath) : null;
  const patch = existsSync(patchPath) ? readFileSync(patchPath, 'utf8') : '';
  if (!manifest) return { state: 'fail', detail: 'the profile has no package.json; re-run install' };
  const bundles = manifest.dsh?.profile?.bundles ?? [];
  if (!bundles.includes('dsh-knowledge')) {
    return { state: 'fail', detail: 'dsh.profile.bundles does not name dsh-knowledge; the plugin is not composed '
      + 'at all (re-run install)' };
  }
  if (!existsSync(join(profileDir, 'node_modules', 'dsh-knowledge'))) {
    return { state: 'fail', detail: 'the bundle is not resolvable from the profile node_modules; re-run install' };
  }
  const dependency = manifest.dependencies?.['dsh-knowledge'] ?? '';
  if (!/^link:/.test(dependency)) {
    return { state: 'fail', detail: 'the profile dependency is not a link: into this instance; re-run install' };
  }
  if (!/- id: knowledge\s*\n\s*config:/.test(patch)) {
    return { state: 'fail', detail: 'the profile patch has no `- id: knowledge` config row; the plugin would load '
      + 'unconfigured and refuse every call with KNOWLEDGE_NOT_CONFIGURED (re-run install)' };
  }
  return `bundle + link dependency + config row (${dependency})`;
}));
// The profile must compose exactly one directory-picker interaction. A concrete pair pinned next to the adaptive
// row is not a style question: it fails at boot with a duplicate `directoryPicker` service, and "the picker did
// nothing" is what the operator sees. Reading the patch here turns that into a check with a name.
record(check('directory picker interaction', () => {
  const patchPath = join(paths.profilesDir, config.profileName, 'cordis.patch.yml');
  const text = existsSync(patchPath) ? readFileSync(patchPath, 'utf8') : '';
  const adaptiveDisabled = /- id: directory-picker\s*\n\s*disabled:\s*true/.test(text);
  const pinned = (text.match(/dsh-host-directory-picker-(browse|native)/) ?? [])[1] ?? null;
  const surface = (text.match(/dsh-client-ui-directory-picker-(browse|native)/) ?? [])[1] ?? null;
  if (pinned === null) return `adaptive (DSH resolves it per boot); config says ${config.picker ?? 'browse'}`;
  if (pinned !== surface) return { state: 'fail',
    detail: `the backend interaction (${pinned}) and its browser half (${surface ?? 'none'}) disagree; re-run install` };
  if (!adaptiveDisabled) return { state: 'fail',
    detail: `the ${pinned} pair is pinned while the adaptive row is still composed; boot will fail with a duplicate `
      + 'directoryPicker service. Re-run install.' };
  return `${pinned} pinned (in-app dialog${pinned === 'browse' ? '' : ' replaced by the host OS chooser'})`;
}));

// --- ports, read-only
for (const [label, port] of [['service port', config.servicePort], ['web port', config.webPort]]) {
  const owner = portOwner(port);
  if (owner === null) ok(`${label} ${port} is free`);
  else {
    const pid = readPidFilePid(paths, port);
    if (pid) ok(`${label} ${port} is held by this instance (pid ${pid})`);
    else { states.push('fail'); say(`  FAIL  ${label} ${port} is held by another process (pid ${owner}) — `
      + 'stop it, or re-run install with a different --service-port/--web-port'); }
  }
}

// --- an optional live probe, used only when the instance is already running
if (!flags['no-live']) {
  // The service authenticates **every** route, `/health` included (`service.authenticated` is a global
  // FastAPI dependency), so a bare request can only ever come back 401. Probing without the token
  // therefore reported a healthy instance as broken - and only ever "passed" while the service was
  // stopped, which is the one state in which a health check says nothing. The token is this instance's
  // own, read from its own data directory, exactly as `start` reads it; it is never printed here.
  const liveToken = existsSync(paths.token) ? readFileSync(paths.token, 'utf8').trim() : '';
  const health = await httpJson(`http://127.0.0.1:${config.servicePort}/health`,
    { timeoutMs: 2500, headers: liveToken ? { authorization: `Bearer ${liveToken}` } : {} });
  if (health.status === 200) ok(`service answers /health (isolationActive=${health.body?.isolationActive})`);
  else if (health.status === 0) unverified('service is not running, so its health was not checked');
  else if (health.status === 401) { states.push('fail'); say('  FAIL  service refused this instance\'s own '
    + `token (HTTP 401); re-run install to regenerate it, or stop whatever else holds port ${config.servicePort}`); }
  else { states.push('fail'); say(`  FAIL  service answered HTTP ${health.status}`); }
}

const passed = states.filter(state => state === 'pass').length;
const failed = states.filter(state => state === 'fail').length;
const unknown = states.filter(state => state === 'unverified').length;
say('');
say(`${passed} passed, ${failed} failed, ${unknown} unverified`);
if (failed > 0) say('A failed check is a blocker: `start` will refuse rather than run in a reduced mode.');
say('');
process.exit(failed > 0 ? 1 : 0);

function readPidFilePid(paths, port) {
  for (const file of [paths.pidService, paths.pidHost]) {
    const record = readJson(file);
    if (record && record.port === port) return record.pid;
  }
  return null;
}
