/**
 * The failure taxonomy: one stable code per thing that can go wrong, and no third-party text in a card.
 *
 * Why this exists. Until now a failed call produced **nothing on screen**. The host renders one keyed
 * `tool.call.toolview` entry per tool name (see `docs/m0-host-failure-cards.md`), so for a tool whose card
 * this plugin owns, returning `null` from that card means the row renders empty - there is no generic
 * fallback behind a hit. A refused read looked like a call that had not happened yet ("本次调用尚未读取，无位置信息"),
 * which is a different and wrong statement.
 *
 * Two rules make the card safe to show:
 *
 * 1. **Display text never comes from an exception.** `FAILURE_ADVICE` is a fixed table keyed by code; the
 *    card resolves its category, reason and action from the table in its own copy of it. A third-party
 *    message, a path, a handle or a request body therefore has no route into the card, because nothing on
 *    the display path ever reads one.
 * 2. **The code is structured, not parsed.** A failure is thrown as a `HarnessError`, whose `code` the host
 *    records as `{name, code}` on the result node; the client reads that field. Classification happens here,
 *    once, where the plugin still knows what it caught.
 *
 * The model-facing sentence is built here too, so a raw upstream message is not forwarded to the model
 * either. `detail` is optional and allow-listed to identifier characters, so it cannot carry a path, a
 * multi-line trace, or a handle.
 */

/** Every failure class this plugin can report. The client's copy of this list is asserted equal by test. */
export const FAILURE_CODES = [
  // Plugin-side refusals: the plugin decided, before any request.
  'KNOWLEDGE_LIBRARY_NOT_SELECTED',
  'KNOWLEDGE_REPEAT_REFUSED',
  'KNOWLEDGE_TURN_LIMIT_REACHED',
  'KNOWLEDGE_NOT_CONFIGURED',
  'KNOWLEDGE_PARAMETER_OUT_OF_RANGE',
  'KNOWLEDGE_READ_REF_UNKNOWN',
  'KNOWLEDGE_READ_REF_FOREIGN',
  // Transport.
  'KNOWLEDGE_SERVICE_UNREACHABLE',
  'KNOWLEDGE_SERVICE_TIMEOUT',
  'KNOWLEDGE_SERVICE_UNAUTHORIZED',
  'KNOWLEDGE_SERVICE_UNAVAILABLE',
  'KNOWLEDGE_REQUEST_CANCELLED',
  // Service refusals that need a specific user action.
  'KNOWLEDGE_SOURCE_CHANGED',
  'KNOWLEDGE_SOURCE_UNAVAILABLE',
  // The presented identifier is not one this service ever issued for this workspace and library. Split out of
  // KNOWLEDGE_SOURCE_UNAVAILABLE on purpose: "you did not copy the identifier correctly, locate again" and
  // "the file behind a valid identifier is gone" are different facts and used to read as the same one, which
  // is how a copy error came to look like a deleted document.
  'KNOWLEDGE_HANDLE_INVALID',
  'KNOWLEDGE_QUOTA_EXHAUSTED',
  // A 429 this build could **not** attribute to a known bound. Deliberately not the quota code: the reason was
  // never identified, and calling it "your quota is spent" both invents a cause and prescribes the wrong
  // recovery (a provider budget needs a new window or a config change; a read limit needs a new session).
  'KNOWLEDGE_RATE_LIMITED',
  'KNOWLEDGE_QUERY_BUSY',
  'KNOWLEDGE_QUERY_NOT_TERMINAL',
  'KNOWLEDGE_QUERY_RESULT_RELEASED',
  'KNOWLEDGE_REQUEST_REJECTED',
  // A refused read that names *which* part of the request was wrong and what the bound was. Split out of
  // KNOWLEDGE_REQUEST_REJECTED because the generic sentence ("check the library id and the parameters") is
  // not actionable when the real fact is "this file has 12 lines and you asked for line 480": a model that
  // receives the generic text cannot tell a request-shape mistake from a missing library, and the observed
  // session guessed "the ref must be stale" and gave up on a file it could have read. The two codes below
  // are the only failures whose message names numbers, and both numbers come from the service's validated
  // facts - never from the sentence the service wrote.
  'KNOWLEDGE_READ_RANGE_INVALID',
  'KNOWLEDGE_READ_REQUEST_INVALID',
  // The caller gave two different instructions about where to read: a continuation reference (which is bound to
  // a position) together with `startLine` (which asks for another one). The service resolves that by silently
  // preferring the cursor and warning; the plugin resolves it by refusing, because the conflict is decidable
  // from the reference's own kind before any request is made - and the recorded session spent three calls and
  // a whole step discovering that its `startLine` was being ignored.
  'KNOWLEDGE_REF_KIND_CONFLICT',
  // Nothing above matched. Still gets a card, a generic statement and a correlation id.
  'KNOWLEDGE_UNKNOWN',
] as const;

export type FailureCode = (typeof FAILURE_CODES)[number];

export interface FailureAdvice {
  /** Fixed, user-visible category label. */
  category: string;
  /** Fixed, one-sentence reason. No interpolation, so no upstream text can reach it. */
  reason: string;
  /** Fixed next step the user (or the main Agent) can take. */
  action: string;
}

/**
 * The display table. The browser half carries the same table; `tests/failure-cards.test.mjs` extracts both
 * and asserts they agree, because the two halves cannot import from each other (the client bundle resolves
 * only the shell's module table).
 */
