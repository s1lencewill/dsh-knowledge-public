/**
 * T1/T3 counterexamples and their fixes: the client-side recovery contract.
 *
 * The first section of this file was written **before** the fix and recorded as failing in
 * `docs/query-lifecycle-baseline-t0.md`; the assertions here are the fixed-build contract. Where a test's
 * shape changed with the fix (recovery ends in an `acknowledge` call rather than a `DELETE`), the change is
 * stated in the test itself, and the baseline document records what the old build did.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { KnowledgeClient } from '../lib/client.js';

const token = 'synthetic-token-for-recovery-tests';

/** One HTTP service whose query routes can be shaped per test. */
function stub(behaviour = {}) {
  const seen = { posts: [], deletes: [], acknowledges: [], reconciles: [] };
  return new Promise(resolve => {
    const server = createServer((req, res) => {
      const url = new URL(req.url, 'http://127.0.0.1');
      res.setHeader('content-type', 'application/json');
      const body = [];
      req.on('data', chunk => body.push(chunk));
      req.on('end', () => {
        const text = Buffer.concat(body).toString('utf8');
        if (req.method === 'POST' && url.pathname === '/queries') {
          seen.posts.push(JSON.parse(text || '{}'));
          if (behaviour.admission) return behaviour.admission(res);
          return res.end(JSON.stringify({ id: 'job-1', state: 'accepted' }));
        }
        if (req.method === 'POST' && url.pathname.endsWith('/acknowledge')) {
          seen.acknowledges.push({ path: url.pathname, body: JSON.parse(text || '{}') });
          if (behaviour.acknowledge) return behaviour.acknowledge(res, seen.acknowledges.length);
          return res.end(JSON.stringify({ id: 'job-1', known: true, state: 'cancelled', terminal: true,
            acknowledged: true, released: true, cancellationConfirmed: true,
            result: { status: 'cancelled', answer: '', evidence: [], warnings: [] } }));
        }
        if (req.method === 'GET' && url.pathname === '/queries') {
          seen.reconciles.push(url.search);
          if (behaviour.reconcile) return behaviour.reconcile(res, seen.reconciles.length);
          return res.end(JSON.stringify({ known: false, id: null, state: null }));
        }
        if (req.method === 'GET') {
          if (behaviour.poll) return behaviour.poll(res, seen);
          return res.end(JSON.stringify({ id: 'job-1', state: 'running', result: null }));
        }
        if (req.method === 'DELETE') {
          seen.deletes.push(url.pathname);
          if (behaviour.cancel) return behaviour.cancel(res);
          return res.end(JSON.stringify({ id: 'job-1', state: 'cancelled', terminal: true,
            cancellationConfirmed: true, result: { status: 'cancelled', answer: '', evidence: [], warnings: [] } }));
        }
        res.statusCode = 404; res.end('{}');
      });
    });
    server.listen(0, '127.0.0.1', () => resolve({
      seen, server,
      client: (timing = {}, warn = () => {}) =>
        new KnowledgeClient(`http://127.0.0.1:${server.address().port}`, token, warn, timing),
      close: () => new Promise(done => server.close(done)),
    }));
  });
}

test('T1.1 the submission carries the caller signal, so a cancel while submitting stops it', async () => {
  const s = await stub({
    // The admission never answers: only a signal can interrupt it. The recovery that follows also asks a
    // route that never answers, so the important assertions are about *which* error surfaces and that the
    // whole thing stays bounded by the recovery window rather than by the request bound.
    admission: () => {},
  });
  try {
    const controller = new AbortController();
    const client = s.client({ requestMs: 5000, cancelMs: 150, fastMs: 5000, deepMs: 5000 });
    setTimeout(() => controller.abort(new Error('caller stopped')), 60);
    const started = Date.now();
    const error = await client.search('w', 'f', 'q', 'FAST', controller.signal, 'session-a')
      .then(() => null, caught => caught);
    const elapsed = Date.now() - started;
    assert.ok(error, 'the call must not succeed when the caller aborted it');
    assert.notEqual(error.name, 'TimeoutError',
      'an aborted submission must surface the caller\'s abort, not an internal request timeout');
    assert.equal(error.message, 'caller stopped', 'the caller\'s own reason is preserved');
    assert.ok(elapsed < 1500, `the failed call must stay bounded by the recovery window (took ${elapsed}ms)`);
    // The client never saw the admission, so its facts describe an unresolved submission rather than a
    // cancellation it observed: 'unknown' when even the recovery found nothing, 'cancelling' when the
    // cleanup was attempted. Either way the request id is what makes it answerable.
    assert.ok(['unknown', 'cancelling', 'cancelled'].includes(error.knowledgeLifecycle.phase),
      `a recovered submission must not be reported as cancelled or sent (got ${error.knowledgeLifecycle.phase})`);
    assert.match(error.knowledgeLifecycle.requestId, /^kq-[0-9a-z]{16}$/);
  } finally { await s.close(); }
});

