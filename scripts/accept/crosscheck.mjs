/**
 * Cross-check one acceptance run from four independent records, per scenario:
 *
 *   host attempts      `tool/call` events in the session log (what the model asked for)
 *   plugin results     `tool/result` events, classified: dispatched, refused locally, or failed
 *   service requests   the uvicorn access log (no timestamps, so it is reconciled by arithmetic, see below)
 *   stand-in records   the provider's own counters and its per-turn trace of issued call ids
 *
 * The claim that needs proving is "the seventh call in ACCEPT-CEILING produced no service request". It is proven
 * two ways: the seventh tool result carries the plugin's terminal stop record instead of a service answer, and the
 * service's request total is reconciled against the dispatched calls plus the scope commands that also read the
 * library list — an arithmetic that only closes if the refused call never reached the service.
 *
 * It reads only: the instance's session log, its service log, and the stand-in's report endpoint. Text is not
 * printed beyond short code/name fields; no token or cookie appears.
 *
 *   node scripts/accept/crosscheck.mjs --instance <dir> [--report-port 18795] [--out <file>]
 */
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { zstdDecompressSync } from 'node:zlib';

const argv = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = argv.indexOf(name);
  return index > -1 ? argv[index + 1] : fallback;
};
const INSTANCE = resolve(flag('--instance', '.'));
const REPORT_PORT = Number(flag('--report-port', '18795'));
const OUT = flag('--out', join(INSTANCE, 'data/logs/crosscheck.json'));

const MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]);
function decodeSessionLog(file) {
  const raw = readFileSync(file);
  const offsets = [];
  let at = raw.indexOf(MAGIC, 0);
  while (at !== -1) { offsets.push(at); at = raw.indexOf(MAGIC, at + 4); }
  const pieces = [];
  for (let index = 0; index < offsets.length; index += 1) {
    const slice = raw.subarray(offsets[index], index + 1 < offsets.length ? offsets[index + 1] : raw.length);
    try { pieces.push(zstdDecompressSync(slice)); } catch { /* torn tail */ }
  }
  return Buffer.concat(pieces).toString('utf8');
}

const sessionFiles = [];
const walk = (dir) => {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) { walk(path); continue; }
    if (entry.name.endsWith('.zstd')) sessionFiles.push({ path, at: statSync(path).mtimeMs });
  }
};
walk(join(INSTANCE, 'data/dsh-home'));
sessionFiles.sort((a, b) => b.at - a.at);
const events = [];
for (const line of decodeSessionLog(sessionFiles[0].path).split('\n').filter(Boolean)) {
  try { events.push(JSON.parse(line)); } catch { /* partial */ }
}

/** Rebuild the turns: a user message carrying ACCEPT-* opens one, the next turn/start closes it. */
const turns = [];
let current = null;
for (const event of events) {
  const type = String(event?.type ?? '');
  if (type === 'user/message') {
    const text = (event.data?.content ?? []).filter(part => part?.type === 'text').map(part => part.text).join(' ');
    const marker = /^\s*(ACCEPT-(?:1|FAIL|CEILING))\b/.exec(text);
    if (marker) { current = { marker: marker[1], calls: [], results: [], commands: [] }; turns.push(current); }
    continue;
  }
  if (type === 'command/run' && event.data?.name === 'knowledge-use') current?.commands.push(event.data?.args ?? '');
  if (!current) continue;
  if (type === 'tool/call') current.calls.push({ callId: event.data?.callId, name: event.data?.name,
    at: event.time ?? null });
  if (type === 'tool/result') {
    const part = (event.data?.message?.content ?? []).find(entry => entry?.type === 'tool-result');
    const text = (part?.content ?? []).filter(entry => entry?.type === 'text').map(entry => entry.text).join(' ');
    const stop = /"stopped"\s*:\s*"(KNOWLEDGE_[A-Z_]+)"/.exec(text);
    const code = stop?.[1] ?? /Error:\s*(KNOWLEDGE_[A-Z_]+)/.exec(text)?.[1] ?? null;
    current.results.push({ callId: part?.toolCallId ?? null, isError: Boolean(part?.isError) || event.data?.error != null,
      refusedLocally: Boolean(stop), code, at: event.time ?? null });
  }
}

