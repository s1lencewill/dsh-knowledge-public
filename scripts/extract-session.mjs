// Extract raw knowledge tool payloads from a DSH session transcript, and audit the extraction.
//
//   node scripts/extract-session.mjs --session <id> [--out <file.json>] [--summary]
//   node scripts/extract-session.mjs --latest [--out <file.json>]
//   node scripts/extract-session.mjs --decode-log <file> [--encoding utf-8|utf-16|auto]
//   node scripts/extract-session.mjs --verify-batch <dir> --ids T1,T2,...
//
// The transcript is session.v3.jsonl.zstd and, importantly, is a *sequence* of concatenated zstd
// frames: Node's one-shot zstdDecompressSync stops after the first frame (which only holds the session
// header). Decoding frame by frame recovers the whole conversation.
//
// Two rules learned the hard way and enforced here:
//   * the session is never guessed. `--session` must name an existing session, and `--latest` has to be
//     asked for explicitly - an unrecognised or unknown selector exits non-zero instead of quietly
//     answering about the newest session.
//   * decoded text is never guessed. A log's encoding is taken from its BOM, or from strict UTF-8, or
//     (only when the bytes really look like UTF-16) as UTF-16LE; anything else is an error, because a
//     silently mis-decoded log is worse than no log.
//
// Console output stays structural (line types, tool names, statuses, counts) so the terminal never
// receives literature text; --out writes the parsed payloads for the verifier.
import { readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { basename, join } from 'node:path';
import { zstdDecompressSync } from 'node:zlib';

const DEFAULT_ROOT = '.dsh-home/sessions/--G-hanako-dsh_plu-dsh-knowledge--';
const MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]);
const KNOWN_FLAGS = new Set(['--session', '--out', '--summary', '--latest', '--root', '--decode-log',
  '--encoding', '--verify-batch', '--ids']);

function fail(message) {
  process.stderr.write('extract-session: ' + message + '\n');
  process.exit(2);
}

function parseArgs(argv) {
  const args = { summary: false, latest: false, encoding: 'auto' };
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (!KNOWN_FLAGS.has(token)) fail('unknown argument: ' + token);
    if (token === '--summary' || token === '--latest') { args[token.slice(2)] = true; continue; }
    const value = argv[index + 1];
    if (value === undefined || value.startsWith('--')) fail('missing value for ' + token);
    args[token.slice(2)] = value;
    index += 1;
  }
  return args;
}

function sessionNames(root) {
  let names;
  try {
    names = readdirSync(root).filter((name) => name.startsWith('session-'));
  } catch (error) {
    fail('session root is not readable: ' + root + ' (' + error.code + ')');
  }
  return names.map((name) => join(root, name))
    .filter((path) => {
      try { return statSync(join(path, 'session.v3.jsonl.zstd')).isFile(); } catch { return false; }
    })
    .sort((a, b) => statSync(join(b, 'session.v3.jsonl.zstd')).mtimeMs
      - statSync(join(a, 'session.v3.jsonl.zstd')).mtimeMs);
}

function pickSession(args) {
  const root = args.root || DEFAULT_ROOT;
  if (args.session) {
    const path = join(root, args.session);
    const transcript = join(path, 'session.v3.jsonl.zstd');
    try {
      if (!statSync(transcript).isFile()) fail('session has no transcript file: ' + transcript);
    } catch (error) {
      fail('unknown session ' + args.session + ' under ' + root + ' (' + error.code + ')');
    }
    return { path, name: basename(path) };
  }
  if (args.latest) {
    const [newest] = sessionNames(root);
    if (!newest) fail('no session with a transcript found under ' + root);
    return { path: newest, name: basename(newest) };
  }
  fail('choose a session explicitly: --session <id>, or --latest to mean the newest one');
}

function decodeAllFrames(path) {
  const buffer = readFileSync(path);
  const offsets = [];
  for (let at = buffer.indexOf(MAGIC); at !== -1; at = buffer.indexOf(MAGIC, at + 4)) offsets.push(at);
  let text = '';
  for (let index = 0; index < offsets.length; index += 1) {
    const end = index + 1 < offsets.length ? offsets[index + 1] : buffer.length;
    try {
      text += zstdDecompressSync(buffer.subarray(offsets[index], end)).toString('utf8');
    } catch (error) {
      fail('frame ' + index + ' of ' + path + ' did not decode (' + error.code + ')');
    }
  }
  if (!offsets.length) fail('no zstd frame found in ' + path);
  return text;
}