export const FAILURE_ADVICE: Record<FailureCode, FailureAdvice> = {
  KNOWLEDGE_LIBRARY_NOT_SELECTED: {
    category: '库范围不匹配',
    reason: '这次调用请求的知识库与当前会话选定的知识库不一致，插件在发出请求前就拒绝了它。',
    action: '用 /knowledge-use <库 ID> 把会话切到该库，或让调用改用当前会话已选定的库。',
  },
  KNOWLEDGE_REPEAT_REFUSED: {
    category: '重复失败已熔断',
    reason: '同一调用在连续失败后被插件停止，插件不再为它做任何工作。',
    action: '结束本轮并说明已知与未知，不要重复同一调用；换参数或换库都不会让同一个失败变成成功。',
  },
  KNOWLEDGE_TURN_LIMIT_REACHED: {
    category: '本轮工具调用已达上限',
    reason: '本轮对知识库工具的调用次数达到了插件设定的上限，后续调用不再发出请求、也不启动容器。',
    action: '用现有结果作答并说明缺口；需要继续检索时，在**新的一轮**里再发起。',
  },
  KNOWLEDGE_NOT_CONFIGURED: {
    category: '知识库服务未配置',
    reason: '插件没有拿到可用的服务地址或访问令牌，因此无法发起调用。',
    // The variable names live in the deployment docs, not here: the browser half carries this same table and
    // must not contain a credential identifier, which `tests/client-bundle.test.mjs` enforces.
    action: '按部署说明设置回环服务地址与访问令牌（两者都由部署侧的环境提供，面板不持有它们），然后重启宿主。',
  },
  KNOWLEDGE_PARAMETER_OUT_OF_RANGE: {
    category: '参数超出允许范围',
    reason: '这次调用的某个参数超出了插件声明的取值范围，插件在发出请求前就拒绝了它，服务没有收到任何内容。',
    action: '按工具说明中给出的整数范围修正该参数后重试；插件不会把越界值静默改成边界值。',
  },
  KNOWLEDGE_READ_REF_UNKNOWN: {
    category: '读取引用已失效',
    reason: '这个短引用在当前插件实例里找不到对应项——引用是进程内、有界的，宿主或插件重载、条目被淘汰、或者它本来就不是本插件签发的，都会是这个结果。',
    action: '重新用 knowledge_find_documents 或 knowledge_search 定位该文件以取得新引用；不要凭记忆改写引用，它不会解析成别的文件。',
  },
  KNOWLEDGE_READ_REF_FOREIGN: {
    category: '读取引用不属于本会话',
    reason: '这个短引用是为另一个会话、工作区或知识库签发的，插件不会把它重定向到当前库里的同名文件。',
    action: '在本次会话范围内重新定位该文件；跨会话共享引用是不被支持的。',
  },
  KNOWLEDGE_SERVICE_UNREACHABLE: {
    category: '知识库服务不可达',
    reason: '插件无法连接到知识库服务，请求没有送达。',
    action: '请在知识库面板启动服务，确认可用后继续。',
  },
  KNOWLEDGE_SERVICE_TIMEOUT: {
    category: '知识库服务超时',
    reason: '知识库服务在超时时间内没有返回结果，插件已放弃这次调用。',
    action: '稍后重试；若反复超时，检查服务负载或改用更小的检索范围。',
  },
  KNOWLEDGE_SERVICE_UNAUTHORIZED: {
    category: '知识库服务拒绝鉴权',
    reason: '知识库服务拒绝了随请求发送的凭据。',
    action: '核对部署侧配置的访问令牌与服务端配置是否一致，然后重启宿主。',
  },
  KNOWLEDGE_SERVICE_UNAVAILABLE: {
    category: '知识库服务内部错误',
    reason: '知识库服务返回了服务端错误，这次调用没有结果。',
    action: '查看服务端日志后重试；这不代表库中没有相关资料。',
  },
  KNOWLEDGE_REQUEST_CANCELLED: {
    category: '调用已取消',
    reason: '调用在完成前被取消（通常是本轮被停止）。',
    action: '需要该结果时重新发起调用。',
  },
  KNOWLEDGE_SOURCE_CHANGED: {
    category: '原文已变更',
    reason: '这个文件自标识签发之后已经改变，插件不会用旧标识读取新内容。',
    action: '重新检索以获得新的标识，再读取；不要沿用旧标识。',
  },
  KNOWLEDGE_HANDLE_INVALID: {
    category: '文件标识无效',
    reason: '随调用提交的文件标识不是本服务为该工作区与该库签发过的标识（被改写、被截断，或属于另一个工作区/库）；服务按签名拒绝，因此没有读取任何内容。',
    action: '重新定位或检索该文件以取得新的标识/短引用；这次失败不代表文件已删除或库中没有该资料。',
  },
  KNOWLEDGE_SOURCE_UNAVAILABLE: {
    category: '原文不可用',
    reason: '标识本身有效，但它指向的原文文件当前不可读（已移出库、被替换为链接、超过大小上限或不可访问）。',
    action: '确认该文件仍在库内且未被移动；需要时重建目录后再定位。',
  },
  KNOWLEDGE_QUOTA_EXHAUSTED: {
    category: '额度已用尽',
    reason: '这次调用被额度限制拒绝：服务报告的额度已经用尽（读取会话额度、提供方预算或结果保留容量），具体是其中哪一项由下方说明里的服务码指出。',
    action: '先看说明里的服务码：读取额度到顶时等到新会话或提高限额；提供方预算到顶时必须等配置或新窗口，短暂等待不会恢复。不要针对目录或读取的限额说"本轮其余调用都会失败"，那与搜索锁无关。',
  },
  // The honest sibling of the code above, and the split the review asked for: an older service (or one this
  // build does not recognise) sends a bare 429, and the reason is *not* identified. Naming it as a quota would
  // turn "the service is rate limiting for a reason it did not state" into "your budget is spent", which is a
  // claim this plugin cannot support and a next step that may be wrong.
  KNOWLEDGE_RATE_LIMITED: {
    category: '请求被限流（原因未识别）',
    reason: '服务以 HTTP 429 拒绝了这次调用，但**没有**给出本插件能识别的机器码，因此具体原因未识别：既不确定是并发占用，也不确定是读取额度或提供方额度。',
    action: '如实报告"被限流且原因未识别"，等待状态变化后由用户决定是否重试；不要换 FAST/DEEP、不要用 shell 等待、不要据此断定库中没有资料。若服务是新版本，卡片上的服务码会指出具体类别。',
  },
  // Split out of KNOWLEDGE_QUOTA_EXHAUSTED because the action is the opposite one: a busy service is
  // *working*, and the recorded session's response to it - switching DEEP to FAST and re-sending - spends
  // the turn on the same refusal, because a second mode does not unlock a single-query service.
  KNOWLEDGE_QUERY_NOT_TERMINAL: {
    category: '作业尚未收口', reason: '作业或本地写入者尚未结束，不能确认终态或释放占用。',
    action: '保留当前作业并查询状态；需要停止时使用取消入口，不要立即重送查询。',
  },
  KNOWLEDGE_QUERY_RESULT_RELEASED: {
    category: '结果已释放', reason: '这次逻辑提交已执行，结果已确认或租约到期释放；重送不会再次执行。',
    action: '查看已保存的结果；确需新查询时发起新的提交，不要重用旧 requestId。',
  },
  KNOWLEDGE_QUERY_BUSY: {
    category: '当前已有查询在运行',
    reason: '服务一次只允许一个查询在运行，因此这次提交没有被接纳；这是并发占用，不是额度耗尽，也不是库中没有资料。',
    action: '不要换模式（FAST/DEEP）重发，也不要靠 shell 等待：占用不会因此解除。按下方说明里的作业状态有界地再查一次，或等待正在运行的查询结束后再提交。',
  },
  KNOWLEDGE_REQUEST_REJECTED: {
    category: '请求被拒绝',
    reason: '知识库服务拒绝了这次请求的参数或目标（例如库不存在或请求不合法）。',
    action: '核对该调用的库 ID 与参数，然后重试。',
  },
  KNOWLEDGE_READ_RANGE_INVALID: {
    category: '读取范围超出文件',
    reason: '这次读取请求的起始行超出了该文件的实际行数，服务在读取内容前拒绝了它，没有返回任何正文。',
    action: '按下方给出的文件行数改小 startLine（1 基，且不超过该行数）后重试一次；也可以用上一条结果里的短引用续读，不必猜行号。',
  },
  KNOWLEDGE_READ_REQUEST_INVALID: {
    category: '读取请求参数不合法',
    reason: '这次读取请求的标识或参数不构成一次合法的读取，服务在打开文件前拒绝了它。',
    action: '按下方给出的固定原因修正参数：线号问题改 startLine；标识问题改用刚返回的短引用重新定位。',
  },
  KNOWLEDGE_REF_KIND_CONFLICT: {
    category: '引用类型与定位参数冲突',
    reason: '这次调用同时给了"续读引用"（nextRef，绑定到窗口结束位置）和 startLine（要求从另一行开始），两者是互相矛盾的指令，插件在发出请求前就拒绝了它。',
    action: '要接着上次的位置读，就去掉 startLine；要跳到某一行，就用同一文件的"文档引用"（readRef，卡片上会给出可用的那个）。',
  },
  KNOWLEDGE_UNKNOWN: {
    category: '未知错误',
    reason: '这次调用以插件没有归类的错误结束，因此不提供具体原因。',
    action: '把下面的关联 ID 提供给维护者，并附上复现步骤。',
  },
};

/**
 * What a failure record may say about itself: typed fields, not text.
 *
 * The previous version allowed a free-form `detail` string and tried to keep secrets out with a character
 * allow-list and a segment-length rule. That is the wrong shape of defence: the rule has to *predict* every
 * dangerous string, and it cannot - a short key with separators in it (`abcd-efgh-ijkl`) passes any such
 * rule, and a rule tight enough to catch it would reject real library ids. There is no filter here at all.
 *
 * Instead every field is one of:
 *
 * - a **closed enum** this plugin declares (`layer`, `serviceCode`, `tool`, `chain`) - membership is an exact
 *   lookup in a fixed set, so an upstream string that is not in the set is not "filtered out", it has no
 *   field to land in;
 * - a **number** (`httpStatus`, `occurrences`, `used`, `max`), which cannot carry text; or
 * - the **system-generated correlation id** (`reference`).
 *
 * Nothing else can be expressed, so no exception message, path, URL, handle, key or request body can reach a
 * card, a tool result or a diagnostic - not because it is stripped, but because there is nowhere to put it.
 */
export type FailureLayer = 'plugin' | 'transport' | 'service' | 'host';

