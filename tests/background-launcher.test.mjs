import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,mkdirSync,readFileSync,writeFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {spawnSync} from 'node:child_process';
import {ensureBackgroundLauncher} from '../trial/background-launcher.mjs';
test('no-window launcher uses GUI subsystem, propagates child exit, rejects foreign script and shell text',{skip:process.platform!=='win32'},()=>{
 const dir=mkdtempSync(join(tmpdir(),'kw-launch 中文 '));try{
  const exe=resolve(ensureBackgroundLauncher(dir)),bytes=readFileSync(exe),pe=bytes.readUInt32LE(0x3c);assert.equal(bytes.readUInt16LE(pe+24+68),2,'Windows GUI subsystem, not console');
  const trial=join(dir,'app','trial');mkdirSync(trial,{recursive:true});const script=join(trial,'service-task.mjs');writeFileSync(script,'process.exit(23);');
  const args=[process.execPath,script,'supervise',dir];const run=spawnSync(exe,args,{windowsHide:true,shell:false,timeout:10000});assert.equal(run.status,23);
  assert.equal(spawnSync(exe,[...args.slice(0,2),'revive & echo no',dir],{windowsHide:true}).status,64);
  assert.equal(spawnSync(exe,[process.execPath,join(dir,'other.mjs'),'revive',dir],{windowsHide:true}).status,65);
  assert.equal(ensureBackgroundLauncher(dir),exe);
 }finally{rmSync(dir,{recursive:true,force:true});}
});
