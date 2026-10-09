// Locally built Windows-subsystem entry point for interactive scheduled tasks.
// The scheduler waits for this process, which waits for the trusted Node action.
import {createHash} from 'node:crypto';
import {existsSync,mkdirSync,readFileSync,renameSync,writeFileSync} from 'node:fs';
import {join,dirname} from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
export function ensureBackgroundLauncher(data) {
  if(process.platform!=='win32')throw Error('WINDOWS_REQUIRED');
  const source=fileURLToPath(new URL('./background-launcher.cs',import.meta.url));
  const hash=createHash('sha256').update(readFileSync(source)).digest('hex');
  const dir=join(data,'launchers'),exe=join(dir,'background-launcher.exe'),stamp=join(dir,'background-launcher.json');
  if(existsSync(exe)&&existsSync(stamp)){try{const m=JSON.parse(readFileSync(stamp));if(m.sourceSHA256===hash&&m.exeSHA256===createHash('sha256').update(readFileSync(exe)).digest('hex'))return exe;}catch{}}
  const windows=process.env.WINDIR||'C:/Windows';
  const compiler=['Framework64','Framework'].map(k=>join(windows,'Microsoft.NET',k,'v4.0.30319','csc.exe')).find(existsSync);
  if(!compiler)throw Error('BACKGROUND_COMPILER_UNAVAILABLE');
  mkdirSync(dir,{recursive:true});const tmp=join(dir,'background-launcher.new.exe');
  const result=spawnSync(compiler,['/nologo','/target:winexe','/optimize+','/out:'+tmp,source],{windowsHide:true,shell:false,encoding:'utf8',timeout:30000});
  if(result.status!==0||!existsSync(tmp))throw Error('BACKGROUND_LAUNCHER_BUILD_FAILED');
  renameSync(tmp,exe);writeFileSync(stamp,JSON.stringify({sourceSHA256:hash,exeSHA256:createHash('sha256').update(readFileSync(exe)).digest('hex')}));return exe;
}
