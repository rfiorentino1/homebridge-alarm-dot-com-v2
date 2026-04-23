import { execFile } from 'child_process';
import { promises as fs } from 'fs';
import { resolve as pathResolve, join as pathJoin } from 'path';
import { promisify } from 'util';

import { Logger } from 'homebridge';

import { MIN_PYTHON_VERSION, PLUGIN_STATE_SUBDIR, PYALARMDOTCOMAJAX_SPEC } from './settings.js';

const execFileAsync = promisify(execFile);

/**
 * Handles first-time and ongoing Python environment setup for the plugin:
 *   - locates a suitable Python 3.13+ binary
 *   - creates a per-plugin virtualenv under the Homebridge storage dir
 *   - pip-installs/upgrades pyalarmdotcomajax as needed
 *
 * The venv is stored outside the plugin's npm install dir so that `npm update`
 * or plugin removal/re-add doesn't destroy it.
 */
export class Bootstrap {
  /** Absolute path to the plugin's state directory (e.g. ~/.homebridge/alarm-dot-com-v2). */
  readonly stateDir: string;
  /** Absolute path to the plugin's private venv. */
  readonly venvDir: string;
  /** Absolute path to the venv's Python binary. */
  readonly venvPython: string;

  constructor(
    homebridgeStorageDir: string,
    private readonly log: Logger,
  ) {
    this.stateDir = pathResolve(homebridgeStorageDir, PLUGIN_STATE_SUBDIR);
    this.venvDir = pathJoin(this.stateDir, 'venv');
    this.venvPython = pathJoin(this.venvDir, 'bin', 'python');
  }

  /**
   * Ensure the venv exists and has pyalarmdotcomajax installed at a compatible version.
   * Returns the absolute path to the venv's python, ready to pass to PythonBridge.
   *
   * @param explicitPython optional override for the system python binary used to create the venv.
   */
  async ensureReady(explicitPython?: string): Promise<string> {
    await fs.mkdir(this.stateDir, { recursive: true });

    const systemPython = explicitPython ?? (await this.findSystemPython());
    this.log.debug(`[bootstrap] system python: ${systemPython}`);

    await this.ensureVenv(systemPython);
    await this.ensurePyalarmdotcomajaxInstalled();

    return this.venvPython;
  }

  /** Locate a `python3.13` (or newer) binary on PATH. Throws with clear guidance if none is found. */
  private async findSystemPython(): Promise<string> {
    const candidates = ['python3.13', 'python3.14', 'python3.15', 'python3'];
    for (const name of candidates) {
      try {
        const { stdout } = await execFileAsync(name, ['--version']);
        const match = stdout.match(/Python (\d+)\.(\d+)/);
        if (!match) continue;
        const [, maj, min] = match;
        if (this.satisfiesMinVersion(Number(maj), Number(min))) {
          return name;
        }
      } catch {
        // Candidate not found; continue.
      }
    }
    throw new Error(
      `Could not find a Python ${MIN_PYTHON_VERSION}+ interpreter on PATH. ` +
        `Install Python ${MIN_PYTHON_VERSION} (e.g. via apt, brew, or python.org) and ` +
        `restart Homebridge. You can also set the "Python executable path" option in the plugin config.`,
    );
  }

  private satisfiesMinVersion(maj: number, min: number): boolean {
    const [reqMaj, reqMin] = MIN_PYTHON_VERSION.split('.').map(Number);
    if (maj > reqMaj) return true;
    if (maj < reqMaj) return false;
    return min >= reqMin;
  }

  private async ensureVenv(systemPython: string): Promise<void> {
    try {
      await fs.access(this.venvPython);
      this.log.debug(`[bootstrap] venv already exists: ${this.venvDir}`);
      return;
    } catch {
      // Need to create it.
    }
    this.log.info(`[bootstrap] creating venv at ${this.venvDir}...`);
    await execFileAsync(systemPython, ['-m', 'venv', this.venvDir]);
  }

  private async ensurePyalarmdotcomajaxInstalled(): Promise<void> {
    this.log.debug(`[bootstrap] ensuring ${PYALARMDOTCOMAJAX_SPEC} is installed in venv`);
    // Upgrade pip first — PyPI's older pips struggle with modern wheels.
    await execFileAsync(this.venvPython, ['-m', 'pip', 'install', '--quiet', '--upgrade', 'pip']);
    // --pre is required because 0.6.x is tagged "beta" on PyPI; it's de-facto stable
    // (Home Assistant pins this same range), so we opt in to prereleases explicitly.
    await execFileAsync(this.venvPython, [
      '-m',
      'pip',
      'install',
      '--quiet',
      '--upgrade',
      '--pre',
      PYALARMDOTCOMAJAX_SPEC,
    ]);
    this.log.debug(`[bootstrap] pyalarmdotcomajax install complete`);
  }
}
