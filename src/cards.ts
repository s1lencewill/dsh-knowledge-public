import { formatParseQuality } from './parse-quality.js';
import type { AnswerCitationMap } from './answer-citations.js';
/**
 * Reading source cards back out of a tool result, and rendering the plain-text fallback.
 *
 * Two consumers share this module's contract:
 *
 * - the Host presenter (`presentResult` in `src/index.ts`), which has no browser and renders content
 *   blocks; and
 * - the client card (`lib/client.js`), which reads the same payload out of `tool/result`'s persisted
 *   `meta`. The client bundle cannot import this file (it is a browser lazy-CJS bundle resolving only
 *   against the shell's module table), so the payload *shape* is the interface and both sides are
 *   covered by tests: see `tests/cards.test.mjs` and `tests/client-bundle.test.mjs`.
 *
 * The payload is deliberately plain JSON - no functions, no class instances - because it is written to
 * the session log verbatim and must survive replay.
 */
import type { SourceCard, SourceLocation } from './contracts.js';

/** The one payload shape carried on `tool/result`'s `meta` for every knowledge tool. */
export interface SourcePayload {
  /** Cards in result order. Empty for a call that produced no sources. */
  sources: SourceCard[];
  answerCitations?: AnswerCitationMap;
  /** True when the tool capped its own output, so an empty tail must not read as "nothing else exists". */
  truncated?: boolean;
  /**
   * The failure record of a call the plugin stopped before doing any work. Absent on success and on a thrown
   * failure (which carries the host's structured `{name, code}` instead, because `presentationMeta` only runs
   * for a returned value). The client re-validates it before display.
   */
  failure?: unknown;
}

/** Cards of a settled tool result's canonical value, whatever tool produced it. */
export function sourcesOf(value: unknown): SourceCard[] {
  if (!value || typeof value !== 'object') return [];
  const record = value as Record<string, unknown>;
  const cards = (items: unknown): SourceCard[] => (Array.isArray(items) ? items : [])
    .map(item => (item as { sourceCard?: SourceCard } | undefined)?.sourceCard)
    .filter((card): card is SourceCard => Boolean(card));
  if (Array.isArray(record.evidence)) return cards(record.evidence);
  if (Array.isArray(record.documents)) return cards(record.documents);
  // The enumeration result names its items `entries`, and they carry the same catalogue cards: a page of a
  // library's contents must render through the same card as a single located document.
  if (Array.isArray(record.entries)) return cards(record.entries);
  if (record.sourceCard) return [record.sourceCard as SourceCard];
  return [];
}

/** The page clause of a location, or the reason there is none. A page appears here only when a map
 * vouched for it; every other case says what is missing rather than substituting the text. */
function pageClause(location: SourceLocation): string {
  if (location.kind === 'pdf-page' && location.pdfPage !== null) {
    const end = location.pdfPageEnd ?? location.pdfPage;
    const pages = end === location.pdfPage ? `PDF 第 ${location.pdfPage} 页` : `PDF 第 ${location.pdfPage}–${end} 页，跨页`;
    return `${pages}，来自抽取映射`;
  }
  switch (location.pageSource) {
    case 'map-gap': return '抽取映射未覆盖该行（映射缺口，请核对抽取）';
    case 'map-unusable': return '页码映射不可用（与当前文本/PDF 版本不匹配或已损坏），不回退到正文标记';
    case 'unmapped': return '页码映射未建立（该 PDF 尚无抽取映射）';
    case 'not-pdf': return '非 PDF 来源，只给行号';
    default: return '无页码';
  }
}

/** Where a card says its quote sits, in words. Never invents a page, and never lets a line the text
 * happens to contain stand in for one: a marker found in the body is reported as a diagnostic hint at
 * most, plainly marked as not adopted. */
export function describeLocation(card: SourceCard): string {
  const location = card.location;
  if (!location) return '位置：尚未读取，无位置';
  const lines = `抽取文本第 ${location.startLine}–${location.endLine} 行`;
  const clause = pageClause(location);
  const hint = location.pageHint !== null && location.pageHint !== undefined
    ? `；正文另有一行疑似标记称第 ${location.pageHint} 页，未采信`
    : '';
  // Only say a page is missing when it is: saying "text coordinates only" beside a resolved page would
  // contradict the page in the same sentence.
  const coordinate = location.pdfPage === null ? '，只能给抽取文本坐标' : '，行号坐标空间为抽取文本';
  return `位置：${lines}（${clause}${coordinate}${hint}）`;
}

const IDENTITY_LABEL: Record<string, string> = {
  verified: '身份已核验',
  partial: '身份部分核验',
  conflict: '身份存在冲突',
  undetermined: '身份未确定',
  'not-assessed': '身份未核验（未查目录）',
};

/** One card as plain text. Every field is data the service actually returned; nothing is computed
 * here that the card does not carry, and the support axis is echoed, never re-derived. */
export function cardText(card: SourceCard, index: number): string {
  const version = card.sourceVersion ? card.sourceVersion.slice(0, 12) : '（无）';
  const notices = card.truncationNotices.length ? card.truncationNotices.join(', ') : '无';
  const identity = card.status.identityDetail
    ? `${IDENTITY_LABEL[card.status.identity] ?? card.status.identity}（${card.status.identityDetail}）`
    : (IDENTITY_LABEL[card.status.identity] ?? card.status.identity);
  const excerpt = card.excerpt?.trim() ? card.excerpt.trim() : '（本次调用未包含摘录）';
  return [
    `来源 ${index + 1}｜${card.title ?? '（目录无标题）'}`,
    `文件 ${card.fileName ?? card.relativePath ?? '（未知）'}｜库 ${card.libraryId}`,
    describeLocation(card),
    `来源标识 ${card.citationId ?? "（未记录）"}｜结构 ${card.structureType ?? "unknown"}｜内容提示 ${(card.contentWarnings ?? []).join(", ") || "无"}`,
    `版本 ${version}｜引用来源 ${card.citationOrigin ?? '（未标注）'}｜截断 ${notices}`,
    `解析质量 ${formatParseQuality(card.parseQuality)}`,
    `状态：${identity}｜位置${card.status.locationVerified ? '已核验' : '未核验'}｜支持未评估（本工具不判定文本是否支持结论）`,
    `摘录：${excerpt}`,
  ].join('\n');
}

/** The whole payload as the Host fallback card's text. */
export function payloadText(payload: SourcePayload, title: string): string {
  if (!payload.sources.length) {
    return `${title}\n本次调用没有产生来源卡（零结果，或结果中没有可展示的来源）。零结果不等于库中没有该资料。`;
  }
  const body = payload.sources.map(cardText).join('\n\n');
  const capped = payload.truncated ? '\n\n（结果已截断：以上并非全部。）' : '';
  return `${title}\n${body}${capped}`;
}
