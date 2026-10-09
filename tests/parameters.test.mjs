/**
 * P1: the model-visible parameter contract, aligned with what the service will actually accept.
 *
 * The observed defect: the model asked `knowledge_find_documents` for `limit: 20` and later `limit: 25`, both
 * of which the service refuses (`catalog.MAX_CANDIDATES = 10`, enforced as a 422). The model had no way to know
 * the ceiling — the declared parameter was `{type: 'number'}` with no bound and no description — so it spent
 * two of its six turn slots learning a constant the plugin already knew, and the refusals arrived classified as
 * a generic "the service rejected this request".
 *
 * Two facts shape the fix, and both are asserted here rather than asserted in prose:
 *
 * 1. **The installed SDK cannot express a numeric range.** `@deepseek-ai/dsh-tools`'s `ValueSchemaAnnotations`
 *    carries `description`, `title`, `default` and `examples` and no `minimum`/`maximum`, so the range lives in
 *    the description *and* in `PARAMETER_RANGES`, which the tool body enforces. A range written only into a
 *    description is advice; this file is what makes it a limit.
 * 2. **Out-of-range is refused, never clamped.** A clamped request is a request the caller believes it made
 *    and did not, which is how "I asked for 25 candidates" becomes "I got 10 and thought that was all there
 *    were". Out of range is refused locally — no HTTP, no container — and the refusal names the parameter and
 *    its range while carrying no echo of the value that was sent.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { definitions, PARAMETER_RANGES } from '../lib/index.js';
import { FAILURE_PARAMETERS } from '../lib/failures.js';
import { TurnLedger } from '../lib/turnlimit.js';

const exec = () => ({ agent: { session: { header: { id: 's1', cwd: 'C:\\ws' } } } });

/** A client that records every dispatched request, so "refused locally" is an external fact. */
function fakeClient() {
  const calls = [];
  const record = (name) => async (...args) => { calls.push({ name, args }); return { libraryId: 'lib', documents: [], entries: [], evidence: [], warnings: [], note: '' }; };
  return {
    calls,
    client: {
      libraries: record('libraries'), search: record('search'), findDocuments: record('findDocuments'),
      catalogue: record('catalogue'), read: record('read'),
    },
  };
}

/**
 * A harness with a deliberately generous turn ceiling.
 *
 * A parameter refusal *does* spend a turn slot — it is an attempt the model made, and the result says so — so a
 * boundary sweep of ten cases would otherwise exhaust the real ceiling and start returning terminal stops
 * instead of the range refusals under test. The counting itself is asserted separately, at the end of this file.
 */
function harness(client, max = 200) {
  return new Map(definitions(client, undefined, undefined, new TurnLedger({ max })).map(tool => [tool.name, tool]));
}

/**
 * One refused call in a **fresh** plugin life.
 *
 * Fresh on purpose: consecutive failures trip the plugin's own breaker at eight, so a ten-case sweep through
 * one harness would stop testing ranges and start testing the breaker. Each case here is about the range check
 * and nothing else.
 */
async function refuses(toolName, args) {
  const service = fakeClient();
  const tools = harness(service.client);
  const error = await tools.get(toolName).execute(args, exec()).then(() => null, (caught) => caught);
  return { error, service, tools };
}

test('every declared range is stated in the description the model reads', () => {
  // The description is the model's only warning: if a range is not in it, the model discovers the bound by
  // being refused, which is exactly the cost this round removes.
  const tools = harness(fakeClient().client);
  for (const key of Object.keys(PARAMETER_RANGES)) {
    const [toolName, parameter] = key.split('.');
    const range = PARAMETER_RANGES[key];
    const declared = tools.get(toolName)?.parameters?.[parameter];
    assert.ok(declared, `${key} must be a declared parameter`);
    assert.equal(declared.type, 'integer', `${key} must be declared as an integer, not a number`);
    // A closed range reads `min..max`; a floor without a ceiling reads `>= min`. The description must match
    // whichever the table declares, because the table is what the tool body enforces.
    const expected = range.max === null ? new RegExp(`>= ${range.min}\\b`) : new RegExp(`${range.min}\\.\\.${range.max}\\b`);
    assert.match(String(declared.description), expected, `${key}'s description must state its range`);
    // And the range is one the failure taxonomy can name, or a refusal could not report it.
    assert.ok(FAILURE_PARAMETERS.includes(parameter), `${parameter} must be a member of the closed parameter set`);
  }
  // `limit` used to be `{type: 'number'}` with no description at all.
  assert.ok(tools.get('knowledge_find_documents').parameters.limit.description.length > 20);
});

test('an out-of-range find limit is refused locally, at the exact boundary', async () => {
  for (const limit of [0, -1, 11, 20, 25, 1000, 1.5, '10', NaN]) {
    const { error, service } = await refuses('knowledge_find_documents', { libraryId: 'lib', title: 't', limit });
    assert.ok(error, `limit=${String(limit)} must be refused`);
    assert.equal(error.code, 'KNOWLEDGE_PARAMETER_OUT_OF_RANGE', `limit=${String(limit)}`);
    assert.equal(service.calls.length, 0, 'a refused parameter must not reach the service');
  }
  // The boundary itself is accepted: 1 and 10 are the declared ends.
  const service = fakeClient();
  const find = harness(service.client).get('knowledge_find_documents');
  for (const limit of [1, 5, 10]) await find.execute({ libraryId: 'lib', title: 't', limit }, exec());
  assert.deepEqual(service.calls.map(call => call.args[2].limit), [1, 5, 10],
    'an accepted value is passed through unchanged, never replaced by a clamp');
});

