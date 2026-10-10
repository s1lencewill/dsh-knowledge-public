import {readFileSync,existsSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {join} from 'node:path';
import {createHash} from 'node:crypto';
import {KnowledgeFailure} from './failures.js';
export interface BuildIdentity {id:string;protocol:number;version:string;runtimeInputs:Record<string,string>;browserSHA256:string;valid?:boolean;}
const hex=(s:unknown):s is string=>typeof s==='string'&&/^[a-f0-9]{64}$/.test(s);
const digest=(b:Buffer)=>createHash('sha256').update(b).digest('hex');
// Snapshot once at module load. Updating files cannot change the identity of a running old host.
export function releaseBuild(root=fileURLToPath(new URL('..',import.meta.url))):BuildIdentity|null{
 if(!existsSync(join(root,'RELEASE-MANIFEST.json')))return null;
 try{
  const b=JSON.parse(readFileSync(join(root,'lib/build-identity.json'),'utf8'));
  if(b.schemaVersion!==1||b.protocol!==1||!hex(b.id)||!hex(b.browserSHA256)||!b.runtimeInputs||!Object.keys(b.runtimeInputs).length)throw Error();
  for(const [path,hash] of Object.entries(b.files??{})){
   if(!/^(lib|engine)\/[\w.-]+\.(js|py)$/.test(path)||!hex(hash)||digest(readFileSync(join(root,path)))!==hash)throw Error();
  }
  if(!Object.keys(b.files??{}).length||digest(readFileSync(join(root,'lib/browser.js')))!==b.browserSHA256)throw Error();
  return {...b,valid:true};
 }catch{return {id:'invalid',protocol:1,version:'unknown',runtimeInputs:{},browserSHA256:'',valid:false};}
}
export const RUNNING_BUILD=releaseBuild();
export function compatibleHealth(health:any,build:BuildIdentity|null=RUNNING_BUILD):boolean{
 if(!build)return true; // Source-only development is explicit, not a release compatibility claim.
 return build.valid!==false && health?.build?.valid===true && health.build.id===build.id && health.build.protocol===build.protocol && health.runtimeBuild?.status==='verified';
}
export async function requireCompatibleService(url:string,token:string,build:BuildIdentity|null=RUNNING_BUILD,signal?:AbortSignal){
 if(!build)return;
 if(build.valid===false)throw new KnowledgeFailure('KNOWLEDGE_BUILD_MISMATCH');
 const deadline=AbortSignal.timeout(5000);
 const response=await fetch(url+'/health',{headers:{Authorization:`Bearer ${token}`},redirect:'error',signal:signal?AbortSignal.any([signal,deadline]):deadline});
 if(response.ok && !compatibleHealth(await response.json(),build))throw new KnowledgeFailure('KNOWLEDGE_BUILD_MISMATCH');
 if(!response.ok)throw new KnowledgeFailure(response.status===401?'KNOWLEDGE_SERVICE_UNAUTHORIZED':'KNOWLEDGE_SERVICE_UNAVAILABLE');
}
