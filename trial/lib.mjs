/**
 * Shared machinery for the trial instance's four entry points (install / doctor / start / stop).
 *
 * Design constraints this module exists to honour:
 *
 * - **the program is independent of the checkout.** Program files are copied into
 *   `<instance>/app`, and the service resolves its own paths from there (`engine/service.py` derives its
 *   project root from its own location), so nothing points back at the repository it was installed from;
 * - **program files and user data are separate directories.** `app/` is replaceable on upgrade; `data/`
 *   holds the libraries, the token, the logs, the pid files and the staged runtime. Uninstall removes `app/`
 *   only unless asked otherwise, so an upgrade or a rollback can never eat a user's library;
 * - **nothing here downloads anything.** No install step fetches a package or a binary; where a component
 *   cannot be shipped (the container runtime, which contains an AGPL-licensed patched rga and two GPL
 *   packages - see THIRD-PARTY-NOTICES.md) the step is explicit, takes a source the user names, and fails
 *   with recovery instructions when it is missing;
 * - **no credential is written by this tool.** The service token is generated locally into `data/`, and the
 *   provider credential is whatever the host's own credential store already resolves.
 */
import { spawn, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  copyFileSync, existsSync, mkdirSync, openSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync,
} from 'node:fs';
import { basename, dirname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

export const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

/** Program files the instance needs. Anything not listed is not copied, and that is the point. */
export const PROGRAM_FILES = [
  'package.json', 'cordis.patch.yml', 'lib', 'engine', 'ui', 'trial', 'tools', 'samples',
  'README.md', 'LICENSE-DRAFT.md', 'THIRD-PARTY-NOTICES.md', 'VERSION', 'RELEASE-MANIFEST.json',
];
/** Documentation the user reads. Kept as a list so `install` can report what it is copying. */
export const DOC_FILES = ['docs/QUICKSTART.md', 'docs/LIMITATIONS.md', 'docs/VERSION-AND-DEPENDENCIES.md',
  'docs/RUNTIME-BUILD.md', 'docs/PRIVATE-BETA.md'];
/** Directories whose absence means this is not a package at all. */
export const REQUIRED_APP_PATHS = ['package.json', 'lib/index.js', 'lib/browser.js', 'engine/service.py',
  'ui/source-card.js', 'cordis.patch.yml',
  // The default library is part of "installed and usable": a package without it produces an instance whose
  // `doctor` fails on the library and on the catalogue. It is checked here so the staged-copy guard refuses an
  // upgrade that would have removed it.
  'samples/libraries.template.json', 'samples/catalogue.json'];

export const defaultTarget = () => process.env.DSH_KNOWLEDGE_TRIAL_HOME
  || join(process.env.LOCALAPPDATA || process.env.HOME || process.cwd(), 'dsh-knowledge-trial');

export const instancePaths = (target) => ({
  root: target,
  app: join(target, 'app'),
  data: join(target, 'data'),
  logs: join(target, 'data', 'logs'),
  run: join(target, 'data', 'run'),
  runtime: join(target, 'data', 'appcontainer-runtime'),
  // The pinned tool binaries live beside the runtime, not inside it: at startup the service copies them *into*
  // the runtime, and a source that is the destination is a self-copy that fails with "file in use".
  tools: join(target, 'data', 'tools'),
  dshHome: join(target, 'data', 'dsh-home'),
  config: join(target, 'data', 'config.json'),
  libraries: join(target, 'data', 'libraries.json'),
  catalogue: join(target, 'data', 'catalogue.json'),
  token: join(target, 'data', 'service-token.txt'),
  profilesDir: join(target, 'data', 'dsh-home', 'profiles'),
  pidService: join(target, 'data', 'run', 'service.pid.json'),
  pidHost: join(target, 'data', 'run', 'host.pid.json'),
  // --- the independent lifecycle (`trial/service-task.mjs`, added 2026-09-23)
  //
  // The backend used to live only as long as whatever launched it. These five paths are the supervisor's own
  // durable record, kept inside the instance's data directory so `status`, `stop` and the next supervisor can
  // all read the same facts:
  //
  // * `pidSupervisor` - the identity of the process the scheduled task runs (pid + creation time + executable
  //   + a marker), so a stop can prove *which* process it is ending;
  // * `supervisorState` - the one machine-readable state file: what the supervisor is doing, how many
  //   restarts it has made, why the last child exited, and whether a stop was requested;
  // * `supervisorLock` - the single-instance lock. Its existence is not the claim; the identity recorded in
  //   it is, so a lock left behind by a killed process is recognised as stale rather than obeyed;
  // * `stopRequest` - the marker a human `stop` writes before anything is killed, so a restarting supervisor
  //   knows the death was requested and must not come back;
  // * `supervisorLog` - append-only JSON lines, one per lifecycle event.
  pidSupervisor: join(target, 'data', 'run', 'supervisor.pid.json'),
  supervisorState: join(target, 'data', 'run', 'supervisor.state.json'),
  supervisorLock: join(target, 'data', 'run', 'supervisor.lock'),
  stopRequest: join(target, 'data', 'run', 'stop.request.json'),
  supervisorLog: join(target, 'data', 'logs', 'supervisor.log'),
});

/** The scheduled task that keeps one instance's backend alive. Per user, and named after the instance. */
export const taskNameFor = (target) => 'dsh-knowledge-backend-' + basename(resolve(target)).toLowerCase()
  .replace(/[^a-z0-9-]+/g, '-').slice(-40);

// ---------------------------------------------------------------------------- small utilities

export const say = (message) => process.stdout.write(`${message}\n`);
export const ok = (message) => say(`  PASS  ${message}`);
export const fail = (message) => say(`  FAIL  ${message}`);
export const unverified = (message) => say(`  ????  ${message}`);
export const info = (message) => say(`  ....  ${message}`);

export function readJson(path, fallback = null) {
  try { return JSON.parse(readFileSync(path, 'utf8')); } catch { return fallback; }
}

export function writeJson(path, value) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(value, null, 2) + '\n', 'utf8');
}

