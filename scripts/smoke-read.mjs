// Offline end-to-end check of knowledge_read: real DSH SDK tool definitions -> HTTP -> Python service.
// No model is configured and no query is submitted, so nothing here exercises retrieval quality: it
// proves the interface, the identifier path and the "no engine, no model" boundary only.
import { spawn, spawnSync, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { once } from 'node:events';
import { createServer } from 'node:net';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync, readdirSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import assert from 'node:assert/strict';
import { apply } from '../lib/index.js';
import { excerptFromOffsets } from './excerpt-offsets.mjs';

const PYTHON = resolve(process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
const TOKEN = 'synthetic-read-smoke-token-not-a-secret';
const VALUE_LINE = '数值 3.0';
const UNIT_LINE = '单位 M/atm';

// The production issuer mints the handle; only the service token can produce a valid one.
const MINT = `
import json, pathlib, sys
sys.path.insert(0, 'engine')
import reader
from evidence import digest
workspace, library, relative, target = sys.argv[1:5]
root = pathlib.Path(workspace) / 'library'
data = (root / relative).read_bytes()
secret = reader.derive_secret(sys.argv[5])
handle = reader.issue_handle(secret, workspace, library, relative, digest(data))
pathlib.Path(target).write_text(json.dumps({'fileHandle': handle, 'sourceVersion': digest(data)}), encoding='utf-8')
`;

const base = mkdtempSync(join(tmpdir(), 'read-smoke-'));
const workspace = join(base, 'workspace');
const library = join(workspace, 'library');
const sample = join(library, 'sample.txt');
mkdirSync(library, { recursive: true });
const body = [VALUE_LINE, UNIT_LINE, '区间 10-40', 'nm']
  .concat(Array.from({ length: 200 }, (_, index) => `第 ${index + 1} 行：这是一个用于读取冒烟测试的句子。`))
  .join('\n') + '\n';
writeFileSync(sample, body, 'utf8');
const config = join(base, 'config.json');
writeFileSync(config, JSON.stringify({ libraries: [{ id: 'fixture', name: 'fixture', workspace,
  root: library, syntheticFixture: true }] }), 'utf8');

const listener = createServer();
await new Promise(done => listener.listen(0, '127.0.0.1', done));
const port = listener.address().port;
await new Promise(done => listener.close(done));

const env = { ...process.env, DSH_KNOWLEDGE_TOKEN: TOKEN, DSH_KNOWLEDGE_CONFIG: config,
  DSH_KNOWLEDGE_ISOLATION: 'off', LLM_API_KEY: '', LLM_MODEL_NAME: '', LLM_BASE_URL: '' };
const serviceLog = [];
const child = spawn(PYTHON, ['-m', 'uvicorn', 'service:app', '--app-dir', 'engine', '--host', '127.0.0.1',
  '--port', String(port), '--no-access-log'], { env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
child.stdout.on('data', chunk => serviceLog.push(String(chunk)));
child.stderr.on('data', chunk => serviceLog.push(String(chunk)));
const exit = once(child, 'exit');
process.env.DSH_KNOWLEDGE_URL = `http://127.0.0.1:${port}`;
process.env.DSH_KNOWLEDGE_TOKEN = TOKEN;
try {
  let ready = false;
  for (let i = 0; i < 100; i++) {
    if (child.exitCode !== null) throw new Error('Service startup failed');
    try {
      const response = await fetch(process.env.DSH_KNOWLEDGE_URL + '/health',
        { headers: { Authorization: 'Bearer ' + TOKEN }, signal: AbortSignal.timeout(1000) });
      if (response.ok) { ready = true; break; }
    } catch {}
    await new Promise(done => setTimeout(done, 100));
  }
  assert.ok(ready, 'Service readiness');

  const minted = join(base, 'handle.json');
  const mintedRun = spawnSync(PYTHON, ['-c', MINT, workspace, 'fixture', 'sample.txt', minted, TOKEN],
    { cwd: resolve('.'), env: { ...process.env, PYTHONIOENCODING: 'utf-8' }, encoding: 'utf8' });
  assert.equal(mintedRun.status, 0, mintedRun.stderr);
  const { fileHandle, sourceVersion } = JSON.parse(readFileSync(minted, 'utf8'));

  const tools = [];
  apply({ tools: { register: tool => tools.push(tool) } });
  assert.equal(tools.length, 5);
  const byName = new Map(tools.map(tool => [tool.name, tool]));
  assert.equal(tools[2].name, 'knowledge_find_documents', 'the locator is registered before the enumeration');
  assert.ok(byName.has('knowledge_list_documents'), 'the enumerator is registered');
  const read = byName.get('knowledge_read');
  const context = { agent: { session: { header: { cwd: workspace, id: 'read-smoke-session' } } },
                    signal: new AbortController().signal };

  const window = await read.execute({ libraryId: 'fixture', fileHandle, sourceVersion }, context);
  const text = readFileSync(sample, 'utf8');
  assert.ok(window.excerpt.includes(VALUE_LINE), 'the read returns the cited line');
  assert.ok(window.excerpt.includes(UNIT_LINE), 'and the line that carries its unit');
  assert.equal(excerptFromOffsets(text, window), window.excerpt, 'the window rebuilds from its offsets');
  assert.equal(window.sourceVersion, sourceVersion);
  assert.equal(window.limits.readsLimit, 40, 'the service reports its hard session limit');
  assert.equal(window.limits.sessionCharsLimit, 400000);
  // The result hands the model a short reference and no long token: the signed handle is stripped, and the
  // reference resolves back to it, so the service still verifies exactly what it verified before.
  assert.equal(window.fileHandle, undefined, 'the long spelling must not reach the model');
  assert.ok(/^kref-/.test(window.readRef), 'the result carries a short reference instead');
  assert.equal(window.budget.remaining, 5, 'a result reports what is left of the turn');
  assert.ok(!JSON.stringify(window).includes(fileHandle), 'no 300-character token survives in the result');

  const byRef = await read.execute({ libraryId: 'fixture', ref: window.readRef, sourceVersion }, context);
  assert.equal(excerptFromOffsets(text, byRef), byRef.excerpt, 'a reference reads the same bytes');
  assert.equal(byRef.readRef, window.readRef, 'and the same reference comes back, because it is the same file');

  assert.ok(window.nextRef, 'the sample is long enough to need a second read');
  // A continuation reference and a start line are two contradictory instructions about where to read, and the
  // plugin now refuses the pair **before** any request: the recorded session spent three calls discovering that
  // its `startLine` was being ignored, each answered with a READ_CURSOR_PRECEDENCE warning. This replaces the
  // old assertion (which pinned that precedence) rather than adding a call, because this smoke runs on a
  // synthetic session with no turn boundary, so every call here shares one 6-call window and the `remaining`
  // assertion below depends on it. The "continuation reference sent alone continues the file" case is pinned in
  // `tests/references.test.mjs`, where the ceiling is not what is being measured.
  const cacheCount = () => (existsSync(join(base, 'query-cache')) ? readdirSync(join(base, 'query-cache')).length : 0);
  const readsBefore = cacheCount();
  await assert.rejects(read.execute({ libraryId: 'fixture', ref: window.nextRef, startLine: 1 }, context),
    error => error.code === 'KNOWLEDGE_REF_KIND_CONFLICT'
      && error.record.detail.reason === 'CONTINUATION_REF_WITH_START_LINE'
      && error.record.detail.suggestedAction === 'USE_DOCUMENT_REF'
      && error.record.detail.suggestedRef === window.readRef
      && error.message.includes(window.readRef)
      && !JSON.stringify(error.record).includes('krc1.'));
  assert.equal(cacheCount(), readsBefore, 'the refusal created nothing');

  // A bad *handle* and a bad *reference* are refused with different codes, because they are different facts:
  // one is "this identifier is not one I issued" and the other is "this pointer is not in my table". Neither
  // is "the document was deleted".
  await assert.rejects(read.execute({ libraryId: 'fixture', fileHandle: 'krh1.not.a.handle' }, context),
    error => /KNOWLEDGE_HANDLE_INVALID/.test(error.message)
      && error.code === 'KNOWLEDGE_HANDLE_INVALID'
      && !error.message.includes(sample));
  await assert.rejects(read.execute({ libraryId: 'fixture', ref: 'kref-not-invented1' }, context),
    error => error.code === 'KNOWLEDGE_READ_REF_UNKNOWN'
      && !error.message.includes(sample));

  // No engine worker and no job directory: a read never goes near the query pipeline.
  const cache = join(base, 'query-cache');
  assert.ok(!existsSync(cache) || readdirSync(cache).length === 0, 'a read creates no job directory');
  assert.ok(!serviceLog.join('').includes('JOB_BUDGET'), 'no job ran during a read');
  assert.ok(!serviceLog.join('').includes('ISOLATION_JOB'), 'no container job ran during a read');
  assert.equal(env.LLM_API_KEY + env.LLM_MODEL_NAME + env.LLM_BASE_URL, '', 'no provider was configured');
  await assert.rejects(tools[2].execute({ libraryId: 'fixture', title: 'anything at all' }, context),
    error => error.code === 'KNOWLEDGE_SERVICE_UNAVAILABLE'
      && error.record?.detail?.serviceCode === 'CATALOG_UNAVAILABLE'
      && error.record?.detail?.httpStatus === 503,
    'without a configured catalogue the locator refuses instead of guessing, and keeps the service code as a typed field');
  console.log('PASS: DSH-normalized knowledge_read and knowledge_find_documents -> HTTP -> service: window, unit completion, reference-kind conflict refusal, other refusal codes, and a catalogue-less locator refusal; no model, no engine worker, no job directory. Read quality is not tested.');
} finally {
  if (child.exitCode === null) {
    if (process.platform === 'win32') await promisify(execFile)('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true });
    else child.kill('SIGTERM');
  }
  await exit;
  rmSync(base, { recursive: true, force: true });
}
