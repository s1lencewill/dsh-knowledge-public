/**
 * The acceptance stand-in provider: three endpoints, routed by *declared configuration identity* rather than by
 * guessing from the request body, and a conversation route whose decisions come from `scenario-machine.mjs`.
 *
 *   18794  the DSH conversation endpoint (the host's `llm-deepseek.baseURL`)
 *            - `model === 'title-stub'`          -> the session-title model declared in the profile patch
 *            - a request with a tool catalog     -> the scenario machine decides, and only a declared scenario
 *                                                   ever produces a tool call
 *            - anything else                     -> 400 + a redacted diagnostic (never an answer, never a default)
 *   18793  the engine endpoint (the service's `LLM_BASE_URL`)
 *            - sirchmunk's keyword-granularity prompt -> the routing JSON
 *            - sirchmunk's summary prompt             -> the SUMMARY / SHOULD_ANSWER body
 *            - anything else                          -> 400 + a redacted diagnostic
 *   18795  a read-only report of what each route served, including refusals and the machine's own trace.
 *
 * This file contains no outbound network code, so it has no path to a real provider. Answers honour `stream`
 * (the engine asks with `stream: false` and gets plain JSON, DSH asks with `stream: true` and gets SSE).
 * Nothing secret is logged: diagnostics carry structure (counts, booleans, a refusal reason), never message text.
 *
 *   node scripts/accept/stub-provider.mjs [--conv-port 18794] [--engine-port 18793] [--report-port 18795]
 */
import { createServer } from 'node:http';
import { createScenarioMachine, EXPECTED_TOOL_CALLS } from './scenario-machine.mjs';

const argv = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = argv.indexOf(name);
  return index > -1 ? argv[index + 1] : fallback;
};
const CONV_PORT = Number(flag('--conv-port', '18794'));
const ENGINE_PORT = Number(flag('--engine-port', '18793'));
const REPORT_PORT = Number(flag('--report-port', '18795'));

const machine = createScenarioMachine();
const counts = { conversation: 0, title: 0, engineRouting: 0, engineAnswer: 0,
  refusedConversation: 0, refusedEngine: 0, httpErrors: 0 };
const offeredToolNames = new Set();
const refusals = [];
const scenarioEnds = [];
/**
 * The observed *shape* of the last messages of each conversation request: roles, whether a tool call or a tool
 * result part was present and which call ids they carried, and whether a text message started with a scenario
 * marker. No message text is stored — the point is to build tests and the scenario rule from the real wire shape
 * instead of from a guess, which is what the previous round lacked.
 */
const requestShapes = [];
function recordShape(body) {
  const messages = Array.isArray(body.messages) ? body.messages : [];
  requestShapes.push({
    at: new Date().toISOString(),
    model: typeof body.model === 'string' ? body.model : null,
    totalMessages: messages.length,
    tail: messages.slice(-12).map(message => {
      const content = Array.isArray(message?.content) ? message.content : [];
      const text = typeof message?.content === 'string' ? message.content
        : content.filter(part => part?.type === 'text').map(part => part.text).join(' ');
      return {
        role: message?.role ?? null,
        toolCalls: Array.isArray(message?.tool_calls) ? message.tool_calls.map(call => call?.id ?? null) : [],
        toolResults: content.filter(part => part?.type === 'tool-result').map(part => part.toolCallId ?? null),
        toolCallId: message?.tool_call_id ?? null,
        textBytes: text ? text.length : 0,
        marker: /^\s*ACCEPT-(?:1|FAIL|CEILING)\b/.test(text),
        textShape: text ? (message?.content === text ? 'string' : 'text-part') : 'none',
      };
    }),
  });
  if (requestShapes.length > 20) requestShapes.shift();
}

const ROUTING = {
  type: 'search', primary: ['sample was held'], fallback: ['vessel', 'temperature'],
  idf: { 'sample was held': 9.0, vessel: 4.0, temperature: 3.0 },
  primary_alt: [], fallback_alt: [], file_hints: [], intent: 'find the recorded temperature and conditions',
};
const ANSWER = ['<SUMMARY>', 'The note records 298 K in a closed vessel for 1 h at 1.0 atm.', '</SUMMARY>',
  '<PRECISE_ANSWER>', '298 K, closed vessel, 1 h, 1.0 atm.', '</PRECISE_ANSWER>',
  '<SHOULD_ANSWER>true</SHOULD_ANSWER>', '<SHOULD_SAVE>true</SHOULD_SAVE>'].join('\n');

const isRoutingPrompt = (prompt) => prompt.includes('Keyword Granularity Strategy')
  || (prompt.includes('granularity') && prompt.includes('IDF'));
const isSummaryPrompt = (prompt) => prompt.includes('SHOULD_ANSWER') || prompt.includes('SHOULD_SAVE')
  || prompt.includes('<SUMMARY>') || prompt.includes('PRECISE_ANSWER');

