// The per-turn tool-call ceiling: counting, atomicity, reset, isolation and boundedness.
//
// The user-visible promise is "at most N knowledge calls in a turn, and a refused call does no work". Almost
// every test here is about the parts that make that promise hard: what counts (everything), what does not
// reset it (success), what bounds it (a host turn boundary), and what happens when two calls race for the
// last slot.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  MAX_TURN_KNOWLEDGE_CALLS, TURN_SCAN_LIMIT, TurnLedger, resolveTurn,
} from '../lib/turnlimit.js';

/**
 * A session log stand-in with the two members the ledger reads: `seq` (the log length) and `eventAt`.
 *
 * It behaves like the host's `Session`: appending is the only way to grow it, and every appended
 * `turn/start` carries its own sequence number, which is what the turn key is derived from.
 */
function fakeSession() {
  const events = [];
  return {
    events,
    get seq() { return events.length; },
    eventAt(index) { return events[index]; },
    append(event) { events.push(event); return event; },
    /** Start a new turn the way the host does: a `turn/start` event with its own seq. */
    startTurn() { const at = events.length; events.push({ type: 'turn/start', seq: at }); return at; },
    /** A step inside the current turn, so calls are not always immediately after `turn/start`. */
    step() { const at = events.length; events.push({ type: 'step/start', seq: at }); return at; },
    assistant() { const at = events.length; events.push({ type: 'assistant/message', seq: at }); return at; },
  };
}

test('the ceiling is exactly the configured number of calls', () => {
  const ledger = new TurnLedger({ max: 6 });
  const session = fakeSession();
  session.startTurn();
  const results = [];
  for (let index = 0; index < 8; index += 1) results.push(ledger.reserve('s1', session));
  assert.deepEqual(results.slice(0, 6).map(result => result.allowed), [true, true, true, true, true, true]);
  assert.deepEqual(results.slice(6).map(result => result.allowed), [false, false]);
  // A refusal is still recorded, so a caller that ignores the stop does not get a fresh allowance.
  assert.equal(results[6].used, 7);
  assert.equal(results[7].used, 8);
  assert.equal(results[5].max, 6);
  assert.equal(MAX_TURN_KNOWLEDGE_CALLS, 6, 'the default the real window pins');
});

test('consecutive successes alone reach the ceiling: this limit is not a failure limit', () => {
  // The reason the two limits are separate. The failure governor clears on success; this must not, or a turn
  // that keeps succeeding would be unbounded.
  const ledger = new TurnLedger({ max: 6 });
  const session = fakeSession();
  session.startTurn();
  for (let index = 0; index < 6; index += 1) assert.equal(ledger.reserve('s1', session).allowed, true);
  assert.equal(ledger.reserve('s1', session).allowed, false);
});

test('successes and failures alternate and every call still counts', () => {
  const ledger = new TurnLedger({ max: 6 });
  const session = fakeSession();
  session.startTurn();
  // The ledger cannot see the outcome by design: the reservation happens before dispatch, so a call is
  // counted whether it succeeds, fails or is cancelled.
  const outcomes = ['ok', 'fail', 'ok', 'fail', 'ok', 'fail', 'ok'];
  const decisions = outcomes.map(() => ledger.reserve('s1', session).allowed);
  assert.deepEqual(decisions, [true, true, true, true, true, true, false]);
});

test('different tools and different arguments consume the same budget', () => {
  // The counter is per turn, not per tool or per argument shape: varying the call is not a way to get more.
  const ledger = new TurnLedger({ max: 6 });
  const session = fakeSession();
  session.startTurn();
  for (let index = 0; index < 6; index += 1) {
    session.step();
    assert.equal(ledger.reserve('s1', session).allowed, true, `call ${index + 1} should be allowed`);
  }
  // A different tool name, and the same tool with different arguments, are both refused.
  assert.equal(ledger.reserve('s1', session).allowed, false);
  assert.equal(ledger.reserve('s1', session).allowed, false);
});

