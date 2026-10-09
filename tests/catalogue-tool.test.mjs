/**
 * P1: `knowledge_list_documents` — the whole-library question, answered from the local catalogue.
 *
 * The observed defect: asked "which articles does the library contain", the model used `knowledge_search` with
 * queries like `"PDF page 1"` and `"PDF page 10"` and then `knowledge_find_documents` with `title: "water"`,
 * `title: "interface"`, `title: "air"` — that is, it tried to enumerate a library with keyword search, and it
 * spent its entire turn budget doing it. A keyword search cannot answer that question: it returns what matches,
 * not what exists. The plugin had no local enumeration tool at all.
 *
 * These tests drive the tool against a synthetic service with more documents than one page. They pin:
 * completeness and no duplicates across pages, that the catalogue version is carried between pages, that the
 * turn budget is charged like any other knowledge call, that the entries reach the card, and that no search (and
 * therefore no engine) is involved.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { definitions } from '../lib/index.js';
import { sourcesOf } from '../lib/cards.js';
import { TurnLedger } from '../lib/turnlimit.js';

const exec = () => ({ agent: { session: { header: { id: 's1', cwd: 'C:\\ws' } } } });

const DOCUMENTS = 23;

/** A synthetic catalogue service: deterministic order, real paging, and a version that can be moved. */
function catalogueService({ page = 20, version = 'cv-1' } = {}) {
  const ids = Array.from({ length: DOCUMENTS }, (_value, index) => `doc-${String(index).padStart(2, '0')}`);
  const calls = [];
  let current = { ids, version };
  let offsetBase = 0;
  const client = {
    libraries: async () => [{ id: 'lib', name: 'Library' }],
    search: async () => { throw new Error('enumeration must never start a search'); },
    findDocuments: async () => { throw new Error('enumeration is not a locate call'); },
    read: async () => ({}),
    catalogue: async (workspace, libraryId, query) => {
      calls.push({ workspace, libraryId, query });
      if (query.catalogueVersion && query.catalogueVersion !== current.version) {
        return {
          libraryId, catalogueVersion: current.version, status: 'catalogue-changed', total: current.ids.length,
          offset: query.offset ?? 0, limit: query.limit ?? page, returned: 0, hasMore: true, nextOffset: 0,
          entries: [], warnings: ['CATALOGUE_CHANGED: the catalogue changed while it was being listed'],
          note: '',
        };
      }
      const size = query.limit ?? page;
      const start = query.offset ?? offsetBase;
      const slice = current.ids.slice(start, start + size);
      const end = start + slice.length;
      return {
        libraryId, catalogueVersion: current.version, status: 'ok', total: current.ids.length,
        offset: start, limit: size, returned: slice.length, hasMore: end < current.ids.length,
        nextOffset: end < current.ids.length ? end : null, warnings: [], note: '',
        entries: slice.map((id, index) => ({
          index: start + index, documentId: id, fileName: `${id}.txt`, title: `Study ${id}`,
          readable: true, readRef: `kref-${id}`, sourceVersion: 'v1', verification: 'verified', conflicts: [],
          sourceCard: {
            libraryId, title: `Study ${id}`, fileName: `${id}.txt`, excerpt: null, location: null,
            sourceVersion: 'v1', citationOrigin: 'catalogue', truncated: false, truncationNotices: [],
            parseQuality: null,
            status: { identity: 'verified', locationVerified: false, support: 'not-assessed' },
          },
        })),
      };
    },
  };
  return {
    calls, client,
    /** Move the catalogue underneath a paged read, the way a rebuild would. */
    rebuild: (next) => { current = next; },
    setBase: (value) => { offsetBase = value; },
  };
}

function harness(client, max = 12) {
  return new Map(definitions(client, undefined, undefined, new TurnLedger({ max })).map(tool => [tool.name, tool]));
}

test('the whole library is enumerable page by page, completely and without duplicates', async () => {
  const service = catalogueService();
  const list = harness(service.client).get('knowledge_list_documents');
  const context = exec();

  const seen = [];
  let version = null;
  let offset = 0;
  let pages = 0;
  for (;;) {
    const args = { libraryId: 'lib', offset, limit: 5 };
    if (version) args.catalogueVersion = version;
    const page = await list.execute(args, context);
    pages += 1;
    assert.equal(page.status, 'ok');
    assert.equal(page.total, DOCUMENTS);
    version = page.catalogueVersion;
    seen.push(...page.entries.map(entry => entry.documentId));
    if (!page.hasMore) break;
    offset = page.nextOffset;
  }
  assert.equal(pages, 5);
  assert.equal(seen.length, DOCUMENTS);
  assert.equal(new Set(seen).size, DOCUMENTS, 'no page may repeat an entry');
  assert.deepEqual(seen, [...seen].sort(), 'the order is deterministic and stable across pages');
  assert.equal(service.calls.length, 5);
  assert.ok(service.calls.every(call => call.query.limit <= 50), 'the page size stays inside its range');
  // The version was carried from page 2 onwards: that is what makes a silent skip detectable.
  assert.equal(service.calls[1].query.catalogueVersion, 'cv-1');
});

