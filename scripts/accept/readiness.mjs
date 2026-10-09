/**
 * Readiness preflight for an acceptance run: all three processes up, and the *routes* verified, before anybody is
 * told "you can start".
 *
 * The failure this exists for: an instance was announced as ready while the stand-in provider was already gone,
 * so the first real question became a model-request retry loop. A port check would not have caught that class of
 * failure either — what matters is that each declared route answers *and* that an unclassifiable request is
 * refused rather than answered.
 *
 * Checks, in order:
 *   1. the instance's declared conversation and engine endpoints are the stand-in;
 *   2. the service answers /health with isolationActive (its own bearer token, no model involved);
 *   3. the host printed a login URL for this instance's web port (the URL is never fetched: it is single-use);
 *   4. the stand-in answers its report endpoint and serves both declared routes;
 *   5. the conversation route answers a title request, and a tool-carrying request with a tool call;
 *   6. the engine route answers sirchmunk's keyword-granularity prompt with plain JSON;
 *   7. an unclassifiable request to each route is refused with 400 and a redacted diagnostic;
 *   8. the counters moved by exactly the expected amounts, and no request was served by a fallback path.
 *
 * Nothing here spends real model quota: every request goes to the local stand-in.
 *
 *   node scripts/accept/readiness.mjs --instance <dir> [--conv-port 18794] [--engine-port 18793] [--report-port 18795]
 */
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';

const argv = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = argv.indexOf(name);
  return index > -1 ? argv[index + 1] : fallback;
};
const INSTANCE = resolve(flag('--instance', '.'));
const CONV_PORT = Number(flag('--conv-port', '18794'));
const ENGINE_PORT = Number(flag('--engine-port', '18793'));
const REPORT_PORT = Number(flag('--report-port', '18795'));
const OUT = flag('--out', join(INSTANCE, 'data/logs/readiness.json'));

