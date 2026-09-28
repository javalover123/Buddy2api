#!/usr/bin/env node
/**
 * 从运行中的 WorkBuddy AI 客户端读 atRestSecretKey。
 *
 * 原理：客户端主进程以 `--inspect=<port>` 启动时会开一个 CDP 端点，
 * 在它的上下文里求值 `require('electron').workbuddyStorage.loggerGet()` 即可。
 *
 * 用法：node wb_at_rest_probe.cjs [port]
 * 输出：{"atRestSecretKey":"..."} 或 {"error":"..."}（永远 exit 0，由调用方判断）
 *
 * 为什么用 CDP 而不是 NODE_OPTIONS=--require：
 *   Electron 的 preload 阶段 `require('electron')` 还不可用，且主脚本会被
 *   app.asar 的启动流程接管 —— 实测注入拿不到。CDP 直接在已就绪的
 *   主进程上下文里求值，最稳。
 */
'use strict';

const http = require('http');

const PORTS = process.argv[2]
  ? [Number(process.argv[2])]
  : [9229, 9230, 9231, 9232, 8315, 5858];

function getJson(port, path) {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port, path, timeout: 2000 }, (res) => {
      let body = '';
      res.on('data', (c) => (body += c));
      res.on('end', () => {
        try {
          resolve(JSON.parse(body));
        } catch (e) {
          resolve(null);
        }
      });
    });
    req.on('error', () => resolve(null));
    req.on('timeout', () => { req.destroy(); resolve(null); });
  });
}

const EVAL_EXPR = `(() => {
  const out = {};
  try {
    const req = process.mainModule && process.mainModule.require;
    if (!req) return JSON.stringify({ error: 'no require in main context' });
    const el = req('electron');
    const storage = el.workbuddyStorage;
    if (!storage) return JSON.stringify({ error: 'workbuddyStorage not exposed' });
    if (typeof storage.loggerGet !== 'function') {
      return JSON.stringify({ error: 'loggerGet not a function', keys: Object.keys(storage) });
    }
    const raw = storage.loggerGet();
    return typeof raw === 'string' ? raw : JSON.stringify(raw);
  } catch (e) {
    return JSON.stringify({ error: 'evaluate failed: ' + e.message });
  }
})()`;

function evaluate(wsUrl) {
  return new Promise((resolve) => {
    const ws = new WebSocket(wsUrl);
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      try { ws.close(); } catch (e) { /* ignore */ }
      resolve(v);
    };
    const timer = setTimeout(() => finish(null), 8000);
    ws.onerror = () => { clearTimeout(timer); finish(null); };
    ws.onopen = () => {
      ws.send(JSON.stringify({ id: 1, method: 'Runtime.enable', params: {} }));
      ws.send(JSON.stringify({
        id: 2,
        method: 'Runtime.evaluate',
        params: { expression: EVAL_EXPR, returnByValue: true, awaitPromise: true },
      }));
    };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.id !== 2) return;
      clearTimeout(timer);
      const value = msg.result && msg.result.result && msg.result.result.value;
      finish(typeof value === 'string' ? value : null);
    };
  });
}

async function main() {
  for (const port of PORTS) {
    const targets = await getJson(port, '/json/list');
    if (!Array.isArray(targets) || targets.length === 0) continue;
    for (const target of targets) {
      const url = target.webSocketDebuggerUrl;
      if (!url) continue;
      const raw = await evaluate(url);
      if (!raw) continue;
      let payload;
      try { payload = JSON.parse(raw); } catch (e) { continue; }
      if (payload.error) {
        process.stdout.write(JSON.stringify(payload));
        return;
      }
      if (payload.atRestSecretKey) {
        process.stdout.write(JSON.stringify({
          atRestSecretKey: payload.atRestSecretKey,
          version: payload.version,
          port,
        }));
        return;
      }
    }
  }
  process.stdout.write(JSON.stringify({
    error: 'WorkBuddy AI 客户端未在运行（或其调试端口不可用）；请先启动客户端，'
      + '或设置 CB_GATEWAY_WB_AT_REST_KEY 直接提供密钥',
  }));
}

main().catch((e) => {
  process.stdout.write(JSON.stringify({ error: String(e && e.message || e) }));
});