/**
 * Copy a file or a directory tree, never following a symlink or junction out of the tree being copied.
 *
 * A single file is handled here too, because the program list is a mix of files (`package.json`) and
 * directories (`lib`), and a caller should not have to know which is which.
 */
export function copyTree(from, to, options = {}) {
  const skip = options.skip ?? (() => false);
  if (!existsSync(from)) return;
  if (!statSync(from).isDirectory()) {
    mkdirSync(dirname(to), { recursive: true });
    copyFileSync(from, to);
    return;
  }
  mkdirSync(to, { recursive: true });
  for (const entry of readdirSync(from, { withFileTypes: true })) {
    const source = join(from, entry.name);
    const destination = join(to, entry.name);
    if (skip(entry.name)) continue;
    if (entry.isSymbolicLink()) continue; // never copy a link; the instance must not reach outside itself
    if (entry.isDirectory()) copyTree(source, destination, options);
    else if (entry.isFile()) copyFileSync(source, destination);
  }
}

/** Load-bearing files only, so the manifest is a check a human can read rather than 9000 hashes. */
export function hashFile(path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex');
}

export function parseFlags(argv, spec) {
  const flags = { ...spec };
  for (let index = 0; index < argv.length; index += 1) {
    const [name, inline] = argv[index].split('=');
    if (!name.startsWith('--')) continue;
    const key = name.slice(2);
    // Flags that take no value must be listed here, or the parser eats the next argument (or `undefined`) as their
    // value: `--service-only` as the last argument silently became falsy that way, and start then launched a second
    // DSH host beside the one the operator already had running. `rebuild`, `json` and `keep-task` are the same
    // shape - a valueless flag that is usually the last argument on the line - so they belong in this list too.
    const wantsValue = !['help', 'force', 'yes', 'quiet', 'keep-data', 'service-only', 'no-live',
      'rebuild', 'json', 'keep-task'].includes(key);
    flags[key] = wantsValue ? (inline ?? argv[++index]) : true;
  }
  return flags;
}

export function run(command, args, options = {}) {
  return spawnSync(command, args, {
    encoding: 'utf8', windowsHide: true, timeout: options.timeoutMs ?? 30_000,
    env: options.env ?? process.env, cwd: options.cwd,
  });
}