/** The service codes this plugin recognises. Membership is the check; there is no pattern to slip past. */
export const KNOWN_SERVICE_CODES = [
  'SOURCE_VERSION_CHANGED',
  'READ_SOURCE_VERSION_MISMATCH',
  'READ_HANDLE_INVALID',
  'READ_HANDLE_FOREIGN',
  'READ_FILE_UNAVAILABLE',
  'READ_REQUEST_INVALID',
  'READ_RANGE_INVALID',
  'READ_LIMIT_EXCEEDED',
  'RESULT_CAPACITY',
  'CATALOG_UNAVAILABLE',
  'CATALOG_UNREADABLE',
  'CATALOGUE_REQUEST_INVALID',
  'CATALOGUE_CHANGED',
  'DOCUMENT_NOT_READABLE',
  'DOCUMENT_CHANGED',
  'NO_CATALOG_MATCH',
  'UNKNOWN_LIBRARY',
  // The query lifecycle's own codes. Each one is a fact a `429` used to hide: "a query is already running",
  // "the result store is full", "the provider budget is spent" and "this session's read budget is spent" are
  // four different answers with four different next steps, and collapsing them into one "quota" code is what
  // made a busy service read as an exhausted library. `UNKNOWN_RATE_LIMIT` is the honest default for an older
  // service that sends a bare 429: the reason is *not* identified, so none is invented.
  'QUERY_BUSY',
  'PROVIDER_QUOTA_EXHAUSTED',
  'UNKNOWN_RATE_LIMIT',
  // Worker outcomes. `ISOLATION_UNAVAILABLE` is only for the isolation setup itself; a container that started,
  // ran and produced nothing is a worker failure and now says so instead of borrowing the isolation code.
  'WORKER_NO_RESULT',
  'WORKER_EXITED',
  'ISOLATION_UNAVAILABLE',
  'QUERY_REQUEST_CONFLICT',
  'QUERY_REQUEST_CAPACITY',
  'QUERY_RESULT_RELEASED',
  'QUERY_NOT_TERMINAL',
  'QUERY_REQUEST_INVALID',
  'REAL_DATA_UNAUTHORIZED',
] as const;

export type ServiceCode = (typeof KNOWN_SERVICE_CODES)[number];

/**
 * Every parameter name this plugin declares, across all of its tools.
 *
 * A closed set for the same reason the tool-name set is one: a refusal that has to name the offending
 * parameter needs a field that can hold *only* a parameter this plugin's own schema declares. A request value,
 * a path or an upstream word has nowhere to land.
 */
export const FAILURE_PARAMETERS = [
  'libraryId', 'query', 'mode',
  'doi', 'title', 'authors', 'year', 'fileName', 'limit', 'offset', 'catalogueVersion',
  'ref', 'fileHandle', 'cursor', 'sourceVersion',
  'startLine', 'maxChars', 'maxLines', 'overlapLines',
] as const;

export type FailureParameter = (typeof FAILURE_PARAMETERS)[number];

/**
 * The fixed reasons a recoverable read refusal can carry, and the fixed next steps.
 *
 * The **service-sourced** members mirror `engine/reader.py`'s `READ_REASONS` / `READ_ACTIONS` exactly: the
 * model is told *which* fact applied, and a member of this union is the only thing the field can hold, so a
 * service that answered with a novel string would have its value dropped rather than forwarded.
 *
 * {@link PLUGIN_REF_REASONS} / {@link PLUGIN_REF_ACTIONS} are the other half: facts the plugin decides on its
 * own, before any request (today, exactly one: the caller combined a continuation reference with a start
 * line). They are declared separately so the cross-check against the engine stays meaningful - the engine has
 * no notion of reference kinds, and padding its list with a member it can never send would weaken the check
 * instead of extending it.
 */
export const READ_REFUSAL_REASONS = [
  'START_LINE_INVALID',
  'START_LINE_PAST_END',
  'START_OUTSIDE_FILE',
  'CURSOR_OUTSIDE_FILE',
  'IDENTIFIER_MISSING',
  'IDENTIFIER_CONFLICT',
  'PARAMETER_TYPE_INVALID',
] as const;

export const PLUGIN_REF_REASONS = ['CONTINUATION_REF_WITH_START_LINE'] as const;

export type ReadRefusalReason = (typeof READ_REFUSAL_REASONS)[number] | (typeof PLUGIN_REF_REASONS)[number];

export const READ_SUGGESTED_ACTIONS = [
  'CORRECT_START_LINE',
  'LOCATE_AGAIN',
  'CORRECT_PARAMETER',
] as const;

export const PLUGIN_REF_ACTIONS = ['USE_DOCUMENT_REF', 'DROP_START_LINE'] as const;

export type ReadSuggestedAction = (typeof READ_SUGGESTED_ACTIONS)[number] | (typeof PLUGIN_REF_ACTIONS)[number];

/**
 * The structured half of one refused read, after validation.
 *
 * Every field has been through {@link readRefusalOf}: the two tokens are members of the unions above, the two
 * numbers are safe integers or null, and the correlation id matched the reference shape. There is
 * deliberately **no** prose field, so the service's own sentence - which can quote a relative path or a
 * handle - has nowhere to land, exactly as with {@link FailureDetail}.
 */
export interface ReadRefusal {
  reason: ReadRefusalReason | null;
  requestedStartLine: number | null;
  fileLines: number | null;
  suggestedAction: ReadSuggestedAction | null;
  /** The service's correlation id when it matched the reference shape, else null. */
  correlationId: string | null;
  /** The route field a schema refusal named, when it named one. A member of the read route's own schema. */
  parameter: FailureParameter | null;
  /**
   * A short reference the caller can use instead, when the refusal has one to offer.
   *
   * Today it is the document reference for the same file that a continuation reference was issued beside, and
   * it is the one field here that is not a number or a set member. It is safe in the same way the correlation
   * id is: an opaque plugin-local id that must match the reference shape to be kept at all, so nothing else -
   * no path, no token, no sentence - can be carried in this slot.
   */
  suggestedRef: string | null;
}

/** The tools that can produce a knowledge failure. A closed set, so a tool name is never free text. */
export const FAILURE_TOOLS = [
  'knowledge_list_libraries',
  'knowledge_search',
  'knowledge_find_documents',
  'knowledge_list_documents',
  'knowledge_read',
] as const;

export type FailureTool = (typeof FAILURE_TOOLS)[number];

export interface FailureDetail {
  layer: FailureLayer;
  /** A number, or null. Never a status line or a reason phrase. */
  httpStatus: number | null;
  /** A member of {@link KNOWN_SERVICE_CODES}, or null when the service named nothing this build knows. */
  serviceCode: ServiceCode | null;
  tool: FailureTool | null;
  /** A member of {@link FAILURE_PARAMETERS}, or null. Never the value that was sent. */
  parameter: FailureParameter | null;
  chain: 'identical' | 'consecutive' | null;
  /** How many consecutive failures that chain had reached. */
  occurrences: number | null;
  /** Per-turn ceiling accounting: calls counted for the turn, and the ceiling. */
  used: number | null;
  max: number | null;
  /**
   * What the turn had actually obtained when it was stopped: verified excerpts, catalogue documents and read
   * windows returned by dispatched calls so far. Numbers, so a deterministic closing statement can name the
   * material that exists without inventing any of it. `reads` is separate from `evidence` because a read window
   * is a range of the original, not a verified quotation, and the two are never added together.
   */
  evidence: number | null;
  documents: number | null;
  reads: number | null;
  /**
   * The recoverable-read facts, when the failure was one of the two read-shape refusals.
   *
   * They live here, as members of closed sets and as numbers, rather than in a message: a card and a
   * model-facing sentence can then both say "the file has 12 lines" without either of them parsing prose.
   */
  reason: ReadRefusalReason | null;
  requestedStartLine: number | null;
  fileLines: number | null;
  suggestedAction: ReadSuggestedAction | null;
  /**
   * A short reference to use instead, when the refusal has one to offer. A plugin-local opaque id (see
   * {@link ReadRefusal.suggestedRef}), validated by shape here for the same reason the correlation id is.
   */
  suggestedRef: string | null;
  /**
   * What this client observed about its **own** submission, when the failure happened while submitting or
   * polling a query.
   *
   * This is the fact that turns "the request timed out" into an answerable state: the id the job was
   * submitted under, the phase the client had reached, and whichever state the service reported. Every field
   * is a closed-set member, the client's own id, or a boolean, and all of them are re-validated on read, so
   * the field can say *which* submission is unresolved without becoming a place for text to travel.
   */
  query: QueryLifecycleFacts | null;
}

