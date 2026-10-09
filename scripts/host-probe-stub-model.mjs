/**
 * Stub #2 of three: the **DSH side** — the main conversation, and session-title generation.
 *
 * The three stubbed callers are separated by endpoint and by declared configuration, never by inspecting
 * what a request happens to look like:
 *
 *   1. DSH main conversation  -> THIS FILE, selected by the session's model id
 *   2. DSH session title      -> THIS FILE, selected by the `title-stub` model id that the probe profile
 *                                declares for `session-title-llm` (`provider`/`model` are real config fields)
 *   3. engine provider        -> `scripts/host-probe-stub-provider.mjs` (port 18782, its own `LLM_BASE_URL`)
 *
 * Deciding by `body.model` is a **configuration identifier**: the two classes are told apart because the
 * deployment says so, not because one of them omitted a tool catalog. A model id this stub does not
 * recognise is refused with a 400 that names it, and a conversation-model call that arrives without a tool
 * catalog is refused too - answering either one would make the later evidence meaningless.
 *
 * Reconciliation with the previous round: the old single stub guessed "engine call == no tools", which is
 * exactly the heuristic being replaced. `HOST_PROBE_DSH_SEARCH=1` still selects the search-first turn
 * script, which is a script choice, not a class choice.
 *
 * It never forwards anywhere: there is no outbound request in this file.
 *
 *   node scripts/host-probe-stub-model.mjs
 */
import { createServer } from 'node:http';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const probe = resolve(root, '.local/host-probe');
const logPath = resolve(probe, 'stub-dsh-log.json');

const port = Number(process.env.HOST_PROBE_DSH_PORT ?? 18784);
const libraryId = process.env.HOST_PROBE_LIBRARY ?? 'synthetic-probe';
/** The model the session runs as, and the one the probe profile declares for title generation. */
const conversationModel = process.env.HOST_PROBE_DSH_MODEL ?? 'deepseek-flash';
const titleModel = process.env.HOST_PROBE_TITLE_MODEL ?? 'title-stub';
const expectedKey = process.env.HOST_PROBE_STUB_KEY ?? '';
const searchFirst = process.env.HOST_PROBE_DSH_SEARCH === '1';
/**
 * The reference-sequence script: search, read through the short reference, then **deliberately** mix a
 * continuation reference with `startLine`, and correct once from the refusal's `suggestedRef`.
 *
 * This is the acceptance the real page has to show, and it is deliberately a *script*, not a model: a real
 * model would have to be trusted to make the same mistake on demand. The mistake is the one the recorded
 * session made for real (three calls asking for lines 200/335/105 through a `nextRef`), so the script
 * reproduces it in one turn so the conflict card can be seen - and then does exactly what the plugin asks:
 * one correction using the reference the refusal named, not a retry of the mix.
 */
const refSequence = process.env.HOST_PROBE_DSH_REF_SEQUENCE === '1';
/**
 * The continuation reference a read window returned, whether the conversation text holds it as plain JSON or
 * with the backslash escaping a nested tool result carries. A regex literal with an optional `\\` is legible;
 * a template-built pattern with quadruple escapes is not, and a probe that silently fails to find the value
 * would look like a plugin that never minted it.
 */
const nextRefOf = (text) => /nextRef\\?"\s*:\s*\\?"(kref-[A-Za-z0-9_-]+)/.exec(text)?.[1] ?? null;

/**
 * What has actually happened in this turn, read from **tool results only**.
 *
 * The first version of this script scanned the whole prompt for words like `"evidence"`,
 * `KNOWLEDGE_REF_KIND_CONFLICT` and `kref-`, and the plugin's own injected protocol contains all three: the
 * tool catalog lists `knowledge_read`, the protocol explains `KNOWLEDGE_REF_KIND_CONFLICT`, and its examples
 * spell `kref-...`. The stub therefore believed a search had already run, invented a reference out of the
 * protocol's example text, and looped - 25 model calls, six searches, and a failed turn, which looks like a
 * plugin defect and is not one. A tool result is identified by the fields only a result has.
 *
 * Only messages after the last user message are considered: an earlier turn's results are history, not state.
 */
