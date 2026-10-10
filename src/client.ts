import { RUNNING_BUILD, requireCompatibleService, type BuildIdentity } from './build.js';
import { AsyncLocalStorage } from 'node:async_hooks';
const httpDispatch = new AsyncLocalStorage<() => void>();
export const withHttpDispatch = <T>(observe: () => void, body: () => T): T => httpDispatch.run(observe, body);
import type { CataloguePage, CatalogueQuery, DocumentFindResult, DocumentQuery, Library, QueryResult, ReadRequest, ReadWindow } from './contracts.js';
import { KnowledgeFailure, KnowledgeServiceError, readRefusalOf, serviceCodeOf, type ServiceCode } from './failures.js';

/** The client's own id for one submission: `kq-` plus 16 lowercase hex characters. */
export const REQUEST_ID_PATTERN = /^kq-[0-9a-z]{8,32}$/;

let requestCounter = 0;

/**
 * Mint the id a query is submitted under, **before** the request goes out.
 *
 * The whole point is that the id exists while the submission may still be lost: the service admits a job
 * under this id, and the same id can then be polled or cancelled even when the client never saw the
 * response. It is generated here rather than accepted from the model, because a caller-chosen id would be a
 * caller-chosen handle on someone else's job.
 */
export function newRequestId(random: () => number = Math.random): string {
  requestCounter += 1;
  const suffix = Math.floor(random() * 0xffffffff).toString(16).padStart(8, '0').slice(0, 8);
  const extra = Math.floor(random() * 0xffffffff).toString(16).padStart(8, '0').slice(0, 8);
  return `kq-${suffix}${extra}`;
}

/** The owner scope a job belongs to, so only its own caller can poll or cancel it. */
export interface QueryOwner {
  workspace: string;
  libraryId: string;
  session?: string;
}

/**
 * Where one submission's progress is reported while it runs.
 *
 * It carries a phase and, when the service named one, the job's own state - both closed-set tokens. It is
 * what lets the panel show "polling" rather than "the service is healthy", and it deliberately has no path
 * for a question, a library or a result to travel: the plugin's live view of a query is a phase, a mode and
 * a clock, not a second copy of the material.
 */
export interface QueryObserver {
  phase(phase: QueryPhase, facts: { id: string | null; requestId: string; jobState: string | null;
    cancellationConfirmed: boolean | null }): void;
  /**
   * The **last** word about this submission, after cleanup has run.
   *
   * It is separate from the phase above because a live view and a settled record answer different questions.
   * A panel wants to keep showing an entry whose cleanup was *not* confirmed (the job may still be running and
   * the user still needs a way to ask about it), while an entry that ended in a confirmed terminal state is
   * finished. Nothing here is a claim about the service's table - only about what this call observed.
   */
  settled?(facts: { requestId: string; phase: QueryPhase; jobState: string | null;
    cancellationConfirmed: boolean | null; jobId: string | null }): void;
}

/**
 * What the client knows about its own submission, at every point of its life.
 *
 * A closed set, and deliberately finer than "sent / returned": the failure this round repairs is a client
 * that could only say "the request timed out" about a job the service was still running. `unknown` is a
 * real answer here - it is what a lost admission response produces - and it is reported as such rather than
 * rounded to either "sent" or "not sent".
 */
export type QueryPhase = 'submitting' | 'accepted' | 'polling' | 'cancelling'
  | 'completed' | 'failed' | 'cancelled' | 'unknown';

export interface QueryLifecycle {
  requestId: string;
  phase: QueryPhase;
  /** What the service said the job was doing, when it said anything. */
  jobState: string | null;
  /** Whether this client's cancellation/acknowledgement was confirmed by the service. */
  cancellationConfirmed: boolean | null;
  /** The refusal code the admission itself produced, when it was refused rather than timed out. */
  admissionCode: ServiceCode | null;
}

