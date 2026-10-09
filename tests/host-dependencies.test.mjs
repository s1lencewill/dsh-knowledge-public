import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, realpathSync, rmSync } from 'node:fs';
import { join, resolve, dirname } from 'node:path';
import { bindHostDependencies, probePlugin } from '../trial/host-dependencies.mjs';

function fixture(t, version = '1.0.0') {
  const base = resolve('.local/host-dependency-tests');
  mkdirSync(base, { recursive: true });
  const root = mkdtempSync(join(base, 'case-'));
  t.after(() => { assert.equal(dirname(root), base); rmSync(root, { recursive: true, force: true }); });
  const app = join(root, 'app 中文');
  const host = join(root, 'host');
  const peer = join(host, 'node_modules/@test/peer');
  mkdirSync(join(app, 'lib'), { recursive: true });
  mkdirSync(peer, { recursive: true });
  writeFileSync(join(app, 'package.json'), JSON.stringify({ type: 'module', peerDependencies: { '@test/peer': '1.0.0' } }));
  writeFileSync(join(app, 'lib/index.js'), 'import { value } from "@test/peer"; if (value !== 42) throw Error("bad peer");');
  writeFileSync(join(peer, 'package.json'), JSON.stringify({ name: '@test/peer', type: 'module', version, exports: { '.': './index.js', './package.json': './package.json' } }));
  writeFileSync(join(peer, 'index.js'), 'export const value = 42;');
  return { app, peer, entry: join(host, 'entry.js') };
}

test('installed app fails without host peers, binds selected host, then imports; idempotent', t => {
  const f = fixture(t);
  assert.equal(probePlugin(f.app).ok, false);
  assert.deepEqual(bindHostDependencies(f.app, f.entry), ['@test/peer']);
  assert.equal(realpathSync(join(f.app, 'node_modules/@test/peer')), realpathSync(f.peer));
  assert.equal(probePlugin(f.app).ok, true);
  bindHostDependencies(f.app, f.entry);
});
test('incompatible host fails before creating bindings', t => {
  const f = fixture(t, '2.0.0');
  assert.throws(() => bindHostDependencies(f.app, f.entry), /version mismatch/);
  assert.equal(probePlugin(f.app).ok, false);
});
test('missing host package is rejected', t => {
  const f = fixture(t);
  assert.throws(() => bindHostDependencies(f.app, join(f.app, 'missing-host.js')), /Cannot find module/);
});
test('probe loads transitive imports instead of checking entry presence', t => {
  const f = fixture(t);
  writeFileSync(join(f.peer, 'index.js'), 'import "missing-transitive-package"; export const value = 42;');
  assert.throws(() => bindHostDependencies(f.app, f.entry), /Installed plugin import failed/);
});
