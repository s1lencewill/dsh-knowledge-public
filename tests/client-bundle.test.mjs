/**
 * The browser half: `ui/source-card.js`, loaded the way the shell loads it.
 *
 * The bundle is plain lazy-CJS with no build step, so it can be exercised directly: this file installs a
 * fake `window.__ModuleLoader__`, evaluates the bundle for its registration side effect, and renders the
 * component it registered with a small React shim. That reaches the things that would otherwise only be
 * visible by clicking around a live page:
 *
 * - it registers exactly the three knowledge tools into the keyed Tool slot;
 * - it renders the service's own fields, and says "未核验 / 未评估" where the service said nothing;
 * - source text containing markup or a script stays inert text - no HTML is ever parsed;
 * - opening a card and clicking its actions issues no request of any kind, so a panel view cannot spend
 *   a model call, a read budget, or a service token the browser never receives.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const SOURCE_PATH = resolve(import.meta.dirname, '../ui/source-card.js');
const sourceText = readFileSync(SOURCE_PATH, 'utf8');

// ------------------------------------------------------------------ a very small React

let frame = null;
const React = {
  createElement(type, props, ...children) {
    return {
      type, props: props ?? {},
      children: children.flat(Infinity).filter(child => child !== null && child !== undefined && typeof child !== 'boolean'),
    };
  },
  useState(initial) {
    assert.ok(frame, 'useState was called outside a component render');
    const store = frame;
    const index = store.cursor++;
    if (!(index in store.states)) store.states[index] = initial;
    return [store.states[index], (value) => {
      store.states[index] = typeof value === 'function' ? value(store.states[index]) : value;
    }];
  },
};

/**
 * Hook storage, keyed by component identity and then by React key, so state survives re-renders the way
 * React's own store does. Without that, clicking a button and re-rendering would silently reset the card.
 */
const stores = new WeakMap();

/** Invoke one function component with its own persistent hook frame. */
function invoke(type, props) {
  let byKey = stores.get(type);
  if (!byKey) stores.set(type, byKey = new Map());
  const key = String(props?.key ?? '');
  let store = byKey.get(key);
  if (!store) byKey.set(key, store = { states: [], cursor: 0 });
  store.cursor = 0;
  const outer = frame;
  frame = store;
  try {
    return type(props);
  } finally {
    frame = outer;
  }
}

/** Render a function component: element tree plus the flat text of everything it produced. */
function render(type, props) {
  const texts = [];
  const elements = [];
  const walk = (node) => {
    if (node === null || node === undefined || typeof node === 'boolean') return;
    if (Array.isArray(node)) return node.forEach(walk);
    if (typeof node === 'string' || typeof node === 'number') return void texts.push(String(node));
    if (typeof node.type === 'function') return walk(invoke(node.type, node.props));
    elements.push(node);
    walk(node.children);
  };
  const tree = invoke(type, props);
  walk(tree);
  return { tree, text: texts.join('\n'), elements };
}

/** Click the first button whose label contains `label`. */
function click(rendered, label) {
  const button = rendered.elements.find(element => element.type === 'button'
    && element.children.some(child => String(child).includes(label)));
  assert.ok(button, `the card must offer a "${label}" button`);
  assert.notEqual(button.props.disabled, true, `"${label}" must be clickable here`);
  button.props.onClick();
  return button;
}

// ------------------------------------------------------------------ loading the bundle

/** Evaluate `ui/source-card.js` under a fake loader and return the module exports it produced. */
function loadBundle() {
  let loaded = null;
  const window = {
    __ModuleLoader__: {
      load(spec) {
        assert.equal(spec.id, 'dsh-knowledge', 'the bundle must register under the package name');
        loaded = spec;
      },
    },
  };
  const require = (spec) => {
    if (spec === 'react') return React;
    throw new Error('unexpected module request: ' + spec);
  };
  const module = { exports: {} };
  new Function('window', 'require', 'module', 'exports', sourceText + '\nreturn module.exports;')(
    window, require, module, module.exports);
  assert.ok(loaded, 'the bundle must call window.__ModuleLoader__.load');
  return loaded.factory(require);
}

/** The three registrations, as `{slot, spec, Component}`, from one fresh load. */
function registrations() {
  const entries = [];
  const slots = {
    inject(slot, callback) { for (const entry of callback()) entries.push({ slot, ...entry }); },
    register(spec, Component) { return { spec, Component }; },
  };
  loadBundle().apply({ slots });
  return entries;
}

