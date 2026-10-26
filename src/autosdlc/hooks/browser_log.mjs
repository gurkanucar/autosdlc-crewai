// Preloaded into the project's test processes (NODE_OPTIONS=--import) by AutoSDLC. It records what the browser of a
// Playwright UI test saw going wrong (responses of 400 and above, failed requests, console errors, uncaught page
// errors) in the file named by AUTOSDLC_BROWSER_LOG, so a failing UI test is reported with the reason. A test that
// only said "locator timeout" hid a GET /api/api/events answering 404 for a whole run.
// It never changes how a test runs: any problem here leaves Playwright untouched.
import { appendFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';

const logFile = process.env.AUTOSDLC_BROWSER_LOG;

function record(line) {
  try {
    appendFileSync(logFile, `${line.replace(/\s+/g, ' ').slice(0, 500)}\n`);
  } catch {
    // The log is a diagnostic only.
  }
}

function watchContext(context) {
  if (context.__autosdlcWatched) return context;
  context.__autosdlcWatched = true;
  context.on('response', (response) => {
    if (response.status() >= 400) record(`${response.status()} ${response.request().method()} ${response.url()}`);
  });
  context.on('requestfailed', (request) => {
    record(`FAILED ${request.method()} ${request.url()} (${request.failure()?.errorText ?? 'no response'})`);
  });
  const watchPage = (page) => {
    page.on('console', (message) => {
      if (message.type() === 'error') record(`console error: ${message.text()}`);
    });
    page.on('pageerror', (error) => record(`page error: ${error.message}`));
  };
  context.pages().forEach(watchPage);
  context.on('page', watchPage);
  return context;
}

function watchBrowser(browser) {
  const newContext = browser.newContext.bind(browser);
  // newPage() makes its context through this.newContext, so both are covered.
  browser.newContext = async (...args) => watchContext(await newContext(...args));
  return browser;
}

function patch(browserType) {
  const proto = Object.getPrototypeOf(browserType);
  if (!proto || proto.__autosdlcPatched || typeof proto.launch !== 'function') return;
  proto.__autosdlcPatched = true;
  const { launch, launchPersistentContext, connect, connectOverCDP } = proto;
  proto.launch = async function (...args) {
    return watchBrowser(await launch.apply(this, args));
  };
  if (launchPersistentContext) {
    proto.launchPersistentContext = async function (...args) {
      return watchContext(await launchPersistentContext.apply(this, args));
    };
  }
  for (const [name, original] of [['connect', connect], ['connectOverCDP', connectOverCDP]]) {
    if (original) {
      proto[name] = async function (...args) {
        return watchBrowser(await original.apply(this, args));
      };
    }
  }
}

if (logFile) {
  try {
    // The project's own Playwright, resolved from where the tests run; the ESM import of 'playwright' shares these
    // objects, so patching the prototype here reaches it too.
    const require = createRequire(path.join(process.cwd(), 'noop.js'));
    for (const name of ['playwright', 'playwright-core', '@playwright/test']) {
      let resolved;
      try {
        resolved = require.resolve(name);
      } catch {
        continue;
      }
      patch(require(resolved).chromium);
    }
  } catch {
    // Playwright is not installed here or looks different: nothing is recorded.
  }
}