/** How long each part of a call may take. Every field has a default; a caller overrides only what it means. */
export interface ClientTiming {
  /** One HTTP request's own bound: admission and polling. */
  requestMs: number;
  /**
   * The bound on the **cleanup** request, which is separate on purpose.
   *
   * A caller that stops a call (or whose deadline expires) still has a job to stop, and that request must not
   * inherit the aborted caller signal. It therefore gets its own short deadline; a caller that omits it gets
   * the default rather than an unbounded - or immediate - one.
   */
  cancelMs: number;
  fastMs: number;
  deepMs: number;
}

/** Host-only adapter. Never expose the token to browser code or tool arguments. */
export class KnowledgeClient {
  readonly url: string;
  private timing: ClientTiming;
  constructor(url: string, private token: string,
    private reportWarning: (message: string) => void = message => console.warn(message),
    // Each field is defaulted by name rather than by replacing the whole record: a caller that supplies only
    // `{requestMs, fastMs, deepMs}` would otherwise leave `cancelMs` undefined, and `AbortSignal.timeout` has
    // no default of its own. That is not a hypothetical - it silently made cleanup abort before it could be
    // sent, which is exactly the "the job ran on with nobody able to stop it" shape this round repairs.
    timing: Partial<ClientTiming> = {}, private build: BuildIdentity | null = RUNNING_BUILD) {
    this.timing = { requestMs: timing.requestMs ?? 10_000, cancelMs: timing.cancelMs ?? 5_000,
      fastMs: timing.fastMs ?? 120_000, deepMs: timing.deepMs ?? 270_000 };
    const parsed = new URL(url);
    if (parsed.protocol !== 'http:' || parsed.hostname !== '127.0.0.1' || parsed.username || parsed.password) {
      throw new KnowledgeFailure('KNOWLEDGE_NOT_CONFIGURED', { detail: { layer: 'plugin' } });
    }
    this.url = parsed.origin;
    if (token.length < 24) throw new KnowledgeFailure('KNOWLEDGE_NOT_CONFIGURED', { detail: { layer: 'plugin' } });
  }
  private async request(path: string, options: RequestInit = {}) {
    const business = path.startsWith('/libraries') || ['/reads','/documents','/catalogue'].includes(path) || (path === '/queries' && options.method === 'POST');
    if (business) await requireCompatibleService(this.url,this.token,this.build,options.signal ?? undefined);
    httpDispatch.getStore()?.();
    let signal: AbortSignal;
    if (options.signal) {
      // `AbortSignal.any` keeps the caller's own signal in charge: an external stop still aborts the
      // request, and a cleanup that must not be defeated by an already-aborted caller signal does not
      // combine them (see `cancelRequest`).
      signal = AbortSignal.any([options.signal, AbortSignal.timeout(this.timing.requestMs)]);
    } else {
      signal = AbortSignal.timeout(this.timing.requestMs);
    }
    const response = await fetch(this.url + path, {
      ...options, redirect: 'error',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${this.token}`, ...(this.build ? {'X-Knowledge-Build':this.build.id} : {}) },
      signal,
    });
    if (!response.ok) {
      // Read the body **once**, take only the leading code and the declared structured fields, and drop the
      // body.
      //
      // This is the whole of the prose handling: the response text is not stored on the error, not attached
      // as a cause, and not logged. A refused request therefore cannot leak the service's message - which can
      // quote a relative path, a file handle or a request field - because no object survives the throw with it.
      const refusal = await this.refusal(response);
      throw new KnowledgeServiceError(response.status, serviceCodeOf(refusal.detail), readRefusalOf(refusal.structured));
    }
    return response.json();
  }
  /**
   * The service's refusal body, reduced at once to the two things this build declared.
   *
   * `detail` keeps its historical meaning - the string `"CODE: sentence"` an older service also sends - and
   * `structured` is the newer `error` object, handed on **unvalidated** so `readRefusalOf` can decide what
   * survives. Neither is retained: the parsed body goes out of scope with this call, and only the code and the
   * validated facts travel on the error. A structured code the older path would have missed is preferred here,
   * which is what lets a 429 that names QUERY_BUSY be classified by its code rather than by its status.
   */
  private async refusal(response: Response): Promise<{ detail: string; structured: unknown }> {
    try {
      const body = await response.json();
      const detail = typeof body?.detail === 'string' ? body.detail : '';
      const structured = body?.error;
      const named = typeof (structured as { code?: unknown } | null)?.code === 'string'
        ? String((structured as { code: string }).code) : '';
      return { detail: named ? named + (detail.startsWith(named) ? detail.slice(named.length) : ': ' + detail) : detail, structured };
    } catch { return { detail: '', structured: null }; }
  }
  async libraries(workspace: string): Promise<Library[]> {
    return this.request('/libraries?workspace=' + encodeURIComponent(workspace));
  }
  /** Locate a library document by title, author, DOI or file name. Local only: the service ranks its
   * document catalogue and re-verifies the mapped file, so a hit carries a `fileHandle` for
   * `knowledge_read`. A hit locates a file; it is not evidence for a content question. */
  async findDocuments(workspace: string, libraryId: string, query: DocumentQuery, signal?: AbortSignal): Promise<DocumentFindResult> {
    signal?.throwIfAborted();
    return this.request('/documents', {
      method: 'POST', signal, body: JSON.stringify({ workspace, libraryId, ...query }),
    });
  }
  /**
   * One page of a library's document catalogue. Like `findDocuments` this is a librarian call: it never starts
   * the engine and never builds a model client. It differs in *what it answers* - "which documents exist" rather
   * than "which document matches this clue" - which is why it is a separate route and not a keyword search: a
   * keyword search returns what matches, and a whole-library question needs what exists.
   */
  async catalogue(workspace: string, libraryId: string, query: CatalogueQuery, signal?: AbortSignal): Promise<CataloguePage> {
    signal?.throwIfAborted();
    return this.request('/catalogue', {
      method: 'POST', signal, body: JSON.stringify({ workspace, libraryId, ...query }),
    });
  }
  /** One bounded read of the original text behind a citation. `session` groups reads under the
   * service's per-session budget; it never grants access. The server re-checks authorization and the
   * file version on every call and refuses with a code (`SOURCE_VERSION_CHANGED` means search again). */
  async read(workspace: string, session: string, request: ReadRequest, signal?: AbortSignal): Promise<ReadWindow> {
    signal?.throwIfAborted();
    return this.request('/reads', {
      method: 'POST', signal, body: JSON.stringify({ workspace, session, ...request }),
    });
  }
  /**
   * One query, from a submission that can be tracked to a cancellation that is confirmed.
   *
   * The state machine this replaces had one hole and it was the whole defect: `POST /queries` sat outside
   * the `try/finally`, so a request that timed out at its own 10s bound - exactly what a slow snapshot
   * causes - neither learned the job's id nor cancelled it. The job then ran on, spending provider calls,
   * with nothing on the client side able to reach it.
   *
   * The shape now:
   *
   * 1. the request id is minted **before** the submission, so the job is reachable by an id the client
   *    already holds even if the response is lost;
   * 2. the submission itself carries the caller's signal *and* the job deadline, so a caller that stops
   *    during the POST stops it, and a slow admission surfaces as the caller's own abort rather than as an
   *    internal timeout;
   * 3. a lost admission is reconciled by request id **and the recovered id becomes the id this call
   *    controls**, so the recovery ends in a real cancellation rather than in a reported id;
   * 4. recovery retries inside one bounded window, because a single `known: false` is a snapshot of the
   *    service's table, not proof that the job does not exist;
   * 5. cleanup has its own bounded deadline and its own signal, so an already-aborted caller signal cannot
   *    defeat the one request that stops the job - and when it cannot be sent at all, the client registers
   *    its **cancel intent** by request id, so a job admitted later is not left running;
   * 6. the failure facts are frozen **after** cleanup, so the phase, the job state and the cancellation
   *    result the caller reads are the last ones observed rather than the ones from before the cleanup.
   */
  async search(workspace: string, libraryId: string, query: string, mode: 'FAST' | 'DEEP', signal?: AbortSignal,
               session?: string, observer?: QueryObserver): Promise<QueryResult> {
    signal?.throwIfAborted();
    const deadline = AbortSignal.timeout(mode === 'FAST' ? this.timing.fastMs : this.timing.deepMs);
    const operation = signal ? AbortSignal.any([signal, deadline]) : deadline;
    const requestId = newRequestId();
    const owner: QueryOwner = { workspace, libraryId, session };
    const lifecycle: QueryLifecycle = {
      requestId, phase: 'submitting', jobState: null, cancellationConfirmed: null, admissionCode: null,
    };
    let identity: string | null = null;
    let failed: unknown = null;
    let produced: QueryResult | null = null;
    // One funnel for every phase change, so the observer and the returned lifecycle can never disagree about
    // what happened: there is one place that moves the phase, and both read it from there. It always reads
    // the *current* job id, because the reconciliation below is exactly what gives this call an id late.
    const note = (phase: QueryPhase, overrides: { jobState?: string | null; cancelled?: boolean | null } = {}) => {
      lifecycle.phase = phase;
      // The state is only changed when this call has something to say about it, so a service that answered
      // with a state keeps it: a phase change is not a reason to forget a fact that was already observed.
      if ('jobState' in overrides) lifecycle.jobState = overrides.jobState ?? null;
      if ('cancelled' in overrides) lifecycle.cancellationConfirmed = overrides.cancelled ?? null;
      try {
        observer?.phase(phase, { id: identity, requestId, jobState: lifecycle.jobState,
          cancellationConfirmed: lifecycle.cancellationConfirmed });
      } catch { /* display only */ }
    };
    try {
      note('submitting');
      const admitted = await this.request('/queries', {
        method: 'POST', signal: operation,
        body: JSON.stringify({ workspace, libraryId, query, mode, requestId, session }),
      });
      identity = typeof admitted?.id === 'string' ? admitted.id : null;
      const admittedState = typeof admitted?.state === 'string' ? admitted.state : null;
      if (identity === null) {
        // A 200 whose body carries no id is not an admission: report it as a failure rather than polling a
        // route with the string "null" in it.
        note('unknown', { jobState: admittedState });
        throw new KnowledgeServiceError(200, null);
      }
      note('accepted', { jobState: admittedState });
      for (;;) {
        operation.throwIfAborted();
        note('polling');
        const job = await this.request(`/queries/${encodeURIComponent(identity)}?${ownerQuery(owner)}`,
          { signal: operation });
        const state = typeof job?.state === 'string' ? job.state : lifecycle.jobState;
        if (job?.resultReleased === true) throw new KnowledgeServiceError(410, 'QUERY_RESULT_RELEASED');
        if (job?.result && job?.terminal !== false) {
          note(job.result.status === 'failed' ? 'failed'
            : job.result.status === 'cancelled' ? 'cancelled' : 'completed', { jobState: state });
          produced = job.result;
          break;
        }
        note('polling', { jobState: state });
        await pollPause(operation, 100);
      }
    } catch (error) {
      // The submission itself may have reached the service and been admitted: a request bound that expires
      // is not evidence that nothing happened. Ask the service what it knows about this id - more than once,
      // because the job may be admitted moments after the response was lost - and adopt the id it names, so
      // the cleanup below cancels the real job instead of reporting an orphan.
      failed = error;
      if (identity === null) {
        note('unknown');
        const reconciled = await this.recover(requestId, owner);
        if (reconciled.id) {
          identity = reconciled.id;
          note('unknown', { jobState: reconciled.state ?? 'unknown' });
        }
      }
    }
    // Cleanup, after the outcome is known and before the caller is told anything. It is a separate, bounded
    // operation: the caller's signal is already aborted along defeat this path, which is why it does not
    // reuse `operation`.
    await this.settle(identity, requestId, owner, mode, lifecycle, note);
    try {
      observer?.settled?.({ requestId, phase: lifecycle.phase, jobState: lifecycle.jobState,
        cancellationConfirmed: lifecycle.cancellationConfirmed, jobId: identity });
    } catch { /* display only */ }
    if (failed !== null) {
      // Freeze the facts the caller will read *now*, so they describe the state this call ended in.
      this.attachFailureFacts(failed, lifecycle);
      throw failed;
    }
    if (produced !== null) {
      this.attachLifecycle(produced, lifecycle);
      return produced;
    }
    // Unreachable by construction: the loop above either produces a result or throws, and `failed` catches the
    // throw. It exists so the compiler sees that this path returns nothing rather than assuming a value.
    throw new KnowledgeServiceError(200, null);
  }

  /**
   * Clean up one submission: confirm a terminal result, or stop the job, or - when nothing can be reached -
   * register the intent to stop it.
   *
   * Three outcomes, and none of them is reported as another:
   *
   * * the job was still in flight and the service confirmed it ended -> `cancellationConfirmed: true`;
   * * the job had already produced a result (or the service left it running) -> the result is *not*
   *   discarded, and the client reports what it actually observed;
   * * the service could not be reached at all -> `cancellationConfirmed: false`, and the client sends a
   *   best-effort *cancel intent* under the same request id so a job admitted after this call returns is
   *   still stopped rather than left with nobody holding its id.
   */
  private async settle(identity: string | null, requestId: string, owner: QueryOwner, mode: 'FAST' | 'DEEP',
                       lifecycle: QueryLifecycle,
                       note: (phase: QueryPhase, overrides?: { jobState?: string | null; cancelled?: boolean | null }) => void): Promise<void> {
    const wasTerminal = lifecycle.phase === 'completed' || lifecycle.phase === 'failed'
      || lifecycle.phase === 'cancelled';
    note(wasTerminal ? lifecycle.phase : 'cancelling');
    const acknowledged = await this.acknowledge(identity, requestId, owner, {
      // A completed result is confirmed, never cancelled: the job has already produced the material this
      // call is about, and stopping it now would be a different action entirely.
      cancelIntent: !wasTerminal,
      deadlineMs: mode === 'FAST' ? this.timing.fastMs : this.timing.deepMs,
    });
    if (wasTerminal) {
      // Keep the terminal phase: an acknowledgement that failed does not make a completed call incomplete.
      note(lifecycle.phase, { jobState: acknowledged.state ?? lifecycle.jobState, cancelled: acknowledged.confirmed && acknowledged.state === 'cancelled' });
    } else {
      const terminal = acknowledged.confirmed && ['cancelled', 'completed', 'failed'].includes(acknowledged.state ?? '');
      note(terminal ? acknowledged.state as QueryLifecycle['phase'] : 'cancelling',
        { jobState: acknowledged.state ?? lifecycle.jobState, cancelled: acknowledged.confirmed && acknowledged.state === 'cancelled' });
    }
    if (!acknowledged.confirmed) {
      // Bounded and visible: a cleanup that could not be confirmed is reported as such, and never as "the job
      // stopped". The result, when there is one, is not touched by this.
      try {
        this.reportWarning('KNOWLEDGE_CLEANUP_FAILED: cancellation/acknowledgement was not confirmed');
      } catch {}
    }
  }

  /**
   * What the service knows about one request id, asked more than once inside **one** bounded window.
   *
   * The window is the correction of a real defect: a client that asked once, received `known: false` and gave
   * up left a job running, because the service had not finished admitting it yet. The window is also a *total*,
   * not a per-attempt bound: three attempts of three seconds each is a nine-second wait for a user who pressed
   * stop, and a cleanup that then has no time left is a cleanup that reports "not confirmed" for a job it could
   * have stopped.
   */
  private async recover(requestId: string, owner: QueryOwner): Promise<{ id: string | null; state: string | null }> {
    const started = Date.now();
    const total = Math.max(this.timing.cancelMs, RECOVERY_ATTEMPTS * Math.min(this.timing.cancelMs, 800));
    let last: { id: string | null; state: string | null } = { id: null, state: null };
    for (let attempt = 0; attempt < RECOVERY_ATTEMPTS; attempt += 1) {
      const left = total - (Date.now() - started);
      if (left <= 0) break;
      last = await this.reconcile(requestId, owner, Math.max(150, Math.min(this.timing.cancelMs, left)));
      if (last.id) return last;
      if (attempt + 1 < RECOVERY_ATTEMPTS) await pause(Math.min(100, Math.max(0, left - 50)));
    }
    return last;
  }

  /**
   * Confirm this submission's outcome, or stop it, under **one** bounded deadline of its own.
   *
   * The deadline is the total for the whole acknowledgement, not per attempt, so a service that answers slowly
   * cannot extend it. `cancelIntent` makes the request id part of the body: if the service has no such job it
   * registers the intent, so a job admitted later is cancelled instead of orphaned.
   */
  private async acknowledge(identity: string | null, requestId: string, owner: QueryOwner,
                            options: { cancelIntent: boolean; deadlineMs: number }):
  Promise<{ confirmed: boolean; state: string | null; acknowledged: boolean }> {
    const started = Date.now();
    const total = Math.max(this.timing.cancelMs, Math.min(this.timing.cancelMs * 2, options.deadlineMs));
    let last: { confirmed: boolean; state: string | null; acknowledged: boolean; retryable: boolean } =
      { confirmed: false, state: null, acknowledged: false, retryable: false };
    for (let attempt = 0; attempt < RECOVERY_ATTEMPTS; attempt += 1) {
      const left = total - (Date.now() - started);
      if (left <= 0) break;
      last = await this.acknowledgeOnce(identity, requestId, owner, options.cancelIntent,
        Math.max(150, Math.min(this.timing.cancelMs, left)));
      if (last.confirmed || last.acknowledged) return last;
      // A job that the service says it does not have is only *not there yet* once; asking again is what makes
      // the recovery bounded instead of hopeful. A job the service refused to acknowledge is not retried: the
      // refusal is an answer.
      if (!last.retryable) break;
      if (attempt + 1 < RECOVERY_ATTEMPTS) await pause(Math.min(100, Math.max(0, left - 50)));
    }
    return last;
  }

  private async acknowledgeOnce(identity: string | null, requestId: string, owner: QueryOwner,
                                cancelIntent: boolean, timeoutMs: number):
  Promise<{ confirmed: boolean; state: string | null; acknowledged: boolean; retryable: boolean }> {
    const path = identity === null ? '/queries/acknowledge' : '/queries/' + encodeURIComponent(identity) + '/acknowledge';
    try {
      const response = await fetch(this.url + path, {
        method: 'POST', redirect: 'error',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${this.token}`, ...(this.build ? {'X-Knowledge-Build':this.build.id} : {}) },
        body: JSON.stringify({ ...owner, requestId, cancelIntent }),
        signal: AbortSignal.timeout(timeoutMs),
      });
      if (!response.ok) {
        // 404 means "no such job yet" and is the one answer worth asking again; anything else is a decision.
        return { confirmed: false, state: null, acknowledged: false, retryable: response.status === 404 };
      }
      const body = await response.json();
      return {
        confirmed: body?.cancellationConfirmed === true || body?.released === true,
        state: typeof body?.state === 'string' ? body.state : null,
        acknowledged: body?.acknowledged === true,
        retryable: body?.known === false,
      };
    } catch {
      // A transport failure (or our own bound expiring) is not retried inside the same deadline: the caller's
      // cleanup time is bounded, and the intent below is what covers a job that appears later.
      return { confirmed: false, state: null, acknowledged: false, retryable: false };
    }
  }

  /** The lifecycle facts of the submission that produced a result, attached so the caller can report them. */
  private attachLifecycle(result: QueryResult, lifecycle: QueryLifecycle): void {
    if (result && typeof result === 'object') {
      (result as QueryResult & { lifecycle?: QueryLifecycle }).lifecycle = { ...lifecycle };
    }
  }

  /**
   * Attach what this client observed about its own submission to the error it is about to throw.
   *
   * Only closed-set values and the client's own id: a phase, a request id, a state token the service sent,
   * a boolean. The failure taxonomy validates every one of them again before anything is displayed, so this
   * is a widening of what can be *said* about a timeout, not of what can be expressed.
   */
  private attachFailureFacts(error: unknown, lifecycle: QueryLifecycle): void {
    if (error instanceof KnowledgeServiceError) {
      (error as KnowledgeServiceError & { lifecycle?: QueryLifecycle }).lifecycle = { ...lifecycle };
    } else if (error && typeof error === 'object') {
      (error as { knowledgeLifecycle?: QueryLifecycle }).knowledgeLifecycle = { ...lifecycle };
    }
  }

  /**
   * Stop a job this plugin already knows about, with no live call to abort.
   *
   * A retained record's whole purpose: the call returned without a confirmed ending, and the panel still needs
   * a way to act on the job. It is the same acknowledge route the call itself uses - by id when one was
   * learned, by request id (which registers a cancel intent) when none was - so the service sees one protocol,
   * not two. Never throws: its answer is data.
   */
  async cancelByIdentity(identity: string | null, owner: QueryOwner, requestId: string | null = null):
  Promise<{ confirmed: boolean; state: string | null }> {
    const result = await this.acknowledgeOnce(identity, requestId ?? '', owner, true, this.timing.cancelMs);
    return { confirmed: result.confirmed, state: result.state };
  }

  /** What the service knows about one request id, if it knows anything. Never throws. */
  private async reconcile(requestId: string, owner: QueryOwner, timeoutMs: number): Promise<{ id: string | null; state: string | null }> {
    try {
      const response = await fetch(`${this.url}/queries?requestId=${encodeURIComponent(requestId)}&${ownerQuery(owner)}`, {
        redirect: 'error', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${this.token}`, ...(this.build ? {'X-Knowledge-Build':this.build.id} : {}) },
        signal: AbortSignal.timeout(timeoutMs),
      });
      if (!response.ok) return { id: null, state: null };
      const body = await response.json();
      return { id: typeof body?.id === 'string' ? body.id : null,
        state: typeof body?.state === 'string' ? body.state : null };
    } catch { return { id: null, state: null }; }
  }
}

/** The owner scope as query parameters, so a GET can be scoped without a body. */
function ownerQuery(owner: QueryOwner): string {
  const parameters = new URLSearchParams({ ownerWorkspace: owner.workspace, ownerLibrary: owner.libraryId });
  if (owner.session) parameters.set('ownerSession', owner.session);
  return parameters.toString();
}

/**
 * How many times one recovery step asks before it stops, and why a small number.
 *
 * One question is not a proof of absence: the service admits the job inside the same request that lost its
 * response, so the id can appear moments later. Three asks inside one bounded window turn "not there yet" into
 * "found", and the window itself stays small enough that a caller's stop button is not a long wait.
 */
const RECOVERY_ATTEMPTS = 3;

/** An uncancellable, bounded pause between recovery attempts. */
function pause(ms: number): Promise<void> {
  return new Promise(resolve => { setTimeout(resolve, ms); });
}

/** A cancellable pause: the polling loop has to stop the moment the operation is aborted. */
function pollPause(operation: AbortSignal, ms: number): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const done = () => { operation.removeEventListener('abort', abort); resolve(); };
    const timer = setTimeout(done, ms);
    const abort = () => { clearTimeout(timer); operation.removeEventListener('abort', abort); reject(operation.reason); };
    operation.addEventListener('abort', abort, { once: true });
    if (operation.aborted) abort();
  });
}
