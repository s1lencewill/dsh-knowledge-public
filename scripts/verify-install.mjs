/**
 * `verify-install` — does the **installed** instance still match the source it was built from?
 *
 * Why this exists as its own check. An instance is a *copy*: `trial/install.mjs` stages `PROGRAM_FILES` into
 * `<instance>/app`, hashes a subset into `MANIFEST.sha256.json`, and swaps it in. So "the tests pass" says
 * nothing about which build the running DSH loads — a stale `lib/`, a client bundle that was never rebuilt, or a
 * tool added to `src/` but not to the installed artifact all look identical from the repository side. This step
 * reads the artifact that would actually be loaded and compares it with the source.
 *
 * It checks, in order:
 *
 * 1. the manifest hashes still describe the files on disk (an edited instance is reported, not assumed fine);
 * 2. the installed entry point loads and registers **exactly** the tools `src/index.ts` declares — no missing
 *    tool, no extra one, and no duplicate;
 * 3. the installed client bundle is byte-identical to the installed `ui/source-card.js` and to the repository's
 *    `ui/source-card.js`, and registers one keyed slot per tool;
 * 4. the installed browser half and the installed `lib/failures.js` declare the same failure, parameter and tool
 *    sets, so a card cannot render a code the host half never emits;
 * 5. `package.json`'s `main` resolves and `cordis.patch.yml` still carries the plugin row.
 *
 * Usage: `node scripts/verify-install.mjs --target <instance-root>` (or `DSH_KNOWLEDGE_TRIAL_HOME`).
 */
import { createHash } from 'node:crypto';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { verifyPayloadDirectory, MANIFEST_NAME } from '../trial/artifact.mjs';

const flag = (name, fallback) => {
  const index = process.argv.indexOf(`--${name}`);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
};
const repository = resolve(import.meta.dirname, '..');
const target = resolve(flag('target', process.env.DSH_KNOWLEDGE_TRIAL_HOME ?? join(repository, '.local', 'no-instance')));
const app = join(target, 'app');

const results = [];
const check = (name, fn) => {
  let outcome;
  try { outcome = fn(); } catch (error) { outcome = { state: 'fail', detail: `${error?.message ?? error}`.slice(0, 200) }; }
  const state = outcome === true ? 'pass' : (outcome && outcome.state) || 'fail';
  results.push({ name, state, detail: outcome === true ? null : (outcome?.detail ?? null) });
  const mark = state === 'pass' ? 'ok  ' : state === 'unverified' ? '??  ' : 'FAIL';
  console.log(`${mark} ${name}${outcome === true || !outcome?.detail ? '' : ` — ${outcome.detail}`}`);
  return state === 'pass';
};

const sha256 = (path) => createHash('sha256').update(readFileSync(path)).digest('hex');
/** Every `knowledge_*` tool name a source or artifact string declares. */
const toolNamesIn = (text) => [...new Set(text.match(/'knowledge_[a-z_]+'/g) ?? [])].map(name => name.slice(1, -1));

console.log(`instance: ${target}`);
console.log(`app:      ${app}\n`);

if (!existsSync(join(app, 'package.json'))) {
  console.log(`FAIL not an installed instance: ${join(app, 'package.json')} does not exist`);
  process.exit(1);
}

// --- 1. the manifest still describes what is on disk
const manifestPath = join(target, 'MANIFEST.sha256.json');
check('the install manifest exists', () => existsSync(manifestPath)
  ? true : { state: 'fail', detail: `${manifestPath} is missing` });
if (existsSync(manifestPath)) {
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
  const entries = Object.entries(manifest.files ?? {});
  const drifted = entries.filter(([relative, digest]) => existsSync(join(app, relative))
    ? sha256(join(app, relative)) !== digest : relative);
  check(`manifest hashes match the ${entries.length} files on disk`, () => drifted.length === 0
    ? true : { state: 'fail', detail: `changed or missing: ${drifted.map(([relative]) => relative).join(', ')}` });
}

if (existsSync(join(app, MANIFEST_NAME))) {
  check('complete release payload still matches its manifest', () => verifyPayloadDirectory(app).ok);
}

// --- 1b. the *copied* program files still match the sources they were copied from
//
// The manifest check above only covers the eight load-bearing paths it happens to hash, which means a change to
// `engine/pipeline.py` or `trial/install.mjs` that was never reinstalled is invisible to it - and an instance
// whose engine half predates its plugin half is a real, confusing state. So every verbatim-copied tree is
// compared, file by file, against the repository.
//
// `lib/` is deliberately **not** in this list: it is build output generated from `src/`, so it cannot be
// compared byte for byte. Its consistency is covered by the tool-registration and bundle checks below.
const COPIED = ['engine', 'ui', 'trial', 'tools', 'samples', 'cordis.patch.yml', 'package.json',
  'README.md', 'LICENSE-DRAFT.md', 'THIRD-PARTY-NOTICES.md', 'VERSION'];
