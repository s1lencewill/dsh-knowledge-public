/**
 * Acceptance scenario machine — the decision logic of the stand-in provider, kept separate from HTTP so it can be
 * tested against the request shapes the host actually sends.
 *
 * Why it exists in this shape. The first version decided from string matching over the *whole* message history
 * ("does any message contain `fileHandle`?"), which is wrong in a session that has run before: history from an
 * earlier scenario won the match, the machine re-read the same handle until the per-turn ceiling stopped it, and
 * each scenario inherited the previous one's stage. This version is a finite state machine per turn, and every
 * decision comes from facts it can point at:
 *
 *   - the **scenario** is only ever read from the last user *text* message of the current turn, matching
 *     `ACCEPT-1 | ACCEPT-FAIL | ACCEPT-CEILING`. History can be background but can never select or advance a
 *     scenario;
 *   - the **stage** is the number of results that answer a call *this machine issued for this turn*. Historical
 *     results answer historical call ids and therefore cannot move the stage;
 *   - the **turn key** is the index of the scenario message in the message list. The list only grows, so the key
 *     is stable inside a turn and different between turns; the state map is bounded and drops the oldest keys;
 *   - every call id is globally unique (`acc-<scenario>-<counter>`), so a result can never be attributed twice;
 *   - a scenario that has finished returns text and never calls a tool again. A result that does not match what
 *     the scenario declared is reported as a failed scenario, not looped on.
 *
 * The tool/result shape is read tolerantly (the OpenAI-style `role: 'tool'` message and the persisted
 * `content: [{type: 'tool-result', toolCallId}]` form both carry the id this machine matches on), and the shape
 * that was observed is reported rather than assumed.
 */

