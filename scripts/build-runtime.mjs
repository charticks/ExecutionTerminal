#!/usr/bin/env node
/**
 * Assemble the private Python runtime that ships inside Charticks.
 *
 * Charticks' trading engine is a Python sidecar. Without this, the installer
 * shipped only `.py` source and the app spawned a bare `python` from PATH — so
 * on a machine without Python it started, sat on "Connecting…", and did
 * nothing. This produces a self-contained interpreter + packages that
 * electron-builder ships into `resources/python`, so nothing has to be
 * installed on the target machine.
 *
 *     npm run build:runtime          assemble (skips if already present)
 *     npm run build:runtime -- --force   rebuild from scratch
 *
 * Approach: python.org's **embeddable** distribution plus `pip install
 * --target`. Deliberately NOT PyInstaller — freezing rewrites how imports
 * resolve, and breeze_connect does a bare `import config` that already needs a
 * sys.modules shim (services/feeds/icici_feed.py). Keeping real files on disk
 * means imports behave exactly as they do in development.
 *
 * The host interpreter must match the bundled one (3.12, 64-bit) because its
 * pip resolves the wheels.
 */
import { execFileSync, spawnSync } from "node:child_process";
import {
  existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const RUNTIME_DIR = join(ROOT, "charticks", "runtime", "python");
const SITE_PACKAGES = join(RUNTIME_DIR, "Lib", "site-packages");
const REQUIREMENTS = join(ROOT, "scripts", "runtime-requirements.txt");
const force = process.argv.includes("--force");

const log = (msg) => console.log(`[runtime] ${msg}`);
const fail = (msg) => { console.error(`[runtime] ERROR: ${msg}`); process.exit(1); };

function hostPython() {
  for (const candidate of ["python", "python3", "py"]) {
    const probe = spawnSync(candidate, ["-c",
      "import sys,platform;print('%d.%d.%d' % sys.version_info[:3]);print(platform.architecture()[0])"],
      { encoding: "utf-8" });
    if (probe.status === 0) {
      const [version, arch] = probe.stdout.trim().split(/\r?\n/);
      return { exe: candidate, version, arch };
    }
  }
  return fail("no Python on PATH. A host Python 3.12 (64-bit) is needed to " +
              "resolve the wheels that get bundled.");
}

function dirSize(dir) {
  let total = 0;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    total += entry.isDirectory() ? dirSize(path) : statSync(path).size;
  }
  return total;
}

function prunePycache(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "__pycache__") rmSync(path, { recursive: true, force: true });
      else prunePycache(path);
    } else if (entry.name.endsWith(".pyc")) {
      rmSync(path, { force: true });
    }
  }
}

/**
 * Extract a zip without adding a dependency.
 *
 * Explicitly the Windows system tar (bsdtar), not whatever `tar` resolves to:
 * when this runs under Git Bash, `tar` is the MSYS build, which reads
 * `C:\path` as a remote host spec and fails with "Cannot connect to C".
 * PowerShell's Expand-Archive is the fallback for anything older.
 */
function extract(zip, dest) {
  const systemTar = join(process.env.SystemRoot || "C:\\Windows", "System32", "tar.exe");
  if (process.platform === "win32" && existsSync(systemTar)) {
    const result = spawnSync(systemTar, ["-xf", zip, "-C", dest], { stdio: "inherit" });
    if (result.status === 0) return;
    log("system tar failed — falling back to Expand-Archive");
  }
  if (process.platform === "win32") {
    const result = spawnSync("powershell", [
      "-NoProfile", "-NonInteractive", "-Command",
      `Expand-Archive -LiteralPath '${zip}' -DestinationPath '${dest}' -Force`,
    ], { stdio: "inherit" });
    if (result.status !== 0) fail("could not extract the Python archive");
    return;
  }
  execFileSync("tar", ["-xf", zip, "-C", dest], { stdio: "inherit" });
}

async function download(url, dest) {
  log(`downloading ${url}`);
  const response = await fetch(url);
  if (!response.ok) fail(`download failed (${response.status}) — ${url}`);
  writeFileSync(dest, Buffer.from(await response.arrayBuffer()));
}

/**
 * The embeddable distribution ships isolated: no site-packages on the path.
 * `pythonNNN._pth` has to opt back in, or every bundled package is invisible.
 */
function enableSitePackages() {
  const pth = readdirSync(RUNTIME_DIR).find((f) => /^python\d+\._pth$/.test(f));
  if (!pth) fail("no pythonNNN._pth in the embeddable distribution");
  const path = join(RUNTIME_DIR, pth);
  const lines = readFileSync(path, "utf-8").split(/\r?\n/);
  const kept = lines.filter((l) => l.trim() !== "#import site" && l.trim() !== "import site");
  writeFileSync(path, [...kept, "Lib\\site-packages", "import site", ""].join("\n"), "utf-8");
  log(`patched ${pth} to load Lib\\site-packages`);
}

