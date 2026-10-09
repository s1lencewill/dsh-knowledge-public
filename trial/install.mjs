/**
 * `install` —put a self-contained trial instance into a directory the user names.
 *
 * What it does, in order: show what it will create, copy the program files, create the data directory and the
 * sample library, generate a local service token, write the instance config, create the DSH profile, and
 * report the next command. It is idempotent: running it again over an existing instance refreshes `app/`,
 * leaves `data/` alone, and never overwrites a user's own library or config.
 *
 * What it deliberately does **not** do: download anything, touch the user's existing DSH profiles, take or
 * write a provider credential, or place a container runtime. The runtime contains an AGPL-licensed modified
 * `rga.exe` and two GPL packages (see THIRD-PARTY-NOTICES.md), so it is never part of the package; a user who
 * wants it runs the separate `--runtime-from` step against a source they name, or `doctor` reports it missing.
 */
import { createHash, randomUUID } from 'node:crypto';
import { copyFileSync, existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { basename, dirname, join, resolve } from 'node:path';
import { verifyPayloadDirectory, MANIFEST_NAME } from './artifact.mjs';
import { bindHostDependencies } from './host-dependencies.mjs';
import {
  APP_ROOT, DOC_FILES, LEGACY_MODEL_BASE_URL, PROGRAM_FILES, REQUIRED_APP_PATHS, check, copyTree, defaultTarget,
  hashFile, info, instancePaths, launchDetached, newToken, ok, parseFlags, readJson, requireInstance, run, say,
  writeJson, writeProfile,
} from './lib.mjs';

// Ports and model settings default to null so "not given" is distinguishable from "given the default": a
// re-install over an existing instance must never silently move its ports or repoint its provider.
const flags = parseFlags(process.argv.slice(2), {
  target: null, python: null, 'runtime-from': null, 'tools-from': null, 'service-port': null, 'web-port': null,
  'model-base-url': null, 'model-api-key-env': null, 'model-provider': null,
  'title-model': null, 'engine-base-url': null, 'engine-model': null, 'engine-api-key': null,
  'engine-api-key-env': null, 'engine-env-file': null,
  picker: null,
  force: false,
});

if (flags.help) {
  say('node trial/install.mjs --target <dir> [--python <python.exe>]');
  say('                        [--runtime-from <dir>] [--tools-from <dir>] [--service-port <n>] [--web-port <n>]');
  say('                        [--picker browse|native|auto]   (default browse: an in-app directory dialog)');
  say('                        [--engine-env-file <file>]      LLM_BASE_URL/LLM_MODEL_NAME/LLM_API_KEY for the engine');
  say('                        [--engine-api-key-env <NAME>]   read the engine key from that environment variable');
  process.exit(0);
}

const target = resolve(flags.target || defaultTarget());
const paths = instancePaths(target);
const existing = existsSync(paths.config);
const python = flags.python || (existing ? readJson(paths.config)?.python : null) || 'python';

// Both spellings are accepted here for the same reason as in step 5: `parseFlags` keys by the literal flag text,
// and a header that read the wrong key printed "none" while the step below was copying a whole runtime.
const runtimeFromArg = flags['runtime-from'] ?? flags.runtimeFrom ?? null;
const toolsFromArg = flags['tools-from'] ?? flags.toolsFrom ?? null;

say('');
say(`Trial instance:  ${target}`);
say(`  program files   ${paths.app}        (replaceable on upgrade)`);
say(`  user data       ${paths.data}        (libraries, token, logs, runtime; kept by uninstall)`);
say(`  DSH home        ${paths.dshHome}     (its own private profile, not your own)`);
say(`  service port    ${flags['service-port']}`);
say(`  web port        ${flags['web-port']}`);
say(`  python          ${python}`);
say(`  runtime source  ${runtimeFromArg ?? '(none: doctor will report it missing)'}`);
say(`  tool binaries   ${toolsFromArg ?? (runtimeFromArg ? 'searched beside the runtime source' : '(none)')}`);
say('');

// --- 1. the package must actually be a package
say('Checking the package being installed');
let fatal = false;
for (const relative of REQUIRED_APP_PATHS) {
  const present = check(`package contains ${relative}`, () => existsSync(join(APP_ROOT, relative)) ? true
    : { state: 'fail', detail: 'missing' });
  if (present === 'fail') fatal = true;
}
if (fatal) {
  say('');
  say('This directory is not an installable package. Rebuild it, then run install from the package root.');
  process.exit(1);
}

// An audited distribution must match its full file list before anything is staged.
// Source checkouts without a release manifest retain the development install path.
if (existsSync(join(APP_ROOT, MANIFEST_NAME))) {
  const integrity = verifyPayloadDirectory(APP_ROOT);
  if (!integrity.ok) { say('FAIL RELEASE_PAYLOAD_MISMATCH'); process.exit(1); }
  ok('complete release payload matches its manifest');
}

// --- 2. program files, staged and swapped.
//
// Copied from an explicit list, not by pruning the source tree: a prune-based copy is a guess about what is safe
// to exclude, and it failed exactly that way - the first version walked the whole checkout and started copying
// the development `.venv`.
//
// The copy goes into `app.new` and is only swapped in at the end. An upgrade that fails half way therefore
// leaves the **previous, complete** `app` in place, so there is no state where the installer reported success
// while the program is incomplete. `app.old` is kept until the swap has succeeded.
mkdirSync(paths.app, { recursive: true });
const before = existing ? readJson(paths.config) : null;
const staging = `${paths.app}.new`;
const previousApp = `${paths.app}.old`;
rmSync(staging, { recursive: true, force: true });
let copied = 0;
try {
  for (const relative of [...PROGRAM_FILES, ...DOC_FILES]) {
    const from = join(APP_ROOT, relative);
    if (!existsSync(from)) {
      info(`${relative} is not present in this package; skipped`);
      continue;
    }
    copyTree(from, join(staging, relative), { skip: (name) => ['.git', '__pycache__', 'node_modules'].includes(name) });
    copied += 1;
  }
  bindHostDependencies(staging);
} catch (error) {
  // A copy that cannot finish (a locked file, a full disk, a path that is too long) used to escape as a raw
  // exception, leaving a half-written `app.new` behind and printing a stack trace instead of a sentence. A
  // failed staging attempt is allowed to fail; it is not allowed to leave debris that looks like a program.
  rmSync(staging, { recursive: true, force: true });
  check('program files staged', () => ({ state: 'fail',
    detail: `${error?.code ?? ''} ${error?.message ?? error}`.trim().slice(0, 160) }));
  say('');
  say(`The staged copy could not be completed, so nothing was changed: ${paths.app} is untouched and the`);
  say('partial copy has been removed. Close whatever is holding a file in the package or the instance and retry.');
  process.exit(1);
}
// A package that staged nothing useful must never replace a working instance.
let stagedFiles = 0;
for (const relative of REQUIRED_APP_PATHS) if (existsSync(join(staging, relative))) stagedFiles += 1;
if (stagedFiles !== REQUIRED_APP_PATHS.length) {
  rmSync(staging, { recursive: true, force: true });
  say('');
  say(`Refusing to install: the staged copy has ${stagedFiles} of ${REQUIRED_APP_PATHS.length} required files.`);
  say('The existing instance was not touched.');
  process.exit(1);
}
if (existsSync(join(staging, MANIFEST_NAME))) {
  const integrity = verifyPayloadDirectory(staging);
  if (!integrity.ok) {
    rmSync(staging, { recursive: true, force: true });
    say('FAIL STAGED_RELEASE_PAYLOAD_MISMATCH'); process.exit(1);
  }
}
ok(`program files staged in ${staging} (${copied} entries; the list is in trial/lib.mjs PROGRAM_FILES)`);

// --- 3. data directory, sample library, token, config
mkdirSync(paths.data, { recursive: true });
mkdirSync(paths.logs, { recursive: true });
mkdirSync(paths.run, { recursive: true });
// The samples are read from the **staged** copy, not from `paths.app`. On a first install there is no `paths.app`
// yet, and reading the sample library from a directory that does not exist yet copied nothing while still
// printing "sample library created" - the instance then had no library at all and `doctor` failed two checks
// further down. A first install is exactly the case this round is about, so it is the case that has to work.
const sampleSource = join(staging, 'samples');
if (!existsSync(paths.libraries)) {
  const samplesStaged = check('sample library files', () => existsSync(join(sampleSource, 'libraries.template.json'))
    ? true : { state: 'fail', detail: `${sampleSource} has no libraries.template.json` });
  if (samplesStaged === 'pass') {
    copyTree(sampleSource, paths.data, { skip: (name) => name === 'README.md' });
    const libraries = readJson(join(sampleSource, 'libraries.template.json'));
    // The sample library's paths are rewritten to the instance, so the shipped template carries no path from
    // the machine it was built on.
    for (const library of libraries.libraries ?? []) {
      library.workspace = resolve(paths.data);
      library.root = resolve(paths.data, library.root);
    }
    writeJson(paths.libraries, libraries);
    const roots = (libraries.libraries ?? []).map(library => library.root);
    if (!roots.length || roots.some(root => !existsSync(root))) {
      check('sample library roots', () => ({ state: 'fail', detail: `no usable library root among ${roots.join(', ')}` }));
    } else {
      ok(`sample library created (${roots.length} root(s): ${roots.join(', ')}; synthetic PDF + Markdown, `
        + 'self-made, not taken from any real literature)');
    }
  }
} else {
  info('existing library config kept (install never overwrites user data)');
}
if (!existsSync(paths.token)) {
  writeFileSync(paths.token, newToken(), 'utf8');
  ok('local service token generated into the instance (never printed, never in the package)');
} else info('existing service token kept');

/** A flag wins; otherwise the existing instance's value; otherwise the built-in default. */
const pick = (flag, previous, fallback) => (flag !== null && flag !== undefined && flag !== true ? flag : (previous ?? fallback));

/** A per-instance profile name: stable for a given target, and not shared with any other instance. */
const profileName = (path) => 'kn-trial-' + createHash('sha256').update(resolve(path).toLowerCase())
  .digest('hex').slice(0, 8);

const config = {
  version: readJson(join(APP_ROOT, 'package.json'))?.version ?? 'unknown',
  installedAt: before?.installedAt ?? new Date().toISOString(),
  updatedAt: new Date().toISOString(),
  profileName: before?.profileName ?? profileName(target),
  python: flags.python ? resolve(flags.python) : (before?.python ?? python),
  servicePort: Number(pick(flags['service-port'], before?.servicePort, 18765)),
  webPort: Number(pick(flags['web-port'], before?.webPort, 18766)),
  modelProvider: pick(flags['model-provider'], before?.modelProvider, 'deepseek-official'),
  // Null means "do not override the adapter's own base URL". DSH 0.2's `llm-deepseek` defaults to
  // `https://api.deepseek.com/anthropic`; this installer's older default (`https://api.deepseek.com`) replaced
  // that with a path that answers 404, so only an explicit `--model-base-url` writes a value now.
  modelBaseUrl: pick(flags['model-base-url'], before?.modelBaseUrl, null),
  modelApiKeyEnv: pick(flags['model-api-key-env'], before?.modelApiKeyEnv, 'DEEPSEEK_API_KEY'),
  titleModel: pick(flags['title-model'], before?.titleModel, 'title-stub'),
  engineBaseUrl: pick(flags['engine-base-url'], before?.engineBaseUrl, 'http://127.0.0.1:18784'),
  engineModel: pick(flags['engine-model'], before?.engineModel, 'deepseek-flash'),
  engineApiKey: pick(flags['engine-api-key'], before?.engineApiKey, 'trial-placeholder-not-a-credential'),
  // Where the engine's provider key comes from: a file the user owns, or the name of an environment variable.
  // Neither writes the key into this configuration.
  engineEnvFile: pick(flags['engine-env-file'], before?.engineEnvFile, null),
  engineApiKeyEnv: pick(flags['engine-api-key-env'], before?.engineApiKeyEnv, null),
  // Which directory-picker interaction the profile composes. `browse` is the default because it is the one that
  // works for every operator, including one who only ever sees the page; see `pickerRows` in trial/lib.mjs.
  picker: pick(flags.picker, before?.picker, 'browse'),
  appRoot: paths.app,
};
// An instance installed by an older version carries this installer's own old default. Recording it is not a
// choice either, so the upgrade drops it: the profile then writes no `baseURL`, and DSH 0.2's `llm-deepseek`
// keeps its own default (`https://api.deepseek.com/anthropic`). An explicit `--model-base-url` still wins.
if (config.modelBaseUrl === LEGACY_MODEL_BASE_URL) config.modelBaseUrl = null;
// The config is rewritten **before** the swap, and it is written by the *incoming* version, so a swap that fails
// would otherwise leave an instance whose `app/` is the old release while its `data/config.json` claims the new
// one - a state where the recorded version is wrong and a later reader cannot tell which release is installed.
// The previous file is therefore kept until the swap has succeeded and put back when it has not, so a failed
// upgrade rolls back both halves of the version change, not just the program.
const configBackup = existsSync(paths.config) ? readFileSync(paths.config, 'utf8') : null;
writeJson(paths.config, config);
ok(`instance config written to ${paths.config}`);

// --- 4. the DSH profile
const profile = writeProfile(paths, config);
if (profile.ok) ok(`DSH profile created (plugin resolves to ${paths.app}); directory picker: ${config.picker}`);
else check('DSH profile', () => ({ state: 'fail', detail: profile.detail }));

// --- 4b. swap the staged program in.
//
// Everything above can fail without harm; from here the instance changes version. The old tree is moved aside
// rather than deleted, so a swap that cannot finish leaves the previous version recoverable by hand:
// `app.old` still exists and `app` is either the old tree or the new one, never a mixture.
//
// On **success** the previous tree is kept as `app.previous-<stamp>`, not deleted. The line here used to
// remove it, which meant an upgrade destroyed the only copy of the version it replaced: after the first
// upgrade there was no artefact to roll back *to*, and the message that promised one ("kept ... until the
// next install") was true only until the next install ran. A copy of the program is about a megabyte, and
// the alternative - reconstructing a version from an uncommitted worktree - is not a rollback.
const previousRelease = join(paths.root, `app.previous-${new Date().toISOString().replace(/[:.]/g, '-')}-${randomUUID().slice(0, 8)}`);
// Never delete an existing rollback point, even when two installs happen in one second.
let swapped = false;
try {
  if (existsSync(paths.app)) {
    renameSync(paths.app, previousApp);
  }
  try { renameSync(staging, paths.app); } catch (swapError) {
    // Put the old tree back before reporting, so the instance is not left without a program.
    if (existsSync(previousApp)) renameSync(previousApp, paths.app);
    throw swapError;
  }
  swapped = true;
} catch (error) {
  check('program swap', () => ({ state: 'fail', detail: `${error?.message ?? error}; the previous version is intact` }));
  // Put the previous config back too, for the reason given where it was saved.
  if (configBackup !== null) writeFileSync(paths.config, configBackup, 'utf8');
  // A staged tree that could not be swapped in is dead weight and must not be mistaken for the installed one.
  // It cannot always be removed: if a file inside it is held open by another process, Windows refuses the
  // delete, and that refusal used to escape as a raw EPERM stack trace *after* the failure had been reported.
  // A tree that cannot be removed is named instead, so the next reader knows exactly what is on disk.
  let stagingLeft = false;
  try { rmSync(staging, { recursive: true, force: true }); } catch { stagingLeft = true; }
  say('');
  say('The instance still runs its previous version. Nothing was left half-updated:');
  say(`  program   ${paths.app} (previous release, untouched)`);
  say(`  config    ${configBackup !== null ? 'restored to what it was before this attempt' : 'unchanged'}`);
  say(stagingLeft
    ? `  staging   ${staging} is still there and could not be removed (a file in it is open elsewhere). It is not `
      + 'the installed program: the next install removes it before staging again, or delete it by hand.'
    : '  staging   the partial copy was removed');
  say('A later reader can therefore trust data/config.json to name the release that is actually installed.');
  process.exit(1);
}
if (swapped) {
  // Keep the version this install replaced, under a name that says what it is. It is the artefact a
  // rollback needs: restoring a *matching* program, config and catalogue is the only thing that rolls an
  // upgrade back, and the program half cannot be reconstructed from an uncommitted worktree.
  try {
    renameSync(previousApp, previousRelease);
    ok(`program swapped in (the version it replaced is kept at ${previousRelease})`);
  } catch (error) {
    ok(`program swapped in (the previous version could not be renamed to ${previousRelease}: ${error?.message ?? error})`);
  }
}

// --- 5. optional: place a container runtime from a source the user names.
// Note the flag name: `parseFlags` keys by the literal flag text, so `--runtime-from` arrives as
// `flags['runtime-from']`. The camelCase spelling is accepted too, because reading the wrong key silently
// skipped this whole step once.
const runtimeFrom = runtimeFromArg;
if (runtimeFrom) {
  const from = resolve(runtimeFrom);
  const staged = check('container runtime source', () => existsSync(from) ? true
    : { state: 'fail', detail: `${from} does not exist` });
  if (staged === 'pass') {
    say('  ....  copying the runtime into the instance (this is the AGPL/GPL payload; see THIRD-PARTY-NOTICES.md)');
    copyTree(from, paths.runtime, { skip: (name) => ['work', 'rga-cache', '__pycache__'].includes(name) });
    const manifest = ['stage-manifest.json', 'Manifest.json', 'manifest.json']
      .map(name => join(paths.runtime, name)).find(existsSync);
    if (manifest) ok(`runtime placed (${basename(manifest)} present)`);
    else check('runtime manifest', () => ({ state: 'unverified', detail: 'no manifest found; doctor will refuse to start' }));
    // The pinned tool binaries go beside the runtime: the service copies them into it at startup, and that copy
    // cannot have the same source and destination.
    //
    // Where they come from is not guesswork. `scripts/pin-tools.py` records the exact paths and hashes in
    // `pinned-tools.json`, and that record is the first thing consulted; only then are the obvious names looked
    // for. The first version of this step read `<runtime-from>\rga.exe` and nothing else, which is where the
    // binaries end up *after* a service run has copied them in - so a freshly staged runtime silently produced
    // an instance with no tools, and the failure only showed up later as a missing dependency at startup.
    mkdirSync(paths.tools, { recursive: true });
    const toolsRoot = toolsFromArg ? resolve(toolsFromArg) : dirname(from);
    const toolDirs = toolsFromArg ? [resolve(toolsFromArg)]
      : [dirname(from), join(dirname(from), 'tools'), from];
    const record = toolDirs.map(dir => readJson(join(dir, 'pinned-tools.json'))).find(Boolean);
    const candidates = (name, key) => [
      record?.tools?.[key]?.path,
      ...toolDirs.map(dir => join(dir, name)),
      ...toolDirs.map(dir => join(dir, 'rga-patched', name)),
    ].filter(candidate => typeof candidate === 'string' && existsSync(candidate));
    for (const [name, key] of [['rga.exe', 'rga'], ['rg.exe', 'rg']]) {
      const found = candidates(name, key)[0];
      if (found) {
        copyFileSync(found, join(paths.tools, name));
        const hash = hashFile(join(paths.tools, name));
        const recorded = record?.tools?.[key]?.sha256;
        ok(`${name} placed (${found})${recorded ? (recorded === hash ? ', hash matches pinned-tools.json'
          : `, WARNING hash ${hash.slice(0, 12)}… does not match the recorded ${String(recorded).slice(0, 12)}…`)
          : ', no pinned hash to compare against'}`);
      } else {
        check(`${name} placed`, () => ({ state: 'fail',
          detail: `not found. Looked in ${toolsRoot}${toolsFromArg ? '' : ' (and its rga-patched subdirectory)'} `
            + `and in ${from}. Pass --tools-from <dir> pointing at a directory that contains ${name}, or the `
            + 'service will report the dependency as missing at startup.' }));
      }
    }
  }
} else if (existsSync(paths.runtime)) {
  info('existing container runtime kept');
} else {
  info('no container runtime placed; `doctor` will report it and `start` will refuse until it is');
}

// --- 6. a manifest of what was installed
const manifest = {};
for (const relative of REQUIRED_APP_PATHS) {
  const path = join(paths.app, relative);
  if (existsSync(path)) manifest[relative] = hashFile(path);
}
writeJson(join(target, 'MANIFEST.sha256.json'), { version: config.version, files: manifest });
ok(`manifest written (${Object.keys(manifest).length} load-bearing files hashed)`);

say('');
say('Installed. Next:');
say(`  node trial/doctor.mjs --target "${target}"     # what is ready and what is missing`);
say(`  node trial/start.mjs  --target "${target}"     # verify, start, wait for health`);
say(`  ${join(paths.app, 'docs', 'QUICKSTART.md')}`);
say('');