test('a refused parameter names itself and its range, and never repeats the value', async () => {
  const refused = await refuses('knowledge_find_documents', { libraryId: 'lib', title: 't', limit: 25 });
  const error = refused.error;
  assert.ok(error);
  assert.equal(error.code, 'KNOWLEDGE_PARAMETER_OUT_OF_RANGE');
  assert.equal(error.record.detail.parameter, 'limit');
  assert.equal(error.record.detail.tool, 'knowledge_find_documents');
  // The range is what makes the refusal actionable, and it has to survive classification into the message the
  // model actually reads. It did not, until `classifyFailure` started carrying a plugin-built sentence.
  assert.match(error.message, /1 to 10/);
  assert.doesNotMatch(error.message, /\b25\b/, 'the sent value is not echoed back');
  // The detail is typed: a parameter is a member of a closed set or null, so nothing else can travel in it.
  const hostile = await refuses('knowledge_read', { libraryId: 'lib', ref: 'kref-aaaaaaaa', maxChars: 999999 });
  assert.ok(hostile.error);
  assert.equal(hostile.error.record.detail.parameter, 'maxChars');
  assert.match(hostile.error.message, /1 to 20000/);
  assert.doesNotMatch(hostile.error.message, /999999/);
  assert.equal(hostile.service.calls.length, 0);
});

test('an omitted bounded parameter takes its declared default', async () => {
  const service = fakeClient();
  const tools = harness(service.client);
  await tools.get('knowledge_find_documents').execute({ libraryId: 'lib', title: 't' }, exec());
  assert.equal(service.calls[0].args[2].limit, 10, 'find defaults to the service maximum');
  await tools.get('knowledge_list_documents').execute({ libraryId: 'lib' }, exec());
  assert.equal(service.calls[1].args[2].offset, 0);
  assert.equal(service.calls[1].args[2].limit, 20, 'a listing defaults to one page of a small library');
});

test('the enumeration ranges are refused at their own boundaries, and never clamped', async () => {
  for (const [parameter, bad] of [['offset', -1], ['offset', 1.5], ['offset', '0'], ['limit', 0], ['limit', 51], ['limit', '20']]) {
    const { error, service } = await refuses('knowledge_list_documents', { libraryId: 'lib', [parameter]: bad });
    assert.ok(error, `${parameter}=${String(bad)} must be refused`);
    assert.equal(error.code, 'KNOWLEDGE_PARAMETER_OUT_OF_RANGE');
    assert.equal(service.calls.length, 0);
  }
  const service = fakeClient();
  const list = harness(service.client).get('knowledge_list_documents');
  await list.execute({ libraryId: 'lib', offset: 0, limit: 50 }, exec());
  // `offset` has a floor and no ceiling — the service answers a position past the end with an empty page, so
  // the plugin does not declare a bound nobody enforces.
  await list.execute({ libraryId: 'lib', offset: 5000, limit: 1 }, exec());
  assert.deepEqual(service.calls.map(call => [call.args[2].offset, call.args[2].limit]), [[0, 50], [5000, 1]]);
});

test('the read ranges match the service limits, refused rather than clamped', async () => {
  const cases = [
    ['startLine', 0], ['startLine', -4], ['startLine', 1.5],
    ['maxChars', 0], ['maxChars', 20001], ['maxChars', 50000],
    ['maxLines', 0], ['maxLines', 201],
    ['overlapLines', -1], ['overlapLines', 4],
  ];
  for (const [parameter, bad] of cases) {
    const { error, service } = await refuses('knowledge_read', { libraryId: 'lib', fileHandle: 'krh1.x', [parameter]: bad });
    assert.ok(error, `${parameter}=${bad} must be refused`);
    assert.equal(error.code, 'KNOWLEDGE_PARAMETER_OUT_OF_RANGE', `${parameter}=${String(bad)}`);
    assert.equal(error.record.detail.parameter, parameter);
    assert.equal(service.calls.length, 0);
  }
  // The declared ends are accepted and forwarded verbatim: the plugin does not silently shorten a request.
  const service = fakeClient();
  const read = harness(service.client).get('knowledge_read');
  await read.execute({ libraryId: 'lib', fileHandle: 'krh1.x', maxChars: 20000, maxLines: 200, overlapLines: 3, startLine: 1 }, exec());
  assert.deepEqual(
    [service.calls[0].args[2].maxChars, service.calls[0].args[2].maxLines, service.calls[0].args[2].overlapLines],
    [20000, 200, 3]);
  assert.equal(service.calls[0].args[2].startLine, 1);
});

test('a parameter refusal is correctable in the same turn: it is not a terminal stop', async () => {
  const service = fakeClient();
  // The real ceiling here on purpose: this is the test that pins the counting, so it must not be relaxed.
  const tools = harness(service.client, 6);
  let concluded = 0;
  const context = { ...exec(), concludeTurn: () => { concluded += 1; }, deferContext: () => { throw new Error('no closing step for a fixable mistake'); } };
  const refused = await tools.get('knowledge_find_documents')
    .execute({ libraryId: 'lib', title: 't', limit: 25 }, context).then(() => null, (error) => error);
  assert.equal(refused.code, 'KNOWLEDGE_PARAMETER_OUT_OF_RANGE');
  assert.equal(concluded, 0, 'a wrong argument must not end the turn: the model can fix it and retry');
  // The corrected call in the same turn succeeds, and it counted: an argument mistake is still an attempt.
  const accepted = await tools.get('knowledge_find_documents').execute({ libraryId: 'lib', title: 't', limit: 10 }, context);
  assert.equal(service.calls.length, 1);
  assert.equal(accepted.budget.used, 2, 'the refused attempt spent a slot, and the result says so');
  assert.equal(accepted.budget.max, 6);
});