const SKIP_DIRS = new Set(['__pycache__', 'node_modules', '.git']);
const listFiles = (root) => {
  const out = [];
  const walk = (dir) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (entry.isDirectory()) { if (!SKIP_DIRS.has(entry.name)) walk(join(dir, entry.name)); continue; }
      if (entry.isFile() && entry.name !== '.npmignore') out.push(relative(root, join(dir, entry.name)).split('\\').join('/'));
    }
  };
  if (existsSync(root)) walk(root);
  return out.sort();
};
check('every copied program file matches the source it was installed from', () => {
  const changed = []; const missing = []; const extra = [];
  for (const name of COPIED) {
    const source = join(repository, name);
    const installed = join(app, name);
    if (!existsSync(source)) continue;
    if (statSync(source).isDirectory()) {
      const sources = new Set(listFiles(source));
      const installeds = new Set(listFiles(installed));
      for (const file of sources) {
        if (!installeds.has(file)) missing.push(`${name}/${file}`);
        else if (sha256(join(source, file)) !== sha256(join(installed, file))) changed.push(`${name}/${file}`);
      }
      for (const file of installeds) if (!sources.has(file)) extra.push(`${name}/${file}`);
    } else if (!existsSync(installed)) missing.push(name);
    else if (sha256(source) !== sha256(installed)) changed.push(name);
  }
  if (changed.length || missing.length || extra.length) {
    return { state: 'fail', detail: `changed: ${changed.slice(0, 6).join(', ') || 'none'}; `
      + `missing: ${missing.slice(0, 6).join(', ') || 'none'}; extra: ${extra.slice(0, 6).join(', ') || 'none'}`
      + (changed.length + missing.length + extra.length > 18 ? ' (+more)' : '') };
  }
  return true;
});

// --- 2. the installed entry point registers exactly the source's tools
const sourceTools = (() => {
  const text = readFileSync(join(repository, 'src', 'index.ts'), 'utf8');
  // The tool table is the only place a tool name is paired with `parameters`, so this is the declared set.
  return [...new Set([...text.matchAll(/name: '(knowledge_[a-z_]+)', description/g)].map(match => match[1]))].sort();
})();
check(`the source declares its tools (${sourceTools.length})`, () => sourceTools.length >= 4
  ? true : { state: 'fail', detail: `found ${sourceTools.length}` });

const installedEntry = join(app, 'package.json');
const main = existsSync(installedEntry) ? JSON.parse(readFileSync(installedEntry, 'utf8')).main : null;
check('package.json main points at a file that exists', () => {
  if (typeof main !== 'string' || !main) return { state: 'fail', detail: 'no main field' };
  return existsSync(join(app, main)) ? true : { state: 'fail', detail: `${main} is missing` };
});

// The instance resolves the SDK through its own `node_modules`, so importing the installed entry is the same
// load a real host performs - not a re-import of the repository's build.
const loadInstalled = async () => {
  const url = pathToFileURL(join(app, main)).href;
  return import(url);
};
let installedTools = null;
try {
  const module = await loadInstalled();
  const registered = [];
  module.apply({ tools: { register: tool => registered.push(tool) } });
  installedTools = registered.map(tool => tool.name).sort();
} catch (error) {
  installedTools = null;
  check('the installed entry point loads', () => ({ state: 'fail', detail: `${error?.message ?? error}`.slice(0, 180) }));
}
if (installedTools) {
  check(`the installed entry point registers every source tool (${sourceTools.length})`, () => {
    const missing = sourceTools.filter(name => !installedTools.includes(name));
    const extra = installedTools.filter(name => !sourceTools.includes(name));
    if (missing.length || extra.length) {
      return { state: 'fail', detail: `missing: ${missing.join(', ') || 'none'}; extra: ${extra.join(', ') || 'none'}` };
    }
    const duplicates = installedTools.filter((name, index) => installedTools.indexOf(name) !== index);
    return duplicates.length === 0 ? true : { state: 'fail', detail: `duplicated: ${duplicates.join(', ')}` };
  });
}

