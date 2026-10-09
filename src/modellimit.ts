/**
 * The third, independent budget: how many main-Agent model requests may *start*.
 *
 * A test window is bounded by three different quantities, and they must never be added up or substituted
 * for one another:
 *
 *   1. **knowledge-tool dispatches** - one per `knowledge_*` call, bounded in `turnlimit.ts` before any
 *      HTTP request leaves the plugin;
 *   2. **Sirchmunk provider requests** - one per model call the *engine* makes, bounded in the service
 *      parent (`engine/model_proxy.py`) before the proxy sends, and persistable across restarts through
 *      `DSH_KNOWLEDGE_QUOTA_FILE` / `DSH_KNOWLEDGE_QUOTA_MAX`;
 *   3. **main-Agent provider requests** - one per model call the *calling Agent* makes, bounded here.
 *
 * They are not the same size. One `knowledge_search` can make the engine issue several provider requests,
 * and the Agent that reads the result spends provider requests of its own that this plugin never sees.
 * So counting (1) as if it bounded (3) would understate a window by an unknown factor - which is the
 * whole reason this module exists rather than a note in the prompt.
 *
 * **The hook is pre-dispatch, and that is the point.** `llm/stream` is a waterfall around every
 * streaming model call in the host: a listener may call `next()` to reach the adapter, or yield its own
 * chunks instead. A listener that does *not* call `next()` costs no provider request at all - so the
 * ceiling is enforced *before* the request is sent, not reported after it.
 *
 * **A retry cannot slip past this count, and that was checked rather than assumed.** The host's retry
 * executor (`@deepseek-ai/dsh-llm-retry`) does not retry inside the waterfall: it listens on
 * `agent/request-error` and answers `{kind: 'retry'}`, which makes the agent loop **re-run the failed
 * step** - a fresh model request that enters `llm/stream` again and is therefore counted again. The
 * adapter below the waterfall does not retry either: `dsh-llm-deepseek` throws `LlmError` on a non-2xx
 * response, and its only internal `continue` is a single re-upload after the provider rejects a file for
 * quota, which never happens on a text-only route. So `attempted` covers retries, and
 * `tests/modellimit.test.mjs` pins that a second dispatch of the same request is a second attempt.
 *
 * The residual case is stated instead of hidden: a *future* adapter that retried transport failures
 * inside one `llm/stream` call would be invisible to every listener on this hook, because no listener
 * can see below it. `usageUnknown` exists so that an attempt whose end was never observed is visible as
 * an unknown rather than counted as a success.
 *
 * **The count can outlive the process.** When a quota file is configured the ledger is read and written
 * per attempt, so a plugin reload - or a host restart mid-window - continues the same window instead of
 * silently granting a fresh one. An unreadable ledger refuses the request: a ceiling a disk hiccup can
 * lift is not a ceiling.
 *
 * The ceiling is **off unless configured**. A default cap would start refusing ordinary conversations,
 * and a budget that silently changes normal behaviour is worse than no budget: the window sets it, and
 * the window is a test.
 */
