/** Actual package identity upgrade check; local HTTP, no provider or system task. */
import {readFileSync,mkdirSync,writeFileSync} from 'node:fs';
import {resolve,join,dirname} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createServer} from 'node:http';
import assert from 'node:assert/strict';
import {unpackArchive,auditArchive} from '../trial/artifact.mjs';
const args={};for(let i=2;i<process.argv.length;i+=2){if(!['--archive','--previous-archive','--work'].includes(process.argv[i])||!process.argv[i+1])throw Error('INVALID_ARGUMENT');args[process.argv[i].slice(2)]=resolve(process.argv[i+1]);}
for(const key of ['archive','previous-archive','work'])if(!args[key])throw Error('MISSING_ARGUMENT');
const load=(archive,dir)=>{if(!auditArchive(archive).ok)throw Error('ARCHIVE_AUDIT_FAILED');const members=unpackArchive(readFileSync(archive));for(const [p,b] of members){mkdirSync(dirname(join(dir,p)),{recursive:true});writeFileSync(join(dir,p),b);}return members.has('lib/build-identity.json')?JSON.parse(members.get('lib/build-identity.json')):null;};
const root=args.work;mkdirSync(root,{recursive:true});const current=load(args.archive,join(root,'current')),previous=load(args['previous-archive'],join(root,'previous'));
assert.ok(current?.id);const {KnowledgeClient}=await import(pathToFileURL(join(root,'current/lib/client.js')));const {RUNNING_BUILD}=await import(pathToFileURL(join(root,'current/lib/build.js')));assert.equal(RUNNING_BUILD?.valid,true);assert.equal(RUNNING_BUILD.id,current.id);
let state={build:previous?{...previous,valid:true}:null,runtimeBuild:{status:'verified'}},business=0,health=0;
const server=createServer((req,res)=>{res.setHeader('Content-Type','application/json');if(req.url==='/health'){health++;res.end(JSON.stringify(state));}else{business++;assert.equal(req.headers['x-knowledge-build'],current.id);res.end('[]');}});
await new Promise(r=>server.listen(0,'127.0.0.1',r));const client=new KnowledgeClient('http://127.0.0.1:'+server.address().port,'synthetic-token-longer-than-24');
const checks=[];try{
 await assert.rejects(client.libraries('synthetic'),e=>e.code==='KNOWLEDGE_BUILD_MISMATCH');assert.equal(business,0);checks.push({name:'new-host-old-backend-refused-before-business',passed:true});
 state={build:{...current,valid:true},runtimeBuild:{status:'stale'}};await assert.rejects(client.libraries('synthetic'),e=>e.code==='KNOWLEDGE_BUILD_MISMATCH');assert.equal(business,0);checks.push({name:'new-backend-old-runtime-refused-before-business',passed:true});
 state.runtimeBuild.status='verified';await client.libraries('synthetic');assert.equal(business,1);checks.push({name:'matching-update-header-and-request',passed:true});
 writeFileSync(join(root,'report.json'),JSON.stringify({completed:true,checks,currentBuild:current.id,previousBuild:previous?.id??null,healthRequests:health,businessRequests:business,realModelCalls:0},null,2));console.log(JSON.stringify({passed:checks.length,realModelCalls:0}));
}finally{await new Promise(r=>server.close(r));}
