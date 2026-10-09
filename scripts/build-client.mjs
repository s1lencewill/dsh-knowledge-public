/**
 * Copy the tracked browser half (`ui/source-card.js`) to the served artifact (`lib/browser.js`).
 *
 * The host serves built client bundles, and `lib/` is a build output (gitignored). The browser half is
 * hand-authored in the loader's lazy-CJS container format, so this step is a copy rather than a bundle:
 * it adds no bundler, no new dependency and no transpile step, and what is reviewed in `ui/` is byte for
 * byte what the browser executes.
 *
 * The artifact is `lib/browser.js`, NOT the conventional `lib/client.js`, and `exports["./client"]`
 * points here. `src/client.ts` already compiles to `lib/client.js` (the host-side service adapter), and
 * the loader resolves the browser half through `exports["./client"].default` rather than by convention
 * (`dsh-client-modules` `clientExportOf`), so a distinct path is both safe and unambiguous.
 */
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const from = resolve(root, 'ui/source-card.js');
const to = resolve(root, 'lib/browser.js');

const source = readFileSync(from, 'utf8');
if (!source.includes('window.__ModuleLoader__.load(')) {
  throw new Error('ui/source-card.js is not in the client-module container format; refusing to publish it');
}
if (!/id:\s*['"]dsh-knowledge['"]/.test(source)) {
  throw new Error("ui/source-card.js must register under the package's own name (id: 'dsh-knowledge')");
}
// The loader composes one combo request per phase and would rather fail loudly than serve a bundle with
// a stale source-map pointer, so the served file must not carry a sourceMappingURL comment.
if (/sourceMappingURL/.test(source)) {
  throw new Error('ui/source-card.js must not declare a sourceMappingURL: no map is served for it');
}
mkdirSync(dirname(to), { recursive: true });
// Bytes, not text: a text write would fold to the platform newline and change the served file.
writeFileSync(to, Buffer.from(source, 'utf8'));
console.log(`client bundle written: ${to} (${Buffer.byteLength(source)} bytes)`);
