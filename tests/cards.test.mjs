/**
 * The Host-side projection of a source card: the payload the browser card renders from, the plain-text
 * fallback, and the Main-Agent protocol's load point and boundaries.
 *
 * These are the parts a browser cannot be asked to prove: `presentationMeta` is what gets persisted on
 * `tool/result`, so what it may contain is a security boundary, and `presentResult` is the fallback for a
 * UI with no knowledge card at all. Nothing here contacts a service or a model.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { apply, definitions, resolveEndpoint } from '../lib/index.js';
import { payloadText, cardText, sourcesOf } from '../lib/cards.js';
import { PROTOCOL_SECTION, protocolText } from '../lib/protocol.js';
import { LibrarySelection } from '../lib/selection.js';
import { MAX_TURN_KNOWLEDGE_CALLS } from '../lib/turnlimit.js';

const card = (overrides = {}) => ({
  libraryId: 'lib-1', title: 'A study', fileName: 'a.pdf', excerpt: 'quoted text',
  location: { coordinateSpace: 'extracted-text', kind: 'pdf-page', pdfPage: 3, pageSource: 'page-map', startLine: 10, endLine: 11, startChar: 4, endChar: 15 },
  sourceVersion: 'abcdef0123456789', citationOrigin: 'engine', truncated: false, truncationNotices: [],
  parseQuality: 'clean',
  status: { identity: 'verified', identityDetail: null, locationVerified: true, support: 'not-assessed' },
  ...overrides,
});

test('a payload is read back from each of the three result shapes, and from nothing else', () => {
  assert.deepEqual(sourcesOf({ evidence: [{ sourceCard: card() }] }).length, 1);
  assert.deepEqual(sourcesOf({ documents: [{ sourceCard: card() }, { sourceCard: card({ title: 'B' }) }] }).length, 2);
  assert.deepEqual(sourcesOf({ sourceCard: card() }).length, 1);
  assert.deepEqual(sourcesOf({ libraries: [] }), []);
  assert.deepEqual(sourcesOf(null), []);
  assert.deepEqual(sourcesOf('nope'), []);
  // A malformed entry is dropped rather than rendered as a half card.
  assert.deepEqual(sourcesOf({ evidence: [{}], documents: [] }), []);
});

test('the card text keeps the three axes separate and never calls support assessed', () => {
  const text = cardText(card(), 0);
  assert.match(text, /身份已核验/);
  assert.match(text, /位置已核验/);
  assert.match(text, /支持未评估/);
  assert.doesNotMatch(text, /支持已核验|答案已核验|已验证结论/);
  assert.match(text, /PDF 第 3 页/);
  assert.match(text, /来自抽取映射/);
  assert.match(text, /解析质量 clean/);
  assert.match(text, /引用来源 engine/);
});

test('a page the text merely claims is never the page, and is shown as an unadopted hint', () => {
  // The same number, from the untrusted source: `pdfPage` must be null and the marker may appear only as
  // a diagnostic hint, because any document can contain a line that looks like the extraction's marker.
  const text = cardText(card({
    location: { coordinateSpace: 'extracted-text', kind: 'text-line', pdfPage: null, pageSource: 'unmapped',
      pageHint: 9, startLine: 4, endLine: 4 },
  }), 0);
  assert.match(text, /页码映射未建立/);
  assert.match(text, /疑似标记称第 9 页，未采信/);
  assert.doesNotMatch(text, /位置：[^（]*PDF 第 9 页/);
});

test('a map that does not cover the line is reported as a mapping gap, not as a missing marker', () => {
  // A gap is a defect in the extraction mapping and must not be described as a source that simply has
  // no pages; the reader needs to know the difference.
  const text = cardText(card({
    location: { coordinateSpace: 'extracted-text', kind: 'text-line', pdfPage: null, pageSource: 'map-gap',
      pageHint: null, startLine: 10, endLine: 11 },
  }), 0);
  assert.match(text, /抽取映射未覆盖该行/);
  assert.match(text, /映射缺口/);
  assert.doesNotMatch(text, /PDF 第/);
});

test('an unusable map says so and does not fall back to the text', () => {
  const text = cardText(card({
    location: { coordinateSpace: 'extracted-text', kind: 'text-line', pdfPage: null, pageSource: 'map-unusable',
      pageHint: null, startLine: 10, endLine: 11 },
  }), 0);
  assert.match(text, /页码映射不可用/);
  assert.match(text, /不回退到正文标记/);
  assert.doesNotMatch(text, /PDF 第|疑似标记/);
});

test('a source with no PDF says it gives line numbers only', () => {
  const text = cardText(card({
    location: { coordinateSpace: 'extracted-text', kind: 'text-line', pdfPage: null, pageSource: 'not-pdf',
      pageHint: null, startLine: 10, endLine: 11 },
  }), 0);
  assert.match(text, /非 PDF 来源/);
  assert.match(text, /只给行号/);
  assert.doesNotMatch(text, /PDF 第|疑似标记/);
});

test('a card with no location at all cannot imply one', () => {
  const text = cardText(card({ location: null }), 0);
  assert.match(text, /无位置/);
});

test('the three axes are not collapsed when the service assessed nothing', () => {
  const text = cardText(card({
    status: { identity: 'not-assessed', identityDetail: null, locationVerified: false, support: 'not-assessed' },
  }), 0);
  assert.match(text, /身份未核验/);
  assert.match(text, /位置未核验/);
  assert.match(text, /支持未评估/);
});

test('truncation notices are echoed verbatim, and an empty payload is not a success claim', () => {
  const truncated = payloadText({ sources: [card({ truncated: true, truncationNotices: ['EXCERPT_TRUNCATED'] })] }, '检索');
  assert.match(truncated, /EXCERPT_TRUNCATED/);
  const empty = payloadText({ sources: [] }, '检索');
  assert.match(empty, /零结果不等于库中没有该资料/);
});

test('presentationMeta carries the cards and nothing path- or token-shaped', () => {
  const tools = definitions({ libraries: async () => [] });
  const payload = tools[1].output.presentationMeta({ libraryId: 'lib-1' }, {
    evidence: [{ sourceCard: card({ relativePath: 'texts/a.txt' }) }],
  });
  assert.equal(payload.sources.length, 1);
  const serialized = JSON.stringify(payload);
  assert.doesNotMatch(serialized, /synthetic-token/, 'a token must never reach a browser payload');
  const windowsPath = new RegExp('[A-Za-z]:' + '[\\\\/]');
  assert.ok(!windowsPath.test(serialized), 'no drive-letter path may reach a browser payload');
  assert.ok(!serialized.includes('/home/'), 'no POSIX home path may reach a browser payload');
  assert.ok(!serialized.includes('file://'), 'no file URL may reach a browser payload');
  assert.match(serialized, /"support":"not-assessed"/);
});

test('presentResult renders the fallback card from the persisted meta, and never declines a failure', () => {
  const tools = definitions({ libraries: async () => [] });
  const args = { libraryId: 'lib-1' };
  const rendered = tools[1].presentResult(args, { content: [], isError: false, meta: { sources: [card()] } });
  assert.equal(rendered.card, 'generic');
  assert.match(rendered.content[0].text, /支持未评估/);

  // A stop carries its failure record in the meta, because a stop has to travel as a successful result to
  // reach `concludeTurn()`. The fallback card must render it as a failure - and must not depend on `sources`,
  // which a stopped call does not have.
  const stopped = tools[1].presentResult(args, { content: [], isError: false, meta: { sources: [], failure: {
    code: 'KNOWLEDGE_TURN_LIMIT_REACHED', detail: 'knowledge_search.turn.calls.7.of.6', reference: 'k-abc12345', terminal: true,
  } } });
  assert.match(stopped.title, /失败/);
  assert.match(stopped.content[0].text, /本轮工具调用已达上限/);
  assert.match(stopped.content[0].text, /k-abc12345/);
  assert.doesNotMatch(stopped.content[0].text, /身份已核验|位置已核验/, 'a failure must not borrow the success axes');

  // A thrown failure has no meta at all (`presentationMeta` runs only for a returned value), so `isError`
  // alone must still produce a card naming it as a failure rather than nothing.
  const thrown = tools[1].presentResult(args, { content: [], isError: true });
  assert.match(thrown.title, /失败/);
  assert.equal(thrown.card, 'generic');

  // Only a successful call with no usable payload declines, which leaves the host rendering the raw content.
  assert.equal(tools[1].presentResult(args, { content: [], isError: false }), undefined);
  assert.equal(tools[1].presentResult(args, { content: [], isError: false, meta: { sources: 'no' } }), undefined);
});

test('presentCall is pure and derived only from the arguments', () => {
  const tools = definitions({ libraries: async () => [] });
  const view = tools[1].presentCall({ query: 'what is the temperature' });
  assert.equal(view.card, 'generic');
  assert.equal(view.kind, 'search');
  assert.match(view.title, /temperature/);
  // Replaying the same call must give the same answer, and no call may throw on odd arguments.
  assert.deepEqual(view, tools[1].presentCall({ query: 'what is the temperature' }));
  assert.equal(tools[1].presentCall({}).card, 'generic');
  assert.equal(tools[3].presentCall({}).card, 'generic');
});

test('the protocol names only tools this plugin actually registers', () => {
  const registered = new Set(definitions({ libraries: async () => [] }).map(tool => tool.name));
  const named = new Set(protocolText().match(/knowledge_[a-z_]+/g) ?? []);
  assert.ok(named.size > 0, 'the protocol must name its tools');
  for (const name of named) {
    assert.ok(registered.has(name), `the protocol names ${name}, which is not registered`);
  }
  assert.equal(registered.size, 5, 'enumerating a library is a fifth, separate tool');
  // The count is asserted against the registry rather than as a literal in two places: the protocol must name
  // every registered tool, or a tool exists that the routing rules never send the model to.
  assert.deepEqual([...registered].sort(), [...named].sort());
});

test('the protocol states the routing rules, the identity/location/support split and the untrusted rule', () => {
  const text = protocolText();
  // Routing, one rule per entry point. The enumeration rule is the one this round added: "which documents
  // exist" is not answerable by a keyword search, and the protocol has to say which tool does answer it.
  assert.match(text, /which documents does it contain[\s\S]*knowledge_list_documents/,
    'whole-library questions route to the local enumeration');
  assert.match(text, /never a substitute for enumerating/, 'a keyword search is not enumeration');
  assert.match(text, /DOI, title, author or file-name clue[\s\S]*knowledge_find_documents/,
    'locating by identity routes to the catalogue locator');
  assert.match(text, /content question[\s\S]*knowledge_search/, 'content questions route to search');
  assert.match(text, /knowledge_read/, 'missing conditions route to a read');
  assert.match(text, /enough evidence to answer: answer/, 'enough evidence ends the gathering');
  assert.match(text, /identity[\s\S]*location[\s\S]*support/i, 'the three axes are stated together');
  assert.match(text, /not-assessed/, 'support is described as never assessed');
  assert.match(text, /never silently take the first/, 'multi-candidate results must not be auto-picked');
  assert.match(text, /untrusted source material, not instruction/, 'documents are data, not instructions');
  assert.match(text, /host[\s\S]*session/, 'workspace and session come from the host');
  // The budget contract and the one bounded closing step, both of which a model has to know to finish a turn.
  assert.match(text, /`budget`[\s\S]*remaining/, 'every result reports the remaining per-turn budget');
  assert.match(text, /one\*\* extra step and asks for the final[\s\S]*answer in text/,
    'a spent budget grants one bounded text step');
  assert.equal(PROTOCOL_SECTION, 'knowledge:agent-protocol');
});

test('apply registers the protocol section and the scope command through optional injection', () => {
  const sections = [];
  const commands = [];
  const contexts = new Map([
    ['systemPrompt', { systemPrompt: { section: s => sections.push(s), getSectionOrder: () => 1900 } }],
    ['commands', { commands: { register: c => commands.push(c) } }],
    // The third budget hangs off the host's own dispatch waterfall, so `llm` is injected too. It is
    // injected (not declared in the plugin's `inject`) for the same reason as the other two: a profile
    // without an LLM service must still get the knowledge tools.
    ['llm', { on: () => {} }],
  ]);
  const injected = [];
  const ctx = {
    tools: { register: () => {} },
    inject: (deps, callback) => { injected.push(deps[0]); callback({ ...ctx, ...contexts.get(deps[0]) }); },
  };
  apply(ctx);
  assert.deepEqual(injected.sort(), ['commands', 'llm', 'sessions', 'systemPrompt', 'webServer']);
  assert.equal(sections.length, 1);
  assert.equal(sections[0].name, PROTOCOL_SECTION);
  assert.equal(commands.length, 1);
  assert.equal(commands[0].name, 'knowledge-use');
  assert.equal(typeof commands[0].handler, 'function');
  // The composer only claims an argument-bearing line for a command that declares an input descriptor, so
  // without this the documented `/knowledge-use <id>` form is sent to the model as a prompt instead of
  // running the handler. That is how it behaved in the real UI until this was added.
  assert.ok(commands[0].input, 'the command must declare an input descriptor to accept an argument');
  assert.equal(typeof commands[0].input.hint, 'string');
});

test('apply subscribes the model-request ceiling to the host dispatch waterfall', () => {
  // The listener is what makes the third budget enforceable before dispatch. It must be registered on
  // `llm/stream` and nowhere else: a hook that never fires would silently mean "no ceiling in force".
  const subscribed = [];
  const ctx = {
    tools: { register: () => {} },
    inject: (deps, callback) => {
      if (deps[0] === 'llm') callback({ on: (name) => subscribed.push(name) });
    },
  };
  apply(ctx);
  assert.deepEqual(subscribed, ['llm/stream']);
});

test('plugin config cannot arm the process-wide model-request ceiling', async () => {
  // The regression test for a real outage. The `llm/stream` hook is process-wide: it carries the request,
  // never a session. Arming a ceiling from the profile row therefore applied it to *every* session that
  // host served, and spending it refused model requests in all of them - the test window and unrelated
  // conversations alike.
  //
  // So there is no config spelling of it. A spent ledger named in plugin config must have no effect; the
  // same ledger named in the process environment must still refuse. Both halves matter: removing the
  // config path must not remove the mechanism.
  const directory = mkdtempSync(join(tmpdir(), 'modellimit-config-'));
  try {
    const ledger = join(directory, 'spent.json');
    writeFileSync(ledger, JSON.stringify({ used: 5, max: 5 }), 'utf8');
    const listeners = [];
    const ctx = {
      tools: { register: () => {} },
      inject: (deps, callback) => {
        if (deps[0] === 'llm') callback({ on: (name, listener) => listeners.push([name, listener]) });
      },
    };
    const listener = () => listeners.find(([name]) => name === 'llm/stream')[1];

    apply(ctx, { url: 'http://127.0.0.1:18765', tokenFile: 'ignored', agentQuotaFile: ledger,
                 maxAgentRequests: 1 });
    assert.ok(listener(), 'the hook is still registered');
    let dispatched = 0;
    const chosen = listener()({}, () => {
      dispatched += 1;
      return (async function* () { yield { type: 'finish', reason: 'stop' }; })();
    });
    for await (const _chunk of chosen) { /* drain */ }
    assert.equal(dispatched, 1, 'config must not be able to arm a ceiling that applies to every session');

    // And the mechanism itself is intact when the *process* is the one that asked for it - with both keys.
    const previous = { ...process.env };
    process.env.DSH_KNOWLEDGE_AGENT_QUOTA_FILE = ledger;
    process.env.DSH_KNOWLEDGE_ARM_AGENT_QUOTA = '1';
    try {
      const armed = [];
      apply({ tools: { register: () => {} },
              inject: (deps, callback) => {
                if (deps[0] === 'llm') callback({ on: (_name, fn) => armed.push(fn) });
              } });
      assert.throws(() => armed[0]({}, () => (async function* () { /* never reached */ })()),
        (error) => error.code === 'AGENT_REQUEST_BUDGET_EXHAUSTED',
        'a spent window named by the environment still refuses, before dispatch');
    } finally {
      for (const key of Object.keys(process.env)) if (!(key in previous)) delete process.env[key];
      Object.assign(process.env, previous);
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('a result carries the model-request budget beside the turn budget, as a separate layer', async () => {
  // Two ceilings are reported by every result, and they are different quantities: the turn's tool-call
  // number says nothing about how many model requests the Agent and the engine have started.
  const tools = definitions({ libraries: async () => [] });
  const exec = { agent: { session: { header: { id: 'session-1', cwd: process.cwd() } } } };
  const result = await tools[0].execute({}, exec);
  assert.equal(result.budget?.layer, 'turn');
  assert.equal(result.budget?.model?.enforced, false, 'no ceiling is configured in this process');
  assert.equal(typeof result.budget?.model?.attempted, 'number');
  assert.match(result.budget.model.countedAs, /dispatch/);
  // And the model counter says what it counts *over*: the host process, not this conversation. A reader who
  // saw a three-digit `attempted` and called it this session's call count would be inventing a measurement.
  assert.equal(result.budget.model.scope, 'host-process');
  assert.equal(result.budget.model.requestChars, null, 'an unmeasured size stays null, never zero');
});

test('a profile with neither service still registers its five tools', () => {
  const tools = [];
  apply({ tools: { register: tool => tools.push(tool) } });
  assert.equal(tools.length, 5);
});

test('the scope command refuses an unknown library and never invents a selection', async () => {
  const selection = new LibrarySelection();
  const commands = [];
  const ctx = {
    tools: { register: () => {} },
    inject: (deps, callback) => { if (deps[0] === 'commands') callback({ commands: { register: c => commands.push(c) } }); },
  };
  apply(ctx);
  const command = commands[0];
  const previousToken = process.env.DSH_KNOWLEDGE_TOKEN;
  process.env.DSH_KNOWLEDGE_TOKEN = 'synthetic-token-for-command-test';
  process.env.DSH_KNOWLEDGE_URL = 'http://127.0.0.1:18765';
  try {
    const agent = { session: { header: { id: 'session-1', cwd: process.cwd() } } };
    const missing = await command.handler({ agent, rawInput: '' });
    assert.equal(missing.kind, 'success');
    assert.match(missing.text, /No library is scoped/);
    // An unknown id is refused before anything is written, even though the service is unreachable here.
    const unknown = await command.handler({ agent, rawInput: 'does-not-exist' });
    assert.equal(unknown.kind, 'error');
    assert.equal(selection.get('session-1'), undefined);
    const cleared = await command.handler({ agent, rawInput: 'none' });
    assert.equal(cleared.kind, 'success');
  } finally {
    if (previousToken === undefined) delete process.env.DSH_KNOWLEDGE_TOKEN;
    else process.env.DSH_KNOWLEDGE_TOKEN = previousToken;
  }
});

test('a bare scope command lists what this session can reach, instead of only printing usage', async () => {
  // Observed in a real session: the operator ran `/knowledge-use` with no id, got usage prose back, and the
  // conversation then answered from an entirely different source - because nothing had told them which id to
  // type. "No library is scoped" is a state, not an answer; the reachable ids are the answer.
  const commands = [];
  const ctx = {
    tools: { register: () => {} },
    inject: (deps, callback) => { if (deps[0] === 'commands') callback({ commands: { register: c => commands.push(c) } }); },
  };
  apply(ctx);
  const command = commands[0];
  const previousToken = process.env.DSH_KNOWLEDGE_TOKEN;
  const previousUrl = process.env.DSH_KNOWLEDGE_URL;
  const previousFetch = globalThis.fetch;
  process.env.DSH_KNOWLEDGE_TOKEN = 'synthetic-token-for-bare-scope-test';
  process.env.DSH_KNOWLEDGE_URL = 'http://127.0.0.1:1';
  globalThis.fetch = async () => new Response(
    JSON.stringify([{ id: 'wiki-articles', name: 'Personal wiki articles' }, { id: 'other', name: 'Other' }]),
    { status: 200, headers: { 'content-type': 'application/json' } });
  try {
    const agent = { session: { header: { id: 'session-bare', cwd: process.cwd() } } };
    const bare = await command.handler({ agent, rawInput: '' });
    assert.equal(bare.kind, 'success');
    assert.match(bare.text, /No library is scoped/, 'the state is still reported');
    assert.match(bare.text, /wiki-articles \(Personal wiki articles\)/, 'and so are the reachable ids');
    assert.match(bare.text, /\bother \(Other\)/);
    assert.match(bare.text, /Usage: \/knowledge-use <libraryId> \| none/);

    // A lookup that fails says so, and never dresses the failure up as "no libraries configured".
    globalThis.fetch = async () => { throw new Error('service unreachable'); };
    const failed = await command.handler({ agent, rawInput: '' });
    assert.match(failed.text, /library list could not be read/);
    assert.doesNotMatch(failed.text, /No library is configured/);

    // And an empty list from a working service is a third, different statement.
    globalThis.fetch = async () => new Response('[]', { status: 200, headers: { 'content-type': 'application/json' } });
    const empty = await command.handler({ agent, rawInput: '' });
    assert.match(empty.text, /No library is configured for this session's directory/);
  } finally {
    globalThis.fetch = previousFetch;
    if (previousToken === undefined) delete process.env.DSH_KNOWLEDGE_TOKEN;
    else process.env.DSH_KNOWLEDGE_TOKEN = previousToken;
    if (previousUrl === undefined) delete process.env.DSH_KNOWLEDGE_URL;
    else process.env.DSH_KNOWLEDGE_URL = previousUrl;
  }
});

test('the scope command says what it ignored instead of dropping the rest of the line', async () => {
  // Observed in a real session: the operator typed the library id and then their question on the same line
  // (`/knowledge-use literature-full-v2 <question>`), the scope was applied, and the question disappeared with
  // no acknowledgement at all - they had to type it again. The command still takes one argument; what changed
  // is that it now says so.
  const commands = [];
  const ctx = {
    tools: { register: () => {} },
    inject: (deps, callback) => { if (deps[0] === 'commands') callback({ commands: { register: c => commands.push(c) } }); },
  };
  apply(ctx);
  const command = commands[0];
  const previousToken = process.env.DSH_KNOWLEDGE_TOKEN;
  const previousUrl = process.env.DSH_KNOWLEDGE_URL;
  const previousFetch = globalThis.fetch;
  process.env.DSH_KNOWLEDGE_TOKEN = 'synthetic-token-for-extra-words-test';
  process.env.DSH_KNOWLEDGE_URL = 'http://127.0.0.1:1';
  globalThis.fetch = async () => new Response(JSON.stringify([{ id: 'lib-a', name: 'A' }]),
    { status: 200, headers: { 'content-type': 'application/json' } });
  try {
    const agent = { session: { header: { id: 'session-extra', cwd: process.cwd() } } };
    const scoped = await command.handler({ agent, rawInput: 'lib-a where does the reaction happen' });
    assert.equal(scoped.kind, 'success');
    assert.match(scoped.text, /Scoped this conversation to "lib-a"/, 'the scope is still applied');
    assert.match(scoped.text, /5 extra words/, 'the ignored words are counted');
    assert.match(scoped.text, /send that text as an ordinary message/,
      'and the operator is told what to do with them');
    assert.doesNotMatch(scoped.text, /reaction happen/, 'arbitrary input is counted, never echoed back');
    // Nothing is said when there is nothing to ignore: this is not a nag on the ordinary form.
    const clean = await command.handler({ agent, rawInput: 'lib-a' });
    assert.match(clean.text, /Scoped this conversation to "lib-a"/);
    assert.doesNotMatch(clean.text, /extra word/);
  } finally {
    globalThis.fetch = previousFetch;
    if (previousToken === undefined) delete process.env.DSH_KNOWLEDGE_TOKEN;
    else process.env.DSH_KNOWLEDGE_TOKEN = previousToken;
    if (previousUrl === undefined) delete process.env.DSH_KNOWLEDGE_URL;
    else process.env.DSH_KNOWLEDGE_URL = previousUrl;
  }
});

test('an empty library list names the session directory and the rule, instead of "none configured"', async () => {  const commands = [];
  const ctx = {
    tools: { register: () => {} },
    inject: (deps, callback) => { if (deps[0] === 'commands') callback({ commands: { register: c => commands.push(c) } }); },
  };
  apply(ctx);
  const command = commands[0];
  const previousToken = process.env.DSH_KNOWLEDGE_TOKEN;
  const previousUrl = process.env.DSH_KNOWLEDGE_URL;
  const previousFetch = globalThis.fetch;
  process.env.DSH_KNOWLEDGE_TOKEN = 'synthetic-token-for-empty-list-test';
  process.env.DSH_KNOWLEDGE_URL = 'http://127.0.0.1:1';
  // The service answers "no library is authorised for this directory"; that is the case the operator hit.
  globalThis.fetch = async () => new Response('[]', { status: 200, headers: { 'content-type': 'application/json' } });
  try {
    const cwd = process.cwd();
    const agent = { session: { header: { id: 'session-empty', cwd } } };
    const result = await command.handler({ agent, rawInput: 'samples' });
    assert.equal(result.kind, 'error');
    assert.match(result.text, /No library is configured for this session's directory/);
    assert.ok(result.text.includes(cwd), 'the message names the directory the session is actually in');
    assert.match(result.text, /at or under the library's "workspace"/);
    assert.doesNotMatch(result.text, /none configured/);
  } finally {
    globalThis.fetch = previousFetch;
    if (previousToken === undefined) delete process.env.DSH_KNOWLEDGE_TOKEN;
    else process.env.DSH_KNOWLEDGE_TOKEN = previousToken;
    if (previousUrl === undefined) delete process.env.DSH_KNOWLEDGE_URL;
    else process.env.DSH_KNOWLEDGE_URL = previousUrl;
  }
});

test('the service endpoint comes from the profile config first, then the environment', () => {
  const env = { DSH_KNOWLEDGE_URL: 'http://127.0.0.1:1111', DSH_KNOWLEDGE_TOKEN: 'env-token-that-is-long-enough-01' };
  // A configuration row is deliberate, so it wins over the ambient environment; the environment is what the
  // trial's own `start` launches a private host with, and it stays the fallback.
  const configured = resolveEndpoint({ url: 'http://127.0.0.1:18765' }, env);
  assert.equal(configured.url, 'http://127.0.0.1:18765');
  assert.equal(configured.token, env.DSH_KNOWLEDGE_TOKEN);
  assert.equal(resolveEndpoint(undefined, env).url, 'http://127.0.0.1:1111');
  assert.equal(resolveEndpoint({ url: '   ' }, env).url, 'http://127.0.0.1:1111', 'a blank row is not a value');
  assert.equal(resolveEndpoint({}, {}).url, 'http://127.0.0.1:18765', 'the default stays the default');
});

test('a token file configures the plugin without touching the host environment', () => {
  // This is what an instance installed beside an already running DSH host needs: the host's environment cannot be
  // changed without restarting it, so the row names a file and the credential stays out of the configuration file.
  const dir = mkdtempSync(join(tmpdir(), 'kn-token-'));
  try {
    const tokenPath = join(dir, 'service-token.txt');
    writeFileSync(tokenPath, 'token-from-a-file-that-is-long-enough\n', 'utf8');
    const resolved = resolveEndpoint({ tokenFile: tokenPath }, {});
    assert.equal(resolved.token, 'token-from-a-file-that-is-long-enough', 'read and trimmed');
    assert.equal(resolveEndpoint({ tokenFile: join(dir, 'missing.txt') }, {}).token, '',
      'an unreadable file leaves the token empty, which the client refuses as KNOWLEDGE_NOT_CONFIGURED');
    // A short environment token does not stop the file from being used, and a long one keeps precedence.
    assert.equal(resolveEndpoint({ tokenFile: tokenPath }, { DSH_KNOWLEDGE_TOKEN: 'short' }).token,
      'token-from-a-file-that-is-long-enough');
    assert.equal(resolveEndpoint({ tokenFile: tokenPath },
      { DSH_KNOWLEDGE_TOKEN: 'environment-token-that-is-long-enough' }).token,
      'environment-token-that-is-long-enough');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('a session scope refuses another library instead of quietly narrowing the call', async () => {
  const selection = new LibrarySelection();
  selection.use('session-1', 'lib-a');
  const tools = definitions({ libraries: async () => [], search: async () => ({}) }, selection);
  const exec = { agent: { session: { header: { id: 'session-1', cwd: process.cwd() } } } };
  await assert.rejects(tools[1].execute({ libraryId: 'lib-b', query: 'q' }, exec), /KNOWLEDGE_LIBRARY_NOT_SELECTED/);
  assert.doesNotReject(tools[1].execute({ libraryId: 'lib-a', query: 'q' }, exec));
});

test('a session with no scope stays permissive, and the other session is unaffected', async () => {
  const selection = new LibrarySelection();
  selection.use('session-1', 'lib-a');
  const tools = definitions({ libraries: async () => [], search: async () => ({ ok: true }) }, selection);
  const other = { agent: { session: { header: { id: 'session-2', cwd: process.cwd() } } } };
  const unscoped = await tools[1].execute({ libraryId: 'lib-b', query: 'q' }, other);
  assert.equal(unscoped.ok, true, 'the call reached the client');
  // ...and it reports the turn's accounting alongside the service's own payload. A result that carried no
  // budget would leave the model unable to plan the rest of the turn.
  assert.equal(unscoped.budget.layer, 'turn');
  assert.equal(unscoped.budget.max, MAX_TURN_KNOWLEDGE_CALLS);
  assert.equal(unscoped.budget.remaining, MAX_TURN_KNOWLEDGE_CALLS - 1);
  const listed = await tools[0].execute({}, { agent: { session: { header: { id: 'session-1', cwd: process.cwd() } } } });
  assert.equal(listed.selectedLibraryId, 'lib-a');
  assert.match(listed.note, /scoped to "lib-a"/);
});

test('switching the scope bounds later calls only, and clearing it removes the bound', async () => {
  const selection = new LibrarySelection();
  selection.use('session-1', 'lib-a');
  const tools = definitions({ libraries: async () => [], search: async () => ({ ok: true }), findDocuments: async () => ({ ok: true }), read: async () => ({ ok: true }) }, selection);
  const read = tools.find(tool => tool.name === 'knowledge_read');
  const exec = { agent: { session: { header: { id: 'session-1', cwd: process.cwd() } } } };
  // A call accepted under lib-a is unaffected by the later switch: validation happened at dispatch.
  assert.equal((await tools[1].execute({ libraryId: 'lib-a', query: 'q' }, exec)).ok, true);
  selection.use('session-1', 'lib-b');
  await assert.rejects(tools[2].execute({ libraryId: 'lib-a', title: 't' }, exec), /KNOWLEDGE_LIBRARY_NOT_SELECTED/);
  assert.equal((await tools[2].execute({ libraryId: 'lib-b', title: 't' }, exec)).ok, true);
  selection.clear('session-1');
  assert.equal((await read.execute({ libraryId: 'lib-a', fileHandle: 'krh1.x' }, exec)).ok, true);
});

test('the selection table is bounded and never grows without limit', () => {
  const selection = new LibrarySelection(3);
  for (const key of ['a', 'b', 'c', 'd']) selection.use(key, 'lib-1');
  assert.equal(selection.size, 3);
  assert.equal(selection.get('a'), undefined, 'the oldest session is evicted, not the newest');
  assert.equal(selection.get('d'), 'lib-1');
});
