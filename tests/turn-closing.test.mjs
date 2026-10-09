/**
 * P0: a turn that spends its knowledge budget still ends with an answer.
 *
 * The failure this file is about was observed in a real session, not hypothesised. Three turns in a row ended
 * with `turn/end {reason: {kind: "completed"}}`, six or seven dispatched calls, material returned — and **no
 * assistant message containing any text at all**. The cause was this plugin: it ended the turn with the host's
 * `concludeTurn()` (correct — that is what stops a refusal loop) and the host's agent loop returns
 * `{kind: 'completed'}` at that step, so the model never got another step in which to write prose.
 *
 * What the plugin can do about it, and what these tests pin:
 *
 * - `exec.deferContext(message)` attaches an `additionalContexts` entry to the concluding **successful** result.
 *   The agent loop splices those into `next-step` and, because `inbox.nextStep.length !== 0`, does *not* close
 *   the turn at that step. The model therefore gets exactly one more step, with the plugin's instruction in
 *   front of it. See `@deepseek-ai/dsh-agent-loop` `turn()` and `@deepseek-ai/dsh-tools`
 *   `dispatchScheduledExecution`.
 * - That step is claimed **once per turn**, atomically, by the same ledger that counts calls. A model that
 *   spends it on another tool call gets a refusal with no instruction, and the turn then closes.
 * - The instruction is built from the plugin's own constants and counts. It is a request to answer, never a
 *   research conclusion, so it cannot put words into the answer.
 *
 * These tests drive the plugin's own control flow with a synthetic client. They prove the plugin asks for the
 * closing step and asks once; they do **not** prove a real model obeys. That is what the host probe with a
 * stand-in provider in `scripts/accept` is for, and it is reported separately.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { MAX_TURN_KNOWLEDGE_CALLS, TurnLedger } from '../lib/turnlimit.js';
import { FailureGovernor } from '../lib/governor.js';
import { describeDetail } from '../lib/failures.js';
import { definitions } from '../lib/index.js';
import { releasedV4SessionFormatCodec, assertV4RowAdmission } from '@deepseek-ai/dsh-session-format-v3-to-v4';
import { createUserMessage } from '@deepseek-ai/dsh-llm';

/** A session log stand-in with the two members the ledger reads, exactly as `tests/turnlimit.test.mjs` uses. */
function fakeSession() {
  const events = [];
  return {
    events,
    get seq() { return events.length; },
    eventAt(index) { return events[index]; },
    startTurn() { const at = events.length; events.push({ type: 'turn/start', seq: at }); return at; },
    step() { const at = events.length; events.push({ type: 'step/start', seq: at }); return at; },
  };
}

/** An execution context that records the two host primitives this file is about. */
function recordingExec(session, id = 's1') {
  const record = { concluded: 0, deferred: [], context: null };
  return {
    record,
    exec: {
      agent: { session: { header: { id, cwd: process.cwd() }, ...session } },
      concludeTurn() { record.concluded += 1; },
      deferContext(context) {
        // Use the real persistence boundary: fake hooks previously accepted retired source kinds.
        const event = { type: 'user/message', seq: 0, time: Date.now(), data: context };
        const row = releasedV4SessionFormatCodec.encodeEvent(event);
        assertV4RowAdmission(row);
        assert.equal(context.source.kind, 'knowledge-closing');
        record.deferred.push(context);
      },
    },
  };
}

/**
 * A synthetic service. Every call is answered from `reply`, and every dispatch is counted — which is how
 * "the seventh call never reached the service" is asserted as an external fact rather than a log line.
 */
function fakeService(reply = () => ({ status: 'partial', answer: '', evidence: [], warnings: [] })) {
  const calls = [];
  const record = (name) => (...args) => {
    calls.push({ name, args });
    const result = reply(name, args);
    if (name === 'search') args[6]?.settled?.({ phase: 'completed', jobState: 'completed', cancellationConfirmed: false });
    return Promise.resolve(result);
  };
  return {
    calls,
    client: {
      libraries: record('libraries'),
      search: record('search'),
      findDocuments: record('findDocuments'),
      catalogue: record('catalogue'),
      read: record('read'),
    },
  };
}

const evidenceItem = (id) => ({
  id, libraryId: 'lib', relativePath: `texts/${id}.txt`, sourceVersion: 'v1',
  startLine: 1, endLine: 2, excerpt: `quote ${id}`,
  sourceCard: {
    libraryId: 'lib', title: id, fileName: `${id}.txt`, excerpt: `quote ${id}`, location: null,
    sourceVersion: 'v1', citationOrigin: 'engine', truncated: false, truncationNotices: [],
    parseQuality: null, status: { identity: 'not-assessed', locationVerified: false, support: 'not-assessed' },
  },
});

