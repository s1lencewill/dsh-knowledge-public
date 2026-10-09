/**
 * A per-turn ceiling on knowledge-tool calls: the second, independent limit.
 *
 * The governor in `governor.ts` bounds *failure*: it stops a call that keeps failing. That is a different
 * budget from this one, and the difference matters:
 *
 * - a failure chain is cleared by any success, so "no failures" says nothing about how many calls a turn
 *   made. A turn can call successfully 200 times and the governor never fires;
 * - the host caps neither tool calls per turn nor consecutive failures (`agent-loop` exposes only
 *   `maxParallelToolCalls`), so nothing above this plugin bounds the total. An observed turn in this project
 *   reached 175 steps before it was killed.
 *
 * So this counts **every** knowledge-tool call of a turn - success, failure or cancellation - and does not
 * clear on success. When the ceiling is reached the call is answered locally: no HTTP, and for a search no
 * container spawn.
 *
 * Scope, stated honestly: this bounds the four `knowledge_*` tools registered by this plugin. It cannot bound
 * the host's other tools, because it is not part of the agent loop and has no authority over dispatch.
 *
 * **The turn identity is a durable host fact, not a heuristic.** A tool call arrives with no turn id
 * (`ToolRunContext` carries `callId`/`rootCallId`, no turn), but the session it belongs to is on the
 * execution (`exec.agent.session`), and that session is an append-only log whose `seq` is the log length and
 * whose `eventAt(seq)` returns each event. Walking back from the end to the last `turn/start` yields that
 * turn's own sequence number - assigned by the host, not by the model, and impossible to reset by changing
 * arguments. The walk is bounded and memoised; a turn whose `turn/start` cannot be found inside the bound is
 * treated as **unobserved**, which counts against one non-resetting window per session rather than being
 * waved through.
 */

/** Knowledge-tool calls allowed in one turn. The real-model window pins the same number. */
export const MAX_TURN_KNOWLEDGE_CALLS = 6;

/** Turns tracked at once, matching the governor's session bound. */
export const MAX_TRACKED_TURNS = 500;

/** How far back the turn scan looks before declaring the boundary unobserved. */
export const TURN_SCAN_LIMIT = 4000;

/** Sessions whose turn scan is memoised at once. */
export const MAX_MEMOISED_SESSIONS = 500;

export interface TurnIdentity {
  /** The session's own id, from the session header. */
  session: string;
  /** The key this call is counted under. `#unobserved` marks a session with no locatable turn boundary. */
  key: string;
  /**
   * `turn` when a `turn/start` was found, `unobserved` when the scan hit its bound or the session log was
   * not reachable. The stop message says which, so a ceiling reached without a boundary is never reported as
   * an ordinary turn limit.
   */
  boundary: 'turn' | 'unobserved';
}

/** The minimal session face this needs. The host's `Session` satisfies it; a test can supply the same shape. */
export interface SessionLike {
  readonly seq?: number;
  eventAt?(seq: number): { type?: unknown; seq?: unknown } | undefined;
}

interface Memo {
  scannedAt: number;
  identity: TurnIdentity;
}

/**
 * Resolve the turn a session is currently in.
 *
 * Memoised on the session's own length, so the common case (several calls inside one turn) rescans only the
 * events appended since the previous call. A `turn/start` event's own `seq` is the turn's identity, which is
 * why two turns can never share a key, and why a resumed session keeps counting from its own log.
 */
