// The failure taxonomy: classification, and the guarantee that no upstream text can reach a card.
//
// The card's safety argument is no longer "we filter dangerous strings". It is stronger and simpler: **a
// failure record has no free-text field at all**. Every field is a member of a declared set, a safe integer,
// or the system-generated correlation id - so there is nowhere for an exception message, a path, a URL, a key,
// a handle or a request body to be stored, let alone displayed.
//
// These tests therefore do not test a filter. They (a) prove the sets are closed, and (b) feed a deliberately
// hostile value through every field, including ones designed to defeat a character allow-list, and assert it
// does not survive - which it cannot, because a non-member is replaced by null rather than cleaned.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  FAILURE_ADVICE, FAILURE_CODES, FAILURE_TOOLS, KNOWN_SERVICE_CODES, KnowledgeFailure, KnowledgeServiceError,
  classifyFailure, describeDetail, failureDetail, failureFrom, failureRecord, failureText, isReferenceId,
  knownParameter, knownServiceCode, knownTool, readFailureRecord, readRefusalOf, referenceId, serviceCodeOf,
  stopMessage,
} from '../lib/failures.js';

/** Values that must never appear on a card, a tool result or a diagnostic. */
const HOSTILE = {
  shortKeyWithSeparators: 'sk-synthetic-public-fixture-not-a-real-credential',
  signedHandle: 'krh1.eyJrIjoiaCIsImwiOiJzeW50aGV0aWMtcHJvYmUifQ.zBVLbHIZSLrFA8j0q6',
  url: 'https://api.example.com/v1/chat/completions?token=abc',
  privatePath: 'C:\\Users\\someone\\Documents\\secret\\study.txt',
  posixPath: '/home/u/secret/study.txt',
  requestBody: '{"query":"the whole request body","apiKey":"sk-live-0123456789abcdef"}',
  bearer: 'Authorization: Bearer sk-live-0123456789abcdef',
  // Synthetic values only. This one is deliberately shaped like a provider key so the leak assertions have
  // something realistic to look for, and it is invented here rather than copied from anywhere.
  token: 'sk-synthetic-public-fixture-not-a-real-credential',
};
const ALL_HOSTILE = Object.values(HOSTILE);

/** Every string a record can put on screen or in a tool result, flattened. */
const renderOf = (record) => [record.code, record.category, record.reason, record.action,
  failureText(record), stopMessage(record), describeDetail(record.detail), JSON.stringify(record)].join('\n');