/** The component the bundle registered for `knowledge_search`. */
function sourceCardComponent() {
  const entry = registrations().find(candidate => candidate.spec.key === 'knowledge_search');
  assert.ok(entry, 'knowledge_search must have a registered card');
  return entry.Component;
}

const card = (overrides = {}) => ({
  libraryId: 'lib-1', title: 'A study', fileName: 'a.pdf', excerpt: 'quoted text',
  location: { coordinateSpace: 'extracted-text', kind: 'pdf-page', pdfPage: 3, pageSource: 'page-map', startLine: 10, endLine: 11, startChar: 4, endChar: 15 },
  sourceVersion: 'abcdef0123456789', citationOrigin: 'engine', truncated: false, truncationNotices: [],
  parseQuality: 'clean',
  status: { identity: 'verified', identityDetail: null, locationVerified: true, support: 'not-assessed' },
  ...overrides,
});

const settled = (sources) => ({
  kind: 'tool-result', callId: 'c1', parentCallId: undefined,
  call: { name: 'knowledge_search', argsRaw: '{}' },
  content: [], isError: false, meta: { sources }, subCalls: [],
});

// ------------------------------------------------------------------ registration

test('the bundle registers each knowledge tool into the keyed Tool slot', () => {
  const entries = registrations();
  assert.equal(entries.length, 5);
  for (const entry of entries) assert.equal(entry.slot, 'tool.call.toolview');
  // All five, `knowledge_list_libraries` included: this component owns the whole row for each name it
  // registers, so a name left out is a name whose failures the host's generic row has to describe.
  assert.deepEqual(entries.map(entry => entry.spec.key).sort(),
    ['knowledge_find_documents', 'knowledge_list_documents', 'knowledge_list_libraries', 'knowledge_read',
      'knowledge_search']);
  for (const entry of entries) assert.equal(entry.spec.name, 'tool.call.toolview');
  // Exactly one entry may declare a child slot, and `tool.call.images` belongs to the Tool layer's own
  // image registrant: a second declaration throws at load.
  for (const entry of entries) assert.equal(entry.spec.children, undefined);
});

test('the one injected service is the slot registry', () => {
  assert.deepEqual(loadBundle().inject, ['slots']);
});

// ------------------------------------------------------------------ payload gating

test('a stopped call shows the machine code the plugin recorded, not only the counters', () => {
  const SourceCard = sourceCardComponent();
  // Exactly what a real run persisted for the per-turn ceiling: a successful result (a stop has to travel as one
  // to reach the host's concludeTurn) whose meta carries the structured failure record.
  const block = { ...settled([]), isError: false, meta: { sources: [], failure: {
    code: 'KNOWLEDGE_TURN_LIMIT_REACHED', reference: 'k-1w7bds91',
    detail: { layer: 'plugin', tool: 'knowledge_read', used: 7, max: 6 } } } };
  const rendered = render(SourceCard, { block, callId: 'acc-9-6' });
  assert.match(rendered.text, /KNOWLEDGE_TURN_LIMIT_REACHED/, 'the closed-set code is shown');
  assert.match(rendered.text, /本轮已用 7 \/ 上限 6/, 'and so is the typed detail');
  assert.match(rendered.text, /k-1w7bds91/, 'and the correlation id');
});

test('a failure whose code is not in the closed table is shown as unknown, never as the raw value', () => {
  const SourceCard = sourceCardComponent();
  const block = { ...settled([]), isError: false, meta: { sources: [], failure: {
    code: 'NOT_A_KNOWN_CODE', detail: { layer: 'plugin' } } } };
  const rendered = render(SourceCard, { block, callId: 'c-unknown' });
  assert.match(rendered.text, /KNOWLEDGE_UNKNOWN/);
  assert.doesNotMatch(rendered.text, /NOT_A_KNOWN_CODE/);
});

test('listing libraries renders a list card, not the zero-result sentence', () => {
  const SourceCard = sourceCardComponent();
  const block = { ...settled([]), call: { name: 'knowledge_list_libraries', argsRaw: '{}' },
    content: [{ type: 'text', text: JSON.stringify({ libraries: [
      { id: 'samples', name: 'Trial sample library (synthetic)' }], selectedLibraryId: 'samples', note: 'scoped' }) }],
    meta: { sources: [] } };
  const rendered = render(SourceCard, { block, callId: 'acc-17-14' });
  assert.match(rendered.text, /本次调用是列出知识库/);
  assert.match(rendered.text, /samples/, 'the libraries the call actually listed');
  assert.doesNotMatch(rendered.text, /本次没有来源|定位未命中/, 'a list call cannot "miss" a document');
});

