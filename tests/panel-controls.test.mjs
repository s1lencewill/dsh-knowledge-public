import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import { PanelSettings, PanelController, registerPanelBridge } from '../lib/panel.js';
import { TurnLedger } from '../lib/turnlimit.js';

test('per-session settings persist, reject invalid input, and pin a running turn', () => {
 const dir=mkdtempSync(join(tmpdir(),'knowledge-panel-'));
 try {
  const file=join(dir,'settings.json'),settings=new PanelSettings(file);
  const ledger=new TurnLedger({maxForSession:id=>settings.get(id)});
  const session={seq:1,eventAt:seq=>({type:'turn/start',seq})};
  assert.equal(ledger.reserve('session-a',session).max,6);
  settings.set('session-a',12);
  for(let n=2;n<=7;n++){const r=ledger.reserve('session-a',session);assert.equal(r.max,6);assert.equal(r.allowed,n<=6);}
  session.seq=2;assert.equal(ledger.reserve('session-a',session).max,12);
  assert.equal(ledger.reserve('session-b',session).max,6);
  assert.equal(new PanelSettings(file).get('session-a'),12);
  for(const v of [0,31,-1,1.1,'10',null,true])assert.throws(()=>settings.set('session-a',v));
  assert.throws(()=>settings.set('../other',10));
  assert.equal(new PanelSettings(file).get('session-a'),12);
 }finally{rmSync(dir,{recursive:true,force:true});}
});
test('broken settings do not break tool registration or overwrite original bytes',()=>{
 const dir=mkdtempSync(join(tmpdir(),'knowledge-panel-'));try{
 const file=join(dir,'settings.json');writeFileSync(file,'not-json');const s=new PanelSettings(file);
 assert.equal(s.readable,false);assert.equal(s.get('session-a'),6);assert.throws(()=>s.set('session-a',9));assert.equal(readFileSync(file,'utf8'),'not-json');
 }finally{rmSync(dir,{recursive:true,force:true});}
});
function bridge(rejected, settings=new PanelSettings()) {
 let handler;
 registerPanelBridge({inject:(_deps,cb)=>cb({effect:f=>f(),webServer:{register:r=>{handler=r.handler;return ()=>{}}},connection:{requestRejection:()=>rejected}})},settings,()=>({url:'http://127.0.0.1:19999',token:'synthetic-token-abcdefghijklmnopqrstuvwxyz'}));
 return async(method,body,origin='http://localhost:3000')=>{
 const req=Readable.from(body?[body]:[]);req.method=method;req.url='/knowledge-panel?sessionId=session-test';req.headers={host:'localhost:3000',origin,'content-type':'application/json'};
 let output;const res={statusCode:200,setHeader(){},end(text){output=text}};
 await handler(req,res);return {status:res.statusCode,body:output?JSON.parse(output):null};
 };
}
test('bridge respects host authentication, same-origin writes and bounded settings',async()=>{
 assert.equal((await bridge(401)('POST','{"maxCalls":9}')).status,401);
 const request=bridge();assert.equal((await request('POST','{"maxCalls":9}','http://attacker.invalid')).status,403);
 assert.equal((await request('POST','{"maxCalls":9}')).body.maxCalls,9);
 assert.equal((await request('POST','{"maxCalls":9,"path":"arbitrary"}')).status,400);
 assert.equal((await request('POST','x'.repeat(4097))).status,413);
 assert.equal((await request('DELETE')).status,405);
});
test('health projection reveals no credential or raw response and does not search',async()=>{
 const old=globalThis.fetch;let count=0;
 try{globalThis.fetch=async(url,options)=>{count++;assert.equal(url,'http://127.0.0.1:19999/health');assert.match(options.headers.Authorization,/^Bearer /);return {ok:true,status:200,json:async()=>({isolation:'required',isolationActive:true,secret:'DO-NOT-RETURN'})}};
 const r=await bridge()('GET');assert.equal(r.body.service,'ready');assert.equal(count,1);assert.doesNotMatch(JSON.stringify(r),/DO-NOT-RETURN|synthetic-token/);
 globalThis.fetch=async()=>{throw new Error('private path')};assert.equal((await bridge()('GET')).body.service,'unreachable');
 }finally{globalThis.fetch=old;}
});

const source=readFileSync(new URL('../ui/source-card.js',import.meta.url),'utf8');
function uiInternals(){let module;const instrumented=source.replace('exports.apply = apply;','exports.journalTree = journalTree; exports.followSession = followSession; exports.createPanelStore = createPanelStore; exports.apply = apply;');
 new Function('window',instrumented)({__ModuleLoader__:{load:spec=>{module=spec.factory(()=>({createElement(){}}))}}});return module;}
