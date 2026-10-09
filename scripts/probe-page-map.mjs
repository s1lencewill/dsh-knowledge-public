/**
 * Verify a rebuilt page map against the running service, using the real library it describes.
 *
 * Two halves, one purpose. Offline it checks the catalogue's own binding (every mapped document's map
 * matches the text it claims to map, coverage has no gaps or overlaps, pages ascend). Online it reads
 * windows through the real service and reports where each page number came from, so the claim under test
 * is the service's resolution and not this script's parsing of the file.
 *
 * Privacy: this touches an authorized real library (`.local/full-literature-v2-20260917`). Excerpt text is
 * never printed and never written outside `.local/`; the console output carries identifiers, line numbers
 * and page numbers only.
 *
 *   node scripts/probe-page-map.mjs --url http://127.0.0.1:18791 --library literature-full-v2 \
 *        --workspace <abs> --catalogue <catalogue.json> --library-root <texts dir> [--out <json>]
 */
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { KnowledgeClient } from '../lib/client.js';
import { definitions } from '../lib/index.js';

function parseArgs(argv) {
  const options = { url: 'http://127.0.0.1:18791', library: 'literature-full-v2', workspace: process.cwd(),
    catalogue: null, libraryRoot: null, out: null, sample: 3 };
  for (let index = 0; index < argv.length; index += 1) {
    const [flag, inline] = argv[index].split('=');
    const next = () => inline ?? argv[++index];
    switch (flag) {
      case '--url': options.url = next(); break;
      case '--library': options.library = next(); break;
      case '--workspace': options.workspace = resolve(next()); break;
      case '--catalogue': options.catalogue = resolve(next()); break;
      case '--library-root': options.libraryRoot = resolve(next()); break;
      case '--out': options.out = resolve(next()); break;
      case '--sample': options.sample = Number(next()); break;
      default: throw new Error(`unknown argument: ${argv[index]}`);
    }
  }
  return options;
}

/** The text's line count by the reader's own definition, not `split('\n').length`. */
function lineCount(text) {
  return (text.match(/\n/g) ?? []).length + (text.endsWith('\n') ? 0 : 1);
}

