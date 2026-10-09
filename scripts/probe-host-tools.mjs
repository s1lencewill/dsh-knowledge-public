/**
 * Drive the four knowledge tools against the synthetic probe library, with no model in the loop.
 *
 * This is the bridge between the offline tests and the live UI: it calls the same `KnowledgeClient`, the
 * same tool `execute` bodies and the same `output.presentationMeta` projector the DSH session uses, and
 * writes down exactly what each call returned. The product is two things:
 *
 *   `.local/host-probe/captured.json`  - the canonical values and the `meta` payloads, for the record
 *   `.local/host-probe/session-seed.json` - the tool calls and their persisted meta, so the live host
 *     step can render the real card without a model call
 *
 * Nothing here is a stub: the service is the real isolated service over HTTP, the library is the
 * synthetic fixture, and every field on the captured cards came from that service. What it is NOT is a
 * test of retrieval quality: `knowledge_search` is not exercised (it needs the engine and a model), so
 * the run proves the locate -> read -> card chain only, and says so.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { KnowledgeClient } from '../lib/client.js';
import { definitions } from '../lib/index.js';
import { LibrarySelection } from '../lib/selection.js';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const probe = resolve(root, '.local/host-probe');

const url = process.env.DSH_KNOWLEDGE_URL ?? 'http://127.0.0.1:18781';
const token = (process.env.DSH_KNOWLEDGE_TOKEN ?? readFileSync(resolve(probe, 'service-token.txt'), 'utf8')).trim();
const workspace = process.env.DSH_KNOWLEDGE_WORKSPACE ?? root;
const libraryId = process.env.DSH_KNOWLEDGE_LIBRARY ?? 'synthetic-probe';
const session = 'host-probe-session';

const client = new KnowledgeClient(url, token);
const selection = new LibrarySelection();
const tools = new Map(definitions(client, selection).map(tool => [tool.name, tool]));
const exec = { agent: { session: { header: { id: session, cwd: workspace } } } };

/** Run one tool exactly as the registry would, and project its durable presentation payload. */
async function call(name, args) {
  const tool = tools.get(name);
  if (!tool) throw new Error(`no such tool: ${name}`);
  const value = await tool.execute(args, exec);
  const meta = tool.output.presentationMeta(args, value);
  const call_ = tool.presentCall ? tool.presentCall(args) : undefined;
  const result = tool.presentResult ? tool.presentResult(args, { content: [], isError: false, meta }) : undefined;
  return { name, args, value, meta, callView: call_, resultView: result };
}

const captured = { url, workspace, libraryId, library: probe, note: 'synthetic fixture; no model call', calls: [] };
const steps = [];

// 1. The libraries the workspace may use, and the session scope (none yet).
steps.push(await call('knowledge_list_libraries', {}));

// 2. Locate by title: the librarian path, no engine, no model.
steps.push(await call('knowledge_find_documents', {
  libraryId, title: 'Thermal Stability of the Synthetic Sample',
}));

// 3. Locate the Markdown note too, so the card can be checked against a document with no page markers.
steps.push(await call('knowledge_find_documents', { libraryId, fileName: 'notes.md' }));

// 4. Read the PDF around the value whose unit sits on the next line, using the reference a real locate
//    returned. This is the citation-completion path the protocol prescribes. The long signed handle is no
//    longer in the result at all, so a real model has nothing long to mistranscribe.
const located = steps[1].value.documents?.find(document => document.readable);
if (!located) throw new Error('the synthetic library did not locate the PDF; nothing to read');
if (!located.readRef) throw new Error('a located document must carry the short reference the model copies');
if (located.fileHandle !== undefined) throw new Error('the long signed handle must not reach the model');
steps.push(await call('knowledge_read', {
  libraryId, ref: located.readRef, sourceVersion: located.sourceVersion,
  startLine: 6, maxChars: 400, maxLines: 8,
}));

// 5. Read the Markdown note at a line range, then continue with the reference the service returned.
const note = steps[2].value.documents?.find(document => document.readable);
if (!note) throw new Error('the synthetic library did not locate the Markdown note');
steps.push(await call('knowledge_read', {
  libraryId, ref: note.readRef, sourceVersion: note.sourceVersion, startLine: 8, maxChars: 300, maxLines: 6,
}));
const nextRef = steps[4].value.nextRef;
if (nextRef) {
  steps.push(await call('knowledge_read', { libraryId, ref: nextRef, maxChars: 300 }));
}

