'use strict';
// One disposable X desktop. The Web service authenticates all client traffic;
// this gateway additionally requires a per-session secret, even on its LAN port.
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const net = require('node:net');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const { createRequire } = require('node:module');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function countChecks(source, parse) {
  const tree = parse(source, {ecmaVersion: 'latest', sourceType: 'module'});
  let checks = 0, nodes = 0;
  const pending = [tree];
  while (pending.length) {
    const node = pending.pop();
    if (!node || typeof node !== 'object') continue;
    if (++nodes > 100000) throw new Error('Too many nodes');
    if (node.type === 'CallExpression' && node.callee?.type === 'MemberExpression' &&
        ['toBeVisible', 'toHaveText', 'toContainText', 'toHaveValue', 'toBeChecked', 'toHaveCount'].includes(node.callee.property?.name)) {
      let target = node.callee.object;
      if (target?.type === 'MemberExpression' && target.property?.name === 'not') target = target.object;
      if (target?.type === 'CallExpression' && target.callee?.name === 'expect') checks++;
    }
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) pending.push(...value);
      else if (value && typeof value === 'object') pending.push(value);
    }
  }
  return checks;
}

function createGateway({secret, assetRoot, capture, resume, ready, vncPort = 6081}) {
  const connections = new Set();
  let acceptingInput = true, capturing = false;
  const acceptInput = value => {
    acceptingInput = value;
    for (const [socket, upstream] of connections) {
      if (value) socket.pipe(upstream);
      else socket.unpipe(upstream);
    }
  };
  const authorized = req => {
    const actual = Buffer.from(req.headers.authorization || '');
    const expected = Buffer.from('Bearer ' + secret);
    return actual.length === expected.length && crypto.timingSafeEqual(actual, expected);
  };
  const server = http.createServer(async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    if (!authorized(req)) { res.writeHead(401).end(); return; }
    try {
      if (req.method === 'GET' && req.url === '/health') {
        res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ready: await ready()}));
      } else if (req.method === 'POST' && req.url === '/capture') {
        if (capturing) { res.writeHead(409).end(); return; }
        capturing = true; acceptInput(false);
        try { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(await capture())); }
        catch (error) { acceptInput(true); throw error; }
        finally { capturing = false; }
      } else if (req.method === 'POST' && req.url === '/resume') {
        await resume(); acceptInput(true); res.setHeader('Content-Type', 'application/json'); res.end('{}');
      } else if (req.method === 'GET' && req.url.startsWith('/assets/')) {
        const name = decodeURIComponent(req.url.slice(8));
        if (!/^[a-zA-Z0-9_./-]+\.(js|css)$/.test(name) || name.split('/').includes('..')) throw new Error('Invalid asset');
        const root = fs.realpathSync(assetRoot), file = fs.realpathSync(path.join(root, name));
        if (!file.startsWith(root + path.sep) || fs.statSync(file).size > 2000000) throw new Error('Invalid asset');
        res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : 'text/css');
        res.end(fs.readFileSync(file));
      } else res.writeHead(404).end();
    } catch (_) { res.writeHead(409).end('Recorder operation unavailable'); }
  });
  server.headersTimeout = 5000;
  server.requestTimeout = 10000;
  server.on('upgrade', (req, socket, head) => {
    if (!authorized(req) || req.url !== '/socket' || !acceptingInput) { socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n'); return; }
    const upstream = net.connect(vncPort, '127.0.0.1');
    const pair = [socket, upstream]; connections.add(pair);
    upstream.on('connect', () => {
      // Reconstruct only WebSocket handshake headers. Never forward the secret.
      const headers = ['GET / HTTP/1.1', 'Host: 127.0.0.1:' + vncPort, 'Connection: Upgrade', 'Upgrade: websocket'];
      for (const name of ['sec-websocket-key', 'sec-websocket-version', 'sec-websocket-protocol']) {
        if (req.headers[name]) headers.push(name + ': ' + req.headers[name]);
      }
      upstream.write(headers.join('\r\n') + '\r\n\r\n');
      if (head.length) upstream.write(head);
      if (acceptingInput) socket.pipe(upstream);
      upstream.pipe(socket);
    });
    upstream.on('error', () => socket.destroy());
    socket.on('error', () => upstream.destroy());
    socket.on('close', () => { connections.delete(pair); upstream.destroy(); });
  });
  return server;
}