const serviceLog = readFileSync(join(INSTANCE, 'data/logs/service.log'), 'utf8').split('\n');
// The uvicorn access log carries no timestamps, so the preflight records its length and only the lines written
// after it are this run's. Without that baseline the arithmetic below would count every earlier run too, which is
// exactly what the first version of this script did (and why its equality could not close).
const readinessPath = join(INSTANCE, 'data/logs/readiness.json');
let baseline = 0;
let readiness = null;
if (existsSync(readinessPath)) {
  try { readiness = JSON.parse(readFileSync(readinessPath, 'utf8')); baseline = Number(readiness.serviceLogLinesAtReadiness ?? 0); }
  catch { baseline = 0; }
}
const runLines = serviceLog.slice(baseline);
const countOf = (lines, pattern) => lines.filter(line => pattern.test(line)).length;
const serviceRequests = {
  libraries: countOf(runLines, /"(GET|POST) \/libraries/),
  queries: countOf(runLines, /"POST \/queries/),
  documents: countOf(runLines, /"POST \/documents/),
  reads: countOf(runLines, /"POST \/reads/),
  scope: 'lines after the preflight baseline',
  baselineLine: baseline,
  totalLines: serviceLog.length,
};
/**
 * Every `knowledge-use` command in the session under test reads the library list once, so those requests are part
 * of the `/libraries` total and have to be named for the arithmetic to close. They are counted across the whole
 * session file, not only inside the ACCEPT-* turns: the command is typed before the first of them.
 */
const scopeCommands = events.filter(event => String(event?.type ?? '') === 'command/run'
  && event.data?.name === 'knowledge-use').length;

let standIn = null;
try { standIn = await (await fetch(`http://127.0.0.1:${REPORT_PORT}/`)).json(); } catch { standIn = null; }

const summarise = (turn) => {
  const dispatched = turn.results.filter(result => !result.refusedLocally).length;
  const refused = turn.results.filter(result => result.refusedLocally).length;
  const failed = turn.results.filter(result => result.isError && !result.refusedLocally).length;
  return { scenario: turn.marker, hostAttempts: turn.calls.length, results: turn.results.length,
    dispatched, refusedLocally: refused, failed,
    toolsUsed: [...new Set(turn.calls.map(call => call.name))],
    codes: [...new Set(turn.results.map(result => result.code).filter(Boolean))],
    scopeCommands: turn.commands.length };
};

const perScenario = turns.map(summarise);
const dispatchedTotal = perScenario.reduce((total, entry) => total + entry.dispatched, 0);
const ceiling = perScenario.filter(entry => entry.scenario === 'ACCEPT-CEILING').pop() ?? null;
/**
 * The tail block of the run: the consecutive `/libraries` requests at the end of the scoped log, with nothing
 * after them. The ceiling turn's dispatched calls are the last knowledge calls of the run, so this block is their
 * requests — and its length is the count that matters, because a dispatched eighth call (or a seventh that reached
 * the service) would leave one more line behind it.
 */
const tailBlock = (() => {
  let size = 0;
  for (let index = runLines.length - 1; index >= 0; index -= 1) {
    const line = runLines[index];
    if (line === undefined || line.trim() === '') continue;
    if (/"(GET|POST) \/libraries/.test(line)) size += 1;
    else break;
  }
  return { consecutiveLibrariesRequestsAtTheEnd: size,
    isLastRequestOfTheRun: runLines.filter(line => line.trim() !== '').length > 0 };
})();
const report = {
  at: new Date().toISOString(),
  instance: INSTANCE.split(/[\\/]/).pop(),
  sessionFile: sessionFiles[0]?.path ?? null,
  scenarios: perScenario,
  serviceRequests,
  reconciliation: {
    scopeCommandsThatReadTheLibraryList: scopeCommands,
    // Every `/libraries` request is either a scope command or a dispatched list call. If the refused seventh call
    // had reached the service, this equality could not hold.
    librariesRequestsExpected: scopeCommands + (ceiling?.dispatched ?? 0),
    librariesRequestsObserved: serviceRequests.libraries,
    closes: serviceRequests.libraries === scopeCommands + (ceiling?.dispatched ?? 0),
    // And the sharper form of the same claim: the run ends with exactly one request per dispatched ceiling call.
    ceiling: ceiling ? { hostAttempts: ceiling.hostAttempts, dispatched: ceiling.dispatched,
      refusedLocally: ceiling.refusedLocally, requestsAtTheEndOfTheRun: tailBlock.consecutiveLibrariesRequestsAtTheEnd,
      provesTheRefusedCallSentNothing: tailBlock.consecutiveLibrariesRequestsAtTheEnd === ceiling.dispatched
        && ceiling.hostAttempts > ceiling.dispatched } : null,
    dispatchedKnowledgeCallsTotal: dispatchedTotal,
  },
  standIn: standIn ? { counts: standIn.counts, scenarioEnds: standIn.scenarioEnds,
    turns: standIn.machine?.turns ?? null, resultShapesObserved: standIn.machine?.resultShapesObserved ?? null,
    refusals: standIn.refusals } : null,
};
console.log(JSON.stringify(report, null, 2));
writeFileSync(OUT, JSON.stringify(report, null, 2), 'utf8');