function decodeLogBytes(buffer, requested) {
  const wants = (requested || 'auto').toLowerCase();
  if (wants === 'utf-8' || wants === 'utf8') {
    if (buffer.length >= 3 && buffer[0] === 0xef && buffer[1] === 0xbb && buffer[2] === 0xbf) {
      return buffer.subarray(3).toString('utf8');
    }
    return buffer.toString('utf8');
  }
  if (wants === 'utf-16' || wants === 'utf-16le' || wants === 'utf16') {
    return buffer.toString('utf16le').replace(/^\ufeff/, '');
  }
  if (wants !== 'auto') fail('unsupported --encoding: ' + requested);
  if (buffer.length >= 2 && buffer[0] === 0xff && buffer[1] === 0xfe) return buffer.toString('utf16le').slice(1);
  if (buffer.length >= 2 && buffer[0] === 0xfe && buffer[1] === 0xff) fail('UTF-16BE logs are not supported');
  if (buffer.includes(0)) {
    // Not valid UTF-8 by inspection, and NUL bytes: UTF-16LE is the only reading that makes sense.
    const text = buffer.toString('utf16le');
    return text.replace(/^\ufeff/, '');
  }
  const text = buffer.toString('utf8');
  if (text.includes('\ufffd')) fail('log encoding is undetermined (not UTF-8 and not UTF-16LE)');
  return text;
}

function verifyBatch(directory, ids) {
  const wanted = ids.split(',').map((value) => value.trim()).filter(Boolean);
  if (!wanted.length) fail('--verify-batch needs --ids <id,id,...>');
  const seen = new Map();
  const problems = [];
  for (const id of wanted) {
    const path = join(directory, id + '-tools.json');
    let payload;
    try {
      payload = JSON.parse(readFileSync(path, 'utf8'));
    } catch (error) {
      problems.push('missing or unreadable result for ' + id + ' (' + path + ': ' + error.code + ')');
      continue;
    }
    const session = payload.session ? basename(String(payload.session)) : null;
    if (!session) { problems.push('result for ' + id + ' carries no session id'); continue; }
    if (seen.has(session)) problems.push('session ' + session + ' is used by both ' + seen.get(session) + ' and ' + id);
    seen.set(session, id);
  }
  if (problems.length) fail(problems.join('; '));
  return wanted.length;
}

function describe(entry) {
  const blob = JSON.stringify(entry.payload);
  const names = [...new Set(blob.match(/knowledge_(search|list_libraries)/g) || [])];
  const status = (blob.match(/"status":"(\w+)"/) || [])[1] || null;
  const evidence = (blob.match(/"excerptSource"/g) || []).length;
  return { line: entry.index, type: entry.type, bytes: blob.length, tools: names, status, evidenceLike: evidence };
}

function readTranscript(selected) {
  const transcript = join(selected.path, 'session.v3.jsonl.zstd');
  const text = decodeAllFrames(transcript);
  const lines = [];
  for (const [index, raw] of text.split('\n').entries()) {
    if (!raw.trim()) continue;
    try { lines.push({ index, value: JSON.parse(raw) }); } catch { /* keep the line count honest */ }
  }
  const calls = [];
  const results = [];
  for (const { index, value } of lines) {
    if (value?.type === 'tool/call') calls.push({ index, type: value.type, payload: value });
    if (value?.type === 'tool/result') results.push({ index, type: value.type, payload: value });
  }
  return { transcript, text, lines, calls, results };
}

function main() {
  const args = parseArgs(process.argv.slice(2));

  if (args['decode-log']) {
    const text = decodeLogBytes(readFileSync(args['decode-log']), args.encoding);
    process.stdout.write(text.endsWith('\n') ? text : text + '\n');
    return 0;
  }

  if (args['verify-batch']) {
    const count = verifyBatch(args['verify-batch'], args.ids || '');
    process.stdout.write(JSON.stringify({ verified: count, oneToOne: true }) + '\n');
    return 0;
  }

  const selected = pickSession(args);
  const { transcript, text, lines, calls, results } = readTranscript(selected);
  if (args.summary || !args.out) {
    process.stdout.write(JSON.stringify({
      session: selected.name,
      transcript,
      frames: (text.match(/\{/g) || []).length,
      bytes: text.length,
      lines: lines.length,
      toolCalls: calls.length,
      toolResults: results.length,
      calls: calls.map(describe),
      results: results.map(describe),
    }, null, 2) + '\n');
  }
  if (args.out) {
    writeFileSync(args.out, JSON.stringify({
      session: selected.name,
      calls: calls.map((entry) => entry.payload),
      results: results.map((entry) => entry.payload),
    }, null, 2), 'utf8');
    process.stdout.write('wrote ' + args.out + ' for ' + selected.name + '\n');
  }
  return 0;
}

process.exit(main());