/** The lifecycle facts of one submission, reduced to closed-set members and the client's own request id. */
export interface QueryLifecycleFacts {
  /** `kq-...`, the id the client minted before sending. Validated by shape. */
  requestId: string | null;
  phase: QueryPhaseName | null;
  /** A state token the **service** reported, kept only as a member of the declared job states. */
  jobState: JobState | null;
  /** Whether the client's cancellation was confirmed. `null` means it never got an answer. */
  cancellationConfirmed: boolean | null;
}

export const QUERY_PHASES = ['submitting', 'accepted', 'polling', 'cancelling', 'completed', 'failed',
  'cancelled', 'unknown'] as const;
export type QueryPhaseName = (typeof QUERY_PHASES)[number];

/** The job states the service declares. A state the service did not name is null, never displayed raw. */
export const JOB_STATES = ['received', 'accepted', 'running', 'cancelling', 'completed', 'failed',
  'cancelled', 'unknown'] as const;
export type JobState = (typeof JOB_STATES)[number];

const EMPTY_DETAIL: FailureDetail = {
  layer: 'plugin', httpStatus: null, serviceCode: null, tool: null, parameter: null,
  chain: null, occurrences: null, used: null, max: null, evidence: null, documents: null, reads: null,
  reason: null, requestedStartLine: null, fileLines: null, suggestedAction: null, suggestedRef: null,
  query: null,
};

/** Keep only a finite integer, so a NaN or an object cannot masquerade as a number. */
function safeCount(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) ? value : null;
}

/** Keep only a non-negative safe integer. A line number or a line count is never negative. */
function nonNegative(value: unknown): number | null {
  const count = safeCount(value);
  return count !== null && count >= 0 ? count : null;
}

/** Membership in the declared sets, or null. This is the whole of the "filtering". */
export function knownServiceCode(value: unknown): ServiceCode | null {
  return typeof value === 'string' && (KNOWN_SERVICE_CODES as readonly string[]).includes(value)
    ? value as ServiceCode : null;
}

export function knownTool(value: unknown): FailureTool | null {
  return typeof value === 'string' && (FAILURE_TOOLS as readonly string[]).includes(value)
    ? value as FailureTool : null;
}

export function knownParameter(value: unknown): FailureParameter | null {
  return typeof value === 'string' && (FAILURE_PARAMETERS as readonly string[]).includes(value)
    ? value as FailureParameter : null;
}

export function knownReadReason(value: unknown): ReadRefusalReason | null {
  if (typeof value !== 'string') return null;
  return (READ_REFUSAL_REASONS as readonly string[]).includes(value)
    || (PLUGIN_REF_REASONS as readonly string[]).includes(value) ? value as ReadRefusalReason : null;
}

export function knownReadAction(value: unknown): ReadSuggestedAction | null {
  if (typeof value !== 'string') return null;
  return (READ_SUGGESTED_ACTIONS as readonly string[]).includes(value)
    || (PLUGIN_REF_ACTIONS as readonly string[]).includes(value) ? value as ReadSuggestedAction : null;
}

/**
 * Validate the structured half of a service refusal, keeping only what is decidable here.
 *
 * The whole point is that this runs on a body the service wrote, and returns a value with no free text in it:
 * an unlisted reason becomes null, a non-integer line count becomes null, a correlation id that is not in the
 * reference shape becomes null. The sentence that arrived beside these fields is never read, so a service -
 * or anything that can answer on its port - cannot put text on a card by sending it a field this build does
 * not declare.
 */
export function readRefusalOf(value: unknown): ReadRefusal | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const reason = knownReadReason(record.reason);
  const suggestedAction = knownReadAction(record.suggestedAction);
  const requestedStartLine = nonNegative(record.requestedStartLine);
  const fileLines = nonNegative(record.fileLines);
  const correlationId = isReferenceId(record.correlationId) ? String(record.correlationId) : null;
  const parameter = knownParameter(record.parameter);
  // `suggestedRef` is deliberately **not** read here. This function parses a body the *service* wrote, and the
  // service has no references - they are the plugin's own. Leaving it null keeps the two sources apart: a
  // service cannot put a reference-shaped value into a card by inventing a field.
  const suggestedRef = null;
  // A body with none of the declared fields is not a structured refusal; returning null keeps the caller on
  // the older path (the code parsed out of `detail`) instead of fabricating an all-null structure.
  if (reason === null && suggestedAction === null && requestedStartLine === null && fileLines === null
    && correlationId === null && parameter === null) return null;
  return { reason, requestedStartLine, fileLines, suggestedAction, correlationId, parameter, suggestedRef };
}

/**
 * Untrusted input to {@link failureDetail}: every field is `unknown` on purpose.
 *
 * The signature says what the function does. It does not take a `FailureDetail` and check it; it takes
 * arbitrary values and returns a detail whose every field is either a declared member or null. A caller
 * cannot pass text through it by telling the type system it is something else.
 */
export interface FailureDetailInput {
  layer?: unknown;
  httpStatus?: unknown;
  serviceCode?: unknown;
  tool?: unknown;
  parameter?: unknown;
  chain?: unknown;
  occurrences?: unknown;
  used?: unknown;
  max?: unknown;
  evidence?: unknown;
  documents?: unknown;
  reads?: unknown;
  reason?: unknown;
  requestedStartLine?: unknown;
  fileLines?: unknown;
  suggestedAction?: unknown;
  suggestedRef?: unknown;
  query?: unknown;
}

/** The shape a request id must have to be displayed. Kept in sync with `src/client.ts`. */
const SAFE_REQUEST_ID = /^kq-[0-9a-z]{8,32}$/;

/**
 * Validate the lifecycle facts of one submission, keeping only what this build declares.
 *
 * `jobState` comes from the **service**, so it is a membership test against {@link JOB_STATES}: a service
 * that answered with a novel token has its value dropped rather than displayed. `requestId` is the client's
 * own id, so it is shape-checked for the same reason a reference is - the slot may hold an opaque id and
 * nothing else.
 */
export function queryFactsOf(value: unknown): QueryLifecycleFacts | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const requestId = typeof record.requestId === 'string' && SAFE_REQUEST_ID.test(record.requestId)
    ? record.requestId : null;
  const phase = typeof record.phase === 'string' && (QUERY_PHASES as readonly string[]).includes(record.phase)
    ? record.phase as QueryPhaseName : null;
  const jobState = typeof record.jobState === 'string' && (JOB_STATES as readonly string[]).includes(record.jobState)
    ? record.jobState as JobState : null;
  const cancellationConfirmed = typeof record.cancellationConfirmed === 'boolean'
    ? record.cancellationConfirmed : null;
  if (requestId === null && phase === null && jobState === null && cancellationConfirmed === null) return null;
  return { requestId, phase, jobState, cancellationConfirmed };
}

/** Build a detail from arbitrary inputs, keeping only members of the declared sets and safe integers. */
export function failureDetail(parts: FailureDetailInput = {}): FailureDetail {
  const layer = parts.layer;
  return {
    layer: layer === 'plugin' || layer === 'transport' || layer === 'service' || layer === 'host' ? layer : 'plugin',
    httpStatus: safeCount(parts.httpStatus),
    serviceCode: knownServiceCode(parts.serviceCode),
    tool: knownTool(parts.tool),
    parameter: knownParameter(parts.parameter),
    chain: parts.chain === 'identical' || parts.chain === 'consecutive' ? parts.chain : null,
    occurrences: safeCount(parts.occurrences),
    used: safeCount(parts.used),
    max: safeCount(parts.max),
    evidence: safeCount(parts.evidence),
    documents: safeCount(parts.documents),
    reads: safeCount(parts.reads),
    reason: knownReadReason(parts.reason),
    // A line number is a count at or above 1 (or 0 for a file with no lines); anything else, including a
    // negative, is dropped rather than displayed as if it had been a real line.
    requestedStartLine: nonNegative(parts.requestedStartLine),
    fileLines: nonNegative(parts.fileLines),
    suggestedAction: knownReadAction(parts.suggestedAction),
    suggestedRef: isShortReference(parts.suggestedRef) ? String(parts.suggestedRef) : null,
    query: queryFactsOf(parts.query),
  };
}

/** A declared integer range for one parameter. The only shape a range refusal can carry. */
export interface ParameterRange {
  readonly min: number;
  /** `null` for a parameter with a floor but no ceiling (a starting line, a page offset). */
  readonly max: number | null;
}

