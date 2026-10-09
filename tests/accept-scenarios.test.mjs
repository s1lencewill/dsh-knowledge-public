/**
 * Regressions for the acceptance scenario machine, built from the shapes the host and SDK actually use.
 *
 * The message list below is the OpenAI-style request a model sees, and the tool results are the two forms that
 * appeared in the real session: an assistant message carrying `tool_calls`, and a tool result as a `content` part
 * of type `tool-result` with a `toolCallId` (the form the session log persists) or as a `role: 'tool'` message
 * with `tool_call_id` (the form the provider sees). The ids are the ones from the run that failed, so the tests
 * are anchored in that evidence rather than in a shape invented for the implementation.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { createScenarioMachine, scenarioOfTurn, toolResults, codeOf, EXPECTED_TOOL_CALLS }
  from '../scripts/accept/scenario-machine.mjs';

const TOOLS = [{ type: 'function', function: { name: 'knowledge_list_libraries' } },
  { type: 'function', function: { name: 'knowledge_read' } }];

const userText = (text) => ({ role: 'user', content: [{ type: 'text', text }] });
const assistantCalls = (...calls) => ({ role: 'assistant', content: null,
  tool_calls: calls.map(([id, name, args]) => ({ id, type: 'function', function: { name, arguments: JSON.stringify(args) } })) });
/** The persisted shape: a user message whose content carries the tool result part. */
const resultPart = (callId, text, isError = false) => ({ role: 'user',
  content: [{ type: 'tool-result', toolCallId: callId, content: [{ type: 'text', text }], isError }] });
/** The provider shape for the same thing. */
const resultRole = (callId, text) => ({ role: 'tool', tool_call_id: callId, content: text });

const FIND_OK = JSON.stringify({ libraryId: 'samples', documents: [{ documentId: 'study', readable: true,
  readRef: 'kref-synthetic1' }], warnings: [] });
const READ_OK = JSON.stringify({ startLine: 1, endLine: 8, excerpt: 'PDF page 1\nThermal Stability', sourceCard: {
  citationOrigin: 'read', libraryId: 'samples', fileName: 'study.pdf' } });
const READ_ERROR = 'Error: KNOWLEDGE_HANDLE_INVALID: 随调用提交的文件标识不是本服务签发过的标识。（关联 ID k-035ix1e2）';
const LIST_OK = JSON.stringify({ libraries: [{ id: 'samples', name: 'Trial sample library (synthetic)' }],
  selectedLibraryId: 'samples' });
const CEILING_STOP = JSON.stringify({ status: 'failed', stopped: 'KNOWLEDGE_TURN_LIMIT_REACHED', tool: 'knowledge_read',
  failure: { code: 'KNOWLEDGE_TURN_LIMIT_REACHED', detail: { layer: 'plugin', tool: 'knowledge_read', used: 7, max: 6 } } });

test('a host-injected reminder in the middle of a scenario does not end it', () => {
  const machine = createScenarioMachine();
  const messages = [userText('ACCEPT-CEILING')];
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const decision = machine.decide({ messages, tools: TOOLS });
    messages.push(assistantCalls([decision.callId, decision.name, decision.args]),
      resultPart(decision.callId, LIST_OK));
  }
  // Exactly what a real run injected after the third identical call: a user message that is not an instruction.
  messages.push(userText('You are repeating the exact same tool call with identical arguments. Carefully analyze the '
    + 'previous result before calling again: if the task is not complete, try a different approach or different arguments.'));
  const fourth = machine.decide({ messages, tools: TOOLS });
  assert.equal(fourth.kind, 'tool', 'the reminder is background, not a turn boundary');
  assert.equal(fourth.name, 'knowledge_list_libraries');
  assert.equal(fourth.stage, 3, 'and the stage is counted from the calls this turn actually issued');
});

test('a runtime-context or skill-list injection is tolerated the same way', () => {
  const machine = createScenarioMachine();
  const messages = [userText('ACCEPT-1'), userText('Current runtime context. This snapshot supersedes earlier ones.'),
    userText('<system-reminder> A skill is a reusable set of instructions.')];
  const decision = machine.decide({ messages, tools: TOOLS });
  assert.equal(decision.kind, 'tool');
  assert.equal(decision.name, 'knowledge_find_documents');
});

