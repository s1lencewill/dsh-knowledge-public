/**
 * The third budget: main-Agent model requests, counted and refused **before** dispatch.
 *
 * The property under test is not "a number goes up". It is that a request over the ceiling never reaches
 * the adapter (`next()` is never called), that the count is a floor rather than an optimistic best case
 * (refused, failed and cancelled attempts all stay counted), and that an outcome nobody observed is
 * reported as `unknown` instead of being folded into a zero.
 */
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';

/**
 * The two keys that arm a window, in one place so "armed" is always deliberate in these tests.
 *
 * Arming takes both on purpose: the ceiling's scope is the whole host process, so a single inherited
 * variable must not be able to open a window that refuses model requests in unrelated conversations.
 */
const armed = (extra = {}) => ({ [ARM_AGENT_QUOTA_ENV]: '1', ...extra });

import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

import {
  AGENT_QUOTA_FILE_ENV, ARM_AGENT_QUOTA_ENV, BUDGET_EXHAUSTED, MAX_AGENT_REQUESTS_ENV, MAX_TRACKED_OUTCOMES,
  ModelRequestBudget, classifyFinish,
} from '../lib/modellimit.js';

/** A stream that yields the given chunks then ends, like an adapter's normalized stream does. */
async function* streamOf(...chunks) {
  for (const chunk of chunks) yield chunk;
}

function finish(reason) {
  return { type: 'finish', reason };
}

async function drain(iterable) {
  const seen = [];
  for await (const chunk of iterable) seen.push(chunk);
  return seen;
}

test('a request over the ceiling is refused without ever reaching the adapter', async () => {
  const budget = new ModelRequestBudget(2);
  let calls = 0;
  const next = () => { calls += 1; return streamOf(finish('stop')); };

  await drain(budget.guard({ messages: [] }, next));
  await drain(budget.guard({ messages: [] }, next));
  assert.equal(calls, 2, 'two permitted requests reached the adapter');

  assert.throws(() => budget.guard({ messages: [] }, next), (error) => {
    assert.match(error.message, new RegExp(BUDGET_EXHAUSTED));
    assert.match(error.message, /not sent/);
    return true;
  });
  assert.equal(calls, 2, 'the refused request must not have called next()');

  const report = budget.report();
  assert.equal(report.attempted, 3, 'the refused attempt is still counted');
  assert.equal(report.dispatched, 2);
  assert.equal(report.refused, 1);
  assert.equal(report.remaining, 0);
});

test('the count is never rolled back: failed, aborted and cancelled attempts stay spent', async () => {
  const budget = new ModelRequestBudget(3);
  await drain(budget.guard({}, () => streamOf(finish('stop'))));
  await drain(budget.guard({}, () => streamOf(finish('error'))));
  await assert.rejects(async () => {
    await drain(budget.guard({}, () => (async function* () { throw new Error('provider exploded'); })()));
  }, /provider exploded/);

  const report = budget.report();
  assert.equal(report.attempted, 3);
  assert.equal(report.dispatched, 3, 'a failed request was still dispatched, so it still spent the call');
  assert.equal(report.completed, 1);
  assert.equal(report.failed, 2, 'the thrown stream and the error finish both count as failures');
  assert.equal(report.remaining, 0);
});

test('an aborted attempt is counted, and an unobserved outcome is unknown rather than zero', async () => {
  const budget = new ModelRequestBudget(6);
  await drain(budget.guard({}, () => streamOf(finish('aborted'))));
  // A stream that ends with no terminal frame at all: the request started and nobody said how it ended.
  await drain(budget.guard({}, () => streamOf({ type: 'text-delta', index: 0, text: 'partial' })));

  const report = budget.report();
  assert.equal(report.aborted, 1);
  assert.equal(report.usageUnknown, 1, 'an unfinished request is not a completed one');
  assert.equal(report.completed, 0);
  assert.equal(report.dispatched, 2, 'both were dispatched and both spent the window');
  assert.match(report.countedAs, /usageUnknown/);
  // The scope is a field, not a sentence: the hook's payload has no session identity, so these numbers belong
  // to the host process and a reader (or a card) must be able to say so without parsing `countedAs`.
  assert.equal(report.scope, 'host-process');
});

test('a terminal finish is classified tolerantly and a missing one is never a success', () => {
  assert.equal(classifyFinish('stop'), 'completed');
  assert.equal(classifyFinish('length'), 'completed');
  assert.equal(classifyFinish('error'), 'failed');
  assert.equal(classifyFinish('provider_failure'), 'failed');
  assert.equal(classifyFinish('aborted'), 'aborted');
  assert.equal(classifyFinish('cancelled'), 'aborted');
  assert.equal(classifyFinish(undefined), 'unknown');
  assert.equal(classifyFinish(''), 'unknown');
});

