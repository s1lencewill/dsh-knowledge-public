import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,readFileSync,rmSync,mkdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {LibrarySelection} from '../lib/selection.js';
import {LibraryPanel} from '../lib/library-panel.js';
import {protocolText} from '../lib/protocol.js';

test('workspace defaults apply to new sessions only; explicit choices and none survive reload',()=>{
 const dir=mkdtempSync(join(tmpdir(),'onboarding-'));
 try {
  const file=join(dir,'selection.json'),s=new LibrarySelection(500,file),space=dir;
  s.observe('old',space,Date.now()-10000);s.setDefault(space,'A');
  s.observe('new',space,Date.now()+1000);assert.equal(s.get('new'),'A');assert.equal(s.get('old'),undefined);
  s.setDefault(space,'B');assert.equal(s.get('new'),'A');s.observe('newer',space,Date.now()+2000);assert.equal(s.get('newer'),'B');
  s.use('old','A');s.clear('new');const reload=new LibrarySelection(500,file);
  reload.observe('new',space,Date.now()+2000);assert.equal(reload.get('new'),undefined);assert.equal(reload.get('old'),'A');
  reload.observe('restored-unseen',space,Date.now()-100000);assert.equal(reload.get('restored-unseen'),undefined);
 } finally{rmSync(dir,{recursive:true,force:true});}
});
test('panel and commands share selection; forged browser workspace and hidden libraries refused',async()=>{
 const old=globalThis.fetch,s=new LibrarySelection(),panel=new LibraryPanel(s,()=>({url:'http://127.0.0.1:19999',token:'synthetic-token-abcdefghijklmnopqrstuvwxyz'}));
 panel.sessions={get:id=>id==='session-a'?{header:{cwd:process.cwd(),createdAt:Date.now()}}:null};let sent=[];
 try{
  globalThis.fetch=async(url,options)=>{
   assert.equal(url,'http://127.0.0.1:19999/library-admin');const body=JSON.parse(options.body);sent.push(body);
   assert.equal(body.workspace,process.cwd());return {ok:true,json:async()=>({libraries:[{id:'A',name:'A',documents:2,state:'ready'}]})};
  };
  await panel.act('session-a',{libraryAction:'select',libraryId:'A'});assert.equal(s.get('session-a'),'A');
  await assert.rejects(panel.act('session-a',{libraryAction:'select',libraryId:'B'}),/LIBRARY_NOT_VISIBLE/);
  await assert.rejects(panel.act('session-a',{libraryAction:'select',libraryId:'A',workspace:'forged'}),/INVALID_LIBRARY_ACTION/);
  await assert.rejects(panel.project('session-missing'),/SESSION_UNAVAILABLE/);
  assert.equal(sent.some(v=>v.action==='preview'),false);assert.equal(sent.some(v=>v.action==='publish'),false);
  const original=s.get('session-a');await panel.act('session-a',{libraryAction:'default',libraryId:null});assert.equal(s.get('session-a'),original);
 }finally{globalThis.fetch=old;}
});
test('changing session scope preserves captured dispatch and rejects later other-library calls',()=>{
 const s=new LibrarySelection();s.use('a','A');const dispatched=s.get('a');s.assertAllowed('a','A');
 s.use('a','B');assert.equal(dispatched,'A');assert.throws(()=>s.assertAllowed('a','A'));s.assertAllowed('a','B');
});
test('coverage guidance distinguishes inventory, source hierarchy and incomparable systems',()=>{
 const text=protocolText();
 assert.doesNotMatch(text,/first two calls is the intended|summarise the whole collection.*call knowledge_list_documents/s);
 for(const phrase of ['uncovered dimensions','original research','Listed document counts','Different systems','execution failed','not full-text coverage'])assert.ok(text.includes(phrase),phrase);
 const index=readFileSync(new URL('../src/index.ts',import.meta.url),'utf8');assert.doesNotMatch(index,/coverage is complete/);
});

test('failed persistence leaves both memory and disk at the previous selection',()=>{
 const dir=mkdtempSync(join(tmpdir(),'onboarding-fail-'));
 try {
  const file=join(dir,'selection.json'),s=new LibrarySelection(500,file);s.use('a','A');const bytes=readFileSync(file,'utf8');
  mkdirSync(file+'.tmp');assert.throws(()=>s.use('a','B'),/SELECTION_NOT_SAVED/);
  assert.equal(s.get('a'),'A');assert.equal(readFileSync(file,'utf8'),bytes);
  assert.throws(()=>s.setDefault(dir,'B'),/SELECTION_NOT_SAVED/);assert.equal(s.defaultFor(dir),null);
 }finally{rmSync(dir,{recursive:true,force:true});}
});

test('management errors discard arbitrary response text and unknown code-shaped secrets',async()=>{
 const old=globalThis.fetch,p=new LibraryPanel(new LibrarySelection(),()=>({url:'http://127.0.0.1:19999',token:'synthetic-token-abcdefghijklmnopqrstuvwxyz'}));
 p.sessions={get:()=>({header:{cwd:process.cwd(),createdAt:Date.now()}})};
 try {globalThis.fetch=async()=>({ok:false,json:async()=>({code:'PRIVATE_ALPHA_SECRET',detail:'private/source/path'})});
 await assert.rejects(p.project('session-a'),e=>e.message==='LIBRARY_ACTION_FAILED');
 }finally{globalThis.fetch=old;}
});

test('an action cannot smuggle irrelevant folder or consent parameters into selection',async()=>{
 const p=new LibraryPanel(new LibrarySelection(),()=>{throw Error('HTTP must not happen');});
 p.sessions={get:()=>({header:{cwd:process.cwd()}})};
 await assert.rejects(p.act('session-a',{libraryAction:'select',libraryId:'A',root:process.cwd()}),/INVALID_LIBRARY_ACTION/);
 await assert.rejects(p.act('session-a',{libraryAction:'preview',libraryId:'A',root:process.cwd()}),/INVALID_LIBRARY_ACTION/);
});