export function resolveTurn(sessionId: string, session: SessionLike | undefined,
                           memo: Map<string, Memo> = new Map()): TurnIdentity {
  const unobserved: TurnIdentity = { session: sessionId, key: sessionId + '#unobserved', boundary: 'unobserved' };
  if (!session || typeof session.eventAt !== 'function' || typeof session.seq !== 'number') return unobserved;
  const end = session.seq;
  if (!Number.isInteger(end) || end <= 0) return unobserved;
  const cached = memo.get(sessionId);
  if (cached && cached.scannedAt === end) return cached.identity;

  const lowest = Math.max(0, end - TURN_SCAN_LIMIT);
  for (let seq = end - 1; seq >= lowest; seq -= 1) {
    let event: { type?: unknown; seq?: unknown } | undefined;
    try { event = session.eventAt(seq); } catch { return unobserved; }
    if (!event) break;
    if (event.type === 'turn/start') {
      const identity: TurnIdentity = { session: sessionId, key: sessionId + '#' + String(event.seq ?? seq), boundary: 'turn' };
      remember(memo, sessionId, { scannedAt: end, identity });
      return identity;
    }
  }
  // No boundary inside the bound. Remember the *unobserved* answer for this log length so repeated calls do
  // not rescan, but never invent a turn key for it.
  const unobservedAnswer: Memo = { scannedAt: end, identity: unobserved };
  remember(memo, sessionId, unobservedAnswer);
  return unobserved;
}

/** Memoise one session's turn answer, keeping the memo bounded. A cleared memo only costs a rescan. */
function remember(memo: Map<string, Memo>, sessionId: string, entry: Memo): void {
  if (!memo.has(sessionId) && memo.size >= MAX_MEMOISED_SESSIONS) memo.clear();
  memo.set(sessionId, entry);
}

export interface Reservation {
  allowed: boolean;
  /** Calls counted for this turn including this one. Exceeds `max` once calls are being refused. */
  used: number;
  max: number;
  identity: TurnIdentity;
}

/**
 * What the count means, in one fixed sentence.
 *
 * Every knowledge result carries this, because a number without its basis is not information: `used` counts
 * **attempts**, so a refused call still moves it, and a model that assumes successes-only accounting will
 * mis-plan. It is a constant string rather than per-call prose so the field cannot become a channel for
 * anything but this statement.
 */
export const BUDGET_BASIS =
  'counts every knowledge-tool attempt of this turn, including attempts this plugin refused locally';

/** The per-turn accounting, as every knowledge result reports it. Numbers only. */
export interface TurnBudget {
  /** Which budget this is. One value today; the field exists so a second budget cannot be confused with it. */
  layer: 'turn';
  activity?: {scope:'turn'; attempts:number; httpRequests:number; searches:number; reads:number; countedAs:string};
  /** The tool the budget is being reported on. */
  tool: string;
  /** Attempts counted for this turn, including this one and including refused ones. */
  used: number;
  max: number;
  /** Further attempts that would still be dispatched. `0` means every further call is refused locally. */
  remaining: number;
  countedAs: string;
  /**
   * A fixed sentence the caller may attach when the budget is nearly or fully spent, so the model learns it
   * must switch from gathering to answering **while the result it is reading is still in front of it**. Set by
   * the tool boundary, never by the ledger: the ledger counts, it does not advise.
   */
  note?: string;
}

/** What a turn actually obtained, so a limit stop can say what exists rather than only what stopped. */
export interface TurnTally {
  /** Dispatched calls by tool name. Bounded: only this plugin's own tool names can be keys. */
  calls: Record<string, number>;
  /** How many verified excerpts every search of this turn returned, summed. */
  evidence: number;
  /** How many catalogue documents every locate/enumerate call of this turn returned, summed. */
  documents: number;
  /**
   * How many read windows this turn returned.
   *
   * Counted separately from `evidence` on purpose: a read window is material the turn obtained, and a closing
   * summary that omitted it would describe a turn of six reads as having obtained nothing. It is not an
   * "excerpt" either — a search excerpt is a verified quotation, a read window is a range of the original — so
   * the two are never added together.
   */
  reads: number;
}

/** Tool names the tally will record. A closed set, so the record cannot grow with caller input. */
const TALLY_TOOLS = [
  'knowledge_list_libraries', 'knowledge_search', 'knowledge_find_documents',
  'knowledge_list_documents', 'knowledge_read',
] as const;

/** Attempts that are still dispatched, from an attempt count that includes refusals. */
export function remainingFrom(used: number, max: number): number {
  return Math.max(0, max - Math.min(used, max));
}