function refuse(response, route, reason, body, extra = {}) {
  const diagnostic = { route, reason, requestBytes: extra.requestBytes ?? null,
    hasToolCatalog: Array.isArray(body?.tools) ? body.tools.length : 0,
    stream: body?.stream === undefined ? null : Boolean(body.stream),
    modelRecognised: body?.model === 'title-stub' };
  if (route.startsWith('conversation')) counts.refusedConversation += 1; else counts.refusedEngine += 1;
  refusals.push(diagnostic);
  response.writeHead(400, { 'content-type': 'application/json' });
  response.end(JSON.stringify({ error: 'unclassified request', ...diagnostic }));
}

function readBody(request) {
  return new Promise(resolve => {
    const chunks = [];
    request.on('data', chunk => chunks.push(chunk));
    request.on('end', () => {
      const raw = Buffer.concat(chunks);
      try { resolve({ body: JSON.parse(raw.toString('utf8')), bytes: raw.length }); }
      catch { resolve({ body: null, bytes: raw.length }); }
    });
  });
}

function answer(response, body, content, usage = { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 }) {
  if (body?.stream === false) {
    response.writeHead(200, { 'content-type': 'application/json' });
    return response.end(JSON.stringify({ id: 'acc', object: 'chat.completion', model: body.model,
      choices: [{ index: 0, message: { role: 'assistant', content }, finish_reason: 'stop' }], usage }));
  }
  response.writeHead(200, { 'content-type': 'text/event-stream; charset=utf-8' });
  response.write('data: ' + JSON.stringify({ id: 'acc', object: 'chat.completion.chunk', model: body.model,
    choices: [{ index: 0, delta: { role: 'assistant', content }, finish_reason: null }] }) + '\n\n');
  response.write('data: ' + JSON.stringify({ id: 'acc', object: 'chat.completion.chunk', model: body.model,
    choices: [{ index: 0, delta: {}, finish_reason: 'stop' }], usage }) + '\n\n');
  return response.end('data: [DONE]\n\n');
}

function toolCall(response, body, callId, name, args) {
  response.writeHead(200, { 'content-type': 'text/event-stream' });
  response.write('data: ' + JSON.stringify({ id: callId, object: 'chat.completion.chunk', model: body.model,
    choices: [{ index: 0, delta: { role: 'assistant', tool_calls: [{ index: 0, id: callId, type: 'function',
      function: { name, arguments: '' } }] }, finish_reason: null }] }) + '\n\n');
  response.write('data: ' + JSON.stringify({ id: callId, object: 'chat.completion.chunk', model: body.model,
    choices: [{ index: 0, delta: { tool_calls: [{ index: 0, function: { arguments: JSON.stringify(args) } }] }, finish_reason: null }] }) + '\n\n');
  response.write('data: ' + JSON.stringify({ id: callId, object: 'chat.completion.chunk', model: body.model,
    choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }] }) + '\n\n');
  return response.end('data: [DONE]\n\n');
}

// --- the conversation/session endpoint
createServer(async (request, response) => {
  const { body, bytes } = await readBody(request);
  if (!body) { counts.httpErrors += 1; response.writeHead(400); return response.end('{"error":"not json"}'); }
  for (const tool of body.tools ?? []) if (tool?.function?.name) offeredToolNames.add(tool.function.name);

  const decision = machine.decide(body);
  if (decision.route === 'title') { counts.title += 1; return answer(response, body, decision.text); }
  if (decision.kind === 'refuse') { return refuse(response, 'conversation', decision.reason, body, { requestBytes: bytes }); }
  counts.conversation += 1;
  recordShape(body);
  if (decision.kind === 'tool') {
    return toolCall(response, body, decision.callId, decision.name, decision.args);
  }
  scenarioEnds.push({ scenario: decision.scenario, stage: decision.stage, text: decision.text });
  return answer(response, body, decision.text);
}).listen(CONV_PORT, '127.0.0.1');

// --- the engine endpoint
createServer(async (request, response) => {
  const { body, bytes } = await readBody(request);
  if (!body) { counts.httpErrors += 1; response.writeHead(400); return response.end('{"error":"not json"}'); }
  const prompt = JSON.stringify(body.messages ?? []);
  if (isRoutingPrompt(prompt)) { counts.engineRouting += 1; return answer(response, body, JSON.stringify(ROUTING)); }
  if (isSummaryPrompt(prompt)) { counts.engineAnswer += 1; return answer(response, body, ANSWER); }
  return refuse(response, 'engine', 'no sirchmunk prompt template matched this request', body, { requestBytes: bytes });
}).listen(ENGINE_PORT, '127.0.0.1');

// --- the report endpoint (read-only)
createServer((request, response) => {
  response.writeHead(200, { 'content-type': 'application/json' });
  response.end(JSON.stringify({ counts, offeredToolNames: [...offeredToolNames].sort(),
    expectedToolCalls: EXPECTED_TOOL_CALLS, scenarioEnds, machine: machine.report(), requestShapes,
    refusals: refusals.slice(-10), ports: { conversation: CONV_PORT, engine: ENGINE_PORT },
    forwardsToRealProvider: false, outboundRequestsInThisFile: 0 }, null, 2));
}).listen(REPORT_PORT, '127.0.0.1', () => console.log(JSON.stringify({
  stubProvider: { conversation: CONV_PORT, engine: ENGINE_PORT, report: REPORT_PORT },
  scenarios: EXPECTED_TOOL_CALLS, forwardsToRealProvider: false })));