/**
 * The model-facing sentence for a range refusal, built from this plugin's own constants.
 *
 * It names the tool and the parameter (both closed sets) and the range (numbers), and nothing else: the value
 * the model sent is deliberately not repeated, because a refusal must not become a way to get arbitrary model
 * output echoed back into the transcript, and the range is all the model needs to correct itself.
 */
export function rangeMessage(tool: string, parameter: FailureParameter, range: ParameterRange): string {
  const bound = range.max === null
    ? `an integer greater than or equal to ${range.min}`
    : `an integer from ${range.min} to ${range.max}`;
  return `KNOWLEDGE_PARAMETER_OUT_OF_RANGE: ${tool}.${parameter} accepts ${bound}. Nothing was sent to the `
    + 'knowledge service; retry with a value inside that range.';
}

/** The reference id shape. Kept in sync with the browser half's validation. */
const SAFE_REFERENCE = /^k-[0-9a-z]{8}$/;

/**
 * The short-reference shape (`kref-...`), kept in sync with `src/refs.ts`'s `isReferenceId`.
 *
 * Declared here rather than imported so the failure taxonomy keeps depending on nothing but itself - and it is
 * the same kind of guard as {@link SAFE_REFERENCE}: the slot can hold an opaque id and nothing else.
 */
const SAFE_SHORT_REF = /^kref-[A-Za-z0-9_-]{8,32}$/;

function isShortReference(value: unknown): boolean {
  return typeof value === 'string' && SAFE_SHORT_REF.test(value);
}

/**
 * The sentence the model receives when it combined a continuation reference with a start line.
 *
 * Built here, from a shape-checked reference id and this plugin's own fixed words: no service text, no path and
 * no handle has a route into it. The two variants exist because the honest answer differs - when the plugin
 * holds the document reference for the same file it can hand it over, and when it does not it says so instead
 * of pointing at a reference the caller may not have kept.
 */
export function refConflictMessage(sibling: string | null): string {
  const head = 'KNOWLEDGE_REF_KIND_CONFLICT: the `ref` you passed is a **continuation** reference (a `nextRef`), '
    + 'which is bound to the position its window stopped at, so it cannot be combined with `startLine`.';
  const tail = ' Nothing was sent to the knowledge service.';
  if (sibling) {
    return head
      + ' To read on from that position, send the same reference without `startLine`.'
      + ' To jump to a line, use the **document** reference for the same file, which was issued beside it: '
      + sibling + '.' + tail;
  }
  return head
    + ' To read on from that position, send the same reference without `startLine`.'
    + ' No document reference for that file is live in this session, so locate or read the file again to get one.'
    + tail;
}

/**
 * The model-facing sentence for a refused read, built from the validated facts.
 *
 * Two rules it keeps, both inherited from {@link rangeMessage}:
 *
 * 1. **the value the model sent is not repeated.** The sentence names the *bound* the model has to stay
 *    inside (`fileLines`), never the offending value, because a refusal must not become a channel that
 *    echoes model output back into the transcript;
 * 2. **nothing is invented.** When the service reported no line count, the sentence says which reason
 *    applied and stops there instead of guessing a line to try.
 */
export function readRefusalMessage(code: FailureCode, refusal: ReadRefusal | null): string {
  const lines = refusal?.fileLines ?? null;
  const reason = refusal?.reason ?? null;
  const head = `${code}:`;
  if (reason === 'START_LINE_PAST_END' || reason === 'START_LINE_INVALID') {
    if (lines !== null) {
      return `${head} the requested start line is not a line of this file: it has ${lines} line(s), and `
        + `startLine is 1-based, so it must be at most ${lines}. The service read nothing, so the excerpt you `
        + 'already have is unchanged. Retry once with a start line inside that bound, or continue with the '
        + '`nextRef`/`readRef` a previous window returned instead of choosing a line.';
    }
    return `${head} startLine must be a positive integer within the file's line count. The service read `
      + 'nothing and did not report a line count for this file. Continue with the `nextRef`/`readRef` a '
      + 'previous window returned, or locate the document again.';
  }
  if (reason === 'CURSOR_OUTSIDE_FILE' || reason === 'START_OUTSIDE_FILE'
    || reason === 'IDENTIFIER_MISSING' || reason === 'IDENTIFIER_CONFLICT'
    || reason === 'PARAMETER_TYPE_INVALID') {
    const count = lines !== null ? ` (this file has ${lines} line(s))` : '';
    return `${head} the request was refused before any text was returned${count}. The identifier or the `
      + 'parameter it carried cannot name a readable position in this file. Locate the document again with '
      + 'knowledge_find_documents or knowledge_search and use the `readRef` it returns; do not rebuild a '
      + 'reference by hand and do not retry the same arguments.';
  }
  return `${head} the service refused this read and named no fact this build can act on. The identifier and `
    + 'parameters were not changed by this plugin. Do not repeat the identical call: locate the document '
    + 'again for a fresh reference, or answer from the material already obtained.';
}

let referenceCounter = 0;

/**
 * A short opaque id for one failure, so a user can quote it without pasting a message.
 *
 * Process-local and not a security token: it exists to correlate a card with a log line. It is built from
 * the module's own counter plus a random suffix, so two failures are never the same id within one process.
 */
export function referenceId(random: () => number = Math.random): string {
  referenceCounter += 1;
  const suffix = Math.floor(random() * 0xffffffff).toString(36).padStart(7, '0').slice(0, 7);
  return ('k-' + suffix + referenceCounter.toString(36)).slice(0, 10);
}

/** Whether a string is an acceptable reference id. Used by the browser half too. */
export function isReferenceId(value: unknown): boolean {
  return typeof value === 'string' && SAFE_REFERENCE.test(value);
}

export interface FailureRecord extends FailureAdvice {
  code: FailureCode;
  /** Typed context. There is no free-text field, so nothing upstream can be carried here. */
  detail: FailureDetail;
  /** Short opaque correlation id shown on the card. */
  reference: string;
  /** The sentence the model receives, built here rather than forwarded from upstream. */
  message: string;
  /**
   * True when the plugin itself stopped the call, before any work. The host exposes `concludeTurn()` only
   * on a successful result, so a terminal stop has to travel as a value; the card still shows it as a
   * failure, and the model-facing text says so too.
   */
  terminal: boolean;
}

export function adviceFor(code: FailureCode): FailureAdvice {
  return FAILURE_ADVICE[code];
}

/** Whether a code is one of the two recoverable read refusals, whose sentence names the bound. */
function isReadRefusal(code: FailureCode): boolean {
  return code === 'KNOWLEDGE_READ_RANGE_INVALID' || code === 'KNOWLEDGE_READ_REQUEST_INVALID';
}

/**
 * The model-facing sentence for a failure that happened around a query submission.
 *
 * Built from the validated lifecycle facts, and it exists because the recorded session's response to this
 * class of failure was wrong in a specific way: told only that its search "timed out" or was refused, it
 * switched mode and sent the same query again, then waited in a shell. The sentence therefore states what the
 * plugin knows in three parts - the submission, its phase, and what the service reported - and, when the
 * plugin truly does not know whether the job stopped, says so and says not to re-send.
 */
export function queryMessage(code: FailureCode, detail: FailureDetail): string {
  const facts = detail.query;
  const requestId = facts?.requestId ?? null;
  const parts: string[] = [`${code}:`];
  if (facts === null) {
    return `${parts[0]} this call did not reach the point of submitting a query, and the plugin has no `
      + 'lifecycle information about it. Do not re-send the identical call; correct the reported problem first.';
  }
  const phase = facts.phase;
  if (phase === 'unknown') {
    parts.push('the submission\'s outcome is **unknown**: the plugin\'s own request bound expired, so it did '
      + 'not see the admission response, and it could not confirm what the service did with it.');
  } else if (phase === 'submitting') {
    parts.push('the query was being submitted when this failed.');
  } else if (phase === 'polling') {
    parts.push('the query was accepted and being polled when this failed.');
  } else if (phase === 'cancelling') {
    parts.push('the query was being cancelled when this failed.');
  } else {
    parts.push(`the submission ended in phase "${phase}".`);
  }
  if (requestId !== null) {
    parts.push(`It was submitted under request id ${requestId}${facts.jobState !== null
      ? `, and the service last reported job state "${facts.jobState}"` : ''}.`);
  } else if (facts.jobState !== null) {
    parts.push(`The service last reported job state "${facts.jobState}".`);
  }
  if (facts.cancellationConfirmed === false) {
    parts.push('The plugin asked the service to stop it and the cancellation was **not confirmed**, so the job '
      + 'may still be running.');
  } else if (facts.cancellationConfirmed === true) {
    parts.push('The cancellation was confirmed by the service.');
  }
  parts.push(phase === 'unknown' || facts.cancellationConfirmed === false
    ? 'Do not send an equivalent query immediately: a second submission competes with the job that may still '
      + 'be running, and changing FAST/DEEP does not release it. Report the state above and, if a search is '
      + 'still needed, check the panel\'s query state first.'
    : 'If the question still needs a search, submit it once more - but do not repeat a call that already '
      + 'returned material, and do not treat a refusal here as evidence that the library lacks the material.');
  return parts.join(' ');
}

