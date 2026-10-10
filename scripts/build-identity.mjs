import {createHash} from 'node:crypto';
import {readFileSync,readdirSync,mkdirSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
export const PLACEHOLDER='__KNOWLEDGE_CLIENT_BUILD_ID__';
export const sha=b=>createHash('sha256').update(b).digest('hex');
export function renderBrowser(source,id,name='dsh-knowledge'){if(!/^[a-f0-9]{64}$/.test(id)||!/^(@[a-z0-9-]+\/)?[a-z0-9-]+$/.test(name))throw Error('BUILD_ID_INVALID');return source.replaceAll(PLACEHOLDER,id).replace(/id:\s*['"]dsh-knowledge['"]/, 'id: '+JSON.stringify(name));}
export function createIdentity(root){
 const pkg=JSON.parse(readFileSync(join(root,'package.json'),'utf8')),files={};
 for(const tree of ['engine','lib'])for(const name of readdirSync(join(root,tree)).sort()){
  if(tree==='engine'?!name.endsWith('.py'):!name.endsWith('.js')||name==='browser.js')continue;
  const p=tree+'/'+name;files[p]=sha(readFileSync(join(root,p)));
 }
 const runtimeInputs={};
 const staging=readFileSync(join(root,'engine/staging.py'),'utf8');
 for(const section of ['ENGINE_FILES','BUILD_INPUTS']){
  const match=staging.match(new RegExp(section+' = \\(([\\s\\S]*?)\\)'));
  if(!match)throw Error('RUNTIME_INPUT_LIST_MISSING');
  for(const m of match[1].matchAll(/"([^"]+)"/g)){const p=section==='ENGINE_FILES'?'engine/'+m[1]:m[1];runtimeInputs[p]=sha(readFileSync(join(root,p)));}
 }
 const browserSourceHash=sha(readFileSync(join(root,'ui/source-card.js')));
 const basis={schemaVersion:1,protocol:1,name:pkg.name,version:pkg.version,hostPeers:pkg.peerDependencies,nodeLockSHA256:sha(readFileSync(join(root,'package-lock.json'))),files,runtimeInputs,browserSourceHash};
 const id=sha(JSON.stringify(basis));
 return {...basis,id,browserSHA256:sha(Buffer.from(renderBrowser(readFileSync(join(root,'ui/source-card.js'),'utf8'),id,pkg.name)))};
}
export function buildIdentity(root){
 const pkg=JSON.parse(readFileSync(join(root,'package.json'),'utf8'));
 for(const name of ['@deepseek-ai/dsh-tools','@deepseek-ai/dsh-llm']){
  const declared=pkg.devDependencies?.[name];
  let local;try{local=JSON.parse(readFileSync(join(root,'node_modules',name,'package.json'),'utf8'));}catch{throw Error('BUILD_SDK_NOT_LOCAL');}
  if(declared!==pkg.peerDependencies?.[name] || local.version!==declared)throw Error('BUILD_SDK_VERSION_MISMATCH');
 }
 const identity=createIdentity(root);mkdirSync(join(root,'lib'),{recursive:true});writeFileSync(join(root,'lib/build-identity.json'),JSON.stringify(identity,null,2)+'\n');return identity;}
if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href)console.log(buildIdentity(resolve(fileURLToPath(new URL('..',import.meta.url)))).id);