function refSequenceState(body) {
  const messages = body?.messages ?? [];
  let start = 0;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index]?.role === 'user') { start = index + 1; break; }
  }
  const turn = messages.slice(start);
  const results = turn.filter(message => message?.role === 'tool' || message?.role === 'function')
    .map(message => typeof message?.content === 'string' ? message.content
      : JSON.stringify(message?.content ?? ''));
  const calls = turn.flatMap(message => (message?.role === 'assistant' ? message.tool_calls ?? [] : []))
    .map(call => call?.function?.name ?? call?.name ?? '')
    .filter(name => name.startsWith('knowledge_'));
  // A **read window**, not a search citation. Both carry `citationOrigin`: the search result's source card sets
  // it from the evidence's origin, so filtering on that counted the search as a read and the script skipped
  // straight to mixing a continuation reference it had never been given. `hasMore`/`fileChars` belong to a
  // window and to nothing else.
  const readResults = results.filter(result => result.includes('"hasMore"') || result.includes('"fileChars"'));
  const searchText = results.filter(result => result.includes('"excerptSource"')).join('\n');
  const conflictResult = results.find(result => result.includes('KNOWLEDGE_REF_KIND_CONFLICT')) ?? null;
  return {
    searches: calls.filter(name => name === 'knowledge_search').length,
    knowledgeCalls: calls.length,
    searchDone: searchText !== '',
    reads: readResults.length,
    conflict: conflictResult !== null,
    // The refusal names exactly one usable reference (the document reference for the same file), and it is the
    // last one mentioned in it: the message is what a real model would read, not a field name.
    suggestion: conflictResult ? (conflictResult.match(/kref-[A-Za-z0-9_-]+/g) ?? []).pop() ?? null : null,
    nextRef: nextRefOf(readResults.join('\n')),
    // "The window ended at a line limit but the file continues" is a *truncation*, not an end of file: the
    // first read here is exactly that (500 of 620 characters). Only the end-of-file notice means the last read
    // reached the end, which is what this step is for.
    eofSeen: results.some(result => result.includes('EXCERPT_INCOMPLETE_AT_EOF')),
    // The first reference the turn's *results* carried - never one the protocol merely mentions.
    firstRef: firstIdentifier(searchText || results.join('\n')),
  };
}
const altLibraryId = process.env.HOST_PROBE_ALT_LIBRARY ?? 'synthetic-alt';
/**
 * Delay before answering, so a turn stays in flight long enough for the browser probe to switch session or
 * library *during* it. Without this the in-flight window is a second or two and the scenario cannot be
 * driven reliably.
 */
const replyDelayMs = Number(process.env.HOST_PROBE_DSH_DELAY_MS ?? 0);
/**
 * Use the OLDEST read identifier in the conversation instead of the newest. That is how a stale identifier from
 * an earlier turn (or from before a page refresh) is re-presented, which must be refused rather than resolved.
 */
const reuseOldHandle = process.env.HOST_PROBE_REUSE_HANDLE === '1';
/**
 * The identifier a result carried, and the argument it belongs in.
 *
 * A current result carries a short `readRef`; the plugin strips the 300-character signed `fileHandle` and the
 * stub copies the reference, exactly as a real model would. `fileHandle` is still matched as a fallback so this
 * stub can also drive a build from before that change. Matching both in one scan means "the oldest identifier"
 * is the oldest of either spelling, not the oldest reference with a handle silently preferred.
 */
const IDENTIFIER = /(readRef|fileHandle)\\?"\s*:\s*\\?"([A-Za-z0-9._~-]+)/g;
function firstIdentifier(text) {
  IDENTIFIER.lastIndex = 0;
  const match = IDENTIFIER.exec(text);
  return match ? { key: match[1] === 'readRef' ? 'ref' : 'fileHandle', value: match[2] } : null;
}
/** The whole conversation as text, which is where a result's identifier appears. */
function conversationTextOf(body) {
  return (body?.messages ?? []).map(message => typeof message?.content === 'string'
    ? message.content
    : Array.isArray(message?.content) ? message.content.map(block => block?.text ?? '').join(' ') : '').join('\n');
}
/** First identifier anywhere in the conversation, when `reuseOldHandle` asks for a deliberately stale one. */
function oldestHandle(body) {
  return firstIdentifier(conversationTextOf(body));
}
/** The identifier to read with: the newest in this turn, or the deliberate oldest one. */
function identifierFor(body, text) {
  return reuseOldHandle ? oldestHandle(body) : firstIdentifier(text);
}
const sleep = ms => new Promise(done => setTimeout(done, ms));
/**
 * A deliberately stuck script, for the two limits that are supposed to stop one.
 *
 * `loop-search` re-issues the same search every step and never answers, so the plugin's failure governor
 * (identical failures) or its per-turn ceiling is what has to end the turn. `loop-read` does the same with
 * `knowledge_read`, which is how a stale handle is re-presented - as a *failing* call, so the failure chain
 * is what runs.
 */
