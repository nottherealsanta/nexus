import assert from 'node:assert/strict';
import { test } from 'node:test';
import { webEnvironment } from './nsh-web.mjs';

test('preserves explicit archiver configuration without probing Rust', () => {
  for (const name of ['AR_wasm32-unknown-unknown', 'AR_wasm32_unknown_unknown', 'AR']) {
    const env = { [name]: '/custom/ar' };
    assert.equal(webEnvironment(env, () => assert.fail('must not probe')), env);
  }
});

test('keeps TARGET_AR from being masked by the NSH default', () => {
  assert.deepEqual(webEnvironment({ TARGET_AR: '/custom/ar' }), {
    TARGET_AR: '/custom/ar', AR_wasm32_unknown_unknown: '/custom/ar',
  });
});

test('uses llvm-ar on PATH without probing Rust', () => {
  const env = { PATH: '/tools' };
  assert.equal(webEnvironment(env, () => assert.fail('must not probe'), p => p === '/tools/llvm-ar'), env);
});

test('finds llvm-ar in the active Rust toolchain when PATH lacks it', () => {
  const env = { PATH: '/tools', RUSTC: '/custom/rustc' };
  const run = (command, args, options) => {
    assert.equal(command, '/custom/rustc');
    assert.equal(options.env, env);
    return args[0] === '--print' ? '/rust toolchain\n' : 'rustc 1.99\nhost: aarch64-apple-darwin\n';
  };
  const ar = '/rust toolchain/lib/rustlib/aarch64-apple-darwin/bin/llvm-ar';
  assert.deepEqual(webEnvironment(env, run, p => p === ar), { ...env, AR_wasm32_unknown_unknown: ar });
  assert.equal(env.AR_wasm32_unknown_unknown, undefined);
});

test('finds rustup llvm-ar when PATH rustc uses a different sysroot', () => {
  const env = { PATH: '/tools' };
  const ar = '/rustup/lib/rustlib/aarch64-apple-darwin/bin/llvm-ar';
  const run = (command, args) => {
    if (command === 'rustup') return '/rustup/bin/rustc\n';
    if (args[0] === '-vV') return 'host: aarch64-apple-darwin\n';
    return command === 'rustc' ? '/homebrew\n' : '/rustup\n';
  };
  assert.deepEqual(webEnvironment(env, run, p => p === ar), { ...env, AR_wasm32_unknown_unknown: ar });
});

test('explains how to install a missing archiver before starting the build', () => {
  assert.throws(() => webEnvironment({}, () => '', () => false), /rustup component add llvm-tools/);
});