/** Every knowledge tool, by name, over one synthetic client. */
function harness(client, ledger, governor = new FailureGovernor()) {
  const tools = new Map(definitions(client, undefined, governor, ledger).map(tool => [tool.name, tool]));
  return tools;
}

test('usable evidence plus an exhausted budget still produces one bounded closing step', async () => {
  const session = fakeSession();
  session.startTurn();
  const ledger = new TurnLedger({ max: MAX_TURN_KNOWLEDGE_CALLS });
  const service = fakeService(() => ({
    status: 'partial', answer: '', warnings: [],
    // Four excerpts per search, so the closing summary has a number it could not have guessed.
    evidence: [evidenceItem('a'), evidenceItem('b'), evidenceItem('c'), evidenceItem('d')],
  }));
  const tools = harness(service.client, ledger);
  const { exec, record } = recordingExec(session);

  const results = [];
  for (let index = 0; index < MAX_TURN_KNOWLEDGE_CALLS; index += 1) {
    session.step();
    results.push(await tools.get('knowledge_search').execute({ libraryId: 'lib', query: `q${index}` }, exec));
  }
  // The call that tips the budget, in the same turn.
  session.step();
  const refused = await tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'overflow' }, exec);

  // 1. No service work happened for the refused call. This is the external fact, not the message.
  assert.equal(service.calls.filter(call => call.name === 'search').length, MAX_TURN_KNOWLEDGE_CALLS,
    'the refused call must not reach the service');
  assert.equal(refused.status, 'failed');
  assert.equal(refused.stopped, 'KNOWLEDGE_TURN_LIMIT_REACHED');

  // 2. The turn is ended through the host primitive, so no further dispatch can continue this turn's work.
  assert.equal(record.concluded, 1, 'a budget stop must conclude the turn');

  // 3. ...and exactly one bounded closing step is requested, carrying the real accounting.
  assert.equal(record.deferred.length, 1, 'exactly one closing instruction per turn');
  const text = record.deferred[0].content.map(block => block.text).join('\n');
  assert.match(text, /KNOWLEDGE_TURN_LIMIT_REACHED/);
  assert.match(text, new RegExp(`\\b${MAX_TURN_KNOWLEDGE_CALLS + 1} of ${MAX_TURN_KNOWLEDGE_CALLS}\\b`),
    'the instruction states the attempts actually counted');
  assert.match(text, /knowledge_search x6/, 'the instruction names what was dispatched');
  assert.match(text, /Verified excerpts returned: 24/, 'the instruction reports what the turn obtained');
  assert.match(text, /Write the final answer now/);
  assert.match(text, /using only the material already obtained/);
  assert.match(text, /which parts it does not/);
  assert.match(text, /Do not invent sources/);
  // The instruction is a request, not a conclusion: it must not carry a finding of its own.
  assert.doesNotMatch(text, /quote a|quote b|\.txt/);

  // 4. The deterministic half is on the payload too, so a model that ignores the instruction still leaves a
  //    result that says what was obtained.
  assert.equal(refused.failure.detail.used, MAX_TURN_KNOWLEDGE_CALLS + 1);
  assert.equal(refused.failure.detail.max, MAX_TURN_KNOWLEDGE_CALLS);
  assert.equal(refused.failure.detail.evidence, 24);
  assert.equal(refused.failure.detail.documents, 0);
  assert.equal(refused.failure.terminal, true);
  assert.match(refused.warnings.join('\n'), /KNOWLEDGE_TURN_LIMIT_REACHED/);

  // 5. Successful results carry the accounting too, with its counting basis stated, and a warning once the
  //    turn is nearly spent — otherwise the model discovers the ceiling only by hitting it.
  const fifth = results[MAX_TURN_KNOWLEDGE_CALLS - 2];
  assert.equal(fifth.budget.remaining, 1);
  assert.match(fifth.budget.countedAs, /including attempts this plugin refused locally/);
  assert.match(String(fifth.budget.note), /One knowledge-tool dispatch remains/);
  assert.equal(results[0].budget.remaining, MAX_TURN_KNOWLEDGE_CALLS - 1);
  assert.equal(results[0].budget.note, undefined, 'a comfortable budget is reported without nagging');
});

test('a further call in the closing step is refused and does not buy a second closing step', async () => {
  const session = fakeSession();
  session.startTurn();
  const ledger = new TurnLedger({ max: 2 });
  const service = fakeService();
  const tools = harness(service.client, ledger);
  const { exec, record } = recordingExec(session);

  await tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'one' }, exec);
  await tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'two' }, exec);
  const first = await tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'three' }, exec);
  // The model ignores the instruction and calls again — the case that used to be an unbounded refusal loop.
  const second = await tools.get('knowledge_list_libraries').execute({}, exec);

  assert.equal(first.stopped, 'KNOWLEDGE_TURN_LIMIT_REACHED');
  assert.equal(second.stopped, 'KNOWLEDGE_TURN_LIMIT_REACHED');
  assert.equal(record.deferred.length, 1, 'the closing step is granted once, not once per refusal');
  assert.equal(record.concluded, 2, 'both refusals still conclude the turn');
  assert.equal(service.calls.length, 2, 'neither refusal did any work');
  assert.equal(ledger.closingClaimed(`s1#${session.events[0].seq}`), true);
});

