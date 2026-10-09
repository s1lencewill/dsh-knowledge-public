/**
 * P1: the short read reference.
 *
 * The observed failure that motivated this: a real session's model called `knowledge_read` with a handle that
 * had never been issued. Its decode showed every field identical to an issued handle's except the relative
 * path, and — decisively — **the signature segment was byte-identical** while the body was not, so the
 * signature could not have covered the body it was presented with. That is the shape of a long value
 * mistranscribed, not of a deleted file, and the plugin had folded the service's `READ_HANDLE_INVALID` into a
 * single card that read as "the original is no longer available".
 *
 * What these tests pin:
 *
 * 1. no long signed token ever leaves a tool result — the model has nothing long to mistranscribe;
 * 2. a reference resolves **back to the exact original token**, so the service's authentication, library
 *    re-check and version check are untouched;
 * 3. a reference is bound to one session, workspace and library, is process-local, and is bounded. A reference
 *    that is unknown, foreign, evicted or from a previous plugin life is **refused**, never re-pointed at
 *    anything — because "resolve the reference to whatever file now looks close enough" is how a copy error
 *    would become a citation for a different document;
 * 4. the long spellings still work, so transcripts and clients written before this change keep working.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { KnowledgeServiceError } from '../lib/failures.js';
import { definitions } from '../lib/index.js';
import { MAX_REFERENCES, ReferenceStore, isReferenceId } from '../lib/refs.js';

const LONG_HANDLE = 'krh1.' + 'A'.repeat(258) + '.sGi33dJc0wS6eJc5Bq0PwQ2nJ9xWz1rT4vY7bN0mK8h';
const LONG_CURSOR = 'krc1.' + 'B'.repeat(258) + '.Zz11dJc0wS6eJc5Bq0PwQ2nJ9xWz1rT4vY7bN0mK8h';
const LONG_HANDLE_B = 'krh1.' + 'C'.repeat(258) + '.Qq22dJc0wS6eJc5Bq0PwQ2nJ9xWz1rT4vY7bN0mK8h';

const exec = (sessionId = 's1', cwd = 'C:\\ws') => ({
  agent: { session: { header: { id: sessionId, cwd } } },
});

/** The longest run of characters in a value that looks like a signed token. */
function longestToken(value) {
  const text = JSON.stringify(value);
  const tokens = text.match(/kr[hc]\d\.[A-Za-z0-9_-]{40,}\.[A-Za-z0-9_-]{40,}/g) ?? [];
  return tokens.length ? tokens[0].length : 0;
}

// ------------------------------------------------------------------ the store on its own

test('a reference is short, opaque, and resolves to the exact token it was issued for', () => {
  const store = new ReferenceStore();
  const scope = { workspace: 'C:\\ws', libraryId: 'lib', session: 's1' };
  const id = store.issue({ kind: 'handle', token: LONG_HANDLE, ...scope, sourceVersion: 'v1' });
  assert.ok(isReferenceId(id));
  assert.ok(id.length <= 32, `a reference must be short enough to copy; got ${id.length}`);
  assert.ok(!id.includes('.') || id.startsWith('kref-'), 'a reference must not look like a handle');
  assert.equal(store.resolve(id, scope).ok, true);
  assert.equal(store.resolve(id, scope).target.token, LONG_HANDLE, 'the token is preserved byte for byte');
  assert.equal(store.resolve(id, scope).target.kind, 'handle');
});

test('the same token in the same scope re-uses its reference instead of filling the store', () => {
  const store = new ReferenceStore();
  const scope = { workspace: 'w', libraryId: 'lib', session: 's1' };
  const first = store.issue({ kind: 'handle', token: LONG_HANDLE, ...scope, sourceVersion: null });
  const second = store.issue({ kind: 'handle', token: LONG_HANDLE, ...scope, sourceVersion: null });
  assert.equal(first, second);
  assert.equal(store.size, 1);
  // A different scope is a different reference: sharing one would share a read permission across a boundary.
  const other = store.issue({ kind: 'handle', token: LONG_HANDLE, workspace: 'w', libraryId: 'lib', session: 's2', sourceVersion: null });
  assert.notEqual(first, other);
  assert.equal(store.size, 2);
});