test('no ceiling is configured by default, and only digits configure one', () => {
  assert.equal(ModelRequestBudget.fromEnvironment({}).maxRequests, 0);
  assert.equal(ModelRequestBudget.fromEnvironment({ [MAX_AGENT_REQUESTS_ENV]: '' }).maxRequests, 0);
  assert.equal(ModelRequestBudget.fromEnvironment({ [MAX_AGENT_REQUESTS_ENV]: '0' }).maxRequests, 0);
  assert.equal(ModelRequestBudget.fromEnvironment(armed({ [MAX_AGENT_REQUESTS_ENV]: '12' })).maxRequests, 12);
  for (const bad of ['abc', '3.5', '-1', '1e3', '  ', '0x10']) {
    assert.equal(ModelRequestBudget.fromEnvironment(armed({ [MAX_AGENT_REQUESTS_ENV]: bad })).maxRequests, 0, bad);
  }
});

test('with no ceiling nothing is refused, and the report says so instead of implying a bound', async () => {
  const budget = ModelRequestBudget.fromEnvironment({});
  for (let index = 0; index < 25; index += 1) {
    await drain(budget.guard({}, () => streamOf(finish('stop'))));
  }
  const report = budget.report();
  assert.equal(report.enforced, false);
  assert.equal(report.refused, 0);
  assert.equal(report.attempted, 25);
  assert.equal(report.max, 0);
  assert.equal(report.requestChars, null, 'sizes are not measured when no window asked for them');
});

test('the request size is measured only when a ceiling is enforced', async () => {
  const measured = new ModelRequestBudget(2);
  await drain(measured.guard({ messages: [{ role: 'user', content: 'abcd' }] }, () => streamOf(finish('stop'))));
  const report = measured.report();
  assert.equal(typeof report.requestChars, 'number');
  assert.ok(report.requestChars > 10, 'the request image was measured');

  // An unmeasurable request image is a missing number, not a refusal.
  const cyclic = {};
  cyclic.self = cyclic;
  const budget = new ModelRequestBudget(2);
  await drain(budget.guard(cyclic, () => streamOf(finish('stop'))));
  assert.equal(budget.report().dispatched, 1);
  assert.equal(budget.report().requestChars, 0, 'the one unmeasurable request contributed nothing');
});

test('the outcome list is bounded, so a long plugin life cannot grow it without limit', async () => {
  const budget = new ModelRequestBudget(0, false);
  for (let index = 0; index < MAX_TRACKED_OUTCOMES + 50; index += 1) {
    await drain(budget.guard({}, () => streamOf(finish('stop'))));
  }
  const report = budget.report();
  assert.equal(report.attempted, MAX_TRACKED_OUTCOMES + 50, 'attempts are counted, not stored');
  assert.ok(report.completed <= MAX_TRACKED_OUTCOMES);
});

test('the ceiling counts model requests, not knowledge calls: one call may spend several', async () => {
  // The distinction the whole module exists for. The engine issues more provider requests than the Agent
  // makes tool calls, so the model-request count is not the tool-call count and must not be derived from it.
  const budget = new ModelRequestBudget(3);
  for (let index = 0; index < 3; index += 1) await drain(budget.guard({}, () => streamOf(finish('stop'))));
  assert.throws(() => budget.guard({}, () => streamOf(finish('stop'))));
  const report = budget.report();
  assert.equal(report.attempted, 4);
  assert.equal(report.max, 3);
  assert.match(report.countedAs, /retry re-enters this hook/);
});

test('parallel dispatches cannot take the last slot twice', async () => {
  // The reservation is synchronous and contains no await, so two requests racing at the ceiling cannot both
  // pass: one of them is refused before either reaches the adapter.
  const budget = new ModelRequestBudget(1);
  const streams = [
    budget.guard({}, () => streamOf(finish('stop'))),
    (() => { try { return budget.guard({}, () => streamOf(finish('stop'))); } catch (error) { return error; } })(),
  ];
  await drain(streams[0]);
  assert.ok(streams[1] instanceof Error, 'the second racing request was refused');
  assert.equal(budget.report().dispatched, 1);
  assert.equal(budget.report().refused, 1);
});

