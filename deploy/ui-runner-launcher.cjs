'use strict';
const { spawn } = require('node:child_process');
const fs = require('node:fs');
async function main() {
const inputDeadline = Date.now() + 30000;
while (!fs.existsSync('/work/tests/.ready')) {
  if (Date.now() > inputDeadline) { fs.writeFileSync('/results/.exit-code', '2'); setInterval(() => {}, 1000); return; }
  await new Promise(resolve => setTimeout(resolve, 100));
}
if (process.env.LIEMA_UI_RECORDED === '1') {
  const deadline = Date.now() + 20000;
  while (!fs.existsSync('/tmp/liema-tests/.ready')) {
    if (Date.now() > deadline) { fs.writeFileSync('/results/.exit-code', '2'); setInterval(() => {}, 1000); return; }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
}
const child = spawn(process.execPath, ['/work/node_modules/@playwright/test/cli.js', 'test', '--config=/work/tests/playwright.config.cjs'], {
  detached: true, stdio: 'ignore', env: { ...process.env, CI: '1' }
});
if (child.pid) fs.writeFileSync('/results/.test-pid', String(child.pid));
// Keep tmpfs mounted until the owning Worker copies the artifacts and removes
// this one container. Docker's resource limits apply to all test descendants.
const finish = code => { fs.writeFileSync('/results/.exit-code', String(code)); setInterval(() => {}, 1000); };
child.on('error', () => finish(2));
child.on('exit', code => finish(Number.isInteger(code) ? code : 2));
}
main().catch(() => process.exit(2));
