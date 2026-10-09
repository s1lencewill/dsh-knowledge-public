/** Offline inspection of the actual npm archive. No extraction, credentials or network. */
import { createHash } from 'node:crypto';
import { gunzipSync } from 'node:zlib';
import { readFileSync, readdirSync, lstatSync } from 'node:fs';
import { join, relative } from 'node:path';

export const MANIFEST_NAME = 'RELEASE-MANIFEST.json';
export const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const MAX_ARCHIVE_BYTES = 16_000_000;
const MAX_PAYLOAD_BYTES = 24_000_000;
const text = (bytes, start, size) => bytes.subarray(start, start + size).toString('utf8').replace(/\0.*$/s, '');
const octal = value => {
  if (!/^[0-7]*$/.test(value.trim())) throw new Error('INVALID_TAR_NUMBER');
  const number = parseInt(value.trim() || '0', 8);
  if (!Number.isSafeInteger(number)) throw new Error('INVALID_TAR_NUMBER');
  return number;
};
export function safeMember(name) {
  if (typeof name !== 'string' || !name.startsWith('package/')) throw new Error('INVALID_PACKAGE_PREFIX');
  const path = name.slice(8);
  if (!path || /[\\\x00-\x1f:]/.test(path) || path.startsWith('/') ||
      path.split('/').some(part => !part || part === '.' || part === '..' ||
        /[. ]$/.test(part) || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(part))) {
    throw new Error('UNSAFE_ARCHIVE_PATH');
  }
  return path;
}
function paxFields(bytes) {
  const fields = {};
  let offset = 0;
  while (offset < bytes.length) {
    const space = bytes.indexOf(32, offset);
    if (space < 0) throw new Error('INVALID_PAX');
    const raw = bytes.subarray(offset, space).toString('ascii');
    const size = /^\d+$/.test(raw) ? Number(raw) : 0;
    if (!size || offset + size > bytes.length || bytes[offset + size - 1] !== 10) throw new Error('INVALID_PAX');
    const value = bytes.subarray(space + 1, offset + size - 1).toString('utf8');
    const equals = value.indexOf('=');
    if (equals < 1) throw new Error('INVALID_PAX');
    fields[value.slice(0, equals)] = value.slice(equals + 1);
    offset += size;
  }
  return fields;
}
export function unpackArchive(compressed) {
  if (compressed.length > MAX_ARCHIVE_BYTES) throw new Error('ARCHIVE_TOO_LARGE');
  const tar = gunzipSync(compressed, { maxOutputLength: MAX_PAYLOAD_BYTES });
  const members = new Map(), folded = new Set();
  let pending = {}, offset = 0, ended = false;
  while (offset + 512 <= tar.length) {
    const header = tar.subarray(offset, offset + 512);
    if (header.every(byte => byte === 0)) { ended = true; break; }
    const sum = header.reduce((total, byte, index) => total + (index >= 148 && index < 156 ? 32 : byte), 0);
    if (sum !== octal(text(header, 148, 8))) throw new Error('TAR_CHECKSUM_MISMATCH');
    const size = octal(text(header, 124, 12));
    const type = String.fromCharCode(header[156] || 48);
    const start = offset + 512, end = start + size;
    if (end > tar.length) throw new Error('TRUNCATED_TAR_MEMBER');
    const bytes = tar.subarray(start, end);
    const prefix = text(header, 345, 155), name = text(header, 0, 100);
    if (type === 'x') {
      if (Object.keys(pending).length) throw new Error('NESTED_PAX');
      pending = paxFields(bytes);
      if ('linkpath' in pending || 'size' in pending) throw new Error('UNSUPPORTED_PAX_FIELD');
    } else {
      if (!['0', '5'].includes(type)) throw new Error('ARCHIVE_LINK_OR_SPECIAL_FILE');
      const full = pending.path || (prefix ? prefix + '/' + name : name);
      pending = {};
      if (type === '5') {
        if (full !== 'package/' && full !== 'package') safeMember(full.replace(/\/$/, ''));
        if (size !== 0) throw new Error('INVALID_DIRECTORY_MEMBER');
      } else {
        const path = safeMember(full), key = path.toLowerCase();
        if (folded.has(key)) throw new Error('DUPLICATE_ARCHIVE_MEMBER');
        folded.add(key); members.set(path, Buffer.from(bytes));
        if (members.size > 2000) throw new Error('TOO_MANY_ARCHIVE_MEMBERS');
      }
    }
    offset = start + Math.ceil(size / 512) * 512;
  }
  if (!ended || Object.keys(pending).length ||
      tar.subarray(offset).some(byte => byte !== 0)) throw new Error('INVALID_TAR_END');
  return members;
}
const RULES = [
  ['cache-or-private-entry', /(?:^|\/)(?:__pycache__|node_modules|\.git|\.local|\.dsh-home|\.mnemon|\.venv|data)(?:\/|$)|\.(?:pyc|pyo|log|jsonl)$|(?:^|\/)(?:\.env|engine\.env|service-token\.txt|credentials[^/]*)$/i],
  ['unapproved-binary', /\.(?:exe|dll|pyd|whl|zip|tgz|sqlite|db)$/i],
];
export function inspectMembers(members) {
  const findings = [];
  for (const [path, bytes] of members) {
    for (const [rule, pattern] of RULES) if (pattern.test(path)) findings.push({ path, rule });
    // Synthetic PDF is the only shipped binary; its metadata is also scanned as UTF-8.
    const body = bytes.toString('utf8');
    if (/sk-[A-Za-z0-9_-]{24,}/.test(body)) findings.push({ path, rule: 'credential-shape' });
    if (/\bBearer\s+[A-Za-z0-9_-]{32,}/.test(body)) findings.push({ path, rule: 'bearer-value' });
    if (/\bkr[hc]1\.[A-Za-z0-9_-]{40,}\.[A-Za-z0-9_-]{40,}/.test(body)) findings.push({ path, rule: 'signed-handle' });
    if (/[A-Za-z]:[\\/]Users[\\/](?!<|%|YOUR_USER|USER_NAME)[A-Za-z0-9_.-]+[\\/]/.test(body)) {
      findings.push({ path, rule: 'personal-home-path' });
    }
    if (/[A-Za-z]:[\\/]CCchat(?:[\\/]|\s)/i.test(body)) findings.push({ path, rule: 'private-corpus-path' });
  }
  return findings;
}
export function makeManifest(members, provenance) {
  const pkg = JSON.parse(members.get('package.json').toString('utf8'));
  return {
    schemaVersion: 1, name: pkg.name, version: pkg.version, channel: 'private-candidate',
    provenance, hostPeers: pkg.peerDependencies, node: pkg.engines.node, python: '3.13',
    runtimeBundled: false, licenceStatus: 'owner-decision-pending',
    pythonLockSHA256: sha256(members.get('engine/requirements-win-py313.lock')),
    files: Object.fromEntries([...members].filter(([name]) => name !== MANIFEST_NAME).sort(([a], [b]) => a.localeCompare(b, 'en')).map(([path, bytes]) =>
      [path, { bytes: bytes.length, sha256: sha256(bytes) }])),
  };
}
export function auditMembers(members, { requireManifest = true } = {}) {
  const findings = inspectMembers(members);
  let manifest = null;
  if (requireManifest) {
    try {
      manifest = JSON.parse(members.get(MANIFEST_NAME).toString('utf8'));
      if (manifest.schemaVersion !== 1 || !manifest.files || typeof manifest.files !== 'object') throw new Error();
      const expected = new Set(Object.keys(manifest.files));
      for (const [path, bytes] of members) {
        if (path === MANIFEST_NAME) continue;
        const item = manifest.files[path];
        if (!item) findings.push({ path, rule: 'unlisted-member' });
        else if (item.bytes !== bytes.length || item.sha256 !== sha256(bytes)) findings.push({ path, rule: 'hash-mismatch' });
        expected.delete(path);
      }
      for (const path of expected) findings.push({ path, rule: 'missing-member' });
      const actual = makeManifest(members, manifest.provenance);
      for (const key of ['name', 'version', 'hostPeers', 'node', 'python', 'runtimeBundled', 'licenceStatus', 'pythonLockSHA256']) {
        if (JSON.stringify(actual[key]) !== JSON.stringify(manifest[key])) findings.push({ path: MANIFEST_NAME, rule: 'metadata-mismatch:' + key });
      }
    } catch { findings.push({ path: MANIFEST_NAME, rule: 'manifest-invalid-or-missing' }); }
  }
  return { ok: findings.length === 0, memberCount: members.size,
    payloadBytes: [...members.values()].reduce((n, b) => n + b.length, 0), findings, manifest };
}
export function auditArchive(file, options) {
  const compressed = readFileSync(file);
  return { archiveSHA256: sha256(compressed), archiveBytes: compressed.length, ...auditMembers(unpackArchive(compressed), options) };
}
/** Integrity only for installed payload. Host SDK junctions are expected external dependencies, not payload. */
export function verifyPayloadDirectory(root) {
  const manifest = JSON.parse(readFileSync(join(root, MANIFEST_NAME), 'utf8'));
  const findings = [], seen = new Set();
  for (const [path, expected] of Object.entries(manifest.files)) {
    safeMember('package/' + path);
    const parts = path.split('/');
    let current = root;
    try {
      for (const part of parts) {
        current = join(current, part);
        if (lstatSync(current).isSymbolicLink()) throw new Error('link');
      }
      const bytes = readFileSync(current);
      if (bytes.length !== expected.bytes || sha256(bytes) !== expected.sha256) findings.push({ path, rule: 'hash-mismatch' });
      seen.add(path);
    } catch { findings.push({ path, rule: 'missing-or-linked-member' }); }
  }
  const walk = dir => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = relative(root, join(dir, entry.name)).split('\\').join('/');
      if (path === 'node_modules' || path === MANIFEST_NAME) continue;
      if (entry.isSymbolicLink()) findings.push({ path, rule: 'unexpected-link' });
      else if (entry.isDirectory()) walk(join(dir, entry.name));
      else if (!seen.has(path)) findings.push({ path, rule: 'unlisted-member' });
    }
  };
  walk(root);
  return { ok: findings.length === 0, memberCount: seen.size, findings };
}
