/**
 * Short, opaque read references: what the model copies instead of a 300-character signed handle.
 *
 * The problem this exists for is measured, not hypothetical. A service handle is
 * `krh1.<base64url(payload)>.<base64url(hmac)>`, 292-316 characters in the observed real library, and the
 * model has to reproduce it byte for byte in a later `knowledge_read` argument. In the recorded failing
 * session it did not: the handle it sent had never been issued, and its signature segment was byte-identical
 * to one that *had* been issued while its payload decoded to a different path — a token-level copy error
 * that the service correctly refused. The plugin cannot make the model a better transcriber; it can stop
 * asking the model to transcribe 300 characters.
 *
 * What this is **not**: a weakening of verification. A reference resolves to the *original signed token*,
 * which the service still authenticates and still re-checks against the library, the path and the source
 * version on every read. A reference grants nothing on its own. It is a pointer held in this process, not a
 * credential, and losing it costs one re-location rather than one wrong answer.
 *
 * Scope and lifetime, stated exactly:
 *
 * - **bound to** the session, the workspace and the library that the reference was issued for. A reference
 *   presented from another session, workspace or library is refused, not re-pointed.
 * - **process-local and not persisted.** A host restart, a plugin reload or a reload of this plugin's
 *   `profile patch` row empties the store. A reference from before that resolves to nothing and is reported
 *   as unknown — the honest answer. It is never resolved to "the file with the same name", because that is
 *   exactly the aliasing that turns a copy error into a citation for a different document.
 * - **bounded** by {@link MAX_REFERENCES}, oldest-insertion-first, so a long session cannot grow it without
 *   bound. A re-minted reference for a token already known to the same scope returns the existing id, so
 *   repeatedly searching the same file does not fill the store.
 * - **synchronous.** Minting and resolving contain no `await`, so two parallel calls cannot interleave.
 */
import { randomBytes } from 'node:crypto';

/** Reference ids start here and are deliberately not handle-shaped, so the two cannot be confused. */
export const REF_PREFIX = 'kref-';

/** How many live references one plugin instance keeps. */
export const MAX_REFERENCES = 500;

/** Which signed token a reference stands for. */
export type ReferenceKind = 'handle' | 'cursor';

/** What a reference resolves to. The token is the service's own, unchanged. */
export interface ReferenceTarget {
  kind: ReferenceKind;
  /** The exact signed token the service issued. Never logged, never echoed in an error. */
  token: string;
  libraryId: string;
  workspace: string;
  session: string;
  /** The source version the handle was minted against, when the result carried one. */
  sourceVersion: string | null;
}

/** The scope a lookup is made in. A mismatch is a refusal, never a re-point. */
export interface ReferenceScope {
  workspace: string;
  libraryId: string;
  session: string;
}

export type ReferenceResolution =
  | { ok: true; target: ReferenceTarget }
  | { ok: false; reason: 'unknown' | 'foreign' };

interface Entry {
  target: ReferenceTarget;
  /** The dedupe key, kept so a re-mint can find the existing id without scanning the store. */
  key: string;
}

/** Whether a value is shaped like a reference id. Shared with callers that route on it. */
export function isReferenceId(value: unknown): value is string {
  return typeof value === 'string' && /^kref-[A-Za-z0-9_-]{8,32}$/.test(value);
}

/**
 * A bounded map from short id to signed token.
 *
 * The class is deliberately tiny: one map, one insertion order. Everything interesting is in the binding
 * rules above, and a store with more mechanism would only be a store with more ways to resolve the wrong
 * thing.
 */
export class ReferenceStore {
  private readonly entries = new Map<string, Entry>();
  private readonly byKey = new Map<string, string>();
  /**
   * Continuation reference id -> the document reference id for the **same file and version**, minted in the
   * same read window.
   *
   * A read window always carries both: a handle for its file and, when the file continues, a cursor for the
   * position it stopped at. Keeping the pairing is what lets the plugin answer "you gave me a continuation
   * reference together with `startLine`" with something the caller can actually use - the document reference
   * for the same authorised file - instead of only saying no.
   */
  private readonly siblings = new Map<string, string>();

