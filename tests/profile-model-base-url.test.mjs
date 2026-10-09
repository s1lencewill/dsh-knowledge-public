// The profile this installer writes must not override the conversation adapter's own base URL unless an operator
// asked for one.
//
// DSH 0.2's `llm-deepseek` is an Anthropic-Messages client whose own default is
// `https://api.deepseek.com/anthropic`, and a `baseURL` in the profile *replaces* it. This installer's older
// default (`https://api.deepseek.com`) therefore sent the adapter to `<base>/v1/messages`, a path that answers
// 404 (one-token probe, 2026-09-28). The three cases below are the ones that matter: nothing asked for, the old
// default coming back through an upgrade of an existing instance, and an explicit choice.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { LEGACY_MODEL_BASE_URL, modelBaseUrlRows, writeProfile } from '../trial/lib.mjs';

const CONFIG = {
  profileName: 'kn-trial-test',
  modelBaseUrl: null,
  modelApiKeyEnv: 'DEEPSEEK_API_KEY',
  modelProvider: 'deepseek-official',
  titleModel: 'title-stub',
  picker: 'browse',
  servicePort: 18765,
};

/** A throwaway instance layout: the profile dir, the app the junction points at, and the token file path. */
function fixture(t) {
  const base = resolve('.local/profile-base-url-tests');
  mkdirSync(base, { recursive: true });
  const root = mkdtempSync(join(base, 'case-'));
  t.after(() => { assert.equal(resolve(root, '..'), base); rmSync(root, { recursive: true, force: true }); });
  const profilesDir = join(root, 'dsh-home', 'profiles');
  const profileDir = join(profilesDir, CONFIG.profileName);
  const app = join(root, 'app');
  mkdirSync(join(app, 'lib'), { recursive: true });
  mkdirSync(join(profileDir, 'node_modules', 'dsh-knowledge'), { recursive: true });
  // The generator adds a `link:` dependency to this manifest; giving it one keeps it on the normal branch.
  writeFileSync(join(profileDir, 'package.json'), JSON.stringify({
    name: 'dsh-profile-kn-trial-test', private: true,
    dsh: { profile: { bundles: ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app'], patchReload: 'live' } },
  }, null, 2));
  const token = join(root, 'data', 'service-token.txt');
  mkdirSync(join(root, 'data'), { recursive: true });
  writeFileSync(token, 'x'.repeat(43));
  return { paths: { profilesDir, app, token }, patch: join(profileDir, 'cordis.patch.yml') };
}

/** The `llm-deepseek` block of the generated patch, as lines. */
function modelBlock(patchPath) {
  const lines = readFileSync(patchPath, 'utf8').split('\n');
  const start = lines.indexOf('- id: llm-deepseek');
  assert.notEqual(start, -1, 'the profile patch has no llm-deepseek row');
  const rest = lines.slice(start + 1);
  const end = rest.findIndex(line => line.startsWith('- '));
  return (end === -1 ? rest : rest.slice(0, end));
}

test('no base URL was asked for, so the adapter default stands', (t) => {
  const { paths, patch } = fixture(t);
  const result = writeProfile(paths, { ...CONFIG, modelBaseUrl: null });
  assert.equal(result.ok, true);
  const block = modelBlock(patch);
  assert.equal(block.some(line => line.trim().startsWith('baseURL:')), false, 'wrote a baseURL nobody asked for');
  assert.equal(block.some(line => line.includes('apiKeyEnv: DEEPSEEK_API_KEY')), true, 'lost the apiKeyEnv row');
});

test("the installer's own old default is not treated as a choice", (t) => {
  const { paths, patch } = fixture(t);
  writeProfile(paths, { ...CONFIG, modelBaseUrl: LEGACY_MODEL_BASE_URL });
  assert.equal(modelBlock(patch).some(line => line.trim().startsWith('baseURL:')), false,
    'an upgrade would keep the 404 path');
});

test('an explicit base URL is written verbatim', (t) => {
  const { paths, patch } = fixture(t);
  writeProfile(paths, { ...CONFIG, modelBaseUrl: 'http://127.0.0.1:18784' });
  assert.equal(modelBlock(patch).some(line => line.includes('baseURL: http://127.0.0.1:18784')), true);
});

test('the row helper is the single place that decides', () => {
  assert.deepEqual(modelBaseUrlRows(undefined), []);
  assert.deepEqual(modelBaseUrlRows('   '), []);
  assert.deepEqual(modelBaseUrlRows(LEGACY_MODEL_BASE_URL), []);
  assert.deepEqual(modelBaseUrlRows('https://example.test/v1'), ['    baseURL: https://example.test/v1']);
});