test('a list card with unreadable output says so instead of inventing libraries', () => {
  const SourceCard = sourceCardComponent();
  const block = { ...settled([]), call: { name: 'knowledge_list_libraries', argsRaw: '{}' },
    content: [{ type: 'text', text: 'not json at all' }], meta: { sources: [] } };
  const rendered = render(SourceCard, { block, callId: 'c-list' });
  assert.match(rendered.text, /输出中没有可识别的库列表/);
  assert.doesNotMatch(rendered.text, /本次没有来源/);
});

test('a locate call with no hits still renders the zero-result sentence', () => {
  const SourceCard = sourceCardComponent();
  const block = { ...settled([]), call: { name: 'knowledge_find_documents', argsRaw: '{}' }, meta: { sources: [] } };
  const rendered = render(SourceCard, { block, callId: 'c-find' });
  assert.match(rendered.text, /本次没有来源/);
  assert.match(rendered.text, /定位未命中/);
});

test('a settled root call renders a card, and a failure renders a failure card', () => {  const SourceCard = sourceCardComponent();
  // A running call has no result yet, and this component owns the whole row for its tool name, so it draws
  // nothing rather than inventing a state.
  assert.equal(SourceCard({ block: { kind: 'running', call: { name: 'knowledge_search' } } }), null);
  assert.equal(SourceCard({ block: { ...settled([card()]), parentCallId: 'p1' } }), null);
  assert.equal(SourceCard({ block: { ...settled([card()]), meta: undefined } }), null);
  assert.equal(SourceCard({ block: { ...settled([card()]), meta: { sources: 'nope' } } }), null);
  assert.notEqual(SourceCard({ block: settled([card()]) }), null);
  // A window-truncated result (`call: null`) still has its persisted payload, so the card still renders:
  // the sources are what it needs, and hiding them would lose information the user can still use.
  assert.notEqual(SourceCard({ block: { ...settled([card()]), call: null } }), null);
  // A failed call must NOT render as nothing. This used to assert null, which is exactly the defect: the
  // keyed slot replaces the host's generic row, so null means the error is invisible.
  const failed = SourceCard({ block: { ...settled([card()]), isError: true, error: { name: 'HarnessError', code: 'KNOWLEDGE_SOURCE_CHANGED' } } });
  assert.notEqual(failed, null, 'an errored call must render a failure card');
});

// ------------------------------------------------------------------ rendering

test('the card renders the service fields and keeps the three axes apart', () => {
  const { text } = render(sourceCardComponent(), { block: settled([card()]) });
  assert.match(text, /A study/);
  assert.match(text, /a\.pdf/);
  assert.match(text, /lib-1/);
  assert.match(text, /PDF 第 3 页/);
  assert.match(text, /身份已核验/);
  assert.match(text, /位置已核验/);
  assert.match(text, /支持：未评估/);
  assert.match(text, /解析质量/);
  assert.match(text, /clean/);
  assert.match(text, /引擎原文片段/);
  assert.doesNotMatch(text, /答案已核验|已验证结论|置信度/);
});

test('an assessed-nothing card says so rather than reading as a weaker verification', () => {
  const { text } = render(sourceCardComponent(), {
    block: settled([card({
      status: { identity: 'not-assessed', identityDetail: null, locationVerified: false, support: 'not-assessed' },
    })]),
  });
  assert.match(text, /身份未核验/);
  assert.match(text, /位置未核验/);
  assert.match(text, /支持：未评估/);
});

test('a window that crosses a page boundary is rendered as a range, not as one page', () => {
  // The exact payload the service persisted for a real engine citation whose excerpt covered lines 1-26 of
  // a three-page document. Read from the session log rather than guessed at, so this pins the rendering
  // that a screenshot can only suggest.
  const { text } = render(sourceCardComponent(), {
    block: settled([card({
      location: { coordinateSpace: 'extracted-text', kind: 'pdf-page', pdfPage: 1, pdfPageEnd: 3,
        pageSource: 'page-map', pageHint: null, startLine: 1, endLine: 26, startChar: 0, endChar: 619 },
    })]),
  });
  assert.match(text, /PDF 第 1–3 页，跨页/);
  assert.match(text, /来自抽取映射/);
  assert.doesNotMatch(text, /只能给抽取文本坐标/);
});

