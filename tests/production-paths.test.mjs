import test from 'node:test';
import assert from 'node:assert/strict';
import {join,resolve} from 'node:path';
import {instanceEnv,instancePaths} from '../trial/lib.mjs';
test('installed service keeps all mutable state outside replaceable app directory',()=>{
 const paths=instancePaths(resolve('.local/never-installed-fixture'));
 const env=instanceEnv(paths,{servicePort:19191,engineBaseUrl:'http://127.0.0.1:19192',engineApiKey:'',engineModel:'fixture'});
 assert.equal(env.DSH_KNOWLEDGE_STATE_DIR,join(paths.data,'state'));
 assert.equal(env.DSH_KNOWLEDGE_DIAGNOSTICS_DIR,join(paths.data,'diagnostics'));
 assert.equal(env.PYTHONDONTWRITEBYTECODE,'1');
 assert.equal(env.DSH_KNOWLEDGE_ISOLATION,'required');
});