/**
 * Acceptance probe: the **search → short reference → read** chain, driven through the plugin's own tool
 * entry, against a real isolated service whose model replies come from the local engine stub.
 *
 * What is real, and what is not - stated because the distinction is the whole point:
 *
 *   real      the plugin tool definitions (`lib/index.js`, the same code the host mounts), the HTTP client,
 *             the reference store and its rewriting, the uvicorn service, the catalogue re-check, the
 *             Sirchmunk engine, the AppContainer (`isolation=required`, the default), the parent-process
 *             verification of engine candidates, the source cards and the model-visible JSON;
 *   stub      only the provider's replies (`scripts/host-probe-stub-provider.mjs`, no outbound request).
 *
 * No candidate is injected: the query runs whatever the engine finds and the parent verifies. The probe
 * therefore proves the *chain*, not retrieval quality - a stub that answers with a plausible sentence says
 * nothing about whether a real model would have found the right passage.
 *
 * Boundary note (why this file exists next to `probe-search-chain.mjs`): that probe already goes through the
 * plugin entry - it imports `definitions` from `lib/index.js` - but it only asserted that a `readRef` came
 * back. It never asserted the *kind* field, never recorded that the long token was really on the wire before
 * the plugin removed it, never rebuilt an excerpt from its offsets, and never covered the empty/malformed
 * cases. A probe that cannot separate "the service returned a handle" from "the model saw a reference" is a
 * service probe wearing a plugin probe's name, so this file records both halves of that boundary.
 *
 *   node scripts/probe-ref-chain.mjs [--query "..."] [--out <json>] [--mode FAST|DEEP]
 *
 * Ports are allocated free and never touch 3080 or the daily backend. Everything it writes lives under
 * `.local/` (gitignored): the run directory holds the service and stub logs, and the report path is printed.
 */
import { spawn, spawnSync } from 'node:child_process';
import { createServer } from 'node:net';
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { KnowledgeClient } from '../lib/client.js';
import { definitions } from '../lib/index.js';
import { ReferenceStore } from '../lib/refs.js';
import { TurnLedger } from '../lib/turnlimit.js';
import { excerptFromOffsets } from './excerpt-offsets.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const venvPython = join(root, '.venv/Scripts/python.exe');
const probe = join(root, '.local/host-probe');
const tokenPath = join(probe, 'service-token.txt');
const configPath = join(probe, 'libraries.json');
const cataloguePath = join(probe, 'catalogue.json');
const stubProvider = join(root, 'scripts/host-probe-stub-provider.mjs');
const LIBRARY = 'synthetic-probe';
const SESSION = 'ref-chain-probe';
/** A service-signed handle or cursor. A reference (`kref-…`) is short by construction, so this cannot match one. */
const TOKEN_SHAPE = /kr[hc]\d\.[A-Za-z0-9_-]{40,}\.[A-Za-z0-9_-]{40,}/g;

const sleep = (ms) => new Promise(done => setTimeout(done, ms));

function parseArgs(argv) {
  const options = {
    query: 'What temperature was the sample held at, and what were the conditions?',
    out: join(root, '.local/verification/ref-chain.json'), mode: 'FAST',
  };
  for (let index = 0; index < argv.length; index += 1) {
    const [flag, inline] = argv[index].split('=');
    const next = () => inline ?? argv[++index];
    switch (flag) {
      case '--query': options.query = next(); break;
      case '--out': options.out = resolve(next()); break;
      case '--mode': options.mode = next(); break;
      default: throw new Error(`unknown argument: ${argv[index]}`);
    }
  }
  return options;
}

function freePort() {
  return new Promise((done, fail) => {
    const probeServer = createServer();
    probeServer.on('error', fail);
    probeServer.listen(0, '127.0.0.1', () => {
      const { port } = probeServer.address();
      probeServer.close(() => done(port));
    });
  });
}

async function fetchJson(url, init = {}, timeoutMs = 5000) {
  try {
    const response = await fetch(url, { ...init, signal: AbortSignal.timeout(timeoutMs) });
    const text = await response.text();
    let body = null;
    try { body = text ? JSON.parse(text) : null; } catch { body = { raw: text.slice(0, 200) }; }
    return { status: response.status, body };
  } catch (error) {
    return { status: 0, body: null, error: error.name };
  }
}

