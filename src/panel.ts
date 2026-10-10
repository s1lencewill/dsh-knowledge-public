import { RUNNING_BUILD, compatibleHealth, type BuildIdentity } from './build.js';
/** Authenticated, host-only panel bridge. No service credential reaches browser or model. */
import { readFileSync, writeFileSync, renameSync, existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { IncomingMessage, ServerResponse } from 'node:http';
import { execFile } from 'node:child_process';
import { MAX_TURN_KNOWLEDGE_CALLS } from './turnlimit.js';
import { ActiveQueries, type QueryStatus } from './operations.js';
import { LIBRARY_CODES, type LibraryPanel } from './library-panel.js';

const sessionValid = (value: unknown): value is string => typeof value === 'string' && /^session-[a-zA-Z0-9_-]{1,100}$/.test(value);
export class PanelSettings {
  private values = new Map<string, number>();
  readable = true;
  autoOpenMode: 'first-call' | 'manual' = 'first-call';
  constructor(private file?: string) {
    try { if (file && existsSync(file)) {
      const parsed = JSON.parse(readFileSync(file, 'utf8'));
      if (parsed.autoOpenMode === 'manual') this.autoOpenMode = 'manual';
      for (const [id, max] of Object.entries(parsed.sessions ?? {}).slice(-500)) {
        if (sessionValid(id) && Number.isInteger(max) && Number(max) >= 1 && Number(max) <= 30) this.values.set(id, Number(max));
      }
    } } catch { this.readable = false; this.values.clear(); }
  }
  get(id: string): number { return this.values.get(id) ?? MAX_TURN_KNOWLEDGE_CALLS; }
  set(id: unknown, max: unknown): void {
    if (!sessionValid(id) || !Number.isInteger(max) || Number(max) < 1 || Number(max) > 30) throw new Error('INVALID_SETTING');
    if (!this.readable) throw new Error("SETTINGS_UNREADABLE");
    const next = new Map(this.values); next.delete(id); next.set(id, Number(max));
    while (next.size > 500) next.delete(next.keys().next().value!);
    // Publish before updating memory; a failed write must not pretend the setting was saved.
    if (this.file) {
      const tmp = this.file + '.' + process.pid + '.tmp';
      writeFileSync(tmp, JSON.stringify({ version: 1, autoOpenMode: this.autoOpenMode, sessions: Object.fromEntries(next) }), { mode: 0o600 });
      renameSync(tmp, this.file);
    }
    this.values = next;
  }
  setAutoOpen(mode: unknown): void {
    if (!this.readable || typeof mode !== 'string' || !['manual', 'first-call'].includes(mode)) throw Error('INVALID_SETTING');
    if (this.file) {const tmp=this.file+'.'+process.pid+'.tmp';writeFileSync(tmp,JSON.stringify({version:1,autoOpenMode:mode,sessions:Object.fromEntries(this.values)}),{mode:0o600});renameSync(tmp,this.file);}
    this.autoOpenMode=mode as 'first-call'|'manual';
  }
  get persisted(): boolean { return Boolean(this.file); }
}

export function panelSettingsFile(tokenFile: unknown): string | undefined {
  return typeof tokenFile === 'string' && tokenFile.trim() ? join(dirname(resolve(tokenFile)), 'panel-settings.json') : undefined;
}

export function installedCommands(moduleUrl = import.meta.url): { start: string; stop: string } | null {
  const app = dirname(dirname(fileURLToPath(moduleUrl)));
  if (dirname(app) === app || !existsSync(join(app, 'trial', 'start.mjs')) || !existsSync(join(dirname(app), 'data', 'config.json'))) return null;
  const target = dirname(app);
  // PowerShell literal arguments, safe even when the installation directory contains quotes.
  const q = (value: string) => "'" + value.replaceAll("'", "''") + "'";
  return {
    start: `node ${q(join(app, 'trial', 'start.mjs'))} --target ${q(target)} --service-only`,
    stop: `node ${q(join(app, 'trial', 'service-task.mjs'))} stop --target ${q(target)}`,
  };
}

type ControlAction = 'start' | 'stop' | 'status';
type Runner = (file: string, args: string[]) => Promise<{code: number; output: string}>;
const runControl: Runner = (file, args) => new Promise(resolve => {
  execFile(process.execPath, [file, ...args], {windowsHide:true, timeout:120000, maxBuffer:262144},
    (error, stdout) => resolve({code:error ? 1 : 0, output:stdout}));
});
export class PanelController {
  private busy: Promise<{code:string;operation:string}> | null = null;
  state = 'idle';
  constructor(private target: string | null, private runner: Runner = runControl) {}
  get available() { return Boolean(this.target); }
  async act(action: ControlAction): Promise<{code:string;operation:string}> {
    if (action === 'status') return {code:'STATUS',operation:this.state};
    if (!this.target) return {code:'CONTROL_UNAVAILABLE',operation:'failed'};
    if (this.busy) return {code:'ACTION_BUSY',operation:this.state};
    const target = this.target;
    this.state = action === 'start' ? 'starting' : 'stopping';
    const work = async () => {
      const task = join(target,'app','trial','service-task.mjs');
      let file = task, args = [action,'--target',target];
      if (action === 'start') {
        const probe = await this.runner(task,['status','--target',target,'--json']);
        let auto: unknown; try { auto = JSON.parse(probe.output).autostart; } catch {}
        if (!['enabled','disabled','not-installed'].includes(String(auto))) throw Error('STATUS_UNAVAILABLE');
        // Disabled tasks stay disabled; this bridge never invokes enable/install.
        if (auto !== 'enabled') {file=join(target,'app','trial','start.mjs');args=['--target',target,'--service-only'];}
      }
      const result = await this.runner(file,args);
      if (result.code !== 0) throw Error('ACTION_FAILED');
      this.state='idle'; return {code:'ACTION_COMPLETED',operation:'idle'};
    };
    this.busy=work();
    try {return await this.busy;} catch {this.state='failed';return {code:'ACTION_FAILED',operation:'failed'};}
    finally {this.busy=null;}
  }
}
function installedTarget(endpoint: () => {url:string;token:string}): string | null {
  const app=dirname(dirname(fileURLToPath(import.meta.url)));
  try {
    const target=dirname(app),config=JSON.parse(readFileSync(join(target,'data','config.json'),'utf8'));
    const actual=new URL(endpoint().url);
    if(actual.origin!==`http://127.0.0.1:${config.servicePort}` || !existsSync(join(app,'trial','service-task.mjs')))return null;
    return target;
  } catch{return null;}
}

export function registerPanelBridge(ctx: any, settings: PanelSettings, endpoint: () => { url: string; token: string },
                                    control = new PanelController(installedTarget(endpoint)),
                                    active = new ActiveQueries(), selectedLibrary: (sessionId: string) => string | null = () => null, libraryPanel?: LibraryPanel, build: BuildIdentity | null = RUNNING_BUILD): void {
  let healthFlight: Promise<{ service: string; isolationActive: boolean; serviceQuery: any }> | null = null;
  const health = () => {
    if (healthFlight) return healthFlight;
    healthFlight=(async()=>{
      let service='unreachable',isolationActive=false, serviceQuery=null;
      try {const {url,token}=endpoint(),parsed=new URL(url);
        if(parsed.protocol!=='http:' || parsed.hostname!=='127.0.0.1' || parsed.username || parsed.password || token.length<24) throw Error();
        const response=await fetch(parsed.origin+'/health',{redirect:'error',headers:{Authorization:`Bearer ${token}`},signal:AbortSignal.timeout(2500)});
        if([401,403].includes(response.status)) service='unauthorized';
        else if(response.ok){
          const value=await response.json();
          isolationActive=value.isolation==='required' && value.isolationActive===true;
          service=compatibleHealth(value,build)?(isolationActive?'ready':'not-isolated'):'build-mismatch';
          // The service's own view: whether *any* session's query is running, with closed-set facts only
          // (state, mode, elapsed). Naming the state is the point - a healthy 200 must not read as "a query
          // can be admitted right now" when it cannot.
          serviceQuery=value.query ?? null;
        }
        else service='unhealthy';
      } catch {}
      return {service,isolationActive,serviceQuery};
    })().finally(()=>{healthFlight=null;});return healthFlight;
  };
  const projection = async (sessionId: string) => {
    const value = await health();
    let libraries:any={};
    if(libraryPanel)try{libraries=await libraryPanel.project(sessionId);}catch{libraries={libraryControlUnavailable:true};}
    return {...value,...libraries, selectedLibraryId: selectedLibrary(sessionId), query: active.status(sessionId), queries: active.list(sessionId)};
  };
  if (typeof ctx?.inject !== 'function') return;
  ctx.inject(['webServer', 'connection'], (child: any) => {
    if (typeof child.effect !== 'function') return;
    child.effect(() => child.webServer.register({ kind: 'exact', path: '/knowledge-panel',
      handler: async (req: IncomingMessage, res: ServerResponse) => {
        const rejection = child.connection.requestRejection(req);
        if (rejection !== undefined) { res.statusCode = rejection; res.end(); return; }
        // Settings mutation additionally requires an explicit same-origin browser request.
        if (req.method === 'POST' && req.headers.origin !== `http://${req.headers.host}` && req.headers.origin !== `https://${req.headers.host}`) {
          res.statusCode = 403; res.end(); return;
        }
        const send = (status: number, body: unknown) => { res.statusCode = status; res.setHeader('Content-Type', 'application/json'); res.setHeader('Cache-Control', 'no-store'); res.end(JSON.stringify(body)); };
        if (req.method !== 'GET' && req.method !== 'POST') { send(405, { code: 'METHOD_NOT_ALLOWED' }); return; }
        const sessionId = new URL(req.url!, 'http://localhost').searchParams.get('sessionId');
        if (!sessionValid(sessionId)) { send(400, { code: 'INVALID_SESSION' }); return; }
        if (req.method === 'POST') {
          if (req.headers['content-type']?.split(';')[0] !== 'application/json') { send(415, { code: 'JSON_REQUIRED' }); return; }
          try {
            let body = ''; for await (const chunk of req) { body += chunk; if (Buffer.byteLength(body) > 4096) { send(413, { code: 'BODY_TOO_LARGE' }); return; } }
            const value = JSON.parse(body);
            if(build && (build.valid===false || req.headers['x-knowledge-client-build']!==build.id) && !(value?.cancelQuery===true) && value?.action!=='stop' && value?.action!=='status'){send(409,{code:'BUILD_MISMATCH'});return;}
            if(value && Object.hasOwn(value,'libraryAction')) {
              if(!libraryPanel){send(503,{code:'LIBRARY_CONTROL_UNAVAILABLE'});return;}
              try{send(200,await libraryPanel.act(sessionId,value));}
              catch(error){const code=error instanceof Error && LIBRARY_CODES.has(error.message)?error.message:'LIBRARY_ACTION_FAILED';send(409,{code});}
              return;
            }
            if (value && Object.keys(value).length===1 && ['start','stop','status'].includes(value.action)) {
              const before=await health();
              let result=value.action==='start' && before.service==='ready' ? {code:'ALREADY_READY',operation:'idle'} : value.action==='start' && ['unauthorized','not-isolated','unhealthy','build-mismatch'].includes(before.service) ? {code:'ACTION_FAILED',operation:'failed'} : await control.act(value.action);
              const checked=await health();
              if(value.action==='start' && result.code==='ACTION_COMPLETED' && checked.service!=='ready')result={code:'ACTION_FAILED',operation:'failed'};
              send(200,{...checked,...result,controlAvailable:control.available,checkedAt:new Date().toISOString()});return;
            }
            // Cancel this session's own query. It is a **local** abort of the operation this plugin is
            // running for this session - never a request to stop somebody else's, and never the service
            // stop (which is a separate, confirmed action that affects every session).
            if (value && Object.keys(value).every(key => ['cancelQuery','requestId'].includes(key)) && value.cancelQuery === true && (value.requestId === undefined || /^kq-[a-zA-Z0-9_-]{8,60}$/.test(value.requestId))) {
              const cancelled = active.cancel(sessionId, value.requestId ?? null);
              // A live call has been asked to stop. What remains - records whose call already returned - is
              // addressed through the service directly, because there is no local signal left to abort.
              const retried = await active.recancel(sessionId, value.requestId ?? null);
              const after = active.status(sessionId);
              send(200, { code: cancelled.requested ? 'CANCEL_REQUESTED'
                  : retried.attempted ? (retried.confirmed ? 'CANCEL_CONFIRMED' : 'CANCEL_RETAINED')
                    : 'NO_ACTIVE_QUERY',
                requestedPhases: cancelled.phases, retried: retried.attempted,
                confirmedByService: retried.confirmed, query: after, queries: active.list(sessionId),
                note: cancelled.requested
                  ? 'The running call was asked to stop; the plugin also asks the service to end the job and '
                    + 'reports whether that was confirmed.'
                  : retried.attempted
                    ? (retried.confirmed
                      ? 'The service confirmed the retained job has ended.'
                      : 'The service did not confirm an ending for the retained job; it may still be running.')
                    : 'This session has no query in flight.',
                checkedAt: new Date().toISOString() });
              return;
            }
            if(Object.keys(value).length===1 && Object.hasOwn(value,'autoOpenMode')){settings.setAutoOpen(value.autoOpenMode);send(200,{autoOpenMode:settings.autoOpenMode,persisted:settings.persisted});return;}
            if (Object.keys(value).length !== 1 || !Object.hasOwn(value, 'maxCalls')) { send(400, { code: 'INVALID_SETTING' }); return; }
            settings.set(sessionId, value.maxCalls);
          } catch { send(400, { code: 'SETTING_NOT_SAVED' }); return; }
          send(200, { maxCalls: settings.get(sessionId), persisted: settings.persisted }); return;
        }
        if(build && (build.valid===false || req.headers['x-knowledge-client-build']!==build.id)){send(200,{service:'build-mismatch',code:'BUILD_MISMATCH',controlAvailable:control.available,query:active.status(sessionId),queries:active.list(sessionId),maxCalls:settings.get(sessionId),autoOpenMode:'manual'});return;}
        send(200,{...await projection(sessionId),operation:control.state,controlAvailable:control.available,checkedAt:new Date().toISOString(),autoOpenMode:settings.autoOpenMode,maxCalls:settings.get(sessionId),persisted:settings.persisted,settingsReadable:settings.readable,commands:installedCommands()});
      },
    }));
  });
}