  constructor(private readonly max = MAX_REFERENCES,
              private readonly mint: () => string = () => REF_PREFIX + randomBytes(6).toString('base64url')) {}

  /** How many references are live. Diagnostics and tests only. */
  get size(): number {
    return this.entries.size;
  }

  /**
   * Remember that `continuationId` continues the same file and version as `documentId`.
   *
   * Both were minted from one read window, so this is a statement the result already made; recording it is
   * what allows a later refusal to offer the usable alternative instead of only saying "no".
   */
  linkDocument(continuationId: string, documentId: string): void {
    if (!this.entries.has(continuationId) || !this.entries.has(documentId)) return;
    this.siblings.set(continuationId, documentId);
  }

  /**
   * The document reference issued alongside a continuation reference, when there is one.
   *
   * Returns null when the continuation reference is unknown or was minted without a sibling - and that null is
   * meaningful: a refusal that cannot offer a usable alternative must say so rather than invent one.
   */
  documentSiblingOf(continuationId: string): string | null {
    const sibling = this.siblings.get(continuationId);
    if (sibling === undefined || !this.entries.has(sibling)) return null;
    return sibling;
  }

  /**
   * Mint (or re-use) a reference for one signed token in one scope.
   *
   * The scope is part of the identity, so the same handle seen from two sessions gets two references: sharing
   * one would be sharing a read permission across a boundary the plugin is supposed to keep.
   */
  issue(target: ReferenceTarget): string {
    const key = dedupeKey(target);
    const existing = this.byKey.get(key);
    if (existing !== undefined) {
      // Refresh recency: a reference that is being used is the last one that should be evicted.
      const entry = this.entries.get(existing);
      if (entry) { this.entries.delete(existing); this.entries.set(existing, entry); }
      return existing;
    }
    const id = this.uniqueId();
    this.entries.set(id, { target, key });
    this.byKey.set(key, id);
    this.evict();
    return id;
  }

  /**
   * Resolve a reference for a call made in `scope`.
   *
   * `unknown` and `foreign` are different refusals on purpose: the first means "re-locate, this pointer is
   * gone", the second means "this pointer was never yours". Neither ever falls back to a guess.
   */
  resolve(id: string, scope: ReferenceScope): ReferenceResolution {
    const entry = this.entries.get(id);
    if (!entry) return { ok: false, reason: 'unknown' };
    const target = entry.target;
    if (target.session !== scope.session || target.workspace !== scope.workspace
      || target.libraryId !== scope.libraryId) {
      return { ok: false, reason: 'foreign' };
    }
    this.entries.delete(id);
    this.entries.set(id, entry);
    return { ok: true, target };
  }

  /** Drop everything. Used when the plugin is disposed, so a reload starts from an empty, honest store. */
  clear(): void {
    this.entries.clear();
    this.byKey.clear();
  }

  /** A short id that is not already taken. The bound is a guard against a pathological generator, not a loop. */
  private uniqueId(): string {
    for (let attempt = 0; attempt < 64; attempt += 1) {
      const candidate = this.mint();
      if (!this.entries.has(candidate)) return candidate;
    }
    // Unreachable with 48 bits of entropy and 500 live entries; if it ever happens, extend rather than fail.
    let candidate = this.mint();
    while (this.entries.has(candidate)) candidate += 'x';
    return candidate;
  }

  /** Oldest-first eviction, keeping the two maps consistent. */
  private evict(): void {
    while (this.entries.size > this.max) {
      const oldest = this.entries.keys().next();
      if (oldest.done) break;
      const entry = this.entries.get(oldest.value);
      this.entries.delete(oldest.value);
      if (entry) this.byKey.delete(entry.key);
    }
  }
}

function dedupeKey(target: ReferenceTarget): string {
  return [target.session, target.workspace, target.libraryId, target.kind, target.token].join('\u0000');
}