test('parallel calls racing for the last slot grant exactly one closing step', async () => {
  const session = fakeSession();
  session.startTurn();
  const ledger = new TurnLedger({ max: 3 });
  const service = fakeService();
  const tools = harness(service.client, ledger);
  const { exec, record } = recordingExec(session);

  const dispatched = [];
  const dispatch = (index) => {
    session.step();
    return Promise.resolve(tools.get('knowledge_search').execute({ libraryId: 'lib', query: `q${index}` }, exec))
      .then((result) => { dispatched.push(result); return result; });
  };
  await Promise.all(Array.from({ length: 10 }, (_value, index) => dispatch(index)));

  assert.equal(service.calls.length, 3, 'exactly the ceiling may be dispatched');
  // Partition by outcome, not by position: results settle in completion order, which is not dispatch order.
  const admitted = dispatched.filter(result => result.stopped === undefined);
  const refused = dispatched.filter(result => result.stopped === 'KNOWLEDGE_TURN_LIMIT_REACHED');
  assert.equal(admitted.length, 3);
  assert.equal(refused.length, 7);
  // Each admitted call reports the ordinal it was admitted with, so the three are exactly #1..#3 even though
  // every one of them was assembled after all ten reservations had been taken.
  assert.deepEqual(admitted.map(result => result.budget.used).sort((left, right) => left - right), [1, 2, 3]);
  assert.deepEqual(refused.map(result => result.budget.used).sort((left, right) => left - right), [4, 5, 6, 7, 8, 9, 10]);
  assert.equal(record.deferred.length, 1, 'a race must not hand out two closing steps');
  assert.equal(record.concluded, 7, 'every refused call still concludes its turn');
  for (const result of refused) assert.equal(result.budget.remaining, 0);
});

test('the failure breaker also grants its one closing step, and only one', async () => {
  const session = fakeSession();
  session.startTurn();
  const ledger = new TurnLedger({ max: 50 });
  const service = fakeService(() => { throw new Error('the service is down'); });
  const tools = harness(service.client, ledger);
  const { exec, record } = recordingExec(session);

  const outcomes = [];
  for (let index = 0; index < 6; index += 1) {
    session.step();
    outcomes.push(await tools.get('knowledge_read').execute({ libraryId: 'lib', fileHandle: 'krh1.same' }, exec)
      .catch(error => ({ thrown: error.code })));
  }
  const stop = outcomes.find(outcome => outcome.stopped === 'KNOWLEDGE_REPEAT_REFUSED');
  assert.ok(stop, 'the identical-failure chain must trip');
  assert.equal(record.deferred.length, 1, 'a breaker stop also asks for the final answer, once');
  assert.equal(record.concluded, 1);
  // A stop has no excerpts to report, and says so with numbers rather than prose.
  assert.equal(stop.failure.detail.evidence, 0);
  assert.equal(stop.failure.detail.documents, 0);
});

test('a service that answers with nothing concludes honestly and invents no material', async () => {
  const session = fakeSession();
  session.startTurn();
  const ledger = new TurnLedger({ max: 2 });
  const service = fakeService(() => ({
    status: 'failed', answer: '', evidence: [],
    warnings: ['NO_MATCH: nothing in this library matches that query'],
  }));
  const tools = harness(service.client, ledger);
  const { exec, record } = recordingExec(session);

  const empty = await tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'nothing' }, exec);
  assert.deepEqual(empty.evidence, [], 'an empty result must stay empty');
  assert.equal(empty.budget.evidence, undefined, 'the tally lives on the budget, not invented on the payload');
  assert.equal(empty.budget.used, 1);
  assert.equal(record.deferred.length, 0, 'an ordinary empty result is not a stop and needs no closing step');
  assert.equal(record.concluded, 0);

  // And when the budget then runs out, the closing summary reports zero obtained rather than implying material.
  const second = await tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'still nothing' }, exec);
  const refused = await tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'again' }, exec);
  assert.equal(second.evidence.length, 0);
  assert.equal(refused.failure.detail.evidence, 0);
  const text = record.deferred[0].content.map(block => block.text).join('\n');
  assert.match(text, /Verified excerpts returned: 0/);
});