import { closeSync, openSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs';
import { LlmError } from '@deepseek-ai/dsh-llm';

/** The configured ceiling, or 0 for "no ceiling". Set by the test window, not by this code. */
export const MAX_AGENT_REQUESTS_ENV = 'DSH_KNOWLEDGE_MAX_AGENT_REQUESTS';

/** A ledger file that keeps the window across plugin reloads and host restarts. */
export const AGENT_QUOTA_FILE_ENV = 'DSH_KNOWLEDGE_AGENT_QUOTA_FILE';

/**
 * The second key that must be turned before a ledger is honoured: `=1` arms it, anything else ignores it.
 *
 * One environment variable is a thin interlock for something whose scope is **the whole host process**. An
 * exported variable is inherited by every process started from that shell - including a host that serves
 * unrelated conversations - and a variable exported for one afternoon and forgotten is exactly how a
 * test-window ceiling outlives its window. Requiring a second, deliberately-named key makes arming a visible
 * two-part decision, and means an operator who inherits one variable by accident gets *no ceiling* rather
 * than a refusal they cannot explain.
 */
export const ARM_AGENT_QUOTA_ENV = 'DSH_KNOWLEDGE_ARM_AGENT_QUOTA';

/** How many outcomes a report keeps per dispatched request. Bounded so a long life cannot grow. */
export const MAX_TRACKED_OUTCOMES = 1000;

/** Why a request did not become a provider request. */
export const BUDGET_EXHAUSTED = 'AGENT_REQUEST_BUDGET_EXHAUSTED';

/** How long to wait for the ledger lock before refusing. Short: this is a byte-sized file, not work. */
const QUOTA_LOCK_TIMEOUT_MS = 2000;

export type AttemptOutcome = 'completed' | 'failed' | 'aborted' | 'unknown';

export interface ModelBudgetReport {
  /** Entries into the dispatch hook. Always >= `dispatched`. */
  attempted: number;
  /** Requests handed to the adapter: the ones a provider actually started. */
  dispatched: number;
  /** Dispatched requests that reached a terminal success frame. */
  completed: number;
  /** Dispatched requests whose terminal frame reported an error. */
  failed: number;
  /** Dispatched requests whose terminal frame reported an abort, or that never finished. */
  aborted: number;
  /** Requests refused here. No provider request was made. */
  refused: number;
  /**
   * Dispatched requests whose terminal frame was never seen. Their token usage is **unknown** - it is
   * reported as a count of unknowns and never folded into a zero.
   */
  usageUnknown: number;
  /** `max > 0`: a ceiling is in force. */
  enforced: boolean;
  /** The ceiling, or 0 when none is configured. */
  max: number;
  /** `max - attempted`, floored at 0, when a ceiling is in force. */
  remaining: number;
  /** Characters of the request images dispatched, or null when measurement was off. */
  requestChars: number | null;
  /** True when the window lives in a ledger file, so a reload continues it instead of resetting it. */
  persisted: boolean;
  /**
   * Where the effective ceiling comes from. **Not** a second way of saying `enforced`: the pair
   * (`persisted: true, source: 'none'`) is the state that caused an outage - a ledger path configured
   * with no window open - and it is the pair a reader needs to tell "not opened yet" from "opened and
   * spent", which the numbers alone cannot express.
   */
  source: 'ledger' | 'configured' | 'none';
  /** The counting basis, so a reader never has to infer it. */
  countedAs: string;
  /**
   * What the counter counts over. A closed set with one member today, and it is not a formality: `llm/stream`
   * is a **host-process** waterfall whose payload carries no session identity, so these numbers belong to the
   * process, not to the conversation reading them. Every session, every plugin that uses the hook and every
   * retry inside them land in the same counter.
   *
   * The field exists because the mistake it prevents was made: a value starting in the hundreds reads as
   * "this conversation has made hundreds of calls", and a reader who says that out loud is reporting a number
   * nobody measured. `scope` sits on the report itself so a card, a model-facing sentence or any later reader
   * can say what the number is *over* without having to parse `countedAs`.
   */
  scope: 'host-process';
}

export interface BudgetEnvironment {
  [MAX_AGENT_REQUESTS_ENV]?: string | undefined;
  [AGENT_QUOTA_FILE_ENV]?: string | undefined;
  [ARM_AGENT_QUOTA_ENV]?: string | undefined;
}

/**
 * What a ledger file says. Three states, because "no file" and "a file that does not state a ceiling"
 * need opposite handling and collapsing them into one value is exactly the defect this type prevents:
 *
 * - `absent` - no file: the window is **not armed**, nothing is counted and nothing is written. Deleting
 *   the file is how an operator disarms a window, which is only true if this state means "no ceiling".
 * - `armed` - a file that states `{used, max}` with an integer `max >= 1`: the window is in force.
 * - `invalid` - a file that exists but cannot be used: a refusal that names the defect, never a silent
 *   fall back to some other number.
 */
export type LedgerState =
  | { state: 'absent' }
  | { state: 'armed'; used: number; max: number }
  | { state: 'invalid'; why: string };

interface Outcome {
  outcome: AttemptOutcome;
  chars: number | null;
}

/**
 * Classify a terminal `finish` frame. Tolerant on purpose: the host normalizes adapter failures to a
 * terminal `error`/`aborted` finish, and a vocabulary that grows must not silently turn a failure into a
 * success.
 */
export function classifyFinish(reason: unknown): AttemptOutcome {
  const text = String(reason ?? '').toLowerCase();
  if (!text) return 'unknown';
  if (text.includes('abort') || text.includes('cancel')) return 'aborted';
  if (text.includes('error') || text.includes('fail')) return 'failed';
  return 'completed';
}

export class ModelRequestBudget {
  private attemptedCount = 0;
  private dispatchedCount = 0;
  private refusedCount = 0;
  private chars: number | null;
  private outcomes: Outcome[] = [];
  private readonly ledger: string | null;
  /** The ceiling a ledger file states. Null when no ledger file is stating one. */
  private declaredMax: number | null = null;
  /** Set when the ledger is configured but unusable. Refuses requests; never fails the plugin load. */
  private ledgerError: string | null = null;

  constructor(private readonly max = 0, measure = max > 0, ledger: string | null = null) {
    this.chars = measure ? 0 : null;
    this.ledger = ledger && ledger.trim() ? ledger.trim() : null;
    // A ledger that already holds a count continues it: the window belongs to the operator's file, not
    // to this process. A ledger that is configured but unusable is *recorded* here and refuses at the
    // first reserve - it must not throw out of the constructor, because a plugin that fails to load loses
    // all five tools, which is a far worse outcome than refusing model requests.
    const stored = this.readLedger();
    if (stored.state === 'armed') {
      this.attemptedCount = stored.used;
      this.dispatchedCount = stored.used;
      this.declaredMax = stored.max;
    } else if (stored.state === 'invalid') {
      this.ledgerError = `the window ledger is unusable: ${stored.why}`;
    }
    // `absent` is not an error and is not a ceiling of zero: a configured ledger path with no file means
    // the window has not been opened, so the only ceiling is the configured number (which may be none).
  }

  /**
   * The ceiling from the environment, so the window can set it without touching plugin config.
   *
   * Two keys are required to arm anything: a ledger path (or a ceiling number) **and**
   * `DSH_KNOWLEDGE_ARM_AGENT_QUOTA=1`. With the arming key absent the whole budget is inert - no ceiling, no
   * file read, no file written - because the blast radius of a mistake here is every session the host serves.
   */
  static fromEnvironment(environment: BudgetEnvironment = {}): ModelRequestBudget {
    const armed = (environment[ARM_AGENT_QUOTA_ENV] ?? '').trim();
    if (armed !== '1') return new ModelRequestBudget(0, false, null);
    const raw = (environment[MAX_AGENT_REQUESTS_ENV] ?? '').trim();
    const max = /^\d+$/.test(raw) ? Number(raw) : 0;
    const ledger = (environment[AGENT_QUOTA_FILE_ENV] ?? '').trim() || null;
    return new ModelRequestBudget(max >= 1 ? max : 0, max >= 1, ledger);
  }

  /**
   * The effective ceiling: a ledger file's number when one is stating it, otherwise the configured
   * number. `0` means **no ceiling at all** - never "a ceiling of zero", which is the reading that
   * refused every request.
   */
  get maxRequests(): number {
    return this.declaredMax ?? this.max;
  }

  /** Where the effective ceiling comes from, so a report never has to imply it by arithmetic. */
  get source(): 'ledger' | 'configured' | 'none' {
    if (this.declaredMax !== null) return 'ledger';
    return this.max >= 1 ? 'configured' : 'none';
  }

  /**
   * What the ledger file says, as three **structurally distinct** states.
   *
   * This used to return `(used, max)` or `null`, with `null` meaning "no file" and a missing `max`
   * silently falling back to `this.max`. Those two conflations were one bug: with a ledger configured and
   * no ceiling given anywhere else, `this.max` is 0, so "no file yet" produced `max = 0` and `used = 1`,
   * and **every** main-Agent request was refused with "the window allows 0 model requests". A missing
   * file must mean *unarmed*, and a file that is present but does not say what the ceiling is must be a
   * refusal that names the defect - never a silent zero.
   */
  private readLedger(): LedgerState {
    if (!this.ledger) return { state: 'absent' };
    let payload: any;
    try {
      payload = JSON.parse(readFileSync(this.ledger, 'utf8'));
    } catch (error: any) {
      if (error?.code === 'ENOENT') return { state: 'absent' }; // no window opened: the window is OFF
      if (error instanceof SyntaxError) return { state: 'invalid', why: 'not valid JSON' };
      return { state: 'invalid', why: error?.code ?? 'unknown' };
    }
    const used = Number.isInteger(payload?.used) && payload.used >= 0 ? payload.used : 0;
    const max = payload?.max;
    if (!Number.isInteger(max) || max < 1) {
      // Present but unusable: refuse, and say which field is wrong. Falling back to a number from
      // somewhere else here is what made a config typo indistinguishable from a spent window.
      return { state: 'invalid', why: `its "max" is ${JSON.stringify(max ?? null)}, expected an integer >= 1` };
    }
    return { state: 'armed', used, max };
  }

  private writeLedger(used: number, max: number): void {
    if (!this.ledger) return;
    const temporary = `${this.ledger}.${process.pid}.tmp`;
    writeFileSync(temporary, JSON.stringify({ used, max, updatedAt: new Date().toISOString() }), 'utf8');
    // A rename is atomic on one volume, so a reader that does *not* hold the lock (the constructor, which
    // resumes a window) can never observe a half-written ledger and mistake it for a corrupt one.
    renameSync(temporary, this.ledger);
  }

  /**
   * Spend one slot in the ledger, or report why it could not be spent.
   *
   * Serialised with an exclusive lock file, because two concurrent model requests in one step must not
   * both read the same `used` and both decide they are under the ceiling - that would lose exactly the
   * requests the ceiling exists to bound.
   */
  private reserveLedger(): { max: number; used: number } | { unarmed: true } | { refusal: string } {
    const lock = `${this.ledger}.lock`;
    const deadline = Date.now() + QUOTA_LOCK_TIMEOUT_MS;
    let handle: number | null = null;
    while (handle === null) {
      try {
        handle = openSync(lock, 'wx');
      } catch (error: any) {
        if (error?.code === 'EEXIST') {
          if (Date.now() > deadline) return { refusal: 'the window ledger is locked by another writer' };
          continue;
        }
        return { refusal: `the window ledger is unavailable (${error?.code ?? 'unknown'})` };
      }
    }
    try {
      const stored = this.readLedger();
      if (stored.state === 'invalid') {
        return { refusal: `the window ledger is unusable: ${stored.why}` };
      }
      // The ceiling, in order of authority: what the file states, else what was configured. When neither
      // says anything the window is **not open** - nothing is counted, nothing is written, and the report
      // says `source: 'none'`. This is the branch that used to deny every request.
      const max = stored.state === 'armed' ? stored.max : this.max;
      if (max < 1) return { unarmed: true };
      const used = (stored.state === 'armed' ? stored.used : 0) + 1;
      if (used > max) {
        return { refusal: `the window allows ${max} model requests and they are spent` };
      }
      this.writeLedger(used, max);
      return { max, used };
    } catch (error: any) {
      return { refusal: `the window ledger is unavailable (${error?.code ?? 'unknown'})` };
    } finally {
      closeSync(handle);
      try { unlinkSync(lock); } catch { /* released by the unlink below at worst */ }
    }
  }

  private measureRequest(options: unknown): number | null {
    if (this.chars === null) return null;
    try {
      return JSON.stringify(options ?? null)?.length ?? 0;
    } catch {
      // A cyclic or otherwise unstringifiable request image is not a reason to refuse the call; it is a
      // reason to say the size was not measured.
      return null;
    }
  }

  /**
   * Count one attempt and decide whether it may reach the provider.
   *
   * The count happens here, at entry, **before** anything is sent, and it is never rolled back: a request
   * that is refused, fails, or is cancelled has already been counted. That is the only counting basis that
   * matches "spent", and it is also what makes a retry visible - the retry is a fresh entry here, not a
   * continuation of the counted one.
   */
  reserve(options?: unknown): { allowed: boolean; reason?: string; chars: number | null } {
    if (this.ledger) {
      if (this.ledgerError) {
        this.refusedCount += 1;
        return { allowed: false, reason: this.refusalReason(this.ledgerError), chars: null };
      }
      const spent = this.reserveLedger();
      if ('refusal' in spent) {
        this.refusedCount += 1;
        return { allowed: false, reason: this.refusalReason(spent.refusal), chars: null };
      }
      if ('unarmed' in spent) {
        // The ledger path is configured but no window is open: no ceiling, and no ceiling is *reported*
        // as none. Counting continues in memory so the report still says how much was spent unarmed.
        this.attemptedCount += 1;
        this.dispatchedCount += 1;
        const measured = this.measureRequest(options);
        if (measured !== null) this.chars = (this.chars ?? 0) + measured;
        return { allowed: true, chars: measured };
      }
      this.attemptedCount = spent.used;
      this.declaredMax = spent.max;
      this.dispatchedCount += 1;
      const measured = this.measureRequest(options);
      if (measured !== null) this.chars = (this.chars ?? 0) + measured;
      return { allowed: true, chars: measured };
    }
    this.attemptedCount += 1;
    if (this.max > 0 && this.attemptedCount > this.max) {
      this.refusedCount += 1;
      return { allowed: false, reason: this.refusalReason(), chars: null };
    }
    const chars = this.measureRequest(options);
    if (chars !== null) this.chars = (this.chars ?? 0) + chars;
    this.dispatchedCount += 1;
    return { allowed: true, chars };
  }

  /** The message a refusal carries. It names the counts and the basis - never a token count. */
  refusalReason(extra?: string): string {
    const ceiling = this.maxRequests > 0 ? `${this.maxRequests}` : 'the configured';
    const used = Math.max(0, this.attemptedCount - (this.ledger ? 0 : 1));
    return `${BUDGET_EXHAUSTED}: this window allows ${ceiling} main-Agent model requests and ${used} have `
      + `already been attempted; the request was not sent${extra ? ` (${extra})` : ''}. The ceiling counts every `
      + 'attempt that reaches dispatch - refused, failed, cancelled and retried alike - so a retry cannot '
      + 'buy a request the window has already spent.';
  }

  /** Record how one dispatched request ended. Called from the wrapper around `next()`. */
  settle(outcome: AttemptOutcome, chars: number | null): void {
    if (this.outcomes.length >= MAX_TRACKED_OUTCOMES) this.outcomes.shift();
    this.outcomes.push({ outcome, chars });
  }

  /**
   * Wrap the adapter's stream so the outcome is observed without touching a single chunk.
   *
   * A stream that ends without a terminal `finish` frame is `unknown`, not `completed`: the request was
   * dispatched and its usage was never reported, and the two must not be conflated.
   */
  async *observe(stream: AsyncIterable<any>, chars: number | null): AsyncIterable<any> {
    let terminal: AttemptOutcome | null = null;
    try {
      for await (const chunk of stream) {
        if (chunk && typeof chunk === 'object' && (chunk as any).type === 'finish') {
          terminal = classifyFinish((chunk as any).reason);
        }
        yield chunk;
      }
    } catch (error) {
      this.settle('failed', chars);
      throw error;
    }
    this.settle(terminal ?? 'unknown', chars);
  }

  /**
   * Move the guard *inside* the stream too, so the refusal is raised by whoever consumes it.
   *
   * `guard` is synchronous by design: the decision must be taken before `next()` exists. But a waterfall
   * listener that throws synchronously where the host expects an iterable produces an unhandled throw
   * rather than a failed request, so the same refusal is also carried as a rejected stream. Both carry the
   * same typed error, so a caller sees one code either way.
   */
  private refusalStream(error: LlmError): AsyncIterable<any> {
    return {
      [Symbol.asyncIterator]() {
        return { next: () => Promise.reject(error) };
      },
    } as AsyncIterable<any>;
  }

  /**
   * The waterfall listener body: count, decide, and either refuse without dispatching or dispatch and
   * observe. Kept free of cordis types so a test can drive it directly.
   */
  guard(options: unknown, next: () => AsyncIterable<any>): AsyncIterable<any> {
    const decision = this.reserve(options);
    if (!decision.allowed) {
      // A typed, dedicated code: `dsh-llm-retry` only retries a failure whose code is in the provider's
      // `retryableCodes`, and this one never is. A ceiling a retry could bounce off would spend more
      // requests than the window allows - which is the whole failure this class exists to prevent.
      throw new LlmError(decision.reason ?? BUDGET_EXHAUSTED, BUDGET_EXHAUSTED);
    }
    return this.observe(next(), decision.chars);
  }

  /** The same guard, refusing through the stream instead of by throwing. Used where the host needs one. */
  guardedStream(options: unknown, next: () => AsyncIterable<any>): AsyncIterable<any> {
    let decision: { allowed: boolean; reason?: string; chars: number | null };
    try {
      decision = this.reserve(options);
    } catch (error) {
      return this.refusalStream(error as LlmError);
    }
    if (!decision.allowed) {
      return this.refusalStream(new LlmError(decision.reason ?? BUDGET_EXHAUSTED, BUDGET_EXHAUSTED));
    }
    return this.observe(next(), decision.chars);
  }

  report(): ModelBudgetReport {
    const counts = { completed: 0, failed: 0, aborted: 0, unknown: 0 } as Record<AttemptOutcome, number>;
    for (const outcome of this.outcomes) counts[outcome.outcome] += 1;
    return {
      attempted: this.attemptedCount,
      dispatched: this.dispatchedCount,
      completed: counts.completed,
      failed: counts.failed,
      aborted: counts.aborted,
      refused: this.refusedCount,
      usageUnknown: counts.unknown,
      enforced: this.maxRequests > 0,
      max: this.maxRequests,
      remaining: this.maxRequests > 0 ? Math.max(0, this.maxRequests - this.attemptedCount) : 0,
      requestChars: this.chars,
      persisted: this.ledger !== null,
      source: this.source,
      countedAs: 'every attempt that reached dispatch, including refused, failed, cancelled and retried '
        + 'ones; a retry re-enters this hook as a new attempt (the host retries by re-running the failed '
        + 'step), so it cannot spend a request the window has already counted - and an attempt whose end '
        + 'was never observed is reported as `usageUnknown`, never as zero',
      scope: 'host-process',
    };
  }
}