async function main() {
  // This timer survives a dead Web/Worker process. Docker --init reaps children.
  setTimeout(() => process.exit(0), 900000);
  let initial;
  for (let i = 0; i < 150; i++) {
    if (fs.existsSync('/tmp/recorder-init.json')) {
      initial = JSON.parse(fs.readFileSync('/tmp/recorder-init.json', 'utf8'));
      fs.unlinkSync('/tmp/recorder-init.json'); break;
    }
    await sleep(200);
  }
  if (!initial || !/^[\w-]{40,100}$/.test(initial.secret)) throw new Error('Invalid initialization');
  const url = new URL(initial.url);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('Invalid target');
  const requireFromWork = createRequire('/work/package.json');
  const {parse} = requireFromWork('acorn');
  const children = [];
  let broken = false;
  const launch = (command, args, options = {}) => {
    const child = spawn(command, args, {stdio: 'ignore', env: {...process.env, DISPLAY: ':99'}, ...options});
    children.push(child); child.on('error', () => { broken = true; }); child.on('exit', () => { broken = true; }); return child;
  };
  // No external X listener; all browser/profile/output files live in /tmp tmpfs.
  launch('Xvfb', [':99', '-screen', '0', '1888x805x24', '-nolisten', 'tcp', '-ac']);
  for (let i = 0; i < 100 && !fs.existsSync('/tmp/.X11-unix/X99'); i++) await sleep(100);
  fs.mkdirSync('/tmp/.config/openbox', {recursive: true});
  // Official Linux Chromium uses Chromium-browser for both windows. Inspector
  // changes its title after mapping; match its stable app WM_CLASS instance.
  // 1280x720 page + Chromium's 8x85 window chrome = 1288x805 left pane.
  fs.writeFileSync('/tmp/.config/openbox/rc.xml', `<?xml version="1.0"?>
<openbox_config xmlns="http://openbox.org/3.4/rc"><theme><keepBorder>no</keepBorder></theme><applications>
  <application class="Chromium-browser" type="normal">
    <decor>no</decor><position force="yes"><x>0</x><y>0</y></position>
    <size><width>1288</width><height>805</height></size>
  </application>
  <application class="Chromium-browser" name="text_html,*" type="normal">
    <decor>no</decor><position force="yes"><x>1288</x><y>0</y></position>
    <size><width>600</width><height>805</height></size>
  </application>
</applications></openbox_config>`);
  launch('openbox', ['--config-file', '/tmp/.config/openbox/rc.xml']);
  launch('x11vnc', ['-display', ':99', '-localhost', '-rfbport', '5900', '-nopw', '-forever', '-shared', '-noxdamage', '-quiet']);
  launch('websockify', ['127.0.0.1:6081', '127.0.0.1:5900']);
  const codegen = launch(process.execPath, ['/work/node_modules/@playwright/test/cli.js', 'codegen',
    '--target=playwright-test', '--output=/tmp/recorded.spec.js', '--viewport-size=1280,720',
    ...(initial.proxy ? ['--proxy-server=' + initial.proxy] : []), initial.url], {detached: true});
  initial.url = '';
  const resume = () => { if (codegen.pid) process.kill(-codegen.pid, 'SIGCONT'); };
  const capture = async () => {
    // Official codegen throttles file writes (250 ms). Stop user input first,
    // then allow the final action and pending write to settle before freezing.
    await sleep(800);
    let previous = '', stable = 0;
    for (let i = 0; i < 20 && stable < 2; i++) {
      const stat = fs.statSync('/tmp/recorded.spec.js');
      const current = stat.size + ':' + stat.mtimeMs;
      stable = current === previous ? stable + 1 : 0; previous = current;
      await sleep(250);
    }
    // Freeze codegen and all browser writers while reading the generated file.
    process.kill(-codegen.pid, 'SIGSTOP');
    try {
      const file = '/tmp/recorded.spec.js';
      if (!fs.existsSync(file) || fs.statSync(file).size > 500000) throw new Error('Invalid recording');
      const source = fs.readFileSync(file, 'utf8');
      return {source, checks: countChecks(source, parse)};
    } catch (error) { resume(); throw error; }
  };
  const ready = () => !broken && fs.existsSync('/tmp/recorded.spec.js') && new Promise(resolve => {
    const socket = net.connect(6081, '127.0.0.1'); socket.setTimeout(300);
    socket.once('connect', () => { socket.destroy(); resolve(true); });
    socket.once('error', () => resolve(false)); socket.once('timeout', () => { socket.destroy(); resolve(false); });
  });
  createGateway({secret: initial.secret, assetRoot: '/usr/share/novnc', capture, resume, ready}).listen(6080, '0.0.0.0');
}

module.exports = {countChecks, createGateway};
if (require.main === module) main().catch(() => process.exit(2));
