import { spawnSync } from 'node:child_process';
import { existsSync, readdirSync, mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

const python = resolve(process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
// A hang guard, not a performance gate. The Python suite genuinely needs ~60-80s on this host (228
// tests, several of which spawn real workers and run real deadline probes), so the old 90s cap turned
// a passing run into a false failure whenever the machine was loaded. A stuck step is still killed.
// A step may name its own cap as a fourth field: the lifecycle acceptance starts and stops real processes
// through the scheduler several times, and 240s truncated it mid-run (the log simply stopped after the
// restart-policy checks, which is exactly what a killed step looks like).
const STEP_TIMEOUT_MS = 240_000;
const LIFECYCLE_TIMEOUT_MS = 900_000;
// The installed-artifact check runs **only** against an instance that exists, and its target is never guessed
// from the user's live installation: verify:all must stay runnable on a machine with no instance at all, and it
// must never treat "the tests pass" as evidence about what a host would load. When no instance is present the
// step is reported as skipped, not as passed.
const instance = process.env.DSH_KNOWLEDGE_TRIAL_HOME
  || (existsSync('.local/inst/app/package.json') ? '.local/inst' : null);
// The lifecycle acceptance creates a real per-user scheduled task and starts a synthetic instance on its own
// ports, so it is opt-in: `verify:all` must stay runnable on a machine where nothing may be registered.
const lifecycle = process.env.DSH_KNOWLEDGE_LIFECYCLE === '1';
const steps = [
  ['preflight', python, ['scripts/preflight.py']],
  ['dependencies', python, ['-m', 'pip', 'check']],
  ['typescript', process.execPath, ['node_modules/typescript/bin/tsc','-p','tsconfig.json']],
  ['browser-bundle', process.execPath, ['scripts/build-client.mjs']],
  ['node-tests', process.execPath, ['--test',...readdirSync('tests').filter(name=>name.endsWith('.test.mjs')).map(name=>'tests/'+name)]],
  ['python-tests', python, ['-m','unittest','discover','-s','tests','-p','*_test.py','-v']],
  ['dsh-sdk-smoke', process.execPath, ['scripts/smoke-dsh.mjs']],
  ['http-smoke', process.execPath, ['scripts/smoke-live.mjs']],
  ['read-smoke', process.execPath, ['scripts/smoke-read.mjs']],
  ['filename-engine', python, ['scripts/probe-engine.py']],
  ...(instance ? [['installed-artifact', process.execPath, ['scripts/verify-install.mjs', '--target', instance], STEP_TIMEOUT_MS]] : []),
  ...(lifecycle ? [['lifecycle', process.execPath, ['scripts/verify-lifecycle.mjs'], LIFECYCLE_TIMEOUT_MS]] : []),
];
const report = { recordedAt: new Date().toISOString(), modelSearchValidated:false, liveDshProfileValidated:false,
  installedArtifactTarget: instance, steps:[] };
if (!instance) console.log('[verify:all] installed-artifact: skipped (no instance; set DSH_KNOWLEDGE_TRIAL_HOME)');
if (!lifecycle) console.log('[verify:all] lifecycle: skipped (set DSH_KNOWLEDGE_LIFECYCLE=1 to run the scheduled-task acceptance)');
mkdirSync('.local/verification', {recursive:true});
for (const [name, command, args, stepTimeout] of steps) {
  console.log(`\n[verify:all] ${name}`);
  const result = spawnSync(command, args, {encoding:'utf8', windowsHide:true, timeout: stepTimeout ?? STEP_TIMEOUT_MS});
  const output = (result.stdout ?? '') + (result.stderr ?? '');
  process.stdout.write(output);
  writeFileSync(`.local/verification/${name}.log`, output);
  report.steps.push({name, exitCode:result.status, passed:result.status===0});
  writeFileSync('.local/verification/results.json', JSON.stringify(report,null,2));
  if (result.status !== 0) {
    console.error(`[verify:all] failed: ${name}`);
    process.exit(1);
  }
}
console.log('\nAll local checks passed. Real FAST/DEEP model calls, AppContainer isolation and the live DSH profile are covered by separate probes (scripts/probe-isolated-*.py, scripts/verify-model.py), not by this command.');
