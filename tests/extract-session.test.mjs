// The session extractor must never guess: not which session, not how the bytes were encoded, and not
// whether a batch of question IDs maps one-to-one onto sessions.
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { zstdCompressSync } from 'node:zlib';

const SCRIPT = 'scripts/extract-session.mjs';

function run(args) {
  return spawnSync(process.execPath, [SCRIPT, ...args], { encoding: 'utf8' });
}

/** One synthetic transcript: a header frame plus one frame per line, like the real files. */
function writeSession(root, name, payload) {
  const directory = join(root, name);
  mkdirSync(directory, { recursive: true });
  const header = Buffer.from(JSON.stringify({ type: 'session', id: name }) + '\n');
  const frames = [zstdCompressSync(header)];
  for (const line of payload) frames.push(zstdCompressSync(Buffer.from(JSON.stringify(line) + '\n')));
  writeFileSync(join(directory, 'session.v3.jsonl.zstd'), Buffer.concat(frames));
  return directory;
}

test('two different sessions are extracted as themselves, each carrying its own id', () => {
  const root = mkdtempSync(join(tmpdir(), 'sessions-'));
  try {
    writeSession(root, 'session-aaa', [{ type: 'tool/call', data: { name: 'knowledge_search', arguments: '{"query":"alpha"}' } }]);
    writeSession(root, 'session-bbb', [{ type: 'tool/call', data: { name: 'knowledge_search', arguments: '{"query":"beta"}' } }]);
    const outA = join(root, 'a.json');
    const outB = join(root, 'b.json');
    assert.equal(run(['--root', root, '--session', 'session-aaa', '--out', outA]).status, 0);
    assert.equal(run(['--root', root, '--session', 'session-bbb', '--out', outB]).status, 0);
    const a = JSON.parse(readFileSync(outA, 'utf8'));
    const b = JSON.parse(readFileSync(outB, 'utf8'));
    assert.equal(a.session, 'session-aaa');
    assert.equal(b.session, 'session-bbb');
    assert.notEqual(JSON.stringify(a.calls), JSON.stringify(b.calls), 'they must not be the same content');
    assert.match(JSON.stringify(a.calls), /alpha/);
    assert.match(JSON.stringify(b.calls), /beta/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('an unknown session, a missing selector and an unknown flag all fail loudly', () => {
  const root = mkdtempSync(join(tmpdir(), 'sessions-'));
  try {
    writeSession(root, 'session-aaa', []);
    const unknown = run(['--root', root, '--session', 'session-nope', '--out', join(root, 'x.json')]);
    assert.equal(unknown.status, 2);
    assert.match(unknown.stderr, /unknown session/);

    const silent = run(['--root', root, '--out', join(root, 'y.json')]);
    assert.equal(silent.status, 2, 'it must not fall back to the newest session');
    assert.match(silent.stderr, /--session/);

    const flag = run(['--root', root, '--summry']);
    assert.equal(flag.status, 2);
    assert.match(flag.stderr, /unknown argument/);

    const latest = run(['--root', root, '--latest']);
    assert.equal(latest.status, 0, '--latest is the explicit way to mean the newest session');
    assert.match(latest.stdout, /session-aaa/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('logs are decoded by their encoding, and undetermined bytes are an error', () => {
  const root = mkdtempSync(join(tmpdir(), 'logs-'));
  try {
    const utf8 = join(root, 'run-utf8.log');
    writeFileSync(utf8, '状态：partial\n引用：1\n', 'utf8');
    const utf16 = join(root, 'run-utf16.log');
    writeFileSync(utf16, Buffer.from('状态：partial\n引用：1\n', 'utf16le'));

    const a = run(['--decode-log', utf8]);
    assert.equal(a.status, 0);
    assert.match(a.stdout, /状态：partial/);
    const b = run(['--decode-log', utf16]);
    assert.equal(b.status, 0);
    assert.match(b.stdout, /状态：partial/, 'a UTF-16 log must not come back as mojibake');

    const bogus = join(root, 'run-bogus.log');
    // Neither valid UTF-8, nor BOM-prefixed, nor NUL-bearing: the encoding is genuinely undetermined.
    writeFileSync(bogus, Buffer.from([0xc3, 0x28, 0xa0, 0xa1]));
    const c = run(['--decode-log', bogus]);
    assert.notEqual(c.status, 0, 'undetermined encoding must not parse silently');
    assert.match(c.stderr, /encoding is undetermined/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('a batch whose question ids do not map one-to-one onto sessions fails', () => {
  const root = mkdtempSync(join(tmpdir(), 'batch-'));
  try {
    writeFileSync(join(root, 'T1-tools.json'), JSON.stringify({ session: 'session-aaa', calls: [] }));
    writeFileSync(join(root, 'T2-tools.json'), JSON.stringify({ session: 'session-aaa', calls: [] }));
    const duplicate = run(['--verify-batch', root, '--ids', 'T1,T2']);
    assert.equal(duplicate.status, 2);
    assert.match(duplicate.stderr, /used by both/);

    writeFileSync(join(root, 'T2-tools.json'), JSON.stringify({ session: 'session-bbb', calls: [] }));
    const ok = run(['--verify-batch', root, '--ids', 'T1,T2']);
    assert.equal(ok.status, 0);
    assert.match(ok.stdout, /"oneToOne":true/);

    const missing = run(['--verify-batch', root, '--ids', 'T1,T2,T3']);
    assert.equal(missing.status, 2);
    assert.match(missing.stderr, /missing or unreadable result for T3/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