test('a window inside one page reports exactly that page', () => {
  const { text } = render(sourceCardComponent(), {
    block: settled([card({
      location: { coordinateSpace: 'extracted-text', kind: 'pdf-page', pdfPage: 2, pdfPageEnd: 2,
        pageSource: 'page-map', pageHint: null, startLine: 12, endLine: 18 },
    })]),
  });
  assert.match(text, /PDF 第 2 页/);
  assert.doesNotMatch(text, /跨页|只能给抽取文本坐标/);
});

test('a PDF with no usable map says so, and the marker appears only as an unadopted hint', () => {
  const { text } = render(sourceCardComponent(), {
    block: settled([card({
      location: { coordinateSpace: 'extracted-text', kind: 'text-line', pdfPage: null, pageSource: 'unmapped',
        pageHint: 9, startLine: 5, endLine: 6 },
    })]),
  });
  assert.match(text, /页码映射未建立/);
  assert.match(text, /疑似标记称第 9 页，未采信/);
});

test('a page the text merely claims is never rendered as the page', () => {
  // The card is the one place a reader sees the number, so the rule has to hold here too: `pdfPage` is
  // null and nothing in the rendered text presents the marker as a page.
  const { text } = render(sourceCardComponent(), {
    block: settled([card({
      location: { coordinateSpace: 'extracted-text', kind: 'text-line', pdfPage: null, pageSource: 'unmapped',
        pageHint: 9, startLine: 4, endLine: 4 },
    })]),
  });
  assert.doesNotMatch(text, /PDF 第 9 页/);
});

test('a mapping gap and an unusable map are surfaced as defects, and neither falls back to text', () => {
  const gap = render(sourceCardComponent(), {
    block: settled([card({
      location: { coordinateSpace: 'extracted-text', kind: 'text-line', pdfPage: null, pageSource: 'map-gap',
        pageHint: null, startLine: 5, endLine: 6 },
    })]),
  });
  assert.match(gap.text, /映射缺口/);
  const unusable = render(sourceCardComponent(), {
    block: settled([card({
      location: { coordinateSpace: 'extracted-text', kind: 'text-line', pdfPage: null, pageSource: 'map-unusable',
        pageHint: null, startLine: 5, endLine: 6 },
    })]),
  });
  assert.match(unusable.text, /页码映射不可用/);
  assert.doesNotMatch(unusable.text, /疑似标记/);
});

test('a Markdown source is given line numbers only, never a page', () => {
  const { text } = render(sourceCardComponent(), {
    block: settled([card({
      location: { coordinateSpace: 'extracted-text', kind: 'text-line', pdfPage: null, pageSource: 'not-pdf',
        pageHint: null, startLine: 5, endLine: 6 },
    })]),
  });
  assert.match(text, /非 PDF 来源/);
  assert.doesNotMatch(text, /PDF 第/);
});

test('truncation is shown, and a zero-source result is not dressed up as success', () => {
  const truncated = render(sourceCardComponent(), {
    block: settled([card({ truncated: true, truncationNotices: ['EXCERPT_TRUNCATED'] })]),
  });
  assert.match(truncated.text, /EXCERPT_TRUNCATED/);
  const empty = render(sourceCardComponent(), { block: settled([]) });
  assert.match(empty.text, /本次没有来源/);
  assert.match(empty.text, /不代表库中没有该资料/);
});

test('a card with no location cannot imply one', () => {
  const { text } = render(sourceCardComponent(), { block: settled([card({ location: null, excerpt: null })]) });
  assert.match(text, /无位置信息/);
  assert.match(text, /没有包含摘录/);
});

test('a catalogue entry that refused the read says so, instead of "not read yet"', () => {
  // "本次调用尚未读取" is a true statement about a call that returned no window. It is the wrong statement
  // about a call whose file the catalogue declared unreadable, which is what the old wording covered.
  const { text } = render(sourceCardComponent(), {
    block: settled([card({ location: null, excerpt: null, truncationNotices: ['DOCUMENT_NOT_READABLE'], truncated: true })]),
  });
  assert.match(text, /本次未读取到正文/);
  assert.match(text, /DOCUMENT_NOT_READABLE/);
  assert.match(text, /不可读/);
});

// ------------------------------------------------------------------ failure cards

/** A failed call as the host records it: `isError` plus the structured `{name, code}` of a HarnessError. */
const failed = (code, overrides = {}) => ({
  kind: 'tool-result', callId: 'call-42', parentCallId: undefined,
  call: { name: 'knowledge_read', argsRaw: '{}' },
  content: [{ type: 'text', text: 'upstream prose that must not be rendered' }],
  isError: true, error: code === undefined ? undefined : { name: 'HarnessError', code },
  meta: undefined, subCalls: [], ...overrides,
});

