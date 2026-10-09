/**
 * Real-HTTP acceptance: the transport is a real TCP connection to the real service, and the engine is a stub.
 *
 * What this adds over the route-level tests. Those call `service.submit(...)` in-process, which proves the
 * state machine but cannot prove that a *response was actually written before the work started* - the exact
 * claim the previous round made on a 0.4s sleep. Here the request goes through uvicorn: if the handler waited
 * for the preparation, the client would still be waiting when its own request bound expired, and the
 * assertions below would fail rather than pass on a technicality.
 *
 * What it does **not** do: no model call, no container, no network beyond loopback. The worker is stubbed and
 * the provider path is never entered; the counters below are what make "no second worker was started"
 * checkable rather than assumed.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import {createHash} from 'node:crypto';
import { KnowledgeClient, newRequestId } from '../lib/client.js';

const repository = resolve(import.meta.dirname, '..');
const python = process.env.DSH_KNOWLEDGE_PYTHON || join(repository, '.venv', 'Scripts', 'python.exe');
const entry = join(repository, 'scripts', 'synthetic-http-service.py');
const workspace = join(repository, '.local', 'verification', 'http-lifecycle');
const token = 'synthetic-token-for-http-lifecycle-tests';
const PREP_SECONDS = Number(process.env.HTTP_PREP_SECONDS ?? '11');

/** Start the synthetic service and wait until its `/health` answers. */
async function startService(options = {}) {
  mkdirSync(workspace, { recursive: true });
  const state = join(workspace, `state-${Date.now()}-${Math.random().toString(16).slice(2, 8)}.json`);
  const config = join(workspace, `config-${Date.now()}.json`);
  const library = join(workspace, 'library');
  mkdirSync(library, { recursive: true });
  writeFileSync(join(library, 'note.md'), '# note\nWALNUT\nsecond line.\n', 'utf8');
  writeFileSync(config, JSON.stringify({ libraries: [{ id: 'fixture', name: 'fixture', workspace: library,
    root: library, ...(options.realAuthorization ? {realDataTrial:{authorized:true,files:{'note.md':createHash('sha256').update(readFileSync(join(library,'note.md'))).digest('hex')}}} : {syntheticFixture: true}) }] }), 'utf8');
  const child = spawn(python, [entry], {
    cwd: repository,
    env: {
      ...process.env,
      PYTHONUTF8: '1',
      PYTHONDONTWRITEBYTECODE: '1',
      SYNTHETIC_TOKEN: token,
      SYNTHETIC_CONFIG: config,
      SYNTHETIC_STATE: state,
      SYNTHETIC_PREP_MS: String(options.prepMs ?? 0),
      SYNTHETIC_AUTH_MS: String(options.authMs ?? 0),
      SYNTHETIC_FAIL_ADMIT_ONCE: options.failAdmitOnce ? '1' : '0',
      SYNTHETIC_WORKER: options.worker ?? 'ok',
      SYNTHETIC_BUDGET_FAST: String(options.fastBudget ?? 90),
      SYNTHETIC_BUDGET_DEEP: String(options.deepBudget ?? 240),
      DSH_KNOWLEDGE_ISOLATION: 'off',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  child.stdout.on('data', chunk => { output += chunk; });
  child.stderr.on('data', chunk => { output += chunk; });
  const port = await new Promise((done, fail) => {
    const deadline = setTimeout(() => fail(new Error(`no port line in 20s: ${output.slice(-400)}`)), 20_000);
    child.stdout.on('data', chunk => {
      const match = /SYNTHETIC_PORT=(\d+)/.exec(String(chunk));
      if (match) { clearTimeout(deadline); done(Number(match[1])); }
    });
    child.on('exit', code => { clearTimeout(deadline); fail(new Error(`service exited ${code}: ${output.slice(-400)}`)); });
  });
  const endpoint = `http://127.0.0.1:${port}`;
  const headers = { 'content-type': 'application/json', authorization: `Bearer ${token}` };
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      const response = await fetch(endpoint + '/health', { headers, signal: AbortSignal.timeout(1000) });
      if (response.ok) break;
    } catch { /* not up yet */ }
    await new Promise(done => setTimeout(done, 50));
  }
  // Read the live counters over HTTP. Holding the Windows file open while os.replace publishes can
  // deny the rename; that would turn a measurement reader into a failure in the job under test.
  const stats = async () => (await fetch(endpoint+'/synthetic/stats', {headers})).json();

  return {
    endpoint, headers, child, state, workspace: library, stats, output:()=>output,
    client: (timing = {}) => new KnowledgeClient(endpoint, token, () => {}, timing),
    stop: () => new Promise(done => { child.once('exit', () => done()); child.kill(); }),
  };
}

test('T6.1 a real POST is admitted before the preparation finishes, and /health answers meanwhile', async () => {
  // The preparation sleeps for `PREP_SECONDS`, which is longer than any client request bound in use. Admission
  // must still answer, and the health route must stay answerable while it sleeps.
  const service = await startService({ prepMs: PREP_SECONDS * 1000 });
  try {
    const submitted = Date.now();
    const response = await fetch(service.endpoint + '/queries', {
      method: 'POST', headers: service.headers,
      body: JSON.stringify({ workspace: service.workspace, libraryId: 'fixture', query: 'q', mode: 'FAST',
        session: 'session-http', requestId: newRequestId() }),
      signal: AbortSignal.timeout(8000),
    });
    const admitted = Date.now() - submitted;
    const text = await response.text();
    assert.equal(response.status, 200, text);
    const body = JSON.parse(text);
    assert.ok(admitted < 3000, `admission must not wait for the preparation (took ${admitted}ms)`);
    assert.equal(body.state, 'accepted');
    assert.ok(body.id, 'the client got the id it needs to poll');
    // The health route answers *while* the preparation is still sleeping, inside a stated threshold.
    const healthStart = Date.now();
    const health = await fetch(service.endpoint + '/health', { headers: service.headers,
      signal: AbortSignal.timeout(3000) });
    const healthMs = Date.now() - healthStart;
    assert.equal(health.status, 200);
    assert.ok(healthMs < 1500, `/health must answer during preparation (took ${healthMs}ms)`);
    const facts = await health.json();
    assert.equal(facts.query.busy, true, 'the job axis reports the running query');
    // And a second submission is refused with the concurrency code, not a quota code.
    const second = await fetch(service.endpoint + '/queries', {
      method: 'POST', headers: service.headers,
      body: JSON.stringify({ workspace: service.workspace, libraryId: 'fixture', query: 'q', mode: 'FAST',
        session: 'session-http', requestId: newRequestId() }), signal: AbortSignal.timeout(5000),
    });
    assert.equal(second.status, 429);
    assert.equal((await second.json()).error.code, 'QUERY_BUSY');
    const after = await service.stats();
    assert.equal(after.workersStarted, 0, 'no worker may start while the preparation is still running');
  } finally { await service.stop(); }
});

test('T6.2 a lost admission response is recovered over HTTP and the real job is stopped (no second worker)', async () => {
  // The service creates the job and then answers 500 instead of returning it: the client never learns the id,
  // which is the recorded failure's shape. Its recovery must find the job by request id and stop it - and the
  // preparation has to finish inside the cleanup window for the service to be able to confirm that.
  const service = await startService({ realAuthorization:true, authMs:1200, failAdmitOnce: true });
  try {
    const client = service.client({ requestMs: 400, cancelMs: 6000, fastMs: 30_000, deepMs: 30_000 });
    const error = await client.search(service.workspace, 'fixture', 'q', 'FAST', undefined, 'session-http')
      .then(() => null, caught => caught);
    assert.ok(error, 'the lost admission means this call cannot succeed');
    // A service error carries `lifecycle`; a transport error carries `knowledgeLifecycle`. Both are the same
    // facts, and a test that assumed one shape would be testing the class rather than the behaviour.
    const facts = error.lifecycle ?? error.knowledgeLifecycle ?? {};
    assert.match(String(facts.requestId), /^kq-/);
    assert.equal(facts.phase, 'cancelled',
      'the recovery found the job, so the call reports the cleanup it performed');
    const stats = await service.stats();
    assert.equal(stats.admissions, 1, 'the recovery must not submit a second job');
    assert.equal(stats.workersStarted, 0, 'a cancelled preparation must never start a worker');
    assert.equal(stats.cancelled, 1, 'the recovered job was cancelled on the service, not only reported');
    assert.equal(facts.cancellationConfirmed, true,
      'the cleanup is confirmed, because the service confirmed the terminal state');
  } finally { await service.stop(); }
});

test('T6.3 a client that aborts mid-submission still stops its job', async () => {
  const service = await startService({ prepMs: 1200 });
  try {
    const controller = new AbortController();
    const client = service.client({ requestMs: 4000, cancelMs: 6000, fastMs: 30_000, deepMs: 30_000 });
    setTimeout(() => controller.abort(new Error('user stopped it')), 300);
    await client.search(service.workspace, 'fixture', 'q', 'FAST', controller.signal, 'session-http')
      .then(() => null, () => null);
    const stats = await service.stats();
    assert.equal(stats.workersStarted, 0, 'a job stopped while preparing must not start a worker');
    assert.equal(stats.cancelled, 1, 'the job was actually stopped on the service');
    assert.equal(stats.admissions, 1);
  } finally { await service.stop(); }
});

test('T6.4 scope refusals, terminal confirmation and capacity recycling over HTTP', async () => {
  const service = await startService({ worker: 'ok' });
  const owner = (session) => 'ownerWorkspace=' + encodeURIComponent(service.workspace)
    + '&ownerLibrary=fixture&ownerSession=' + encodeURIComponent(session);
  try {
    const submit = (extra = {}) => fetch(service.endpoint + '/queries', {
      method: 'POST', headers: service.headers,
      body: JSON.stringify({ workspace: service.workspace, libraryId: 'fixture', query: 'q', mode: 'FAST',
        session: 'session-http', requestId: newRequestId(), ...extra }),
      signal: AbortSignal.timeout(8000),
    });
    const first = await submit();
    assert.equal(first.status, 200);
    const identity = (await first.json()).id;
    // Poll until the stub worker's result lands.
    let state = null;
    for (let attempt = 0; attempt < 200; attempt += 1) {
      const response = await fetch(`${service.endpoint}/queries/${identity}?${owner('session-http')}`,
        { headers: service.headers, signal: AbortSignal.timeout(3000) });
      state = await response.json();
      if (state.result) break;
      await new Promise(done => setTimeout(done, 50));
    }
    assert.ok(state.result, 'the stub worker must produce a result');
    // A request without the session is refused as an incomplete scope - and the refusal names the field.
    const incomplete = await fetch(`${service.endpoint}/queries/${identity}` +
      `?ownerWorkspace=${encodeURIComponent(service.workspace)}&ownerLibrary=fixture`,
      { headers: service.headers });
    assert.equal(incomplete.status, 403);
    const refusal = await incomplete.json();
    assert.equal(refusal.error.code, 'QUERY_SCOPE_REQUIRED');
    assert.deepEqual(refusal.error.missing, ['session']);
    // Another session cannot reach it at all.
    const foreign = await fetch(`${service.endpoint}/queries/${identity}?${owner('session-other')}`,
      { headers: service.headers });
    assert.equal(foreign.status, 404);
    // Confirmation releases the capacity and preserves the result.
    const confirmed = await fetch(`${service.endpoint}/queries/${identity}/acknowledge`, {
      method: 'POST', headers: service.headers,
      body: JSON.stringify({ workspace: service.workspace, libraryId: 'fixture', session: 'session-http',
        cancelIntent: false }),
      signal: AbortSignal.timeout(5000),
    });
    const settled = await confirmed.json();
    assert.equal(settled.acknowledged, true);
    assert.equal(settled.released, true);
    assert.equal(settled.result.status, state.result.status, 'confirmation never rewrites the result');
    // With the slot released, a new submission is admitted rather than refused with RESULT_CAPACITY.
    const second = await submit();
    assert.equal(second.status, 200, await second.text());
    const stats = await service.stats();
    assert.equal(stats.admissions, 2);
  } finally { await service.stop(); }
});

test('T6.5 the synthetic service has no outbound forwarding and counts what it did', async () => {
  const service = await startService();
  try {
    const stats = await service.stats();
    assert.equal(stats.providerCalls, 0, 'the stub provider must not be reached by a synthetic admission');
    assert.equal(stats.forwarded, 0, 'the synthetic service forwards nothing off the machine');
    assert.equal(typeof stats.admissions, 'number');
    assert.equal(typeof stats.workersStarted, 'number');
    assert.equal(existsSync(dirname(service.state)), true);
  } finally { await service.stop(); }
});


test('R2 slow real authorization keeps admission, health, status and cancel responsive', async () => {
  const service = await startService({realAuthorization:true,authMs:11000});
  const owner={workspace:service.workspace,libraryId:'fixture',session:'auth-session'};
  const scope=new URLSearchParams({ownerWorkspace:owner.workspace,ownerLibrary:owner.libraryId,ownerSession:owner.session});
  const requestId=newRequestId();
  try {
    const at=Date.now();
    const first=await fetch(service.endpoint+'/queries',{method:'POST',headers:service.headers,body:JSON.stringify({...owner,query:'WALNUT',requestId})});
    const admitted=await first.json();
    assert.equal(first.status,200);
    const measurements={postMs:Date.now()-at,healthMs:null,statusMs:null,authorizationDelayMs:11000};
    assert.ok(measurements.postMs<3000);
    for(let i=0;i<100 && !(await service.stats()).authStarted;i++)await new Promise(r=>setTimeout(r,30));
    assert.equal((await service.stats()).authStarted,1,JSON.stringify({admitted,stats:await service.stats(),job:await (await fetch(service.endpoint+'/queries/'+admitted.id+'?'+scope,{headers:service.headers})).json(),output:service.output()}));
    for (const path of ['/health','/queries/'+admitted.id+'?'+scope]) {
      const before=Date.now();
      assert.equal((await fetch(service.endpoint+path,{headers:service.headers})).status,200);
      const elapsed=Date.now()-before;
      if(path==='/health')measurements.healthMs=elapsed;else measurements.statusMs=elapsed;
      assert.ok(elapsed<1500);
    }
    const repeated=await fetch(service.endpoint+'/queries',{method:'POST',headers:service.headers,body:JSON.stringify({...owner,query:'WALNUT',requestId})});
    assert.equal((await repeated.json()).id,admitted.id);
    const busy=await fetch(service.endpoint+'/queries',{method:'POST',headers:service.headers,body:JSON.stringify({...owner,query:'new',requestId:newRequestId()})});
    assert.equal((await busy.json()).error.code,'QUERY_BUSY');
    const early=await fetch(service.endpoint+'/queries/'+admitted.id+'/acknowledge',{method:'POST',headers:service.headers,body:JSON.stringify(owner)});
    assert.equal(early.status,409);assert.equal((await early.json()).error.code,'QUERY_NOT_TERMINAL');
    writeFileSync(join(workspace,'r2-measured.json'),JSON.stringify(measurements,null,2));
    const stopping=fetch(service.endpoint+'/queries/'+admitted.id+'/acknowledge',{method:'POST',headers:service.headers,body:JSON.stringify({...owner,cancelIntent:true})});
    await new Promise(r=>setTimeout(r,150));
    const current=await (await fetch(service.endpoint+'/queries/'+admitted.id+'?'+scope,{headers:service.headers})).json();
    assert.equal(current.state,'cancelling');assert.equal(current.terminal,false);
    const stopped=await (await stopping).json();
    assert.equal(stopped.cancellationConfirmed,true);
    const stats=await service.stats();assert.equal(stats.authFinished,1);assert.equal(stats.authAllowed,1);
    assert.equal(stats.workersStarted,0);assert.equal(stats.providerCalls,0);
    const late=await fetch(service.endpoint+'/queries',{method:'POST',headers:service.headers,body:JSON.stringify({...owner,query:'WALNUT',requestId})});
    assert.equal((await late.json()).error.code,'QUERY_RESULT_RELEASED');
  }finally{await service.stop();}
});

test('R2 changed authorization fails before worker; deadline shares slow authorization time', async()=>{
  for (const scenario of ['changed','deadline','success']) {
    const service=await startService({realAuthorization:true,authMs:scenario==='success'?30:1100,fastBudget:scenario==='deadline'?1:90});
    const owner={workspace:service.workspace,libraryId:'fixture',session:'auth-outcome'};
    try {
      const response=await fetch(service.endpoint+'/queries',{method:'POST',headers:service.headers,body:JSON.stringify({...owner,query:'WALNUT',requestId:newRequestId()})});
      const admitted=await response.json();
      if(scenario==='changed')writeFileSync(join(service.workspace,'note.md'),'changed during authorization','utf8');
      const scope=new URLSearchParams({ownerWorkspace:owner.workspace,ownerLibrary:owner.libraryId,ownerSession:owner.session});
      let result;
      for(let i=0;i<200;i++) {
        result=await (await fetch(service.endpoint+'/queries/'+admitted.id+'?'+scope,{headers:service.headers})).json();
        if(result.terminal)break;
        await new Promise(r=>setTimeout(r,20));
      }
      assert.equal(result.terminal,true);
      const stats=await service.stats();
      assert.equal(stats.authStarted,1);assert.equal(stats.authFinished,1);
      assert.equal(stats.workersStarted,scenario==='success'?1:0);
      if(scenario==='changed')assert.equal(result.result.status,'failed');
      if(scenario==='deadline')assert.match(result.result.warnings.join(' '),/BUDGET_EXCEEDED/);
      assert.equal(stats.providerCalls,0);
    }finally{await service.stop();}
  }
});