/**
 * The default model-facing sentence for a code, given the details already validated for it.
 *
 * One function for both paths - building a record and reading one back out of the log - because the two must
 * agree: a message that is assembled differently on the two paths is a message that changes when a session is
 * replayed, and the card and the transcript would then disagree about the same call.
 */
function defaultMessage(code: FailureCode, detail: FailureDetail): string {
  // For a read refusal the sentence comes from the validated facts (which may be absent, giving the honest
  // "named no fact this build can act on" variant). For the reference-kind conflict it is the plugin's own
  // sentence, with the reference it can offer. A query failure names the submission it is about. For every
  // other code the table's fixed sentence is right: there is no number to name.
  if (isReadRefusal(code)) return readRefusalMessage(code, readRefusalOf(detail));
  if (code === 'KNOWLEDGE_REF_KIND_CONFLICT') return refConflictMessage(detail.suggestedRef);
  if (code === 'KNOWLEDGE_QUERY_BUSY') {
    const state = detail.query?.jobState ?? null;
    return `${code}: the knowledge service admits one query at a time and another query is running`
      + `${state !== null ? ` (its state is "${state}")` : ''}. This is a concurrency limit, not an exhausted `
      + 'budget: do not switch FAST/DEEP and re-send, and do not wait in a shell - neither releases it. The '
      + 'catalogue and read tools are not blocked by this and keep their own limits, so do not claim they will '
      + 'fail too. Wait for the state to change, or answer from what has already been obtained.';
  }
  if (code === 'KNOWLEDGE_SERVICE_TIMEOUT' && detail.query !== null) return queryMessage(code, detail);
  // Any other failure that travelled with a submission's lifecycle facts says so too: the phase and the
  // request id are what make it answerable, whichever code the transport produced.
  if (detail.query !== null) return queryMessage(code, detail);
  return `${code}: ${FAILURE_ADVICE[code].reason} ${FAILURE_ADVICE[code].action}`;
}

/** Build a record from a code plus typed detail. The only way to reach the card. */
export function failureRecord(code: FailureCode, options: {
  detail?: FailureDetailInput;
  terminal?: boolean;
  reference?: string;
  message?: string;
  random?: () => number;
} = {}): FailureRecord {
  const advice = FAILURE_ADVICE[code];
  const detail = failureDetail(options.detail);
  return {
    code,
    category: advice.category,
    reason: advice.reason,
    action: advice.action,
    detail,
    reference: isReferenceId(options.reference) ? String(options.reference) : referenceId(options.random),
    // The code is part of the model-facing sentence on purpose: the model gets a stable token it can act on,
    // and it is this plugin's own token rather than a forwarded upstream message. The display text stays
    // free of it, because a card should read as prose.
    message: options.message ?? defaultMessage(code, detail),
    terminal: options.terminal === true,
  };
}

/**
 * Re-validate a record that came back from persisted metadata before it is displayed or acted on.
 *
 * `presentationMeta` is written by this plugin, but a card is rendered from a log that a different build (or
 * a hand-edited record) may have produced, so the display path re-checks rather than trusts: an unknown code
 * becomes `KNOWLEDGE_UNKNOWN`, and a detail field that is not a member of its declared set becomes null. Note
 * that the `message` is **not** read back from the record: the model-facing sentence is rebuilt from this
 * build's own table, so a foreign log cannot inject text into it either.
 */
export function readFailureRecord(value: unknown): FailureRecord | null {
  if (value === null || typeof value !== 'object') return null;
  const candidate = value as Record<string, unknown>;
  const code = candidate.code;
  if (typeof code !== 'string' || !(FAILURE_CODES as readonly string[]).includes(code)) return null;
  const advice = FAILURE_ADVICE[code as FailureCode];
  const stored = candidate.detail && typeof candidate.detail === 'object'
    ? candidate.detail as Record<string, unknown> : {};
  const detail = failureDetail({
    layer: stored.layer as FailureDetail['layer'],
    httpStatus: stored.httpStatus as number,
    serviceCode: stored.serviceCode,
    tool: stored.tool,
    parameter: stored.parameter,
    chain: stored.chain as FailureDetail['chain'],
    occurrences: stored.occurrences as number,
    used: stored.used as number,
    max: stored.max as number,
    evidence: stored.evidence as number,
    documents: stored.documents as number,
    reads: stored.reads as number,
    reason: stored.reason,
    requestedStartLine: stored.requestedStartLine,
    fileLines: stored.fileLines,
    suggestedAction: stored.suggestedAction,
    suggestedRef: stored.suggestedRef,
    query: stored.query,
  });
  return {
    code: code as FailureCode,
    category: advice.category,
    reason: advice.reason,
    action: advice.action,
    detail,
    reference: isReferenceId(candidate.reference) ? String(candidate.reference) : 'k-unknown',
    // The model-facing sentence is rebuilt here from this build's own table and its own validated facts, so a
    // log written by another build cannot inject text into it through the stored `message` field.
    message: defaultMessage(code as FailureCode, detail),
    terminal: candidate.terminal === true,
  };
}

/** The model-facing sentence for a stop, with the reference appended so a log line can be matched. */
export function stopMessage(record: FailureRecord): string {
  return `${record.message}（关联 ID ${record.reference}）`;
}

/** One line of typed context, rendered from the fields rather than from stored text. */
export function describeDetail(detail: FailureDetail): string | null {
  const parts: string[] = [];
  if (detail.httpStatus !== null) parts.push(`HTTP ${detail.httpStatus}`);
  if (detail.serviceCode !== null) parts.push(`服务码 ${detail.serviceCode}`);
  if (detail.layer !== 'plugin') parts.push(`层 ${detail.layer}`);
  if (detail.tool !== null) parts.push(`工具 ${detail.tool}`);
  if (detail.parameter !== null) parts.push(`参数 ${detail.parameter}`);
  // The recoverable-read facts, so the card states the same bound the model's sentence does. Both numbers
  // were validated as non-negative integers on the way in; neither is a value the plugin chose.
  if (detail.reason !== null) parts.push(`原因 ${detail.reason}`);
  if (detail.requestedStartLine !== null) parts.push(`请求起始行 ${detail.requestedStartLine}`);
  if (detail.fileLines !== null) parts.push(`文件行数 ${detail.fileLines}`);
  if (detail.suggestedAction !== null) parts.push(`建议动作 ${detail.suggestedAction}`);
  // The one field a card may show that is not a number or a set member: a plugin-minted short reference, which
  // passed the reference shape to be here at all. It is what makes the conflict refusal actionable.
  if (detail.suggestedRef !== null) parts.push(`可改用引用 ${detail.suggestedRef}`);
  if (detail.chain !== null && detail.occurrences !== null) parts.push(`${detail.chain} × ${detail.occurrences}`);
  if (detail.used !== null && detail.max !== null) parts.push(`本轮已用 ${detail.used} / 上限 ${detail.max}`);
  // The submission's own lifecycle, when the failure happened around a query: which request id the job is
  // reachable under, what this client had reached, and what the service said. Every field is a closed-set
  // member or the client's own id, so this line says *which* submission is unresolved without carrying text.
  if (detail.query !== null) {
    const facts: string[] = [];
    if (detail.query.requestId !== null) facts.push(`请求 ID ${detail.query.requestId}`);
    if (detail.query.phase !== null) facts.push(`阶段 ${detail.query.phase}`);
    if (detail.query.jobState !== null) facts.push(`作业状态 ${detail.query.jobState}`);
    if (detail.query.cancellationConfirmed !== null) {
      facts.push(detail.query.cancellationConfirmed ? '取消已确认' : '取消未确认');
    }
    if (facts.length) parts.push(facts.join('，'));
  }
  // The deterministic half of the closing story: when a turn was stopped, say what it had obtained, so the
  // card reports material rather than only the stopping. Every number comes from the plugin's own tally.
  if (detail.evidence !== null || detail.documents !== null || detail.reads !== null) {
    parts.push(`本轮已取得 检索结果 ${detail.evidence ?? 0} 条 / 目录项 ${detail.documents ?? 0} 条 / 读取窗口 ${detail.reads ?? 0} 个`);
  }
  return parts.length ? parts.join('｜') : null;
}