const loopMode = process.env.HOST_PROBE_LOOP ?? '';
/**
 * The stub's own per-turn bound on knowledge calls, so a broken limit cannot run away.
 *
 * Set above the plugin's ceiling for the cap probe (the plugin must fire first), or above the failure
 * threshold for the breaker probe, and always finite.
 */
const maxKnowledgeCalls = Number(process.env.HOST_PROBE_MAX_CALLS ?? 3);

const counts = { conversation: 0, title: 0, rejected: 0 };
const log = [];
/** Session-unique call ids: the client pairs a result to a call by id and rejects a reused one. */
let callSeq = 0;
const runToken = `${process.pid.toString(36)}-${Date.now().toString(36)}`;

function persist() {
  mkdirSync(probe, { recursive: true });
  writeFileSync(logPath, Buffer.from(JSON.stringify({ counts, requests: log }, null, 2), 'utf8'));
}

function refuse(response, status, why, detail) {
  counts.rejected += 1;
  log.push({ at: new Date().toISOString(), class: 'rejected', why, detail });
  persist();
  response.writeHead(status, { 'content-type': 'application/json' });
  response.end(JSON.stringify({ error: { code: 'DSH_STUB_REFUSED', message: why, detail } }));
}

/** Serialize the current turn only: a later turn's request still carries earlier tool results. */
function conversationText(body) {
  const messages = body?.messages ?? [];
  let start = 0;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index]?.role === 'user') { start = index; break; }
  }
  const parts = [];
  for (const message of messages.slice(start)) {
    if (typeof message?.content === 'string') parts.push(message.content);
    else if (Array.isArray(message?.content)) {
      for (const block of message.content) parts.push(typeof block?.text === 'string' ? block.text : JSON.stringify(block));
    }
    for (const call of message?.tool_calls ?? []) parts.push(JSON.stringify(call));
  }
  return parts.join('\n');
}

function firstMatch(text, pattern) {
  const found = text.match(pattern);
  return found ? found[1] : null;
}

function sse(response, chunks) {
  response.writeHead(200, { 'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-cache', connection: 'keep-alive' });
  for (const chunk of chunks) response.write(`data: ${JSON.stringify(chunk)}\n\n`);
  response.write('data: [DONE]\n\n');
  response.end();
}

/**
 * The turn script: which knowledge tool to call next, from what the conversation already contains.
 *
 * `HOST_PROBE_DSH_SEARCH=1` starts from a real search through the engine; otherwise it starts from a
 * locate. Both then read the cited file and answer, so the session-switch probe can watch the *whole*
 * chain land in the right session and the right library.
 */