function auditCatalogue(cataloguePath, libraryRoot) {
  const catalogue = JSON.parse(readFileSync(cataloguePath, 'utf8'));
  const documents = catalogue.documents ?? {};
  const findings = [];
  for (const [key, record] of Object.entries(documents)) {
    const entry = { documentId: record.documentId ?? key, mapped: Array.isArray(record.pageMap) && record.pageMap.length > 0 };
    if (!entry.mapped) { findings.push({ ...entry, problem: 'no page map' }); continue; }
    const text = readFileSync(resolve(libraryRoot, record.textFile), 'utf8');
    const textHash = createHash('sha256').update(readFileSync(resolve(libraryRoot, record.textFile))).digest('hex');
    const source = record.pageMapSource ?? {};
    const covered = record.pageMap.reduce((total, page) => total + (page.endLine - page.startLine + 1), 0);
    const lines = lineCount(text);
    const ascending = record.pageMap.every((page, index) => page.page === index + 1);
    const contiguous = record.pageMap.every((page, index) => index === 0
      ? page.startLine === 1
      : page.startLine === record.pageMap[index - 1].endLine + 1);
    findings.push({ ...entry, lines, coveredLines: covered, pages: record.pageMap.length, ascending, contiguous,
      textHashMatches: textHash === record.textSHA256,
      bindingMatches: source.auditTextSHA256 === record.textSHA256 && source.mappingVersion === catalogue.pageMapVersion });
  }
  return { catalogueVersion: catalogue.catalogueVersion, documents: Object.keys(documents).length, findings };
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const report = { url: options.url, library: options.library };
  if (options.catalogue && options.libraryRoot) {
    report.catalogue = auditCatalogue(options.catalogue, options.libraryRoot);
    const bad = report.catalogue.findings.filter(entry => !entry.mapped || !entry.bindingMatches
      || !entry.ascending || !entry.contiguous || entry.coveredLines !== entry.lines);
    report.catalogue.problems = bad;
    console.log(`catalogue: ${report.catalogue.documents} documents, ${bad.length} with a problem`);
  }

  const token = process.env.DSH_KNOWLEDGE_TOKEN ?? '';
  if (!token) throw new Error('DSH_KNOWLEDGE_TOKEN must be exported for this probe');
  const client = new KnowledgeClient(options.url, token);
  const tools = new Map(definitions(client).map(tool => [tool.name, tool]));
  const exec = { agent: { session: { header: { id: 'page-map-probe', cwd: options.workspace } } } };

  const located = await tools.get('knowledge_find_documents').execute(
    { libraryId: options.library, title: 'water', authors: 'Zhao', limit: 8 }, exec);
  const readable = (located.documents ?? []).filter(document => document.readable);
  report.located = readable.length;
  report.reads = [];
  // Aim the windows with the map itself, so the probe exercises a non-first page and a page crossing
  // rather than whatever happens to sit at the top of a document.
  const catalogue = options.catalogue ? JSON.parse(readFileSync(options.catalogue, 'utf8')) : null;
  const mapOf = (documentId) => {
    const record = Object.values(catalogue?.documents ?? {}).find(item => item.documentId === documentId);
    return record?.pageMap ?? null;
  };
  for (const candidate of readable.slice(0, options.sample)) {
    const pageMap = mapOf(candidate.documentId);
    const targets = [];
    if (pageMap && pageMap.length > 2) {
      const middle = pageMap[Math.floor(pageMap.length / 2)];
      targets.push({ why: 'middle-page', startLine: middle.startLine });
      // One line above the next page's first line: the window starts on the previous page and the range
      // ends on the next one, which is the case a single page number would misdescribe.
      targets.push({ why: 'straddles-a-boundary', startLine: Math.max(1, pageMap[1].startLine - 1) });
    } else {
      targets.push({ why: 'default', startLine: 40 });
    }
    for (const target of targets) {
      const read = await tools.get('knowledge_read').execute(
        { libraryId: options.library, ref: candidate.readRef, sourceVersion: candidate.sourceVersion,
          startLine: target.startLine, maxChars: 900, maxLines: 30 }, exec);
      report.reads.push({
        why: target.why,
        documentId: candidate.documentId,
        fileName: candidate.fileName,
        pdfPage: read.pdfPage, pdfPageEnd: read.sourceCard?.location?.pdfPageEnd ?? null,
        pageSource: read.pageSource, pageHint: read.pageHint ?? null,
        startLine: read.startLine, endLine: read.endLine,
        excerptNonEmpty: typeof read.excerpt === 'string' && read.excerpt.length > 0,
        excerptChars: typeof read.excerpt === 'string' ? read.excerpt.length : 0,
        locationVerified: read.locationVerified,
        support: read.sourceCard?.status?.support,
      });
    }
  }
  // A window that straddles a page boundary, aimed at the second page's first line, to exercise the
  // cross-page report rather than a range comfortably inside one page.
  if (readable[0]) {
    const pageMap = mapOf(readable[0].documentId) ?? [];
    const second = pageMap[1] ?? { startLine: 1 };
    const read = await tools.get('knowledge_read').execute(
      { libraryId: options.library, ref: readable[0].readRef,
        sourceVersion: readable[0].sourceVersion, startLine: second.startLine, maxChars: 400, maxLines: 3 }, exec);
    report.firstWindow = { startLine: read.startLine, endLine: read.endLine, pdfPage: read.pdfPage,
      pdfPageEnd: read.sourceCard?.location?.pdfPageEnd ?? null,
      pageSource: read.pageSource, nextRef: Boolean(read.nextRef) };
  }
  if (options.out) writeFileSync(options.out, Buffer.from(JSON.stringify(report, null, 2), 'utf8'));
  console.log(JSON.stringify({ located: report.located, reads: report.reads, firstWindow: report.firstWindow,
    catalogueProblems: report.catalogue?.problems?.length ?? null }, null, 2));
}

await main();
