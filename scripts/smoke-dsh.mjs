import assert from 'node:assert/strict';
import { apply } from '../lib/index.js';
// Uses actual installed defineTool; registry seam is simulated, not a running DSH session.
process.env.DSH_KNOWLEDGE_TOKEN = 'synthetic-token-for-sdk-smoke-only';
const registered = [];
apply({ tools: { register: definition => registered.push(definition) } });
assert.deepEqual(registered.map(value => value.name),
  ['knowledge_list_libraries', 'knowledge_search', 'knowledge_find_documents', 'knowledge_list_documents', 'knowledge_read']);
assert.ok(registered.every(value => typeof value.execute === 'function'));
const find = registered[2];
const list = registered[3];
const read = registered[4];
assert.ok(read.parameters.properties.ref, 'a read identifies its file with the short reference a result carried');
assert.ok(read.parameters.properties.fileHandle, 'the long spelling stays declared for older transcripts');
assert.ok(!read.parameters.required.includes('ref') && !read.parameters.required.includes('fileHandle'),
  'neither identifier spelling is schema-required: exactly one of the three is checked in the body');
assert.ok(read.parameters.properties.cursor, 'continuation goes through the server cursor');
assert.ok(list.parameters.properties.offset && list.parameters.properties.limit
  && list.parameters.properties.catalogueVersion,
'paged enumeration carries the catalogue version a page was taken from');
assert.ok(find.parameters.properties.doi && find.parameters.properties.title,
  'the locator takes identity clues and no path');
assert.ok(!find.parameters.properties.path && !find.parameters.properties.url,
  'the locator accepts no path or URL');
assert.ok(!read.parameters.properties.path && !read.parameters.properties.file, 'no path-shaped argument exists');
assert.ok(!read.parameters.properties.title && !find.parameters.required.includes('title'),
  'a document clue is never a substitute for the read identity');
// Every bounded numeric parameter is an integer with a stated range, because the SDK cannot declare one.
for (const [tool, name] of [[find, 'limit'], [list, 'offset'], [list, 'limit'], [read, 'maxChars'], [read, 'maxLines'],
  [read, 'overlapLines'], [read, 'startLine']]) {
  assert.equal(tool.parameters.properties[name].type, 'integer', `${tool.name}.${name} must be an integer`);
  assert.match(String(tool.parameters.properties[name].description), /\d/, `${tool.name}.${name} states its range`);
}
console.log('PASS: five tool definitions normalized with installed DSH SDK; live profile/UI not tested.');
