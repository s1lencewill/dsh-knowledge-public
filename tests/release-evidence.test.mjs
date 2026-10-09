import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,writeFileSync,existsSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {spawnSync} from 'node:child_process';
import {gzipSync} from 'node:zlib';
import {readProvenance} from '../scripts/package-release.mjs';
import {REQUIRED_INSTALL_CHECKS,assertInstallEvidence,assertProvenance,verifyReadWindow} from '../scripts/release-evidence.mjs';
import {makeManifest,sha256} from '../trial/artifact.mjs';
const candidate='a'.repeat(64),previous='b'.repeat(64);
function valid(){return {schemaVersion:2,completed:true,checks:REQUIRED_INSTALL_CHECKS.map(name=>({name,passed:true,...(name==='candidate-archive-audit'?{archiveSHA256:candidate}:{}),...(name==='previous-archive-audit'?{archiveSHA256:previous}:{}),...(name==='read-version-offset-contract'?{verification:'source-codepoint-and-lines'}:{})}))};}
test('installation evidence requires every mandatory check, including runtime/stop, once',()=>{
 assert.equal(assertInstallEvidence(valid(),candidate,previous),true);
 for(const name of REQUIRED_INSTALL_CHECKS){const report=valid();report.checks=report.checks.filter(c=>c.name!==name);assert.throws(()=>assertInstallEvidence(report,candidate,previous));}
 const duplicate=valid();duplicate.checks.push(duplicate.checks[0]);assert.throws(()=>assertInstallEvidence(duplicate,candidate,previous));
});
test('failed, skipped, truthy or incomplete results cannot grant installation acceptance',()=>{
 for(const result of [false,undefined,'true',1]){const report=valid();report.checks[0].passed=result;assert.throws(()=>assertInstallEvidence(report,candidate,previous));}
 const report=valid();report.checks.push({name:'optional-cleanup',passed:false});assert.throws(()=>assertInstallEvidence(report,candidate,previous));
 for(const done of [false,'true',1])assert.throws(()=>assertInstallEvidence({...valid(),completed:done},candidate,previous));
 assert.throws(()=>assertInstallEvidence({completed:true,checks:[{name:'candidate-archive-audit',archiveSHA256:candidate,passed:false}]},candidate,previous));
});
test('both exact package hashes and the upgraded read-check contract are mandatory',()=>{
 assert.throws(()=>assertInstallEvidence(valid(),previous,candidate));
 for(const name of ['candidate-archive-audit','previous-archive-audit']){const report=valid();report.checks.find(c=>c.name===name).archiveSHA256='c'.repeat(64);assert.throws(()=>assertInstallEvidence(report,candidate,previous));}
 const old=valid();old.schemaVersion=1;assert.throws(()=>assertInstallEvidence(old,candidate,previous));
 const weak=valid();delete weak.checks.find(c=>c.name==='read-version-offset-contract').verification;assert.throws(()=>assertInstallEvidence(weak,candidate,previous));
});
test('Git command failure, signal and invalid output never mean a clean source tree',()=>{
 for(const result of [{status:1,stdout:''},{status:null,stdout:''},{status:0,stdout:'',error:Error('missing')},{status:0,stdout:'a'.repeat(40),signal:'SIGTERM'}])assert.throws(()=>readProvenance(()=>result),/GIT_PROVENANCE_UNAVAILABLE/);
 for(const commit of ['', 'HEAD', 'a'.repeat(39), 'a'.repeat(40)+'\ntrailing'])assert.throws(()=>readProvenance(()=>({status:0,stdout:commit})),/GIT_COMMIT_INVALID/);
 let n=0;assert.throws(()=>readProvenance(()=>++n===1?{status:0,stdout:'a'.repeat(40)}:{status:128,stdout:''}),/GIT_PROVENANCE_UNAVAILABLE/);
});
test('confirmed Git results distinguish clean/dirty and Beta rejects unknown provenance',()=>{
 for(const dirty of [false,true]){let n=0;const p=readProvenance(()=>({status:0,stdout:++n===1?'a'.repeat(40):(dirty?' M README.md':'')}));assert.equal(p.dirty,dirty);assertProvenance(p);}
 for(const p of [{commit:'',dirty:false,source:'working-tree'},{commit:'a'.repeat(40),source:'working-tree'},{commit:'a'.repeat(40),dirty:0,source:'working-tree'}])assert.throws(()=>assertProvenance(p));
});
function readFixture(){
 const raw=Buffer.from('\uFEFF标题😀\r\n中𠀀文 value 1.0 K.\r\n尾行\r\n');
 const text=raw.toString('utf8').replace(/^\uFEFF/,'').replace(/\r\n/g,'\n');const chars=Array.from(text),start=chars.indexOf('\n')+1,end=start+8;
 const document={textFile:'synthetic.md',textSHA256:sha256(raw)};
 const read={sourceVersion:sha256(raw),libraryId:'samples',relativePath:'synthetic.md',startChar:start,endChar:end,startLine:2,endLine:2,startOffset:0,endOffset:8,excerpt:chars.slice(start,end).join('')};
 return {raw,read,document,options:{libraryId:'samples',requestedStartLine:2}};
}
test('BOM/CRLF/non-BMP source reconstruction uses codepoints and preserves partial last lines',()=>{
 const f=readFixture();assert.equal(verifyReadWindow(f.raw,f.read,f.document,f.options),true);
 assert.notEqual(f.read.excerpt.length,f.read.endChar-f.read.startChar);
 const text=f.raw.toString('utf8').replace(/^\uFEFF/,'').replace(/\r\n/g,'\n'),chars=Array.from(text);const end=chars.indexOf('\n',f.read.startChar)+1;
 const read={...f.read,endChar:end,endLine:3,endOffset:0,excerpt:chars.slice(f.read.startChar,end).join('')};assert.equal(verifyReadWindow(f.raw,read,f.document,f.options),true);
});
test('same-length wrong text, position or line metadata cannot pass',()=>{
 const f=readFixture();
 for(const delta of [{excerpt:'X'.repeat(Array.from(f.read.excerpt).length)},{startChar:f.read.startChar+1,endChar:f.read.endChar+1},{startLine:1},{endLine:3},{startOffset:1},{endOffset:7},{startChar:NaN},{endChar:99999}])assert.equal(verifyReadWindow(f.raw,{...f.read,...delta},f.document,f.options),false);
 assert.equal(verifyReadWindow(f.raw,f.read,f.document,{...f.options,requestedStartLine:1}),false);
});
test('wrong document/library, raw source hash or invalid UTF-8 fail reconstruction',()=>{
 const f=readFixture();for(const delta of [{relativePath:'other.md'},{libraryId:'other'},{sourceVersion:'c'.repeat(64)}])assert.equal(verifyReadWindow(f.raw,{...f.read,...delta},f.document,f.options),false);
 assert.equal(verifyReadWindow(f.raw,f.read,{...f.document,textSHA256:'c'.repeat(64)},f.options),false);
 const bad=Buffer.from([255]);assert.equal(verifyReadWindow(bad,{...f.read,sourceVersion:sha256(bad)},{...f.document,textSHA256:sha256(bad)},f.options),false);
});
function tar(members){const blocks=[];for(const [path,bytes]of members){const h=Buffer.alloc(512);h.write('package/'+path);h.write('0000644',100);h.write('0000000',108);h.write('0000000',116);h.write(bytes.length.toString(8).padStart(11,'0')+'\0',124);h.write('00000000000\0',136);h.fill(32,148,156);h.write('0',156);h.write('ustar\0',257);h.write('00',263);h.write(h.reduce((n,b)=>n+b,0).toString(8).padStart(6,'0')+'\0 ',148);blocks.push(h,bytes,Buffer.alloc((512-bytes.length%512)%512));}return gzipSync(Buffer.concat([...blocks,Buffer.alloc(1024)]));}
test('real preparation CLI rejects the reported false-pass reproduction before creating a kit',()=>{
 const dir=mkdtempSync(join(tmpdir(),'kb-gate-'));
 try{
  const members=new Map([['package.json',Buffer.from(JSON.stringify({name:'dsh-knowledge',version:'0.1.0',peerDependencies:{},engines:{node:'>=24'}}))],['engine/requirements-win-py313.lock',Buffer.from('synthetic==1')]]);
  members.set('RELEASE-MANIFEST.json',Buffer.from(JSON.stringify(makeManifest(members,{commit:'a'.repeat(40),dirty:true,source:'working-tree'}))));
  const archive=join(dir,'test.tgz'),report=join(dir,'report.json'),out=join(dir,'kit');writeFileSync(archive,tar(members));
  writeFileSync(report,JSON.stringify({schemaVersion:2,completed:true,checks:[{name:'candidate-archive-audit',archiveSHA256:sha256(readFileSyncCompat(archive)),passed:false}]}));
  const run=spawnSync(process.execPath,[resolve('scripts/prepare-private-beta.mjs'),'--archive',archive,'--previous-archive',archive,'--install-report',report,'--out',out],{encoding:'utf8',windowsHide:true});
  assert.notEqual(run.status,0);assert.equal(existsSync(out),false);assert.match(run.stderr,/INSTALL_EVIDENCE/);
 }finally{rmSync(dir,{recursive:true,force:true});}
});
import {readFileSync as readFileSyncCompat} from 'node:fs';