/**
 * The plain-text fallback a Host renderer shows when the browser card is unavailable.
 *
 * Built only from the taxonomy fields and the typed detail, so it is the same guarantee as the card: no
 * upstream text can reach it, because nothing on this path ever reads a caught exception.
 */
export function failureText(record: FailureRecord): string {
  const lines = [
    `类别：${record.category}`,
    `原因：${record.reason}`,
    `建议：${record.action}`,
  ];
  const detail = describeDetail(record.detail);
  if (detail) lines.push(`说明：${detail}`);
  lines.push(`关联 ID：${record.reference}`);
  return lines.join('\n');
}

/** An error a plugin boundary can throw that still carries a card-bearing code. */
export class KnowledgeFailure extends Error {
  readonly code: FailureCode;
  readonly record: FailureRecord;
  constructor(code: FailureCode, options: { detail?: FailureDetailInput; random?: () => number; message?: string } = {}) {
    const record = failureRecord(code, options);
    // The message is the record's, so the model never sees upstream text for a failure this plugin named.
    super(record.message);
    this.name = 'KnowledgeFailure';
    this.code = code;
    this.record = record;
  }
}

/**
 * One HTTP refusal from the knowledge service, reduced to two machine facts at the moment it is built.
 *
 * The service's response body is read once, a code is looked up in a closed set, and **the body is dropped**.
 * It is not stored on the error, so there is no field anywhere that a later layer could accidentally display,
 * log or forward: a refused request cannot leak the service's prose (which can quote a relative path or a
 * handle) because no object in this process holds it after the throw.
 */
export class KnowledgeServiceError extends Error {
  constructor(readonly status: number, readonly serviceCode: ServiceCode | null,
              /** The validated structured half of the refusal, when the service sent one. */
              readonly refusal: ReadRefusal | null = null) {
    super(`Knowledge service HTTP ${status}${serviceCode ? ' ' + serviceCode : ''}`);
    this.name = 'KnowledgeServiceError';
  }
}

/** The service writes `CODE: prose`. Only a CODE that this build already knows survives; anything else is null. */
export function serviceCodeOf(detail: unknown): ServiceCode | null {
  if (typeof detail !== 'string') return null;
  const match = /^\s*([A-Z][A-Z0-9_]{2,40})\b/.exec(detail);
  return match ? knownServiceCode(match[1]) : null;
}

export interface ClassifiedFailure {
  code: FailureCode;
  /** Typed context. Never upstream prose, because there is no field for prose. */
  detail: FailureDetail;
  /**
   * The sentence the model receives, when the plugin built it itself.
   *
   * Only a {@link KnowledgeFailure} contributes this, and only from its own `record.message` — a string this
   * build constructed out of a code, a tool name, a parameter name (all closed sets) and numbers. It exists
   * because a range refusal has to *name the range* to be actionable, and the display table cannot: one fixed
   * sentence per code has no room for "1 to 10". Nothing caught from the network, from `fetch` or from any
   * third party ever reaches this field — those paths leave it undefined and get the table's sentence.
   */
  message?: string;
  /**
   * The service's own correlation id, when the refusal carried one in the reference shape.
   *
   * Adopting it rather than minting a second id is the point: the id on the card, in the model's sentence and
   * in the service's `READ_REFUSAL` log line are then the same string, so a user who quotes one can be
   * answered by grepping the service log.
   */
  reference?: string;
}

/**
 * Turn one caught value into a code plus typed detail.
 *
 * Order matters: a named refusal keeps its own code, then the transport shape is read, then the service's
 * status and code. Everything else is `KNOWLEDGE_UNKNOWN`, which still gets a card. Note what is *not* read:
 * no caught value's `message`, `stack`, `cause` or any other property is inspected except the two structural
 * facts below (`name` for an abort/timeout, the class for a transport failure).
 */
