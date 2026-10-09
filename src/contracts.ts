import type { ParseQuality } from './parse-quality.js';
import type { TurnBudget } from './turnlimit.js';

/** Where a citation's text came from. Only the engine values are verbatim upstream excerpts; a
 * source-window is a parent-side window around the query and must not be presented as minimal. */
export type ExcerptSource = 'engine' | 'engine-fragment' | 'source-window';

/** Which coordinate a card's numbers refer to. Always the citable extracted text (`texts/`), never the
 * PDF's own text layer and never a normalised matching copy. */
export type SourceCoordinate = 'extracted-text';

/** Where a reported page came from. **Only `page-map` carries a page**, because a page is a claim about a
 * PDF's structure and only the extraction's own per-page line ranges can support it:
 *
 * - `page-map`   — the extraction's ranges, bound to this exact text/PDF version;
 * - `map-gap`    — a usable map covers no range for this line: a mapping defect, reported as one;
 * - `map-unusable` — the record claims a map that cannot be trusted (malformed, or bound to a different
 *   text/PDF version). The text is deliberately **not** consulted as a substitute;
 * - `unmapped`   — a PDF with no map: the card says the mapping has not been established;
 * - `not-pdf`    — no PDF behind the source (Markdown): line numbers only.
 *
 * A document that merely prints a line looking like the extraction's marker can never move `pdfPage`. */
export type PageSource = 'page-map' | 'map-gap' | 'map-unusable' | 'unmapped' | 'not-pdf';

/** The position of a quoted range inside one source version.
 *
 * `kind` says which coordinate to trust, so a consumer never has to infer it from a null: `pdf-page`
 * means a page is known, `text-line` means it is not and only the line range is meaningful. `pageSource`
 * says where that page came from, because "a page number" and "a trustworthy page number" are different
 * claims. */
export interface SourceLocation {
  coordinateSpace: SourceCoordinate;
  kind: 'pdf-page' | 'text-line';
  pdfPage: number | null;
  /** The page the LAST line of the range is on, when a page map resolved both ends. Equal to `pdfPage`
   * for a range inside one page; different when a read window crosses a page boundary, so the crossing is
   * visible rather than smoothed into a single number. */
  pdfPageEnd?: number | null;
  pageSource: PageSource;
  /** **Diagnostic only.** A marker the text happens to contain, for a PDF whose map is missing. It is
   * never the reported page and must never be turned into a jumpable citation. */
  pageHint?: number | null;
  startLine: number;
  endLine: number;
  /** 0-based half-open Unicode code point offsets. A JS consumer converts via scripts/excerpt-offsets.mjs. */
  startChar?: number;
  endChar?: number;
}

/** The three axes a card reports separately. They are never folded into one score, and the last two
 * answer different questions: *where* text came from versus *whether it says* what a reader concludes. */
export interface SourceStatus {
  /** Does the identity catalogue/locator agree this is the document it claims to be? `not-assessed`
   * when no catalogue record was consulted (a search citation is verified by the parent, not the
   * librarian). */
  identity: 'verified' | 'partial' | 'conflict' | 'undetermined' | 'not-assessed';
  identityDetail?: string | null;
  /** Do the returned offsets rebuild the excerpt, verbatim, in the named source version? Measured by
   * the parent, never assumed. This is not a statement about the document's content. */
  locationVerified: boolean;
  /** Whether the text supports the conclusion. **Never derived by the plugin or the service**: locating
   * a quotation proves where text came from, not what it supports. Always this literal. */
  support: 'not-assessed';
}

/** Everything a UI needs to display one source without re-deriving it. Presentation only: a card adds
 * no evidentiary claim and never upgrades `status.support`. */
export interface SourceCard {
  citationId?: string;
  structureType?: 'metadata' | 'body' | 'code' | 'references' | 'unknown';
  bodyStartLine?: number | null;
  fallbackReason?: string;
  contentWarnings?: string[];
  libraryId: string;
  title: string | null;
  fileName: string | null;
  excerpt: string | null;
  location: SourceLocation | null;
  sourceVersion: string | null;
  citationOrigin: ExcerptSource | 'read' | 'catalogue' | null;
  truncated: boolean;
  truncationNotices: string[];
  parseQuality: ParseQuality;
  parseQualityDegraded?: boolean;
  status: SourceStatus;
  documentId?: string;
  /** Library-relative name of the citable text file. Never an absolute path. */
  relativePath?: string;
  conflicts?: unknown[];
}

export interface Evidence {
  id: string;
  libraryId: string;
  relativePath: string;
  sourceVersion: string;
  startLine: number;
  endLine: number;
  excerpt: string;
  excerptSource?: ExcerptSource;
  /**
   * **What the model copies.** A short, opaque, process-local reference the plugin resolves back to the
   * service-signed identifier. Present on every excerpt whose file the service could verify for a read.
   */
  readRef?: string;
  /** The service-signed identifier behind {@link readRef}. The plugin strips it from the model-visible
   * result: it is 300 characters of transcription risk, and the model has no use for it. It remains part of
   * this type because legacy callers may still hold one and `knowledge_read` still accepts it. */
  fileHandle?: string;
  /** Does the quote end a statement, or stop inside a hyphenated word? A boundary check only - it says
   * nothing about whether the excerpt carries every condition, unit or the answer itself. */
  excerptComplete?: boolean;
  /** Where the quote sits, in the extraction's own coordinates. */
  location?: SourceLocation;
  /** Whether the offsets rebuild the excerpt in this exact source version. */
  locationVerified?: boolean;
  sourceCard?: SourceCard;
}
export interface QueryResult {
  status: 'complete' | 'partial' | 'failed' | 'cancelled';
  answer: string;
  evidence: Evidence[];
  warnings: string[];
  /** The per-turn accounting this call was made under. Added by the plugin; never by the service. */
  budget?: TurnBudget;
}

