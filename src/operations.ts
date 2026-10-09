/**
 * The plugin's own view of the queries this session has in flight - and of the ones whose ending it could not
 * confirm.
 *
 * Two reasons this exists next to the service's `/health`:
 *
 * 1. "the service is up" and "a query can be admitted right now" are different facts, and a panel that shows
 *    only the first reads as "search will work" while every submission is refused;
 * 2. the plugin's `AbortSignal` is the one handle that stops the local wait *and* triggers its bounded
 *    cleanup, so a controlled cancel from the panel cancels the real operation rather than starting a second
 *    one to fight it.
 *
 * The second point is why an entry is **not** simply deleted when the tool call returns. A tool call that
 * ends is not a job that ended: when the cleanup could not be confirmed - an unknown outcome, a stop the
 * service did not acknowledge - the session keeps a record of it, so the panel can still say what is
 * unresolved and still offer to stop it. Only a confirmed terminal state retires an entry.
 *
 * The store is in memory and per session: phases, modes, ids and clocks, never a question, a library or a
 * result. It is bounded by session count and by per-session entry count.
 */
export interface ActiveQuery {
  /** The service's job id, once admission was confirmed. */
  id: string | null;
  /** The state the **service** last reported for that job, or null while it has reported none. */
  jobState: string | null;
  /** The id the submission was made under, minted before it was sent. */
  requestId: string | null;
  mode: 'FAST' | 'DEEP';
  phase: string;
  /** Whether this client's cancel/cleanup was confirmed by the service. */
  cancellationConfirmed: boolean | null;
  startedAt: number;
  settledAt: number | null;
  /** Stop the **local** call this entry belongs to. A no-op once that call has returned. */
  cancel: () => void;
  /**
   * Ask the service to settle the job again, directly - the only thing left when the local call is gone.
   *
   * A retained record exists precisely because a cleanup did not confirm an ending. The panel's button has to
   * be able to try again, and the only way to do that without a live call is to address the job by its own id
   * (or by the request id when no id was ever learned). Returns what the service said.
   */
  recancel: (identity: string | null, requestId: string | null) =>
    Promise<{ confirmed: boolean; state: string | null }>;
}

/** The live or retained view of one session's query, as the panel renders it. */
export interface QueryStatus {
  phase: string;
  mode: 'FAST' | 'DEEP';
  elapsedSeconds: number;
  /** The state the service reported, a member of `JOB_STATES`, or null when it reported none. */
  jobState: string | null;
  requestId: string | null;
  cancellationConfirmed: boolean | null;
  /** Whether this entry is still live (a call the plugin is running) or a retained record. */
  live: boolean;
  cancellable: boolean;
}

const MAX_SESSIONS = 200;
const MAX_PER_SESSION = 4;
/** A retained record is bounded in time too: after this it is history, not something to act on. */
const RETAINED_SECONDS = 600;

export class ActiveQueries {
  private sessions = new Map<string, ActiveQuery[]>();

  begin(session: string, mode: 'FAST' | 'DEEP', cancel: () => void, requestId: string | null = null,
        recancel?: ActiveQuery['recancel']): ActiveQuery {
    const query: ActiveQuery = {
      id: null, jobState: null, requestId, mode, phase: 'submitting', cancellationConfirmed: null,
      startedAt: Date.now(), settledAt: null, cancel,
      recancel: recancel ?? (async () => ({ confirmed: false, state: null })),
    };
    const entries = this.current(session);
    // A session may hold several entries at once - a concurrent pair, or a live call beside a retained record
    // - so the newest is never allowed to overwrite the only handle to an older one. The bound is per session
    // and drops the oldest *retained* entry first.
    if (entries.length >= MAX_PER_SESSION) {
      const retained = entries.findIndex(entry => entry.settledAt !== null && this.isSettled(entry));
      if (retained === -1) throw new Error("QUERY_LOCAL_CAPACITY");
      entries.splice(retained, 1);
    }
    entries.push(query);
    if (!this.sessions.has(session) && this.sessions.size >= MAX_SESSIONS) {
      for (const [key, values] of this.sessions) {
        if (key !== session && values.every(entry => entry.settledAt !== null)) {
          this.sessions.delete(key);
          break;
        }
      }
    }
    this.sessions.set(session, entries);
    return query;
  }

  update(session: string, query: ActiveQuery,
         patch: Partial<Pick<ActiveQuery, 'id' | 'requestId' | 'jobState' | 'phase' | 'cancellationConfirmed'>>): void {
    // Only an entry this store still holds may be updated: a late callback from a finished call must not
    // rewrite the state of whatever replaced it.
    if (!this.holds(session, query)) return;
    Object.assign(query, patch);
  }

