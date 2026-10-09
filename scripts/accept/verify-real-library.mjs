/**
 * Verify the authorised real-library configuration against the files on disk before any of it is used by the daily
 * instance: every recorded file hash, the page-map catalogue's own hash, and the absence of a synthetic marker.
 *
 * It reads the configuration and hashes files; it writes nothing and prints only counts, ids and mismatches.
 */
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';

const LIBRARY_DIR = resolve(process.argv[2] ?? '.local/full-literature-v2-20260917');
const config = JSON.parse(readFileSync(join(LIBRARY_DIR, 'libraries.json'), 'utf8'));
const activation = JSON.parse(readFileSync(join(LIBRARY_DIR, 'page-map-activation.json'), 'utf8'));
const sha256 = (path) => createHash('sha256').update(readFileSync(path)).digest('hex');

const report = { libraryDir: LIBRARY_DIR, libraries: [] };
for (const library of config.libraries ?? []) {
  const entry = { id: library.id, syntheticFixture: library.syntheticFixture ?? null,
    workspace: library.workspace, root: library.root, rootExists: existsSync(library.root),
    authorized: library.realDataTrial?.authorized ?? null, files: { total: 0, matched: 0, mismatched: [], missing: [] },
  };
  for (const [name, expected] of Object.entries(library.realDataTrial?.files ?? {})) {
    entry.files.total += 1;
    const path = join(library.root, name);
    if (!existsSync(path)) { entry.files.missing.push(name); continue; }
    if (sha256(path) === expected) entry.files.matched += 1;
    else entry.files.mismatched.push({ name, bytes: statSync(path).size });
  }
  report.libraries.push(entry);
}
const activePath = join(LIBRARY_DIR, activation.active);
report.pageMap = {
  active: activation.active,
  activeExists: existsSync(activePath),
  recordedSHA256: activation.activeSHA256,
  actualSHA256: existsSync(activePath) ? sha256(activePath) : null,
  matches: existsSync(activePath) && sha256(activePath) === activation.activeSHA256,
  mappedDocuments: activation.activeMappedDocuments, unmappedDocuments: activation.unmappedDocuments?.length ?? null,
  previous: activation.previous, previousExists: existsSync(join(LIBRARY_DIR, activation.previous)),
  serviceEnvHint: activation.pointServiceAt,
  rollbackHint: activation.rollback,
};
report.textsOnDisk = (await import('node:fs')).readdirSync(join(LIBRARY_DIR, 'texts')).length;
report.ok = report.libraries.every(entry => entry.rootExists && entry.authorized === true
  && entry.syntheticFixture === false && entry.files.total === entry.files.matched)
  && report.pageMap.matches && report.pageMap.mappedDocuments === report.textsOnDisk;
console.log(JSON.stringify(report, null, 2));