test('a retried request is a second attempt, not a continuation of the counted one', async () => {
  // The bypass this guards against: the host retries by re-running the failed step, which is a *fresh*
  // dispatch. If a retry were free, a window of 3 could spend far more than 3 provider requests.
  const budget = new ModelRequestBudget(3);
  let dispatches = 0;
  const attempt = () => {
    dispatches += 1;
    // First attempt fails, its retry succeeds: two provider requests, one logical call.
    return streamOf(finish(dispatches === 1 ? 'error' : 'stop'));
  };
  await drain(budget.guard({}, attempt));   // attempt 1, fails
  await drain(budget.guard({}, attempt));   // attempt 2, the retry, succeeds
  assert.equal(budget.report().attempted, 2, 'the retry spent a second slot');
  assert.equal(budget.report().failed, 1);
  assert.equal(budget.report().completed, 1);
  // A third and fourth dispatch reach the ceiling, and the fifth is refused before dispatch.
  await drain(budget.guard({}, attempt));
  assert.throws(() => budget.guard({}, attempt));
  assert.equal(dispatches, 3, 'the refused attempt never reached the adapter');
});

test('the refusal carries a dedicated code that no retry policy lists', () => {
  const budget = new ModelRequestBudget(0 + 1);
  budget.reserve({});
  const refusal = (() => { try { budget.guard({}, () => streamOf(finish('stop'))); } catch (error) { return error; } })();
  assert.equal(refusal.code, BUDGET_EXHAUSTED, 'a typed code, so a retry policy can exclude it by name');
  assert.match(refusal.message, /cancelled and retried alike/);
});

