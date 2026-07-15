'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const readline = require('node:readline');
const { MessageChannel } = require('node:worker_threads');
const { JSDOM, VirtualConsole } = require('jsdom');

const runtimes = new Map();

function define(target, key, value) {
  try {
    Object.defineProperty(target, key, {
      configurable: true,
      enumerable: true,
      value,
    });
  } catch (_error) {
    try {
      target[key] = value;
    } catch (_ignored) {
      // Some jsdom properties are intentionally read-only.
    }
  }
}

function installEnvironment(window, options) {
  const userAgent = options.userAgent;
  define(window.navigator, 'userAgent', userAgent);
  define(window.navigator, 'platform', 'MacIntel');
  define(window.navigator, 'vendor', 'Google Inc.');
  define(window.navigator, 'webdriver', false);
  define(window.navigator, 'hardwareConcurrency', 8);
  define(window.navigator, 'deviceMemory', 8);
  define(window.navigator, 'maxTouchPoints', 0);
  define(window.navigator, 'languages', ['en-US', 'en']);
  define(window.navigator, 'language', 'en-US');
  define(window.navigator, 'pdfViewerEnabled', true);
  define(window.navigator, 'connection', {
    effectiveType: '4g',
    downlink: 10,
    rtt: 50,
    saveData: false,
  });
  define(window.navigator, 'userAgentData', {
    brands: [
      { brand: 'Not_A Brand', version: '8' },
      { brand: 'Chromium', version: '120' },
      { brand: 'Google Chrome', version: '120' },
    ],
    mobile: false,
    platform: 'macOS',
  });
  define(window.navigator, 'permissions', {
    async query() {
      return {
        state: 'prompt',
        onchange: null,
        addEventListener() {},
        removeEventListener() {},
      };
    },
  });

  define(window, 'innerWidth', 1440);
  define(window, 'innerHeight', 900);
  define(window, 'outerWidth', 1440);
  define(window, 'outerHeight', 900);
  define(window, 'devicePixelRatio', 2);
  define(window, 'screenX', 0);
  define(window, 'screenY', 25);
  define(window.screen, 'width', 1440);
  define(window.screen, 'height', 900);
  define(window.screen, 'availWidth', 1440);
  define(window.screen, 'availHeight', 875);
  define(window.screen, 'colorDepth', 24);
  define(window.screen, 'pixelDepth', 24);

  define(window, 'chrome', {
    app: {},
    runtime: {},
    loadTimes() {
      return {};
    },
    csi() {
      return {};
    },
  });
  define(window, 'Notification', function Notification() {});
  window.Notification.permission = 'default';
  define(window, 'matchMedia', (query) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {
      return false;
    },
  }));

  define(window, 'fetch', globalThis.fetch);
  define(window, 'Headers', globalThis.Headers);
  define(window, 'Request', globalThis.Request);
  define(window, 'Response', globalThis.Response);
  define(window, 'AbortController', globalThis.AbortController);
  define(window, 'AbortSignal', globalThis.AbortSignal);
  define(window, 'MessageChannel', MessageChannel);
  define(window, 'TextEncoder', globalThis.TextEncoder);
  define(window, 'TextDecoder', globalThis.TextDecoder);
  define(window, 'crypto', crypto.webcrypto);
  window.crypto.randomUUID = crypto.randomUUID;
  window.document.hasFocus = () => true;

  const originalGetBoundingClientRect = window.Element.prototype.getBoundingClientRect;
  window.Element.prototype.getBoundingClientRect = function getBoundingClientRect() {
    const rect = originalGetBoundingClientRect.call(this);
    return {
      x: rect.x || 0,
      y: rect.y || 0,
      width: rect.width || 100,
      height: rect.height || 20,
      top: rect.top || 0,
      right: rect.right || 100,
      bottom: rect.bottom || 20,
      left: rect.left || 0,
      toJSON() {
        return this;
      },
    };
  };

  const silent = () => {};
  define(window, 'console', {
    log: silent,
    info: silent,
    warn: silent,
    error: silent,
    debug: silent,
    trace: silent,
  });
}

async function createRuntime(options) {
  const virtualConsole = new VirtualConsole();
  const dom = new JSDOM('<!doctype html><html><body></body></html>', {
    url: options.url,
    referrer: options.referrer,
    pretendToBeVisual: true,
    runScripts: 'dangerously',
    userAgent: options.userAgent,
    virtualConsole,
  });
  const { window } = dom;
  installEnvironment(window, options);

  let castleExports;
  window.TURBOPACK = {
    push(definition) {
      const factory = definition && definition[2];
      if (typeof factory !== 'function') return;
      const exports = {};
      factory({ e: true }, {}, exports);
      if (typeof exports.configure === 'function') castleExports = exports;
    },
  };

  const source = fs.readFileSync(options.sdkPath, 'utf8');
  window.eval(source);
  if (!castleExports || typeof castleExports.configure !== 'function') {
    dom.window.close();
    throw new Error('Castle SDK did not export configure()');
  }
  const configured = castleExports.configure({ pk: options.pk });
  if (!configured || typeof configured.createRequestToken !== 'function') {
    dom.window.close();
    throw new Error('Castle SDK did not create a token provider');
  }
  return { dom, configured };
}

async function runtimeFor(options) {
  const key = JSON.stringify([
    options.sdkPath,
    options.pk,
    options.url,
    options.referrer,
    options.userAgent,
  ]);
  let runtime = runtimes.get(key);
  if (!runtime) {
    runtime = await createRuntime(options);
    runtimes.set(key, runtime);
  }
  return runtime;
}

async function createToken(request) {
  const runtime = await runtimeFor(request);
  const timeoutMs = Math.max(1000, Number(request.timeoutMs) || 10000);
  const token = await Promise.race([
    runtime.configured.createRequestToken(),
    new Promise((_resolve, reject) => {
      setTimeout(() => reject(new Error('createRequestToken timeout')), timeoutMs);
    }),
  ]);
  if (typeof token !== 'string' || !token.trim()) {
    throw new Error('Castle returned an empty token');
  }
  return token.trim();
}

async function handleLine(line) {
  let request;
  try {
    request = JSON.parse(line);
    const token = await createToken(request);
    process.stdout.write(`${JSON.stringify({ id: request.id, ok: true, token })}\n`);
  } catch (error) {
    process.stdout.write(
      `${JSON.stringify({
        id: request && request.id,
        ok: false,
        error: String((error && error.message) || error || 'Castle runner failed'),
      })}\n`,
    );
  }
}

const input = readline.createInterface({
  input: process.stdin,
  crlfDelay: Infinity,
});

let queue = Promise.resolve();
input.on('line', (line) => {
  if (!line.trim()) return;
  queue = queue.then(() => handleLine(line));
});

function closeRuntimes() {
  for (const runtime of runtimes.values()) runtime.dom.window.close();
  runtimes.clear();
}

process.on('exit', closeRuntimes);
process.on('SIGTERM', () => {
  closeRuntimes();
  process.exit(0);
});
