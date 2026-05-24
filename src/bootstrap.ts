import { execFile, spawn } from 'child_process';
import { createHash } from 'crypto';
import { createWriteStream, promises as fs } from 'fs';
import { tmpdir } from 'os';
import { resolve as pathResolve, join as pathJoin } from 'path';
import { Readable } from 'stream';
import { pipeline } from 'stream/promises';
import { promisify } from 'util';

import { Logger } from 'homebridge';

import {
  MANAGED_PYTHON_RELEASE,
  MANAGED_PYTHON_VERSION,
  MIN_PYTHON_VERSION,
  PLUGIN_STATE_SUBDIR,
  PYALARMDOTCOMAJAX_SPEC,
} from './settings.js';

const execFileAsync = promisify(execFile);

/**
 * Handles first-time and ongoing Python environment setup for the plugin:
 *   - locates a suitable Python 3.13+ binary (system, previously-managed, or
 *     freshly downloaded from python-build-standalone)
 *   - creates a per-plugin virtualenv under the Homebridge storage dir
 *   - pip-installs/upgrades pyalarmdotcomajax as needed
 *
 * The venv (and any managed Python) are stored outside the plugin's npm install
 * dir so that `npm update` or plugin removal/re-add doesn't destroy them.
 */
export class Bootstrap {
  /** Absolute path to the plugin's state directory (e.g. ~/.homebridge/alarm-dot-com-v2). */
  readonly stateDir: string;
  /** Absolute path to the plugin's private venv. */
  readonly venvDir: string;
  /** Absolute path to the venv's Python binary. */
  readonly venvPython: string;
  /** Absolute path to the managed CPython install root, if one is needed. */
  readonly managedPythonDir: string;
  /** Absolute path to the managed CPython binary, if one is needed. */
  readonly managedPython: string;

  constructor(
    homebridgeStorageDir: string,
    private readonly log: Logger,
  ) {
    this.stateDir = pathResolve(homebridgeStorageDir, PLUGIN_STATE_SUBDIR);
    this.venvDir = pathJoin(this.stateDir, 'venv');
    this.venvPython = pathJoin(this.venvDir, 'bin', 'python');
    this.managedPythonDir = pathJoin(this.stateDir, 'python');
    this.managedPython = pathJoin(this.managedPythonDir, 'bin', 'python3.13');
  }

  /**
   * Ensure the venv exists and has pyalarmdotcomajax installed at a compatible version.
   * Returns the absolute path to the venv's python, ready to pass to PythonBridge.
   *
   * @param explicitPython optional override for the system python binary used to create the venv.
   */
  async ensureReady(explicitPython?: string): Promise<string> {
    await fs.mkdir(this.stateDir, { recursive: true });

    const systemPython = explicitPython ?? (await this.findOrInstallPython());
    this.log.debug(`[bootstrap] system python: ${systemPython}`);

    await this.ensureVenv(systemPython);
    await this.ensurePyalarmdotcomajaxInstalled();

    return this.venvPython;
  }

  /**
   * Resolve a Python interpreter satisfying MIN_PYTHON_VERSION. Tries in order:
   *   1. A previously-downloaded managed Python at `<state>/python/bin/python3.13`.
   *   2. A system `python3.13` (or newer) on PATH.
   *   3. Download python-build-standalone and use the binary inside.
   */
  private async findOrInstallPython(): Promise<string> {
    if (await this.pathSatisfiesMinVersion(this.managedPython)) {
      this.log.debug(`[bootstrap] using cached managed python: ${this.managedPython}`);
      return this.managedPython;
    }

    const systemPython = await this.findSystemPython();
    if (systemPython) {
      return systemPython;
    }

    this.log.info(
      `[bootstrap] No system Python ${MIN_PYTHON_VERSION}+ found. Downloading managed CPython ${MANAGED_PYTHON_VERSION} from python-build-standalone (one-time, ~30 MB)…`,
    );
    return this.installManagedPython();
  }