test('a reference from another session, workspace or library is refused, never re-pointed', () => {
  const store = new ReferenceStore();
  const id = store.issue({ kind: 'handle', token: LONG_HANDLE, workspace: 'w', libraryId: 'lib', session: 's1', sourceVersion: null });
  assert.deepEqual(store.resolve(id, { workspace: 'w', libraryId: 'lib', session: 's2' }),
    { ok: false, reason: 'foreign' });
  assert.deepEqual(store.resolve(id, { workspace: 'other', libraryId: 'lib', session: 's1' }),
    { ok: false, reason: 'foreign' });
  assert.deepEqual(store.resolve(id, { workspace: 'w', libraryId: 'other', session: 's1' }),
    { ok: false, reason: 'foreign' });
  // ...and the refusal did not consume or move it: the rightful scope still resolves.
  assert.equal(store.resolve(id, { workspace: 'w', libraryId: 'lib', session: 's1' }).ok, true);
});

test('a tampered or unknown reference resolves to nothing at all', () => {
  const store = new ReferenceStore();
  const scope = { workspace: 'w', libraryId: 'lib', session: 's1' };
  const id = store.issue({ kind: 'handle', token: LONG_HANDLE, ...scope, sourceVersion: null });
  const mutated = id.slice(0, -1) + (id.endsWith('a') ? 'b' : 'a');
  assert.deepEqual(store.resolve(mutated, scope), { ok: false, reason: 'unknown' });
  assert.deepEqual(store.resolve('kref-00000000', scope), { ok: false, reason: 'unknown' });
  assert.deepEqual(store.resolve('krh1.x.y', scope), { ok: false, reason: 'unknown' },
    'a handle is not a reference and is not silently accepted as one here');
  // The original is untouched by a failed lookup.
  assert.equal(store.resolve(id, scope).ok, true);
});

test('the store is bounded and evicts oldest-first, and clearing it forgets everything', () => {
  let counter = 0;
  const store = new ReferenceStore(3, () => `kref-${String(++counter).padStart(8, '0')}`);
  const scope = { workspace: 'w', libraryId: 'lib', session: 's1' };
  const ids = ['a', 'b', 'c', 'd'].map((token) => store.issue({ kind: 'handle', token, ...scope, sourceVersion: null }));
  assert.equal(store.size, 3);
  assert.deepEqual(store.resolve(ids[0], scope), { ok: false, reason: 'unknown' }, 'the oldest is evicted');
  assert.equal(store.resolve(ids[3], scope).ok, true);
  store.clear();
  assert.equal(store.size, 0);
  assert.deepEqual(store.resolve(ids[3], scope), { ok: false, reason: 'unknown' },
    'a cleared store is an empty store, not a store that guesses');
  assert.equal(MAX_REFERENCES, 500);
});

// ------------------------------------------------------------------ through the tool boundary

