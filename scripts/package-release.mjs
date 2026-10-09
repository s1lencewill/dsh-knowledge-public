/** Build and audit a private candidate from two actual npm archives. Never publishes. */
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve, basename } from 'node:path';
import { pathToFileURL } from 'node:url';
import { auditArchive, auditMembers, unpackArchive, makeManifest, MANIFEST_NAME, sha256 } from '../trial/artifact.mjs';

const root = resolve(import.meta.dirname, '..');
function npm(args, cwd) {
  const npmCli = join(dirname(process.execPath), 'node_modules', 'npm', 'bin', 'npm-cli.js');
  const run = spawnSync(process.execPath, [npmCli, ...args, '--cache', join(root, '.local/npm-cache')],
    { cwd, encoding: 'utf8', windowsHide: true, timeout: 120000 });
  if (run.status !== 0) throw new Error('NPM_PACK_FAILED');
  return JSON.parse(run.stdout);
}
export function readProvenance(run = spawnSync, cwd = root) {
  const git = args => {
    const result = run('git', args, { cwd, encoding: 'utf8', windowsHide: true, timeout: 15000 });
    if (!result || result.error || result.status !== 0 || result.signal || typeof result.stdout !== 'string') throw new Error('GIT_PROVENANCE_UNAVAILABLE');
    return result.stdout.trim();
  };
  const commit = git(['rev-parse', '--verify', 'HEAD^{commit}']);
  if (!/^([a-f0-9]{40}|[a-f0-9]{64})$/.test(commit)) throw new Error('GIT_COMMIT_INVALID');
  return { commit, dirty: Boolean(git(['status', '--porcelain=v1'])), source: 'working-tree' };
}
export function prepare(out, { allowDirty = false } = {}) {
  const origin = readProvenance();
  if (origin.dirty && !allowDirty) throw new Error('DIRTY_SOURCE_REQUIRES_ALLOW_DIRTY');
  out = resolve(out);
  if (existsSync(out)) throw new Error('OUTPUT_ALREADY_EXISTS');
  mkdirSync(out, { recursive: true });
  const input = join(out, 'input'), stage = join(out, 'stage');
  mkdirSync(input); mkdirSync(stage);
  const first = npm(['pack', '--json', '--ignore-scripts', '--pack-destination', input], root)[0];
  const members = unpackArchive(readFileSync(join(input, first.filename)));
  members.delete(MANIFEST_NAME);
  const before = auditMembers(members, { requireManifest: false });
  if (!before.ok) {
    writeFileSync(join(out, 'audit.json'), JSON.stringify(before, null, 2) + '\n');
    throw new Error('PAYLOAD_AUDIT_FAILED');
  }
  const pkg = JSON.parse(members.get('package.json').toString('utf8'));
  if (!Array.isArray(pkg.files) || !pkg.files.includes(MANIFEST_NAME)) throw new Error('MANIFEST_NOT_DECLARED_IN_PACKAGE_FILES');
  // Preserve the actual npm package.json bytes, including its line endings.
  for (const [path, bytes] of members) {
    const target = join(stage, path); mkdirSync(dirname(target), { recursive: true }); writeFileSync(target, bytes);
  }
  const manifest = makeManifest(members, origin);
  writeFileSync(join(stage, MANIFEST_NAME), JSON.stringify(manifest, null, 2) + '\n');
  const packed = npm(['pack', '--json', '--ignore-scripts', '--pack-destination', out], stage)[0];
  const archive = join(out, packed.filename);
  const audit = auditArchive(archive);
  const report = { ...audit, manifest: undefined, provenance: origin, channel: 'private-candidate',
    betaGate: 'pending-clean-windows-and-interaction-acceptance', publicRelease: false };
  writeFileSync(join(out, 'audit.json'), JSON.stringify(report, null, 2) + '\n');
  writeFileSync(join(out, 'SHA256SUMS'), audit.archiveSHA256 + '  ' + basename(archive) + '\n');
  if (!audit.ok) throw new Error('FINAL_ARCHIVE_AUDIT_FAILED');
  return { archive, report };
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    const [action, ...args] = process.argv.slice(2);
    if (action === 'audit' && args.length === 1) {
      const result = auditArchive(resolve(args[0])); console.log(JSON.stringify({ ...result, manifest: undefined }, null, 2));
      if (!result.ok) process.exitCode = 1;
    } else if (action === 'pack') {
      let out, allowDirty = false;
      for (let i = 0; i < args.length; i++) {
        if (args[i] === '--out' && args[i + 1]) out = args[++i];
        else if (args[i] === '--allow-dirty') allowDirty = true;
        else throw new Error('INVALID_ARGUMENT');
      }
      if (!out) throw new Error('OUTPUT_REQUIRED');
      console.log(JSON.stringify(prepare(out, { allowDirty }), null, 2));
    } else throw new Error('USAGE: pack --out <new-directory> [--allow-dirty] | audit <archive>');
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