  /**
   * Retire an entry, or keep it as a retained record.
   *
   * The decision is the whole point of this class: a confirmed terminal state is finished, and everything
   * else - a phase that never reached a result, a cancellation the service did not confirm - stays visible
   * because it is exactly what a user may still need to act on.
   */
  finish(session: string, query: ActiveQuery, facts: { phase?: string; jobState?: string | null;
    cancellationConfirmed?: boolean | null } = {}): void {
    const entries = this.sessions.get(session);
    if (!entries) return;
    const index = entries.indexOf(query);
    if (index === -1) return;
    if ('phase' in facts) query.phase = facts.phase ?? query.phase;
    if ('jobState' in facts) query.jobState = facts.jobState ?? null;
    if ('cancellationConfirmed' in facts) query.cancellationConfirmed = facts.cancellationConfirmed ?? null;
    query.settledAt = Date.now();
    // Keep the bounded terminal fact visible for refresh and cancellation confirmation.

  }

  /** Whether one record describes a job whose ending is known: nothing left to ask about or stop. */
  private isSettled(query: ActiveQuery): boolean {
    return query.cancellationConfirmed === true
      || query.phase === 'completed' || query.phase === 'failed' || query.phase === 'cancelled';
  }

  private holds(session: string, query: ActiveQuery): boolean {
    return (this.sessions.get(session) ?? []).includes(query);
  }

  /** Every entry for one session, live ones first, without the retained records that have aged out. */
  private current(session: string): ActiveQuery[] {
    const entries = this.sessions.get(session) ?? [];
    const now = Date.now();
    const kept = entries.filter(entry => entry.settledAt === null
      || now - entry.settledAt < RETAINED_SECONDS * 1000);
    if (kept.length !== entries.length) {
      if (kept.length) this.sessions.set(session, kept);
      else this.sessions.delete(session);
    }
    return kept;
  }

  list(session: string): QueryStatus[] {
    return this.current(session).map(query => this.describe(query));
  }

  /** The entry a panel would act on: the newest live one, else the newest retained one. */
  status(session: string): QueryStatus | null {
    const entries = this.current(session);
    const live = [...entries].reverse().find(entry => entry.settledAt === null);
    const newest = live ?? entries.at(-1);
    return newest ? this.describe(newest) : null;
  }

  private describe(query: ActiveQuery): QueryStatus {
    const ended = query.settledAt ?? Date.now();
    return {
      phase: query.phase, mode: query.mode,
      elapsedSeconds: Math.round(((ended - query.startedAt) / 1000) * 10) / 10,
      jobState: query.jobState,
      requestId: query.requestId,
      cancellationConfirmed: query.cancellationConfirmed,
      live: query.settledAt === null,
      // Cancellable when there is something *to* cancel: a live call this entry can abort, or a retained
      // record whose job the panel can still address through the service.
      cancellable: !this.isSettled(query) && (query.phase !== 'cancelling' || query.settledAt !== null),
    };
  }

  /**
   * Ask every stoppable entry for this session to stop.
   *
   * Plural on purpose: a session can hold a live call and an unresolved record, and cancelling only the
   * newest would leave the one the user is looking at untouched. It returns what it *asked*, never what it
   * achieved - the answer to "did it stop" comes from the call's own cleanup.
   */
  cancel(session: string, requestId: string | null = null): { requested: boolean; retained: boolean; phases: string[] } {
    const entries = this.current(session);
    const phases: string[] = [];
    let retained = false;
    for (const query of entries) {
      if ((requestId !== null && query.requestId !== requestId) || this.isSettled(query)) continue;
      if (query.settledAt !== null) {
        // Nothing local is left to abort. The entry keeps its handle on the job, and the route tries the
        // service directly - which is what a retained record is *for*.
        phases.push(`${query.phase}:retained`);
        retained = true;
        void query;
        continue;
      }
      query.phase = 'cancelling';
      try { query.cancel(); } catch { /* a cancel that throws has already stopped being useful */ }
      phases.push(query.phase);
    }
    return { requested: phases.some(phase => !phase.includes(':')), retained, phases };
  }

  /**
   * Try again on the retained records only, through their own service handle.
   *
   * This is what the panel's button does when there is no live call to abort: it re-asks the service about the
   * job, by id when one was learned and by request id when none was. It returns what the service answered, so
   * a still-unconfirmed result stays unconfirmed rather than becoming a claim.
   */
  async recancel(session: string, requestId: string | null = null): Promise<{ attempted: number; confirmed: number; states: (string | null)[] }> {
    const retained = this.current(session).filter(entry => entry.settledAt !== null
      && !this.isSettled(entry) && (requestId === null || entry.requestId === requestId));
    let confirmed = 0;
    const states: (string | null)[] = [];
    for (const entry of retained) {
      const result = await entry.recancel(entry.id, entry.requestId);
      states.push(result.state);
      if (result.confirmed) {
        confirmed += 1;
        entry.cancellationConfirmed = result.state === 'cancelled';
        entry.jobState = result.state ?? entry.jobState;
        entry.phase = result.state === 'completed' || result.state === 'failed' ? result.state : 'cancelled';
      } else if (result.state) {
        entry.jobState = result.state;
      }
    }
    return { attempted: retained.length, confirmed, states };
  }
}

/** Is this session id one the panel may address? Mirrors the panel bridge's own check. */
export const validSessionId = (value: unknown): value is string =>
  typeof value === 'string' && /^session-[a-zA-Z0-9_-]{1,100}$/.test(value);