/** A client whose search hands out a long handle and whose read records what it was asked to read. */
function fakeClient(overrides = {}) {
  const reads = [];
  const searchResult = overrides.search ?? ({
    status: 'partial', answer: '', warnings: [], usage: null,
    evidence: [{
      id: 'e1', libraryId: 'lib', relativePath: 'texts/a.txt', sourceVersion: 'v1',
      startLine: 3, endLine: 4, excerpt: 'a verified quote', fileHandle: LONG_HANDLE,
    }],
  });
  return {
    reads,
    client: {
      libraries: async () => [{ id: 'lib', name: 'Library' }],
      // Deliberately the **real** shape of a query result: `{status, answer, evidence, warnings, usage}` with no
      // `libraryId`. The fixture used to add one, and that invented field is exactly why the suite passed while
      // the live search path handed the model a 320-character handle and no `readRef`: the transform was gated
      // on a field the real payload does not have. A fixture that is friendlier than production hides the bug
      // it is supposed to catch.
      // A caller may substitute the search payload; the default is the real shape (see the note above).
      search: async () => (typeof overrides.search === 'function' ? overrides.search() : searchResult),
      findDocuments: async () => ({
        libraryId: 'lib', warnings: [], note: '',
        documents: [{ documentId: 'd1', fileName: 'a.txt', readable: true, fileHandle: LONG_HANDLE_B,
          sourceVersion: 'v1', verification: 'verified', score: 1, matched: [], mismatched: [], conflicts: [] }],
      }),
      catalogue: async () => ({
        libraryId: 'lib', catalogueVersion: 'cv1', status: 'ok', total: 1, offset: 0, limit: 20,
        returned: 1, hasMore: false, nextOffset: null, warnings: [], note: '',
        entries: [{ index: 0, documentId: 'd1', fileName: 'a.txt', readable: true, fileHandle: LONG_HANDLE_B,
          sourceVersion: 'v1', verification: 'verified', conflicts: [] }],
      }),
      read: async (workspace, session, request) => {
        reads.push({ workspace, session, request });
        return {
          libraryId: 'lib', relativePath: 'texts/a.txt', fileHandle: LONG_HANDLE, sourceVersion: 'v1',
          startLine: 1, endLine: 5, startChar: 0, endChar: 20, startOffset: 0, endOffset: 20,
          excerpt: 'the original window', excerptComplete: true, endsAtLineBoundary: true,
          midLineStart: false, midLineStop: false, lineLimitReached: false, overlapsPrevious: false,
          hasMore: true, nextStartChar: 20, nextCursor: LONG_CURSOR, fileChars: 200, fileLines: 20,
          warnings: [], limits: { readsUsed: 1, readsLimit: 40, sessionCharsUsed: 20, sessionCharsLimit: 400000, sessionCharsRemaining: 399980 },
          ...overrides.read,
        };
      },
    },
  };
}

function harness(client) {
  return new Map(definitions(client).map(tool => [tool.name, tool]));
}

test('no long signed token ever leaves a tool result', async () => {
  const { client } = fakeClient();
  const tools = harness(client);
  const context = exec();

  const search = await tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'q' }, context);
  assert.equal(search.evidence[0].fileHandle, undefined, 'the model must not receive the long spelling');
  assert.ok(isReferenceId(search.evidence[0].readRef));
  assert.equal(longestToken(search), 0, 'no 300-character token may survive anywhere in the result');

  const found = await tools.get('knowledge_find_documents').execute({ libraryId: 'lib', title: 'a' }, context);
  assert.equal(found.documents[0].fileHandle, undefined);
  assert.ok(isReferenceId(found.documents[0].readRef));
  assert.equal(longestToken(found), 0);

  const listed = await tools.get('knowledge_list_documents').execute({ libraryId: 'lib' }, context);
  assert.equal(listed.entries[0].fileHandle, undefined);
  assert.ok(isReferenceId(listed.entries[0].readRef));
  assert.equal(longestToken(listed), 0);

  const read = await tools.get('knowledge_read').execute(
    { libraryId: 'lib', ref: search.evidence[0].readRef }, context);
  assert.equal(read.fileHandle, undefined);
  assert.equal(longestToken(read), 0);
  assert.ok(isReferenceId(read.nextRef), 'the continuation is a reference too');
  assert.equal(read.nextCursor, undefined);
});

