/**
 * Stub #1 of three: the **Sirchmunk engine's provider**, and nothing else.
 *
 * The three stubbed callers are separated by endpoint and by declared configuration, not by inspecting
 * what a request happens to look like:
 *
 *   1. DSH main conversation  -> `scripts/host-probe-stub-model.mjs`   (port 18784, model id = session model)
 *   2. DSH session title      -> the same stub, selected by the `title-stub` **model id declared in the
 *                                profile patch** for `session-title-llm`
 *   3. engine provider        -> THIS FILE                                 (port 18782, `LLM_BASE_URL`)
 *
 * So a request's class is decided by *which endpoint the deployment points at it* or by a configured
 * identifier, never by "does it have tools?". This listener answers the engine only: anything else gets an
 * explicit 400 naming what it was, because silently answering a misrouted call would make a later
 * observation meaningless.
 *
 * It never forwards anywhere. There is no outbound request in this file at all, so "no real provider call"
 * is a property of the code rather than a promise.
 *
 * What the engine asks for was read out of `sirchmunk` rather than guessed:
 *
 *   * FAST step 1 wants a JSON routing analysis; `primary` is tried first by the ripgrep pass and must
 *     appear verbatim in the document, and without it the run ends after that one call with no candidates;
 *   * step 4 wants the `<SUMMARY>`/`<PRECISE_ANSWER>`/`<SHOULD_ANSWER>` tagged form, and
 *     `SHOULD_ANSWER=true` is what makes the engine accept its own evidence rather than fall back to a
 *     parent-side window.
 *
 *   node scripts/host-probe-stub-provider.mjs
 */
import { createServer } from 'node:http';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const probe = resolve(root, '.local/host-probe');
const logPath = resolve(probe, 'stub-provider-log.json');

const port = Number(process.env.HOST_PROBE_PROVIDER_PORT ?? 18782);
const expectedKey = process.env.HOST_PROBE_STUB_KEY ?? '';
const counts = { routing: 0, answer: 0, other: 0, rejected: 0 };
const log = [];

/** One engine call, classified by the prompt the engine actually sent. */
function classify(prompt) {
  if (prompt.includes('extract search terms at two granularity levels')) return 'routing';
  if (prompt.includes('SHOULD_ANSWER')) return 'answer';
  return 'other';
}

const ROUTING = {
  type: 'search',
  primary: ['sample was held'],
  fallback: ['vessel', 'temperature'],
  idf: { 'sample was held': 9.0, vessel: 4.0, temperature: 3.0 },
  primary_alt: [],
  fallback_alt: [],
  file_hints: [],
  intent: 'find the recorded temperature and conditions',
};

const ANSWER = [
  '<SUMMARY>',
  'The synthetic note records that the sample was held at 298 K in a closed vessel for 1 h.',
  '</SUMMARY>',
  '<PRECISE_ANSWER>',
  '298 K in a closed vessel.',
  '</PRECISE_ANSWER>',
  '<SHOULD_ANSWER>true</SHOULD_ANSWER>',
  '<SHOULD_SAVE>true</SHOULD_SAVE>',
].join('\n');

function refuse(response, status, why, detail) {
  counts.rejected += 1;
  const entry = { at: new Date().toISOString(), class: 'rejected', why, detail };
  log.push(entry);
  persist();
  response.writeHead(status, { 'content-type': 'application/json' });
  response.end(JSON.stringify({ error: { code: 'PROVIDER_STUB_REFUSED', message: why, detail } }));
}

function persist() {
  mkdirSync(probe, { recursive: true });
  writeFileSync(logPath, Buffer.from(JSON.stringify({ counts, requests: log }, null, 2), 'utf8'));
}

const server = createServer((request, response) => {
  const url = new URL(request.url ?? '/', `http://127.0.0.1:${port}`);
  if (url.pathname === '/health') {
    response.writeHead(200, { 'content-type': 'application/json' });
    return response.end(JSON.stringify({ stub: 'engine-provider', port, counts }));
  }
  if (url.pathname !== '/chat/completions' || request.method !== 'POST') {
    return refuse(response, 404, 'this endpoint answers the engine provider only', url.pathname);
  }
  if (expectedKey && request.headers.authorization !== `Bearer ${expectedKey}`) {
    return refuse(response, 401, 'unexpected Authorization header', 'check HOST_PROBE_STUB_KEY');
  }
  const chunks = [];
  request.on('data', piece => chunks.push(piece));
  request.on('end', () => {
    let body = null;
    try { body = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { /* refused below */ }
    if (!body || !Array.isArray(body.messages) || body.messages.length === 0) {
      return refuse(response, 400, 'expected a chat request with messages', 'malformed body');
    }
    // The engine has no tool catalog. A request that carries one is not the engine, and answering it with
    // an engine reply is exactly the mixing this separation exists to prevent.
    if (Array.isArray(body.tools) && body.tools.length > 0) {
      return refuse(response, 400, 'the engine provider is not sent a tool catalog',
        `${body.tools.length} tool(s); this looks like a DSH conversation call pointed at the wrong endpoint`);
    }
    const prompt = body.messages.map(message => typeof message.content === 'string' ? message.content : '').join('\n');
    const kind = classify(prompt);
    counts[kind] += 1;
    log.push({ at: new Date().toISOString(), class: kind, model: body.model ?? null,
      stream: body.stream === true, promptChars: prompt.length, promptHead: prompt.slice(0, 120) });
    persist();
    const content = kind === 'routing' ? JSON.stringify(ROUTING) : kind === 'answer' ? ANSWER : JSON.stringify(ROUTING);
    // `stream: false` wants one JSON object; answering it with SSE is what made the engine's client raise.
    if (body.stream === true) {
      response.writeHead(200, { 'content-type': 'text/event-stream; charset=utf-8' });
      response.write(`data: ${JSON.stringify({ id: 'stub', object: 'chat.completion.chunk', model: body.model, choices: [{ index: 0, delta: { role: 'assistant', content }, finish_reason: null }] })}\n\n`);
      response.write(`data: ${JSON.stringify({ id: 'stub', object: 'chat.completion.chunk', model: body.model, choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] })}\n\n`);
      response.write('data: [DONE]\n\n');
      return response.end();
    }
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(JSON.stringify({
      id: 'stub', object: 'chat.completion', model: body.model,
      choices: [{ index: 0, message: { role: 'assistant', content }, finish_reason: 'stop' }],
      usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
    }));
  });
});

server.listen(port, '127.0.0.1', () => {
  console.log(JSON.stringify({ stub: 'engine-provider', port, keyChecked: Boolean(expectedKey),
    log: logPath, forwardsToRealProvider: false }));
});