/** Scenario definitions: the declared, finite tool sequence and what ends it. */
export const SCENARIOS = {
  'ACCEPT-1': {
    description: 'locate a document, read it once, then answer in plain text',
    expectedToolCalls: 2,
    steps: [
      { tool: 'knowledge_find_documents', args: { libraryId: 'samples', title: 'Thermal Stability of the Synthetic Sample' } },
      // The read step carries every required argument itself; only the handle comes from the locate result. A step
      // whose arguments were missing `libraryId` was rejected by the host as `invalid arguments`, which the card
      // could only render as the generic unknown failure.
      { tool: 'knowledge_read', fromHandleOfStep: 0,
        args: { libraryId: 'samples', startLine: 1, maxChars: 400, maxLines: 8 } },
    ],
  },
  'ACCEPT-FAIL': {
    description: 'one call that must fail, then answer in plain text',
    expectedToolCalls: 1,
    steps: [
      { tool: 'knowledge_read', args: { libraryId: 'samples',
        fileHandle: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', startLine: 1, maxChars: 200, maxLines: 4 } },
    ],
    /** The one expected outcome: an error carrying a knowledge failure code. */
    expect: 'error',
  },
  'ACCEPT-CEILING': {
    description: 'six list calls the plugin dispatches, then a seventh the per-turn ceiling refuses locally',
    // Seven *attempts*: the plugin dispatches the first six and refuses the seventh before any HTTP request, so
    // the script has to ask seven times for the ceiling to be the thing that stops it.
    expectedToolCalls: 7,
    repeat: { tool: 'knowledge_list_libraries', args: {}, attempts: 7 },
  },
};

const SCENARIO_MARKER = /^\s*(ACCEPT-(?:1|FAIL|CEILING))\b/;
const TERMINAL_STOP = /stopped"\s*:\s*"(KNOWLEDGE_[A-Z_]+)"/;
const ERROR_CODE = /Error:\s*(KNOWLEDGE_[A-Z_]+)/;

/** Every call id this process hands out is unique, so a result can never be counted twice. */
let callCounter = 0;
export const nextCallId = (scenario) => `acc-${String(scenario).replace('ACCEPT-', '').toLowerCase()}-${++callCounter}`;

/**
 * A turn key from the scenario message: its index, its scenario, and a hash of its own text.
 *
 * The hash is what keeps a readiness probe and a real session apart even when both messages are the first thing in
 * their own list, and it keeps two `ACCEPT-1` turns apart inside one session. Nothing else is derived from the
 * text — the scenario name and the index already carry the meaning.
 */
export function turnKeyOf(index, scenario, prompt) {
  let hash = 0x811c9dc5;
  const text = String(prompt ?? '');
  for (let position = 0; position < text.length; position += 1) {
    hash ^= text.charCodeAt(position);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return `${index}:${scenario}:${hash.toString(16)}`;
}

/** Plain text of one message, ignoring tool-result carriers. */
function textOf(message) {
  const content = message?.content;
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content.filter(part => part?.type === 'text').map(part => String(part.text ?? '')).join(' ');
  }
  return '';
}

/** The scenario of the current turn: read from the last user *instruction* message of this turn, never from a
 * previous one, and never abandoned because the host injected something. */
export function scenarioOfTurn(messages) {
  const list = messages ?? [];
  for (let index = list.length - 1; index >= 0; index -= 1) {
    const message = list[index];
    const role = message?.role ?? null;
    const content = Array.isArray(message?.content) ? message.content : [];
    const hasToolResult = content.some(part => part?.type === 'tool-result');
    if (role === 'assistant') {
      // A final assistant answer - text and no tool calls - closes the turn: a marker before it belongs to a
      // previous turn and must not be replayed.
      const calls = Array.isArray(message?.tool_calls) ? message.tool_calls : [];
      if (calls.length === 0 && textOf(message).trim() !== '') return null;
      continue;
    }
    if (role === 'tool') continue;
    if (role !== 'user') continue;
    if (hasToolResult) continue;                     // a result carrier is not an instruction
    const text = textOf(message);
    const match = SCENARIO_MARKER.exec(text);
    if (match) return { scenario: match[1], index, prompt: text.trim() };
    // Anything else the host puts in the conversation as a user message - a repeat-tool reminder, a runtime
    // context snapshot, a skill list - is background, not an instruction and not a boundary. Treating it as a
    // boundary is what made the third `knowledge_list_libraries` call in a real run get a 400 from the stand-in:
    // the reminder arrived, the scenario could no longer be found, and the request was refused as unclassifiable.
    continue;
  }
  return null;
}

/** One tool result, as either wire shape, with the id it answers. */
export function toolResults(messages, fromIndex = 0) {
  const results = [];
  for (let index = fromIndex; index < (messages ?? []).length; index += 1) {
    const message = messages[index];
    if (message?.role === 'tool' && (message.tool_call_id ?? message.toolCallId)) {
      results.push({ index, callId: String(message.tool_call_id ?? message.toolCallId),
        text: typeof message.content === 'string' ? message.content : JSON.stringify(message.content ?? ''),
        isError: Boolean(message.isError), shape: 'role-tool' });
      continue;
    }
    for (const part of Array.isArray(message?.content) ? message.content : []) {
      if (part?.type !== 'tool-result' || !part.toolCallId) continue;
      const inner = Array.isArray(part.content) ? part.content : [];
      results.push({ index, callId: String(part.toolCallId),
        text: inner.filter(p => p?.type === 'text').map(p => String(p.text ?? '')).join(' ') || JSON.stringify(part.content ?? ''),
        isError: Boolean(part.isError), shape: 'content-part' });
    }
  }
  return results;
}

/** The parsed body of a successful knowledge tool result, or null. */
function parseJson(text) {
  try { const value = JSON.parse(text); return value && typeof value === 'object' ? value : null; } catch { return null; }
}

/** The failure code of an error result: the text form the model sees, or the structured stop. */
export function codeOf(result) {
  if (!result) return null;
  const stop = TERMINAL_STOP.exec(result.text);
  if (stop) return stop[1];
  const failure = parseJson(result.text);
  if (failure?.failure?.code) return String(failure.failure.code);
  if (failure?.stopped) return String(failure.stopped);
  const error = ERROR_CODE.exec(result.text);
  if (error) return error[1];
  return null;
}

/**
 * Create one machine. `decide` is pure with respect to the messages it is handed: the only state is the map of
 * per-turn progress, bounded so a long session cannot grow it without limit.
 */
export function createScenarioMachine(options = {}) {
  const maxTurns = options.maxTurns ?? 20;
  const state = new Map();          // turn key (message index) -> { scenario, issued: [callId], shape }
  const observed = new Set();
  const trace = [];

  const decide = ({ messages, model, tools } = {}) => {
    if (model === 'title-stub') return { kind: 'text', text: 'Synthetic acceptance session', route: 'title' };
    if (!Array.isArray(tools) || tools.length === 0) {
      return { kind: 'refuse', reason: 'a conversation request must carry the tool catalog' };
    }
    const found = scenarioOfTurn(messages);
    if (!found) {
      return { kind: 'refuse', reason: 'the current turn carries no ACCEPT-1 / ACCEPT-FAIL / ACCEPT-CEILING instruction' };
    }
    const key = turnKeyOf(found.index, found.scenario, found.prompt);
    let turn = state.get(key);
    if (!turn) {
      turn = { scenario: found.scenario, issued: [] };
      state.set(key, turn);
      while (state.size > maxTurns) state.delete(state.keys().next().value);
    }
    const definition = SCENARIOS[found.scenario];
    const results = toolResults(messages, found.index)
      .filter(result => turn.issued.includes(result.callId));
    for (const result of results) observed.add(result.shape);
    const stage = results.length;
    const last = results[results.length - 1] ?? null;

    const emit = (decision) => {
      if (decision.kind === 'tool') {
        const callId = nextCallId(found.scenario);
        turn.issued.push(callId);
        trace.push({ turn: key, stage, callId, tool: decision.name });
        return { ...decision, callId, route: 'conversation', scenario: found.scenario, stage };
      }
      trace.push({ turn: key, stage, end: decision.kind, reason: decision.reason ?? null, scenario: found.scenario });
      return { ...decision, route: 'conversation', scenario: found.scenario, stage };
    };

    if (definition.repeat) {
      // A fixed number of identical calls that all succeed, so the failure governor cannot fire first: the only
      // thing that can stop this scenario is the per-turn ceiling.
      if (stage < definition.repeat.attempts) {
        return emit({ kind: 'tool', name: definition.repeat.tool, args: definition.repeat.args });
      }
      const lastAttempt = results[definition.repeat.attempts - 1] ?? last;
      if (stage === definition.repeat.attempts) {
        const code = codeOf(lastAttempt);
        return emit(code === 'KNOWLEDGE_TURN_LIMIT_REACHED'
          ? { kind: 'text', text: `ACCEPT-CEILING: the seventh call was refused locally as expected (${code}). Script complete; no eighth call is issued.` }
          : { kind: 'text', text: `ACCEPT-CEILING FAILED: the seventh call was expected to be refused locally, but the result was ${code ?? 'a success'}. Script ends here.` });
      }
      return emit({ kind: 'text', text: 'ACCEPT-CEILING: script already complete.' });
    }

    const step = definition.steps[stage];
    if (!step) {
      // No further step is declared: finish in text. A scenario must never fall through into another tool call,
      // and it must not call a failed script "complete" either - a required step that came back an error is
      // reported as the scenario failing.
      if (definition.expect === 'error') {
        const code = codeOf(last);
        return emit(code
          ? { kind: 'text', text: `${found.scenario}: the controlled call failed as expected (${code}). Script complete.` }
          : (last?.isError
            ? { kind: 'text', text: `${found.scenario} FAILED: the controlled call returned an error without a knowledge code. Script ends here.` }
            : { kind: 'text', text: `${found.scenario} FAILED: the controlled call was expected to fail, but it returned a result. Script ends here.` }));
      }
      const failedStep = results.map((result, position) => ({ result, position }))
        .find(entry => entry.result.isError || codeOf(entry.result));
      if (failedStep) {
        const code = codeOf(failedStep.result);
        return emit({ kind: 'text', text: `${found.scenario} FAILED: step ${failedStep.position + 1} of `
          + `${definition.steps.length} returned ${code ? code : 'an error without a knowledge code'}. Script ends here.` });
      }
      return emit({ kind: 'text', text: `${found.scenario}: script complete after ${stage} tool call(s).` });
    }

    // A step that depends on an earlier result must find it; when it cannot, the scenario fails instead of
    // guessing a reference or repeating the previous call.
    if (step.fromHandleOfStep !== undefined) {
      const source = results[step.fromHandleOfStep];
      const parsed = source ? parseJson(source.text) : null;
      // `readRef` is what a current result carries: the plugin mints a short reference and strips the long
      // signed handle, so a real model copies thirteen characters instead of three hundred. `fileHandle` is
      // still read as a fallback so this machine also drives a build from before that change.
      const reference = parsed?.readRef
        ?? parsed?.documents?.[0]?.readRef ?? parsed?.entries?.[0]?.readRef ?? null;
      const handle = parsed?.fileHandle
        ?? parsed?.documents?.[0]?.fileHandle ?? null;
      if (!reference && !handle) {
        return emit({ kind: 'text', text:
          `${found.scenario} FAILED: step ${stage + 1} needs a read reference from step ${step.fromHandleOfStep + 1}, `
          + `and that result carried none (${codeOf(source) ?? (source?.isError ? 'an error without a knowledge code' : 'no code')}). `
          + 'Script ends here.' });
      }
      // Every declared argument goes with the call: the read step's own `libraryId` and window are what the tool
      // schema requires, and a call missing them is rejected by the host before any knowledge code exists.
      return emit({ kind: 'tool', name: step.tool,
        args: reference ? { ...(step.args ?? {}), ref: reference } : { ...(step.args ?? {}), fileHandle: handle } });
    }
    return emit({ kind: 'tool', name: step.tool, args: step.args });
  };

  return {
    decide,
    report: () => ({ scenariosDeclared: Object.keys(SCENARIOS), turns: [...state.entries()].map(([key, turn]) => ({
      key, scenario: turn.scenario, callsIssued: turn.issued.length, callIds: turn.issued })),
      resultShapesObserved: [...observed], trace }),
  };
}

export const EXPECTED_TOOL_CALLS = Object.fromEntries(
  Object.entries(SCENARIOS).map(([name, definition]) => [name, definition.expectedToolCalls]));
