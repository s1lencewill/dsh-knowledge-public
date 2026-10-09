import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { Readable } from 'node:stream';
import { KnowledgeClient } from '../lib/client.js';
import { ActiveQueries } from '../lib/operations.js';
import { registerPanelBridge, PanelSettings } from '../lib/panel.js';
import { classifyFailure, failureDetail, failureRecord, FAILURE_CODES, KnowledgeServiceError } from '../lib/failures.js';

const token = 'synthetic-token-for-lifecycle-tests';

/** A service stub that answers the query routes as this build's service does. */
function serviceStub(behaviour = {}) {
  const seen = { posts: [], gets: [], deletes: [], reconciles: [], acknowledges: [] };
  return new Promise(resolve => {
    const server = createServer((req, res) => {
      res.setHeader('content-type', 'application/json');
      const url = new URL(req.url, 'http://127.0.0.1');
      // The body is read once, before any route decision: a handler that needs it must not be the second
      // reader of the same stream.
      const chunks = [];
      req.on('data', chunk => chunks.push(chunk));
      req.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        if (req.method === 'POST' && url.pathname === '/queries') {
          const parsed = JSON.parse(text || '{}');
          seen.posts.push(parsed);
          if (behaviour.admission) return behaviour.admission(res, parsed);
          return res.end(JSON.stringify({ id: 'job-1', requestId: parsed.requestId, state: 'accepted' }));
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
          return res.end(JSON.stringify(behaviour.defaultReconcile ?? { known: false, id: null, state: null }));
        }
        if (req.method === 'GET') {
          seen.gets.push(url.pathname + url.search);
          if (behaviour.poll) return behaviour.poll(res, seen.gets.length);
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
      server, seen, client: new KnowledgeClient(`http://127.0.0.1:${server.address().port}`, token,
        () => {}, behaviour.timing ?? {}),
      close: () => new Promise(done => server.close(done)),
    }));
  });
}

test('the request id exists before the submission is sent, so a lost response is still addressable', async () => {
  const stub = await serviceStub({
    poll: res => res.end(JSON.stringify({ id: 'job-1', state: 'completed',
      result: { status: 'partial', answer: '', evidence: [], warnings: [] } })),
  });
  try {
    const result = await stub.client.search('w', 'f', 'q', 'FAST', undefined, 'session-a');
    const [admitted] = stub.seen.posts;
    assert.match(admitted.requestId, /^kq-[0-9a-z]{16}$/,
      'the client chose the id before sending it, which is the whole of its recoverability');
    assert.equal(result.lifecycle.requestId, admitted.requestId,
      'the lifecycle reports the same id the submission was made under');
    assert.equal(admitted.session, 'session-a', 'the owner scope travels with the submission');
    assert.equal(admitted.mode, 'FAST');
    // The poll carries the same owner scope, so a job can only be polled from the scope that submitted it.
    assert.match(stub.seen.gets[0], /ownerWorkspace=w/);
    assert.match(stub.seen.gets[0], /ownerSession=session-a/);
  } finally { await stub.close(); }
});

test('a lost admission response is reconciled by request id and reported as unknown rather than as nothing', async () => {
  // The admission hangs past the client's own request bound: the client never learns the job id, which is
  // exactly the shape of the recorded failure.
  const stub = await serviceStub({
    timing: { requestMs: 120, cancelMs: 2000, fastMs: 5000, deepMs: 5000 },
    admission: () => { /* never answers */ },
    defaultReconcile: { known: true, id: 'job-lost', state: 'running', terminal: false },
  });
  try {
    await assert.rejects(stub.client.search('w', 'f', 'q', 'FAST', undefined, 'session-a'),
      error => error.name === 'TimeoutError');
    assert.equal(stub.seen.reconciles.length, 1, 'the client asks the service what it knows about its own id');
    assert.match(stub.seen.reconciles[0], /requestId=kq-/);
    // Nothing was DELETEd: the client has no id to address, and inventing one would cancel a stranger's job.
    assert.deepEqual(stub.seen.deletes, []);
  } finally { await stub.close(); }
});