/** Collect assertion results instead of throwing at the first failure: a report that stops early hides the rest. */
function makeChecks() {
  const results = [];
  const record = (id, description, ok, evidence) => {
    results.push({ id, description, ok: Boolean(ok), evidence });
    console.log(`${ok ? 'PASS' : 'FAIL'}  [${id}] ${description}`);
    if (!ok) console.log(`      ${JSON.stringify(evidence)?.slice(0, 500)}`);
  };
  return { results, record };
}

// ---------------------------------------------------------------- the service under test

async function startProviderStub(port, runDir) {
  const child = spawn(process.execPath, [stubProvider], {
    cwd: root,
    env: { ...process.env, HOST_PROBE_PROVIDER_PORT: String(port),
      HOST_PROBE_STUB_KEY: 'local-stub-placeholder-not-a-secret' },
    stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true,
  });
  let log = '';
  child.stdout.on('data', chunk => { log += chunk; });
  child.stderr.on('data', chunk => { log += chunk; });
  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (child.exitCode !== null) throw new Error(`engine stub exited ${child.exitCode}: ${log.slice(0, 300)}`);
    const health = await fetchJson(`http://127.0.0.1:${port}/health`, {}, 1000);
    if (health.status === 200) { writeFileSync(join(runDir, 'stub-provider.log'), log, 'utf8'); return child; }
    await sleep(100);
  }
  throw new Error(`engine stub never became healthy: ${log.slice(0, 300)}`);
}