test('a final assistant answer closes the turn, so an older marker is never replayed', () => {
  const messages = [userText('ACCEPT-FAIL'), assistantCalls(['old-1', 'knowledge_read', {}]),
    resultPart('old-1', READ_ERROR, true),
    { role: 'assistant', content: [{ type: 'text', text: 'ACCEPT-FAIL: script complete.' }] },
    userText('and now a normal question with no marker')];
  const found = scenarioOfTurn(messages);
  assert.equal(found, null, 'the previous turn\'s ACCEPT-FAIL must not be selected again');
});

test('every emitted call carries the arguments its tool requires', () => {
  const machine = createScenarioMachine();
  for (const scenario of ['ACCEPT-1', 'ACCEPT-FAIL', 'ACCEPT-CEILING']) {
    const messages = [userText(scenario)];
    for (let step = 0; step < 3; step += 1) {
      const decision = machine.decide({ messages, tools: TOOLS });
      if (decision.kind !== 'tool') break;
      if (decision.name !== 'knowledge_list_libraries') {
        // This is the defect the rerun exposed: the read step carried a handle but no `libraryId`, and the host
        // rejected it as `invalid arguments` before any knowledge code existed.
        assert.equal(decision.args.libraryId, 'samples', `${scenario}: ${decision.name} must name a library`);
      }
      if (decision.name === 'knowledge_read') {
        // A current result carries `readRef`; the machine falls back to `fileHandle` only for an older build.
        assert.ok(typeof (decision.args.ref ?? decision.args.fileHandle) === 'string',
          `${scenario}: the read must carry an identifier`);
        assert.ok(decision.args.maxChars > 0 && decision.args.maxLines > 0);
      }
      messages.push(assistantCalls([decision.callId, decision.name, decision.args]),
        resultPart(decision.callId, scenario === 'ACCEPT-1' ? (step === 0 ? FIND_OK : READ_OK) : LIST_OK));
    }
  }
});

test('ACCEPT-1 does not call a failed read "complete"', () => {
  const machine = createScenarioMachine();
  const messages = [userText('ACCEPT-1')];
  const find = machine.decide({ messages, tools: TOOLS });
  messages.push(assistantCalls([find.callId, find.name, find.args]), resultPart(find.callId, FIND_OK));
  const read = machine.decide({ messages, tools: TOOLS });
  assert.equal(read.name, 'knowledge_read');
  assert.equal(read.args.libraryId, 'samples');
  // The host's own argument-validation error: an error with no knowledge code at all, which is what the rerun hit.
  messages.push(assistantCalls([read.callId, read.name, read.args]),
    resultPart(read.callId, 'Error: invalid arguments: missing required property "libraryId"', true));
  const after = machine.decide({ messages, tools: TOOLS });
  assert.equal(after.kind, 'text');
  assert.match(after.text, /ACCEPT-1 FAILED: step 2 of 2/);
  assert.match(after.text, /an error without a knowledge code/);
  assert.doesNotMatch(after.text, /script complete/);
  assert.equal(machine.report().turns[0].callsIssued, 2, 'and it still issues no third call');
});

test('ACCEPT-1 reports the knowledge code when the read fails with one', () => {
  const machine = createScenarioMachine();
  const messages = [userText('ACCEPT-1')];
  const find = machine.decide({ messages, tools: TOOLS });
  messages.push(assistantCalls([find.callId, find.name, find.args]), resultPart(find.callId, FIND_OK));
  const read = machine.decide({ messages, tools: TOOLS });
  messages.push(assistantCalls([read.callId, read.name, read.args]), resultPart(read.callId, READ_ERROR, true));
  const after = machine.decide({ messages, tools: TOOLS });
  assert.match(after.text, /ACCEPT-1 FAILED: step 2 of 2 returned KNOWLEDGE_HANDLE_INVALID/);
});