/** A probe that never throws and never prints a secret: the doctor reports what it found, not what it ran. */
export function check(label, fn) {
  let outcome;
  try { outcome = fn(); } catch (error) { outcome = { state: 'fail', detail: String(error?.message ?? error).slice(0, 200) }; }
  if (outcome === true) outcome = { state: 'pass' };
  if (outcome === false) outcome = { state: 'fail', detail: 'check returned false' };
  if (typeof outcome === 'string') outcome = { state: 'pass', detail: outcome };
  const state = outcome?.state ?? 'unverified';
  const detail = outcome?.detail ? ` — ${outcome.detail}` : '';
  if (state === 'pass') ok(`${label}${detail}`);
  else if (state === 'fail') fail(`${label}${detail}`);
  else unverified(`${label}${detail}`);
  return state;
}

export function sleep(ms) { return new Promise(done => setTimeout(done, ms)); }

/** A short local token. Generated here, written only into the instance, never into the package or git. */
export function newToken() {
  return createHash('sha256')
    .update(String(Date.now()) + String(Math.random()) + String(process.pid))
    .digest('base64url').slice(0, 43);
}

// ---------------------------------------------------------------------------- instance state

export function loadInstance(target) {
  const paths = instancePaths(target);
  const config = readJson(paths.config);
  return { paths, config };
}

export function requireInstance(target) {
  const loaded = loadInstance(target);
  if (!loaded.config) {
    say(`No trial instance at ${target}.`);
    say('Run:  node trial/install.mjs --target <dir>');
    process.exit(2);
  }
  return loaded;
}

/** The python the instance uses, recorded at install time so every later run is reproducible. */
export function pythonOf(config) {
  return config?.python || null;
}

/**
 * The directory-picker interaction this instance composes, as patch rows.
 *
 * DSH ships an adaptive row (`@deepseek-ai/dsh-host-directory-picker-auto`) that resolves once per boot: on
 * win32 with a loopback bind and no SSH launch it mounts the **native** interaction, whose browser half is
 * renderless by design, because its chooser opens as an OS dialog on the *host's* desktop. That interaction
 * cannot serve an operator who only sees the page - an automated, remote or headless browser, or a desktop where
 * the dialog lands behind the window. The trial therefore pins the **browse** interaction by default, which is
 * the seam's documented swap point ("Pinning an interaction means composing that backend directly") and the one
 * DSH describes as working everywhere: an in-app "Select Workspace Directory" dialog with a path field, crumbs,
 * a hidden-files toggle and folder creation. `--picker native` restores the OS dialog for an operator sitting at
 * the host; `--picker auto` leaves DSH's own choice alone.
 *
 * The adaptive row must be disabled when a concrete pair is pinned: mounting both fails loudly at boot with a
 * duplicate `directoryPicker` service and a duplicate client flow.
 */
export const PICKER_KINDS = ['browse', 'native', 'auto'];
export const pickerRows = (kind) => (kind === 'auto' ? [] : [
  { id: 'directory-picker', disabled: true },
  { insert: [
    { id: `directory-picker-${kind}`, name: `@deepseek-ai/dsh-host-directory-picker-${kind}` },
    { id: `ui-directory-picker-${kind}`, name: `@deepseek-ai/dsh-client-ui-directory-picker-${kind}` },
  ] },
]);

/**
 * Install the DSH profile for this instance.
 *
 * The plugin package is exposed to the profile through a **junction into the instance's own `app/`**, never
 * into the directory the package was built in: an installed instance must keep working after the source
 * checkout is moved, changed or deleted.
 */
/**
 * The `llm-deepseek.config.baseURL` rows for a profile patch - usually no rows at all.
 *
 * DSH 0.2's `llm-deepseek` is an Anthropic-Messages client with its own default base
 * (`https://api.deepseek.com/anthropic`), and a `baseURL` written here *replaces* it. `LEGACY_MODEL_BASE_URL` is
 * the value this installer used to write by default; it is not treated as a choice, because it was ours and it
 * makes the adapter post to `<base>/v1/messages`, which answers 404 (probed with one token on 2026-09-28).
 *
 * @param {unknown} value - the configured base URL, or null/undefined/'' for "leave the adapter's default".
 * @returns {string[]} zero or one YAML rows, ready to splice into the patch.
 */
export function modelBaseUrlRows(value) {
  const baseUrl = typeof value === 'string' ? value.trim() : '';
  if (baseUrl === '' || baseUrl === LEGACY_MODEL_BASE_URL) return [];
  return [`    baseURL: ${baseUrl}`];
}