/** A stopped call: a successful result whose persisted meta carries the plugin's own failure record. */
const stopped = (failure, overrides = {}) => ({
  kind: 'tool-result', callId: 'call-7', parentCallId: undefined,
  call: { name: 'knowledge_search', argsRaw: '{}' },
  content: [{ type: 'text', text: '{"stopped":"KNOWLEDGE_TURN_LIMIT_REACHED"}' }],
  isError: false, error: undefined, meta: { sources: [], failure }, subCalls: [], ...overrides,
});

test('every failure code has display text, and the two halves agree', () => {
  // The browser half cannot import the host half, so the table is duplicated. A code added on one side only
  // would render as an unknown error on the other, which is why the copies are compared rather than trusted:
  // not just the code list, but every displayed string.
  const hostSource = readFileSync(resolve(import.meta.dirname, '../src/failures.ts'), 'utf8');
  const parse = (text) => {
    const table = {};
    for (const match of text.matchAll(/(KNOWLEDGE_[A-Z_]+):\s*\{([\s\S]*?)\n\s*\},/g)) {
      const body = match[2];
      const field = (name) => {
        const found = new RegExp(`${name}:\\s*'((?:[^'\\\\]|\\\\.)*)'`).exec(body);
        return found ? found[1] : null;
      };
      table[match[1]] = { category: field('category'), reason: field('reason'), action: field('action') };
    }
    return table;
  };
  const host = parse(hostSource);
  const client = parse(sourceText);
  const codes = Object.keys(host).sort();
  assert.ok(codes.length >= 13, `expected the full taxonomy, found ${codes.length}`);
  assert.deepEqual(Object.keys(client).sort(), codes);
  for (const code of codes) {
    // The client table carries no comment lines between its fields, so a null here is a parse failure rather
    // than a real difference; both cases must be reported.
    assert.deepEqual(client[code], host[code], `${code} must read identically in both halves`);
    assert.ok(host[code].category && host[code].reason && host[code].action, `${code} must complete the table`);
  }
});

test('a thrown failure renders the category, the reason and the action from the table', () => {
  const { text } = render(sourceCardComponent(), { block: failed('KNOWLEDGE_SOURCE_CHANGED') });
  assert.match(text, /知识库调用失败/);
  assert.match(text, /原文已变更/);
  assert.match(text, /重新检索以获得新的标识/);
  // The call id is a host handle for exactly this call and appears in the log, so it is the correlation id
  // when the plugin's own record is unavailable (a thrown failure carries no meta).
  assert.match(text, /call-42/);
});

test('a failure card never borrows the success vocabulary', () => {
  const { text } = render(sourceCardComponent(), { block: failed('KNOWLEDGE_LIBRARY_NOT_SELECTED') });
  assert.match(text, /库范围不匹配/);
  // A call that returned no sources has nothing to verify; the three axes and the source badge belong to a
  // card that did return something.
  assert.doesNotMatch(text, /身份已核验|位置已核验|支持：未评估/);
  assert.doesNotMatch(text, /来源$/m);
  assert.doesNotMatch(text, /本次没有来源/);
  assert.doesNotMatch(text, /展开已返回内容|打开原件|查看候选差异/);
});

test('a stopped call renders as a failure even though the host sees a success', () => {
  // A stop must travel as a successful result: `concludeTurn()` exists only on one. The card is where that
  // asymmetry is repaired for the reader, and it must not present the stop as a source result.
  const { text } = render(sourceCardComponent(), {
    block: stopped({ code: 'KNOWLEDGE_TURN_LIMIT_REACHED',
      detail: { layer: 'plugin', tool: 'knowledge_search', used: 7, max: 6 },
      reference: 'k-abc12345', terminal: true }),
  });
  assert.match(text, /本轮工具调用已达上限/);
  assert.match(text, /新的一轮/);
  assert.match(text, /k-abc12345/);
  // The context line is built by this half from typed fields, not copied from the record.
  assert.match(text, /本轮已用 7 \/ 上限 6/);
  assert.match(text, /工具 knowledge_search/);
  assert.doesNotMatch(text, /身份已核验|位置已核验/);
  assert.doesNotMatch(text, /本次没有来源/);
});

test('an unknown failure still gets a card and a correlation id', () => {
  const { text } = render(sourceCardComponent(), { block: failed('SOMETHING_ELSE') });
  assert.match(text, /未知错误/);
  assert.match(text, /关联 ID/);
  assert.match(text, /call-42/);
  // An error with no code at all (a plain Error yields no structured info) must not render as nothing.
  const bare = render(sourceCardComponent(), { block: failed(undefined) });
  assert.match(bare.text, /未知错误/);
});

