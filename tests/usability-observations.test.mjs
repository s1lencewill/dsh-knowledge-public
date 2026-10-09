import test from 'node:test';
import assert from 'node:assert/strict';
import {EvidenceObservations} from '../lib/observations.js';
import {KnowledgeClient,withHttpDispatch} from '../lib/client.js';
import {TurnLedger} from '../lib/turnlimit.js';
import {failureFrom} from '../lib/failures.js';
const item=(start,end,version='v1')=>({relativePath:'a.md',sourceVersion:version,startChar:start,endChar:end,sourceCard:{libraryId:'a',relativePath:'a.md',sourceVersion:version,location:{startChar:start,endChar:end}}});
test('range novelty is session scoped, version bound, not file-only, and carries durable card identity',()=>{
 const o=new EvidenceObservations();const run=(s,start,end,call,version)=>o.observe(s,{evidence:[item(start,end,version)]},call);
 const a=run('a',0,10,'call-a');assert.equal(a.novelty.newDocuments,1);assert.equal(a.novelty.newRanges,1);assert.match(a.evidence[0].citationId,/^ksrc-[a-f0-9]{16}$/);
 assert.equal(run('a',2,5,'call-b').novelty.newRanges,0);assert.equal(run('a',11,20,'call-c').novelty.newRanges,1);
 assert.equal(run('b',0,10,'call-d').novelty.newDocuments,1);assert.equal(run('a',0,10,'call-e','v2').novelty.newDocuments,1);
 assert.equal(run('a',0,10,'call-a').evidence[0].citationId,a.evidence[0].sourceCard.citationId);
 assert.equal('support' in a.novelty,false);
});
test('HTTP dispatch counts are not provider counts and concurrent contexts do not cross',async()=>{
 const old=globalThis.fetch;globalThis.fetch=async()=>new Response('[]');try{
  const c=new KnowledgeClient('http://127.0.0.1:19999','synthetic-token-abcdefghijklmnopqrstuvwxyz');let a=0,b=0;
  await Promise.all([withHttpDispatch(()=>a++,()=>c.libraries('a')),withHttpDispatch(()=>b++,()=>c.libraries('b'))]);assert.equal(a,1);assert.equal(b,1);
 }finally{globalThis.fetch=old;}
 const l=new TurnLedger();const r=l.reserve('session-a',undefined,'knowledge_search');l.noteHttp(r.identity.key);
 assert.deepEqual({...l.activityFor(r.identity.key),countedAs:undefined},{scope:'turn',attempts:1,httpRequests:1,searches:1,reads:0,countedAs:undefined});
 l.reserve('session-a',undefined,'knowledge_read');assert.equal(l.activityFor(r.identity.key).attempts,2);
});
test('thrown host error persists only declared quota projection; hostile activity is dropped',()=>{
 const e=failureFrom(new TypeError('private'),undefined,{used:2,max:6,tool:'knowledge_read',activity:{scope:'turn',attempts:2,httpRequests:1,searches:0,reads:1,secret:'sk-private'}});
 const facts=JSON.parse(e.message.split('KNOWLEDGE_FAILURE_FACTS=')[1]);assert.equal(facts.budget.used,2);assert.equal(facts.budget.activity.httpRequests,1);assert.ok(!e.message.includes('sk-private'));assert.ok(!e.message.includes('private'));
});

test('stop interrupts an in-flight knowledge request without refunding attempts or restarting',async()=>{
 const {PanelController}=await import('../lib/panel.js');const old=globalThis.fetch;let rejectPoll,entered;
 const polling=new Promise(r=>entered=r);const paths=[];
 globalThis.fetch=async url=>{paths.push(new URL(url).pathname);if(paths.length===1)return new Response('{"id":"synthetic-job"}');if(paths.length===2){entered();return new Promise((_r,j)=>rejectPoll=j);}throw new TypeError('connection stopped');};
 try {
  const l=new TurnLedger();const r=l.reserve('session-a',undefined,'knowledge_search');
  const warnings=[];const c=new KnowledgeClient('http://127.0.0.1:19999','synthetic-token-abcdefghijklmnopqrstuvwxyz',message=>warnings.push(message));
  const result=withHttpDispatch(()=>l.noteHttp(r.identity.key),()=>c.search('fixture','fixture','query','FAST')).catch(e=>e);
  await polling;let stops=0;const control=new PanelController('synthetic-instance',async(_file,args)=>{assert.equal(args[0],'stop');stops++;rejectPoll(new TypeError('connection stopped'));return {code:0,output:''};});
  assert.equal((await control.act('stop')).code,'ACTION_COMPLETED');assert.ok((await result) instanceof TypeError);
  assert.equal(warnings.length,1);assert.match(warnings[0],/KNOWLEDGE_CLEANUP_FAILED/);assert.equal(stops,1);assert.equal(l.usedFor(r.identity.key),1);assert.ok(paths.every(p=>p.startsWith('/queries')));assert.equal(control.state,'idle');
 }finally{globalThis.fetch=old;}
});