  /** Locate a `python3.13` (or newer) binary on PATH. Returns null if none found. */
  private async findSystemPython(): Promise<string | null> {
    const candidates = ['python3.13', 'python3.14', 'python3.15', 'python3'];
    for (const name of candidates) {
      if (await this.pathSatisfiesMinVersion(name)) {
        return name;
      }
    }
    return null;
  }

  /**
   * Return true if `binary` (a path or PATH-resolved name) is a working Python
   * >= MIN_PYTHON_VERSION.
   */
  private async pathSatisfiesMinVersion(binary: string): Promise<boolean> {
    try {
      const { stdout } = await execFileAsync(binary, ['--version']);
      const match = stdout.match(/Python (\d+)\.(\d+)/);
      if (!match) return false;
      const [, maj, min] = match;
      return this.satisfiesMinVersion(Number(maj), Number(min));
    } catch {
      return false;
    }
  }

  private satisfiesMinVersion(maj: number, min: number): boolean {
    const [reqMaj, reqMin] = MIN_PYTHON_VERSION.split('.').map(Number);
    if (maj > reqMaj) return true;
    if (maj < reqMaj) return false;
    return min >= reqMin;
  }

  /**
   * Download python-build-standalone for the current OS/arch, verify SHA-256
   * against the release's `SHA256SUMS`, extract under `<state>/python/`, and
   * return the path to the resulting python binary.
   */
  private async installManagedPython(): Promise<string> {
    const target = managedPythonTarget();
    const release = MANAGED_PYTHON_RELEASE;
    const version = MANAGED_PYTHON_VERSION;
    const tarballName = `cpython-${version}+${release}-${target}-install_only_stripped.tar.gz`;
    const baseUrl = `https://github.com/astral-sh/python-build-standalone/releases/download/${release}`;
    const tarballUrl = `${baseUrl}/${tarballName}`;
    const sha256SumsUrl = `${baseUrl}/SHA256SUMS`;

    // Fetch the expected SHA-256 from the release's aggregate sums file.
    const expectedSha = await this.fetchExpectedSha256(sha256SumsUrl, tarballName);

    // Download tarball to a tmp file, hashing as we go.
    const tmpTarball = pathJoin(await fs.mkdtemp(pathJoin(tmpdir(), 'hb-adc-py-')), tarballName);
    this.log.debug(`[bootstrap] downloading ${tarballUrl}`);
    const actualSha = await this.downloadWithHash(tarballUrl, tmpTarball);
    if (actualSha !== expectedSha) {
      await fs.rm(tmpTarball, { force: true });
      throw new Error(
        `[bootstrap] managed Python download failed integrity check (expected ${expectedSha}, got ${actualSha})`,
      );
    }

    // Extract into the managed python dir. The install_only tarball contains a
    // top-level `python/` directory with `bin/`, `lib/`, etc. We extract into a
    // staging dir and then move into place atomically-ish.
    //
    // tar may exit non-zero on some hosts (e.g. QNAP's Docker bind-mounted
    // overlay returns EFAULT when tar tries to chmod symlinks inside terminfo).
    // The actual file data extracts fine in those cases, so we ignore tar's
    // exit code and rely on the downstream validation (python --version + venv
    // create + pip install) to catch genuine corruption.
    const stagingDir = await fs.mkdtemp(pathJoin(tmpdir(), 'hb-adc-pyx-'));
    this.log.debug(`[bootstrap] extracting ${tmpTarball} -> ${stagingDir}`);
    await this.extractTarballIgnoringExit(tmpTarball, stagingDir);

    // Remove any pre-existing managed install (e.g. failed prior attempt).
    await fs.rm(this.managedPythonDir, { recursive: true, force: true });
    await fs.mkdir(pathResolve(this.managedPythonDir, '..'), { recursive: true });
    await fs.rename(pathJoin(stagingDir, 'python'), this.managedPythonDir);

    // Clean up tmp.
    await fs.rm(stagingDir, { recursive: true, force: true });
    await fs.rm(pathResolve(tmpTarball, '..'), { recursive: true, force: true });

    if (!(await this.pathSatisfiesMinVersion(this.managedPython))) {
      throw new Error(
        `[bootstrap] downloaded managed Python at ${this.managedPython} did not satisfy ${MIN_PYTHON_VERSION}+`,
      );
    }
    this.log.info(`[bootstrap] managed Python ready at ${this.managedPython}`);
    return this.managedPython;
  }