test('a record cannot put its own text on the card', () => {
  // The log is data. A record's detail and reference are validated against a token shape, and the reason,
  // action and category always come from the table, so a hostile or merely sloppy record cannot render prose.
  const hostile = {
    code: 'KNOWLEDGE_UNKNOWN',
    detail: 'C:\\secret\\path\\file.txt',
    reference: '<script>alert(1)</script>',
    reason: 'RAW REASON FROM THE LOG',
    action: 'RAW ACTION FROM THE LOG',
    category: 'RAW CATEGORY FROM THE LOG',
  };
  const { text, elements } = render(sourceCardComponent(), { block: stopped(hostile) });
  assert.match(text, /未知错误/);
  assert.doesNotMatch(text, /RAW REASON|RAW ACTION|RAW CATEGORY/);
  assert.doesNotMatch(text, /C:\\secret/);
  assert.doesNotMatch(text, /alert\(1\)/);
  // The call id is the fallback correlation id, and it is a host field rather than the record's.
  assert.match(text, /call-7/);
  assert.ok(!elements.some(element => element.type === 'script'), 'a record must not become an element');
});

test('a signed file handle in a record cannot reach the screen', () => {
  // A handle is made only of identifier characters, so a character allow-list would have displayed it - that
  // was the whole reason the previous rule existed. There is no such rule now: every detail field is a member
  // of a declared set, and a handle is not a member of any of them.
  const handle = 'krh1.eyJrIjoiaCIsImwiOiJzeW50aGV0aWMtcHJvYmUiLCJwIjoic3R1ZHkudHh0In0.zBVLbHIZSLrFA8j0q6';
  const { text } = render(sourceCardComponent(), {
    block: stopped({ code: 'KNOWLEDGE_SOURCE_CHANGED',
      detail: { layer: 'service', serviceCode: handle, tool: handle, httpStatus: handle },
      reference: 'k-abc12345' }),
  });
  assert.match(text, /原文已变更/);
  assert.doesNotMatch(text, /krh1\./, 'a full handle must never be displayed');
  assert.doesNotMatch(text, /eyJrIjoiaC/);
  // Nothing landed in a field, so there is no 说明 line at all - the handle was not cleaned, it had nowhere to go.
  assert.doesNotMatch(text, /服务码|工具|HTTP krh1/);
  // The reference id is still shown, so the failure remains correlatable.
  assert.match(text, /k-abc12345/);
});

test('a short key with separators cannot reach the screen either', () => {
  // The case that defeats any character-or-length rule: short segments joined by separators.
  const key = 'sk-synthetic-public-fixture-not-a-real-credential';
  const { text } = render(sourceCardComponent(), {
    block: stopped({ code: 'KNOWLEDGE_LIBRARY_NOT_SELECTED', detail: { tool: key, serviceCode: key, layer: key } }),
  });
  assert.match(text, /库范围不匹配/);
  assert.doesNotMatch(text, /sk-abcd/);
  assert.doesNotMatch(text, /服务码|工具/);
});

test('both halves declare the same failure sets', () => {
  // The halves cannot import each other, so the sets are duplicated. A member added on one side only would
  // either be dropped on the other or render as an unlabelled value, so they are compared.
  const hostSource = readFileSync(resolve(import.meta.dirname, '../src/failures.ts'), 'utf8');
  const members = (text, name) => {
    const block = new RegExp(`${name}\\s*=\\s*\\[([\\s\\S]*?)\\]`).exec(text);
    assert.ok(block, `${name} must be declared in both halves`);
    return [...block[1].matchAll(/'([A-Za-z_]+)'/g)].map(match => match[1]).sort();
  };
  assert.deepEqual(members(sourceText, 'SERVICE_CODES'), members(hostSource, 'KNOWN_SERVICE_CODES'));
  assert.deepEqual(members(sourceText, 'FAILURE_TOOLS'), members(hostSource, 'FAILURE_TOOLS'));
  assert.deepEqual(members(sourceText, 'FAILURE_LAYERS'), ['host', 'plugin', 'service', 'transport']);
  // The parameter set is duplicated for the same reason and is compared the same way: a range refusal names
  // the offending parameter, so a name present on one side only would be dropped on the other or rendered
  // unlabelled.
  assert.deepEqual(members(sourceText, 'FAILURE_PARAMETERS'), members(hostSource, 'FAILURE_PARAMETERS'));
  assert.equal(members(sourceText, 'SERVICE_CODES').length, 29);
  // The query lifecycle sets are the same kind of duplication and get the same comparison: the browser half
  // renders a job state, so a state present on one side only would be dropped there or rendered unlabelled.
  assert.deepEqual(members(sourceText, 'QUERY_PHASES'), members(hostSource, 'QUERY_PHASES'));
  assert.deepEqual(members(sourceText, 'JOB_STATES'), members(hostSource, 'JOB_STATES'));
});

