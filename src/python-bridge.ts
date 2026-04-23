import { ChildProcessByStdio, spawn } from 'child_process';
import { EventEmitter } from 'events';
import { Readable, Writable } from 'stream';
import { Logger } from 'homebridge';

import type {
  RpcIncoming,
  RpcNotification,
  RpcRequest,
  RpcResponse,
  RpcResponseOk,
} from './types.js';

/**
 * JSON-RPC bridge to the Python daemon (`python/daemon.py`).
 *
 * Wire protocol: each line on stdin/stdout is one JSON-RPC 2.0 message
 * (request, response, or notification). Messages are newline-delimited.
 *
 * Lifecycle:
 *   1. `start()` spawns the daemon subprocess with the provided Python path.
 *   2. `call(method, params)` sends a request and returns a Promise for the result.
 *   3. Notifications from the daemon are emitted as `'notification'` events.
 *   4. `stop()` sends SIGTERM and waits briefly for clean exit.
 *
 * If the subprocess exits unexpectedly, we emit `'exit'` so the platform
 * can decide whether to restart.
 */
export class PythonBridge extends EventEmitter {
  private proc: ChildProcessByStdio<Writable, Readable, Readable> | null = null;
  private nextRequestId = 1;
  private pending = new Map<string, { resolve: (v: unknown) => void; reject: (e: Error) => void }>();
  private stdoutBuffer = '';

  constructor(
    private readonly pythonPath: string,
    private readonly daemonScript: string,
    private readonly daemonArgs: string[],
    private readonly log: Logger,
  ) {
    super();
  }

  /**
   * Spawn the Python daemon. Resolves once the process is up; does NOT wait
   * for the daemon to finish any login/connect work — that's signalled via
   * notifications after.
   */
  start(): void {
    if (this.proc) {
      throw new Error('PythonBridge already started');
    }

    this.log.debug(
      `[python-bridge] spawning: ${this.pythonPath} ${this.daemonScript} ${this.daemonArgs.join(' ')}`,
    );

    const proc = spawn(this.pythonPath, [this.daemonScript, ...this.daemonArgs], {
      stdio: ['pipe', 'pipe', 'pipe'],
    }) as ChildProcessByStdio<Writable, Readable, Readable>;
    this.proc = proc;

    proc.stdout.setEncoding('utf8');
    proc.stdout.on('data', (chunk: string) => this.onStdoutData(chunk));

    proc.stderr.setEncoding('utf8');
    proc.stderr.on('data', (chunk: string) => {
      // Forward stderr at warn level — daemon should prefer structured log notifications.
      for (const line of chunk.split('\n')) {
        if (line.trim()) this.log.warn(`[python-daemon] ${line}`);
      }
    });

    proc.on('exit', (code, signal) => {
      this.log.debug(`[python-bridge] daemon exited code=${code} signal=${signal}`);
      this.proc = null;
      // Reject any still-pending requests.
      for (const { reject } of this.pending.values()) {
        reject(new Error(`Python daemon exited (code=${code}, signal=${signal})`));
      }
      this.pending.clear();
      this.emit('exit', { code, signal });
    });

    proc.on('error', (err) => {
      this.log.error(`[python-bridge] spawn error: ${err.message}`);
      this.emit('error', err);
    });
  }

  /** Send a JSON-RPC request and await a response. */
  call<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T> {
    if (!this.proc || !this.proc.stdin.writable) {
      return Promise.reject(new Error('Python daemon is not running'));
    }

    const id = String(this.nextRequestId++);
    const req: RpcRequest = {
      jsonrpc: '2.0',
      id,
      method,
      ...(params !== undefined && { params }),
    };

    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, {
        resolve: (v) => resolve(v as T),
        reject,
      });
      const payload = JSON.stringify(req) + '\n';
      this.proc!.stdin.write(payload, 'utf8', (err) => {
        if (err) {
          this.pending.delete(id);
          reject(err);
        }
      });
    });
  }

  /**
   * Gracefully stop the daemon. Sends SIGTERM, and escalates to SIGKILL
   * if the process hasn't exited within `graceMs`.
   */
  stop(graceMs = 3000): Promise<void> {
    const proc = this.proc;
    if (!proc) return Promise.resolve();

    return new Promise<void>((resolve) => {
      const onExit = () => {
        clearTimeout(timer);
        resolve();
      };
      proc.once('exit', onExit);
      try {
        proc.kill('SIGTERM');
      } catch {
        // Process may already be dead.
      }
      const timer = setTimeout(() => {
        try {
          proc.kill('SIGKILL');
        } catch {
          // ignore
        }
      }, graceMs);
    });
  }

  private onStdoutData(chunk: string): void {
    this.stdoutBuffer += chunk;
    let newlineAt: number;
    while ((newlineAt = this.stdoutBuffer.indexOf('\n')) !== -1) {
      const line = this.stdoutBuffer.slice(0, newlineAt).trim();
      this.stdoutBuffer = this.stdoutBuffer.slice(newlineAt + 1);
      if (!line) continue;

      let msg: RpcIncoming;
      try {
        msg = JSON.parse(line) as RpcIncoming;
      } catch (err) {
        this.log.warn(`[python-bridge] malformed JSON from daemon: ${line.slice(0, 200)}`);
        continue;
      }

      this.dispatch(msg);
    }
  }

  private dispatch(msg: RpcIncoming): void {
    if ('id' in msg) {
      const pending = this.pending.get(msg.id);
      if (!pending) {
        this.log.warn(`[python-bridge] unexpected response for id=${msg.id}`);
        return;
      }
      this.pending.delete(msg.id);
      const resp = msg as RpcResponse;
      if ('error' in resp) {
        pending.reject(new Error(`${resp.error.code}: ${resp.error.message}`));
      } else {
        pending.resolve((resp as RpcResponseOk).result);
      }
      return;
    }

    const notif = msg as RpcNotification;
    this.emit('notification', notif);
  }
}