/**
 * The budget one result reports.
 *
 * `used` is **the ordinal this call was admitted with**, not the ledger's count at the moment the result was
 * assembled. The difference is visible exactly when it matters: two parallel calls are admitted as #5 and #6,
 * and the second one's result may be assembled after both. Reporting the later count would label the call that
 * was admitted as #5 with `used: 6`, which is not true of it — and would make `remaining` on an admitted call
 * depend on how long its own service round trip happened to take.
 *
 * So the numbers describe this call's own admission, and the `note` says the same thing. It is a *historical*
 * fact, which is the only kind a result can honestly carry: what the turn's budget was when this call was let
 * through.
 */
export function turnBudget(tool: string, used: number, max: number, note?: string): TurnBudget {
  const budget: TurnBudget = {
    layer: 'turn', tool, used, max, remaining: remainingFrom(used, max), countedAs: BUDGET_BASIS,
  };
  if (note !== undefined) budget.note = note;
  return budget;
}

export interface TurnLimitOptions {
  max?: number;
  maxForSession?: (sessionId: string) => number;
  maxTurns?: number;
  /** Test seam: a monotonic clock, so eviction can be driven deterministically. */
  now?: () => number;
}

/**
 * The counter itself.
 *
 * `reserve()` is **synchronous and contains no `await`**: reading and writing the count are one step, which
 * is what makes the reservation atomic. Parallel tool calls in one step therefore cannot both take the last
 * slot - a property the tests assert with concurrent callers, and the reason the reservation is taken before
 * dispatch rather than after the result.
 */
export class TurnLedger {
  private readonly counts = new Map<string, { used: number; max: number; touched: number; tally: TurnTally; closing: boolean; httpRequests:number; searches:number; reads:number }>();
  private readonly maxForSession?: (sessionId: string) => number;
  private readonly memo = new Map<string, Memo>();
  private readonly now: () => number;
  readonly max: number;
  readonly maxTurns: number;

  constructor(options: TurnLimitOptions = {}) {
    this.maxForSession = options.maxForSession;
    this.max = Number.isInteger(options.max) && (options.max as number) > 0 ? options.max as number : MAX_TURN_KNOWLEDGE_CALLS;
    this.maxTurns = Number.isInteger(options.maxTurns) && (options.maxTurns as number) > 0 ? options.maxTurns as number : MAX_TRACKED_TURNS;
    this.now = options.now ?? (() => Date.now());
  }

  /**
   * Take one slot for this turn, or refuse.
   *
   * A refusal is still recorded, so a caller that ignores the stop and calls again gets the same answer
   * rather than a fresh allowance.
   */
  reserve(sessionId: string, session?: SessionLike, tool?: string): Reservation {
    const identity = resolveTurn(sessionId, session, this.memo);
    const existing = this.counts.get(identity.key);
    // Pin at first admission; settings changes never replenish a running turn.
    const configured = this.maxForSession?.(sessionId) ?? this.max;
    const max = existing?.max ?? (Number.isInteger(configured) && configured >= 1 && configured <= 30 ? configured : this.max);
    const used = (existing?.used ?? 0) + 1;
    if (!existing) this.evictIfNeeded();
    this.counts.set(identity.key, {
      used, max, touched: this.now(),
      httpRequests: existing?.httpRequests ?? 0,
      searches: (existing?.searches ?? 0) + (tool === "knowledge_search" ? 1 : 0),
      reads: (existing?.reads ?? 0) + (tool === "knowledge_read" ? 1 : 0),
      tally: existing?.tally ?? { calls: {}, evidence: 0, documents: 0, reads: 0 },
      // The closing claim travels with the count so the two can never be evicted apart: a turn whose
      // count is forgotten must not be able to hand out a second final-answer step.
      closing: existing?.closing ?? false,
    });
    return { allowed: used <= max, used, max, identity };
  }