const card={libraryId:'test',title:'Sample',fileName:'sample.md',sourceVersion:'v1',excerpt:'facts',location:{startLine:2,endLine:4}};
const events=[{event:{type:'tool/call',data:{callId:'c1',name:'knowledge_read'}}},{event:{type:'tool/result',data:{message:{toolCallId:'c1',content:[]},meta:{sources:[card,card]}}}}];
test('tree is reconstructed from persisted events even when tool cards never rendered',()=>{
 const {journalTree}=uiInternals();let tree=journalTree({entries:events,hasMore:true});
 assert.equal(tree.groups[0].documents[0].cards.length,1);assert.equal(tree.pending,0);assert.equal(tree.hasMore,true);
 assert.equal(journalTree({entries:[events[0]]}).pending,1);
 assert.equal(journalTree({entries:[]}).groups.length,0);
 const newer=structuredClone(events);newer[0].event.data.callId='c2';newer[1].event.data.message.toolCallId='c2';newer[1].event.data.meta.sources[0].sourceVersion='v2';
 assert.equal(journalTree({entries:[...events,...newer]}).groups[0].documents.length,2);
});
test('session switch cancels old subscription, excludes stale asynchronous callbacks and does not reopen on every result',async()=>{
 const {followSession,createPanelStore}=uiInternals();let mounted='session-a',mountListener,feedListener,releaseCount=0;const opens=[];
 const feed={getSnapshot:()=>({entries:events}),subscribe:fn=>{feedListener=fn;return ()=>{feedListener=null}}};
 const runtime={trees:createPanelStore(),sidebar:{mounted:{getSnapshot:()=>mounted,subscribe:fn=>{mountListener=fn;return ()=>{}}},openTab:()=>opens.push(mounted)}};
 const sessions={retain:id=>({ready:Promise.resolve({eventSource:id==='session-a'?feed:{getSnapshot:()=>({entries:[]}),subscribe:()=>()=>{}}}),release:()=>releaseCount++})};
 const dispose=followSession(runtime,sessions);await Promise.resolve();assert.equal(runtime.trees.snapshot('session-a').groups.length,1);assert.deepEqual(opens,[]);
 feedListener();assert.equal(opens.length,0);mounted='session-b';mountListener();await Promise.resolve();assert.equal(releaseCount,1);assert.equal(runtime.trees.snapshot('session-b').groups.length,0);
 dispose();assert.equal(releaseCount,2);assert.equal(feedListener,null);
});


test('control serializes clicks, keeps disabled tasks disabled and never returns raw failures',async()=>{
 let resolveRun;const calls=[];
 const c=new PanelController('fixture-root',async(file,args)=>{calls.push({file,args});if(args[0]==='status')return {code:1,output:JSON.stringify({autostart:'disabled'})};return await new Promise(r=>{resolveRun=r});});
 const first=c.act('start');await Promise.resolve();
 assert.equal((await c.act('start')).code,'ACTION_BUSY');
 assert.match(calls[1].file,/start.mjs$/);assert.ok(calls[1].args.includes('--service-only'));
 assert.ok(!calls.flatMap(c=>c.args).includes('enable'));
 resolveRun({code:0,output:'private token path'});assert.equal((await first).code,'ACTION_COMPLETED');
 const broken=new PanelController('fixture-root',async()=>{throw Error('sk-private-secret')});
 assert.doesNotMatch(JSON.stringify(await broken.act('stop')),/sk-private/);
 assert.equal((await new PanelController(null).act('start')).code,'CONTROL_UNAVAILABLE');
});
test('bridge cannot accept command, target or unknown action',async()=>{
 const request=bridge();for(const body of [{action:'exec'},{action:'start',target:'other'},{command:'anything'}])assert.equal((await request('POST',JSON.stringify(body))).status,400);
});

test('parallel health checks coalesce without caching a completed failure',async()=>{
 const old=globalThis.fetch;let resolveFetch,count=0;
 globalThis.fetch=()=>{count++;return new Promise(r=>resolveFetch=r);};
 try {
  const call=bridge(undefined);const a=call('GET'),b=call('GET');await Promise.resolve();assert.equal(count,1);
  resolveFetch(new Response('{}',{status:503}));assert.equal((await a).body.service,'unhealthy');await b;
  const c=call('GET');await Promise.resolve();assert.equal(count,2);resolveFetch(new Response(JSON.stringify({isolation:'required',isolationActive:true})));assert.equal((await c).body.service,'ready');
 }finally{globalThis.fetch=old;}
});

test('starting an already healthy or unauthorized endpoint never executes lifecycle commands',async()=>{
 const old=globalThis.fetch;let handler,commands=0;
 const control=new PanelController('synthetic-instance',async()=>{commands++;throw Error('must not run');});
 registerPanelBridge({inject:(_deps,cb)=>cb({effect:f=>f(),webServer:{register:r=>{handler=r.handler;return()=>{}}},connection:{requestRejection:()=>undefined}})},new PanelSettings(),()=>({url:'http://127.0.0.1:19999',token:'synthetic-token-abcdefghijklmnopqrstuvwxyz'}),control);
 const request=async()=>{const req=Readable.from(['{"action":"start"}']);req.method='POST';req.url='/knowledge-panel?sessionId=session-a';req.headers={host:'localhost',origin:'http://localhost','content-type':'application/json'};let body;await handler(req,{setHeader(){},end:s=>body=JSON.parse(s)});return body;};
 try{globalThis.fetch=async()=>new Response('{"isolation":"required","isolationActive":true}');assert.equal((await request()).code,'ALREADY_READY');
 globalThis.fetch=async()=>new Response('',{status:401});assert.equal((await request()).code,'ACTION_FAILED');assert.equal(commands,0);}finally{globalThis.fetch=old;}
});
