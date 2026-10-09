// Read windows report code point offsets; JavaScript slices UTF-16 code units. This drives the real
// Python reader, hands its JSON to Node, and checks that the window can be rebuilt exactly.
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { codePointToUtf16Index, excerptFromOffsets } from '../scripts/excerpt-offsets.mjs';

const PYTHON = process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python';

// Python: build a BOM + CRLF file with non-BMP characters, read two windows, hand both back as JSON.
const PY = `
import json, pathlib, sys
sys.path.insert(0, 'engine')
import reader
from evidence import digest, normalised_text
base = pathlib.Path(sys.argv[1])
root = base / 'library'; root.mkdir(parents=True, exist_ok=True)
body = ('第一行 emoji 😀 与 𠀀 结束。\\n第二行：数值 3.0\\n第三行带着很长的尾巴' + '长尾abc' * 40 + '\\n第四行结束。\\n') * 2
data = b'\\xef\\xbb\\xbf' + body.replace('\\n', '\\r\\n').encode('utf-8')
(root / 'source.txt').write_bytes(data)
secret = reader.derive_secret('x' * 24)
handle = reader.issue_handle(secret, str(root), 'fixture', 'source.txt', digest(data))
window = reader.read(secret, str(root), 'fixture', root, handle=handle, max_chars=120)
nxt = reader.read(secret, str(root), 'fixture', root, cursor=window['nextCursor'], max_chars=120)
# Written as UTF-8 rather than printed: a piped stdout on this host is the ANSI code page.
(base / 'window.json').write_text(json.dumps(
    {'text': normalised_text(data), 'window': window, 'next': nxt}, ensure_ascii=False), encoding='utf-8')
`;

function readWindows() {
  const base = mkdtempSync(join(tmpdir(), 'read-window-'));
  try {
    const result = spawnSync(PYTHON, ['-c', PY, base], { encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    return JSON.parse(readFileSync(join(base, 'window.json'), 'utf8'));
  } finally {
    rmSync(base, { recursive: true, force: true });
  }
}

test('a read window rebuilds exactly, and a naive slice of its offsets does not', () => {
  const { text, window, next } = readWindows();
  assert.ok(text.length > [...text].length, 'the fixture must contain characters outside the BMP');
  assert.equal(excerptFromOffsets(text, window), window.excerpt);
  assert.equal([...window.excerpt].length, window.endChar - window.startChar,
               'the reported span is counted in code points');
  // The first window starts at the file head, so its start needs no conversion - but the emoji inside
  // it makes the raw UTF-16 slice land short, and every later offset is shifted further.
  assert.equal(window.startChar, 0);
  assert.ok(codePointToUtf16Index(text, window.endChar) > window.endChar,
            'non-BMP characters inside the span shift the UTF-16 end');
  assert.notEqual(text.slice(window.startChar, window.endChar), window.excerpt);
  assert.ok(codePointToUtf16Index(text, next.startChar) > next.startChar,
            'non-BMP characters before the window shift its UTF-16 start');
  assert.notEqual(text.slice(next.startChar, next.endChar), next.excerpt);
  assert.equal(excerptFromOffsets(text, next), next.excerpt);
});

test('consecutive windows stay contiguous in code point space', () => {
  const { text, window, next } = readWindows();
  assert.equal(next.startChar, window.nextStartChar);
  const gap = [...text].slice(window.endChar, next.startChar).join('');
  assert.ok(gap === '' || gap === '\n', 'only the line separator may sit between two windows');
  assert.equal(excerptFromOffsets(text, next), next.excerpt);
  assert.equal(next.midLineStart, gap === '');
});
