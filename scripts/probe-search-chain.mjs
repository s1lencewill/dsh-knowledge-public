/**
 * Run the real search chain end to end: `knowledge_search` -> citation -> source card -> `knowledge_read`.
 *
 * What is real here, and what is not, stated plainly because the distinction is the whole point:
 *
 *   real   the service, the snapshot, the citation verification, the AppContainer, the Sirchmunk engine,
 *          the model proxy, the tool definitions and the cards;
 *   stub   only the provider's reply, answered by `scripts/host-probe-stub-model.mjs`.
 *
 * No candidate is injected: the search runs whatever the engine finds, and the excerpts a citation carries
 * are read and verified by the parent against the snapshot. That is why this proves the *chain* and not
 * retrieval quality - a stub that answers with a plausible sentence says nothing about whether a real model
 * would have found the right passage.
 *
 * The chain asserted at the end is version agreement: the citation's `sourceVersion`, the card's
 * `sourceVersion` and the read window's `sourceVersion` must all be the same value, so a page, an excerpt
 * and a follow-up read cannot describe three different revisions of one file.
 *
 *   node scripts/probe-search-chain.mjs --url http://127.0.0.1:18781 --library synthetic-probe \
 *        --workspace <abs> --query "..." [--out <json>]
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { KnowledgeClient } from '../lib/client.js';
import { definitions } from '../lib/index.js';

function parseArgs(argv) {
  const options = { url: 'http://127.0.0.1:18781', library: 'synthetic-probe', workspace: process.cwd(),
    query: 'What temperature was the sample held at, and what were the conditions?', out: null, mode: 'FAST' };
  for (let index = 0; index < argv.length; index += 1) {
    const [flag, inline] = argv[index].split('=');
    const next = () => inline ?? argv[++index];
    switch (flag) {
      case '--url': options.url = next(); break;
      case '--library': options.library = next(); break;
      case '--workspace': options.workspace = resolve(next()); break;
      case '--query': options.query = next(); break;
      case '--mode': options.mode = next(); break;
      case '--out': options.out = resolve(next()); break;
      default: throw new Error(`unknown argument: ${argv[index]}`);
    }
  }
  return options;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const token = process.env.DSH_KNOWLEDGE_TOKEN ?? '';
  if (!token) throw new Error('DSH_KNOWLEDGE_TOKEN must be exported for this probe');
  const client = new KnowledgeClient(options.url, token);
  const tools = new Map(definitions(client).map(tool => [tool.name, tool]));
  const exec = { agent: { session: { header: { id: 'search-chain-probe', cwd: options.workspace } } } };

  const search = await tools.get('knowledge_search').execute(
    { libraryId: options.library, query: options.query, mode: options.mode }, exec);
  const searchMeta = tools.get('knowledge_search').output.presentationMeta({}, search);

  const report = {
    query: options.query, mode: options.mode,
    serviceStatus: search.status,
    warnings: search.warnings ?? [],
    evidenceCount: (search.evidence ?? []).length,
    origins: [...new Set((search.evidence ?? []).map(item => item.excerptSource ?? null))],
    searchCards: (searchMeta.sources ?? []).map(card => ({
      fileName: card.fileName, origin: card.citationOrigin,
      pdfPage: card.location?.pdfPage ?? null, pdfPageEnd: card.location?.pdfPageEnd ?? null,
      pageSource: card.location?.pageSource ?? null,
      lines: card.location ? `${card.location.startLine}-${card.location.endLine}` : null,
      locationVerified: card.status?.locationVerified ?? null,
      identity: card.status?.identity ?? null, support: card.status?.support ?? null,
      truncated: card.truncated, sourceVersion: card.sourceVersion,
      excerptChars: typeof card.excerpt === 'string' ? card.excerpt.length : 0,
    })),
    versionAgreement: null,
    read: null,
    problems: [],
  };

  // The citation carries a short reference, not the long signed handle: the plugin strips the token so a model
  // has nothing long to mistranscribe, and resolves the reference back to it when the read is dispatched.
  const cited = (search.evidence ?? []).find(item => item.readRef);
  if (!cited) {
    report.problems.push('no citation carried a readRef, so the chain cannot continue');
  } else {
    const read = await tools.get('knowledge_read').execute(
      { libraryId: options.library, ref: cited.readRef, sourceVersion: cited.sourceVersion,
        startLine: cited.startLine, maxChars: 900, maxLines: 20 }, exec);
    report.read = {
      pdfPage: read.pdfPage, pageSource: read.pageSource, pageHint: read.pageHint ?? null,
      startLine: read.startLine, endLine: read.endLine, sourceVersion: read.sourceVersion,
      locationVerified: read.locationVerified,
      excerptChars: typeof read.excerpt === 'string' ? read.excerpt.length : 0,
      excerptNonEmpty: typeof read.excerpt === 'string' && read.excerpt.length > 0,
      support: read.sourceCard?.status?.support ?? null,
      relativePath: read.relativePath,
    };
    // The same file version has to appear on the citation, on the card and on the read window.
    const card = (searchMeta.sources ?? []).find(source => source.relativePath === cited.relativePath);
    report.versionAgreement = {
      citation: cited.sourceVersion,
      card: card?.sourceVersion ?? null,
      read: read.sourceVersion,
      relativePath: cited.relativePath,
      readPath: read.relativePath,
      sameFile: cited.relativePath === read.relativePath,
      sameVersion: cited.sourceVersion === read.sourceVersion && card?.sourceVersion === read.sourceVersion,
      pageMatchesCitationRange: card?.location
        ? cited.startLine >= card.location.startLine && cited.startLine <= card.location.endLine
        : null,
    };
    if (!report.versionAgreement.sameVersion) report.problems.push('the citation, card and read disagree on the source version');
    if (!report.versionAgreement.sameFile) report.problems.push('the citation and the read name different files');
    if (!report.read.locationVerified) report.problems.push('the read window did not verify its own location');
  }

  if (options.out) writeFileSync(options.out, Buffer.from(JSON.stringify(report, null, 2), 'utf8'));
  console.log(JSON.stringify(report, null, 2));
}

await main();
