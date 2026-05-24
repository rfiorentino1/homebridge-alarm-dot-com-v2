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

class UiServer extends HomebridgePluginUiServer {
  constructor() {
    super();

    this.onRequest('/discover', (p) => this.runHelper({ action: 'discover', ...p }));
    this.onRequest('/request-otp', (p) => this.runHelper({ action: 'request_otp', ...p }));
    this.onRequest('/submit-otp', (p) => this.runHelper({ action: 'submit_otp', ...p }));

    this.ready();
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
        env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
      });
      let stdout = '';
      let stderr = '';
      const TIMEOUT_MS = 60_000;
      const timer = setTimeout(() => {
        try { child.kill('SIGKILL'); } catch {}
        reject(new RequestError(`Helper timed out after ${TIMEOUT_MS / 1000}s`, { status: 504 }));
      }, TIMEOUT_MS);

      child.stdout.on('data', (b) => { stdout += b.toString(); });
      child.stderr.on('data', (b) => { stderr += b.toString(); });
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
                `Helper returned unparseable output (exit ${code}): ${trimmedOut.slice(0, 400)} — stderr: ${stderr.slice(0, 200)}`,
                { status: 500 },
              ),
            );
            return;
          }
        }
        reject(
          new RequestError(
            `Helper produced no output (exit ${code}). stderr: ${stderr.slice(0, 400) || '(empty)'}`,
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
