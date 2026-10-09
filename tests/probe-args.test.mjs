// The browser probe's argument handling, tested without launching a browser.
//
// Why this file exists: a shell that rewrites or splits a native command's arguments turned an inline
// script and an inline eval into *silently shortened* inputs - the composer received only the last word,
// and an expression failed with a syntax error in the page. Both were found live, so both are pinned here.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';

import { parseArgs } from '../scripts/probe-host-panel.mjs';

const scratch = mkdtempSync(resolve(tmpdir(), 'probe-args-'));

test('a script given as a file keeps its line structure', () => {
  const file = resolve(scratch, 'script.txt');
  writeFileSync(file, 'click-text=新会话\ntypekeys=A B C\nenter=1\nwait=500\n');
  const options = parseArgs(['--script-file', file]);
  // One command per line becomes one `;`-separated command, in order.
  assert.equal(options.script, 'click-text=新会话;typekeys=A B C;enter=1;wait=500');
  // The value with spaces survives intact; this is the defect the file form exists to avoid.
  assert.deepEqual(options.script.split(';')[1], 'typekeys=A B C');
});

test('a script file is read after the arguments are parsed', () => {
  // The first version read `options.scriptFile` before the loop that sets it, so the run silently used
  // an empty script: no steps, no shots, and a report that still claimed success.
  const file = resolve(scratch, 'script2.txt');
  writeFileSync(file, 'wait=1\n');
  const options = parseArgs(['--out', 'somewhere', '--script-file', file]);
  assert.equal(options.script, 'wait=1');
  assert.equal(options.out, 'somewhere');
});

test('eval files are collected in order and referenced by index', () => {
  const options = parseArgs(['--eval-file', 'a.js', '--eval', 'inline()', '--eval-file', 'b.js']);
  assert.deepEqual(options.evalFiles, ['a.js', 'b.js']);
  assert.deepEqual(options.eval, ['inline()']);
});

test('an unknown flag is still an error', () => {
  assert.throws(() => parseArgs(['--shoot', 'typo']), /unknown argument/);
});

test('no script source means no script, rather than an empty one', () => {
  const options = parseArgs(['--url', 'http://127.0.0.1:1']);
  assert.equal(options.script, null);
  assert.equal(options.scriptFile, null);
  assert.deepEqual(options.evalFiles, []);
});