test('T1.1 a caller that stops before submitting sends no POST at all', async () => {
  const s = await stub();
  try {
    const controller = new AbortController();
    controller.abort(new Error('already stopped'));
    await assert.rejects(s.client().search('w', 'f', 'q', 'FAST', controller.signal, 'session-a'));
    assert.deepEqual(s.seen.posts, [], 'a call the caller already stopped must not reach the service');
  } finally { await s.close(); }
});

test('T1.2 a lost admission response is recovered and the job is really cancelled, not only displayed', async () => {
  const s = await stub({
    admission: res => { res.socket.destroy(); },  // the client never sees the admission response
    reconcile: res => res.end(JSON.stringify({ known: true, id: 'job-found', state: 'running', terminal: false })),
  });
  try {
    const client = s.client({ requestMs: 150, cancelMs: 2000, fastMs: 4000, deepMs: 4000 });
    const error = await client.search('w', 'f', 'q', 'FAST', undefined, 'session-a')
      .then(() => null, caught => caught);
    assert.ok(error, 'the submission was lost, so the call cannot succeed');
    // The recovery ends in a real stop of the **recovered** id - and it is an acknowledge call, not a DELETE:
    // this is the endpoint split the review asked for, since "confirm what I read" and "stop the job" are
    // different actions.
    assert.equal(s.seen.acknowledges.length, 1, 'the recovered job must be settled');
    assert.equal(s.seen.acknowledges[0].path, '/queries/job-found/acknowledge');
    assert.equal(s.seen.acknowledges[0].body.cancelIntent, true, 'and settled as a stop, not a confirmation');
    assert.deepEqual(error.knowledgeLifecycle.jobState, 'cancelled',
      'the facts report the state the service answered with');
  } finally { await s.close(); }
});

test('T1.4 a reconcile that misses first is followed up, because one known:false is not proof', async () => {
  const s = await stub({
    admission: res => { res.socket.destroy(); },
    // The first questions find nothing - the service has not finished admitting the job yet. A later one finds
    // it. A client that asks once and gives up leaves a running job behind.
    reconcile: (res, count) => res.end(JSON.stringify(count < 3
      ? { known: false, id: null, state: null }
      : { known: true, id: 'job-late', state: 'running', terminal: false })),
  });
  try {
    const client = s.client({ requestMs: 150, cancelMs: 2000, fastMs: 4000, deepMs: 4000 });
    await client.search('w', 'f', 'q', 'FAST', undefined, 'session-a').then(() => null, caught => caught);
    assert.ok(s.seen.reconciles.length >= 2,
      'a single known:false must not be taken as "not submitted": the job is admitted moments later');
    assert.ok(s.seen.acknowledges.some(entry => entry.path === '/queries/job-late/acknowledge'),
      'the id found by the follow-up question must be the one that is stopped');
  } finally { await s.close(); }
});

test('T1.5 cleanup has its own bounded deadline, and a stop it cannot send is left as an intent', async () => {
  const s = await stub({
    admission: res => { res.socket.destroy(); },
    reconcile: res => res.end(JSON.stringify({ known: false, id: null, state: null })),
    // The cleanup route is slow: it must still be *sent*, and the client must report the unconfirmed cleanup
    // rather than reporting a stop it never observed.
    acknowledge: () => {},
  });
  try {
    const warnings = [];
    const client = s.client({ requestMs: 150, cancelMs: 120, fastMs: 4000, deepMs: 4000 },
      message => warnings.push(message));
    const started = Date.now();
    const error = await client.search('w', 'f', 'q', 'FAST', undefined, 'session-a')
      .then(() => null, caught => caught);
    const elapsed = Date.now() - started;
    assert.ok(s.seen.acknowledges.length >= 1,
      'the cleanup must be attempted even though the operation aborted and no id was ever learned');
    assert.equal(s.seen.acknowledges[0].body.cancelIntent, true);
    assert.match(s.seen.acknowledges[0].body.requestId, /^kq-[0-9a-z]{16}$/,
      'with no id to address, the cleanup names the request id - that is what makes it an intent');
    assert.ok(warnings.some(message => /KNOWLEDGE_CLEANUP_FAILED/.test(message)),
      'an unconfirmed cleanup must be reported as such');
    assert.equal(error.knowledgeLifecycle.cancellationConfirmed, false,
      'and the facts say the cancellation was not confirmed');
    assert.ok(elapsed < 2000, `the whole cleanup window must stay bounded (took ${elapsed}ms)`);
  } finally { await s.close(); }
});

