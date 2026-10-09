/** Closed acceptance contract; report validation is not report authenticity. */
import {sha256} from '../trial/artifact.mjs';
export const REQUIRED_INSTALL_CHECKS = Object.freeze([
 'candidate-archive-audit','previous-archive-audit','install-previous','previous-installed-integrity',
 'damaged-upgrade-refused','damaged-upgrade-preserves-app','damaged-upgrade-preserves-data',
 'upgrade-candidate','candidate-installed-integrity','upgrade-preserves-user-data','previous-app-retained',
 'rollback-integrity','rollback-restores-old-browser','rollback-preserves-user-data','upgrade-after-rollback',
 'reupgrade-integrity','reupgrade-preserves-user-data','missing-runtime-doctor','missing-runtime-start-refused',
 'place-explicit-runtime','doctor-with-runtime','start-with-isolation','health-isolation-required-active',
 'local-catalogue','catalogue-readable-handle','read-version-offset-contract','stop-owned-service',
 'stop-releases-service-port','payload-unchanged-after-start-stop',
]);
export function assertProvenance(value){
 if(!value||!/^([a-f0-9]{40}|[a-f0-9]{64})$/.test(value.commit)||typeof value.dirty!=='boolean'||value.source!=='working-tree')throw Error('SOURCE_PROVENANCE_INVALID');
 return value;
}
export function assertInstallEvidence(report,candidateHash,previousHash){
 if(report?.schemaVersion!==2||report.completed!==true||!Array.isArray(report.checks))throw Error('INSTALL_EVIDENCE_WRONG_OR_INCOMPLETE');
 const entries=new Map();
 for(const entry of report.checks){
  if(!entry||typeof entry.name!=='string'||entry.passed!==true||entries.has(entry.name))throw Error('INSTALL_EVIDENCE_FAILED_OR_DUPLICATE');
  entries.set(entry.name,entry);
 }
 if(REQUIRED_INSTALL_CHECKS.some(name=>!entries.has(name)))throw Error('INSTALL_EVIDENCE_MISSING_CHECK');
 for(const [name,hash]of [['candidate-archive-audit',candidateHash],['previous-archive-audit',previousHash]]){
  if(!/^[a-f0-9]{64}$/.test(hash)||entries.get(name).archiveSHA256!==hash)throw Error('INSTALL_EVIDENCE_ARCHIVE_MISMATCH');
 }
 if(entries.get('read-version-offset-contract').verification!=='source-codepoint-and-lines')throw Error('INSTALL_EVIDENCE_LEGACY_READ_CHECK');
 return true;
}
/** Python str offsets: UTF-8 strict decode, drop leading BOM, CRLF -> LF; codepoints, not UTF-16. */
export function verifyReadWindow(raw,read,document,{libraryId,requestedStartLine=1}={}){
 try{
  if(!Buffer.isBuffer(raw)||!document||!read) return false;
  const hash=sha256(raw);
  if(document.textSHA256!==hash||read.sourceVersion!==hash||read.relativePath!==document.textFile||read.libraryId!==libraryId)return false;
  const text=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(raw).replace(/^\uFEFF/,'').replace(/\r\n/g,'\n');
  const chars=Array.from(text),{startChar:start,endChar:end}=read;
  if(!Number.isSafeInteger(start)||!Number.isSafeInteger(end)||start<0||end<=start||end>chars.length||typeof read.excerpt!=='string')return false;
  if(chars.slice(start,end).join('')!==read.excerpt)return false;
  const countNewlines=part=>part.filter(c=>c==='\n').length;
  const startLine=1+countNewlines(chars.slice(0,start)),endLine=1+countNewlines(chars.slice(0,end));
  const startOffset=start-(chars.slice(0,start).lastIndexOf('\n')+1),endOffset=end-(chars.slice(0,end).lastIndexOf('\n')+1);
  return read.startLine===startLine&&read.endLine===endLine&&read.startOffset===startOffset&&read.endOffset===endOffset&&startLine===requestedStartLine&&startOffset===0;
 }catch{return false;}
}
