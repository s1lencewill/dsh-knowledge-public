import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { KnowledgeClient } from '../lib/client.js';
import { apply, definitions } from '../lib/index.js';

const token = 'synthetic-token-for-protocol-testing';
test('reject remote endpoints and absent token', () => {
  assert.throws(() => new KnowledgeClient('https://example.com', token));
  assert.throws(() => new KnowledgeClient('http://127.0.0.1', ''));
});
test('plugin registers without token and explains configuration at invocation', async () => {
  const original = process.env.DSH_KNOWLEDGE_TOKEN;
  delete process.env.DSH_KNOWLEDGE_TOKEN;
  try {
    const tools = [];
    apply({tools:{register:tool=>tools.push(tool)}});
    assert.equal(tools.length, 5);
    await assert.rejects(tools[0].execute({}, {agent:{session:{cwd:process.cwd()}}}), /KNOWLEDGE_NOT_CONFIGURED/);
  } finally {
    if (original === undefined) delete process.env.DSH_KNOWLEDGE_TOKEN;
    else process.env.DSH_KNOWLEDGE_TOKEN = original;
  }
});

for (const pollStatus of [200, 503]) {
  test(`${pollStatus === 200 ? 'a returned result' : 'a polling failure'} reports its cleanup state without losing it`, async () => {
    const warnings = [];
    let acknowledges = 0;
    const server = createServer((req, res) => {
      res.setHeader('content-type', 'application/json');
      // The settlement route: it is what confirms a result and what stops a running job, and the client
      // distinguishes the two by `cancelIntent`. A service that refuses it (500) must produce a warning that
      // never replaces the result or the original failure.
      if (req.method === 'POST' && req.url.includes('/acknowledge')) {
        acknowledges++; res.statusCode = 500; return res.end('{}');
      }
      if (req.method === 'POST') return res.end('{"id":"cleanup"}');
      res.statusCode = pollStatus;
      res.end(JSON.stringify({result:{status:'partial',answer:'',evidence:[],warnings:[]}}));
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    try {
      const client = new KnowledgeClient(`http://127.0.0.1:${server.address().port}`,token,message=>warnings.push(message));
      if (pollStatus === 200) {
        // A result the client actually received is **confirmed**, never stopped: the result is preserved and
        // the failure of the confirmation is reported on its own without touching it.
        const result = await client.search('w','f','q','FAST',undefined,'session-a');
        assert.equal(result.status, 'partial');
        assert.match(result.lifecycle.requestId, /^kq-[0-9a-z]{16}$/);
        assert.equal(acknowledges, 1, 'the confirmation is what releases the capacity slot');
        assert.equal(warnings.length, 1);
        assert.match(warnings[0], /KNOWLEDGE_CLEANUP_FAILED/);
        assert.ok(!warnings[0].includes(token));
      } else {
        // A polling failure still has a job behind it, so cleanup runs; when the service refuses it, that is
        // reported as a warning and never replaces the original failure.
        await assert.rejects(client.search('w','f','q','FAST',undefined,'session-a'), /HTTP 503/);
        assert.equal(acknowledges, 1);
        assert.equal(warnings.length, 1);
        assert.match(warnings[0], /KNOWLEDGE_CLEANUP_FAILED/);
        assert.ok(!warnings[0].includes(token));
      }
    } finally {await new Promise(resolve => server.close(resolve));}
  });
}

test('total query deadline cancels an endlessly running job', async () => {
  let acknowledges = 0;
  const server = createServer((req,res) => {
    res.setHeader('content-type','application/json');
    if (req.method === 'POST' && req.url.includes('/acknowledge')) {
      acknowledges++;
      return res.end('{"cancellationConfirmed":true,"state":"cancelled","settled":true}');
    }
    res.end(req.method === 'POST' ? '{"id":"timeout"}' : '{"result":null}');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try {
    const client = new KnowledgeClient(`http://127.0.0.1:${server.address().port}`,token,()=>{}, {requestMs:1000,fastMs:40,deepMs:40});
    await assert.rejects(client.search('w','f','q','FAST'), error=>error.name === 'TimeoutError');
    // The cleanup request went out even though the caller's deadline had already aborted the operation: it has
    // its own signal, so a deadline cannot leave a job running with nobody able to stop it.
    assert.equal(acknowledges,1);
  } finally {await new Promise(resolve=>server.close(resolve));}
});
test('polls result with bearer token and leaves an unacknowledged result in place', async () => {
  let acknowledges = 0;
  const server = createServer((req, res) => {
    assert.equal(req.headers.authorization, `Bearer ${token}`);
    res.setHeader('content-type', 'application/json');
    if (req.method === 'POST' && req.url.includes('/acknowledge')) {
      acknowledges++;
      return res.end(JSON.stringify({ acknowledged: true, released: true, cancellationConfirmed: true,
        state: 'completed' }));
    }
    if (req.method === 'POST') return res.end(JSON.stringify({id: 'test'}));
    res.end(JSON.stringify({result: {status:'partial',answer:'',evidence:[],warnings:['mock']}}));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const client = new KnowledgeClient(`http://127.0.0.1:${server.address().port}`, token);
    const result = await client.search('workspace', 'fixture', 'question', 'FAST', undefined, 'session-a');
    assert.equal(result.status, 'partial');
    // A received result is confirmed and kept: the lifecycle the client attaches says which submission it
    // came from, and the confirmation is what releases the slot.
    assert.equal(acknowledges, 1);
    assert.match(result.lifecycle.requestId, /^kq-[0-9a-z]{16}$/);
    assert.equal(result.lifecycle.phase, 'completed');
  } finally { await new Promise(resolve => server.close(resolve)); }
});
test('tools take the workspace from the real session header', async () => {
  const seen = [];
  const client = { libraries: async workspace => { seen.push(workspace); return []; },
                   search: async workspace => { seen.push(workspace); return {}; } };
  const tools = definitions(client);
  await tools[0].execute({}, {agent:{session:{header:{cwd:'C:\\real\\workspace'}}}});
  await tools[1].execute({libraryId:'f', query:'q'}, {agent:{session:{header:{cwd:'C:\\other'}}}});
  assert.deepEqual(seen, ['C:\\real\\workspace', 'C:\\other']);
  // The legacy spelling still works for the structural smoke harness.
  await tools[0].execute({}, {agent:{session:{cwd:'C:\\legacy'}}});
  assert.equal(seen[2], 'C:\\legacy');
  await assert.rejects(tools[0].execute({}, {}), /KNOWLEDGE_NOT_CONFIGURED/);
});

test('abort requests server cancellation before returning', async () => {
  let acknowledges = 0;
  const abort = new AbortController();
  const server = createServer((req, res) => {
    res.setHeader('content-type', 'application/json');
    if (req.method === 'POST' && req.url.includes('/acknowledge')) {
      acknowledges++;
      return res.end('{"cancellationConfirmed":true,"state":"cancelled","settled":true}');
    }
    if (req.method === 'POST') return res.end(JSON.stringify({id:'cancel'}));
    res.end(JSON.stringify({result:null}));
    setTimeout(() => abort.abort(new Error('stop')), 10);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const client = new KnowledgeClient(`http://127.0.0.1:${server.address().port}`, token);
    await assert.rejects(client.search('workspace','fixture','q','FAST',abort.signal,'session-a'), /stop/);
    assert.equal(acknowledges, 1);
  } finally { await new Promise(resolve => server.close(resolve)); }
});

test('a read posts the workspace, session and identifier, and returns the window', async () => {
  let seen;
  const server = createServer((req, res) => {
    res.setHeader('content-type', 'application/json');
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      seen = { url: req.url, method: req.method, authorization: req.headers.authorization, body: JSON.parse(body) };
      res.end(JSON.stringify({ fileHandle: 'krh1.x.y', excerpt: '原文', startChar: 0, endChar: 2, sourceVersion: 'v',
                               nextCursor: 'krc1.a.b', warnings: [], limits: { readsUsed: 1, readsLimit: 40 } }));
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const client = new KnowledgeClient(`http://127.0.0.1:${server.address().port}`, token);
    const window = await client.read('C:\\ws', 'session-1', { libraryId: 'fixture', fileHandle: 'krh1.x.y', sourceVersion: 'v', startLine: 12 });
    assert.equal(seen.url, '/reads');
    assert.equal(seen.method, 'POST');
    assert.equal(seen.authorization, `Bearer ${token}`);
    assert.deepEqual(seen.body, { workspace: 'C:\\ws', session: 'session-1', libraryId: 'fixture',
                                  fileHandle: 'krh1.x.y', sourceVersion: 'v', startLine: 12 });
    assert.equal(window.excerpt, '原文');
  } finally { await new Promise(resolve => server.close(resolve)); }
});

test('a refused read surfaces the service code, and drops the service prose', async () => {
  const server = createServer((req, res) => {
    res.statusCode = 409;
    res.setHeader('content-type', 'application/json');
    res.end(JSON.stringify({ detail: 'SOURCE_VERSION_CHANGED: the file changed since this identifier was issued; run knowledge_search again' }));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const client = new KnowledgeClient(`http://127.0.0.1:${server.address().port}`, token);
    await assert.rejects(client.read('w', 's', { fileHandle: 'krh1.x.y' }),
      error => {
        assert.match(error.message, /HTTP 409/);
        assert.match(error.message, /SOURCE_VERSION_CHANGED/);
        // The code is kept as a field for classification, and the service's prose is deliberately not carried:
        // the card renders a fixed sentence per code, so forwarding the prose would create a route for
        // upstream text (which can quote a path or an identifier) into a card.
        assert.equal(error.status, 409);
        assert.equal(error.serviceCode, 'SOURCE_VERSION_CHANGED');
        assert.ok(!error.message.includes('the file changed since'), 'service prose must not be in the message');
        return true;
      });
  } finally { await new Promise(resolve => server.close(resolve)); }
});

test('the read tool forwards the calling session as the budget key', async () => {
  const seen = [];
  const client = { read: async (workspace, session, args) => { seen.push({ workspace, session, args }); return {}; } };
  const byName = new Map(definitions(client).map(tool => [tool.name, tool]));
  const read = byName.get('knowledge_read');
  const evidence = { fileHandle: 'krh1.x.y', sourceVersion: 'v1', startLine: 40 };
  await read.execute({ libraryId: 'f', ...evidence }, { agent: { session: { header: { cwd: 'C:\\ws', id: 'abc' } } } });
  // Round-tripped like the wire payload: absent options must not be sent as nulls or empty strings.
  assert.deepEqual(JSON.parse(JSON.stringify(seen[0])),
    { workspace: 'C:\\ws', session: 'abc', args: { libraryId: 'f', ...evidence } });
  // Without a session id the workspace is the coarser, stricter grouping key.
  await read.execute({ libraryId: 'f', fileHandle: 'krh1.x.y', cursor: 'krc1.a.b' },
                     { agent: { session: { cwd: 'C:\\legacy' } } });
  assert.equal(seen[1].session, 'workspace:C:\\legacy');
  assert.equal(seen[1].args.cursor, 'krc1.a.b');
});

test('the read tool sends no path-shaped field and no budget key', () => {
  const byName = new Map(definitions({ read: async () => ({}) }).map(tool => [tool.name, tool]));
  const parameters = Object.keys(byName.get('knowledge_read').parameters).sort();
  assert.deepEqual(parameters,
    ['cursor', 'fileHandle', 'libraryId', 'maxChars', 'maxLines', 'overlapLines', 'ref', 'sourceVersion', 'startLine']);
  assert.ok(!parameters.includes('session'),
    'the model cannot choose or change its budget key; the tool derives it from the session header');
  // `ref` is the short reference; the two long spellings stay declared for older transcripts, and the
  // descriptions say so. Nothing here is a path, a URL or a workspace.
  for (const name of parameters) {
    assert.ok(!/path|url|workspace|root/i.test(name), `${name} must not be a path-shaped parameter`);
  }
});