test('a reference resolves back to the exact signed token, so nothing about verification is relaxed', async () => {
  const { client, reads } = fakeClient();
  const tools = harness(client);
  const context = exec();
  const search = await tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'q' }, context);
  const ref = search.evidence[0].readRef;

  const window = await tools.get('knowledge_read').execute(
    { libraryId: 'lib', ref, sourceVersion: 'v1', startLine: 1, maxChars: 2000 }, context);
  assert.equal(reads.length, 1);
  assert.equal(reads[0].request.fileHandle, LONG_HANDLE, 'the service still receives the signed handle');
  assert.equal(reads[0].request.cursor, undefined);
  assert.equal(reads[0].request.startLine, 1);
  assert.equal(reads[0].request.maxChars, 2000);
  assert.equal(reads[0].session, 's1', 'the read budget key is still the durable session id');
  assert.equal(window.excerpt, 'the original window');
});

test('a continuation reference resolves to the cursor, and the next window continues from it', async () => {
  const { client, reads } = fakeClient();
  const tools = harness(client);
  const context = exec();
  const first = await tools.get('knowledge_read').execute({ libraryId: 'lib', fileHandle: LONG_HANDLE }, context);
  assert.ok(isReferenceId(first.nextRef));
  await tools.get('knowledge_read').execute({ libraryId: 'lib', ref: first.nextRef }, context);
  assert.equal(reads[1].request.cursor, LONG_CURSOR, 'the cursor, not the handle, is what continues a window');
  assert.equal(reads[1].request.fileHandle, undefined);
});

test('search evidence is short-referenced even though a query result names no library', async () => {
  // The exact defect the recorded session exposed (turn 4): `knowledge_search` returned `fileHandle` and no
  // `readRef`, so the model had to carry a 320-character token into the next call - and the read it produced
  // was the only one in the session that needed a long handle. The scope is the *call's* library, not a field
  // the query route never returns.
  const { client } = fakeClient();
  const tools = harness(client);
  const summary = await tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'q' }, exec());
  assert.ok(!('libraryId' in summary), 'the fixture must stay in the real shape: no libraryId on a query result');
  assert.equal(summary.evidence[0].fileHandle, undefined, 'the long spelling must not reach the model');
  assert.ok(isReferenceId(summary.evidence[0].readRef), 'search evidence must carry a short reference');
  assert.equal(summary.evidence[0].readRefKind, 'document');
  assert.match(summary.referenceGuide, /readRef = document reference/);
  assert.equal(longestToken(summary), 0);
});

test('every emitted reference declares its kind, and the guide says which may take startLine', async () => {
  const { client } = fakeClient();
  const tools = harness(client);
  const context = exec();
  const search = await tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'q' }, context);
  assert.equal(search.evidence[0].readRefKind, 'document');
  const found = await tools.get('knowledge_find_documents').execute({ libraryId: 'lib', title: 'a' }, context);
  assert.equal(found.documents[0].readRefKind, 'document');
  const listed = await tools.get('knowledge_list_documents').execute({ libraryId: 'lib' }, context);
  assert.equal(listed.entries[0].readRefKind, 'document');
  const window = await tools.get('knowledge_read').execute({ libraryId: 'lib', ref: search.evidence[0].readRef }, context);
  assert.equal(window.readRefKind, 'document', 'a read window names its own file');
  assert.equal(window.nextRefKind, 'continuation', 'and the continuation it returns is a different kind');
  assert.match(window.referenceGuide, /must not be combined with `startLine`/);
});

