/** Actual archive -> independent install -> local reads -> upgrade -> rollback. No model requests. */
import {spawnSync} from 'node:child_process';
import {mkdirSync,readFileSync,writeFileSync,existsSync,readdirSync,renameSync} from 'node:fs';
import {dirname,resolve,join} from 'node:path';
import net from 'node:net';
import {verifyReadWindow} from './release-evidence.mjs';
import {auditArchive,unpackArchive,sha256,verifyPayloadDirectory} from '../trial/artifact.mjs';
const opts={};for(let i=2;i<process.argv.length;i++){const name=process.argv[i];if(!/^--[a-z-]+$/.test(name)||!process.argv[i+1]||process.argv[i+1].startsWith('--'))throw Error('INVALID_ARGUMENT');opts[name.slice(2)]=process.argv[++i];}
for(const key of ['archive','previous-archive','work','python'])if(!opts[key])throw Error('REQUIRED_'+key);
const work=resolve(opts.work);if(existsSync(work))throw Error('WORK_ALREADY_EXISTS');mkdirSync(work,{recursive:true});
const report={schemaVersion:2,environment:'same-machine-independent-directory',cleanWindowsVerified:false,modelCalls:0,startedAt:new Date().toISOString(),checks:[]};
const save=()=>writeFileSync(join(work,'report.json'),JSON.stringify(report,null,2)+'\n');
function check(name,ok,detail={}){report.checks.push({name,passed:Boolean(ok),...detail});save();if(!ok)throw Error(name);}
function execute(name,file,args=[],expected=0){const r=spawnSync(process.execPath,[file,...args],{encoding:'utf8',windowsHide:true,timeout:180000,env:{...process.env,PYTHONUTF8:'1',PYTHONDONTWRITEBYTECODE:'1'}});writeFileSync(join(work,name+'.log'),(r.stdout||'')+(r.stderr||''));check(name,expected===null?r.status!==0:r.status===expected,{exitCode:r.status});return r;}
async function freePort(){return new Promise((done,reject)=>{const s=net.createServer();s.once('error',reject);s.listen(0,'127.0.0.1',()=>{const p=s.address().port;s.close(()=>done(p));});});}
function extract(file,label){const audit=auditArchive(resolve(file));check(label+'-archive-audit',audit.ok,{archiveSHA256:audit.archiveSHA256,members:audit.memberCount});const dir=join(work,label);mkdirSync(dir);for(const [path,bytes]of unpackArchive(readFileSync(file))){const dest=join(dir,path);mkdirSync(dirname(dest),{recursive:true});writeFileSync(dest,bytes);}return dir;}
const current=extract(opts.archive,'candidate'),previous=extract(opts['previous-archive'],'previous');
const target=join(work,'instance one 试用'),app=()=>join(target,'app'),install=(source,label,extra=[])=>execute(label,join(source,'trial/install.mjs'),['--target',target,'--python',resolve(opts.python),...extra]);
let active=false;
function protectedData(){const paths=['config.json','libraries.json','catalogue.json','service-token.txt','library/user-note.md'];return Object.fromEntries(paths.map(p=>[p,p==='config.json'?sha256(Buffer.from(JSON.stringify(Object.fromEntries(Object.entries(JSON.parse(readFileSync(join(target,'data',p),'utf8'))).filter(([key])=>!['updatedAt','version'].includes(key)))))):sha256(readFileSync(join(target,'data',p)))]));}
try{
 const servicePort=await freePort(),webPort=await freePort();
 install(previous,'install-previous',['--service-port',String(servicePort),'--web-port',String(webPort)]);
 writeFileSync(join(target,'data/library/user-note.md'),'User-owned data: do not replace. 中文 😀\n');
 const data=protectedData(),oldConfig=readFileSync(join(target,'data/config.json')),oldInstallManifest=readFileSync(join(target,'MANIFEST.sha256.json'));check('previous-installed-integrity',verifyPayloadDirectory(app()).ok);
 // Damaged candidate must be refused before the existing app or data changes.
 const damage=join(current,'ui/source-card.js'),original=readFileSync(damage),before=sha256(readFileSync(join(app(),'lib/browser.js')));
 writeFileSync(damage,Buffer.concat([original,Buffer.from('\n// injected damage\n')]));
 execute('damaged-upgrade-refused',join(current,'trial/install.mjs'),['--target',target,'--python',resolve(opts.python)],null);
 check('damaged-upgrade-preserves-app',sha256(readFileSync(join(app(),'lib/browser.js')))===before);
 check('damaged-upgrade-preserves-data',JSON.stringify(protectedData())===JSON.stringify(data));writeFileSync(damage,original);
 install(current,'upgrade-candidate');check('candidate-installed-integrity',verifyPayloadDirectory(app()).ok);
 check('upgrade-preserves-user-data',JSON.stringify(protectedData())===JSON.stringify(data));
 const retained=readdirSync(target).filter(n=>n.startsWith('app.previous-')).sort();const rollback=retained.find(n=>existsSync(join(target,n,'lib/browser.js'))&&sha256(readFileSync(join(target,n,'lib/browser.js')))===before);check('previous-app-retained',Boolean(rollback));
 // Roll back the program only; don't delete either version.
 renameSync(app(),join(target,'app.candidate-retained'));renameSync(join(target,rollback),app());writeFileSync(join(target,'data/config.json'),oldConfig);writeFileSync(join(target,'MANIFEST.sha256.json'),oldInstallManifest);
 check('rollback-integrity',verifyPayloadDirectory(app()).ok);check('rollback-restores-old-browser',sha256(readFileSync(join(app(),'lib/browser.js')))===before);
 check('rollback-preserves-user-data',JSON.stringify(protectedData())===JSON.stringify(data));
 install(current,'upgrade-after-rollback');check('reupgrade-integrity',verifyPayloadDirectory(app()).ok);
 check('reupgrade-preserves-user-data',JSON.stringify(protectedData())===JSON.stringify(data));
 execute('missing-runtime-doctor',join(app(),'trial/doctor.mjs'),['--target',target],null);
 execute('missing-runtime-start-refused',join(app(),'trial/start.mjs'),['--target',target,'--service-only'],null);
 if(opts['runtime-from']){
  const extra=['--runtime-from',resolve(opts['runtime-from'])];if(opts['tools-from'])extra.push('--tools-from',resolve(opts['tools-from']));
  install(current,'place-explicit-runtime',extra);execute('doctor-with-runtime',join(app(),'trial/doctor.mjs'),['--target',target]);
  execute('start-with-isolation',join(app(),'trial/start.mjs'),['--target',target,'--service-only']);active=true;
  const config=JSON.parse(readFileSync(join(target,'data/config.json'),'utf8')),token=readFileSync(join(target,'data/service-token.txt'),'utf8').trim();
  const endpoint='http://127.0.0.1:'+config.servicePort,headers={Authorization:'Bearer '+token,'Content-Type':'application/json'};
  const health=await(await fetch(endpoint+'/health',{headers})).json();check('health-isolation-required-active',health.isolation==='required'&&health.isolationActive===true);
  const workspace=join(target,'data');
  const listResponse=await fetch(endpoint+'/catalogue',{method:'POST',headers,body:JSON.stringify({workspace,libraryId:'samples',limit:50})});const list=await listResponse.json();
  const docs=list.documents||list.entries||[];check('local-catalogue',listResponse.ok&&docs.length>0);
  const doc=docs.find(d=>d.fileHandle);check('catalogue-readable-handle',Boolean(doc));
  const response=await fetch(endpoint+'/reads',{method:'POST',headers,body:JSON.stringify({workspace,libraryId:'samples',fileHandle:doc.fileHandle,sourceVersion:doc.sourceVersion,startLine:1,maxChars:500,maxLines:10,session:'package-acceptance'})});const read=await response.json();
  const library=JSON.parse(readFileSync(join(target,'data/libraries.json'),'utf8')).libraries.find(l=>l.id==='samples');
  const catalog=JSON.parse(readFileSync(join(target,'data/catalogue.json'),'utf8'));
  const known=Object.values(catalog.documents).find(d=>d.documentId===doc.documentId);
  const raw=known?readFileSync(join(library.root,known.textFile)):null;
  check('read-version-offset-contract',response.ok&&doc.sourceVersion===read.sourceVersion&&verifyReadWindow(raw,read,known,{libraryId:'samples',requestedStartLine:1}),{verification:'source-codepoint-and-lines'});
  execute('stop-owned-service',join(app(),'trial/stop.mjs'),['--target',target]);active=false;
  let reachable=false;try{await fetch(endpoint+'/health',{headers,signal:AbortSignal.timeout(1500)});reachable=true;}catch{}
  check('stop-releases-service-port',!reachable);check('payload-unchanged-after-start-stop',verifyPayloadDirectory(app()).ok);
 }else report.runtimeAcceptance='not-run-no-runtime-supplied';
 report.completed=true;
}catch(error){report.completed=false;report.failure=error.message;process.exitCode=1;}finally{
 if(active)execute('cleanup-owned-service',join(app(),'trial/stop.mjs'),['--target',target]);save();
 console.log(JSON.stringify({completed:report.completed,passed:report.checks.filter(c=>c.passed).length,failed:report.checks.filter(c=>!c.passed).length,cleanWindowsVerified:false,report:join(work,'report.json')}));
}