/** A bounded read of a file a citation points at. The identifier comes from a prior search result;
 * the cursor comes from the previous read of the same file. Offsets are Unicode code point offsets
 * into BOM-stripped, CRLF-folded text: `text.slice` needs `scripts/excerpt-offsets.mjs` first. */
export interface ReadRequest {
  libraryId: string;
  /** The short reference from a previous result. Resolved by the plugin; never sent to the service. */
  ref?: string;
  fileHandle?: string;
  cursor?: string;
  sourceVersion?: string;
  startLine?: number;
  maxChars?: number;
  maxLines?: number;
  overlapLines?: number;
}
export interface ReadWindow {
  libraryId: string;
  relativePath: string;
  /** The service-signed identifier. Stripped from the model-visible result in favour of `readRef`. */
  fileHandle: string;
  /** What the model copies back for a follow-up read of this same file. */
  readRef?: string;
  sourceVersion: string;
  startLine: number;
  endLine: number;
  startChar: number;
  endChar: number;
  startOffset: number;
  endOffset: number;
  excerpt: string;
  excerptComplete: boolean;
  endsAtLineBoundary: boolean;
  midLineStart: boolean;
  midLineStop: boolean;
  lineLimitReached: boolean;
  overlapsPrevious: boolean;
  hasMore: boolean;
  nextStartChar: number | null;
  nextCursor: string | null;
  /** The short reference that stands for `nextCursor`. What the model copies to continue. */
  nextRef?: string | null;
  fileChars: number;
  fileLines: number;
  warnings: string[];
  limits: { readsUsed: number; readsLimit: number; sessionCharsUsed: number; sessionCharsLimit: number; sessionCharsRemaining: number };
  /** The extraction's own page marker at or above `startLine`, or null when the source has none. */
  pdfPage?: number | null;
  /** Whether the offsets rebuild this window's excerpt out of the verified bytes. */
  locationVerified?: boolean;
  sourceCard?: SourceCard;
  /** The per-turn accounting this call was made under. Added by the plugin; never by the service. */
  budget?: TurnBudget;
}
export interface Library { id: string; name: string }

/** A clue for the local document locator. At least one field is required. */
export interface DocumentQuery {
  doi?: string;
  title?: string;
  authors?: string;
  year?: string;
  fileName?: string;
  limit?: number;
}

/** One located document: identity, catalogue provenance and - when it verified - a read handle.
 * `readable: false` means the catalogue pointed at something the library refused; the refusal code
 * says why. A hit only locates a file: it is not evidence for a content question. */
export interface DocumentCandidate {
  documentId: string;
  title: string | null;
  authors: string | null;
  year: string | null;
  doi: string | null;
  fileName: string;
  textFile: string;
  pages?: number;
  verification: 'verified' | 'partial' | 'conflict' | 'undetermined';
  /** The extraction's own parse-quality mark from the identity catalogue, when one was recorded. */
  quality?: string | null;
  conflicts: unknown[];
  score: number;
  matched: string[];
  mismatched: string[];
  readable: boolean;
  /** The short reference to hand to `knowledge_read`, when the entry verified as readable. */
  readRef?: string;
  /** The service-signed identifier behind `readRef`. Stripped from the model-visible result. */
  fileHandle?: string;
  sourceVersion?: string;
  refusal?: string;
  refusalDetail?: string;
  sourceCard?: SourceCard;
}

export interface DocumentFindResult {
  libraryId: string;
  documents: DocumentCandidate[];
  warnings: string[];
  note: string;
  budget?: TurnBudget;
}

/** How one page of the catalogue was asked for. Paging is over a deterministic order, never a keyword. */
export interface CatalogueQuery {
  offset?: number;
  limit?: number;
  /** The version the previous page reported. Supplying it is what turns a silent skip into a refusal. */
  catalogueVersion?: string;
}

/** One catalogue entry. Identity and quality only: a catalogue entry is not a read authorisation, so the
 * handle it may carry was re-verified against the library at the moment this page was built. */
export interface CatalogueEntry {
  /** Position in the full deterministic order, so a page can be reassembled without re-sorting. */
  index: number;
  documentId: string;
  title: string | null;
  authors: string | null;
  year: string | null;
  doi: string | null;
  fileName: string;
  textFile: string;
  pages?: number;
  /** The extraction's own parse-quality mark, when the catalogue recorded one. */
  quality?: string | null;
  verification: 'verified' | 'partial' | 'conflict' | 'undetermined';
  conflicts: unknown[];
  /** Whether this entry verified against the library on the service **just now**. */
  readable: boolean;
  readRef?: string;
  sourceVersion?: string;
  refusal?: string;
  sourceCard?: SourceCard;
}

export interface CataloguePage {
  libraryId: string;
  /** Digest of the catalogue this page was taken from. Send it back on the next page. */
  catalogueVersion: string;
  /** `ok` for a page; `catalogue-changed` when the catalogue moved under a paged read (see below). */
  status: 'ok' | 'catalogue-changed';
  total: number;
  offset: number;
  limit: number;
  returned: number;
  hasMore: boolean;
  nextOffset: number | null;
  entries: CatalogueEntry[];
  warnings: string[];
  note: string;
  budget?: TurnBudget;
}