test('a continuation reference combined with startLine is refused, and the document ref is offered', async () => {
  // The recorded session's other defect (turn 3): three calls asked for lines 200/335/105 with a `nextRef` and
  // were silently given 88/177/229, each with a warning. The conflict is decidable from the reference's own
  // kind, so it is refused here - before any request - and the refusal names the usable alternative.
  const { client, reads } = fakeClient();
  const tools = harness(client);
  const context = exec();
  const window = await tools.get('knowledge_read').execute({ libraryId: 'lib', fileHandle: LONG_HANDLE }, context);
  const documentRef = window.readRef;
  const continuationRef = window.nextRef;
  assert.ok(isReferenceId(documentRef) && isReferenceId(continuationRef));

  await assert.rejects(
    tools.get('knowledge_read').execute({ libraryId: 'lib', ref: continuationRef, startLine: 200 }, context),
    (error) => {
      assert.equal(error.code, 'KNOWLEDGE_REF_KIND_CONFLICT');
      assert.equal(error.record.detail.reason, 'CONTINUATION_REF_WITH_START_LINE');
      assert.equal(error.record.detail.suggestedAction, 'USE_DOCUMENT_REF');
      assert.equal(error.record.detail.parameter, 'startLine');
      assert.equal(error.record.detail.suggestedRef, documentRef,
        'the document reference issued beside the continuation is the recovery entry');
      assert.match(error.message, new RegExp(documentRef), 'the model is told which reference to use');
      assert.equal(longestToken(error.message), 0, 'and never a signed token');
      return true;
    });
  assert.equal(reads.length, 1, 'nothing was sent to the service for the refused call');

  // And both legitimate shapes still work: the continuation alone, and the document ref with a start line.
  await tools.get('knowledge_read').execute({ libraryId: 'lib', ref: continuationRef }, context);
  assert.equal(reads[1].request.cursor, LONG_CURSOR);
  assert.equal(reads[1].request.startLine, undefined);
  await tools.get('knowledge_read').execute({ libraryId: 'lib', ref: documentRef, startLine: 335 }, context);
  assert.equal(reads[2].request.fileHandle, LONG_HANDLE);
  assert.equal(reads[2].request.startLine, 335, 'a document reference with startLine still jumps');
});

test('a conflict whose document reference is gone says so instead of naming a dead reference', async () => {
  // The sibling is process-local like everything else. When it has been evicted, the refusal must not point at
  // it: the action changes to "drop startLine", and no reference is named.
  const refs = new ReferenceStore(2, (() => { let n = 0; return () => `kref-${String(++n).padStart(8, '0')}`; })());
  const { client, reads } = fakeClient();
  const tools = new Map(definitions(client, undefined, undefined, undefined, refs).map(tool => [tool.name, tool]));
  const context = exec();
  const window = await tools.get('knowledge_read').execute({ libraryId: 'lib', fileHandle: LONG_HANDLE }, context);
  const continuationRef = window.nextRef;
  // The store holds two (the document reference and its continuation); one more evicts the older document one
  // while leaving the continuation - exactly the state the refusal has to describe honestly.
  refs.issue({ kind: 'handle', token: LONG_HANDLE_B, libraryId: 'lib', workspace: 'C:\\ws', session: 's1', sourceVersion: 'v1' });

  await assert.rejects(
    tools.get('knowledge_read').execute({ libraryId: 'lib', ref: continuationRef, startLine: 105 }, context),
    (error) => {
      assert.equal(error.code, 'KNOWLEDGE_REF_KIND_CONFLICT');
      assert.equal(error.record.detail.suggestedAction, 'DROP_START_LINE');
      assert.equal(error.record.detail.suggestedRef, null);
      assert.match(error.message, /No document reference for that file is live in this session/);
      return true;
    });
  assert.equal(reads.length, 1);
});