/** The base URL this installer wrote before it learned to leave the adapter's own default alone. */
export const LEGACY_MODEL_BASE_URL = 'https://api.deepseek.com';

export function writeProfile(paths, config) {
  // The profile is named per instance, not `kn-trial` for everyone: the host's command line carries the
  // profile name and nothing else of the instance, so that name is the marker `stop` verifies. A shared name
  // would let one instance's host look like another's.
  const profileDir = join(paths.profilesDir, config.profileName);
  mkdirSync(profileDir, { recursive: true });
  const modules = join(profileDir, 'node_modules');
  mkdirSync(modules, { recursive: true });
  const link = join(modules, 'dsh-knowledge');
  if (!existsSync(link)) {
    const made = run('cmd', ['/c', 'mklink', '/J', link, paths.app]);
    if (made.status !== 0) return { ok: false, detail: (made.stderr || made.stdout || '').trim().slice(0, 160) };
  }
  const patch = [
    '# Generated by trial/install.mjs. Do not edit by hand; re-run install after changing config.',
    '- id: llm-deepseek',
    '  config:',
    // The conversation model's base URL is the adapter's business, not this installer's. DSH 0.2's
    // `llm-deepseek` is an Anthropic-Messages client whose own default is `https://api.deepseek.com/anthropic`,
    // and a `baseURL` written here *replaces* that default. This installer's older default was
    // `https://api.deepseek.com`, which sent the adapter to `<base>/v1/messages` - a path that answers 404
    // (confirmed with a one-token probe on 2026-09-28) - so the value is written only when it is a real choice:
    // an explicit `--model-base-url`, or a value an operator recorded that is not that old default.
    ...modelBaseUrlRows(config.modelBaseUrl),
    `    apiKeyEnv: ${config.modelApiKeyEnv}`,
    '',
    '- id: session-title-llm',
    '  config:',
    '    targetWords: 5',
    '    targetCjkCharacters: 10',
    '    maxInputBytes: 4096',
    '    maxOutputTokens: 64',
    '    timeoutMs: 60000',
    `    provider: ${config.modelProvider}`,
    `    model: ${config.titleModel}`,
    '',
    '# The directory-picker interaction this instance serves (see docs/QUICKSTART.md).',
    ...pickerRows(PICKER_KINDS.includes(config.picker) ? config.picker : 'browse').flatMap(row => (row.insert
      ? ['- insert:', ...row.insert.flatMap(entry => [`    - id: ${entry.id}`, `      name: '${entry.name}'`]), '']
      : [`- id: ${row.id}`, `  disabled: ${row.disabled}`, ''])),
    // The plugin is registered the way DSH 0.1.7 composes a profile: the package is a **bundle** in
    // `dsh.profile.bundles`, its own `cordis.patch.yml` inserts the entry, and this profile patch only supplies
    // the config. The 0.1.6-and-earlier spelling put everything in one `- insert:` row here; that still composes
    // on 0.1.7, but it is invisible to the plugin manager, which owns the bundle list - and after the 0.1.7
    // upgrade rewrote a profile exactly that way, the plugin was silently gone from the composed tree
    // (198 entries, no `knowledge`). A bundle entry is what the manager preserves.
    '- id: knowledge',
    '  config:',
    `    url: http://127.0.0.1:${config.servicePort}`,
    // The credential is named by path, not written here: a profile patch is a configuration file that gets copied
    // around, and the row also has to work for a host that is already running (whose environment cannot change).
    `    tokenFile: ${paths.token}`,
    '',
  ].join('\n');
  writeFileSync(join(profileDir, 'cordis.patch.yml'), patch, 'utf8');
  const pkg = readJson(join(profileDir, 'package.json'));
  if (pkg) {
    pkg.dsh = pkg.dsh || {};
    pkg.dsh.profile = {
      bundles: [...new Set([...(pkg.dsh.profile?.bundles ?? ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app']),
        'dsh-knowledge'])],
      patchReload: 'live',
    };
    // The bundle has to be resolvable from the profile too: DSH resolves a bundle name from the profile's own
    // `node_modules`, and the junction above is that node_modules entry. Recording it as a `link:` dependency is
    // what keeps a later `dsh plugin install` from pruning the junction as an extraneous package.
    writeJson(join(profileDir, 'package.json'), addLinkDependency(pkg, 'dsh-knowledge', paths.app));
  } else {
    // A profile is composed from the bundles its own package.json names, so an instance's fresh profile needs
    // one: without it DSH has no rows to compose and the plugin would never be loaded.
    writeJson(join(profileDir, 'package.json'), {
      name: 'dsh-profile-' + config.profileName,
      private: true,
      dependencies: { 'dsh-knowledge': linkSpec(paths.app) },
      dsh: {
        profile: {
          bundles: ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app', 'dsh-knowledge'],
          patchReload: 'live',
        },
      },
    });
    writeFileSync(join(profileDir, 'cordis.yml'),
      '# dsh profile root - an empty entry list; the tree is composed as patches.\n[]\n', 'utf8');
  }
  return { ok: true, link };
}

/**
 * A `link:` spec for a local package, spelled the way this profile's other linked plugins are.
 *
 * Forward slashes and no `./`: pnpm treats `link:C:/x/y` as the absolute local package, and a backslash path
 * pasted into a JSON manifest is a value every tool that reads it has to guess about.
 */
export function linkSpec(dir) {
  return 'link:' + resolve(dir).replace(/\\/g, '/');
}

/** Add or refresh the `link:` dependency for a local package without disturbing the rest of the manifest. */
export function addLinkDependency(pkg, name, dir) {
  const next = { ...pkg, dependencies: { ...(pkg.dependencies ?? {}) } };
  const spec = linkSpec(dir);
  if (next.dependencies[name] !== spec) next.dependencies[name] = spec;
  return next;
}

/**
 * Environment every service/host launch shares. The token is read from the instance, never from argv.
 *
 * `DSH_KNOWLEDGE_RGA` / `DSH_KNOWLEDGE_RG` are set here because the service resolves them at startup to verify
 * the runtime's freshness, and an installed instance has no development `.local/tools` to fall back on. Without
 * them the service refuses to start with `DEPENDENCY_MISSING`, which is the correct failure but a confusing
 * one to hit as a user.
 */
export function instanceEnv(paths, config, extra = {}) {
  const token = existsSync(paths.token) ? readFileSync(paths.token, 'utf8').trim() : '';
  const engine = engineCredentials(config);
  return {
    ...process.env,
    DSH_HOME: paths.dshHome,
    PYTHONDONTWRITEBYTECODE: '1',
    DSH_KNOWLEDGE_URL: `http://127.0.0.1:${config.servicePort}`,
    DSH_KNOWLEDGE_TOKEN: token,
    DSH_KNOWLEDGE_CONFIG: paths.libraries,
    DSH_KNOWLEDGE_CATALOG: paths.catalogue,
    DSH_KNOWLEDGE_RUNTIME: paths.runtime,
    DSH_KNOWLEDGE_STATE_DIR: join(paths.data, 'state'),
    DSH_KNOWLEDGE_DIAGNOSTICS_DIR: join(paths.data, 'diagnostics'),
    DSH_KNOWLEDGE_ISOLATION: 'required',
    DSH_KNOWLEDGE_RGA: join(paths.tools, 'rga.exe'),
    DSH_KNOWLEDGE_RG: join(paths.tools, 'rg.exe'),
    LLM_BASE_URL: engine.baseUrl,
    LLM_API_KEY: engine.apiKey,
    LLM_MODEL_NAME: engine.model,
    NO_PROXY: '127.0.0.1,localhost',
    ...extra,
  };
}

/**
 * The engine's provider credentials, resolved without ever writing the key down.
 *
 * A local engine that answers real questions needs a real provider key, and the three ways to give it one differ
 * in where the secret lives: a **file** the user owns (`--engine-env-file`, the same `LLM_*` names
 * `scripts/verify-model.py` already loads), the **name** of an environment variable (`--engine-api-key-env`), or
 * the literal placeholder the trial ships with (which makes the engine refuse, loudly, rather than pretend). The
 * file wins over the name, the name wins over the placeholder, and nothing here prints a value: a missing or
 * unreadable file leaves the placeholder in place, which the service reports as a provider failure rather than
 * silently calling nobody.
 */
export function engineCredentials(config) {
  const fallback = { baseUrl: config.engineBaseUrl, apiKey: config.engineApiKey, model: config.engineModel };
  const file = typeof config.engineEnvFile === 'string' && config.engineEnvFile.trim() !== '' ? config.engineEnvFile : null;
  if (file && existsSync(file)) {
    const values = {};
    for (const line of readFileSync(file, 'utf8').split(/\r?\n/)) {
      const match = /^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$/.exec(line);
      if (!match || line.trimStart().startsWith('#')) continue;
      values[match[1]] = match[2].trim().replace(/^["']|["']$/g, '');
    }
    return { baseUrl: values.LLM_BASE_URL || fallback.baseUrl,
      apiKey: values.LLM_API_KEY || fallback.apiKey, model: values.LLM_MODEL_NAME || fallback.model };
  }
  const envName = typeof config.engineApiKeyEnv === 'string' && config.engineApiKeyEnv.trim() !== ''
    ? config.engineApiKeyEnv.trim() : null;
  if (envName && process.env[envName]) {
    return { baseUrl: fallback.baseUrl, apiKey: process.env[envName], model: fallback.model };
  }
  return fallback;
}

/**
 * A health probe that distinguishes "not up yet" from "something else is on the port".
 *
 * The timeout is a timer this function **clears**, not `AbortSignal.timeout`. That distinction is not
 * cosmetic: an `AbortSignal.timeout` timer stays armed after the response has been read, and a process that
 * calls `process.exit()` with one still pending trips a libuv assertion on Windows
 * (`!(handle->flags & UV_HANDLE_CLOSING)` in `win/async.c`) - so a command that had already printed its
 * correct answer exited non-zero with a crash trace after it.
 */
export async function httpJson(url, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error('timeout')), options.timeoutMs ?? 3000);
  timer.unref?.();
  try {
    const response = await fetch(url, { signal: controller.signal, headers: options.headers });
    const text = await response.text();
    let body = null;
    try { body = JSON.parse(text); } catch { /* not json */ }
    return { status: response.status, body, text };
  } catch (error) {
    return { status: 0, error: String(error?.message ?? error) };
  } finally {
    clearTimeout(timer);
  }
}

export async function waitForHealth(url, options = {}) {
  const deadline = Date.now() + (options.timeoutMs ?? 40_000);
  let last = null;
  while (Date.now() < deadline) {
    last = await httpJson(url, { headers: options.headers });
    if (last.status === 200) return last;
    await sleep(options.everyMs ?? 700);
  }
  return last;
}

/** Write a pid file that records enough to prove identity later, so `stop` can never kill a recycled pid. */
export function writePidFile(path, record) {
  writeJson(path, { ...record, at: new Date().toISOString() });
}

export function readPidFile(path) {
  const record = readJson(path);
  if (!record || typeof record.pid !== 'number') return null;
  return record;
}

/** The command line of a live process, or null. Used only to confirm identity before a kill. */
export function processCommandLine(pid) {
  const result = run('powershell', ['-NoProfile', '-Command',
    `(Get-CimInstance Win32_Process -Filter "ProcessId=${pid}" -ErrorAction SilentlyContinue).CommandLine`]);
  const text = (result.stdout || '').trim();
  return text.length ? text : null;
}

export function processStartTime(pid) {
  const result = run('powershell', ['-NoProfile', '-Command',
    `(Get-CimInstance Win32_Process -Filter "ProcessId=${pid}" -ErrorAction SilentlyContinue).CreationDate`]);
  const text = (result.stdout || '').trim();
  return text.length ? text : null;
}

export function portOwner(port) {
  const result = run('powershell', ['-NoProfile', '-Command',
    `(Get-NetTCPConnection -State Listen -LocalPort ${port} -ErrorAction SilentlyContinue | `
    + 'Select-Object -First 1 -ExpandProperty OwningProcess)']);
  const text = (result.stdout || '').trim();
  return /^\d+$/.test(text) ? Number(text) : null;
}

/**
 * Launch a detached child whose output goes to a log file inside the instance's data directory, and return the
 * **identity the operating system reports**, not just the pid.
 *
 * The pid alone is not identity evidence: Windows reuses pids aggressively. So the creation time and the
 * executable path are read back from the OS immediately after the spawn and stored with the record, and `stop`
 * re-reads them later. Those two facts plus an instance-owned marker are what authorise a kill; a port is
 * corroboration at most, because any process can hold a port.
 */
export function launchDetached(command, args, { env, logPath, cwd, kind, marker, port }) {
  mkdirSync(dirname(logPath), { recursive: true });
  const handle = openSync(logPath, 'a');
  const child = spawn(command, args, {
    env, cwd, detached: true, windowsHide: true, stdio: ['ignore', handle, handle],
  });
  child.unref();
  return { pid: child.pid, logPath, kind, marker, port };
}

/** The OS-reported facts about one process: creation time and executable, or null when it is gone. */
export function processFacts(pid) {
  const result = run('powershell', ['-NoProfile', '-Command',
    `$p = Get-CimInstance Win32_Process -Filter "ProcessId=${pid}" -ErrorAction SilentlyContinue; `
    + 'if ($p) { "$($p.CreationDate.ToUniversalTime().Ticks)|$($p.ExecutablePath)|$($p.Name)" }']);
  const text = (result.stdout || '').trim();
  if (!text.includes('|')) return null;
  const [ticks, executable, name] = text.split('|');
  if (!/^\d+$/.test(ticks)) return null;
  return { pid, creationTicks: Number(ticks), executable: executable || null, name: name || null };
}

/**
 * The whole process table, as parent -> children.
 *
 * Needed because a pid is not always the process that *does* the thing. A venv's `python.exe` is a redirector
 * stub: `python.exe -m uvicorn ...` starts, and the interpreter that actually binds the port is its **child**.
 * So "the port is held by a pid other than the one we recorded" is not evidence of a foreign process, and a
 * check that treated it as one would refuse to start a perfectly healthy instance.
 */
export function processChildren() {
  const result = run('powershell', ['-NoProfile', '-Command',
    'Get-CimInstance Win32_Process | ForEach-Object { "$($_.ProcessId)|$($_.ParentProcessId)" }'], { timeoutMs: 30_000 });
  const map = new Map();
  for (const line of (result.stdout || '').trim().split(/\r?\n/)) {
    const [pid, parent] = line.split('|').map(value => Number(value));
    if (!Number.isInteger(pid) || !Number.isInteger(parent)) continue;
    if (!map.has(parent)) map.set(parent, []);
    map.get(parent).push(pid);
  }
  return map;
}

/** `pid` and every descendant of it, from a snapshot already taken. */
export function processTree(pid, children = processChildren()) {
  const seen = new Set([pid]);
  const queue = [pid];
  while (queue.length) {
    const next = queue.shift();
    for (const child of children.get(next) ?? []) {
      if (seen.has(child)) continue;
      seen.add(child);
      queue.push(child);
    }
  }
  return seen;
}

/**
 * Whether a live process is the one this instance recorded.
 *
 * Three independent facts, all read from the OS rather than from the pid file:
 *
 * 1. the pid exists;
 * 2. its creation time equals the recorded one - this is what defeats pid reuse, and it is the reason a
 *    recycled pid cannot be killed by accident;
 * 3. its executable matches the recorded one, and either its command line names this instance's own
 *    directories or its command carries the instance-owned marker (the profile this instance created).
 *
 * Port ownership is deliberately **not** part of this decision. It is returned as corroboration for the
 * report only: a port can be held by any process, so it cannot authorise ending one.
 */
export function verifyIdentity(record, paths) {
  const facts = processFacts(record.pid);
  if (!facts) return { verdict: 'gone', detail: 'the pid does not exist' };
  if (record.creationTicks !== undefined && record.creationTicks !== null
    && Number(record.creationTicks) !== facts.creationTicks) {
    return { verdict: 'foreign',
      detail: 'the pid exists but its creation time differs from the recorded one (pid reuse)' };
  }
  const recordedExe = record.executable ? String(record.executable).toLowerCase() : null;
  if (recordedExe && facts.executable && facts.executable.toLowerCase() !== recordedExe) {
    return { verdict: 'foreign', detail: 'the pid exists but runs a different executable' };
  }
  const command = processCommandLine(record.pid) ?? '';
  const lower = command.toLowerCase();
  const namesInstance = lower.includes(paths.app.toLowerCase()) || lower.includes(paths.data.toLowerCase());
  const hasMarker = Boolean(record.marker) && command.includes(String(record.marker));
  if (!namesInstance && !hasMarker) {
    return { verdict: 'foreign',
      detail: 'the pid exists but nothing in it identifies this instance (no path, no marker)' };
  }
  const port = record.port !== undefined ? portOwner(record.port) : null;
  return { verdict: 'ours', detail: namesInstance ? 'command line names this instance'
    : 'command carries this instance\'s marker', facts,
  portHeldBy: port, portIsOurs: port !== null && port === record.pid };
}

/** Every process this instance has launched, newest last. Its own record, not a system-wide file. */
export function appendHistory(paths, record) {
  const file = join(paths.run, 'history.jsonl');
  mkdirSync(paths.run, { recursive: true });
  try {
    const line = JSON.stringify({ ...record, at: new Date().toISOString() }) + '\n';
    writeFileSync(file, existsSync(file) ? readFileSync(file, 'utf8') + line : line, 'utf8');
  } catch { /* history is an aid, not a requirement */ }
}

export function readHistory(paths) {
  const file = join(paths.run, 'history.jsonl');
  if (!existsSync(file)) return [];
  return readFileSync(file, 'utf8').split('\n').filter(Boolean).map(line => {
    try { return JSON.parse(line); } catch { return null; }
  }).filter(Boolean);
}

/** Whether a path is inside the package directory this script was loaded from. */
export const insideRepo = (path) => resolve(path).toLowerCase().startsWith(APP_ROOT.toLowerCase() + sep);

/**
 * Every live process whose own command line names this instance's program or data directory.
 *
 * This is the evidence `stop` uses to find a process no pid file recorded (an earlier failed start, or a
 * helper the launcher spawned). It is deliberately **not** a sweep by process name or by port: the filter is
 * "this process is running from this instance's directory", which is a fact about the process, not a guess
 * about what it might be.
 */
export function processesOfInstance(paths) {
  // The comparison happens *inside* PowerShell, and only `pid|name` crosses back. Two reasons: the instance
  // path may contain non-ASCII characters, and PowerShell 5.1 writes non-ASCII stdout in the console codepage
  // rather than UTF-8 — matching on this side compared real characters against mojibake and found nothing,
  // which made `stop` claim a live service was not its own.
  //
  // The filter is a verifiable instance marker: the process's own command line names this instance's app or
  // data directory. It is not a name sweep and not a port sweep.
  const script = [
    `$app = '${paths.app.replace(/'/g, "''")}'`,
    `$data = '${paths.data.replace(/'/g, "''")}'`,
    'Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and '
      + '($_.CommandLine.Contains($app) -or $_.CommandLine.Contains($data)) } | '
      + 'ForEach-Object { "$($_.ProcessId)|$($_.CreationDate.ToUniversalTime().Ticks)|$($_.Name)" }',
  ].join('\n');
  const result = run('powershell', ['-NoProfile', '-Command', script], { timeoutMs: 30_000 });
  return (result.stdout || '').split('\n')
    .map(line => line.trim())
    .filter(line => /^\d+\|/.test(line))
    .map(line => {
      const [pid, ticks, name] = line.split('|');
      return { pid: Number(pid), creationTicks: Number(ticks), name: name || 'unknown' };
    })
    .filter(entry => entry.pid !== process.pid);
}

/**
 * How to launch this machine's DSH without a shell.
 *
 * The npm install exposes `dsh.cmd`, and Node refuses to spawn a `.cmd` without `shell: true` (`EINVAL`).
 * A shell is the wrong answer here: it would re-parse the argument list, and the instance's paths contain
 * spaces and non-ASCII characters. So the shim's real entry point is read from the package's own `bin` field
 * and launched with `node` directly, which keeps every argument a single argv entry.
 */
export function resolveDshLaunch() {
  const candidates = [
    process.env.DSH_BIN_DIR,
    process.env.APPDATA ? join(process.env.APPDATA, 'npm', 'node_modules', '@deepseek-ai', 'dsh') : null,
    process.env.ProgramFiles ? join(process.env.ProgramFiles, 'nodejs', 'node_modules', '@deepseek-ai', 'dsh') : null,
  ].filter(Boolean);
  for (const directory of candidates) {
    const pkg = readJson(join(directory, 'package.json'));
    const bin = typeof pkg?.bin === 'string' ? pkg.bin : pkg?.bin?.dsh;
    if (!bin) continue;
    const entry = join(directory, bin);
    if (existsSync(entry)) return { command: process.execPath, args: [entry], how: `node ${entry}` };
  }
  return { command: 'dsh', args: [], how: 'dsh (from PATH)' };
}