test('a submission that timed out is classified with its phase, request id and job state', async () => {
  const stub = await serviceStub({
    timing: { requestMs: 120, cancelMs: 2000, fastMs: 5000, deepMs: 5000 },
    admission: () => { /* never answers */ },
    defaultReconcile: { known: true, id: 'job-lost', state: 'running', terminal: false },
  });
  try {
    const error = await stub.client.search('w', 'f', 'q', 'FAST', undefined, 'session-a')
      .then(() => null, caught => caught);
    const classified = classifyFailure(error);
    assert.equal(classified.code, 'KNOWLEDGE_SERVICE_TIMEOUT');
    // The recovery found the job, so the client's phase is the cleanup it then performed - not "unknown",
    // and never "cancelled": nothing has confirmed that yet.
    assert.equal(classified.detail.query.phase, 'cancelled');
    assert.equal(classified.detail.query.jobState, 'cancelled',
      'the state the service reported for the job is carried as a fact, not as prose');
    assert.match(classified.detail.query.requestId, /^kq-[0-9a-z]{16}$/);
    // The model-facing sentence states the uncertainty and refuses the two wrong recoveries. `classifyFailure`
    // names the code and the facts; the sentence is assembled from them by the record that carries the failure.
    const sentence = failureRecord(classified.code, { detail: classified.detail }).message;
    assert.match(sentence, /cancelled/, 'the sentence carries the state the service reported');
    assert.match(sentence, /cancellation/i);
  } finally { await stub.close(); }
});