export function classifyFailure(error: unknown): ClassifiedFailure {
  if (error instanceof KnowledgeFailure) {
    // The message travels with it: this plugin built it, from a code, closed-set names and numbers.
    return { code: error.code, detail: error.record.detail, message: error.record.message };
  }
  const serviceShape = error as { name?: unknown; status?: unknown; serviceCode?: unknown;
    refusal?: unknown; lifecycle?: unknown; knowledgeLifecycle?: unknown } | null;
  // DSH may load the client and tool guard in distinct module instances. Prototype identity is
  // not a wire contract. Accept only the declared name and integer HTTP status; all other facts
  // are revalidated below. Never inspect or copy its message, cause or stack.
  if (error instanceof KnowledgeServiceError || (serviceShape?.name === 'KnowledgeServiceError'
      && Number.isSafeInteger(serviceShape.status) && Number(serviceShape.status) >= 100
      && Number(serviceShape.status) <= 599)) {
    const serviceCode = knownServiceCode(serviceShape?.serviceCode);
    const status = Number.isSafeInteger(serviceShape?.status) ? Number(serviceShape?.status) : null;
    const refusal = readRefusalOf(serviceShape?.refusal);
    // The submission's own lifecycle facts, attached by the client at the moment it caught the failure. They
    // travel with **every** service detail, so a timeout on `POST /queries` can name the request id the job
    // was reachable under instead of only saying that the request failed.
    const query = queryFactsOf(serviceShape?.lifecycle ?? serviceShape?.knowledgeLifecycle);
    const detail = (code: FailureCode, extra: FailureDetailInput = {}): FailureDetail => failureDetail({
      layer: 'service', httpStatus: status, serviceCode,
      ...(query ? { query } : {}),
      // The typed read facts travel with **every** service detail when the service sent them, so the card
      // can show the bound even on a code this build maps elsewhere. They are already-validated members and
      // integers, so this is a widening of what is *shown*, never of what can be expressed.
      ...(refusal ? { reason: refusal.reason, requestedStartLine: refusal.requestedStartLine,
        fileLines: refusal.fileLines, suggestedAction: refusal.suggestedAction,
        ...(refusal.parameter ? { parameter: refusal.parameter } : {}) } : {}),
      ...extra,
    });
    const readReference = refusal?.correlationId ?? undefined;
    // A `429` used to be one fact here ("quota exhausted") and that single line was wrong three times out of
    // four: it told a caller whose query was merely *busy* to wait for a quota to recover, and it told the
    // model that "every remaining call this turn will fail too", which is false for catalogue and read calls
    // (they have their own limits and never take the query lock). The service now names which one it is, so
    // the mapping is a closed-set lookup and the status is only the fallback for a service that names nothing.
    if (status === 429) {
      if (serviceCode === 'QUERY_BUSY') {
        return { code: 'KNOWLEDGE_QUERY_BUSY', detail: detail('KNOWLEDGE_QUERY_BUSY'),
          ...(readReference ? { reference: readReference } : {}) };
      }
      // Only a code that *names* a bound is called a quota. RESULT_CAPACITY, READ_LIMIT_EXCEEDED and
      // PROVIDER_QUOTA_EXHAUSTED share one next step (wait for the bound to move), which is what that code's
      // advice says. A 429 with no recognised code is a different fact and gets its own code.
      if (serviceCode === 'QUERY_REQUEST_CAPACITY' || serviceCode === 'RESULT_CAPACITY' || serviceCode === 'READ_LIMIT_EXCEEDED'
        || serviceCode === 'PROVIDER_QUOTA_EXHAUSTED') {
        return { code: 'KNOWLEDGE_QUOTA_EXHAUSTED', detail: detail('KNOWLEDGE_QUOTA_EXHAUSTED'),
          ...(readReference ? { reference: readReference } : {}) };
      }
      return { code: 'KNOWLEDGE_RATE_LIMITED', detail: detail('KNOWLEDGE_RATE_LIMITED'),
        ...(readReference ? { reference: readReference } : {}) };
    }
    if (status === 401) return { code: 'KNOWLEDGE_SERVICE_UNAUTHORIZED', detail: detail('KNOWLEDGE_SERVICE_UNAUTHORIZED') };
    // The reader's own codes decide these three, because they are the structured fact and the HTTP status is
    // not: the service raises READ_HANDLE_FOREIGN as 403 from one path and 404 from another, and reading the
    // status first would classify the same refusal two different ways depending on which one it came from.
    if (serviceCode === 'READ_HANDLE_INVALID' || serviceCode === 'READ_HANDLE_FOREIGN') {
      // The service checked the signature and refused, or the signature is valid but was minted for a
      // different workspace or library. Either way the identifier in hand is not usable *here*, and neither
      // is evidence about whether the file exists. Folded together on purpose: the caller's next action is
      // the same, and separating them would tell a caller which opaque identifiers exist elsewhere.
      return { code: 'KNOWLEDGE_HANDLE_INVALID', detail: detail('KNOWLEDGE_HANDLE_INVALID') };
    }
    if (serviceCode === 'READ_FILE_UNAVAILABLE') {
      // A valid identifier whose file the library can no longer resolve. This one *is* about the file.
      return { code: 'KNOWLEDGE_SOURCE_UNAVAILABLE', detail: detail('KNOWLEDGE_SOURCE_UNAVAILABLE') };
    }
    if (status === 409 && serviceCode === 'SOURCE_VERSION_CHANGED') return { code: 'KNOWLEDGE_SOURCE_CHANGED', detail: detail('KNOWLEDGE_SOURCE_CHANGED') };
    // The two read-shape refusals, before any status-based fallback: a 400 that names *which* part of the
    // request was wrong is actionable, and folding it into KNOWLEDGE_REQUEST_REJECTED is what made an
    // observed session conclude "the reference must be stale" and abandon a file it could have read.
    if (serviceCode === 'READ_RANGE_INVALID') {
      return { code: 'KNOWLEDGE_READ_RANGE_INVALID', detail: detail('KNOWLEDGE_READ_RANGE_INVALID'),
        message: readRefusalMessage('KNOWLEDGE_READ_RANGE_INVALID', refusal),
        ...(readReference ? { reference: readReference } : {}) };
    }
    if (serviceCode === 'READ_REQUEST_INVALID') {
      return { code: 'KNOWLEDGE_READ_REQUEST_INVALID', detail: detail('KNOWLEDGE_READ_REQUEST_INVALID'),
        message: readRefusalMessage('KNOWLEDGE_READ_REQUEST_INVALID', refusal),
        ...(readReference ? { reference: readReference } : {}) };
    }
    if (serviceCode === 'QUERY_NOT_TERMINAL') return {code:'KNOWLEDGE_QUERY_NOT_TERMINAL',detail:detail('KNOWLEDGE_QUERY_NOT_TERMINAL')};
    if (serviceCode === 'QUERY_RESULT_RELEASED') return {code:'KNOWLEDGE_QUERY_RESULT_RELEASED',detail:detail('KNOWLEDGE_QUERY_RESULT_RELEASED')};
    if (serviceCode === 'QUERY_REQUEST_CONFLICT') {
      // The same request id arrived with different parameters. Reusing the id would hide a second, separately
      // billed job behind the first, so the service refuses; the plugin reports it as a request-shape refusal.
      return { code: 'KNOWLEDGE_REQUEST_REJECTED', detail: detail('KNOWLEDGE_REQUEST_REJECTED') };
    }
    if (status === 403) return { code: 'KNOWLEDGE_REQUEST_REJECTED', detail: detail('KNOWLEDGE_REQUEST_REJECTED') };
    if (status !== null && status >= 500) {
      // The service's own code is kept when this build knows it: `CATALOG_UNAVAILABLE` and a bare 500 need
      // different investigation. It is kept as an enum member, never as text.
      return { code: 'KNOWLEDGE_SERVICE_UNAVAILABLE', detail: detail('KNOWLEDGE_SERVICE_UNAVAILABLE') };
    }
    return { code: 'KNOWLEDGE_REQUEST_REJECTED', detail: detail('KNOWLEDGE_REQUEST_REJECTED') };
  }
  // A DOMException from `AbortSignal.timeout` is a TimeoutError; an external stop is an AbortError. Only the
  // `name` is read, and only to pick between two codes.
  const name = typeof (error as { name?: unknown })?.name === 'string' ? String((error as { name: string }).name) : '';
  // A submission that timed out or was aborted before it was admitted keeps its lifecycle facts, so the
  // failure can name the request id and the phase instead of reporting a bare transport timeout.
  const query = queryFactsOf((error as { knowledgeLifecycle?: unknown })?.knowledgeLifecycle);
  if (name === 'TimeoutError') {
    return { code: 'KNOWLEDGE_SERVICE_TIMEOUT',
      detail: failureDetail({ layer: 'transport', ...(query ? { query } : {}) }) };
  }
  if (name === 'AbortError') {
    return { code: 'KNOWLEDGE_REQUEST_CANCELLED',
      detail: failureDetail({ layer: 'transport', ...(query ? { query } : {}) }) };
  }
  // `fetch` reports a transport failure as a TypeError. Node hangs the cause on it; the cause is never read,
  // because it can name an address and a port.
  if (error instanceof TypeError) return { code: 'KNOWLEDGE_SERVICE_UNREACHABLE', detail: failureDetail({ layer: 'transport' }) };
  return { code: 'KNOWLEDGE_UNKNOWN', detail: failureDetail({ layer: 'host' }) };
}

/**
 * Throw the classified failure as a `HarnessError`, which is the only shape the host records structurally as
 * `{name, code}` on the result node - a plain `Error` yields no code, and the card would have nothing but
 * prose to key on. `build` is injected so the offline tests do not need the DSH package.
 */
export function failureFrom(error: unknown, build: (message: string, code: string) => Error = (message, code) => {
  const fallback = new Error(message);
  fallback.name = 'HarnessError';
  (fallback as Error & { code: string }).code = code;
  return fallback;
}, context: { used?: number; max?: number; tool?: unknown; activity?: unknown; callerInterrupted?: boolean } = {}): Error {
  const classified = classifyFailure(error);
  if (classified.code === 'KNOWLEDGE_REQUEST_CANCELLED' && context.callerInterrupted !== false) {
    // Cancellation is the caller's decision, not a knowledge failure: keep the original abort so the host's
    // own `interrupted` handling stays in charge.
    return error instanceof Error ? error : new Error('aborted');
  }
  const record = failureRecord(classified.code, {
    // The turn's own accounting is attached to a *dispatched* failure too, not only to a stop: a model that
    // is refused a read and cannot see how many attempts the turn has left will either re-plan blindly or
    // stop early. `used`/`max` are the plugin's own numbers, and the refusal is counted exactly like a
    // success (see `TurnLedger.reserve`).
    detail: { ...classified.detail, ...(context.used === undefined ? {} : { used: context.used }),
      ...(context.max === undefined ? {} : { max: context.max }),
      ...(context.tool === undefined ? {} : { tool: context.tool as never }) },
    ...classified.message === undefined ? {} : { message: classified.message },
    ...classified.reference === undefined ? {} : { reference: classified.reference },
  });
  const detail: Record<string, unknown> = {...record.detail};
  delete detail.requestedStartLine;
  const activity = context.activity as Record<string, unknown> | undefined;
  const measured = activity && activity.scope === 'turn' && ['attempts','httpRequests','searches','reads'].every(k => Number.isSafeInteger(activity[k]) && Number(activity[k]) >= 0)
    ? {scope:'turn',attempts:activity.attempts,httpRequests:activity.httpRequests,searches:activity.searches,reads:activity.reads} : undefined;
  const projection = {code:record.code,reference:record.reference,detail,
    budget:{used:record.detail?.used,max:record.detail?.max,...(measured ? {activity:measured} : {})}};
  const suffix = context.used === undefined ? '' : "\nKNOWLEDGE_FAILURE_FACTS=" + JSON.stringify(projection);
  const thrown = build(stopMessage(record) + suffix, record.code);
  (thrown as Error & { record?: FailureRecord }).record = record;
  return thrown;
}