test('concurrent calls cannot take the last slot twice', () => {
  // `reserve` is synchronous with no `await` between reading and writing the count, which is what makes the
  // reservation atomic. Parallel tool calls in one step all run before any of their bodies settle, so the
  // count must already have accounted for every one of them.
  const ledger = new TurnLedger({ max: 6 });
  const session = fakeSession();
  session.startTurn();
  const decisions = Array.from({ length: 12 }, () => ledger.reserve('s1', session));
  assert.equal(decisions.filter(decision => decision.allowed).length, 6, 'exactly the ceiling may pass');
  assert.deepEqual(decisions.map(decision => decision.used), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
  // The same, expressed the way the tool boundary uses it: reserve, then start the body. A Promise.all over
  // twelve dispatches must admit six.
  const ledger2 = new TurnLedger({ max: 6 });
  const session2 = fakeSession();
  session2.startTurn();
  const admitted = [];
  const dispatch = (index) => {
    const decision = ledger2.reserve('s2', session2);
    if (!decision.allowed) return Promise.resolve(`refused:${index}`);
    admitted.push(index);
    return new Promise(resolve => setImmediate(() => resolve(`done:${index}`)));
  };
  return Promise.all(Array.from({ length: 12 }, (_value, index) => dispatch(index))).then(results => {
    assert.equal(admitted.length, 6);
    assert.equal(results.filter(result => result.startsWith('done')).length, 6);
    assert.equal(results.filter(result => result.startsWith('refused')).length, 6);
  });
});

test('a cancelled call has already spent its slot', () => {
  const ledger = new TurnLedger({ max: 2 });
  const session = fakeSession();
  session.startTurn();
  assert.equal(ledger.reserve('s1', session).allowed, true);
  // The body would reject with an AbortError here; the ledger does not need to be told.
  assert.equal(ledger.reserve('s1', session).allowed, true);
  assert.equal(ledger.reserve('s1', session).allowed, false);
});

test('a new turn starts a fresh window, and the old one stays spent', () => {
  const ledger = new TurnLedger({ max: 6 });
  const session = fakeSession();
  const first = session.startTurn();
  for (let index = 0; index < 6; index += 1) ledger.reserve('s1', session);
  assert.equal(ledger.reserve('s1', session).allowed, false);
  assert.equal(ledger.usedFor(`s1#${first}`), 7);

  // The next turn's `turn/start` is a new key, so the ceiling applies again - and the previous turn's count
  // is not reused.
  const second = session.startTurn();
  assert.notEqual(second, first);
  assert.equal(ledger.reserve('s1', session).allowed, true);
  assert.equal(ledger.usedFor(`s1#${second}`), 1);
});

test('a turn is identified by its own durable event, not by anything the caller supplies', () => {
  const session = fakeSession();
  const memo = new Map();
  const first = session.startTurn();
  session.step();
  const during = resolveTurn('s1', session, memo);
  assert.equal(during.boundary, 'turn');
  assert.equal(during.key, `s1#${first}`);
  // The memo returns the same answer while the log is unchanged...
  assert.deepEqual(resolveTurn('s1', session, memo), during);
  // ...and a new `turn/start` changes it, without any caller input.
  const second = session.startTurn();
  const after = resolveTurn('s1', session, memo);
  assert.equal(after.key, `s1#${second}`);
  assert.notEqual(after.key, during.key);
});

test('sessions are counted independently', () => {
  const ledger = new TurnLedger({ max: 2 });
  const left = fakeSession();
  const right = fakeSession();
  left.startTurn();
  right.startTurn();
  assert.equal(ledger.reserve('a', left).allowed, true);
  assert.equal(ledger.reserve('a', left).allowed, true);
  assert.equal(ledger.reserve('a', left).allowed, false);
  // The other session is untouched by its neighbour's exhaustion.
  assert.equal(ledger.reserve('b', right).allowed, true);
  assert.equal(ledger.reserve('b', right).allowed, true);
  assert.equal(ledger.reserve('b', right).allowed, false);
});

test('a session with no reachable turn boundary is conservative, not unlimited', () => {
  // The honest fallback. With no `turn/start` to key on (an older host, a detached session, a structural
  // harness), the count does not reset per turn - so the ceiling becomes a per-session ceiling. That is
  // stricter than the intended behaviour, which is the only safe direction for a limit to fail.
  const ledger = new TurnLedger({ max: 3 });
  const noEvents = { seq: 0, eventAt: () => undefined };
  const first = ledger.reserve('s9', noEvents);
  assert.equal(first.identity.boundary, 'unobserved');
  assert.match(first.identity.key, /#unobserved$/);
  assert.equal(ledger.reserve('s9', noEvents).allowed, true);
  assert.equal(ledger.reserve('s9', noEvents).allowed, true);
  assert.equal(ledger.reserve('s9', noEvents).allowed, false, 'the fallback still refuses');

  // No session object at all is the same answer, not an exception.
  const none = ledger.reserve('s8', undefined);
  assert.equal(none.identity.boundary, 'unobserved');
  assert.equal(none.allowed, true);

  // A session whose log has events but no `turn/start` inside the scan bound is also unobserved.
  const stepsOnly = { seq: 5, eventAt: (index) => ({ type: 'step/start', seq: index }) };
  assert.equal(resolveTurn('s7', stepsOnly).boundary, 'unobserved');
});

test('the turn scan is bounded and a broken log is not fatal', () => {
  const deep = {
    seq: TURN_SCAN_LIMIT + 10,
    eventAt: (index) => ({ type: index < 10 ? 'turn/start' : 'assistant/message', seq: index }),
  };
  // The `turn/start` sits outside the bound, so the answer is the conservative one rather than a full scan.
  assert.equal(resolveTurn('deep', deep).boundary, 'unobserved');
  const throwing = { seq: 4, eventAt: () => { throw new Error('the log is gone'); } };
  assert.equal(resolveTurn('broken', throwing).boundary, 'unobserved');
  const odd = { seq: 4, eventAt: (index) => (index === 4 ? undefined : { type: 'step/start', seq: index }) };
  assert.equal(resolveTurn('odd', odd).boundary, 'unobserved');
});

test('state stays bounded and evicts the oldest turn first', () => {
  let clock = 0;
  const ledger = new TurnLedger({ max: 2, maxTurns: 3, now: () => (clock += 1) });
  const sessions = [fakeSession(), fakeSession(), fakeSession(), fakeSession()];
  for (const session of sessions) session.startTurn();
  for (const [index, session] of sessions.entries()) ledger.reserve(`s${index}`, session);
  assert.ok(ledger.size <= 3, `expected the turn map to stay bounded, got ${ledger.size}`);
  // The newest turn survived and still counts.
  assert.equal(ledger.usedFor('s3#' + sessions[3].events[0].seq), 1);
  assert.equal(ledger.reserve('s3', sessions[3]).allowed, true);
});