test('a service failure keeps its own code and is not turned into a closing step', async () => {
  const session = fakeSession();
  session.startTurn();
  const ledger = new TurnLedger({ max: 5 });
  const service = fakeService((name) => {
    if (name === 'search') throw new TypeError('fetch failed');
    return { libraries: [] };
  });
  const tools = harness(service.client, ledger);
  const { exec, record } = recordingExec(session);

  await assert.rejects(
    tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'q' }, exec),
    (error) => error.code === 'KNOWLEDGE_SERVICE_UNREACHABLE',
  );
  assert.equal(record.deferred.length, 0,
    'the turn is not concluded for a recoverable failure: the model may fix and retry in the same turn');
  assert.equal(record.concluded, 0);
  // The attempt still spent the turn's budget, and the next result says so.
  const next = await tools.get('knowledge_list_libraries').execute({}, exec);
  assert.equal(next.budget.used, 2);
});

test('the closing summary counts every kind of material the turn obtained, under its own name', async () => {
  const session = fakeSession();
  session.startTurn();
  const ledger = new TurnLedger({ max: 3 });
  const service = fakeService((name) => {
    if (name === 'catalogue') {
      return { libraryId: 'lib', status: 'ok', catalogueVersion: 'cv', total: 4, offset: 0, limit: 20,
        returned: 4, hasMore: false, nextOffset: null, warnings: [], note: '',
        entries: [1, 2, 3, 4].map(index => ({ index, documentId: `d${index}`, fileName: `d${index}.txt` })) };
    }
    if (name === 'read') {
      return { libraryId: 'lib', relativePath: 'texts/d1.txt', sourceVersion: 'v1', excerpt: 'a window' };
    }
    return { libraryId: 'lib', status: 'partial', answer: '', warnings: [], evidence: [evidenceItem('e1'), evidenceItem('e2')] };
  });
  const tools = harness(service.client, ledger);
  const { exec, record } = recordingExec(session);

  await tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'q' }, exec);
  await tools.get('knowledge_list_documents').execute({ libraryId: 'lib' }, exec);
  await tools.get('knowledge_read').execute({ libraryId: 'lib', fileHandle: 'krh1.x' }, exec);
  const refused = await tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'overflow' }, exec);

  // Each kind is counted under its own name and the three are never summed: a catalogue page is not an excerpt,
  // and a read window is a range of the original rather than a verified quotation.
  assert.equal(refused.failure.detail.evidence, 2);
  assert.equal(refused.failure.detail.documents, 4, 'a catalogue page counts its entries');
  assert.equal(refused.failure.detail.reads, 1, 'a read window counts itself');
  const text = record.deferred[0].content.map(block => block.text).join('\n');
  assert.match(text, /Verified excerpts returned: 2/);
  assert.match(text, /Catalogue documents returned: 4/);
  assert.match(text, /Read windows returned: 1/);
  // And the card line renders all three, because it is the deterministic half of the closing.
  const detail = describeDetail(refused.failure.detail);
  assert.match(detail, /本轮已取得 检索结果 2 条 \/ 目录项 4 条 \/ 读取窗口 1 个/);
});

test('the closing instruction is fixed text plus numbers, so it cannot carry material or a question', () => {
  // A direct check that the instruction has no field for anything that came from the library: the only
  // variable parts are the code, the counts and the tool names.
  const session = fakeSession();
  session.startTurn();
  const ledger = new TurnLedger({ max: 1 });
  const service = fakeService(() => ({
    status: 'partial', answer: '', warnings: [],
    evidence: [{ ...evidenceItem('secret'), excerpt: 'a distinctive sentence from a private document' }],
  }));
  const tools = harness(service.client, ledger);
  const { exec, record } = recordingExec(session);
  return tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'q' }, exec)
    .then(() => tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'q2' }, exec))
    .then(() => {
      const text = record.deferred[0].content.map(block => block.text).join('\n');
      assert.doesNotMatch(text, /distinctive sentence/);
      assert.doesNotMatch(text, /private document/);
      assert.match(text, /Verified excerpts returned: 1/);
    });
});


test('v4 codec rejects the exact retired closing source before accepting producer attribution', () => {
  const message = createUserMessage({content:[{type:'text',text:'Write the final answer now.'}], source:{kind:'plugin',plugin:'knowledge'}});
  assert.throws(() => releasedV4SessionFormatCodec.encodeEvent({type:'user/message',seq:0,time:Date.now(),data:message}), /producer-owned source kind/);
});

test('ordinary successful tool work never injects a closing message', async () => {
  const session=fakeSession();session.startTurn();
  const {exec,record}=recordingExec(session);
  const service=fakeService();const tools=harness(service.client,new TurnLedger({max:6}));
  await tools.get('knowledge_list_libraries').execute({},exec);
  assert.equal(record.deferred.length,0);assert.equal(record.concluded,0);
});