test('a cleanup that the service refuses is reported as unconfirmed and never as a stop', async () => {
  const warnings = [];
  const server = createServer((req, res) => {
    res.setHeader('content-type', 'application/json');
    if (req.method === 'POST') return res.end(JSON.stringify({ id: 'job-1', state: 'accepted' }));
    if (req.method === 'DELETE') { res.statusCode = 500; return res.end('{}'); }
    res.end(JSON.stringify({ id: 'job-1', state: 'running', result: null }));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const client = new KnowledgeClient(`http://127.0.0.1:${server.address().port}`, token,
      message => warnings.push(message), { requestMs: 150, fastMs: 300, deepMs: 300 });
    await assert.rejects(client.search('w', 'f', 'q', 'FAST'));
    assert.equal(warnings.length, 1);
    assert.match(warnings[0], /KNOWLEDGE_CLEANUP_FAILED/);
    assert.ok(!warnings[0].includes(token));
  } finally { await new Promise(resolve => server.close(resolve)); }
});

test('the cleanup request has its own bound, so an aborted caller cannot defeat it', async () => {
  let acknowledges = 0;
  const server = createServer((req, res) => {
    res.setHeader('content-type', 'application/json');
    if (req.method === 'POST' && req.url.includes('/acknowledge')) {
      acknowledges++;
      return res.end(JSON.stringify({ cancellationConfirmed: true, state: 'cancelled', settled: true }));
    }
    if (req.method === 'POST') return res.end(JSON.stringify({ id: 'job-1', state: 'accepted' }));
    res.end(JSON.stringify({ id: 'job-1', state: 'running', result: null }));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const controller = new AbortController();
    const client = new KnowledgeClient(`http://127.0.0.1:${server.address().port}`, token, () => {},
      { requestMs: 40, cancelMs: 3000, fastMs: 5000, deepMs: 5000 });
    setTimeout(() => controller.abort(new Error('interrupted')), 10);
    await assert.rejects(client.search('w', 'f', 'q', 'FAST', controller.signal));
    assert.equal(acknowledges, 1, 'the cleanup went out even though the caller signal was already aborted');
  } finally { await new Promise(resolve => server.close(resolve)); }
});

test('the observer follows the submission through its phases and reports the service state', async () => {
  let polls = 0;
  const stub = await serviceStub({
    poll: res => {
      polls += 1;
      // Two polls that report "running", then a terminal result: enough to see the phase repeated, which is
      // what a long search actually looks like to the panel.
      if (polls <= 2) return res.end(JSON.stringify({ id: 'job-1', state: 'running', result: null }));
      return res.end(JSON.stringify({ id: 'job-1', state: 'completed',
        result: { status: 'partial', answer: '', evidence: [], warnings: [] } }));
    },
  });
  try {
    const seen = [];
    const result = await stub.client.search('w', 'f', 'q', 'FAST', undefined, 'session-a',
      { phase: (phase, facts) => seen.push([phase, facts.jobState]) });
    const phases = seen.map(entry => entry[0]);
    // The phase order is what matters, not how many `polling` reports fit into the loop's timing: it opens
    // with the submission, becomes `accepted` exactly once the service assigned an id, stays `polling` while
    // the job runs, and reaches the job's own terminal status. The final `cancelling` is the cleanup that
    // follows every call - this build confirms a result and stops a job through the same route, and the
    // lifecycle reports that step rather than hiding it.
    assert.equal(phases[0], 'submitting');
    assert.equal(phases[1], 'accepted');
    assert.ok(phases.includes('polling'));
    assert.ok(phases.includes('completed'), `the job's terminal phase must be observed (got ${phases.join(',')})`);
    // The final report is the call's own outcome: a result that was obtained keeps "completed" through the
    // confirmation that follows it. (A call that never obtained a result ends on "cancelling" instead - see
    // the failure cases in `client-recovery.test.mjs`.)
    assert.equal(phases.at(-1), 'completed');
    assert.deepEqual([...new Set(phases)], ['submitting', 'accepted', 'polling', 'completed']);
    assert.ok(seen.some(entry => entry[1] === 'running'));
    // The *result's* lifecycle is the job's terminal one: the cleanup note is about the call, not the job.
    assert.equal(result.lifecycle.phase, 'completed');
    assert.match(result.lifecycle.requestId, /^kq-[0-9a-z]{16}$/);
    // A completed result is confirmed, not cancelled: the result is preserved and the call is settled.
    assert.equal(stub.seen.acknowledges.length, 1);
    assert.equal(stub.seen.acknowledges[0].body.cancelIntent, false);
    assert.deepEqual(stub.seen.deletes, []);
  } finally { await stub.close(); }
});

test('every 429 the service can name lands on its own code, and an unnamed one invents no reason', () => {
  const cases = [
    ['QUERY_BUSY', 'KNOWLEDGE_QUERY_BUSY'],
    ['RESULT_CAPACITY', 'KNOWLEDGE_QUOTA_EXHAUSTED'],
    ['READ_LIMIT_EXCEEDED', 'KNOWLEDGE_QUOTA_EXHAUSTED'],
    ['PROVIDER_QUOTA_EXHAUSTED', 'KNOWLEDGE_QUOTA_EXHAUSTED'],
  ];
  for (const [serviceCode, expected] of cases) {
    const failure = classifyFailure(new KnowledgeServiceError(429, serviceCode));
    assert.equal(failure.code, expected, `${serviceCode} must classify as ${expected}`);
    assert.equal(failure.detail.httpStatus, 429);
    assert.equal(failure.detail.serviceCode, serviceCode);
    assert.ok(FAILURE_CODES.includes(failure.code));
  }
  // An older service sends a bare 429: the reason is *not* identified, so none is invented. This is the
  // split the review asked for - an unnamed rate limit is not called "quota", because that names a cause the
  // service never gave and prescribes a recovery that may be wrong.
  const unknown = classifyFailure(new KnowledgeServiceError(429, null));
  assert.equal(unknown.code, 'KNOWLEDGE_RATE_LIMITED');
  assert.equal(unknown.detail.serviceCode, null);
  const unknownSentence = failureRecord(unknown.code, { detail: unknown.detail }).message;
  assert.doesNotMatch(unknownSentence, /并发锁/);
  assert.match(unknownSentence, /未识别|not identified/i);
  // A service code this build does not know is the same case as none: it is not a quota and not a busy signal.
  const unrecognised = classifyFailure(new KnowledgeServiceError(429, null));
  assert.notEqual(unrecognised.code, 'KNOWLEDGE_QUERY_BUSY');
  // The busy sentence is the one that refuses the two wrong recoveries, and the false generalisation.
  const busy = classifyFailure(new KnowledgeServiceError(429, 'QUERY_BUSY'));
  assert.equal(busy.code, 'KNOWLEDGE_QUERY_BUSY');
  const busySentence = failureRecord(busy.code, { detail: busy.detail }).message;
  assert.match(busySentence, /concurrency limit, not an exhausted budget/);
  assert.match(busySentence, /do not wait in a shell/);
  assert.match(busySentence, /do not claim they will fail too/);
});

test('the worker codes are distinct from an isolation failure and stay identifiers', () => {
  for (const code of ['WORKER_NO_RESULT', 'WORKER_EXITED']) {
    const failure = classifyFailure(new KnowledgeServiceError(500, code));
    assert.equal(failure.detail.serviceCode, code, 'the worker outcome is preserved as a member, not as text');
  }
  assert.equal(classifyFailure(new KnowledgeServiceError(500, 'ISOLATION_UNAVAILABLE')).detail.serviceCode,
    'ISOLATION_UNAVAILABLE');
});

test('the lifecycle facts can only hold declared members, an id in shape, and booleans', () => {
  const detail = failureDetail({ query: {
    requestId: 'kq-abcdef0123456789', phase: 'polling', jobState: 'running', cancellationConfirmed: false,
    question: 'the library says X', path: 'C:/private/file.md',
  } });
  assert.deepEqual(detail.query, { requestId: 'kq-abcdef0123456789', phase: 'polling', jobState: 'running',
    cancellationConfirmed: false });
  // A service that answered with a state this build does not declare has its value dropped, not displayed.
  assert.equal(failureDetail({ query: { jobState: 'QUERY_BUSY_MAYBE' } }).query, null);
  assert.equal(failureDetail({ query: { phase: 'made-up' } }).query, null);
  assert.equal(failureDetail({ query: { requestId: 'sk-live-0123456789' } }).query, null);
  assert.equal(failureDetail({ query: 'not-an-object' }).query, null);
});

test('the panel keeps a retained record when a call ends without a confirmed ending', async () => {
  // T5: a tool call that returns is not a job that ended. The registry is driven exactly as the plugin drives
  // it - `begin`, `phase` updates, then `settled` - so this tests the decision rather than the wiring.
  const active = new ActiveQueries();
  const recancelled = [];
  const query = active.begin('session-test', 'FAST', () => { /* no live call left */ }, 'kq-abcdef0123456789',
    async (identity, requestId) => {
      recancelled.push([identity, requestId]);
      return { confirmed: false, state: 'running' };
    });
  active.update('session-test', query, { phase: 'cancelling', jobState: 'running', id: 'job-1' });
  // The call returns with the cleanup unconfirmed: the record is kept and stays actionable.
  active.finish('session-test', query, { phase: 'unknown', jobState: 'running', cancellationConfirmed: false });
  const retained = active.status('session-test');
  assert.ok(retained, 'an unresolved call keeps a record the panel can act on');
  assert.equal(retained.live, false, 'the call itself has returned');
  assert.equal(retained.cancellable, true, 'and the panel can still offer to stop the job');
  assert.equal(retained.jobState, 'running');
  assert.equal(retained.cancellationConfirmed, false);
  // Acting on it goes through the service handle, which is the only one left, and its answer is what is kept.
  const outcome = await active.recancel('session-test');
  assert.equal(outcome.attempted, 1);
  assert.equal(outcome.confirmed, 0, 'an unconfirmed stop stays unconfirmed');
  assert.deepEqual(recancelled, [['job-1', 'kq-abcdef0123456789']]);
  assert.equal(active.status('session-test').jobState, 'running');
  // A confirmed ending retires the record: there is nothing left to ask about.
  const confirmed = active.begin('session-other', 'FAST', () => {}, 'kq-0000000000000000');
  active.finish('session-other', confirmed, { phase: 'completed', jobState: 'completed', cancellationConfirmed: true });
  assert.equal(active.status('session-other').phase, 'completed');
  assert.equal(active.status('session-other').cancellable, false);
});

test('repeated searches in one session do not overwrite the only cancellable handle', async () => {
  // T5: the same session submitting again must not make the older call unreachable - the new entry is added
  // beside the live one, and the panel can still stop both.
  const active = new ActiveQueries();
  const cancelled = [];
  const first = active.begin('session-test', 'FAST', () => cancelled.push('first'), 'kq-1111111111111111');
  active.update('session-test', first, { phase: 'polling', jobState: 'running' });
  const second = active.begin('session-test', 'FAST', () => cancelled.push('second'), 'kq-2222222222222222');
  active.update('session-test', second, { phase: 'polling', jobState: 'running' });
  assert.equal(active.list('session-test').length, 2, 'both entries are visible');
  const outcome = active.cancel('session-test');
  assert.equal(outcome.requested, true);
  assert.deepEqual(cancelled.sort(), ['first', 'second'], 'both handles were asked, not only the newest');
  // And the two sessions never see each other's entries.
  assert.equal(active.list('session-other').length, 0);
});

test('a retained record is not reported as a second cancellation', async () => {
  const active = new ActiveQueries();
  const query = active.begin('session-test', 'FAST', () => {}, 'kq-3333333333333333');
  active.update('session-test', query, { phase: 'cancelling', jobState: 'running' });
  active.finish('session-test', query, { phase: 'cancelling', jobState: 'running', cancellationConfirmed: false });
  const again = active.cancel('session-test');
  assert.equal(again.requested, false, 'the local call has already returned: there is nothing to abort');
  assert.equal(again.retained, true, 'the panel is told what remains instead of a request that does nothing');
  assert.deepEqual(again.phases, ['cancelling:retained']);
  assert.equal(active.status('session-test').cancellable, true, 'the retained record offers server confirmation recovery, not another local abort');
});

/** One panel bridge, wired the way the plugin wires it, with an injected operation registry. */
function panel(active) {
  let handler;
  registerPanelBridge({
    inject: (_deps, cb) => cb({ effect: f => f(),
      webServer: { register: route => { handler = route.handler; return () => {}; } },
      connection: { requestRejection: () => undefined } }),
  }, new PanelSettings(), () => ({ url: 'http://127.0.0.1:19999', token: 'synthetic-token-abcdefghijklmnop' }),
    { available: false, state: 'idle', act: async () => ({ code: 'CONTROL_UNAVAILABLE', operation: 'failed' }) },
    active);
  return async (method, body) => {
    const req = Readable.from(body ? [body] : []);
    req.method = method;
    req.url = '/knowledge-panel?sessionId=session-test';
    req.headers = { host: 'localhost:3000', origin: 'http://localhost:3000', 'content-type': 'application/json' };
    let output;
    const res = { statusCode: 200, setHeader() {}, end(text) { output = text; } };
    await handler(req, res);
    return { status: res.statusCode, body: output ? JSON.parse(output) : null };
  };
}

test('the panel reports this session\'s query and cancels only that one', async () => {
  const active = new ActiveQueries();
  const cancelled = [];
  const query = active.begin('session-test', 'DEEP', () => cancelled.push('called'), 'kq-abcdef0123456789');
  active.update('session-test', query, { phase: 'polling', jobState: 'running', id: 'job-1' });
  const call = panel(active);
  const before = await call('GET');
  assert.equal(before.body.query.phase, 'polling');
  assert.equal(before.body.query.mode, 'DEEP');
  assert.equal(before.body.query.jobState, 'running');
  assert.equal(before.body.query.requestId, 'kq-abcdef0123456789');
  assert.equal(before.body.query.cancellable, true);
  assert.ok(!JSON.stringify(before.body).includes('synthetic-token'));
  const cancelledResponse = await call('POST', JSON.stringify({ cancelQuery: true }));
  assert.equal(cancelledResponse.body.code, 'CANCEL_REQUESTED');
  assert.deepEqual(cancelled, ['called'], 'the cancel reaches the live operation, not the service stop');
  assert.equal(cancelledResponse.body.query.phase, 'cancelling');
  assert.equal(cancelledResponse.body.query.cancellable, false, 'a cancelling query is not cancelled twice');
  // With nothing in flight the same action says so rather than pretending something stopped.
  active.finish('session-test', query);
  const empty = await call('POST', JSON.stringify({ cancelQuery: true }));
  assert.equal(empty.body.code, 'CANCEL_RETAINED',
    'the record of an unresolved call is kept, and the panel is told that nothing live is left to stop');
  assert.equal(empty.body.query.phase, 'cancelling');
  assert.equal(empty.body.query.live, false, 'a retained record is not reported as a live call');
});

test('the panel keeps the query axis visible even when the service is unreachable', async () => {
  const active = new ActiveQueries();
  const query = active.begin('session-test', 'FAST', () => {});
  active.update('session-test', query, { phase: 'submitting' });
  const call = panel(active);
  const response = await call('GET');
  assert.equal(response.body.service, 'unreachable');
  assert.equal(response.body.query.phase, 'submitting');
  assert.equal(response.body.query.requestId, null);
  assert.equal(response.body.query.cancellable, true);
  assert.ok(!JSON.stringify(response.body).includes('synthetic-token'));
});


test('R4 individual request cancellation and terminal facts remain independently visible',()=>{
  const active=new ActiveQueries();const cancelled=[];
  const first=active.begin('session-one','FAST',()=>cancelled.push('first'),'kq-1111111111111111');
  const second=active.begin('session-one','FAST',()=>cancelled.push('second'),'kq-2222222222222222');
  active.cancel('session-one',first.requestId);assert.deepEqual(cancelled,['first']);
  active.finish('session-one',first,{phase:'cancelled',jobState:'cancelled',cancellationConfirmed:true});
  assert.equal(active.list('session-one').length,2);
  assert.equal(active.list('session-one')[0].phase,'cancelled');
  assert.equal(active.status('session-one').requestId,second.requestId);
  assert.equal(active.list('session-other').length,0);
});

test('normal result acknowledgement reports completion without claiming cancellation', async () => {
 const stub=await serviceStub({poll:r=>r.end(JSON.stringify({id:'job-1',state:'completed',terminal:true,result:{status:'partial',evidence:[],warnings:[]}})),
 acknowledge:r=>r.end(JSON.stringify({known:true,state:'completed',terminal:true,acknowledged:true,released:true,cancellationConfirmed:false}))});
 try {const facts=[];await stub.client.search('w','f','q','FAST',undefined,'s',{phase:(phase,value)=>facts.push([phase,value])});
 assert.equal(facts.at(-1)[0],'completed');assert.equal(facts.at(-1)[1].cancellationConfirmed,false);
 }finally{await stub.close();}
});
