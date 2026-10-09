/**
 * The failure governor: the plugin's own answer to "can a model just keep calling a tool that keeps failing?"
 *
 * The host cannot answer that question for us. `@deepseek-ai/dsh-agent-loop` exposes only
 * `maxParallelToolCalls` — no per-turn call ceiling and no consecutive-failure ceiling — and
 * `@deepseek-ai/dsh-repeat-tool-reminder` is documented as advisory, "without vetoing or rewriting calls".
 * A provider quota and a tool-call count are different budgets, so an exhausted provider does not stop the
 * loop. These tests pin what the plugin itself can do: refuse to do work for a repeating call, and end the
 * turn through `ToolRunContext.concludeTurn()`.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { KnowledgeServiceError, describeDetail } from '../lib/failures.js';
import { FailureGovernor, MAX_CONSECUTIVE_FAILURES, MAX_IDENTICAL_FAILURES } from '../lib/governor.js';
import { definitions } from '../lib/index.js';
import { TurnLedger } from '../lib/turnlimit.js';

const exec = (sessionId = 's1', extra = {}) => ({
  agent: { session: { header: { id: sessionId, cwd: process.cwd() } } }, ...extra,
});

test('the same failing call trips the identical chain at its threshold', () => {
  const governor = new FailureGovernor();
  for (let attempt = 1; attempt < MAX_IDENTICAL_FAILURES; attempt += 1) {
    assert.equal(governor.noteFailure('s1', 'knowledge_read', { a: 1 }), undefined,
      `attempt ${attempt} must still propagate the original error`);
  }
  const stop = governor.noteFailure('s1', 'knowledge_read', { a: 1 });
  assert.equal(stop.code, 'KNOWLEDGE_REPEAT_REFUSED');
  assert.equal(stop.chain, 'identical');
  assert.equal(stop.occurrences, MAX_IDENTICAL_FAILURES);
  assert.match(stop.message, /same arguments/);
});

test('arguments are compared canonically, so key order is not a way around the guard', () => {
  const governor = new FailureGovernor();
  let stop;
  for (let attempt = 0; attempt < MAX_IDENTICAL_FAILURES; attempt += 1) {
    stop = governor.noteFailure('s1', 'knowledge_find_documents', attempt % 2 === 0 ? { a: 1, b: 2 } : { b: 2, a: 1 });
  }
  assert.equal(stop.chain, 'identical', 'a reordered argument object is the same call');
});

test('different arguments do not trip the identical chain', () => {
  const governor = new FailureGovernor();
  for (let attempt = 0; attempt < MAX_IDENTICAL_FAILURES + 2; attempt += 1) {
    const stop = governor.noteFailure('s1', 'knowledge_search', { query: `q${attempt}` });
    if (stop) assert.notEqual(stop.chain, 'identical');
  }
});

test('changing arguments forever still trips the consecutive chain', () => {
  const governor = new FailureGovernor();
  let stop;
  for (let attempt = 0; attempt < MAX_CONSECUTIVE_FAILURES; attempt += 1) {
    stop = governor.noteFailure('s1', 'knowledge_search', { query: `q${attempt}` });
  }
  assert.equal(stop.code, 'KNOWLEDGE_REPEAT_REFUSED');
  assert.equal(stop.chain, 'consecutive');
  assert.match(stop.message, /failed \d+ times in a row/);
});

test('one success clears both chains, so ordinary work never trips the guard', () => {
  const governor = new FailureGovernor();
  for (let attempt = 0; attempt < MAX_CONSECUTIVE_FAILURES - 1; attempt += 1) {
    governor.noteFailure('s1', 'knowledge_search', { query: `q${attempt}` });
  }
  governor.noteSuccess('s1');
  assert.equal(governor.size, 0);
  for (let attempt = 0; attempt < MAX_CONSECUTIVE_FAILURES - 1; attempt += 1) {
    assert.equal(governor.noteFailure('s1', 'knowledge_search', { query: `r${attempt}` }), undefined);
  }
});

test('sessions are governed independently', () => {
  const governor = new FailureGovernor();
  for (let attempt = 0; attempt < MAX_CONSECUTIVE_FAILURES - 1; attempt += 1) {
    governor.noteFailure('s1', 'knowledge_search', { query: `q${attempt}` });
  }
  assert.equal(governor.noteFailure('s2', 'knowledge_search', { query: 'q' }), undefined,
    'another session must start from zero');
});

test('the governor is bounded and evicts the oldest session', () => {
  const governor = new FailureGovernor(2);
  governor.noteFailure('a', 'knowledge_read', {});
  governor.noteFailure('b', 'knowledge_read', {});
  governor.noteFailure('c', 'knowledge_read', {});
  assert.equal(governor.size, 2);
});

// ------------------------------------------------------------------ through the tools themselves

/**
 * A client whose calls always fail, so the wrapper's failure path is what runs.
 *
 * It throws the same `KnowledgeServiceError` the real client throws, because classification reads the status
 * and the service code rather than the message: a plain `Error` is deliberately unclassifiable.
 */