function nextTurnStep(body) {
  const text = conversationText(body);
  const whole = (body?.messages ?? []).map(message => typeof message?.content === 'string'
    ? message.content
    : Array.isArray(message?.content) ? message.content.map(block => block?.text ?? '').join(' ') : '').join('\n');
  const hasDocuments = text.includes('"documents"');
  const hasEvidence = text.includes('"evidence"');
  const hasRead = text.includes('"citationOrigin":"read"') || text.includes('\\"citationOrigin\\":\\"read"');
  // The library a turn targets is read from the WHOLE conversation, not just the current turn. A later
  // call in the same turn sees only the tool results after the last user message, and DSH injects user-role
  // reminders into that gap, so a current-turn-only test loses the marker mid-turn and silently switches
  // libraries.
  const wantsAlt = whole.includes('ALT-LIBRARY');
  const library = wantsAlt ? altLibraryId : libraryId;
  // A search that finds nothing still returns `"evidence": []`, and a refused call returns an error with no
  // payload at all, so "have I already tried?" cannot be read from any single key. Count the knowledge calls
  // this turn instead and stop: without a bound the stub re-issues the same call forever, which happened
  // twice (a 331-call search loop and a 175-call locator loop on a refused call).
  const knowledgeCalls = (text.match(/knowledge_(search|find_documents|read)/g) ?? []).length;
  // A stuck script for the limit probes. It ignores whether previous calls succeeded or failed, because the
  // point is that the plugin - not the stub - has to stop it.
  if (loopMode) {
    if (knowledgeCalls >= maxKnowledgeCalls) {
      return { step: 'loop-give-up', text: `STUB MODEL (not a real model): the stub's own bound of `
        + `${maxKnowledgeCalls} knowledge calls for this turn is reached. If the plugin's limits were working, `
        + 'this turn ended well before the stub got here.' };
    }
    if (loopMode === 'read') {
      const identifier = identifierFor(body, text);
      if (!identifier) {
        return { step: 'loop-seed-read', text: 'STUB MODEL (not a real model): no read identifier is in the '
          + 'conversation yet, so a read loop cannot start. Seed one first with a search-and-read turn.' };
      }
      return { step: 'loop-read', toolCall: { name: 'knowledge_read',
        arguments: { libraryId: library, [identifier.key]: identifier.value, startLine: 1, maxChars: 400, maxLines: 6 } } };
    }
    // `loop-search` repeats the *same* arguments, so the identical-failure chain applies if it fails.
    // `loop-search-varying` changes the query every time: the identical chain can never trip, so only the
    // per-turn ceiling can stop the turn. That is the case that isolates the ceiling from the governor.
    const query = loopMode === 'search-varying'
      ? `What temperature was the sample held at, and what were the conditions? variant ${callSeq + 1}`
      : 'What temperature was the sample held at, and what were the conditions?';
    return { step: 'loop-search', toolCall: { name: 'knowledge_search',
      arguments: { libraryId: library, query, mode: 'FAST' } } };
  }
  if (refSequence) {
    // The four steps the page acceptance has to show, in order: search, read through the short reference,
    // **mix** the continuation reference with a start line, then correct once with what the refusal named.
    // A sixth call reads the file to its end so the EOF notice is on the page in the same turn.
    const state = refSequenceState(body);
    if (state.knowledgeCalls >= 6) {
      return { step: 'ref-sequence-stop', text: 'STUB MODEL (not a real model): the reference sequence reached '
        + 'its own bound of six knowledge calls for this turn.' };
    }
    if (!state.searchDone) {
      return { step: 'ref-search', toolCall: { name: 'knowledge_search',
        arguments: { libraryId: library, query: 'What temperature was the sample held at, and what were the '
          + 'conditions?', mode: 'FAST' } } };
    }
    if (state.reads === 0) {
      return { step: 'ref-read', toolCall: { name: 'knowledge_read',
        arguments: { libraryId: library, ref: state.firstRef?.value, startLine: 1, maxChars: 500, maxLines: 6 } } };
    }
    if (!state.conflict) {
      // The deliberate mistake: a continuation reference and a start line are two different instructions about
      // where to begin, and the plugin must refuse the pair instead of silently preferring the cursor.
      return { step: 'ref-mix', toolCall: { name: 'knowledge_read',
        arguments: { libraryId: library, ref: state.nextRef, startLine: 200, maxChars: 500, maxLines: 6 } } };
    }
    if (state.reads === 1 && state.suggestion) {
      // One correction, with the reference the refusal offered - not a retry of the refused call.
      return { step: 'ref-correct', toolCall: { name: 'knowledge_read',
        arguments: { libraryId: library, ref: state.suggestion, startLine: 1, maxChars: 400, maxLines: 4 } } };
    }
    if (state.reads === 2 && !state.eofSeen && state.firstRef) {
      // Read the same file to its end, so the EOF notice renders on the page in this same turn.
      return { step: 'ref-eof', toolCall: { name: 'knowledge_read',
        arguments: { libraryId: library, ref: state.firstRef.value, maxChars: 20000, maxLines: 200 } } };
    }
    return { step: 'ref-answer', text: 'STUB MODEL (not a real model): this turn searched through the engine, '
      + `read through the search-issued reference (${state.reads} window(s)), sent a continuation reference with `
      + 'a start line on purpose, was refused once with KNOWLEDGE_REF_KIND_CONFLICT, and corrected with the '
      + 'reference that refusal named. Nothing here judges retrieval quality or the answer.' };
  }
  if (knowledgeCalls >= 3) {
    const refusal = /KNOWLEDGE_[A-Z_]+/.exec(text);
    return { step: 'give-up', text: 'STUB MODEL (not a real model): stopping after this turn\'s knowledge '
      + 'calls. '
      + (refusal ? `The last one was refused with ${refusal[0]}, which is the scope doing its job - this `
        + 'session is scoped elsewhere, so the call was refused before any request was sent.'
        : 'The searches returned no verifiable excerpt, so there is nothing to read. Reported as '
        + 'insufficient evidence rather than repeating the search.') };
  }
  if (wantsAlt) {
    // The alternate library's only document is a Markdown note, so the locator is the entry point; a search
    // through the engine would have nothing to match. Its card is deliberately unlike the first library's:
    // different title, different library, and no PDF behind it.
    if (!hasDocuments) {
      return { step: 'locate-alt', toolCall: { name: 'knowledge_find_documents',
        arguments: { libraryId: library, fileName: 'alt-note.md' } } };
    }
    if (!hasRead) {
      const identifier = identifierFor(body, text);
      if (!identifier) return { step: 'locate-alt-again', toolCall: { name: 'knowledge_find_documents',
        arguments: { libraryId: library, title: 'Alternate synthetic note' } } };
      return { step: 'read-alt', toolCall: { name: 'knowledge_read',
        arguments: { libraryId: library, [identifier.key]: identifier.value, startLine: 1, maxChars: 400, maxLines: 8 } } };
    }
    return { step: 'answer-alt', text: 'STUB MODEL (not a real model): this card belongs to the alternate '
      + 'library and to this session only. It has no PDF behind it, so it reports line numbers and no page.' };
  }
  if (searchFirst) {
    if (!hasEvidence) {
      return { step: 'search', toolCall: { name: 'knowledge_search',
        arguments: { libraryId: library, query: 'What temperature was the sample held at, and what were the conditions?', mode: 'FAST' } } };
    }
    if (!hasRead) {
      const identifier = identifierFor(body, text);
      if (!identifier) return { step: 'search-again', toolCall: { name: 'knowledge_search',
        arguments: { libraryId: library, query: 'sample was held at 298', mode: 'FAST' } } };
      return { step: 'read', toolCall: { name: 'knowledge_read',
        arguments: { libraryId: library, [identifier.key]: identifier.value, startLine: 1, maxChars: 500, maxLines: 6 } } };
    }
    return { step: 'answer', text: 'STUB MODEL (not a real model): the cards above come from a real search '
      + 'through the engine and a real read of the file it cited. Nothing here judges retrieval quality.' };
  }
  if (!hasDocuments) {
    return { step: 'locate', toolCall: { name: 'knowledge_find_documents',
      arguments: { libraryId: library, title: 'Thermal Stability of the Synthetic Sample' } } };
  }
  if (!hasRead) {
    const identifier = identifierFor(body, text);
    if (!identifier) return { step: 'locate-again', toolCall: { name: 'knowledge_find_documents',
      arguments: { libraryId: library, fileName: 'study.pdf' } } };
    return { step: 'read', toolCall: { name: 'knowledge_read',
      arguments: { libraryId: library, [identifier.key]: identifier.value, startLine: 19, maxChars: 400, maxLines: 4 } } };
  }
  return { step: 'answer', text: 'STUB MODEL (not a real model): the card above is rendered from the tool '
    + 'result this stub triggered. Location is the service\'s; text support is not assessed by any tool.' };
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url ?? '/', `http://127.0.0.1:${port}`);
  if (url.pathname === '/health') {
    response.writeHead(200, { 'content-type': 'application/json' });
    return response.end(JSON.stringify({ stub: 'dsh', port, conversationModel, titleModel, counts }));
  }
  // Two paths, one handler: DSH 0.1.7's own `llm-deepseek` adapter speaks the Anthropic Messages API
  // (`<baseURL>/v1/messages`, which this stub refuses on purpose - see the probe profile), while the
  // `openai-completions` route used for the probe posts to `<baseURL>/chat/completions`. Whether the
  // OpenAI-compatible client prefixes `/v1` is a detail of that client, so both spellings are accepted
  // rather than guessed at; anything else is still refused by name.
  const chatPaths = new Set(['/chat/completions', '/v1/chat/completions']);
  if (!chatPaths.has(url.pathname) || request.method !== 'POST') {
    return refuse(response, 404, 'this endpoint answers the DSH conversation and title calls only', url.pathname);
  }
  if (expectedKey && request.headers.authorization !== `Bearer ${expectedKey}`) {
    return refuse(response, 401, 'unexpected Authorization header', 'check HOST_PROBE_STUB_KEY');
  }
  const chunks = [];
  request.on('data', piece => chunks.push(piece));
  request.on('end', async () => {
    let body = null;
    try { body = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { /* refused below */ }
    if (!body || !Array.isArray(body.messages) || body.messages.length === 0) {
      return refuse(response, 400, 'expected a chat request with messages', 'malformed body');
    }
    const model = body.model ?? null;
    const toolNames = Array.isArray(body.tools) ? body.tools.map(tool => tool?.function?.name).filter(Boolean) : [];

    if (model === titleModel) {
      // Title generation, selected by the identifier the profile declares for `session-title-llm`.
      counts.title += 1;
      log.push({ at: new Date().toISOString(), class: 'title', model, stream: body.stream === true,
        tools: toolNames.length, toolNames, promptHead: String(body.messages[0]?.content ?? '').slice(0, 90) });
      persist();
      // A counter makes each probe session identifiable in the sidebar, so a screenshot can be tied to the
      // scenario it belongs to. The prompt is never echoed, so no conversation text leaks into a title.
      const content = `Synthetic knowledge session ${counts.title}`;
      if (body.stream !== true) {
        response.writeHead(200, { 'content-type': 'application/json' });
        return response.end(JSON.stringify({ id: 'stub', object: 'chat.completion', model,
          choices: [{ index: 0, message: { role: 'assistant', content }, finish_reason: 'stop' }],
          usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } }));
      }
      return sse(response, [
        { id: 'stub', object: 'chat.completion.chunk', model, choices: [{ index: 0, delta: { role: 'assistant', content }, finish_reason: null }] },
        { id: 'stub', object: 'chat.completion.chunk', model, choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] },
      ]);
    }

    if (model !== conversationModel) {
      return refuse(response, 400, 'unrecognised model id for this endpoint', String(model));
    }
    // A conversation call carries the tool catalog. Its absence means something else was pointed here.
    if (toolNames.length === 0) {
      return refuse(response, 400, 'a conversation call carries a tool catalog', 'none present');
    }

    const script = nextTurnStep(body);
    counts.conversation += 1;
    // A held reply keeps the turn in flight, which is the only state in which a session switch or a library
    // switch can be attempted mid-request. The delay is applied before the first byte, so the caller is
    // genuinely waiting rather than assembling an already-complete answer.
    if (replyDelayMs > 0) await sleep(replyDelayMs);
    const step = { at: new Date().toISOString(), class: 'conversation', model, stream: body.stream === true,
      tools: toolNames.length, toolNames, script: script.step,
      promptHead: String(body.messages[0]?.content ?? '').slice(0, 90) };
    if (script.toolCall) {
      step.toolCall = script.toolCall.name;
      callSeq += 1;
      const id = `stub-${runToken}-${callSeq}`;
      log.push(step);
      persist();
      return sse(response, [
        { id: 'stub', object: 'chat.completion.chunk', model, choices: [{ index: 0, delta: { role: 'assistant', tool_calls: [{ index: 0, id, type: 'function', function: { name: script.toolCall.name, arguments: '' } }] }, finish_reason: null }] },
        { id: 'stub', object: 'chat.completion.chunk', model, choices: [{ index: 0, delta: { tool_calls: [{ index: 0, function: { arguments: JSON.stringify(script.toolCall.arguments) } }] }, finish_reason: null }] },
        { id: 'stub', object: 'chat.completion.chunk', model, choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }] },
      ]);
    }
    step.answered = true;
    log.push(step);
    persist();
    sse(response, [
      { id: 'stub', object: 'chat.completion.chunk', model, choices: [{ index: 0, delta: { role: 'assistant', content: script.text }, finish_reason: null }] },
      { id: 'stub', object: 'chat.completion.chunk', model, choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] },
    ]);
  });
});

server.listen(port, '127.0.0.1', () => {
  console.log(JSON.stringify({ stub: 'dsh', port, conversationModel, titleModel, searchFirst, loopMode,
  maxKnowledgeCalls, reuseOldHandle, libraryId, altLibraryId,
    keyChecked: Boolean(expectedKey), log: logPath, forwardsToRealProvider: false }));
});
