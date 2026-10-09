import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { once } from 'node:events';
import { createServer } from 'node:net';
import { resolve } from 'node:path';
import assert from 'node:assert/strict';
import { apply } from '../lib/index.js';

const listener = createServer();
await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
const port = listener.address().port;
await new Promise(resolve => listener.close(resolve));
const env = { ...process.env, DSH_KNOWLEDGE_TOKEN: 'synthetic-live-smoke-token-not-a-secret',
  DSH_KNOWLEDGE_CONFIG: resolve('.local/libraries.json'),
  // Structural smoke only: it exercises the plain worker path on purpose, so isolation (the
  // default) is explicitly switched off here.
  DSH_KNOWLEDGE_ISOLATION: 'off',
  LLM_API_KEY: '', LLM_MODEL_NAME: '', LLM_BASE_URL: '' };
process.env.DSH_KNOWLEDGE_TOKEN = env.DSH_KNOWLEDGE_TOKEN;
process.env.DSH_KNOWLEDGE_URL = `http://127.0.0.1:${port}`;
const child = spawn(resolve('.venv/Scripts/python.exe'), ['-m', 'uvicorn', 'service:app', '--app-dir', 'engine',
  '--host', '127.0.0.1', '--port', String(port), '--no-access-log'], {env, windowsHide:true, stdio:'ignore'});
const exit = once(child, 'exit');
try {
  let ready = false;
  for (let i=0;i<100;i++) {
    if (child.exitCode !== null) throw new Error('Service startup failed');
    try {
      const response = await fetch(process.env.DSH_KNOWLEDGE_URL + '/health', {
        headers: {Authorization:'Bearer '+env.DSH_KNOWLEDGE_TOKEN}, signal:AbortSignal.timeout(1000)});
      if (response.ok) { ready=true; break; }
    } catch {}
    await new Promise(resolve=>setTimeout(resolve,100));
  }
  assert.ok(ready, 'Service readiness');
  const tools = [];
  apply({tools:{register:tool=>tools.push(tool)}});
  const context = {agent:{session:{cwd:process.cwd()}},signal:new AbortController().signal};
  const list = await tools[0].execute({},context);
  assert.equal(list.libraries[0].id, 'fixtures');
  const result = await tools[1].execute({libraryId:'fixtures',query:'What is WALNUT?',mode:'FAST'},context);
  assert.equal(result.status,'failed');
  assert.ok(result.warnings.includes('DEPENDENCY_MISSING: rg and rga must be installed explicitly') || result.warnings.includes('MODEL_NOT_CONFIGURED'));
  console.log('PASS: actual DSH-normalized tools -> HTTP -> Python service -> worker; missing model/dependency fails explicitly. Not a live DSH profile test.');
} finally {
  if (child.exitCode === null) {
    if (process.platform === 'win32') await promisify(execFile)('taskkill',['/PID',String(child.pid),'/T','/F'],{windowsHide:true});
    else child.kill('SIGTERM');
  }
  await exit;
}