const failingClient = () => ({
  libraries: async () => { throw new KnowledgeServiceError(401, null, ''); },
  search: async () => { throw new KnowledgeServiceError(503, null, ''); },
  findDocuments: async () => { throw new KnowledgeServiceError(503, null, ''); },
  read: async () => { throw new KnowledgeServiceError(503, null, ''); },
});

/**
 * A ledger large enough that a test about the *failure* chains never trips the *turn* ceiling.
 *
 * The two limits are independent by design, which also means a test of one must not accidentally exercise
 * the other; `tests/turnlimit.test.mjs` and the independence test at the end of this file cover the ceiling.
 */
const roomy = () => new TurnLedger({ max: 100 });

test('a repeated failing tool call is answered locally and ends the turn', async () => {
  const governor = new FailureGovernor();
  const tools = new Map(definitions(failingClient, undefined, governor, roomy()).map(tool => [tool.name, tool]));
  const contexts = [];
  const context = exec('s1', { concludeTurn: () => contexts.push('concluded') });

  for (let attempt = 1; attempt < MAX_IDENTICAL_FAILURES; attempt += 1) {
    await assert.rejects(tools.get('knowledge_search').execute({ libraryId: 'l', query: 'same' }, context),
      // Below the threshold the failure reaches the model - as a classified code, never as upstream prose.
      /KNOWLEDGE_SERVICE_UNAVAILABLE/, 'below the threshold the real failure must reach the model');
  }
  assert.deepEqual(contexts, [], 'no turn is concluded before the threshold');

  const stopped = await tools.get('knowledge_search').execute({ libraryId: 'l', query: 'same' }, context);
  assert.equal(stopped.stopped, 'KNOWLEDGE_REPEAT_REFUSED');
  assert.equal(stopped.failure.code, 'KNOWLEDGE_REPEAT_REFUSED');
  assert.equal(stopped.failure.terminal, true);
  assert.deepEqual(contexts, ['concluded'], 'the host primitive that actually ends a loop is called');
  assert.match(stopped.warnings[0], /重复失败已熔断|连续失败后被插件停止/);
});

test('the stop payload is not presented as a source card', async () => {
  const governor = new FailureGovernor();
  const tools = new Map(definitions(failingClient, undefined, governor, roomy()).map(tool => [tool.name, tool]));
  const context = exec('s1', { concludeTurn: () => {} });
  let stopped;
  for (let attempt = 0; attempt < MAX_IDENTICAL_FAILURES; attempt += 1) {
    stopped = await tools.get('knowledge_read').execute({ libraryId: 'l', fileHandle: 'krh1.x' }, context)
      .catch(error => ({ threw: String(error.message) }));
  }
  assert.equal(stopped.stopped, 'KNOWLEDGE_REPEAT_REFUSED');
  const meta = tools.get('knowledge_read').output.presentationMeta({}, stopped);
  assert.deepEqual(meta.sources, [], 'a stopped call carries no cards, and must not look like one');
  // The card is driven by the failure record, not by an empty source list: an empty list means "no results",
  // which is a different statement from "the plugin stopped this call".
  assert.equal(meta.failure.code, 'KNOWLEDGE_REPEAT_REFUSED');
  assert.equal(meta.failure.terminal, true);
  assert.ok(!JSON.stringify(meta.failure).includes('krh1.'), 'no handle may ride along in the record');
});

test('a host without concludeTurn still gets the stop payload', async () => {
  const governor = new FailureGovernor();
  const tools = new Map(definitions(failingClient, undefined, governor, roomy()).map(tool => [tool.name, tool]));
  const context = exec('s1');
  let stopped;
  for (let attempt = 0; attempt < MAX_IDENTICAL_FAILURES; attempt += 1) {
    stopped = await tools.get('knowledge_search').execute({ libraryId: 'l', query: 'same' }, context)
      .catch(() => undefined);
  }
  assert.equal(stopped.stopped, 'KNOWLEDGE_REPEAT_REFUSED');
});

test('a successful call resets the chain through the tool wrapper', async () => {
  let mode = 'fail';
  const flipping = () => ({
    libraries: async () => [],
    search: async () => { if (mode === 'fail') throw new KnowledgeServiceError(503, null, ''); return { status: 'partial', evidence: [] }; },
    findDocuments: async () => { throw new KnowledgeServiceError(503, null, ''); },
    read: async () => { throw new KnowledgeServiceError(503, null, ''); },
  });
  const governor = new FailureGovernor();
  const tools = new Map(definitions(flipping, undefined, governor, roomy()).map(tool => [tool.name, tool]));
  const context = exec('s1', { concludeTurn: () => {} });
  for (let attempt = 0; attempt < MAX_CONSECUTIVE_FAILURES - 1; attempt += 1) {
    await tools.get('knowledge_search').execute({ libraryId: 'l', query: `q${attempt}` }, context).catch(() => undefined);
  }
  mode = 'ok';
  await tools.get('knowledge_search').execute({ libraryId: 'l', query: 'works' }, context);
  assert.equal(governor.size, 0, 'a success clears the session chain');
});

