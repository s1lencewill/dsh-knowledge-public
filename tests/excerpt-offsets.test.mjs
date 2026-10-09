// The parent reports code point offsets; JavaScript slices UTF-16 code units. This test drives the real
// Python verifier, hands its JSON to Node, and checks both readings - including the one that misaligns.
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { excerptFromOffsets, codePointToUtf16Index, utf16ToCodePointIndex } from '../scripts/excerpt-offsets.mjs';

const PYTHON = process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python';

// A source with an emoji and a CJK extension B ideograph (both outside the BMP) before the quote, so the
// two numbering schemes differ, plus ordinary Chinese inside the quote.
const SOURCE = '开头 😀 𠀀 证据：体相主导，界面约 1%。\n第二行尾巴\n';

// Python: snapshot a temp library, verify an excerpt, print the evidence item as JSON.
const PY = `
import json, pathlib, sys, tempfile
sys.path.insert(0, 'engine')
from evidence import snapshot, verify
base = pathlib.Path(sys.argv[1])
root = base / 'library'; root.mkdir(parents=True, exist_ok=True)
(root / 'source.txt').write_text(sys.argv[2], encoding='utf-8')
manifest, _ = snapshot(root, base / 'snapshot')
key = next(iter(manifest))
item = verify(manifest, key, sys.argv[3], 'cross-lang')
text = (base / 'snapshot' / 'source.txt').read_text(encoding='utf-8-sig').replace('\\r\\n', '\\n')
# Handed back through a file, not stdout: a piped stdout uses the host ANSI code page (GBK on this
# host) and cannot encode the non-BMP characters this test deliberately carries.
(base / 'evidence.json').write_text(json.dumps({'item': item, 'text': text}, ensure_ascii=False), encoding='utf-8')
`;

function pythonEvidence(excerpt) {
  const base = mkdtempSync(join(tmpdir(), 'crosslang-'));
  try {
    const result = spawnSync(PYTHON, ['-c', PY, base, SOURCE, excerpt], { encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    return JSON.parse(readFileSync(join(base, 'evidence.json'), 'utf8'));
  } finally {
    rmSync(base, { recursive: true, force: true });
  }
}

test('offsets are code point based, and a naive JS slice misaligns on non-BMP text', () => {
  const { item, text } = pythonEvidence('证据：体相主导，界面约 1%');
  assert.equal(item.startChar, 7, 'Python counts 开(1) 头(2) space(3) emoji(4) space(5) 𠀀(6) space(7) …');
  // The emoji and the extension-B ideograph each take two UTF-16 units, so slicing the raw offsets lands late.
  const naive = text.slice(item.startChar, item.endChar);
  assert.notEqual(naive, item.excerpt, 'the raw offsets are not UTF-16 indices');
  // The reported span is measured in code points: this excerpt is all BMP, so its own counts agree, but the
  // *position* does not - the prefix's two non-BMP characters shift the UTF-16 index by two.
  assert.equal([...item.excerpt].length, item.endChar - item.startChar);
  assert.notEqual(codePointToUtf16Index(text, item.startChar), item.startChar,
                  'the position needs converting even when the excerpt itself is BMP');

  const rebuilt = excerptFromOffsets(text, item);
  assert.equal(rebuilt, item.excerpt);
  assert.equal(rebuilt, '证据：体相主导，界面约 1%');
  assert.equal(codePointToUtf16Index(text, item.startChar), 9);
  assert.equal(utf16ToCodePointIndex(text, 9), item.startChar);
});

test('a quote that starts before the non-BMP characters still rebuilds', () => {
  const { item, text } = pythonEvidence('开头 😀');
  assert.equal(item.startChar, 0);
  assert.equal(excerptFromOffsets(text, item), '开头 😀');
});

test('a quote that ends after a non-BMP character rebuilds, with a trailing partial line', () => {
  const quote = '𠀀 证据：体相主导，界面约';
  const { item, text } = pythonEvidence(quote);
  // 13 code points, 14 UTF-16 units: the leading 𠀀 is one code point but two units.
  assert.equal(item.endChar - item.startChar, [...quote].length);
  assert.equal(item.endChar - item.startChar, 13);
  assert.equal(quote.length, 14);
  assert.equal(excerptFromOffsets(text, item), quote);
  // The last line is partial (the quote stops before " 1%。"), which is exactly the case line numbers alone
  // cannot rebuild; the offsets can.
  assert.equal(item.startLine, 1);
  assert.equal(item.endLine, 1);
});

test('a mismatch between offsets and excerpt is reported, not silently returned', () => {
  const { item, text } = pythonEvidence('证据：体相主导，界面约 1%');
  const tampered = { ...item, endChar: item.endChar + 1 };
  assert.throws(() => excerptFromOffsets(text, tampered), /does not match/);
});

test('a code point offset past the end of the text is an error', () => {
  assert.throws(() => codePointToUtf16Index('abc', 99), /past the end/);
  assert.throws(() => codePointToUtf16Index('abc', -1), /non-negative/);
});