test('a ledger file keeps the window across a reload, and the file own ceiling wins', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'modellimit-'));
  try {
    const ledger = join(directory, 'window.json');
    const environment = armed({ [AGENT_QUOTA_FILE_ENV]: ledger, [MAX_AGENT_REQUESTS_ENV]: '2' });
    const first = ModelRequestBudget.fromEnvironment(environment);
    assert.equal(first.maxRequests, 2);
    await drain(first.guard({}, () => streamOf(finish('stop'))));
    assert.deepEqual(JSON.parse(readFileSync(ledger, 'utf8')).used, 1, 'the spend is on disk, not in memory');

    // A plugin reload builds a new budget over the same file: the window continues instead of resetting.
    const second = ModelRequestBudget.fromEnvironment(environment);
    assert.equal(second.report().attempted, 1, 'the window resumed where it was');
    assert.equal(second.report().persisted, true);
    await drain(second.guard({}, () => streamOf(finish('stop'))));
    assert.throws(() => second.guard({}, () => streamOf(finish('stop'))), /window allows 2/);
    assert.equal(JSON.parse(readFileSync(ledger, 'utf8')).used, 2, 'a refused request spends nothing');

    // The number lives with the counter: raising it in the file takes effect without restarting anything.
    writeFileSync(ledger, JSON.stringify({ used: 2, max: 4 }), 'utf8');
    const third = ModelRequestBudget.fromEnvironment(environment);
    assert.equal(third.maxRequests, 4, "the file's ceiling wins over the environment's");
    await drain(third.guard({}, () => streamOf(finish('stop'))));
    assert.equal(JSON.parse(readFileSync(ledger, 'utf8')).used, 3);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('an unusable ledger refuses rather than quietly starting a fresh window', () => {
  const directory = mkdtempSync(join(tmpdir(), 'modellimit-bad-'));
  try {
    const ledger = join(directory, 'window.json');
    writeFileSync(ledger, '{not json', 'utf8');
    const budget = ModelRequestBudget.fromEnvironment(armed({ [AGENT_QUOTA_FILE_ENV]: ledger }));
    assert.throws(() => budget.guard({}, () => streamOf(finish('stop'))), (error) => {
      assert.equal(error.code, BUDGET_EXHAUSTED);
      assert.match(error.message, /ledger is unusable: not valid JSON/);
      return true;
    });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('a window ledger that exists without a usable ceiling is refused, not read as zero', () => {
  // The other half of the same defect: a file that is present must state what the ceiling is. Falling
  // back to a number from somewhere else is what made a typo indistinguishable from a spent window.
  const directory = mkdtempSync(join(tmpdir(), 'modellimit-ceiling-'));
  try {
    for (const payload of [{ used: 0 }, { used: 0, max: 0 }, { used: 0, max: -1 }, { used: 0, max: '30' },
                           { used: 0, max: null }]) {
      const ledger = join(directory, `w-${JSON.stringify(payload).replace(/\W/g, '')}.json`);
      writeFileSync(ledger, JSON.stringify(payload), 'utf8');
      const budget = ModelRequestBudget.fromEnvironment(armed({ [AGENT_QUOTA_FILE_ENV]: ledger }));
      assert.equal(budget.maxRequests, 0, JSON.stringify(payload));
      assert.throws(() => budget.guard({}, () => streamOf(finish('stop'))), (error) => {
        assert.equal(error.code, BUDGET_EXHAUSTED);
        assert.match(error.message, /ledger is unusable: its "max" is/);
        return true;
      }, JSON.stringify(payload));
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('a configured ledger with no file is NOT a ceiling of zero', async () => {
  // THE regression test for the outage: `agentQuotaFile` configured, no env ceiling, no file yet. This
  // combination refused every main-Agent request with "the window allows 0 model requests", because the
  // missing file's ceiling fell back to the configured number, which was 0. No file means *not opened*.
  const directory = mkdtempSync(join(tmpdir(), 'modellimit-unopened-'));
  try {
    const ledger = join(directory, 'never-written.json');
    const budget = ModelRequestBudget.fromEnvironment(armed({ [AGENT_QUOTA_FILE_ENV]: ledger }));
    const report = budget.report();
    assert.equal(report.persisted, true, 'the path is configured');
    assert.equal(report.source, 'none', 'but nothing states a ceiling');
    assert.equal(report.enforced, false);
    assert.equal(report.max, 0);
    for (let index = 0; index < 5; index += 1) {
      await drain(budget.guard({}, () => streamOf(finish('stop'))));
    }
    assert.equal(budget.report().refused, 0, 'an unopened window must never refuse');
    assert.equal(budget.report().dispatched, 5);
    assert.equal(existsSync(ledger), false, 'and it must not write a ledger nobody opened');
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('the ledger ceiling wins over the configured one, and deleting the file disarms it', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'modellimit-priority-'));
  try {
    const ledger = join(directory, 'window.json');
    const environment = armed({ [AGENT_QUOTA_FILE_ENV]: ledger, [MAX_AGENT_REQUESTS_ENV]: '9' });
    writeFileSync(ledger, JSON.stringify({ used: 0, max: 2 }), 'utf8');
    const budget = ModelRequestBudget.fromEnvironment(environment);
    assert.equal(budget.maxRequests, 2, "the file's ceiling wins");
    assert.equal(budget.report().source, 'ledger');
    await drain(budget.guard({}, () => streamOf(finish('stop'))));
    await drain(budget.guard({}, () => streamOf(finish('stop'))));
    assert.throws(() => budget.guard({}, () => streamOf(finish('stop'))), /window allows 2/);

    // Disarm: with the file gone the configured 9 is what is in force, and the window is not reset to 0.
    rmSync(ledger);
    const reopened = ModelRequestBudget.fromEnvironment(environment);
    assert.equal(reopened.maxRequests, 9, 'the configured ceiling is the fallback, not zero');
    assert.equal(reopened.report().source, 'configured');
    await drain(reopened.guard({}, () => streamOf(finish('stop'))));
    assert.equal(JSON.parse(readFileSync(ledger, 'utf8')).used, 1, 'a configured ceiling is persisted again');
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('the same refusal can be delivered through the stream instead of by throwing', async () => {
  const budget = new ModelRequestBudget(1);
  await drain(budget.guardedStream({}, () => streamOf(finish('stop'))));
  await assert.rejects(async () => drain(budget.guardedStream({}, () => streamOf(finish('stop')))),
    (error) => error.code === BUDGET_EXHAUSTED);
});

test('one key is not enough: a ledger path alone never arms a window', async () => {
  // The blast radius of arming is every session the host serves, so arming takes two deliberate keys. This is
  // the failure mode it prevents: a variable exported for one afternoon, inherited by a host that serves
  // unrelated conversations, silently imposing a spent window on all of them.
  const directory = mkdtempSync(join(tmpdir(), 'modellimit-onekey-'));
  try {
    const ledger = join(directory, 'window.json');
    writeFileSync(ledger, JSON.stringify({ used: 9, max: 9 }), 'utf8');

    const byPath = ModelRequestBudget.fromEnvironment({ [AGENT_QUOTA_FILE_ENV]: ledger });
    assert.equal(byPath.report().persisted, false, 'without the arming key the path is ignored entirely');
    assert.equal(byPath.report().source, 'none');
    assert.equal(byPath.report().enforced, false);
    await drain(byPath.guard({}, () => streamOf(finish('stop'))));
    assert.equal(byPath.report().refused, 0, 'a spent ledger must not refuse anything');
    assert.equal(JSON.parse(readFileSync(ledger, 'utf8')).used, 9, 'and it must not be written either');

    const byNumber = ModelRequestBudget.fromEnvironment({ [MAX_AGENT_REQUESTS_ENV]: '3' });
    assert.equal(byNumber.maxRequests, 0, 'the number form is inert on its own too');
    assert.equal(byNumber.report().enforced, false);

    // Anything other than '1' is not an arming signal. A trailing space is tolerated: trimming an
    // environment variable is friendlier than refusing, and the key's job is to be deliberate, not exact.
    for (const value of ['', '0', 'true', 'yes', '2']) {
      const budget = ModelRequestBudget.fromEnvironment({
        [ARM_AGENT_QUOTA_ENV]: value, [AGENT_QUOTA_FILE_ENV]: ledger,
      });
      assert.equal(budget.report().enforced, false, JSON.stringify(value));
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