// Kotak Neo is not published to PyPI — `pip install neo-api-client` fails with
// "No matching distribution found". It exists only on GitHub.
const KOTAK_SDK = "git+https://github.com/Kotak-Neo/Kotak-neo-api-v2.git@v2.0.1";

/**
 * Install Kotak's SDK with --no-deps, deliberately.
 *
 * Its declared dependencies cannot be honoured and should not be:
 *   * `websockets==8.1` contradicts dhanhq (>=12.0.1) and uvicorn[standard]
 *     (>=10.4). Unresolvable as a set. The SDK runs fine on a modern release.
 *   * `asyncio==3.4.3` is a long-dead PyPI backport that would SHADOW the
 *     standard library's asyncio — actively harmful to install.
 *   * `certifi==2022.12.7`, `idna==2.10`, `urllib3==1.26.14` would drag the
 *     TLS stack back years, on the process that talks to brokers.
 *
 * What it genuinely needs is listed in runtime-requirements.txt and installed
 * in the pass above. This mirrors the development environment, which runs the
 * modern versions and works.
 */
function installKotak(pythonExe) {
  log("installing Kotak Neo SDK from GitHub (--no-deps — see comment)");
  const result = spawnSync(pythonExe, [
    "-m", "pip", "install", KOTAK_SDK,
    "--target", SITE_PACKAGES, "--no-deps", "--no-compile", "--upgrade",
  ], { stdio: "inherit" });
  if (result.status !== 0) {
    fail("Kotak SDK install failed. It is a git dependency, so `git` must be " +
         "on PATH on this build machine (testers do not need it).");
  }
}

async function main() {
  if (existsSync(RUNTIME_DIR) && !force) {
    log(`already present at ${RUNTIME_DIR} (${(dirSize(RUNTIME_DIR) / 1e6).toFixed(0)} MB)`);
    log("pass --force to rebuild");
    return;
  }
  const host = hostPython();
  log(`host python ${host.version} ${host.arch} (${host.exe})`);
  const [major, minor] = host.version.split(".");
  if (major !== "3" || Number(minor) < 10) {
    fail(`host python ${host.version} is too old; 3.10+ required`);
  }
  if (host.arch !== "64bit") {
    fail(`host python is ${host.arch}; a 64-bit host is required to resolve ` +
         `win_amd64 wheels`);
  }

  rmSync(RUNTIME_DIR, { recursive: true, force: true });
  mkdirSync(RUNTIME_DIR, { recursive: true });

  // Bundle the SAME version as the host, so the wheels its pip selects are the
  // ones the bundled interpreter can import.
  const url = `https://www.python.org/ftp/python/${host.version}/python-${host.version}-embed-amd64.zip`;
  const zip = join(RUNTIME_DIR, "embed.zip");
  await download(url, zip);
  extract(zip, RUNTIME_DIR);
  rmSync(zip, { force: true });
  enableSitePackages();

  mkdirSync(SITE_PACKAGES, { recursive: true });
  log("installing packages (this is the slow part — a few minutes)");
  const pip = spawnSync(host.exe, [
    "-m", "pip", "install",
    "--requirement", REQUIREMENTS,
    "--target", SITE_PACKAGES,
    // Prefer wheels — the target machine has no toolchain, so anything built
    // from source here has to be pure Python. Not --only-binary: Kotak's SDK
    // is a git reference with no wheel anywhere, and it is pure Python. The
    // compiled packages (pandas, numpy) all publish win_amd64 wheels, so this
    // never silently invokes a compiler.
    "--prefer-binary",
    "--no-compile",
    "--upgrade",
  ], { stdio: "inherit" });
  if (pip.status !== 0) fail("pip install failed — see the output above");

  installKotak(host.exe);
  prunePycache(RUNTIME_DIR);

  // Prove the bundled interpreter can actually import everything the sidecar
  // needs, in isolation from the host environment. A runtime that ships broken
  // is worse than none, because it fails on the tester's machine instead.
  log("verifying the bundled interpreter…");
  const verify = spawnSync(join(RUNTIME_DIR, "python.exe"), ["-c",
    "import fastapi, uvicorn, requests, pyotp, pytz, logzero;" +
    "import SmartApi, dhanhq, neo_api_client, breeze_connect;" +
    "print('imports OK')"],
    { encoding: "utf-8", env: { ...process.env, PYTHONHOME: "", PYTHONPATH: "" } });
  if (verify.status !== 0) {
    fail(`bundled interpreter cannot import the sidecar's dependencies:\n${verify.stderr}`);
  }
  log(verify.stdout.trim());
  log(`done — ${(dirSize(RUNTIME_DIR) / 1e6).toFixed(0)} MB at ${RUNTIME_DIR}`);
}

main().catch((err) => fail(err?.stack || String(err)));
