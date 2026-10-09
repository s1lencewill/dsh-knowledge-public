/**
 * The session-scoped library selection: the user control plane that bounds what the model can search.
 *
 * The host injects the calling session (never a tool argument), and only a human command
 * (`/knowledge-use`) or the panel may change the selection. That is the point: the model can ask for
 * *content*, but it cannot decide *which library is in scope* for the conversation, and it cannot widen
 * the scope by passing a different `libraryId` - a call that contradicts the selection is refused
 * outright rather than quietly honoured.
 *
 * Scope and lifetime rules this type deliberately keeps:
 *
 * - one entry per session key, so two conversations never share a scope;
 * - no cross-library concurrent search: the selection is one library, not a set;
 * - changing the selection cannot affect a call that already validated, because validation happens once
 *   per dispatch and against the value current at that moment ("the switch affects later calls only");
 * - bounded size, oldest-first eviction, so a long-lived process cannot grow without limit.
 */
import { readFileSync, writeFileSync, renameSync, existsSync, rmSync } from 'node:fs';
import { resolve } from 'node:path';
import { KnowledgeFailure, failureRecord } from './failures.js';

/** Sessions tracked at once. A blunt bound, not a budget: the state is one short string per session. */
export const MAX_SELECTED_SESSIONS = 500;

export class LibrarySelection {
  private readonly chosen = new Map<string, string>();

  private pins = new Map<string, string | null>();
  private defaults = new Map<string, {id: string; since: number}>();
  readable = true;
  private durable: any = null;
  get persisted() {return Boolean(this.file);}
  constructor(private readonly max = MAX_SELECTED_SESSIONS, private file?: string) {
    try { if(file && existsSync(file)) {
      const value = JSON.parse(readFileSync(file,'utf8'));
      if(!value || value.version!==1 || !Array.isArray(value.sessions) || !Array.isArray(value.defaults))throw Error();
      for(const [key,id] of value.sessions.slice(-max)) {
        if(typeof key!=='string' || (id!==null && typeof id!=='string'))throw Error();
        this.pins.set(key,id);if(id!==null)this.chosen.set(key,id);
      }
      for(const [key,record] of value.defaults.slice(-max)) {
        if(typeof key!=='string' || typeof record.id!=='string' || !Number.isFinite(record.since))throw Error();
        this.defaults.set(key,record);
      }
    }} catch {this.readable=false;this.pins.clear();this.chosen.clear();this.defaults.clear();}
    this.durable=this.capture();
  }
  private workspaceKey(workspace: string) {const key=resolve(workspace);return process.platform==='win32'?key.toLowerCase():key;}
  defaultFor(workspace: string): string | null {return this.defaults.get(this.workspaceKey(workspace))?.id ?? null;}
  setDefault(workspace: string, id: string | null): void {
    if(!this.readable)throw Error('SELECTION_FILE_UNREADABLE');
    const key=this.workspaceKey(workspace);
    this.defaults.delete(key);if(id!==null)this.defaults.set(key,{id,since:Date.now()});
    while(this.defaults.size>this.max)this.defaults.delete(this.defaults.keys().next().value!);
    this.save();
  }
  observe(session: string, workspace: string, createdAt?: number): void {
    if(!this.readable)throw Error('SELECTION_FILE_UNREADABLE');
    if(this.pins.has(session))return;
    const value=this.defaults.get(this.workspaceKey(workspace));
    // An unobserved restored session never acquires a default installed after its creation.
    const id=value && typeof createdAt==='number' && createdAt>=value.since?value.id:null;
    this.pins.set(session,id);if(id!==null)this.chosen.set(session,id);
    this.trim();this.save();
  }
  private trim() {
    while(this.pins.size>this.max){const key=this.pins.keys().next().value!;this.pins.delete(key);this.chosen.delete(key);}
  }
  private capture() {return {chosen:[...this.chosen],pins:[...this.pins],defaults:[...this.defaults]};}
  private save() {
    if(!this.readable)throw Error('SELECTION_FILE_UNREADABLE');
    if(!this.file){this.durable=this.capture();return;}
    const temp=this.file+'.tmp';
    try {
      writeFileSync(temp,JSON.stringify({version:1,sessions:[...this.pins],defaults:[...this.defaults]}),'utf8');renameSync(temp,this.file);
      this.durable=this.capture();
    } catch {
      this.chosen.clear();this.pins.clear();this.defaults.clear();
      for(const [k,v] of this.durable.chosen)this.chosen.set(k,v);
      for(const [k,v] of this.durable.pins)this.pins.set(k,v);
      for(const [k,v] of this.durable.defaults)this.defaults.set(k,v);
      try{rmSync(temp,{force:true});}catch{}
      throw Error('SELECTION_NOT_SAVED');
    }
  }

  /** The library this session is scoped to, or undefined when the user has not chosen one. */
  get(session: string): string | undefined {
    return this.chosen.get(session);
  }

  /** Scope `session` to `libraryId` for its later calls. Returns the previous value, if any. */
  use(session: string, libraryId: string): string | undefined {
    if(!this.readable)throw Error("SELECTION_FILE_UNREADABLE");
    const previous = this.chosen.get(session);
    // Delete before set so re-selecting the same library refreshes its recency rather than leaving the
    // insertion order stale.
    this.chosen.delete(session);
    this.chosen.set(session, libraryId);
    this.pins.delete(session);this.pins.set(session,libraryId);this.trim();this.save();
    while (this.chosen.size > this.max) {
      const oldest = this.chosen.keys().next();
      if (oldest.done) break;
      this.chosen.delete(oldest.value);
    }
    return previous;
  }

  /** Drop this session's scope ("not this time"). Returns the value that was in effect. */
  clear(session: string): string | undefined {
    if(!this.readable)throw Error("SELECTION_FILE_UNREADABLE");
    const previous = this.chosen.get(session);
    this.chosen.delete(session);
    this.pins.delete(session);this.pins.set(session,null);this.trim();this.save();
    return previous;
  }

  /** Tracked session count. Exposed for tests and diagnostics, never for a model-visible decision. */
  get size(): number {
    return this.chosen.size;
  }

  /**
   * The one place a tool call is checked against the scope.
   *
   * A session with no selection stays permissive (M0 behaviour: the caller names the library). A session
   * with a selection refuses any other library, including a superset: silently narrowing a request to
   * something the caller did not ask for would answer a question nobody asked.
   */
  assertAllowed(session: string, libraryId: string): void {
    const selected = this.chosen.get(session);
    if (selected === undefined || selected === libraryId) return;
    // A structured refusal carrying no text at all: a library id is free text, so it does not go into the
    // detail. The model-facing sentence still names both libraries, because it is built here from the user's
    // own configuration rather than from anything caught - that is a different route from the card's fields.
    const detail = { layer: 'plugin' as const };
    const record = failureRecord('KNOWLEDGE_LIBRARY_NOT_SELECTED', { detail });
    throw new KnowledgeFailure('KNOWLEDGE_LIBRARY_NOT_SELECTED', {
      detail,
      message: `${record.code}: this session is scoped to library "${selected}", not "${libraryId}". `
        + 'The scope is the user\'s choice: change it with /knowledge-use <libraryId> (or /knowledge-use none) '
        + 'and then retry. Do not try to reach another library by another route.',
    });
  }
}
