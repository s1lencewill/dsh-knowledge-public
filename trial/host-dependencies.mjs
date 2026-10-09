import { createRequire } from 'node:module';
import { existsSync, mkdirSync, readFileSync, realpathSync, symlinkSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';
import { resolveDshLaunch } from './lib.mjs';

export function probePlugin(app) {
  const entry = pathToFileURL(join(resolve(app), 'lib/index.js')).href;
  const result = spawnSync(process.execPath, ['--input-type=module', '-e',
    'try { await import(process.argv[1]); console.log("ok"); } catch (e) { console.error(e.code || e.name); process.exit(1); }', entry],
  { encoding: 'utf8', timeout: 30000, windowsHide: true });
  return { ok: result.status === 0, code: result.error?.code || result.stderr?.trim() || 'IMPORT_FAILED' };
}

// ESM resolves from the real app path, not the profile junction or NODE_PATH.
// Link only the declared peers to the selected host: do not copy a second SDK graph.
export function bindHostDependencies(app, hostEntry = resolveDshLaunch().args[0]) {
  if (!hostEntry) throw new Error('DSH host not found; set DSH_BIN_DIR to its package directory');
  const hostRequire = createRequire(pathToFileURL(resolve(hostEntry)));
  const pkg = JSON.parse(readFileSync(join(app, 'package.json'), 'utf8'));
  const bindings = Object.entries(pkg.peerDependencies || {}).map(([name, version]) => {
    const manifest = hostRequire.resolve(`${name}/package.json`);
    const installed = JSON.parse(readFileSync(manifest, 'utf8'));
    if (installed.version !== version) throw new Error(`Host peer version mismatch: ${name}; expected ${version}, got ${installed.version}`);
    return { name, source: realpathSync(dirname(manifest)), link: join(app, 'node_modules', name) };
  });
  for (const { name, source, link } of bindings) {
    if (existsSync(link) && realpathSync(link) !== source) throw new Error(`Existing peer differs from selected host: ${name}; refusing to overwrite`);
  }
  for (const { source, link } of bindings) {
    if (existsSync(link)) continue;
    mkdirSync(dirname(link), { recursive: true });
    symlinkSync(source, link, process.platform === 'win32' ? 'junction' : 'dir');
  }
  const probe = probePlugin(app);
  if (!probe.ok) throw new Error(`Installed plugin import failed: ${probe.code}`);
  return bindings.map(({ name }) => name);
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  try {
    if (!process.argv[2]) throw new Error('Usage: node trial/host-dependencies.mjs <instance/app>');
    console.log(JSON.stringify({ ok: true, peers: bindHostDependencies(resolve(process.argv[2])) }));
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
