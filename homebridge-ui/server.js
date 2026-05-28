// Custom Homebridge UI backend for AlarmDotComV2.
//
// Spawns a short-lived Python helper (python/ui_auth.py) per request to drive
// the Alarm.com login + 2FA flow. The frontend (public/index.js) calls these
// handlers via homebridge.request() and handles the user-facing state machine.

const { HomebridgePluginUiServer, RequestError } = require('@homebridge/plugin-ui-utils');
const { spawn } = require('child_process');
const { existsSync } = require('fs');
const { join } = require('path');

// State directory mirrors what Bootstrap (src/bootstrap.ts) uses to find the
// per-plugin venv. We don't have access to Homebridge's storagePath here at
// require time, but the UI server runtime exposes it via this.homebridgeStoragePath.
const VENV_SUBDIR = ['alarm-dot-com-v2', 'venv', 'bin', 'python'];

// homebridge-config-ui-x captures the UiServer process's stdout/stderr and
// echoes it into the main homebridge log prefixed with "[Homebridge UI]". A
// short, stable prefix here keeps those lines greppable.
const LOG_PREFIX = '[adc-ui]';

function log(...args) {
  // eslint-disable-next-line no-console
  console.log(LOG_PREFIX, ...args);
}
function logWarn(...args) {
  // eslint-disable-next-line no-console
  console.warn(LOG_PREFIX, ...args);
}

class UiServer extends HomebridgePluginUiServer {
  constructor() {
    super();

    this.onRequest('/discover', (p) => this.handle('/discover', { action: 'discover', ...p }));
    this.onRequest('/request-otp', (p) => this.handle('/request-otp', { action: 'request_otp', ...p }));
    this.onRequest('/submit-otp', (p) => this.handle('/submit-otp', { action: 'submit_otp', ...p }));

    this.ready();
    log('UI server ready. storagePath=', this.homebridgeStoragePath);
  }

  resolveVenvPython() {
    const candidate = join(this.homebridgeStoragePath, ...VENV_SUBDIR);
    if (existsSync(candidate)) return candidate;
    return null;
  }

  resolveHelperScript() {
    // server.js lives at <plugin>/homebridge-ui/server.js — helper is at <plugin>/python/ui_auth.py.
    return join(__dirname, '..', 'python', 'ui_auth.py');
  }

  async handle(route, payload) {
    log(`${route} start (action=${payload.action})`);
    const t0 = Date.now();
    try {
      const result = await this.runHelper(payload);
      log(`${route} done in ${Date.now() - t0}ms, ok=${!!(result && result.ok)}`);
      return result;
    } catch (e) {
      logWarn(`${route} failed in ${Date.now() - t0}ms:`, e?.message || e);
      throw e;
    }
  }

  async runHelper(payload) {
    const venvPython = this.resolveVenvPython();
    if (!venvPython) {
      throw new RequestError(
        'The plugin has not been initialized yet. Save any value once (Username/Password) and ' +
          'restart Homebridge so the plugin can bootstrap its Python environment, then return to this screen.',
        { status: 503 },
      );
    }
    const script = this.resolveHelperScript();
    return await new Promise((resolve, reject) => {
      const child = spawn(venvPython, [script], {
        stdio: ['pipe', 'pipe', 'pipe'],
        env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' },
      });
      let stdout = '';
      let stderrCollected = '';
      // Stream stderr to the UI log as it arrives — the helper writes a short
      // tag per phase, so even if it hangs we'll see exactly where.
      child.stderr.on('data', (b) => {
        const chunk = b.toString();
        stderrCollected += chunk;
        chunk.split(/\r?\n/).forEach((line) => {
          if (line.trim()) logWarn('helper:', line);
        });
      });

      const TIMEOUT_MS = 60_000;
      const timer = setTimeout(() => {
        try { child.kill('SIGKILL'); } catch {}
        reject(new RequestError(`Helper timed out after ${TIMEOUT_MS / 1000}s`, { status: 504 }));
      }, TIMEOUT_MS);

      child.stdout.on('data', (b) => { stdout += b.toString(); });
      child.on('error', (err) => {
        clearTimeout(timer);
        reject(new RequestError(`Failed to spawn helper: ${err.message}`, { status: 500 }));
      });
      child.on('close', (code) => {
        clearTimeout(timer);
        const trimmedOut = stdout.trim();
        if (trimmedOut) {
          try {
            const last = trimmedOut.split('\n').pop();
            const parsed = JSON.parse(last);
            resolve(parsed);
            return;
          } catch (e) {
            reject(
              new RequestError(
                `Helper returned unparseable output (exit ${code}): ${trimmedOut.slice(0, 400)} — stderr: ${stderrCollected.slice(0, 200)}`,
                { status: 500 },
              ),
            );
            return;
          }
        }
        reject(
          new RequestError(
            `Helper produced no output (exit ${code}). stderr: ${stderrCollected.slice(0, 400) || '(empty)'}`,
            { status: 500 },
          ),
        );
      });

      child.stdin.write(JSON.stringify(payload));
      child.stdin.end();
    });
  }
}

(() => new UiServer())();