// ------------------------------------------------------------------ the two limits are independent

test('the turn ceiling is not the failure chain: successes alone reach it', async () => {
  // The property that makes a second limit necessary. The governor is cleared by every success, so this turn
  // never accumulates a failure - and must still be bounded.
  let calls = 0;
  const working = () => ({
    libraries: async () => [],
    search: async () => { calls += 1; return { status: 'partial', evidence: [] }; },
    findDocuments: async () => ({}),
    read: async () => ({}),
  });
  const governor = new FailureGovernor();
  const ledger = new TurnLedger({ max: 3 });
  const tools = new Map(definitions(working, undefined, governor, ledger).map(tool => [tool.name, tool]));
  const contexts = [];
  const context = exec('s1', { concludeTurn: () => contexts.push('concluded') });

  for (let attempt = 0; attempt < 3; attempt += 1) {
    await tools.get('knowledge_search').execute({ libraryId: 'l', query: `q${attempt}` }, context);
  }
  const refused = await tools.get('knowledge_search').execute({ libraryId: 'l', query: 'fourth' }, context);
  assert.equal(refused.stopped, 'KNOWLEDGE_TURN_LIMIT_REACHED');
  assert.equal(refused.failure.code, 'KNOWLEDGE_TURN_LIMIT_REACHED');
  assert.equal(calls, 3, 'a refused call must not reach the client at all - no HTTP, no container');
  assert.deepEqual(contexts, ['concluded'], 'the turn is ended through the host primitive');
  // The governor never saw a failure, so its state is untouched: the two limits are separate books.
  assert.equal(governor.size, 0);
});

test('a refused call does not consume the failure chain either', async () => {
  const governor = new FailureGovernor();
  const ledger = new TurnLedger({ max: 1 });
  const tools = new Map(definitions(failingClient, undefined, governor, ledger).map(tool => [tool.name, tool]));
  const context = exec('s1', { concludeTurn: () => {} });
  // First call fails and counts once against the consecutive chain.
  await tools.get('knowledge_search').execute({ libraryId: 'l', query: 'a' }, context).catch(() => undefined);
  assert.equal(governor.size, 1);
  // Second call is refused by the ceiling before it can fail, so the chain stays where it was.
  const refused = await tools.get('knowledge_search').execute({ libraryId: 'l', query: 'b' }, context);
  assert.equal(refused.stopped, 'KNOWLEDGE_TURN_LIMIT_REACHED');
  assert.equal(governor.size, 1, 'a call that never ran cannot be a failure the chain counts');
});

test('the ceiling counts every knowledge tool, and the record says which one was stopped', async () => {
  const working = () => ({
    libraries: async () => [],
    search: async () => ({}),
    findDocuments: async () => ({}),
    read: async () => ({}),
  });
  const ledger = new TurnLedger({ max: 4 });
  const tools = new Map(definitions(working, undefined, new FailureGovernor(), ledger).map(tool => [tool.name, tool]));
  const context = exec('s1', { concludeTurn: () => {} });
  await tools.get('knowledge_list_libraries').execute({}, context);
  await tools.get('knowledge_search').execute({ libraryId: 'l', query: 'q' }, context);
  await tools.get('knowledge_find_documents').execute({ libraryId: 'l', title: 't' }, context);
  await tools.get('knowledge_read').execute({ libraryId: 'l', fileHandle: 'krh1.x' }, context);
  const fifth = await tools.get('knowledge_list_libraries').execute({}, context);
  assert.equal(fifth.stopped, 'KNOWLEDGE_TURN_LIMIT_REACHED');
  assert.equal(fifth.tool, 'knowledge_list_libraries');
  // Typed, not textual: the tool is a member of a closed set and the counts are numbers. This is the fifth
  // host attempt and the first refused one, so `used` reports 5 against a ceiling of 4.
  assert.deepEqual(fifth.failure.detail, {
    layer: 'plugin', httpStatus: null, serviceCode: null, tool: 'knowledge_list_libraries', parameter: null,
    chain: null, occurrences: null, used: 5, max: 4, evidence: 0, documents: 0, reads: 0,
    reason: null, requestedStartLine: null, fileLines: null, suggestedAction: null, suggestedRef: null,
    query: null,
  });
  // The rendered context line is built from those fields by whichever half displays it.
  assert.match(describeDetail(fifth.failure.detail), /本轮已用 5 \/ 上限 4/);
});