test('every readable evidence item gets a reference, and nothing invents one for the rest', async () => {
  // The shapes a real search returns, in one fixture: two files, the same file twice (the engine can cite one
  // file in two ranges), an item with no handle at all, an item with a malformed handle, and an empty result.
  // The rule is the same for all of them: a reference is minted only for a handle the service actually issued,
  // and an item that has none is returned without one rather than with a fabricated pointer.
  const real = 'krh1.' + 'A'.repeat(258) + '.sGi33dJc0wS6eJc5Bq0PwQ2nJ9xWz1rT4vY7bN0mK8h';
  const other = 'krh1.' + 'D'.repeat(258) + '.sGi33dJc0wS6eJc5Bq0PwQ2nJ9xWz1rT4vY7bN0mK8h';
  const items = [
    { id: 'e1', libraryId: 'lib', relativePath: 'texts/a.txt', sourceVersion: 'v1', startLine: 1, endLine: 2, excerpt: 'q1', fileHandle: real },
    { id: 'e2', libraryId: 'lib', relativePath: 'texts/a.txt', sourceVersion: 'v1', startLine: 9, endLine: 11, excerpt: 'q2', fileHandle: real },
    { id: 'e3', libraryId: 'lib', relativePath: 'texts/b.txt', sourceVersion: 'v1', startLine: 3, endLine: 4, excerpt: 'q3', fileHandle: other },
    { id: 'e4', libraryId: 'lib', relativePath: 'texts/c.txt', sourceVersion: 'v1', startLine: 1, endLine: 1, excerpt: 'q4' },
    { id: 'e5', libraryId: 'lib', relativePath: 'texts/d.txt', sourceVersion: 'v1', startLine: 1, endLine: 1, excerpt: 'q5', fileHandle: 'not-a-handle' },
  ];
  const { client } = fakeClient({ search: async () => ({ status: 'partial', answer: '', warnings: [], usage: null, evidence: items }) });
  const tools = harness(client);
  const context = exec();
  const result = await tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'q' }, context);

  const refs = result.evidence.map(item => item.readRef);
  assert.ok(isReferenceId(refs[0]) && isReferenceId(refs[1]) && isReferenceId(refs[2]), 'each issued handle gets one');
  assert.equal(refs[0], refs[1], 'the same file and version under the same session re-uses one reference');
  assert.notEqual(refs[0], refs[2], 'a different file gets its own');
  assert.equal(refs[3], undefined, 'an item with no handle gets no reference');
  assert.equal(refs[4], undefined, 'and neither does one whose handle is not shaped like a handle');
  assert.equal(result.evidence[3].fileHandle, undefined, 'the (absent) long spelling is not re-invented');
  assert.equal(result.evidence[4].fileHandle, 'not-a-handle', 'a malformed value is left as the service sent it, not dropped silently');
  assert.equal(longestToken({ readRefs: refs.slice(0, 3) }), 0, 'and no long token is echoed in the references');

  // An empty result is a result: no references, no crash, and the guide is not attached to nothing.
  const empty = fakeClient({ search: async () => ({ status: 'partial', answer: '', warnings: [], usage: null, evidence: [] }) });
  const emptyResult = await harness(empty.client).get('knowledge_search').execute({ libraryId: 'lib', query: 'q' }, context);
  assert.deepEqual(emptyResult.evidence, []);
  assert.equal(emptyResult.referenceGuide, undefined);
});

test('a reference used from another session or library is refused with its own code', async () => {  const { client, reads } = fakeClient();
  const tools = harness(client);
  const search = await tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'q' }, exec('s1'));
  const ref = search.evidence[0].readRef;

  await assert.rejects(tools.get('knowledge_read').execute({ libraryId: 'lib', ref }, exec('s2')),
    (error) => error.code === 'KNOWLEDGE_READ_REF_FOREIGN');
  await assert.rejects(tools.get('knowledge_read').execute({ libraryId: 'other', ref }, exec('s1')),
    (error) => error.code === 'KNOWLEDGE_READ_REF_FOREIGN');
  await assert.rejects(tools.get('knowledge_read').execute({ libraryId: 'lib', ref }, exec('s1', 'C:\\elsewhere')),
    (error) => error.code === 'KNOWLEDGE_READ_REF_FOREIGN');
  assert.equal(reads.length, 0, 'a refused reference must not produce a service request');
});

