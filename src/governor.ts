/**
 * A per-session failure governor: the plugin's own termination aid for a model that keeps retrying.
 *
 * Why this exists, stated exactly: **the host caps neither tool calls nor consecutive failures.**
 * `@deepseek-ai/dsh-agent-loop` exposes only `maxParallelToolCalls` — there is no per-turn call ceiling and
 * no failure ceiling — and `@deepseek-ai/dsh-repeat-tool-reminder` describes itself as "advisory … without
 * vetoing or rewriting calls", so its thresholds only inject a reminder. Model quota and tool-call count are
 * different budgets: an exhausted provider does not stop an agent from calling a tool that will be refused
 * again. Observed directly during this project: a stub that kept calling produced a 175-step turn that only
 * ended when the process was killed.
 *
 * What a plugin can and cannot do about that:
 *
 * - it **cannot** forbid a call — the dispatch decision belongs to the agent loop;
 * - it **can** refuse to do any work for a call that is repeating, so a loop costs one local decision
 *   instead of an HTTP round trip (and, for a search, instead of a container spawn);
 * - and it **can** end the turn through the host's own primitive, `ToolRunContext.concludeTurn()`, which
 *   marks this execution's result as terminal for the current turn.
 *
 * Two chains are tracked per session, because a loop shows up in two shapes: the *same* call failing over
 * and over (a stuck file handle, a scope that will not change), and *any* call failing over and over (the
 * model varying the query each time). Either chain reaching its threshold ends the turn. Any success clears
 * both, so ordinary work — a search, then a read, then another read — is never touched.
 */

/** Consecutive failures of the same tool with the same arguments before the turn is ended. */
export const MAX_IDENTICAL_FAILURES = 4;
/** Consecutive failures of any kind in one session before the turn is ended. */
export const MAX_CONSECUTIVE_FAILURES = 8;

/** Sessions tracked at once. Same order of magnitude as the library selection's bound. */
export const MAX_GOVERNED_SESSIONS = 500;

export interface StopDecision {
  /** Stable code the model and the log can key on. */
  code: 'KNOWLEDGE_REPEAT_REFUSED';
  /** Which chain tripped, for the message and for a test to assert. */
  chain: 'identical' | 'consecutive';
  /** How many consecutive failures that chain had reached. */
  occurrences: number;
  /** Human-readable instruction the model receives in the tool's own result. */
  message: string;
}

/** Canonical JSON with sorted keys, so two spellings of the same arguments count as the same call. */
function canonical(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value) ?? 'null';
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, item]) => item !== undefined)
    .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0);
  return '{' + entries.map(([key, item]) => JSON.stringify(key) + ':' + canonical(item)).join(',') + '}';
}

export class FailureGovernor {
  private readonly identical = new Map<string, number>();
  private readonly consecutive = new Map<string, number>();
  private readonly order: string[] = [];

  constructor(private readonly maxSessions = MAX_GOVERNED_SESSIONS) {}

  private touch(session: string): void {
    if (this.consecutive.has(session)) return;
    this.order.push(session);
    while (this.order.length > this.maxSessions) {
      const oldest = this.order.shift();
      if (oldest === undefined) break;
      this.consecutive.delete(oldest);
      for (const key of [...this.identical.keys()]) {
        if (key.startsWith(oldest + '\u0000')) this.identical.delete(key);
      }
    }
  }

  /**
   * Record one failed call. Returns the decision to stop when a chain has reached its threshold, and
   * `undefined` while the caller should keep propagating the original error.
   */
  noteFailure(session: string, tool: string, args: unknown): StopDecision | undefined {
    this.touch(session);
    const consecutive = (this.consecutive.get(session) ?? 0) + 1;
    this.consecutive.set(session, consecutive);
    const key = session + '\u0000' + tool + '\u0000' + canonical(args);
    const same = (this.identical.get(key) ?? 0) + 1;
    this.identical.set(key, same);

    const tripped: 'identical' | 'consecutive' | undefined = same >= MAX_IDENTICAL_FAILURES
      ? 'identical'
      : consecutive >= MAX_CONSECUTIVE_FAILURES ? 'consecutive' : undefined;
    if (!tripped) return undefined;
    // The chains are cleared, so if the model calls again anyway it gets a fresh window rather than an
    // immediate second stop; the turn has been ended, which is the part that matters.
    this.identical.delete(key);
    this.consecutive.delete(session);
    const occurrences = tripped === 'identical' ? same : consecutive;
    return {
      code: 'KNOWLEDGE_REPEAT_REFUSED',
      chain: tripped,
      occurrences,
      message: tripped === 'identical'
        ? `${tool} was called ${same} times in a row with the same arguments and failed every time. `
          + 'This plugin has stopped attempting it: repeating an identical call cannot succeed. The turn '
          + 'ends here — report what is already known and what could not be obtained, and do not call it again.'
        : `The knowledge tools have failed ${consecutive} times in a row in this session. This plugin has `
          + 'stopped attempting them: the turn ends here — report the limitation instead of retrying, and do '
          + 'not call the knowledge tools again in this turn.',
    };
  }

  /** Any success clears this session's chains: ordinary work must never trip the governor. */
  noteSuccess(session: string): void {
    this.consecutive.delete(session);
    for (const key of [...this.identical.keys()]) {
      if (key.startsWith(session + '\u0000')) this.identical.delete(key);
    }
  }

  /** Sessions currently carrying a failure count. Exposed for tests and diagnostics only. */
  get size(): number {
    return this.consecutive.size;
  }

  /** Test seam: the canonical form two argument objects are compared by. */
  static canonical = canonical;
}