const assertNoLeak = (text, label) => {
  for (const [name, value] of Object.entries(HOSTILE)) {
    assert.ok(!text.includes(value), `${label} must not contain the ${name}`);
  }
  // A handful of loose invariants, so a value that is a substring of one of the samples is also caught.
  assert.ok(!/sk-[a-z]*-?[A-Za-z0-9]{12,}/.test(text), `${label} must not contain anything key-shaped`);
  assert.ok(!/krh1\./.test(text), `${label} must not contain a signed handle`);
  assert.ok(!/[A-Za-z]:\\|\/home\//.test(text), `${label} must not contain a path`);
  assert.ok(!/https?:\/\//.test(text), `${label} must not contain a URL`);
};

test('every code has a complete, non-empty display entry', () => {
  for (const code of FAILURE_CODES) {
    const advice = FAILURE_ADVICE[code];
    assert.ok(advice, `${code} must have display text`);
    for (const field of ['category', 'reason', 'action']) {
      assert.equal(typeof advice[field], 'string');
      assert.ok(advice[field].length >= 3, `${code}.${field} must say something`);
      assert.ok(!/\n/.test(advice[field]), `${code}.${field} must be one line`);
    }
  }
  assert.equal(Object.keys(FAILURE_ADVICE).length, FAILURE_CODES.length, 'no entry may be orphaned');
});

test('the declared sets are closed and small', () => {
  assert.ok(FAILURE_TOOLS.length === 5, 'the enumeration tool is a failure source like the others');
  assert.ok(KNOWN_SERVICE_CODES.length >= 10);
  // The module exposes the membership tests the record builder uses, so a test can drive them directly.
  assert.equal(knownServiceCode('SOURCE_VERSION_CHANGED'), 'SOURCE_VERSION_CHANGED');
  assert.equal(knownServiceCode('NOT_A_CODE'), null);
  assert.equal(knownServiceCode('source_version_changed'), null, 'membership is exact, not case-folded');
  assert.equal(knownTool('knowledge_read'), 'knowledge_read');
  assert.equal(knownTool('pwsh'), null, 'a tool outside the set has no field to occupy');
  assert.equal(knownParameter('limit'), 'limit');
  assert.equal(knownParameter('fileHandle'), 'fileHandle');
  // A refusal names the offending *parameter*, so the set has to exist and has to be closed: `limit=<value>`
  // must have nowhere to land.
  assert.equal(knownParameter('limit=25'), null);
  assert.equal(knownParameter('../../etc/passwd'), null);
  assert.equal(knownParameter('pwsh'), null);
});

test('the service code is taken only when it is already a member of the declared set', () => {
  assert.equal(serviceCodeOf('SOURCE_VERSION_CHANGED: the file changed'), 'SOURCE_VERSION_CHANGED');
  assert.equal(serviceCodeOf('  READ_LIMIT_EXCEEDED: budget exhausted'), 'READ_LIMIT_EXCEEDED');
  // Everything below looks like a code by shape and must still be refused, because shape is not the check.
  assert.equal(serviceCodeOf('SOMETHING_ELSE: x'), null, 'an unknown code is refused, not carried');
  assert.equal(serviceCodeOf('SK_LIVE_0123456789: not a service code'), null);
  assert.equal(serviceCodeOf('lowercase code: x'), null);
  assert.equal(serviceCodeOf(''), null);
  assert.equal(serviceCodeOf(null), null);
  assert.equal(serviceCodeOf({ detail: 'SOURCE_VERSION_CHANGED' }), null);
});

test('each required failure class is classified to its own code', () => {
  const required = {
    SOURCE_VERSION_CHANGED: new KnowledgeServiceError(409, 'SOURCE_VERSION_CHANGED'),
    LIBRARY: new KnowledgeFailure('KNOWLEDGE_LIBRARY_NOT_SELECTED'),
    UNREACHABLE: new TypeError('fetch failed'),
    TIMEOUT: Object.assign(new Error('The operation was aborted due to timeout'), { name: 'TimeoutError' }),
    AUTH: new KnowledgeServiceError(401, null),
    QUOTA: new KnowledgeServiceError(429, 'READ_LIMIT_EXCEEDED'),
    CAPACITY: new KnowledgeServiceError(429, 'RESULT_CAPACITY'),
    BREAKER: new KnowledgeFailure('KNOWLEDGE_REPEAT_REFUSED'),
    TURN_LIMIT: new KnowledgeFailure('KNOWLEDGE_TURN_LIMIT_REACHED'),
    UNKNOWN: new Error('something nobody classified'),
  };
  assert.equal(classifyFailure(required.SOURCE_VERSION_CHANGED).code, 'KNOWLEDGE_SOURCE_CHANGED');
  assert.equal(classifyFailure(required.LIBRARY).code, 'KNOWLEDGE_LIBRARY_NOT_SELECTED');
  assert.equal(classifyFailure(required.UNREACHABLE).code, 'KNOWLEDGE_SERVICE_UNREACHABLE');
  assert.equal(classifyFailure(required.TIMEOUT).code, 'KNOWLEDGE_SERVICE_TIMEOUT');
  assert.equal(classifyFailure(required.AUTH).code, 'KNOWLEDGE_SERVICE_UNAUTHORIZED');
  assert.equal(classifyFailure(required.QUOTA).code, 'KNOWLEDGE_QUOTA_EXHAUSTED');
  assert.equal(classifyFailure(required.CAPACITY).code, 'KNOWLEDGE_QUOTA_EXHAUSTED');
  assert.equal(classifyFailure(required.BREAKER).code, 'KNOWLEDGE_REPEAT_REFUSED');
  assert.equal(classifyFailure(required.TURN_LIMIT).code, 'KNOWLEDGE_TURN_LIMIT_REACHED');
  assert.equal(classifyFailure(required.UNKNOWN).code, 'KNOWLEDGE_UNKNOWN');
  for (const error of Object.values(required)) {
    assert.ok(FAILURE_CODES.includes(classifyFailure(error).code));
  }
});

test('the remaining transport shapes land on an actionable code with a typed detail', () => {
  const shapes = {
    FORBIDDEN: new KnowledgeServiceError(403, 'UNKNOWN_LIBRARY'),
    HANDLE: new KnowledgeServiceError(404, 'READ_HANDLE_INVALID'),
    FILE: new KnowledgeServiceError(404, 'READ_FILE_UNAVAILABLE'),
    FOREIGN: new KnowledgeServiceError(404, 'READ_HANDLE_FOREIGN'),
    SERVER: new KnowledgeServiceError(500, null),
    CATALOG: new KnowledgeServiceError(503, 'CATALOG_UNAVAILABLE'),
    BAD_REQUEST: new KnowledgeServiceError(400, 'READ_REQUEST_INVALID'),
  };
  assert.equal(classifyFailure(shapes.FORBIDDEN).code, 'KNOWLEDGE_REQUEST_REJECTED');
  // The three refusals a read can meet are deliberately three different answers. Folding them together - which
  // is what this plugin used to do - is how a mistyped identifier came to read as "the document is gone".
  assert.equal(classifyFailure(shapes.HANDLE).code, 'KNOWLEDGE_HANDLE_INVALID',
    'a signature refusal is about the identifier, not the file');
  assert.equal(classifyFailure(shapes.FILE).code, 'KNOWLEDGE_SOURCE_UNAVAILABLE',
    'a valid identifier whose file is unreadable is about the file');
  assert.equal(classifyFailure(shapes.FOREIGN).code, 'KNOWLEDGE_HANDLE_INVALID',
    'an identifier for another workspace or library is not this caller\'s');
  assert.equal(classifyFailure(shapes.SERVER).code, 'KNOWLEDGE_SERVICE_UNAVAILABLE');
  assert.equal(classifyFailure(shapes.CATALOG).code, 'KNOWLEDGE_SERVICE_UNAVAILABLE');
  // A 400 whose code names the read *shape* is its own answer now, not the generic rejection. This is the
  // case the observed session met: it was told "check the library id and the parameters", concluded the
  // reference must be stale, and abandoned a file it could have read.
  assert.equal(classifyFailure(shapes.BAD_REQUEST).code, 'KNOWLEDGE_READ_REQUEST_INVALID');
  // The status and the known code are the typed facts a card can render.
  assert.equal(classifyFailure(shapes.FORBIDDEN).detail.httpStatus, 403);
  assert.equal(classifyFailure(shapes.CATALOG).detail.serviceCode, 'CATALOG_UNAVAILABLE');
  assert.equal(classifyFailure(shapes.SERVER).detail.serviceCode, null);
  const abort = Object.assign(new Error('aborted'), { name: 'AbortError' });
  assert.equal(classifyFailure(abort).code, 'KNOWLEDGE_REQUEST_CANCELLED');
});

test('a detail field accepts a member of its set, a safe integer, or nothing', () => {
  const detail = failureDetail({
    layer: 'service', httpStatus: 409, serviceCode: 'SOURCE_VERSION_CHANGED',
    tool: 'knowledge_read', parameter: 'maxChars', chain: 'identical', occurrences: 4, used: 7, max: 6,
    evidence: 3, documents: 0, reads: 2,
  });
  assert.deepEqual(detail, {
    layer: 'service', httpStatus: 409, serviceCode: 'SOURCE_VERSION_CHANGED',
    tool: 'knowledge_read', parameter: 'maxChars', chain: 'identical', occurrences: 4, used: 7, max: 6,
    evidence: 3, documents: 0, reads: 2,
    reason: null, requestedStartLine: null, fileLines: null, suggestedAction: null, suggestedRef: null,
    query: null,
  });
  // Non-members and non-integers become null. This is the whole mechanism: no cleaning, no trimming.
  const refused = failureDetail({
    layer: 'made-up', httpStatus: '409', serviceCode: 'SK_LIVE_0123456789', tool: 'pwsh',
    parameter: 'maxChars=99999', chain: 'nonsense', occurrences: NaN, used: 1.5, max: Infinity,
    evidence: -1.5, documents: 'many', reads: {},
    reason: 'START_LINE_PAST_ENDISH', requestedStartLine: -3, fileLines: 'twelve', suggestedAction: 'RETRY_HARDER',
    suggestedRef: HOSTILE.signedHandle,
  });
  assert.deepEqual(refused, {
    layer: 'plugin', httpStatus: null, serviceCode: null, tool: null, parameter: null,
    chain: null, occurrences: null, used: null, max: null, evidence: null, documents: null, reads: null,
    reason: null, requestedStartLine: null, fileLines: null, suggestedAction: null, suggestedRef: null,
    query: null,
  });
  // A line number is a *non-negative* integer: `-3` is a valid integer and would otherwise render as if it
  // had described a file. Zero is kept, because a file with no lines really is a file with 0 lines.
  assert.equal(failureDetail({ requestedStartLine: 0 }).requestedStartLine, 0);
  assert.equal(failureDetail({ fileLines: 0 }).fileLines, 0);
  assert.equal(failureDetail({ requestedStartLine: -1 }).requestedStartLine, null);
  assert.equal(failureDetail({ requestedStartLine: true }).requestedStartLine, null);
  assert.equal(failureDetail({ requestedStartLine: '480' }).requestedStartLine, null);
});

test('a short key with separators cannot survive any field', () => {
  // The case that defeated the previous allow-list rule: every character is plausible in an identifier and
  // the segments are short, so no character-or-length filter could catch it. Enum membership does.
  for (const value of ALL_HOSTILE) {
    const detail = failureDetail({ serviceCode: value, tool: value, layer: value, chain: value });
    assert.deepEqual(detail, failureDetail({}), `"${value}" must not occupy any field`);
    assert.equal(knownServiceCode(value), null);
    assert.equal(knownTool(value), null);
  }
});

test('a hostile service detail cannot reach a record, a message or the fallback text', () => {
  // The service writes `CODE: prose`, and the prose is where a path, a handle or a body would appear. The
  // client parses the code, drops the body, and constructs the error from two machine facts - so the hostile
  // string does not exist in any object after the throw. This drives the same two facts a hostile response
  // would produce.
  const error = new KnowledgeServiceError(409, serviceCodeOf(
    'SOURCE_VERSION_CHANGED: ' + ALL_HOSTILE.join(' ')));
  assert.equal(error.serviceCode, 'SOURCE_VERSION_CHANGED', 'the code is recognised');
  assert.ok(!('serverDetail' in error), 'the error must not carry the service body at all');
  assert.ok(!('detail' in error), 'and not under any other name either');
  const thrown = failureFrom(error, (message, code) => Object.assign(new Error(message), { code }));
  assertNoLeak(renderOf(thrown.record), 'a classified service refusal');
  assert.match(renderOf(thrown.record), /原文已变更/);
  assert.equal(thrown.record.detail.httpStatus, 409);
  assert.equal(thrown.record.detail.serviceCode, 'SOURCE_VERSION_CHANGED');
});

test('an unknown upstream code is reported as unknown rather than echoed', () => {
  const code = serviceCodeOf('SOMETHING_ELSE: ' + HOSTILE.privatePath);
  assert.equal(code, null);
  const error = new KnowledgeServiceError(400, code);
  const thrown = failureFrom(error, (message, seed) => Object.assign(new Error(message), { code: seed }));
  assert.equal(thrown.code, 'KNOWLEDGE_REQUEST_REJECTED');
  assertNoLeak(renderOf(thrown.record), 'an unclassified service refusal');
});

test('thrown failures are built from the taxonomy, never from the caught message', () => {
  const caught = new Error('upstream said: ' + ALL_HOSTILE.join(' '));
  caught.stack = 'Error: ' + HOSTILE.signedHandle;
  const thrown = failureFrom(caught, (message, code) => Object.assign(new Error(message), { code }));
  assert.equal(thrown.code, 'KNOWLEDGE_UNKNOWN');
  assertNoLeak(renderOf(thrown.record), 'an unclassified thrown error');
  assert.match(thrown.message, /关联 ID/);
});

test('the tool result of a terminal stop carries only typed fields', () => {
  const record = failureRecord('KNOWLEDGE_TURN_LIMIT_REACHED', {
    detail: { layer: 'plugin', tool: 'knowledge_search', used: 7, max: 6 }, terminal: true,
  });
  assertNoLeak(renderOf(record), 'a terminal stop');
  assert.match(describeDetail(record.detail), /本轮已用 7 \/ 上限 6/);
  assert.match(describeDetail(record.detail), /工具 knowledge_search/);
  // A hostile value in the same slot is dropped rather than escaping.
  const hostile = failureRecord('KNOWLEDGE_TURN_LIMIT_REACHED', {
    detail: { layer: 'plugin', tool: HOSTILE.shortKeyWithSeparators, used: 7, max: 6 },
  });
  assertNoLeak(renderOf(hostile), 'a stop whose tool name was hostile');
  assert.equal(hostile.detail.tool, null);
});

test('cancellation keeps the host\'s own abort rather than becoming a knowledge failure', () => {
  const abort = Object.assign(new Error('The operation was aborted'), { name: 'AbortError' });
  const thrown = failureFrom(abort, () => { throw new Error('must not be called for an abort'); });
  assert.equal(thrown, abort, 'the original abort must be rethrown so the host owns the interruption');
});

test('a persisted record is re-validated before it is trusted', () => {
  const good = failureRecord('KNOWLEDGE_TURN_LIMIT_REACHED', {
    detail: { layer: 'plugin', tool: 'knowledge_search', used: 7, max: 6 }, terminal: true,
  });
  const round = readFailureRecord(JSON.parse(JSON.stringify(good)));
  assert.deepEqual(round, good, 'a well-formed record must survive a log round trip');

  assert.equal(readFailureRecord(null), null);
  assert.equal(readFailureRecord('nope'), null);
  assert.equal(readFailureRecord({}), null, 'a record without a code is not a record');
  assert.equal(readFailureRecord({ code: 'NOT_A_CODE' }), null, 'an unknown code is refused rather than shown');

  // A hand-edited log is the other place hostile text could enter, so every field is re-checked on read.
  const dirty = readFailureRecord({
    code: 'KNOWLEDGE_SOURCE_CHANGED',
    detail: { layer: HOSTILE.privatePath, serviceCode: HOSTILE.shortKeyWithSeparators, tool: HOSTILE.signedHandle,
      httpStatus: HOSTILE.url, used: HOSTILE.requestBody },
    reference: HOSTILE.bearer,
    message: HOSTILE.token,
    reason: 'RAW REASON FROM THE LOG',
    terminal: 'yes',
  });
  assert.equal(dirty.detail.layer, 'plugin', 'a non-member layer becomes the default');
  assert.equal(dirty.detail.serviceCode, null);
  assert.equal(dirty.detail.tool, null);
  assert.equal(dirty.detail.httpStatus, null);
  assert.equal(dirty.detail.used, null);
  assert.equal(dirty.reference, 'k-unknown', 'a non-conforming reference is replaced');
  assert.equal(dirty.terminal, false, 'only a real boolean counts');
  assert.equal(dirty.reason, FAILURE_ADVICE.KNOWLEDGE_SOURCE_CHANGED.reason, 'display text comes from the table');
  assert.equal(dirty.message, `${'KNOWLEDGE_SOURCE_CHANGED'}: ${dirty.reason} ${dirty.action}`,
    'the model-facing sentence is rebuilt here, not read back from the log');
  assertNoLeak(renderOf(dirty), 'a hand-edited log record');
});

test('a reference id is short and shape-checked', () => {
  const id = referenceId();
  assert.ok(isReferenceId(id), `generated id ${id} must satisfy its own shape`);
  assert.ok(id.length <= 10);
  const seen = new Set();
  for (let index = 0; index < 200; index += 1) seen.add(referenceId());
  assert.ok(seen.size > 190, 'reference ids must not repeat within a process');
  for (const value of ALL_HOSTILE) assert.equal(isReferenceId(value), false, 'a hostile reference is not an id');
  assert.equal(isReferenceId('k-ABC12345'), false, 'uppercase is not the shape');
  assert.equal(isReferenceId(undefined), false);
});

// ---------------------------------------------------------------------------------------------------
// The recoverable read refusal: the two codes that name a bound instead of a generic failure.

/** The structured half of a refused read, as the service sends it (`engine/service.py:read_refusal`). */
const refusalBody = (over = {}) => ({
  code: 'READ_RANGE_INVALID', reason: 'START_LINE_PAST_END', requestedStartLine: 480,
  fileLines: 12, suggestedAction: 'CORRECT_START_LINE', correlationId: 'k-0vnqzh02', ...over,
});

test('the plugin and the engine declare the same read reasons and suggested actions', async () => {
  // The two halves cannot import from each other (one is TypeScript, one is Python), so the enums are read
  // out of the engine's source and compared. A reason added on one side alone fails here rather than
  // silently becoming "an unlisted value the other half drops".
  const { readFileSync } = await import('node:fs');
  const { READ_REFUSAL_REASONS, READ_SUGGESTED_ACTIONS } = await import('../lib/failures.js');
  const source = readFileSync(new URL('../engine/reader.py', import.meta.url), 'utf8');
  const tuple = (name) => {
    const match = new RegExp(`${name} = \\(([^)]*)\\)`, 's').exec(source);
    assert.ok(match, `${name} must be declared in engine/reader.py`);
    return [...match[1].matchAll(/READ_(?:REASON|ACTION)_[A-Z_]+\b/g)].map(token => token[0]);
  };
  const names = (tokens) => tokens.map(token => {
    const match = new RegExp(`${token} = "([A-Z_]+)"`).exec(source);
    assert.ok(match, `${token} must have a literal value`);
    return match[1];
  });
  assert.deepEqual([...READ_REFUSAL_REASONS].sort(), names(tuple('READ_REASONS')).sort(),
    'the plugin must declare exactly the engine\'s reasons');
  assert.deepEqual([...READ_SUGGESTED_ACTIONS].sort(), names(tuple('READ_ACTIONS')).sort(),
    'the plugin must declare exactly the engine\'s actions');
  // The browser half carries a copy of the same two sets; it cannot import this module either.
  const bundle = readFileSync(new URL('../ui/source-card.js', import.meta.url), 'utf8');
  for (const member of [...READ_REFUSAL_REASONS, ...READ_SUGGESTED_ACTIONS]) {
    assert.ok(bundle.includes(`'${member}'`), `ui/source-card.js must declare ${member}`);
  }
});

test('a refusal body is reduced to declared members and numbers, and its id is adopted', () => {
  const error = new KnowledgeServiceError(400, 'READ_RANGE_INVALID', readRefusalOf(refusalBody()));
  const classified = classifyFailure(error);
  assert.equal(classified.code, 'KNOWLEDGE_READ_RANGE_INVALID');
  assert.equal(classified.detail.serviceCode, 'READ_RANGE_INVALID');
  assert.equal(classified.detail.reason, 'START_LINE_PAST_END');
  assert.equal(classified.detail.requestedStartLine, 480);
  assert.equal(classified.detail.fileLines, 12);
  assert.equal(classified.detail.suggestedAction, 'CORRECT_START_LINE');
  // The service's own id, not a second one minted here: the card, the model's sentence and the service log
  // line then all quote one string.
  assert.equal(classified.reference, 'k-0vnqzh02');

  const thrown = failureFrom(error, (message, code) => Object.assign(new Error(message), { code }));
  assert.equal(thrown.code, 'KNOWLEDGE_READ_RANGE_INVALID');
  assert.equal(thrown.record.reference, 'k-0vnqzh02');
  // The sentence names the *bound* the file imposes. The offending value (480) is deliberately absent: a
  // refusal must not become a channel that echoes model output back into the transcript.
  assert.match(thrown.message, /12 line/);
  assert.match(thrown.message, /at most 12/);
  assert.ok(!thrown.message.includes('480'), 'the value the model sent must not be echoed back');
  assert.match(describeDetail(thrown.record.detail), /原因 START_LINE_PAST_END/);
  assert.match(describeDetail(thrown.record.detail), /文件行数 12/);
  assert.match(describeDetail(thrown.record.detail), /建议动作 CORRECT_START_LINE/);
  assert.match(failureText(thrown.record), /读取范围超出文件/);
});

test('a read refusal that names no recoverable fact is still split out of the generic rejection', () => {
  // An older service sends only `detail: "CODE: sentence"`. The code is still recognised, so the model learns
  // *which* kind of refusal it met - but no number is invented to go with it.
  const error = new KnowledgeServiceError(400, 'READ_REQUEST_INVALID', null);
  const classified = classifyFailure(error);
  assert.equal(classified.code, 'KNOWLEDGE_READ_REQUEST_INVALID');
  assert.equal(classified.detail.reason, null);
  assert.equal(classified.detail.fileLines, null);
  assert.equal(classified.reference, undefined);
  const thrown = failureFrom(error, (message, code) => Object.assign(new Error(message), { code }));
  assert.ok(!/\d+ line/.test(thrown.message), 'no line count may be invented for a service that sent none');
  assert.match(thrown.message, /locate the document again/);
});

test('a hostile structured refusal cannot put text on a card', () => {
  // The `error` object is written by the service, and the service is a process on this machine - so it is
  // treated as untrusted input. Every field is fed a hostile value here; the record must come out with nulls.
  const hostile = {
    code: 'READ_RANGE_INVALID',
    reason: HOSTILE.privatePath,
    requestedStartLine: HOSTILE.signedHandle,
    fileLines: HOSTILE.url,
    suggestedAction: HOSTILE.requestBody,
    correlationId: HOSTILE.bearer,
    parameter: HOSTILE.shortKeyWithSeparators,
    note: HOSTILE.token,
  };
  const refusal = readRefusalOf(hostile);
  assert.equal(refusal, null, 'a body with no recognisable field is not a structured refusal');
  const error = new KnowledgeServiceError(400, 'READ_RANGE_INVALID', refusal);
  const thrown = failureFrom(error, (message, code) => Object.assign(new Error(message), { code }));
  assertNoLeak(renderOf(thrown.record), 'a hostile structured refusal');
  assert.equal(thrown.record.detail.reason, null);
  assert.equal(thrown.record.detail.fileLines, null);
  assert.equal(thrown.record.detail.requestedStartLine, null);

  // And a value that is *almost* right is still dropped: the sets are membership checks, not patterns. One
  // field is left valid on purpose, because a body with nothing recognisable in it is not a structured
  // refusal at all (asserted just above).
  const nearMiss = readRefusalOf(refusalBody({ reason: 'START_LINE_PAST_END_', suggestedAction: 'CORRECT_START_LINES',
    correlationId: 'k-ABC12345', requestedStartLine: 480.5, fileLines: -1, parameter: 'startLine' }));
  assert.ok(nearMiss, 'one declared member is enough for the body to be a structured refusal');
  assert.equal(nearMiss.reason, null);
  assert.equal(nearMiss.suggestedAction, null);
  assert.equal(nearMiss.correlationId, null, 'uppercase is not the reference shape');
  assert.equal(nearMiss.requestedStartLine, null, 'a non-integer line is not a line');
  assert.equal(nearMiss.fileLines, null, 'a negative line count is not a count');
  assert.equal(nearMiss.parameter, 'startLine', 'a declared parameter name is kept');
  // The plugin's own guarantee here is "no free text": a name is kept only if this build declares a parameter
  // by that name. Narrowing it further to the read route's fields is the *service's* job (it reports only a
  // name from that route's schema), and inventing a second, narrower list here would let the two halves
  // disagree about what a valid parameter is.
  assert.equal(readRefusalOf(refusalBody({ parameter: 'query' })).parameter, 'query');
  assert.equal(readRefusalOf(refusalBody({ parameter: HOSTILE.shortKeyWithSeparators })).parameter, null);
  assert.equal(readRefusalOf(refusalBody({ parameter: 'query=the whole request body' })).parameter, null);
});

test('a persisted read refusal is rebuilt from typed fields, not from the stored sentence', () => {
  const record = failureRecord('KNOWLEDGE_READ_RANGE_INVALID', {
    detail: { layer: 'service', httpStatus: 400, serviceCode: 'READ_RANGE_INVALID',
      reason: 'START_LINE_PAST_END', requestedStartLine: 480, fileLines: 12,
      suggestedAction: 'CORRECT_START_LINE' },
    reference: 'k-0vnqzh02',
  });
  const round = readFailureRecord(JSON.parse(JSON.stringify(record)));
  assert.deepEqual(round.detail, record.detail);
  assert.equal(round.reference, 'k-0vnqzh02');
  assert.equal(round.message, record.message, 'the model-facing sentence round-trips through the log');

  // A hand-edited log cannot move the numbers into prose: they survive only as members and integers.
  const dirty = readFailureRecord({
    code: 'KNOWLEDGE_READ_RANGE_INVALID',
    detail: { reason: HOSTILE.token, fileLines: HOSTILE.url, requestedStartLine: HOSTILE.bearer },
    message: HOSTILE.requestBody,
  });
  assert.equal(dirty.detail.reason, null);
  assert.equal(dirty.detail.fileLines, null);
  // With no recognisable fact left, the rebuilt sentence is the honest "named no fact" variant - not the
  // stored prose, which for a hand-edited log could be anything at all.
  assert.match(dirty.message, /named no fact this build can act on/);
  assert.ok(!dirty.message.includes(HOSTILE.requestBody), 'the stored sentence must not be read back');
  assertNoLeak(renderOf(dirty), 'a hand-edited read refusal');
});

test('a dispatched failure carries the turn accounting, so the model can see what is left', () => {
  const error = new KnowledgeServiceError(400, 'READ_RANGE_INVALID', readRefusalOf(refusalBody()));
  const thrown = failureFrom(error, (message, code) => Object.assign(new Error(message), { code }),
    { used: 5, max: 6, tool: 'knowledge_read' });
  assert.equal(thrown.record.detail.used, 5);
  assert.equal(thrown.record.detail.max, 6);
  assert.equal(thrown.record.detail.tool, 'knowledge_read');
  assert.match(describeDetail(thrown.record.detail), /本轮已用 5 \/ 上限 6/);
  // Absent context stays absent rather than becoming a zero.
  const bare = failureFrom(error, (message, code) => Object.assign(new Error(message), { code }));
  assert.equal(bare.record.detail.used, null);
  assert.equal(bare.record.detail.tool, null);
});


test('service refusals from another module realm retain only validated machine facts', () => {
  for (const [status,serviceCode,expected] of [[429,'QUERY_BUSY','KNOWLEDGE_QUERY_BUSY'],[429,'FUTURE_LIMIT','KNOWLEDGE_RATE_LIMITED'],[409,'QUERY_NOT_TERMINAL','KNOWLEDGE_QUERY_NOT_TERMINAL']]) {
    const foreign=Object.assign(new Error('sk-abcd-efgh-ijkl private upstream prose'),{name:'KnowledgeServiceError',status,serviceCode,refusal:null});
    assert.equal(foreign instanceof KnowledgeServiceError,false);
    const facts=classifyFailure(foreign);
    assert.equal(facts.code,expected);
    assert.equal(JSON.stringify(facts).includes('sk-abcd'),false);
  }
  assert.equal(classifyFailure({name:'KnowledgeServiceError',status:'429',serviceCode:'QUERY_BUSY'}).code,'KNOWLEDGE_UNKNOWN');
});

test('panel cancellation carries closed diagnostics while host interruption preserves the abort',()=>{
 const error=new DOMException('private input','AbortError');
 error.knowledgeLifecycle={requestId:'kq-abcdef0123456789',phase:'cancelled',jobState:'cancelled',cancellationConfirmed:true};
 assert.equal(failureFrom(error),error);
 const projected=failureFrom(error,undefined,{used:1,max:6,tool:'knowledge_search',callerInterrupted:false});
 assert.equal(projected.code,'KNOWLEDGE_REQUEST_CANCELLED');
 assert.ok(projected.message.includes('KNOWLEDGE_FAILURE_FACTS='));
 assert.ok(!projected.message.includes('private input'));
});