test('the scenario is read from the current turn only, never from history', () => {
  const history = [userText('ACCEPT-FAIL'), assistantCalls(['a1', 'knowledge_read', {}]), resultPart('a1', READ_ERROR),
    assistantCalls(['a2', 'knowledge_read', {}]), resultPart('a2', READ_ERROR), assistantCalls(['a3', 'knowledge_read', {}]),
    userText('ACCEPT-1 样例库问答')];
  const found = scenarioOfTurn(history);
  assert.equal(found.scenario, 'ACCEPT-1');
  assert.equal(found.index, history.length - 1);
});

test('a turn with no scenario marker is refused rather than defaulting to a read', () => {
  const machine = createScenarioMachine();
  const decision = machine.decide({ messages: [userText('just a question')], tools: TOOLS });
  assert.equal(decision.kind, 'refuse');
  assert.match(decision.reason, /no ACCEPT-1/);
});

test('a request without a tool catalog is refused, and the title route is answered', () => {
  const machine = createScenarioMachine();
  assert.equal(machine.decide({ messages: [userText('ACCEPT-1')], tools: [] }).kind, 'refuse');
  const title = machine.decide({ model: 'title-stub', messages: [userText('ACCEPT-1')], tools: TOOLS });
  assert.equal(title.kind, 'text');
  assert.equal(title.route, 'title');
});

test('ACCEPT-1 issues exactly a locate and one read, then ends in text', () => {
  const machine = createScenarioMachine();
  const messages = [userText('ACCEPT-1 样例库问答')];
  const first = machine.decide({ messages, tools: TOOLS });
  assert.equal(first.name, 'knowledge_find_documents');
  messages.push(assistantCalls([first.callId, first.name, first.args]), resultPart(first.callId, FIND_OK));

  const second = machine.decide({ messages, tools: TOOLS });
  assert.equal(second.name, 'knowledge_read');
  assert.equal(second.args.ref, 'kref-synthetic1',
    'the machine copies the short reference the result carried, not a long handle');
  messages.push(assistantCalls([second.callId, second.name, second.args]), resultPart(second.callId, READ_OK, false));

  const third = machine.decide({ messages, tools: TOOLS });
  assert.equal(third.kind, 'text');
  assert.match(third.text, /script complete after 2 tool call/);
  assert.equal(machine.report().turns[0].callsIssued, 2, 'no third call is ever issued');
  assert.ok(EXPECTED_TOOL_CALLS['ACCEPT-1'] < 6, 'the declared script stays under the per-turn ceiling');
});

test('ACCEPT-1 ends without a read when the locate result carries no handle', () => {
  const machine = createScenarioMachine();
  const messages = [userText('ACCEPT-1')];
  const first = machine.decide({ messages, tools: TOOLS });
  messages.push(assistantCalls([first.callId, first.name, first.args]),
    resultPart(first.callId, JSON.stringify({ libraryId: 'samples', documents: [], warnings: [] })));
  const second = machine.decide({ messages, tools: TOOLS });
  assert.equal(second.kind, 'text');
  assert.match(second.text, /ACCEPT-1 FAILED/);
  assert.equal(machine.report().turns[0].callsIssued, 1);
});