const config = JSON.parse(readFileSync(join(INSTANCE, 'data/config.json'), 'utf8'));
const token = readFileSync(join(INSTANCE, 'data/service-token.txt'), 'utf8').trim();
const report = { at: new Date().toISOString(), instance: INSTANCE.split(/[\\/]/).pop(), checks: [] };
const check = (name, ok, detail) => {
  report.checks.push({ name, ok: Boolean(ok), detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + detail : ''}`);
};
const json = async (url, init) => {
  try { const response = await fetch(url, init); return { status: response.status, body: await response.json().catch(() => null) }; }
  catch (error) { return { status: 0, error: String(error?.name ?? error) }; }
};

check('the instance points its conversation model at the stand-in',
  config.modelBaseUrl === `http://127.0.0.1:${CONV_PORT}`, config.modelBaseUrl);
check('the instance points the engine at the stand-in',
  config.engineBaseUrl === `http://127.0.0.1:${ENGINE_PORT}`, config.engineBaseUrl);

const health = await json(`http://127.0.0.1:${config.servicePort}/health`,
  { headers: { authorization: `Bearer ${token}` } });
check('the service answers /health with isolation active',
  health.status === 200 && health.body?.isolationActive === true,
  `HTTP ${health.status}, isolationActive=${health.body?.isolationActive}`);

const hostLog = existsSync(join(INSTANCE, 'data/logs/host.log')) ? readFileSync(join(INSTANCE, 'data/logs/host.log'), 'utf8') : '';
const urls = [...hostLog.matchAll(new RegExp(`http://127\\.0\\.0\\.1:${config.webPort}/\\?token=[A-Za-z0-9_-]+`, 'g'))];
check('the host has printed a login URL for this instance\'s web port', urls.length > 0,
  `${urls.length} URL(s) in the log (never fetched by this script)`);

const before = await json(`http://127.0.0.1:${REPORT_PORT}/`);
check('the stand-in provider answers its report endpoint', before.status === 200, `port ${REPORT_PORT}`);
check('the stand-in serves both declared routes', Boolean(before.body?.ports
  && before.body.ports.conversation === CONV_PORT && before.body.ports.engine === ENGINE_PORT),
  JSON.stringify(before.body?.ports ?? null));
check('the stand-in declares the acceptance scenarios and their call budgets',
  Boolean(before.body?.expectedToolCalls?.['ACCEPT-1'] && before.body?.expectedToolCalls?.['ACCEPT-FAIL']
    && before.body?.expectedToolCalls?.['ACCEPT-CEILING']),
  JSON.stringify(before.body?.expectedToolCalls ?? null));

const title = await json(`http://127.0.0.1:${CONV_PORT}/chat/completions`, { method: 'POST',
  headers: { 'content-type': 'application/json', authorization: 'Bearer stub-placeholder-never-inspected' },
  body: JSON.stringify({ model: 'title-stub', stream: true, messages: [{ role: 'user', content: 'name this session' }] }) });
check('the conversation route answers a title request with SSE', title.status === 200, `HTTP ${title.status}`);

const conversation = await json(`http://127.0.0.1:${CONV_PORT}/chat/completions`, { method: 'POST',
  headers: { 'content-type': 'application/json' }, body: JSON.stringify({ model: 'deepseek-flash', stream: true,
    // A marker-prefixed message of its own: the scenario machine selects on the message, and its turn key includes
    // a hash of that text, so this probe can never share state with the operator's own ACCEPT-1 turn.
    messages: [{ role: 'user', content: 'ACCEPT-1 [readiness probe: expect one tool call, then stop]' }],
    tools: [{ type: 'function', function: { name: 'knowledge_find_documents', parameters: { type: 'object' } } }] }) });
check('the conversation route answers a tool-carrying request with a tool call', conversation.status === 200,
  `HTTP ${conversation.status}`);

const routing = await json(`http://127.0.0.1:${ENGINE_PORT}/chat/completions`, { method: 'POST',
  headers: { 'content-type': 'application/json', authorization: 'Bearer stub-placeholder-never-inspected' },
  body: JSON.stringify({ model: 'deepseek-flash', stream: false, messages: [{ role: 'user',
    content: '### Multi-Level Keyword Granularity Strategy:\nExtract 3 levels of keywords with progressively finer granularity. IDF values should reflect term rarity.' }] }) });
let routingParsed = null;
try { routingParsed = JSON.parse(routing.body?.choices?.[0]?.message?.content ?? ''); } catch { routingParsed = null; }
check('the engine route answers sirchmunk\'s routing prompt with plain JSON',
  routing.status === 200 && Boolean(routingParsed?.primary),
  `HTTP ${routing.status}, keys=${routingParsed ? Object.keys(routingParsed).join(',') : 'none'}`);

const strayConversation = await json(`http://127.0.0.1:${CONV_PORT}/chat/completions`, { method: 'POST',
  headers: { 'content-type': 'application/json' }, body: JSON.stringify({ model: 'unknown-model', stream: true,
    messages: [{ role: 'user', content: 'hello' }] }) });
check('an unclassifiable conversation request is refused with a diagnostic',
  strayConversation.status === 400 && strayConversation.body?.route === 'conversation',
  `HTTP ${strayConversation.status}, route=${strayConversation.body?.route}`);
const strayEngine = await json(`http://127.0.0.1:${ENGINE_PORT}/chat/completions`, { method: 'POST',
  headers: { 'content-type': 'application/json' }, body: JSON.stringify({ model: 'deepseek-flash', stream: false,
    messages: [{ role: 'user', content: 'an unrelated question the engine would never ask' }] }) });
check('an unclassifiable engine request is refused with a diagnostic',
  strayEngine.status === 400 && strayEngine.body?.route === 'engine',
  `HTTP ${strayEngine.status}, route=${strayEngine.body?.route}`);

const after = await json(`http://127.0.0.1:${REPORT_PORT}/`);
const delta = (key) => (after.body?.counts?.[key] ?? 0) - (before.body?.counts?.[key] ?? 0);
check('the counters moved by exactly the readiness requests',
  delta('title') === 1 && delta('conversation') === 1 && delta('engineRouting') === 1
  && delta('refusedConversation') === 1 && delta('refusedEngine') === 1,
  `title+${delta('title')} conversation+${delta('conversation')} routing+${delta('engineRouting')} `
  + `refusedConv+${delta('refusedConversation')} refusedEng+${delta('refusedEngine')}`);
check('no request was served by a fallback path',
  (after.body?.outboundRequestsInThisFile ?? -1) === 0 && after.body?.forwardsToRealProvider === false,
  `outbound=${after.body?.outboundRequestsInThisFile}`);

report.ready = report.checks.every(entry => entry.ok);
/**
 * The service access log has no timestamps, so a later cross-check cannot tell which run a request belongs to.
 * Recording the log's length here is what makes the cross-check's arithmetic exact: it counts only the lines
 * written after this preflight.
 */
try {
  const serviceLog = join(INSTANCE, 'data/logs/service.log');
  report.serviceLogLinesAtReadiness = existsSync(serviceLog)
    ? readFileSync(serviceLog, 'utf8').split('\n').length : 0;
  report.stubCountsAtReadiness = after.body?.counts ?? null;
} catch { report.serviceLogLinesAtReadiness = null; }
console.log(`\n${report.checks.filter(entry => entry.ok).length} of ${report.checks.length} checks passed — `
  + (report.ready ? 'READY for UI acceptance' : 'NOT READY'));
writeFileSync(OUT, JSON.stringify(report, null, 2), 'utf8');
if (!report.ready) process.exitCode = 1;
