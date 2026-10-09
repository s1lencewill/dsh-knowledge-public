/**
 * Drive the real DSH web UI in a headless browser over the Chrome DevTools Protocol.
 *
 * Why this exists: "it compiles" and "the bundle is served" are not the same claim as "the card renders
 * in the real host". This script opens the real page served by the real DSH web profile, clicks real
 * elements with real mouse events, and writes a PNG per step, so the acceptance record is a picture of
 * the running application rather than an assertion about it.
 *
 * It talks CDP over a plain WebSocket (Node 24 has one built in) to avoid a Playwright/Puppeteer
 * dependency. Every action is a real input event dispatched by the browser, not a DOM method call, so
 * what the screenshot shows is what a user would see after that click.
 *
 * Usage:
 *   node scripts/probe-host-panel.mjs --url http://127.0.0.1:18783 \
 *     --out .local/host-probe/shots --report .local/host-probe/host-report.json \
 *     --type "synthetic probe question" --click-text knowledge --wait-ms 8000
 *
 * Exits non-zero when the page reports an uncaught exception, so a silent client-plugin failure cannot
 * be mistaken for a passing run.
 */
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');

// Exported so the argument handling can be tested without launching a browser: the two file-based inputs
// below exist because a shell silently rewrote the inline forms, and a silent rewrite is exactly the kind of
// defect that needs a test rather than a note.
export function parseArgs(argv) {
  const options = { url: 'http://127.0.0.1:18783', out: '.local/host-probe/shots',
    report: '.local/host-probe/host-report.json', port: 0, waitMs: 12000, settleMs: 2500,
    clickText: [], clickSelector: [], type: null, submit: true, enterCount: 1, height: 1000, script: null, scriptFile: null, keepBrowserOpen: false, navigationTimeoutMs: 45000,
    secrets: [], eval: [], evalFiles: [] };
  for (let index = 0; index < argv.length; index += 1) {
    const [flag, inline] = argv[index].split('=');
    const next = () => inline ?? argv[++index];
    switch (flag) {
      case '--url': options.url = next(); break;
      case '--out': options.out = next(); break;
      case '--report': options.report = next(); break;
      case '--port': options.port = Number(next()); break;
      case '--wait-ms': options.waitMs = Number(next()); break;
      case '--settle-ms': options.settleMs = Number(next()); break;
      case '--click-text': options.clickText.push(next()); break;
      // Click by CSS selector, for an element with no stable text (a session row, an icon button) or when
      // the same text appears several times and the first match is the wrong session.
      case '--click-selector': options.clickSelector.push(next()); break;
      case '--type': options.type = next(); break;
      // A taller viewport so a whole card fits in one shot, instead of a screenshot that cuts off the
      // very fields the acceptance is about.
      case '--height': options.height = Number(next()); break;
      case '--no-submit': options.submit = false; break;
      // Slash commands open a suggestion menu: the first Enter accepts the highlighted entry and the
      // second submits the command line. One Enter would only pick the suggestion.
      case '--enter-count': options.enterCount = Number(next()); break;
      // <cmd>[=<value>] joined by ';': type, enter[=n], click-text, click-selector, wait=<ms>, shoot=<name>, eval
      case '--script': options.script = next(); break;
      // The same script, read from a file. A `--script` value travels as one argv entry only when the host
      // shell leaves it alone; Windows PowerShell splits it at the spaces it contains, which silently ran a
      // shortened script (the composer received only the last word). A file has no such problem.
      case '--script-file': options.scriptFile = next(); break;
      case '--navigation-timeout-ms': options.navigationTimeoutMs = Number(next()); break;
      case '--keep-open': options.keepBrowserOpen = true; break;
      // A value that must NOT be reachable from the page. Passed on the command line rather than
      // hard-coded so the check can be re-run against any credential without editing the script.
      case '--check-secret': options.secrets.push(next()); break;
      case '--eval': options.eval.push(next()); break;
      // An expression too large or too punctuation-heavy to survive a shell that mangles quotes. The host
      // shell rewrites `"` inside a native command's arguments, so a multi-line or quote-bearing probe is
      // written to a file and read here instead of being spliced into argv.
      case '--eval-file': options.evalFiles.push(next()); break;
      default: throw new Error(`unknown argument: ${argv[index]}`);
    }
  }
  // After the loop: this used to sit before it, so the option was still unset and the script silently ran empty.
  // One command per line, since a line is the only separator a shell cannot fold into the value; newlines are
  // normalised to the `;` the runner already splits on rather than teaching it a second separator.
  if (options.scriptFile) {
    options.script = readFileSync(resolve(process.cwd(), options.scriptFile), 'utf8')
      .replace(/^\uFEFF/, '').trim().replace(/\r?\n/g, ';');
  }
  return options;
}

