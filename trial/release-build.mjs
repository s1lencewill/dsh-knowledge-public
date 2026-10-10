/** Running process must match the installed package; health alone is insufficient. */
import {existsSync,readFileSync} from 'node:fs';
import {join} from 'node:path';
export function compatibleInstalledHealth(app,health){
 if(!existsSync(join(app,'RELEASE-MANIFEST.json')))return true;
 try{
  const expected=JSON.parse(readFileSync(join(app,'lib/build-identity.json'),'utf8'));
  return /^[a-f0-9]{64}$/.test(expected.id) && health?.build?.id===expected.id && health.build.valid===true && health.build.protocol===expected.protocol && health.runtimeBuild?.status==='verified';
 }catch{return false;}
}