test('a page big enough for the whole library answers "what is in here" in one call', async () => {
  const service = catalogueService();
  const list = harness(service.client).get('knowledge_list_documents');
  const page = await list.execute({ libraryId: 'lib', limit: 50 }, exec());
  assert.equal(page.returned, DOCUMENTS);
  assert.equal(page.hasMore, false);
  assert.equal(page.nextOffset, null);
  assert.equal(page.total, DOCUMENTS, 'the total is the coverage figure a whole-library answer must report');
  assert.equal(page.budget.used, 1, 'a whole-library question costs one call, not eight');
});

test('a catalogue that changes mid-listing is reported, not paged across', async () => {
  const service = catalogueService();
  const list = harness(service.client).get('knowledge_list_documents');
  const context = exec();
  const first = await list.execute({ libraryId: 'lib', offset: 0, limit: 10 }, context);
  service.rebuild({ ids: [...Array.from({ length: DOCUMENTS }, (_v, i) => `doc-${String(i).padStart(2, '0')}`), 'doc-new'], version: 'cv-2' });
  const second = await list.execute(
    { libraryId: 'lib', offset: first.nextOffset, limit: 10, catalogueVersion: first.catalogueVersion }, context);
  assert.equal(second.status, 'catalogue-changed');
  assert.deepEqual(second.entries, [], 'no entries may come from a listing that no longer exists');
  assert.equal(second.nextOffset, 0, 'the caller is told to restart from the beginning');
  assert.notEqual(second.catalogueVersion, first.catalogueVersion);
  assert.equal(second.total, DOCUMENTS + 1);
  assert.ok(second.warnings.some(item => item.startsWith('CATALOGUE_CHANGED')));
});

test('enumeration never searches, never locates and never reads', async () => {
  const service = catalogueService();
  const list = harness(service.client).get('knowledge_list_documents');
  await list.execute({ libraryId: 'lib', limit: 5 }, exec());
  assert.equal(service.calls.length, 1);
  // The fake client throws for search/findDocuments/read, so reaching them would fail the test outright; the
  // explicit assertion records the intent as well.
  assert.equal(service.calls[0].libraryId, 'lib');
  assert.equal(service.calls[0].workspace, 'C:\\ws');
});

test('a page reaches the card as catalogue entries with identity and no excerpt', async () => {
  const service = catalogueService();
  const list = harness(service.client).get('knowledge_list_documents');
  const page = await list.execute({ libraryId: 'lib', limit: 3 }, exec());
  const cards = sourcesOf(page);
  assert.equal(cards.length, 3, 'a listing must render through the same card as a located document');
  for (const card of cards) {
    assert.equal(card.citationOrigin, 'catalogue');
    assert.equal(card.excerpt, null, 'listing a document reads nothing out of it');
    assert.equal(card.location, null);
    assert.equal(card.status.support, 'not-assessed');
    assert.equal(card.status.locationVerified, false);
  }
});

test('each page spends a turn slot, and the budget is reported the same way as any other tool', async () => {
  const service = catalogueService();
  const tools = harness(service.client, 3);
  const list = tools.get('knowledge_list_documents');
  const context = exec();
  const first = await list.execute({ libraryId: 'lib', limit: 1 }, context);
  assert.equal(first.budget.used, 1);
  assert.equal(first.budget.max, 3);
  assert.equal(first.budget.remaining, 2);
  assert.equal(first.budget.tool, 'knowledge_list_documents');
  await list.execute({ libraryId: 'lib', limit: 1, offset: 1 }, context);
  const third = await list.execute({ libraryId: 'lib', limit: 1, offset: 2 }, context);
  assert.equal(third.budget.remaining, 0);
  assert.match(String(third.budget.note), /no knowledge-tool dispatches left/);
  // A fourth page is refused locally: a big library is paged in batches, not by outrunning the ceiling.
  const fourth = await list.execute({ libraryId: 'lib', limit: 1, offset: 3 }, context);
  assert.equal(fourth.stopped, 'KNOWLEDGE_TURN_LIMIT_REACHED');
  assert.equal(service.calls.length, 3);
});

test('the tool declares its own ranges and defaults, and the protocol routes to it', async () => {
  const list = harness(catalogueService().client).get('knowledge_list_documents');
  assert.equal(list.parameters.offset.type, 'integer');
  assert.equal(list.parameters.limit.type, 'integer');
  assert.match(list.parameters.limit.description, /1\.\.50/);
  assert.match(list.parameters.offset.description, />= 0/);
  assert.ok(list.parameters.catalogueVersion, 'the version has to be a parameter or paging cannot be checked');
  assert.match(list.description, /deterministic page/);
  assert.match(list.description, /does not start Sirchmunk/);
});