test('ACCEPT-FAIL issues one failing call, recognises the code, and never repeats it', () => {
  const machine = createScenarioMachine();
  const messages = [userText('ACCEPT-FAIL')];
  const first = machine.decide({ messages, tools: TOOLS });
  assert.equal(first.name, 'knowledge_read');
  assert.equal(first.args.fileHandle, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
  messages.push(assistantCalls([first.callId, first.name, first.args]), resultPart(first.callId, READ_ERROR, true));

  const second = machine.decide({ messages, tools: TOOLS });
  assert.equal(second.kind, 'text');
  assert.match(second.text, /KNOWLEDGE_HANDLE_INVALID/);
  assert.match(second.text, /Script complete/);
  assert.equal(machine.report().turns[0].callsIssued, 1, 'the failing call is not repeated');
});

test('ACCEPT-FAIL reports a failed scenario when the controlled call unexpectedly succeeds', () => {
  const machine = createScenarioMachine();
  const messages = [userText('ACCEPT-FAIL')];
  const first = machine.decide({ messages, tools: TOOLS });
  messages.push(assistantCalls([first.callId, first.name, first.args]), resultPart(first.callId, READ_OK, false));
  const second = machine.decide({ messages, tools: TOOLS });
  assert.equal(second.kind, 'text');
  assert.match(second.text, /ACCEPT-FAIL FAILED/);
  assert.equal(machine.report().turns[0].callsIssued, 1);
});

test('ACCEPT-CEILING asks seven times, ends when the seventh is refused, and never asks an eighth', () => {
  const machine = createScenarioMachine();
  const messages = [userText('ACCEPT-CEILING')];
  for (let attempt = 1; attempt <= 6; attempt += 1) {
    const decision = machine.decide({ messages, tools: TOOLS });
    assert.equal(decision.name, 'knowledge_list_libraries', `attempt ${attempt} is a list call`);
    messages.push(assistantCalls([decision.callId, decision.name, decision.args]), resultPart(decision.callId, LIST_OK));
  }
  const seventh = machine.decide({ messages, tools: TOOLS });
  assert.equal(seventh.name, 'knowledge_list_libraries', 'the seventh attempt is made, so the ceiling is what refuses it');
  messages.push(assistantCalls([seventh.callId, seventh.name, seventh.args]), resultPart(seventh.callId, CEILING_STOP));

  const after = machine.decide({ messages, tools: TOOLS });
  assert.equal(after.kind, 'text');
  assert.match(after.text, /KNOWLEDGE_TURN_LIMIT_REACHED/);
  const again = machine.decide({ messages, tools: TOOLS });
  assert.equal(again.kind, 'text', 'a later request in the same turn still produces no eighth call');
  assert.equal(machine.report().turns[0].callsIssued, 7);
});

test('ACCEPT-CEILING reports a failed scenario if the seventh call is dispatched instead of refused', () => {
  const machine = createScenarioMachine();
  const messages = [userText('ACCEPT-CEILING')];
  for (let attempt = 1; attempt <= 7; attempt += 1) {
    const decision = machine.decide({ messages, tools: TOOLS });
    messages.push(assistantCalls([decision.callId, decision.name, decision.args]), resultPart(decision.callId, LIST_OK));
  }
  const after = machine.decide({ messages, tools: TOOLS });
  assert.equal(after.kind, 'text');
  assert.match(after.text, /ACCEPT-CEILING FAILED/);
  assert.equal(machine.report().turns[0].callsIssued, 7, 'it still does not ask an eighth time');
});

test('three scenarios in one session do not contaminate each other', () => {
  const machine = createScenarioMachine();
  const messages = [userText('ACCEPT-1')];
  const find = machine.decide({ messages, tools: TOOLS });
  messages.push(assistantCalls([find.callId, find.name, find.args]), resultPart(find.callId, FIND_OK));
  const read = machine.decide({ messages, tools: TOOLS });
  messages.push(assistantCalls([read.callId, read.name, read.args]), resultPart(read.callId, READ_OK));
  assert.match(machine.decide({ messages, tools: TOOLS }).text, /ACCEPT-1: script complete/);

  messages.push(userText('ACCEPT-FAIL'));
  const failing = machine.decide({ messages, tools: TOOLS });
  assert.equal(failing.name, 'knowledge_read', 'the new turn starts its own script');
  assert.equal(failing.args.fileHandle, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
  messages.push(assistantCalls([failing.callId, failing.name, failing.args]), resultPart(failing.callId, READ_ERROR, true));
  assert.match(machine.decide({ messages, tools: TOOLS }).text, /ACCEPT-FAIL: the controlled call failed as expected/);

  messages.push(userText('ACCEPT-CEILING'));
  const listed = machine.decide({ messages, tools: TOOLS });
  assert.equal(listed.name, 'knowledge_list_libraries');
  assert.equal(machine.report().turns.length, 3, 'one state entry per turn');
  assert.deepEqual(machine.report().turns.map(turn => turn.callsIssued), [2, 1, 1]);
});

test('the same scenario twice in a row starts over instead of continuing the old stage', () => {
  const machine = createScenarioMachine();
  const messages = [userText('ACCEPT-1')];
  const first = machine.decide({ messages, tools: TOOLS });
  messages.push(assistantCalls([first.callId, first.name, first.args]), resultPart(first.callId, FIND_OK));
  const second = machine.decide({ messages, tools: TOOLS });
  messages.push(assistantCalls([second.callId, second.name, second.args]), resultPart(second.callId, READ_OK));
  assert.equal(machine.decide({ messages, tools: TOOLS }).kind, 'text');

  messages.push(userText('ACCEPT-1 again, same wording'));
  const restart = machine.decide({ messages, tools: TOOLS });
  assert.equal(restart.name, 'knowledge_find_documents', 'a second ACCEPT-1 runs its own script from step 1');
  assert.equal(machine.report().turns.length, 2);
});

test('two interleaved turns keep separate state', () => {
  const machine = createScenarioMachine();
  const turnA = [userText('ACCEPT-FAIL')];
  const turnB = [userText('ACCEPT-CEILING')];
  const a1 = machine.decide({ messages: turnA, tools: TOOLS });
  const b1 = machine.decide({ messages: turnB, tools: TOOLS });
  assert.equal(a1.name, 'knowledge_read');
  assert.equal(b1.name, 'knowledge_list_libraries');
  turnA.push(assistantCalls([a1.callId, a1.name, a1.args]), resultPart(a1.callId, READ_ERROR, true));
  turnB.push(assistantCalls([b1.callId, b1.name, b1.args]), resultPart(b1.callId, LIST_OK));
  assert.match(machine.decide({ messages: turnA, tools: TOOLS }).text, /ACCEPT-FAIL/);
  assert.equal(machine.decide({ messages: turnB, tools: TOOLS }).name, 'knowledge_list_libraries',
    'turn B is still on its own second step');
});

test('a tool result this machine never issued cannot advance the stage', () => {
  const machine = createScenarioMachine();
  const messages = [userText('ACCEPT-1')];
  // A result from an earlier turn (a different call id) sits in the history: the old implementation counted it.
  messages.push(assistantCalls(['acc-3-0', 'knowledge_find_documents', {}]), resultPart('acc-3-0', FIND_OK));
  const decision = machine.decide({ messages, tools: TOOLS });
  assert.equal(decision.name, 'knowledge_find_documents', 'the script still starts at step 1');
});

test('both wire shapes of a tool result are read, and the shape is reported', () => {
  const machine = createScenarioMachine();
  const messages = [userText('ACCEPT-1')];
  const first = machine.decide({ messages, tools: TOOLS });
  messages.push(assistantCalls([first.callId, first.name, first.args]), resultRole(first.callId, FIND_OK));
  const second = machine.decide({ messages, tools: TOOLS });
  assert.equal(second.name, 'knowledge_read');
  assert.deepEqual(machine.report().resultShapesObserved, ['role-tool']);
  const parsed = toolResults(messages, 0);
  assert.equal(parsed[0].callId, first.callId);
});

test('a failure code is read from the error text and from the structured stop', () => {
  assert.equal(codeOf({ text: READ_ERROR }), 'KNOWLEDGE_HANDLE_INVALID');
  assert.equal(codeOf({ text: CEILING_STOP }), 'KNOWLEDGE_TURN_LIMIT_REACHED');
  assert.equal(codeOf({ text: JSON.stringify({ ok: true }) }), null);
});

test('call ids are globally unique across scenarios and turns', () => {
  const machine = createScenarioMachine();
  const ids = new Set();
  for (const scenario of ['ACCEPT-1', 'ACCEPT-CEILING', 'ACCEPT-FAIL']) {
    const messages = [userText(scenario)];
    for (let step = 0; step < 3; step += 1) {
      const decision = machine.decide({ messages, tools: TOOLS });
      if (decision.kind !== 'tool') break;
      assert.ok(!ids.has(decision.callId), `duplicate call id ${decision.callId}`);
      ids.add(decision.callId);
      messages.push(assistantCalls([decision.callId, decision.name, decision.args]),
        resultPart(decision.callId, scenario === 'ACCEPT-1' ? (step === 0 ? FIND_OK : READ_OK) : LIST_OK));
    }
  }
  // The point is uniqueness, not a count: every id handed out in three different scenarios is distinct.
  assert.equal(ids.size, 6, 'ACCEPT-1 asked twice, ACCEPT-CEILING three times, ACCEPT-FAIL once — all distinct');
});