// --- 3. the served client bundle is the tracked browser half, in both copies
const bundle = join(app, 'lib', 'browser.js');
const installedUi = join(app, 'ui', 'source-card.js');
const repositoryUi = join(repository, 'ui', 'source-card.js');
check('the installed client bundle is byte-identical to the installed browser half', () => {
  if (!existsSync(bundle) || !existsSync(installedUi)) return { state: 'fail', detail: 'a bundle file is missing' };
  return sha256(bundle) === sha256(installedUi) ? true : { state: 'fail', detail: 'lib/browser.js != ui/source-card.js' };
});
check('the installed browser half is byte-identical to the repository source', () => {
  if (!existsSync(repositoryUi)) return { state: 'fail', detail: 'the repository has no ui/source-card.js' };
  if (!existsSync(installedUi)) return { state: 'fail', detail: 'the installed ui/source-card.js is missing' };
  return sha256(installedUi) === sha256(repositoryUi)
    ? true : { state: 'fail', detail: 'rebuild the client bundle and reinstall' };
});
check('the installed build is not older than the repository source', () => {
  // A hash comparison cannot see a *newer* repository change that was never rebuilt, which is the failure mode
  // this guard exists for, so the tracked sources are compared too.
  const installedNames = toolNamesIn(readFileSync(installedUi, 'utf8'));
  const repositoryNames = toolNamesIn(readFileSync(repositoryUi, 'utf8'));
  return installedNames.sort().join(',') === repositoryNames.sort().join(',')
    ? true : { state: 'fail', detail: `tools differ: ${installedNames.join(', ')} vs ${repositoryNames.join(', ')}` };
});

// --- 4. the two halves declare the same sets
const members = (text, name) => {
  const block = new RegExp(`${name}\\s*=\\s*\\[([\\s\\S]*?)\\]`).exec(text);
  if (!block) return null;
  return [...block[1].matchAll(/'([A-Za-z_]+)'/g)].map(match => match[1]).sort();
};
check('the installed halves declare the same failure code, tool and parameter sets', () => {
  const host = readFileSync(join(app, 'lib', 'failures.js'), 'utf8');
  const client = readFileSync(installedUi, 'utf8');
  const hostCodes = members(host, 'FAILURE_CODES') ?? members(host, 'FAILURE_CODES');
  const clientAdvice = [...client.matchAll(/^\s{6}(KNOWLEDGE_[A-Z_]+):\s*\{/gm)].map(match => match[1]).sort();
  if (!hostCodes) return { state: 'fail', detail: 'the host half declares no FAILURE_CODES' };
  if (hostCodes.join(',') !== clientAdvice.join(',')) {
    return { state: 'fail', detail: `codes differ: host ${hostCodes.length} vs card ${clientAdvice.length}` };
  }
  const pairs = [['FAILURE_TOOLS', 'FAILURE_TOOLS'], ['FAILURE_PARAMETERS', 'FAILURE_PARAMETERS'],
    ['KNOWN_SERVICE_CODES', 'SERVICE_CODES']];
  for (const [hostName, clientName] of pairs) {
    const left = members(host, hostName);
    const right = members(client, clientName);
    if (!left || !right) return { state: 'fail', detail: `${hostName}/${clientName} missing on one side` };
    if (left.join(',') !== right.join(',')) return { state: 'fail', detail: `${hostName} differs from ${clientName}` };
  }
  return true;
});

// --- 5. the plugin still declares itself to the host
check('cordis.patch.yml still carries the plugin row', () => {
  const patch = join(app, 'cordis.patch.yml');
  if (!existsSync(patch)) return { state: 'fail', detail: 'cordis.patch.yml is missing' };
  const text = readFileSync(patch, 'utf8');
  return /name:\s*dsh-knowledge/.test(text) ? true : { state: 'fail', detail: 'no dsh-knowledge row' };
});
check('the installed client bundle declares one keyed slot per tool', () => {
  const text = readFileSync(bundle, 'utf8');
  const declared = (members(text, 'TOOL_NAMES') ?? []).sort();
  if (!declared.length) return { state: 'fail', detail: 'the bundle declares no TOOL_NAMES' };
  const expected = (members(readFileSync(join(repository, 'ui', 'source-card.js'), 'utf8'), 'TOOL_NAMES') ?? []).sort();
  if (declared.join(',') !== expected.join(',')) {
    return { state: 'fail', detail: `bundle ${declared.length} vs source ${expected.length}` };
  }
  const missing = (installedTools ?? []).filter(name => !declared.includes(name));
  return missing.length === 0 ? true
    : { state: 'fail', detail: `no card slot for: ${missing.join(', ')}` };
});

// --- the engine half, when this instance carries one
const engineTools = join(app, 'engine', 'service.py');
check('the installed service exposes the enumeration route', () => {
  if (!existsSync(engineTools)) return { state: 'fail', detail: 'engine/service.py is missing' };
  const text = readFileSync(engineTools, 'utf8');
  if (!/@app\.post\("\/catalogue"\)/.test(text)) return { state: 'fail', detail: 'no /catalogue route' };
  return /def perform_catalogue/.test(text) ? true : { state: 'fail', detail: 'no perform_catalogue' };
});

const failed = results.filter(result => result.state === 'fail');
const unverified = results.filter(result => result.state === 'unverified');
console.log(`\n${results.length} checks: ${results.length - failed.length - unverified.length} passed, `
  + `${failed.length} failed, ${unverified.length} unverified`);
console.log(`instance files: ${readdirSync(app).length} entries under ${app}`);
process.exit(failed.length ? 1 : 0);