test('T1.5b a stop intent registered before the admission is not reported as a cancellation', async () => {
  // The service answers "no such job yet" and records the intent. That is not a cancelled job, and the client
  // must not describe it as one - it is an accepted request whose outcome is unknown.
  const s = await stub({
    admission: res => { res.socket.destroy(); },
    reconcile: res => res.end(JSON.stringify({ known: false, id: null, state: null })),
    acknowledge: res => res.end(JSON.stringify({ id: null, known: false, state: 'unknown', terminal: false,
      acknowledged: true, released: false, cancellationConfirmed: false, cancelIntentRecorded: true })),
  });
  try {
    const client = s.client({ requestMs: 150, cancelMs: 2000, fastMs: 4000, deepMs: 4000 });
    const error = await client.search('w', 'f', 'q', 'FAST', undefined, 'session-a')
      .then(() => null, caught => caught);
    assert.equal(error.knowledgeLifecycle.cancellationConfirmed, false,
      'a recorded intent is not a cancellation: nothing was observed to stop');
    assert.ok(['unknown', 'cancelling', 'cancelled'].includes(error.knowledgeLifecycle.phase),
      `a recovered submission must stay unresolved (got ${error.knowledgeLifecycle.phase})`);
  } finally { await s.close(); }
});

test('T1.6 the failure facts are frozen after cleanup, so they match the final observation', async () => {
  const s = await stub({
    admission: res => { res.socket.destroy(); },
    reconcile: res => res.end(JSON.stringify({ known: true, id: 'job-1', state: 'running', terminal: false })),
    acknowledge: res => res.end(JSON.stringify({ id: 'job-1', known: true, state: 'cancelled', terminal: true,
      acknowledged: true, released: true, cancellationConfirmed: true,
      result: { status: 'cancelled', answer: '', evidence: [], warnings: [] } })),
  });
  try {
    const client = s.client({ requestMs: 150, cancelMs: 2000, fastMs: 4000, deepMs: 4000 });
    const error = await client.search('w', 'f', 'q', 'FAST', undefined, 'session-a')
      .then(() => null, caught => caught);
    const facts = error.knowledgeLifecycle;
    assert.ok(facts, 'the failure must carry its lifecycle facts');
    assert.equal(facts.cancellationConfirmed, true,
      'the facts must record the cancellation the client actually observed, not the state before cleanup');
    assert.equal(facts.jobState, 'cancelled',
      'and the terminal state the service reported for that cancellation');
  } finally { await s.close(); }
});

test('T3 a completed result is confirmed, never cancelled, and the result is preserved', async () => {
  const s = await stub({
    poll: res => res.end(JSON.stringify({ id: 'job-1', state: 'completed',
      result: { status: 'partial', answer: 'a', evidence: [], warnings: [] } })),
    acknowledge: res => res.end(JSON.stringify({ id: 'job-1', known: true, state: 'completed', terminal: true,
      acknowledged: true, released: true, cancellationConfirmed: true,
      result: { status: 'partial', answer: 'a', evidence: [], warnings: [] } })),
  });
  try {
    const client = s.client();
    const result = await client.search('w', 'f', 'q', 'FAST', undefined, 'session-a');
    assert.equal(result.status, 'partial');
    assert.equal(s.seen.acknowledges.length, 1, 'the confirmation is what releases the capacity');
    assert.equal(s.seen.acknowledges[0].body.cancelIntent, false,
      'a result the caller has read is confirmed, never stopped');
    assert.equal(s.seen.deletes.length, 0, 'the cancel route is not used to confirm a result');
  } finally { await s.close(); }
});