test('a record with an unknown code falls back to the generic statement', () => {
  const { text } = render(sourceCardComponent(), {
    block: stopped({ code: 'NOT_A_REAL_CODE', reference: 'k-abc12345' }),
  });
  assert.match(text, /未知错误/);
  assert.doesNotMatch(text, /NOT_A_REAL_CODE/);
});

test('the upstream error message is never rendered', () => {
  const { text } = render(sourceCardComponent(), {
    block: failed('KNOWLEDGE_SOURCE_CHANGED', { content: [{ type: 'text', text: 'Error: HTTP 409 SOURCE_VERSION_CHANGED /home/u/secret/study.txt krh1.eyJzZWNyZXQ' }] }),
  });
  // The card describes the class of failure; it does not echo the transport error, the path or the handle.
  assert.match(text, /原文已变更/);
  assert.doesNotMatch(text, /HTTP 409/);
  assert.doesNotMatch(text, /study\.txt/);
  assert.doesNotMatch(text, /krh1\./);
});

test('markup and script inside a document stay inert text', () => {
  const hostile = '<img src=x onerror="steal()"><script>globalThis.pwned = true</script>';
  const { text, elements } = render(sourceCardComponent(), {
    block: settled([card({ excerpt: hostile, title: hostile })]),
  });
  assert.match(text, /<script>/);
  assert.ok(!elements.some(element => element.type === 'script' || element.type === 'img'),
    'source text must never become an element');
  assert.equal(globalThis.pwned, undefined);
});

test('expanding and reading carry different labels, and only a read is called a read', () => {
  // The reported problem: a button labelled "查看上下文" that makes no request invites the reader to think
  // the panel fetched context. Expanding already-returned content and calling knowledge_read are different
  // acts, so they get different words, and only the second one is described as reading.
  const search = render(sourceCardComponent(), { block: settled([card({ citationOrigin: 'engine' })]) });
  const expand = search.elements.find(element => element.type === 'button'
    && element.children.some(child => String(child).includes('已返回内容')));
  assert.ok(expand, 'the expand action is labelled as expanding what was returned');
  assert.ok(!search.elements.some(element => element.type === 'button'
    && element.children.some(child => String(child).includes('查看上下文'))),
    'no action may be labelled "上下文", which reads as fetching it');
  assert.match(search.text, /未读取原文/);
  assert.match(search.text, /本面板不发起任何请求/);

  const read = render(sourceCardComponent(), { block: settled([card({ citationOrigin: 'read' })]) });
  assert.match(read.text, /本次读取返回的窗口/);
  assert.match(read.text, /继续读取需主 Agent 再次调用 knowledge_read/);
  assert.doesNotMatch(read.text, /未读取原文/);
});

test('the excerpt is rendered as text, never as HTML', () => {
  assert.ok(!/dangerouslySetInnerHTML|innerHTML|outerHTML|insertAdjacentHTML|document\.write/.test(sourceText),
    'the card must not have an HTML sink');
});

// ------------------------------------------------------------------ no requests, no secrets

/** The bundle with comments removed: the forbidden-token scan must read code, not prose about code. */
const sourceCode = sourceText.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