  private async fetchExpectedSha256(sumsUrl: string, assetName: string): Promise<string> {
    const res = await fetch(sumsUrl);
    if (!res.ok) {
      throw new Error(`[bootstrap] failed to fetch ${sumsUrl}: HTTP ${res.status}`);
    }
    const text = await res.text();
    // Format: "<hash>  <filename>" per line.
    for (const line of text.split('\n')) {
      const m = line.trim().match(/^([0-9a-f]{64})\s+(.+)$/i);
      if (m && m[2] === assetName) {
        return m[1].toLowerCase();
      }
    }
    throw new Error(`[bootstrap] could not find ${assetName} in SHA256SUMS at ${sumsUrl}`);
  }

  /**
   * Run `tar -xzf src -C dest` without throwing on non-zero exit. python-build-
   * standalone tarballs contain many symlinks under `share/terminfo/` and a
   * handful under `bin/`; some container filesystems (notably QNAP's Docker
   * overlay) return EFAULT when tar calls `fchmodat(AT_SYMLINK_NOFOLLOW)` on
   * them, so tar exits 2 even though every file extracted correctly. We log
   * the first lines of stderr if anything was reported, then let downstream
   * validation (python --version, venv create, pip install) decide whether the
   * extraction was actually usable.
   */
  private async extractTarballIgnoringExit(srcTarball: string, destDir: string): Promise<void> {
    return new Promise((resolve, reject) => {
      const child = spawn('tar', ['-xzf', srcTarball, '-C', destDir], {
        stdio: ['ignore', 'ignore', 'pipe'],
      });
      let stderrBuf = '';
      child.stderr.on('data', (chunk: Buffer) => {
        if (stderrBuf.length < 4096) stderrBuf += chunk.toString();
      });
      child.on('error', (err) => reject(err));
      child.on('close', (code) => {
        if (code !== 0) {
          const head = stderrBuf
            .split('\n')
            .slice(0, 3)
            .filter(Boolean)
            .join(' | ');
          this.log.debug(
            `[bootstrap] tar exited ${code} during extract (continuing — downstream validation will catch real corruption). First stderr lines: ${head}`,
          );
        }
        resolve();
      });
    });
  }

  private async downloadWithHash(url: string, destPath: string): Promise<string> {
    const res = await fetch(url, { redirect: 'follow' });
    if (!res.ok || !res.body) {
      throw new Error(`[bootstrap] failed to fetch ${url}: HTTP ${res.status}`);
    }
    const hash = createHash('sha256');
    const sink = createWriteStream(destPath);
    // Tee the body through the hash and into the file.
    const source = Readable.fromWeb(res.body as Parameters<typeof Readable.fromWeb>[0]);
    source.on('data', (chunk: Buffer) => hash.update(chunk));
    await pipeline(source, sink);
    return hash.digest('hex');
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

/**
 * Map Node's `process.platform` + `process.arch` to a python-build-standalone
 * release target string. Throws if the runtime is unsupported by upstream.
 */
function managedPythonTarget(): string {
  const { platform, arch } = process;
  if (platform === 'linux' && arch === 'arm64') return 'aarch64-unknown-linux-gnu';
  if (platform === 'linux' && arch === 'x64') return 'x86_64-unknown-linux-gnu';
  if (platform === 'darwin' && arch === 'arm64') return 'aarch64-apple-darwin';
  if (platform === 'darwin' && arch === 'x64') return 'x86_64-apple-darwin';
  throw new Error(
    `[bootstrap] no managed Python available for ${platform}/${arch}. Install Python ${MIN_PYTHON_VERSION}+ manually and set the "Python executable path" plugin option.`,
  );
}
