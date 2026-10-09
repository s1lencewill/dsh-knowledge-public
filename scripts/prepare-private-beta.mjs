/** Prepare a private acceptance kit. A kit cannot grant itself clean-machine/page/release approval. */
import {mkdirSync,readFileSync,writeFileSync,copyFileSync,existsSync} from 'node:fs';
import {dirname,resolve,join,basename} from 'node:path';
import {auditArchive,sha256} from '../trial/artifact.mjs';
import {assertInstallEvidence,assertProvenance} from './release-evidence.mjs';
const options={};for(let i=2;i<process.argv.length;i++){const key=process.argv[i];if(!['--archive','--previous-archive','--install-report','--out'].includes(key)||!process.argv[i+1])throw Error('INVALID_ARGUMENT');options[key.slice(2)]=process.argv[++i];}
for(const key of ['archive','previous-archive','install-report','out'])if(!options[key])throw Error('REQUIRED_'+key);
const candidate=auditArchive(resolve(options.archive)),previous=auditArchive(resolve(options['previous-archive']));
if(!candidate.ok||!previous.ok)throw Error('ARCHIVE_AUDIT_FAILED');
const install=JSON.parse(readFileSync(options['install-report'],'utf8'));
assertInstallEvidence(install,candidate.archiveSHA256,previous.archiveSHA256);
assertProvenance(candidate.manifest.provenance);
assertProvenance(previous.manifest.provenance);
const out=resolve(options.out);if(existsSync(out))throw Error('OUTPUT_ALREADY_EXISTS');mkdirSync(out,{recursive:true});
const root=resolve(import.meta.dirname,'..');const copies=[
 [resolve(options.archive),basename(options.archive)],[resolve(options['previous-archive']),basename(options['previous-archive'])],
 [join(root,'scripts/accept-release.mjs'),'scripts/accept-release.mjs'],[join(root,'scripts/release-evidence.mjs'),'scripts/release-evidence.mjs'],[join(root,'trial/artifact.mjs'),'trial/artifact.mjs'],
 [join(root,'docs/PRIVATE-BETA.md'),'README.md'],[join(root,'THIRD-PARTY-NOTICES.md'),'THIRD-PARTY-NOTICES.md']];
const inventory={};for(const [source,path]of copies){const target=join(out,path);mkdirSync(dirname(target),{recursive:true});copyFileSync(source,target);inventory[path]=sha256(readFileSync(target));}
const status={schemaVersion:1,status:'blocked-candidate',version:candidate.manifest.version,archiveSHA256:candidate.archiveSHA256,
 gates:{archiveAudit:true,sameMachineInstallUpgradeRollback:true,cleanSource:!candidate.manifest.provenance.dirty,realRightbarPage:false,cleanWindows:false,privateBetaAccepted:false},
 provenance:candidate.manifest.provenance,runtimeBundled:false,modelCallsDuringInstall:install.modelCalls,
 installationChecks:install.checks.map(({name,passed})=>({name,passed})),
 missing:['real DSH rightbar page acceptance','clean Windows installation and operational rollback','clean-checkout source provenance'],
 publicRelease:false,licenceStatus:'owner-decision-pending'};
writeFileSync(join(out,'BETA-READINESS.json'),JSON.stringify(status,null,2)+'\n');inventory['BETA-READINESS.json']=sha256(readFileSync(join(out,'BETA-READINESS.json')));
writeFileSync(join(out,'KIT-SHA256SUMS'),Object.entries(inventory).map(([path,hash])=>hash+'  '+path).join('\n')+'\n');
console.log(JSON.stringify({out,status:status.status,version:status.version,archiveSHA256:status.archiveSHA256,gates:status.gates}));