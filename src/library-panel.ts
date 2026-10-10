/** Human control plane. Workspace is read from the host session, never from browser or model arguments. */
import { RUNNING_BUILD, requireCompatibleService } from './build.js';
import { LibrarySelection } from './selection.js';
export const LIBRARY_CODES=new Set(['BUILD_MISMATCH','SESSION_UNAVAILABLE','SERVICE_UNAVAILABLE','INVALID_LIBRARY_ACTION','LIBRARY_NOT_VISIBLE','FOLDER_INVALID','NAME_INVALID','UPDATE_UNAVAILABLE','UPDATE_WORKSPACE_MISMATCH','FOLDER_OUTSIDE_WORKSPACE','FOLDER_OVERLAPS_STORAGE','LINK_NOT_ALLOWED','WORKSPACE_TOO_BROAD','LIBRARY_CAPACITY_EXCEEDED','SOURCE_CHANGED','CONFIG_INVALID','PREVIEW_LIMIT','CONSENT_REQUIRED','PREVIEW_EXPIRED','PREVIEW_FOREIGN','PREFLIGHT_FAILED','PUBLISH_BUSY','CONFIG_CHANGED','LOCAL_BUILD_FAILED','LIBRARY_ACTION_FAILED','SELECTION_FILE_UNREADABLE','SELECTION_NOT_SAVED']);
const ACTIONS=new Set(['select','default','preview','publish','cancel','refresh','remove']);
const FIELDS=new Set(['libraryAction','libraryId','root','name','previewId','consent']);
export class LibraryPanel {
  sessions: any = null;
  constructor(private selection: LibrarySelection, private endpoint:()=>{url:string;token:string}) {}
  private context(id:string) {
    const header=this.sessions?.get(id)?.header;
    if(typeof header?.cwd!=='string')throw Error('SESSION_UNAVAILABLE');
    this.selection.observe(id,header.cwd,header.createdAt);
    return {workspace:header.cwd,session:id};
  }
  private async request(context:any,value:any) {
    const {url,token}=this.endpoint(),parsed=new URL(url);
    if(parsed.protocol!=='http:' || parsed.hostname!=='127.0.0.1' || parsed.username || parsed.password || token.length<24)throw Error('SERVICE_UNAVAILABLE');
    try{await requireCompatibleService(parsed.origin,token,RUNNING_BUILD);}catch{throw Error('BUILD_MISMATCH');}
    const response=await fetch(parsed.origin+'/library-admin',{method:'POST',redirect:'error',
      headers:{Authorization:`Bearer ${token}`,'Content-Type':'application/json',...(RUNNING_BUILD?{'X-Knowledge-Build':RUNNING_BUILD.id}:{})},
      body:JSON.stringify({...value,...context}),signal:AbortSignal.timeout(value.action==='preview'||value.action==='publish'?120000:5000)});
    const body=await response.json();
    if(!response.ok)throw Error(typeof body.code==='string' && LIBRARY_CODES.has(body.code)?body.code:'LIBRARY_ACTION_FAILED');
    return body;
  }
  async project(id:string) {
    const context=this.context(id),value=await this.request(context,{action:'list'});
    const selected=this.selection.get(id)??null;
    return {...value,workspace:context.workspace,selectedLibraryId:selected,workspaceDefault:this.selection.defaultFor(context.workspace),
      selectionUnavailable:selected!==null&&!value.libraries.some((v:any)=>v.id===selected),selectionReadable:this.selection.readable,selectionPersisted:this.selection.persisted};
  }
  async act(id:string,value:any) {
    if(!value || Object.keys(value).some(k=>!FIELDS.has(k)) || !ACTIONS.has(value.libraryAction))throw Error('INVALID_LIBRARY_ACTION');
    const fields: Record<string,string[]>={select:['libraryId'],default:['libraryId'],refresh:[],preview:['libraryId','root','name'],publish:['previewId','consent'],cancel:['previewId'],remove:['libraryId','consent']};
    if(Object.keys(value).some(k=>k!=='libraryAction'&&!fields[value.libraryAction]?.includes(k)) || (value.libraryAction==='preview'&&value.libraryId!==undefined&&value.root!==undefined))throw Error('INVALID_LIBRARY_ACTION');
    const context=this.context(id);
    if(['select','default'].includes(value.libraryAction)) {
      if(value.libraryId!==null && typeof value.libraryId!=='string')throw Error('INVALID_LIBRARY_ACTION');
      const listed=await this.request(context,{action:'list'});
      if(value.libraryId!==null && !listed.libraries.some((v:any)=>v.id===value.libraryId))throw Error('LIBRARY_NOT_VISIBLE');
      if(value.libraryAction==='select'){if(value.libraryId===null)this.selection.clear(id);else this.selection.use(id,value.libraryId);}
      else this.selection.setDefault(context.workspace,value.libraryId);
      return {code:value.libraryAction==='select'?'SESSION_LIBRARY_CHANGED':'WORKSPACE_DEFAULT_CHANGED',...await this.project(id)};
    }
    if(value.libraryAction==='refresh')return this.project(id);
    if(value.root!==undefined && (typeof value.root!=='string' || value.root.length>512))throw Error('FOLDER_INVALID');
    if(value.name!==undefined && (typeof value.name!=='string' || value.name.length>120))throw Error('NAME_INVALID');
    const {libraryAction,...arguments_}=value;
    const result=await this.request(context,{...arguments_,action:libraryAction});
    if(libraryAction==='remove') {
      if(this.selection.defaultFor(context.workspace)===value.libraryId)this.selection.setDefault(context.workspace,null);
      if(this.selection.get(id)===value.libraryId)this.selection.clear(id);
      return {...result,...await this.project(id)};
    }
    return result;
  }
}
