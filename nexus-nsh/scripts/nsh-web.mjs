// NSH's web runtime compiles C dependencies and needs a WebAssembly-aware ar.
import { accessSync, constants, existsSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn, execFileSync } from 'node:child_process';

export function webRustEnvironment(env = process.env, run = execFileSync, exists = existsSync) {
  if (env.RUSTC) return env;
  const hasTarget = rustc => {
    const dir = run(rustc, ['--print', 'target-libdir', '--target', 'wasm32-unknown-unknown'], { env, encoding: 'utf8' }).trim();
    return exists(dir);
  };
  try { if (hasTarget('rustc')) return env; } catch { /* Try rustup below. */ }
  try {
    const rustc = run('rustup', ['which', 'rustc'], { env, encoding: 'utf8' }).trim();
    if (hasTarget(rustc)) return { ...env, RUSTC: rustc };
  } catch { /* Let the build report the missing target. */ }
  return env;
}

export function webEnvironment(env = process.env, run = execFileSync, executable = path => {
  try { accessSync(path, constants.X_OK); return true; } catch { return false; }
}) {
  const key = 'AR_wasm32_unknown_unknown';
  // cc-rs accepts either target spelling, then TARGET_AR, then AR.
  if (['AR_wasm32-unknown-unknown', key, 'AR'].some(name => env[name])) return env;
  // NSH otherwise injects its own target-specific value, masking TARGET_AR.
  if (env.TARGET_AR) return { ...env, [key]: env.TARGET_AR };
  const onPath = (env.PATH || '').split(process.platform === 'win32' ? ';' : ':');
  const binary = process.platform === 'win32' ? 'llvm-ar.exe' : 'llvm-ar';
  if (onPath.some(dir => executable(join(dir, binary)))) return env;
  const probe = rustc => {
    const sysroot = run(rustc, ['--print', 'sysroot'], { env, encoding: 'utf8' }).trim();
    const version = run(rustc, ['-vV'], { env, encoding: 'utf8' });
    const host = /^host: (.+)$/m.exec(version)?.[1].trim();
    if (host) {
      const ar = join(sysroot, 'lib', 'rustlib', host, 'bin', binary);
      if (executable(ar)) return ar;
    }
  };
  try {
    const ar = probe(env.RUSTC || 'rustc');
    if (ar) return { ...env, [key]: ar };
  } catch { /* Try the rustup toolchain below. */ }
  try {
    const rustc = run('rustup', ['which', 'rustc'], { env, encoding: 'utf8' }).trim();
    const ar = probe(rustc);
    if (ar) return { ...env, [key]: ar };
  } catch { /* Give an actionable error below instead of a cc-rs build failure. */ }
  throw new Error('NSH web needs llvm-ar. Run `rustup component add llvm-tools` for the active rustup toolchain, or set AR_wasm32_unknown_unknown to a WebAssembly-capable llvm-ar.');
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  try {
    const child = spawn('nsh', process.argv.slice(2), { env: webEnvironment(webRustEnvironment()), stdio: 'inherit' });
    const forward = signal => child.kill(signal);
    const stop = () => forward('SIGTERM');
    const interrupt = () => forward('SIGINT');
    process.on('SIGTERM', stop);
    process.on('SIGINT', interrupt);
    child.on('error', error => { console.error(error.message); process.exitCode = 1; });
    child.on('exit', (code, signal) => {
      process.off('SIGTERM', stop);
      process.off('SIGINT', interrupt);
      if (signal) process.kill(process.pid, signal);
      else process.exitCode = code ?? 1;
    });
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