test('the bundle only fetches the host panel bridge and holds no service secret', () => {
  for (const forbidden of ['XMLHttpRequest', 'localStorage', 'sessionStorage', 'document.cookie',
    'Authorization', 'Bearer', 'DSH_KNOWLEDGE_TOKEN', 'http://', 'https://', 'WebSocket', 'EventSource',
    'process.env', 'openFile']) {
    assert.ok(!sourceCode.includes(forbidden), `the browser half must not contain ${forbidden}`);
  }
  const fetches = [...sourceCode.matchAll(/fetch\(([^,]+)/g)].map(match => match[1]);
  // Every browser request goes to the host's own panel bridge, which holds the credential; the browser half
  // never addresses the service itself. The count is asserted so a new request cannot be added silently.
  assert.equal(fetches.length, 7);
  assert.ok(fetches.every(value => value.startsWith("'/knowledge-panel?sessionId='")));
  // The prose may explain the boundary; the code is what must not cross it.
  assert.match(sourceText, /Bearer token/, 'the reason the panel holds no token is documented');
});

test('clicking every action issues no request and injects nothing into the conversation', () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (...args) => { calls.push(args); throw new Error('the card must not fetch'); };
  let text;
  try {
    const SourceCard = sourceCardComponent();
    const props = { block: settled([card(), card({ title: 'B' })]), callId: 'c1' };
    const first = render(SourceCard, props);
    const buttons = first.elements.filter(element => element.type === 'button');
    assert.ok(buttons.length >= 3, 'the card offers its actions');
    for (const button of buttons) if (typeof button.props.onClick === 'function') button.props.onClick();
    // Under the toggled state both panels are open; nothing was fetched to produce them.
    const opened = render(SourceCard, props);
    text = opened.text;
    // Clicking again closes them, which must be just as inert.
    for (const button of opened.elements.filter(element => element.type === 'button')) {
      if (typeof button.props.onClick === 'function' && button.props.disabled !== true) button.props.onClick();
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.deepEqual(calls, [], 'no action may reach the network');
  assert.match(text, /不向服务发起任何请求/);
});

test('the candidate panel shows every candidate with its reasons, and still sends nothing', () => {
  const SourceCard = sourceCardComponent();
  const props = {
    callId: 'c1',
    block: settled([
      card({ title: 'First', matched: ['doi-exact'], mismatched: [] }),
      card({ title: 'Second', matched: ['title-partial'], mismatched: ['year-mismatch'],
        conflicts: [{ kind: 'year', detail: 'the two copies disagree' }],
        status: { identity: 'conflict', identityDetail: null, locationVerified: false, support: 'not-assessed' } }),
    ]),
  };
  const first = render(SourceCard, props);
  // Both candidates are on screen from the start: the card never picks one for the reader.
  assert.match(first.text, /First/);
  assert.match(first.text, /Second/);
  assert.match(first.text, /查看候选差异（2）/);
  click(first, '查看候选差异');
  const opened = render(SourceCard, props);
  assert.match(opened.text, /不自动选取第一个/);
  assert.match(opened.text, /doi-exact/);
  assert.match(opened.text, /year-mismatch/);
  assert.match(opened.text, /冲突记录/);
});

test('the original-file action is visible but disabled, and says why', () => {
  const { elements } = render(sourceCardComponent(), { block: settled([card()]) });
  const openButton = elements.find(element => element.type === 'button'
    && element.children.some(child => String(child).includes('打开原件')));
  assert.ok(openButton, 'the action stays visible so its absence is not read as a missing file');
  assert.equal(openButton.props.disabled, true, 'it must not pretend to open anything');
  // The label and tooltip must name the real blocker, so a reader is not left guessing whether this is
  // unfinished work or a host limitation.
  assert.match(String(openButton.children[0]), /宿主不支持/);
  assert.match(String(openButton.props.title), /open-in-app/);
  assert.match(String(openButton.props.title), /目录/);
  assert.match(String(openButton.props.title), /不安全绕过/);
});

// DSH 0.2 toolview may omit the journal's outer error envelope; the plugin's persisted
// fixed diagnostic remains available as text. No arbitrary error prose is displayed.
test('a thrown failure without the outer error envelope retains its closed code and correlation', () => {
  const Component = sourceCardComponent();
  for (const code of ['KNOWLEDGE_QUERY_BUSY','KNOWLEDGE_RATE_LIMITED','KNOWLEDGE_QUERY_NOT_TERMINAL']) {
    const block = failed(code);
    block.error = undefined;
    block.content = [{type:'text',text:'Error: secret upstream prose\nKNOWLEDGE_FAILURE_FACTS='+JSON.stringify({code,reference:'k-test1234',detail:{httpStatus:429,serviceCode:'QUERY_BUSY'}})}];
    const value = render(Component,{block});
    assert.ok(value.text.includes(code));
    assert.ok(value.text.includes('k-test1234'));
    assert.ok(!value.text.includes('secret upstream prose'));
  }
});

test('an unconfirmed cancellation card does not encourage immediate resubmission',()=>{
 const value=render(sourceCardComponent(),{block:stopped({code:'KNOWLEDGE_REQUEST_CANCELLED',reference:'k-test1234',detail:{query:{requestId:'kq-abcdef0123456789',phase:'cancelling',jobState:'running',cancellationConfirmed:false}}})});
 assert.ok(value.text.includes('不要立即重发'));assert.ok(!value.text.includes('需要该结果时重新发起调用'));
});
