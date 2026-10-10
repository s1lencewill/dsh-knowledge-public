import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,mkdirSync,writeFileSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createHash} from 'node:crypto';
import {releaseBuild,compatibleHealth} from '../lib/build.js';
import {KnowledgeClient} from '../lib/client.js';
import {renderBrowser,createIdentity} from '../scripts/build-identity.mjs';
const id='a'.repeat(64),other='b'.repeat(64), build={id,version:'same',protocol:1,runtimeInputs:{'engine/worker.py':id},browserSHA256:id,valid:true};
const good={build:{...build},runtimeBuild:{status:'verified'}};
test('same version but different build is refused; missing and stale runtime are refused',()=>{
 assert.equal(compatibleHealth(good,build),true);
 for(const value of [{},{...good,build:{...build,id:other}},{...good,runtimeBuild:{status:'stale'}},{...good,build:{...build,valid:false}}])assert.equal(compatibleHealth(value,build),false);
});
test('mismatched service receives only health, never business request',async()=>{
 const original=globalThis.fetch,requests=[];globalThis.fetch=async(url)=>{requests.push(url);return new Response(JSON.stringify({...good,build:{...build,id:other}}));};
 try{const client=new KnowledgeClient('http://127.0.0.1:1234','synthetic-token-at-least-24-chars',()=>{},{},build);await assert.rejects(client.libraries('synthetic'),e=>e.code==='KNOWLEDGE_BUILD_MISMATCH');assert.deepEqual(requests,['http://127.0.0.1:1234/health']);}finally{globalThis.fetch=original;}
});
test('matching service receives frozen host build on actual business request',async()=>{
 const original=globalThis.fetch,requests=[];globalThis.fetch=async(url,options)=>{requests.push({url,headers:options.headers});return new Response(JSON.stringify(url.endsWith('/health')?good:[]));};
 try{await new KnowledgeClient('http://127.0.0.1:1234','synthetic-token-at-least-24-chars',()=>{},{},build).libraries('synthetic');assert.equal(requests.length,2);assert.equal(requests[1].headers['X-Knowledge-Build'],id);}finally{globalThis.fetch=original;}
});
test('release startup identity checks installed files and remains frozen after disk update',()=>{
 const root=mkdtempSync(join(tmpdir(),'build-identity-'));try{
 mkdirSync(join(root,'lib'));writeFileSync(join(root,'RELEASE-MANIFEST.json'),'{}');writeFileSync(join(root,'lib/a.js'),'original');writeFileSync(join(root,'lib/browser.js'),'browser');
 const hash=s=>createHash('sha256').update(s).digest('hex');const meta={...build,schemaVersion:1,files:{'lib/a.js':hash('original')},browserSHA256:hash('browser')};writeFileSync(join(root,'lib/build-identity.json'),JSON.stringify(meta));const frozen=releaseBuild(root);assert.equal(frozen.valid,true);
 writeFileSync(join(root,'lib/a.js'),'changed');assert.equal(releaseBuild(root).valid,false);assert.equal(frozen.id,id);assert.equal(frozen.valid,true);
 }finally{rmSync(root,{recursive:true,force:true});}
});
test('build identity matches generated browser; child payload hashes are explicit',()=>{
 const b=createIdentity(process.cwd());assert.ok(b.runtimeInputs['engine/worker.py']);assert.equal(b.browserSHA256,createHash('sha256').update(renderBrowser(readFileSync('ui/source-card.js','utf8'),b.id,b.name)).digest('hex'));
});
import {Readable} from 'node:stream';
import {PanelSettings,registerPanelBridge} from '../lib/panel.js';
test('cached browser from another build cannot change settings or issue data requests; cancellation remains',async()=>{
 let handler, fetched=0,cancelled=0;const original=globalThis.fetch;globalThis.fetch=async()=>{fetched++;throw Error('no business request allowed');};
 try{
 const active={status:()=>null,list:()=>[],cancel:()=>{cancelled++;return {requested:false,phases:[]};},recancel:async()=>({attempted:false,confirmed:false})};
 registerPanelBridge({inject:(_,cb)=>cb({effect:f=>f(),webServer:{register:r=>{handler=r.handler;}},connection:{requestRejection:()=>undefined}})},new PanelSettings(),()=>({url:'http://127.0.0.1:1234',token:'synthetic-token-24-characters'}),undefined,active,undefined,undefined,build);
 const call=async(body,header=other,method='POST')=>{
 const req=Readable.from(body?[JSON.stringify(body)]:[]);Object.assign(req,{method,url:'/knowledge-panel?sessionId=session-test',headers:{origin:'http://localhost',host:'localhost','content-type':'application/json','x-knowledge-client-build':header}});
 let text;const res={statusCode:200,setHeader(){},end(t){text=t;}};await handler(req,res);return {status:res.statusCode,body:JSON.parse(text)};};
 assert.equal((await call({maxCalls:10})).status,409);assert.equal((await call({libraryAction:'select',libraryId:'anything'})).status,409);
 assert.equal((await call(null,other,'GET')).body.service,'build-mismatch');assert.equal(fetched,0);
 assert.equal((await call({cancelQuery:true})).status,200);assert.equal(cancelled,1);
 assert.equal((await call({maxCalls:10},id)).status,200);
 }finally{globalThis.fetch=original;}
});
test('rendered browser bridge sends only public build identity, never service token',async()=>{
 const original=globalThis.fetch;let actual;globalThis.fetch=async(input,opts)=>{actual={input,opts};return new Response('{}');};
 try{let api;const source=renderBrowser(readFileSync('ui/source-card.js','utf8'),id).replace('exports.apply = apply;','exports.bridgeFetch=fetch; exports.apply = apply;');
 new Function('window',source)({__ModuleLoader__:{load:spec=>{api=spec.factory(()=>({createElement(){}}));}}});
 await api.bridgeFetch('/knowledge-panel?sessionId=session-test',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
 assert.equal(actual.opts.headers.get('X-Knowledge-Client-Build'),id);assert.equal(actual.opts.headers.get('Authorization'),null);
 }finally{globalThis.fetch=original;}
});
import {buildIdentity} from '../scripts/build-identity.mjs';
test('SDK validation refuses parent fallback and mismatched local versions',()=>{
 const root=mkdtempSync(join(tmpdir(),'sdk-build-'));try{
 const sdk=['@deepseek-ai/dsh-tools','@deepseek-ai/dsh-llm'];const dependencies=Object.fromEntries(sdk.map(n=>[n,'0.2.0-rc.2']));writeFileSync(join(root,'package.json'),JSON.stringify({devDependencies:dependencies,peerDependencies:dependencies}));
 assert.throws(()=>buildIdentity(root),/BUILD_SDK_NOT_LOCAL/);
 for(const name of sdk){const path=join(root,'node_modules',name);mkdirSync(path,{recursive:true});writeFileSync(join(path,'package.json'),JSON.stringify({version:'old'}));}
 assert.throws(()=>buildIdentity(root),/BUILD_SDK_VERSION_MISMATCH/);
 }finally{rmSync(root,{recursive:true,force:true});}
});