// 6. The spoof check. The fixture's page 2 body deliberately contains a line reading `PDF page 9` at
//    line 17, so a page resolved by scanning the text backwards reports 9 for any window that starts
//    below it. The extraction's own page map still says page 2, and says the page came from the map.
steps.push(await call('knowledge_read', {
  libraryId, ref: located.readRef, sourceVersion: located.sourceVersion, startLine: 19, maxChars: 400, maxLines: 3,
}));
const spoofWindow = steps[steps.length - 1].value;
const spoofCard = steps[steps.length - 1].meta.sources[0];

for (const step of steps) {
  captured.calls.push({
    name: step.name, args: step.args,
    canonical: step.value,
    presentationMeta: step.meta,
    callView: step.callView,
    resultCardText: step.resultView?.content?.[0]?.text ?? null,
  });
}

// 6. The session scope: refuse another library, then allow the selected one, then clear it.
const scope = [];
try {
  await tools.get('knowledge_find_documents').execute({ libraryId: 'not-the-scoped-library', title: 'x' }, exec);
  scope.push({ case: 'unscoped-other-library', refused: false });
} catch (error) {
  scope.push({ case: 'unscoped-other-library', refused: true, code: String(error.message).split(':')[0] });
}
selection.use(session, libraryId);
try {
  await tools.get('knowledge_find_documents').execute({ libraryId: 'another-library', title: 'x' }, exec);
  scope.push({ case: 'scoped-other-library', refused: false });
} catch (error) {
  scope.push({ case: 'scoped-other-library', refused: true, code: String(error.message).split(':')[0] });
}
const scoped = await tools.get('knowledge_list_libraries').execute({}, exec);
scope.push({ case: 'scoped-list', selectedLibraryId: scoped.selectedLibraryId, note: scoped.note });
selection.clear(session);
scope.push({ case: 'cleared', selectedLibraryId: null });
captured.scope = scope;

const seed = {
  sessionId: session,
  workspace,
  libraryId,
  note: 'Persisted tool/result payloads captured from real service responses, for rendering the card in a live host.',
  tools: steps.map(step => ({ name: step.name, args: step.args, meta: step.meta, canonical: step.value })),
};

// The naive answer, computed here for contrast: what a page scan over the same text would have said.
const naivePage = (() => {
  const text = readFileSync(resolve(probe, 'library/texts/study.txt'), 'utf8');
  const lines = text.split('\n');
  for (let index = Math.min(spoofWindow.startLine, lines.length) - 1; index >= 0; index -= 1) {
    const marker = /^\s*PDF\s+page\s+(\d+)\s*$/i.exec(lines[index]);
    if (marker) return Number(marker[1]);
  }
  return null;
})();
captured.spoof = {
  what: 'A body line reading "PDF page 9" sits inside PDF page 2 of the fixture.',
  window: { startLine: spoofWindow.startLine, endLine: spoofWindow.endLine },
  naiveTextScanWouldReport: naivePage,
  cardReports: spoofCard?.location?.pdfPage ?? null,
  cardPageSource: spoofCard?.location?.pageSource ?? null,
};
captured.spoof.agreesWithNaiveScan = captured.spoof.naiveTextScanWouldReport === captured.spoof.cardReports;

writeFileSync(resolve(probe, 'captured.json'), Buffer.from(JSON.stringify(captured, null, 2), 'utf8'));
writeFileSync(resolve(probe, 'session-seed.json'), Buffer.from(JSON.stringify(seed, null, 2), 'utf8'));

const summary = {
  calls: captured.calls.map(entry => ({
    tool: entry.name,
    cards: entry.presentationMeta?.sources?.length ?? 0,
    locations: (entry.presentationMeta?.sources ?? []).map(source => ({
      file: source.fileName, page: source.location?.pdfPage ?? null, kind: source.location?.kind ?? null,
      lines: source.location ? `${source.location.startLine}-${source.location.endLine}` : null,
      identity: source.status?.identity, locationVerified: source.status?.locationVerified,
      support: source.status?.support, quality: source.parseQuality, truncated: source.truncated,
      origin: source.citationOrigin,
    })),
  })),
  scope,
  spoof: captured.spoof,
};
console.log(JSON.stringify(summary, null, 2));