test('an evicted, cleared or invented reference says "locate again" rather than resolving elsewhere', async () => {
  let counter = 0;
  const refs = new ReferenceStore(1, () => `kref-${String(++counter).padStart(8, '0')}`);
  const { client, reads } = fakeClient();
  const tools = new Map(definitions(client, undefined, undefined, undefined, refs).map(tool => [tool.name, tool]));
  const context = exec();

  const search = await tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'q' }, context);
  const firstRef = search.evidence[0].readRef;
  // A second search mints a second reference; the store holds one, so the first is evicted.
  await tools.get('knowledge_find_documents').execute({ libraryId: 'lib', title: 'a' }, context);
  await assert.rejects(tools.get('knowledge_read').execute({ libraryId: 'lib', ref: firstRef }, context),
    (error) => error.code === 'KNOWLEDGE_READ_REF_UNKNOWN');
  assert.equal(reads.length, 0);

  // A plugin reload (a new store) forgets everything: the honest answer is "re-locate", not a guess.
  refs.clear();
  const survived = (await tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'q' }, context)).evidence[0].readRef;
  refs.clear();
  await assert.rejects(tools.get('knowledge_read').execute({ libraryId: 'lib', ref: survived }, context),
    (error) => error.code === 'KNOWLEDGE_READ_REF_UNKNOWN');
  await assert.rejects(tools.get('knowledge_read').execute({ libraryId: 'lib', ref: 'kref-invented1' }, context),
    (error) => error.code === 'KNOWLEDGE_READ_REF_UNKNOWN');
  assert.equal(reads.length, 0);
});

test('a reference never masks a real service refusal: the failure keeps its own code', async () => {
  const cases = [
    ['READ_HANDLE_INVALID', 404, 'KNOWLEDGE_HANDLE_INVALID'],
    ['READ_HANDLE_FOREIGN', 403, 'KNOWLEDGE_HANDLE_INVALID'],
    ['READ_FILE_UNAVAILABLE', 404, 'KNOWLEDGE_SOURCE_UNAVAILABLE'],
    ['SOURCE_VERSION_CHANGED', 409, 'KNOWLEDGE_SOURCE_CHANGED'],
  ];
  for (const [serviceCode, status, expected] of cases) {
    const { client, reads } = fakeClient({ read: null });
    client.read = async () => { throw new KnowledgeServiceError(status, serviceCode); };
    const tools = harness(client);
    const context = exec();
    const search = await tools.get('knowledge_search').execute({ libraryId: 'lib', query: 'q' }, context);
    await assert.rejects(
      tools.get('knowledge_read').execute({ libraryId: 'lib', ref: search.evidence[0].readRef }, context),
      (error) => error.code === expected,
      `${serviceCode} must classify as ${expected}`);
    assert.equal(typeof reads, 'object');
  }
});

test('the long spellings still work, for transcripts and clients written before references existed', async () => {
  const { client, reads } = fakeClient();
  const tools = harness(client);
  const context = exec();
  await tools.get('knowledge_read').execute({ libraryId: 'lib', fileHandle: LONG_HANDLE }, context);
  assert.equal(reads[0].request.fileHandle, LONG_HANDLE);
  // handle and cursor together are left to the service, which checks that they agree.
  await tools.get('knowledge_read').execute({ libraryId: 'lib', fileHandle: LONG_HANDLE, cursor: LONG_CURSOR }, context);
  assert.equal(reads[1].request.fileHandle, LONG_HANDLE);
  assert.equal(reads[1].request.cursor, LONG_CURSOR);
  // A reference plus a long spelling is ambiguous and refused locally, with no request.
  await assert.rejects(
    tools.get('knowledge_read').execute({ libraryId: 'lib', ref: 'kref-aaaaaaaa', fileHandle: LONG_HANDLE }, context),
    (error) => error.code === 'KNOWLEDGE_REQUEST_REJECTED');
  assert.equal(reads.length, 2);
  // ...and no identifier at all is refused locally too.
  await assert.rejects(tools.get('knowledge_read').execute({ libraryId: 'lib' }, context),
    (error) => error.code === 'KNOWLEDGE_REQUEST_REJECTED');
  assert.equal(reads.length, 2);
});