  /**
   * Claim this turn's single closing instruction. **Exactly one caller can win.**
   *
   * Synchronous and `await`-free, for the same reason `reserve` is: parallel calls in one step race for the
   * last slot, and they must race for the closing step the same way. The claim is per turn key, so a model
   * that spends the closing step on further tool calls does not get another one - which is what bounds the
   * extra model step at one instead of restoring an unbounded refusal loop.
   */
  claimClosing(key: string): boolean {
    const entry = this.counts.get(key);
    if (!entry || entry.closing) return false;
    entry.closing = true;
    return true;
  }

  /** Whether the closing instruction has already been handed to this turn. Diagnostics and tests. */
  closingClaimed(key: string): boolean {
    return this.counts.get(key)?.closing === true;
  }

  /** Record what a dispatched call returned, so a later stop can describe the turn's actual yield. */
  noteOutcome(key: string, tool: string, value: unknown): void {
    const entry = this.counts.get(key);
    if (!entry) return;
    if ((TALLY_TOOLS as readonly string[]).includes(tool)) {
      entry.tally.calls[tool] = (entry.tally.calls[tool] ?? 0) + 1;
    }
    const record = value && typeof value === 'object' ? value as Record<string, unknown> : null;
    if (!record) return;
    if (Array.isArray(record.evidence)) entry.tally.evidence += record.evidence.length;
    // Two spellings, one meaning: a locate returns `documents`, a catalogue page returns `entries`. Counting
    // only the first would report a full enumeration of a library as having obtained nothing.
    if (Array.isArray(record.documents)) entry.tally.documents += record.documents.length;
    if (Array.isArray(record.entries)) entry.tally.documents += record.entries.length;
    // A read window is identified by its own tool rather than by shape: `excerpt` alone is also a field of an
    // evidence item, and counting those twice would inflate the summary.
    if (tool === 'knowledge_read' && typeof record.relativePath === 'string') entry.tally.reads += 1;
  }

  /**
   * Note that a **dispatched** call failed. The attempt is counted in the per-tool tally exactly as a success
   * is, because the tally answers "what did this turn spend itself on"; the turn's yield (`evidence` and
   * `documents`) is deliberately left alone, since a failed call returned no material.
   */
  noteFailure(key: string, tool: string): void {
    const entry = this.counts.get(key);
    if (!entry) return;
    if ((TALLY_TOOLS as readonly string[]).includes(tool)) {
      entry.tally.calls[tool] = (entry.tally.calls[tool] ?? 0) + 1;
    }
  }

  /** The turn's yield so far. A copy, so a caller can neither read nor write the ledger's own record. */
  tallyFor(key: string): TurnTally {
    const tally = this.counts.get(key)?.tally;
    return tally
      ? { calls: { ...tally.calls }, evidence: tally.evidence, documents: tally.documents, reads: tally.reads }
      : { calls: {}, evidence: 0, documents: 0, reads: 0 };
  }

  noteHttp(key:string):void { const entry=this.counts.get(key); if(entry) entry.httpRequests++; }
  activityFor(key:string): NonNullable<TurnBudget['activity']> {
    const e=this.counts.get(key);
    return {scope:'turn',attempts:e?.used ?? 0,httpRequests:e?.httpRequests ?? 0,searches:e?.searches ?? 0,reads:e?.reads ?? 0,
      countedAs:'HTTP fetch dispatch attempts, including polls and cleanup; not server receipts or provider requests'};
  }

  /** Calls counted for a turn so far, for diagnostics and tests. */
  usedFor(key: string): number {
    return this.counts.get(key)?.used ?? 0;
  }

  /** Turns currently carrying a count. Exposed for tests and diagnostics only. */
  get size(): number {
    return this.counts.size;
  }

  /** Oldest-first eviction, so a long-lived host cannot grow this without bound. */
  private evictIfNeeded(): void {
    if (this.counts.size < this.maxTurns) return;
    const ordered = [...this.counts.entries()].sort((left, right) => left[1].touched - right[1].touched);
    while (ordered.length > 0 && this.counts.size >= this.maxTurns) {
      const oldest = ordered.shift();
      if (oldest) this.counts.delete(oldest[0]);
    }
  }
}