function findBrowser() {
  const candidates = [
    process.env.HOST_PROBE_BROWSER,
    `${process.env['ProgramFiles']}\\Microsoft\\Edge\\Application\\msedge.exe`,
    `${process.env['ProgramFiles(x86)']}\\Microsoft\\Edge\\Application\\msedge.exe`,
    `${process.env['ProgramFiles']}\\Google\\Chrome\\Application\\chrome.exe`,
    `${process.env['ProgramFiles(x86)']}\\Google\\Chrome\\Application\\chrome.exe`,
  ].filter(Boolean);
  const found = candidates.find(candidate => existsSync(candidate));
  if (!found) throw new Error('no Chromium-based browser found; set HOST_PROBE_BROWSER');
  return found;
}

const sleep = ms => new Promise(done => setTimeout(done, ms));

/** A minimal CDP client over the browser websocket, with flat session routing. */
class Cdp {
  constructor(socket) {
    this.socket = socket;
    this.sequence = 0;
    this.pending = new Map();
    this.listeners = [];
    socket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (message.id !== undefined) {
        const entry = this.pending.get(message.id);
        if (!entry) return;
        this.pending.delete(message.id);
        if (message.error) entry.reject(new Error(`${entry.method}: ${message.error.message}`));
        else entry.resolve(message.result);
        return;
      }
      for (const listener of this.listeners) listener(message);
    });
  }
  on(listener) { this.listeners.push(listener); }
  send(method, params = {}, sessionId) {
    const id = ++this.sequence;
    return new Promise((resolve_, reject) => {
      this.pending.set(id, { resolve: resolve_, reject, method });
      this.socket.send(JSON.stringify({ id, method, params, ...sessionId ? { sessionId } : {} }));
    });
  }
  close() { try { this.socket.close(); } catch { /* already closed */ } }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const out = resolve(root, options.out);
  const reportPath = resolve(root, options.report);
  mkdirSync(out, { recursive: true });
  const profileDir = resolve(root, '.local/host-probe/browser-profile');
  mkdirSync(profileDir, { recursive: true });

  const debugPort = options.port || (19000 + Math.floor(Math.random() * 900));
  const browser = findBrowser();
  const child = spawn(browser, [
    '--headless=new', '--disable-gpu', '--hide-scrollbars', '--mute-audio',
    '--no-first-run', '--no-default-browser-check', '--disable-extensions', '--disable-sync',
    `--remote-debugging-port=${debugPort}`, `--user-data-dir=${profileDir}`,
    '--window-size=1440,1000', 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });

  const report = { url: options.url, browser, debugPort, steps: [], console: [], exceptions: [],
    requests: [], shots: [], boot: null, bundle: null, ok: false, evaluations: [], secrets: [] };

  let cdp = null;
  let sessionId = null;
  try {
    // Wait for the DevTools endpoint, then connect to the browser target.
    let version = null;
    for (let attempt = 0; attempt < 60 && !version; attempt += 1) {
      try {
        version = await (await fetch(`http://127.0.0.1:${debugPort}/json/version`)).json();
      } catch { await sleep(250); }
    }
    if (!version) throw new Error(`the browser never opened a DevTools endpoint on ${debugPort}`);
    const socket = new WebSocket(version.webSocketDebuggerUrl);
    await new Promise((ready, failed) => {
      socket.addEventListener('open', ready, { once: true });
      socket.addEventListener('error', () => failed(new Error('DevTools websocket failed')), { once: true });
    });
    cdp = new Cdp(socket);

    const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
    const attached = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
    sessionId = attached.sessionId;
    const send = (method, params) => cdp.send(method, params, sessionId);

    cdp.on(message => {
      if (message.sessionId !== sessionId) return;
      if (message.method === 'Runtime.consoleAPICalled') {
        report.console.push({
          type: message.params.type,
          text: (message.params.args ?? []).map(value => value.value ?? value.description ?? value.type).join(' '),
        });
      } else if (message.method === 'Runtime.exceptionThrown') {
        const details = message.params.exceptionDetails;
        report.exceptions.push({
          text: details.exception?.description ?? details.text,
          url: details.url, line: details.lineNumber,
        });
      } else if (message.method === 'Network.requestWillBeSent') {
        report.requests.push(message.params.request.url);
      }
    });

    await send('Page.enable');
    await send('Runtime.enable');
    await send('Network.enable');
    await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: options.height, deviceScaleFactor: 1, mobile: false });

    /** Screenshot the viewport to `out/<name>.png` and record it. */
    const shoot = async (name) => {
      const { data } = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
      const file = resolve(out, `${name}.png`);
      writeFileSync(file, Buffer.from(data, 'base64'));
      report.shots.push({ name, file, bytes: Buffer.from(data, 'base64').byteLength });
      return file;
    };
    /** Evaluate an expression in the page and return its JSON value. */
    const evaluate = async (expression) => {
      const result = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? 'evaluate failed');
      return result.result.value;
    };
    /**
     * Real mouse click at the center of the innermost element whose text contains `label`.
     *
     * Real events on purpose: the browser hit-tests the coordinates, so clicking the innermost matching
     * span still activates whatever control actually owns that point - the same path a user's click
     * takes, instead of calling a DOM method the application may not listen to. `scope` narrows the
     * search when a label appears several times.
     */
    const clickText = async (label, scope = '*') => {
      const rect = await evaluate(`(() => {
        const wanted = ${JSON.stringify(label)};
        const nodes = Array.from(document.querySelectorAll(${JSON.stringify(scope)}))
          .filter(node => !['HTML', 'BODY'].includes(node.tagName))
          .filter(node => (node.innerText || node.textContent || '').includes(wanted));
        const withBox = nodes.map(node => ({ node, box: node.getBoundingClientRect() }))
          .filter(entry => entry.box.width > 0 && entry.box.height > 0)
          // Innermost first: the shortest text that still contains the label.
          .sort((left, right) => (left.node.textContent || '').length - (right.node.textContent || '').length);
        if (withBox.length === 0) return null;
        const { node, box } = withBox[0];
        return { x: box.left + box.width / 2, y: box.top + box.height / 2,
                 tag: node.tagName, text: (node.innerText || node.textContent || '').trim().slice(0, 120) };
      })()`);
      if (!rect) return null;
      for (const type of ['mousePressed', 'mouseReleased']) {
        await send('Input.dispatchMouseEvent', { type, x: rect.x, y: rect.y, button: 'left', clickCount: 1 });
      }
      return rect;
    };

    /** Real mouse click on the first element matching a CSS selector. */
    const clickSelector = async (selector) => {
      const rect = await evaluate(`(() => {
        const node = document.querySelector(${JSON.stringify(selector)});
        if (!node) return null;
        const box = node.getBoundingClientRect();
        if (box.width === 0 || box.height === 0) return null;
        return { x: box.left + box.width / 2, y: box.top + box.height / 2, tag: node.tagName,
                 text: (node.innerText || node.textContent || '').trim().slice(0, 120) };
      })()`);
      if (!rect) return null;
      for (const type of ['mousePressed', 'mouseReleased']) {
        await send('Input.dispatchMouseEvent', { type, x: rect.x, y: rect.y, button: 'left', clickCount: 1 });
      }
      return rect;
    };

    await send('Page.navigate', { url: options.url });
    // Wait for the app to mount: the root element carrying the shell.
    const deadline = Date.now() + options.navigationTimeoutMs;
    let mounted = false;
    while (Date.now() < deadline && !mounted) {
      await sleep(400);
      try {
        mounted = await evaluate(`Boolean(document.querySelector('#root')?.childElementCount)`);
      } catch { /* navigation in flight */ }
    }
    report.mounted = mounted;
    report.title = await evaluate('document.title');
    report.href = await evaluate('location.href');
    // The boot graph is what the host composed for the browser: our plugin must be a row in it.
    report.boot = await evaluate(`(() => {
      const graph = globalThis.__DSH_BOOT__;
      if (!graph) return null;
      const text = JSON.stringify(graph);
      return { present: true, bytes: text.length, mentionsKnowledgePlugin: text.includes('dsh-knowledge'),
               entries: Array.isArray(graph.entries) ? graph.entries.length
                 : (Array.isArray(graph.plugins) ? graph.plugins.length : null) };
    })()`);
    await sleep(options.settleMs);
    await shoot('01-loaded');

    for (const label of options.clickText) {
      const clicked = await clickText(label);
      report.steps.push({ action: 'click', label, clicked });
      await sleep(options.settleMs);
      await shoot(`click-${String(report.steps.length).padStart(2, '0')}-${label.replace(/[^a-z0-9]+/gi, '-').slice(0, 40)}`);
    }

    for (const selector of options.clickSelector) {
      const clicked = await clickSelector(selector);
      report.steps.push({ action: 'click-selector', label: selector, clicked });
      await sleep(options.settleMs);
      await shoot(`select-${String(report.steps.length).padStart(2, '0')}-${selector.replace(/[^a-z0-9]+/gi, '-').slice(0, 40)}`);
    }

    if (options.type) {
      const focused = await evaluate(`(() => {
        const node = document.querySelector('textarea, [contenteditable=true], [role=textbox]');
        if (!node) return null;
        node.focus();
        return node.tagName + (node.getAttribute('contenteditable') ?? '');
      })()`);
      report.steps.push({ action: 'focus-composer', focused });
      await send('Input.insertText', { text: options.type });
      await sleep(500);
      await shoot('typed');
      if (!options.submit) {
        report.steps.push({ action: 'typed-only', submitted: false });
      } else {
        for (let press = 0; press < options.enterCount; press += 1) {
          for (const type of ['keyDown', 'keyUp']) {
            await send('Input.dispatchKeyEvent', { type, key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 });
          }
          await sleep(350);
        }
        report.steps.push({ action: 'submit', key: 'Enter', presses: options.enterCount });
      }
      await sleep(options.waitMs);
      await shoot('submitted');
    }

    if (options.script) {
      let shootIndex = report.shots.length;
      for (const raw of options.script.split(';')) {
        const token = raw.trim();
        if (!token) continue;
        const [command, ...rest] = token.split('=');
        const value = rest.join('=');
        if (command === 'typekeys') {
          await evaluate("(() => { const node = document.querySelector('textarea, [contenteditable=true], [role=textbox]'); if (node) node.focus(); return true; })()");
          for (const character of value) {
            await send('Input.dispatchKeyEvent', { type: 'keyDown', text: character, unmodifiedText: character, key: character });
            await send('Input.dispatchKeyEvent', { type: 'keyUp', key: character });
            await sleep(45);
          }
          await sleep(400);
        } else if (command === 'key') {
          const named = { Escape: { key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 },
                          Tab: { key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 },
                          Backspace: { key: 'Backspace', code: 'Backspace', windowsVirtualKeyCode: 8 } }[value];
          if (!named) {
            report.steps.push({ action: 'script-unknown-key', label: value });
          } else {
            for (const type of ['keyDown', 'keyUp']) await send('Input.dispatchKeyEvent', { type, ...named });
            await sleep(400);
          }
        } else if (command === 'type') {
          await evaluate("(() => { const node = document.querySelector('textarea, [contenteditable=true], [role=textbox]'); if (node) node.focus(); return true; })()");
          await send('Input.insertText', { text: value });
          await sleep(600);
        } else if (command === 'enter') {
          const presses = Number(value) || 1;
          for (let press = 0; press < presses; press += 1) {
            for (const type of ['keyDown', 'keyUp']) {
              await send('Input.dispatchKeyEvent', { type, key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 });
            }
            await sleep(400);
          }
        } else if (command === 'click-text') {
          const clicked = await clickText(value);
          report.steps.push({ action: 'script-click-text', label: value, clicked });
          await sleep(options.settleMs);
        } else if (command === 'click-selector') {
          const clicked = await clickSelector(value);
          report.steps.push({ action: 'script-click-selector', label: value, clicked });
          await sleep(options.settleMs);
        } else if (command === 'wait') {
          await sleep(Number(value) || 1000);
        } else if (command === 'shoot') {
          shootIndex += 1;
          await shoot(String(shootIndex).padStart(2, '0') + '-' + value);
        } else if (command === 'eval') {
          let result;
          try { result = await evaluate(value); } catch (error) { result = { error: String(error?.message ?? error) }; }
          report.evaluations.push({ expression: value, value: result, fromScript: true });
        } else if (command === 'eval-index') {
          // A multi-statement expression (one containing the `;` the mini-script splits on) is passed as a
          // separate `--eval` and referenced by position here. Shells that splice array elements into
          // separate argv entries make a literal `eval=<code>` unusable for anything but a single
          // expression, so the split is deliberate: the code travels as its own argv entry.
          const index = Number(value) - 1;
          const expression = options.eval[index];
          if (typeof expression !== 'string') {
            report.steps.push({ action: 'script-eval-missing', label: value });
          } else {
            let result;
            try { result = await evaluate(expression); } catch (error) { result = { error: String(error?.message ?? error) }; }
            report.evaluations.push({ expression, value: result, fromScript: true, scriptIndex: Number(value) });
          }
        } else if (command === 'eval-file-index') {
          // The same idea as `eval-index`, for an expression that had to travel as a file.
          const index = Number(value) - 1;
          const file = options.evalFiles[index];
          if (typeof file !== 'string') {
            report.steps.push({ action: 'script-eval-file-missing', label: value });
          } else {
            let expression = null;
            try { expression = readFileSync(resolve(process.cwd(), file), 'utf8').replace(/^\uFEFF/, '').trim(); } catch { /* reported below */ }
            let result;
            if (expression === null) result = { error: `unreadable: ${file}` };
            else { try { result = await evaluate(expression); } catch (error) { result = { error: String(error?.message ?? error) }; } }
            report.evaluations.push({ expression: expression ?? file, value: result, fromScript: true, scriptFileIndex: Number(value) });
          }
        } else {
          report.steps.push({ action: 'script-unknown', label: token });
        }
      }
    }

    report.text = await evaluate('document.body.innerText.slice(0, 20000)');

    // The credential check: a protected service token must not be reachable from the page by ANY route
    // the browser can reach - markup, page source, storage, cookies, or a global the bundles left behind.
    report.secrets = await Promise.all(options.secrets.map(async secret => {
      const probe = `(() => {
        const needle = ${JSON.stringify(secret)};
        const haystacks = {
          'document.documentElement.outerHTML': document.documentElement.outerHTML,
          'document.scripts text': Array.from(document.scripts).map(script => script.textContent || '').join('\\n'),
          'location.href': location.href,
          'document.cookie': document.cookie,
        };
        for (let index = 0; index < localStorage.length; index += 1) {
          const key = localStorage.key(index);
          haystacks['localStorage[' + key + ']'] = String(localStorage.getItem(key));
        }
        for (let index = 0; index < sessionStorage.length; index += 1) {
          const key = sessionStorage.key(index);
          haystacks['sessionStorage[' + key + ']'] = String(sessionStorage.getItem(key));
        }
        const boot = JSON.stringify(globalThis.__DSH_BOOT__ ?? null);
        haystacks['__DSH_BOOT__'] = boot;
        const found = Object.entries(haystacks)
          .filter(([, value]) => typeof value === 'string' && value.includes(needle))
          .map(([where]) => where);
        return { length: needle.length, found, leaked: found.length > 0 };
      })()`;
      return await evaluate(probe);
    }));

    // Free-form probes recorded in the report, for checks that only matter to one acceptance run.
    if (!report.evaluations) report.evaluations = [];
    for (const file of options.evalFiles) {
      let expression = null;
      try { expression = readFileSync(resolve(process.cwd(), file), 'utf8').replace(/^\uFEFF/, '').trim(); } catch (error) {
        report.evaluations.push({ expression: file, value: { error: `unreadable: ${error?.message ?? error}` } });
        continue;
      }
      let value;
      try { value = await evaluate(expression); } catch (error) { value = { error: String(error?.message ?? error) }; }
      report.evaluations.push({ expression, value, fromFile: file });
    }
    for (const expression of options.eval) {
      let value;
      try { value = await evaluate(expression); } catch (error) { value = { error: String(error?.message ?? error) }; }
      report.evaluations.push({ expression, value });
    }

    report.ok = report.exceptions.length === 0 && report.secrets.every(entry => entry && entry.leaked === false);
  } catch (error) {
    report.error = String(error?.message ?? error);
  } finally {
    try { if (cdp) cdp.close(); } catch { /* best effort */ }
    if (!options.keepBrowserOpen) {
      try { child.kill(); } catch { /* already gone */ }
    }
    writeFileSync(reportPath, Buffer.from(JSON.stringify(report, null, 2), 'utf8'));
  }
  console.log(JSON.stringify({
    ok: report.ok, mounted: report.mounted, title: report.title, exceptions: report.exceptions.length,
    consoleErrors: report.console.filter(entry => entry.type === 'error').map(entry => entry.text.slice(0, 300)),
    boot: report.boot, steps: report.steps.map(step => ({ action: step.action, label: step.label, clicked: Boolean(step.clicked ?? step.focused) })),
    secrets: report.secrets,
    evaluations: report.evaluations,
    serviceRequests: report.requests.filter(url => /127\.0\.0\.1:1878[12]/.test(url)),
    shots: report.shots.map(shot => shot.file), error: report.error ?? null,
  }, null, 2));
  process.exit(report.ok && report.mounted ? 0 : 1);
}

// Only drive the browser when this file *is* the program. Importing it (the parser test does) must not
// launch anything, or a unit test would open a real page and leave a browser behind.
const invokedDirectly = process.argv[1]
  && resolve(process.argv[1]).toLowerCase() === fileURLToPath(import.meta.url).toLowerCase();
if (invokedDirectly) await main();
