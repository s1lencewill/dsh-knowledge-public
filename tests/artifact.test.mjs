import test from 'node:test';
import assert from 'node:assert/strict';
import { gzipSync } from 'node:zlib';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { unpackArchive, safeMember, inspectMembers, makeManifest, auditMembers, verifyPayloadDirectory } from '../trial/artifact.mjs';

function archive(entries) {
  const parts = [];
  for (const [name, value, type = '0'] of entries) {
    const body = Buffer.from(value), head = Buffer.alloc(512);
    head.write(name); head.write('0000644', 100); head.write('0000000', 108); head.write('0000000', 116);
    head.write(body.length.toString(8).padStart(11, '0') + '\0', 124);
    head.write('00000000000\0', 136); head.fill(32, 148, 156); head.write(type, 156);
    head.write('ustar\0', 257); head.write('00', 263);
    const sum = head.reduce((n, b) => n + b, 0);
    head.write(sum.toString(8).padStart(6, '0') + '\0 ', 148);
    parts.push(head, body, Buffer.alloc((512 - body.length % 512) % 512));
  }
  return gzipSync(Buffer.concat([...parts, Buffer.alloc(1024)]));
}
const payload = () => new Map([
  ['package.json', Buffer.from(JSON.stringify({ name: 'dsh-knowledge', version: '0.1.0', peerDependencies: {}, engines: { node: '>=24' } }))],
  ['engine/requirements-win-py313.lock', Buffer.from('synthetic==1')],
  ['README.md', Buffer.from('Safe payload.')],
]);
function sealed() {
  const members = payload();
  members.set('RELEASE-MANIFEST.json', Buffer.from(JSON.stringify(makeManifest(members, { commit: 'a'.repeat(40), dirty: true }))));
  return members;
}
test('actual tar bytes are read and unsafe paths, case aliases and links fail closed', () => {
  assert.equal(unpackArchive(archive([['package/README.md', 'safe']])).get('README.md').toString(), 'safe');
  for (const path of ['package/../outside', 'package/C:drive', '/absolute', 'package/dir/CON.txt', 'package/end.']) {
    assert.throws(() => safeMember(path));
  }
  assert.throws(() => unpackArchive(archive([['package/A.txt', 'a'], ['package/a.txt', 'b']])), /DUPLICATE/);
  assert.throws(() => unpackArchive(archive([['package/link', '', '2']])), /LINK/);
});
test('tar checksum corruption and incomplete end are rejected', () => {
  const raw = Buffer.alloc(512); raw.write('package/test');
  assert.throws(() => unpackArchive(gzipSync(raw)));
  assert.throws(() => unpackArchive(Buffer.from('not gzip')));
});
test('cache files, binaries, personal paths and credential shapes are reported without content', () => {
  const secret = 'sk-' + 'a1b2c3d4'.repeat(8);
  const members = new Map([
    ['engine/__pycache__/x.pyc', Buffer.from('bytecode')],
    ['tools/tool.exe', Buffer.from('exe')],
    ['README.md', Buffer.from(secret + ' C:\\Users\\private-user\\papers')],
  ]);
  const result = inspectMembers(members);
  assert.ok(result.some(v => v.rule === 'cache-or-private-entry'));
  assert.ok(result.some(v => v.rule === 'unapproved-binary'));
  assert.ok(result.some(v => v.rule === 'credential-shape'));
  assert.ok(result.some(v => v.rule === 'personal-home-path'));
  assert.ok(!JSON.stringify(result).includes(secret));
});
test('manifest catches modified, missing and unlisted members and metadata drift', () => {
  assert.equal(auditMembers(sealed()).ok, true);
  const changed = sealed(); changed.set('README.md', Buffer.from('tampered'));
  assert.ok(auditMembers(changed).findings.some(f => f.rule === 'hash-mismatch'));
  const missing = sealed(); missing.delete('README.md');
  assert.ok(auditMembers(missing).findings.some(f => f.rule === 'missing-member'));
  const extra = sealed(); extra.set('extra.txt', Buffer.from('x'));
  assert.ok(auditMembers(extra).findings.some(f => f.rule === 'unlisted-member'));
  const invalid = sealed(); const manifest = JSON.parse(invalid.get('RELEASE-MANIFEST.json')); manifest.version = 'false';
  invalid.set('RELEASE-MANIFEST.json', Buffer.from(JSON.stringify(manifest)));
  assert.equal(auditMembers(invalid).ok, false);
});
test('installed directory verification catches changed and extra files', () => {
  const root = mkdtempSync(join(tmpdir(), 'knowledge-payload-'));
  try {
    for (const [name, bytes] of sealed()) {
      mkdirSync(join(root, name, '..'), { recursive: true }); writeFileSync(join(root, name), bytes);
    }
    assert.equal(verifyPayloadDirectory(root).ok, true);
    writeFileSync(join(root, 'README.md'), 'corrupt');
    assert.equal(verifyPayloadDirectory(root).ok, false);
  } finally { rmSync(root, { recursive: true, force: true }); }
});