async function startService(port, providerPort, runDir) {
  const environment = { ...process.env };
  // The service must default to isolation. A probe that silently ran a plain worker would be measuring a
  // different system, so this is asserted rather than assumed: the variable is *removed*, and `/health` has to
  // report `isolationActive: true` before any assertion runs.
  delete environment.DSH_KNOWLEDGE_ISOLATION;
  delete environment.DSH_KNOWLEDGE_RUNTIME;
  Object.assign(environment, {
    DSH_KNOWLEDGE_TOKEN: TOKEN,
    DSH_KNOWLEDGE_CONFIG: configPath,
    DSH_KNOWLEDGE_CATALOG: cataloguePath,
    // No `/v1` suffix: the stub serves `/chat/completions` at its root. With `/v1` the parent proxy posted to
    // `/v1/chat/completions`, the stub answered 404 before it logged anything, and the engine came back with
    // zero candidates and a `NotFoundError` in the diagnostics - a probe failure that looks exactly like an
    // engine that found nothing.
    LLM_BASE_URL: `http://127.0.0.1:${providerPort}`,
    LLM_API_KEY: 'local-stub-placeholder-not-a-secret',
    LLM_MODEL_NAME: 'stub-model',
  });
  const child = spawn(venvPython, ['-m', 'uvicorn', 'service:app', '--app-dir', 'engine', '--host', '127.0.0.1',
    '--port', String(port), '--no-access-log'],
  { cwd: root, env: environment, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
  let log = '';
  child.stdout.on('data', chunk => { log += chunk; });
  child.stderr.on('data', chunk => { log += chunk; });
  const auth = { authorization: `Bearer ${TOKEN}` };
  for (let attempt = 0; attempt < 300; attempt += 1) {
    if (child.exitCode !== null) {
      writeFileSync(join(runDir, 'service.log'), log, 'utf8');
      throw new Error(`service exited ${child.exitCode}: ${log.slice(-600)}`);
    }
    const health = await fetchJson(`http://127.0.0.1:${port}/health`, { headers: auth }, 1500);
    if (health.status === 200) {
      writeFileSync(join(runDir, 'service.log'), log, 'utf8');
      return { child, health: health.body, logPath: join(runDir, 'service.log') };
    }
    await sleep(100);
  }
  writeFileSync(join(runDir, 'service.log'), log, 'utf8');
  throw new Error('service never became healthy');
}

function killTree(child) {
  if (!child || child.exitCode !== null) return;
  try { spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' }); }
  catch { /* best effort */ }
}

// ---------------------------------------------------------------- the plugin's own entry

/**
 * The tool map the host would mount, plus a record of every client call and every raw service payload.
 *
 * `calls` is what makes "the refusal sent nothing" checkable, and `wire` is what makes the strip checkable:
 * `wire.search` is the payload the service returned (long handle and all), the tool result is what the model
 * would receive.
 */
function mount({ client = null, max = 24 } = {}) {
  const calls = [];
  const wire = { search: [], read: [], libraries: [], documents: [], catalogue: [] };
  const base = client ?? new KnowledgeClient(URL, TOKEN);
  const spy = {
    libraries: async (...args) => {
      calls.push({ method: 'libraries' });
      const result = await base.libraries(...args);
      wire.libraries.push(result);
      return result;
    },
    search: async (...args) => {
      calls.push({ method: 'search' });
      const result = await base.search(...args);
      wire.search.push(result);
      return result;
    },
    read: async (...args) => {
      calls.push({ method: 'read' });
      const result = await base.read(...args);
      wire.read.push(result);
      return result;
    },
    findDocuments: async (...args) => {
      calls.push({ method: 'findDocuments' });
      const result = await base.findDocuments(...args);
      wire.documents.push(result);
      return result;
    },
    catalogue: async (...args) => {
      calls.push({ method: 'catalogue' });
      const result = await base.catalogue(...args);
      wire.catalogue.push(result);
      return result;
    },
  };
  const refs = new ReferenceStore();
  const map = new Map(definitions(spy, undefined, undefined, new TurnLedger({ max }), refs)
    .map(tool => [tool.name, tool]));
  return { map, calls, wire, refs, client: spy };
}

const exec = (id = SESSION) => ({ agent: { session: { header: { id, cwd: root } } } });

function longestToken(value) {
  const found = JSON.stringify(value ?? null).match(TOKEN_SHAPE);
  return found ? Math.max(...found.map(token => token.length)) : 0;
}

/**
 * Mint a service-issued handle for a synthetic file, the way `smoke-read.mjs` does: through the reader.
 *
 * These handles belong to files the probe itself wrote under `.local/host-probe/`, and they exist so the EOF
 * cases can be driven on texts whose endings are known. They are the service's own identifier, minted with the
 * service's own secret - not a fabricated token.
 */
function mintHandle(relative) {
  const script = [
    'import sys, pathlib, json',
    'sys.path.insert(0, "engine")',
    'import reader, evidence',
    'workspace, library, relative, token, absolute = sys.argv[1:6]',
    'data = pathlib.Path(absolute).read_bytes()',
    'print(json.dumps({"fileHandle": reader.issue_handle(reader.derive_secret(token), workspace, library,',
    '                  relative, evidence.digest(data)), "sourceVersion": evidence.digest(data)}))',
  ].join('\n');
  const absolute = join(probe, 'library/texts', relative);
  const result = spawnSync(venvPython, ['-c', script, root.replace(/\\/g, '\\'), LIBRARY, relative, TOKEN, absolute],
    { cwd: root, encoding: 'utf8', windowsHide: true, timeout: 60000 });
  if (result.status !== 0) throw new Error(`python helper failed: ${(result.stderr || '').slice(0, 300)}`);
  return JSON.parse(result.stdout.trim().split('\n').pop());
}

async function attempt(promise) {
  return promise.then(value => ({ value }), error => ({ error }));
}

// ---------------------------------------------------------------- the assertions

let TOKEN = '';
let URL = '';

async function main() {
  const options = parseArgs(process.argv.slice(2));
  TOKEN = readFileSync(tokenPath, 'utf8').trim();
  const runDir = join(root, '.local/verification', `ref-chain-${new Date().toISOString().replace(/[:.]/g, '-')}`);
  mkdirSync(runDir, { recursive: true });
  const { results, record } = makeChecks();

  for (const [what, path] of [['token', tokenPath], ['library config', configPath], ['catalogue', cataloguePath],
    ['venv python', venvPython], ['engine stub', stubProvider]]) {
    if (!existsSync(path)) throw new Error(`${what} is missing: ${path}`);
  }
  const baseline = { cache: readdirSync(join(probe, 'query-cache')).length };

  const providerPort = await freePort();
  const servicePort = await freePort();
  URL = `http://127.0.0.1:${servicePort}`;
  const report = {
    startedAt: new Date().toISOString(), query: options.query, mode: options.mode, runDir,
    ports: { provider: providerPort, service: servicePort }, baseline, assertions: results,
    realParts: ['plugin tool definitions', 'HTTP client', 'reference store', 'uvicorn service', 'catalogue',
      'Sirchmunk engine', 'AppContainer (isolation=required)', 'parent verification', 'source cards'],
    stubbedParts: ['provider replies (scripts/host-probe-stub-provider.mjs, no outbound request)'],
    notes: [],
  };
  console.log(`engine stub http://127.0.0.1:${providerPort}   service http://127.0.0.1:${servicePort}`);

  let provider = null;
  let service = null;
  try {
    provider = await startProviderStub(providerPort, runDir);
    const started = await startService(servicePort, providerPort, runDir);
    service = started.child;
    report.health = started.health;
    record('health', 'the service runs with required isolation active',
      started.health?.isolationActive === true && started.health?.isolation === 'required',
      { isolation: started.health?.isolation, isolationActive: started.health?.isolationActive });

    const { map, calls, wire } = mount();
    const context = exec();

    // --- 1. the search returns a verifiable candidate, through the plugin entry
    const search = await map.get('knowledge_search').execute(
      { libraryId: LIBRARY, query: options.query, mode: options.mode }, context);
    const evidence = search.evidence ?? [];
    report.search = {
      status: search.status, evidence: evidence.length, warnings: search.warnings ?? [],
      origins: [...new Set(evidence.map(item => item.excerptSource ?? null))],
      excerptChars: evidence.map(item => (item.excerpt ?? '').length),
      answerChars: typeof search.answer === 'string' ? search.answer.length : 0,
      relativePaths: evidence.map(item => item.relativePath ?? null),
    };
    record('search', 'the search returns at least one engine-sourced, verifiable candidate',
      evidence.length > 0 && evidence.every(item => typeof item.excerpt === 'string' && item.excerpt.length > 0),
      report.search);
    // The raw service payload, kept as evidence: it is the half of the boundary that carried the long handle,
    // and without it "the plugin stripped the token" is an assertion nobody can re-check.
    writeFileSync(join(runDir, 'wire-search.json'), Buffer.from(JSON.stringify(wire.search[0] ?? null, null, 2), 'utf8'));

    // --- 2. every readable item carries a document reference with its kind
    const readable = evidence.filter(item => item.readRef);
    record('reference', 'every readable evidence item carries readRef with readRefKind=document',
      evidence.length > 0 && readable.length === evidence.length
        && evidence.every(item => item.readRefKind === 'document'),
      { readable: readable.length, ofEvidence: evidence.length,
        kinds: evidence.map(item => item.readRefKind ?? null), refs: evidence.map(item => item.readRef ?? null) });
    record('guide', 'the result explains the two reference kinds to the model',
      typeof search.referenceGuide === 'string' && /document reference/.test(search.referenceGuide)
        && /must not be combined with `startLine`/.test(search.referenceGuide),
      { guide: typeof search.referenceGuide === 'string' ? search.referenceGuide.slice(0, 200) : null });

    // --- 3. the boundary: the service sent long tokens, the model-visible result has none
    const rawWire = JSON.stringify(wire.search[0] ?? {});
    const modelVisible = JSON.stringify({ ...search, budget: undefined });
    const cards = map.get('knowledge_search').output?.presentationMeta?.({}, search) ?? null;
    record('strip', 'the service payload carried a long signed handle and no reference',
      /krh1\./.test(rawWire) && !/kref-/.test(rawWire),
      { wireTokenChars: longestToken(wire.search[0]), wireHasRef: /kref-/.test(rawWire) });
    record('strip', 'the model-visible result has references and no long token',
      longestToken(modelVisible) === 0 && /kref-/.test(modelVisible),
      { resultTokenChars: longestToken(modelVisible), cardTokenChars: longestToken(cards) });
    report.boundary = { wireTokenChars: longestToken(wire.search[0]), resultTokenChars: longestToken(modelVisible),
      cardTokenChars: longestToken(cards) };

    // --- 4/5/6. read through the search-issued reference; the citation, the card and the window must agree
    const cited = readable[0] ?? null;
    if (!cited) {
      record('read', 'a read through the search-issued readRef succeeds', false, { note: 'no readable evidence' });
    } else {
      const read = await map.get('knowledge_read').execute(
        { libraryId: LIBRARY, ref: cited.readRef, sourceVersion: cited.sourceVersion, startLine: cited.startLine ?? 1,
          maxChars: 2000, maxLines: 40 }, context);
      record('read', 'a read through the search-issued readRef succeeds on the same file',
        typeof read.excerpt === 'string' && read.excerpt.length > 0 && read.relativePath === cited.relativePath,
        { relativePath: read.relativePath, citationPath: cited.relativePath, excerptChars: (read.excerpt ?? '').length });
      const sourceText = readFileSync(join(probe, 'library/texts', cited.relativePath), 'utf8');
      const rebuilt = excerptFromOffsets(sourceText, read);
      record('offsets', 'the excerpt rebuilds verbatim from its own offsets', rebuilt === read.excerpt,
        { rebuiltChars: rebuilt.length, excerptChars: (read.excerpt ?? '').length,
          rebuiltHead: rebuilt.slice(0, 60), excerptHead: (read.excerpt ?? '').slice(0, 60) });
      const searchCard = (cards?.sources ?? []).find(source => source.relativePath === cited.relativePath) ?? null;
      record('version', 'citation, card and read agree on the file and its sourceVersion',
        cited.sourceVersion === read.sourceVersion && (searchCard?.sourceVersion ?? cited.sourceVersion) === read.sourceVersion
          && cited.relativePath === read.relativePath,
        { citation: cited.sourceVersion, card: searchCard?.sourceVersion ?? null, read: read.sourceVersion,
          citationPath: cited.relativePath, readPath: read.relativePath });
      report.read = { relativePath: read.relativePath, sourceVersion: read.sourceVersion, startLine: read.startLine,
        endLine: read.endLine, excerptChars: (read.excerpt ?? '').length, locationVerified: read.locationVerified,
        sourceCard: read.sourceCard ? { truncated: read.sourceCard.truncated, notices: read.sourceCard.truncationNotices,
          identity: read.sourceCard.status?.identity, locationVerified: read.sourceCard.status?.locationVerified,
          support: read.sourceCard.status?.support } : null };
    }

    // --- 7. empty and malformed payloads mint nothing (synthetic payloads, deliberately not engine retrieval)
    const emptySearch = async () => ({ status: 'partial', answer: '', warnings: [], usage: null, evidence: [] });
    const emptyTools = mount({ client: { search: emptySearch } });
    const empty = await emptyTools.map.get('knowledge_search').execute({ libraryId: LIBRARY, query: 'q' }, context);
    const malformedSearch = async () => ({ status: 'partial', answer: '', warnings: [], usage: null, evidence: [
      { id: 'e1', libraryId: LIBRARY, relativePath: 'x.md', sourceVersion: 'v1', startLine: 1, endLine: 1,
        excerpt: 'q', fileHandle: 'not-a-handle' },
      { id: 'e2', libraryId: LIBRARY, relativePath: 'y.md', sourceVersion: 'v1', startLine: 1, endLine: 1, excerpt: 'q' },
    ] });
    const malformedTools = mount({ client: { search: malformedSearch } });
    const malformed = await malformedTools.map.get('knowledge_search').execute({ libraryId: LIBRARY, query: 'q' }, context);
    record('empty', 'an empty result mints no reference and claims no guide',
      (empty.evidence ?? []).length === 0 && empty.referenceGuide === undefined,
      { evidence: (empty.evidence ?? []).length, guide: empty.referenceGuide ?? null });
    record('malformed', 'a malformed or absent handle mints no readable reference',
      malformed.evidence.every(item => item.readRef === undefined)
        && malformed.evidence[0].fileHandle === 'not-a-handle',
      { refs: malformed.evidence.map(item => item.readRef ?? null),
        handles: malformed.evidence.map(item => item.fileHandle ?? null) });

    // --- reference kinds, live: document jump, continuation paging, the refused pair, the recovery entry
    const window = await map.get('knowledge_read').execute(
      { libraryId: LIBRARY, ref: cited?.readRef, sourceVersion: cited?.sourceVersion, maxChars: 600, maxLines: 6 },
      context);
    record('kinds', 'a read window declares its own document reference and a continuation reference',
      window.readRefKind === 'document' && window.nextRefKind === 'continuation' && /^kref-/.test(window.nextRef ?? ''),
      { readRef: window.readRef ?? null, readRefKind: window.readRefKind ?? null, nextRefKind: window.nextRefKind ?? null,
        nextRef: window.nextRef ?? null });
    const readsBefore = calls.filter(call => call.method === 'read').length;
    const conflict = await attempt(map.get('knowledge_read').execute(
      { libraryId: LIBRARY, ref: window.nextRef, startLine: 200 }, context));
    record('conflict', 'a continuation reference combined with startLine is refused locally',
      conflict.error?.code === 'KNOWLEDGE_REF_KIND_CONFLICT'
        && conflict.error?.record?.detail?.reason === 'CONTINUATION_REF_WITH_START_LINE'
        && conflict.error?.record?.detail?.suggestedAction === 'USE_DOCUMENT_REF',
      { code: conflict.error?.code ?? null, detail: conflict.error?.record?.detail ?? null,
        messageHead: (conflict.error?.message ?? '').slice(0, 160) });
    const readsAfter = calls.filter(call => call.method === 'read').length;
    record('norequest', 'the refusal sent nothing to the service', readsAfter === readsBefore,
      { readsBefore, readsAfter });
    const suggested = conflict.error?.record?.detail?.suggestedRef ?? null;
    record('suggested', 'the refusal offers the same file\'s document reference',
      typeof suggested === 'string' && suggested === window.readRef,
      { suggested, windowReadRef: window.readRef ?? null });
    if (suggested) {
      const jumped = await map.get('knowledge_read').execute(
        { libraryId: LIBRARY, ref: suggested, sourceVersion: window.sourceVersion, startLine: 1, maxChars: 400,
          maxLines: 4 }, context);
      record('recover', 'the suggested reference reads the same file and version at the requested line',
        jumped.startLine === 1 && jumped.relativePath === window.relativePath
          && jumped.sourceVersion === window.sourceVersion,
        { startLine: jumped.startLine, relativePath: jumped.relativePath, sourceVersion: jumped.sourceVersion,
          windowPath: window.relativePath, windowVersion: window.sourceVersion });
    } else {
      record('recover', 'the suggested reference reads the same file and version at the requested line', false,
        { note: 'the refusal offered no reference' });
    }
    if (window.nextRef) {
      const continued = await attempt(map.get('knowledge_read').execute({ libraryId: LIBRARY, ref: window.nextRef }, context));
      record('continue', 'the continuation reference alone continues from where the window stopped',
        continued.value?.startChar === window.nextStartChar && typeof continued.value?.excerpt === 'string',
        { nextStartChar: window.nextStartChar, startedAt: continued.value?.startChar ?? null,
          error: continued.error?.code ?? null });
    } else {
      record('continue', 'the continuation reference alone continues from where the window stopped', false,
        { note: 'the window returned no continuation reference' });
    }

    const byLine = await map.get('knowledge_read').execute(
      { libraryId: LIBRARY, ref: cited?.readRef, sourceVersion: cited?.sourceVersion, startLine: 3, maxChars: 400,
        maxLines: 3 }, context);
    record('docjump', 'a document reference with startLine reads the requested line', byLine.startLine === 3,
      { startLine: byLine.startLine, endLine: byLine.endLine });

    // --- eviction: a dead pairing must not produce a dead suggestion
    let evictionCounter = 0;
    const evictionRefs = new ReferenceStore(2, () => `kref-${String(++evictionCounter).padStart(8, '0')}`);
    const evictionTools = new Map(definitions(new KnowledgeClient(URL, TOKEN), undefined, undefined,
      new TurnLedger({ max: 24 }), evictionRefs).map(tool => [tool.name, tool]));
    const minted = cited ? mintHandle(cited.relativePath) : null;
    const seeded = minted ? await attempt(evictionTools.get('knowledge_read').execute(
      { libraryId: LIBRARY, fileHandle: minted.fileHandle, maxChars: 600, maxLines: 6 }, context)) : { error: { code: 'NO_MINT' } };
    if (seeded.value?.nextRef) {
      evictionRefs.issue({ kind: 'handle', token: `krh1.${'Z'.repeat(60)}.${'Z'.repeat(43)}`, libraryId: LIBRARY,
        workspace: root, session: SESSION, sourceVersion: 'v1' });
      const evicted = await attempt(evictionTools.get('knowledge_read').execute(
        { libraryId: LIBRARY, ref: seeded.value.nextRef, startLine: 5 }, context));
      record('evicted', 'an evicted pairing returns DROP_START_LINE and names no dead reference',
        evicted.error?.code === 'KNOWLEDGE_REF_KIND_CONFLICT'
          && evicted.error?.record?.detail?.suggestedAction === 'DROP_START_LINE'
          && evicted.error?.record?.detail?.suggestedRef === null,
        { code: evicted.error?.code ?? null, detail: evicted.error?.record?.detail ?? null });
    } else {
      record('evicted', 'an evicted pairing returns DROP_START_LINE and names no dead reference', false,
        { note: 'no continuation reference from the freshly minted handle', error: seeded.error?.code ?? null });
    }

    // --- EOF: the three ways a window can end, on the fixture's own files
    const truncating = await map.get('knowledge_read').execute(
      { libraryId: LIBRARY, ref: cited?.readRef, sourceVersion: cited?.sourceVersion, startLine: 1, maxLines: 2,
        maxChars: 300 }, context);
    record('eof-more', 'a window with content after it is truncated and may be continued',
      truncating.hasMore === true && (truncating.warnings ?? []).some(w => w.startsWith('EXCERPT_TRUNCATED')),
      { hasMore: truncating.hasMore, warnings: truncating.warnings ?? [] });
    const atEnd = await map.get('knowledge_read').execute(
      { libraryId: LIBRARY, fileHandle: mintHandle('study.txt').fileHandle }, context);
    record('eof-no-boundary', 'the end of a file without an ASCII sentence stop says there is no next window',
      atEnd.hasMore === false && !atEnd.nextRef
        && (atEnd.warnings ?? []).some(w => w.startsWith('EXCERPT_INCOMPLETE_AT_EOF'))
        && !(atEnd.warnings ?? []).some(w => w.startsWith('EXCERPT_TRUNCATED')),
      { hasMore: atEnd.hasMore, nextRef: atEnd.nextRef ?? null, warnings: atEnd.warnings ?? [],
        card: atEnd.sourceCard ? { truncated: atEnd.sourceCard.truncated,
          notices: atEnd.sourceCard.truncationNotices } : null });
    const closed = await map.get('knowledge_read').execute(
      { libraryId: LIBRARY, fileHandle: mintHandle('notes.md').fileHandle }, context);
    record('eof-closed', 'the end of a file that closes a sentence carries no truncation notice',
      closed.hasMore === false && closed.excerptComplete === true
        && !(closed.warnings ?? []).some(w => w.startsWith('EXCERPT_TRUNCATED') || w.startsWith('EXCERPT_INCOMPLETE_AT_EOF')),
      { hasMore: closed.hasMore, excerptComplete: closed.excerptComplete, warnings: closed.warnings ?? [] });

    // --- the model-request counter is a host-process counter, and unknown values stay unknown
    const model = search.budget?.model ?? null;
    record('budget', 'the model-request counter carries scope=host-process and keeps unknown values unknown',
      model === null || (model.scope === 'host-process'
        && Object.prototype.hasOwnProperty.call(model, 'usageUnknown')
        && (model.requestChars === null || typeof model.requestChars === 'number')),
      { model: model === null ? null : { scope: model.scope ?? null, countedAs: model.countedAs ?? null,
        attempted: model.attempted ?? null, completed: model.completed ?? null,
        requestChars: model.requestChars ?? null, usageUnknown: model.usageUnknown ?? null },
      turn: search.budget ? { used: search.budget.used ?? null, max: search.budget.max ?? null,
        remaining: search.budget.remaining ?? null } : null });
    report.budget = search.budget ?? null;
  } finally {
    killTree(service);
    killTree(provider);
    await sleep(1500);
  }

  // --- 8. nothing left behind
  const cleanup = { ports: {}, cacheEntries: readdirSync(join(probe, 'query-cache')).length };
  for (const [name, port] of Object.entries(report.ports)) {
    const answer = await fetchJson(`http://127.0.0.1:${port}/health`, {}, 800);
    cleanup.ports[name] = answer.status === 0 ? 'free' : `answered(${answer.status})`;
  }
  record('cleanup', 'the probe left no listening port behind',
    Object.values(cleanup.ports).every(state => state === 'free'), cleanup.ports);
  record('cleanup', 'the probe left no job directory behind', cleanup.cacheEntries <= baseline.cache,
    { baseline: baseline.cache, after: cleanup.cacheEntries });
  report.cleanup = cleanup;

  report.finishedAt = new Date().toISOString();
  report.failures = results.filter(result => !result.ok).map(result => `${result.id}: ${result.description}`);
  report.passed = results.filter(result => result.ok).length;
  report.total = results.length;
  mkdirSync(dirname(options.out), { recursive: true });
  writeFileSync(options.out, Buffer.from(JSON.stringify(report, null, 2), 'utf8'));
  console.log(`\n${report.passed}/${report.total} assertions passed; report ${options.out}`);
  if (report.failures.length) { console.log(`failed: ${report.failures.join(' | ')}`); process.exitCode = 1; }
  else process.exitCode = 0;
}

await main();
