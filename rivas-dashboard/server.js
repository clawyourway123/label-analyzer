const express = require('express');
const http = require('http');
const WebSocket = require('ws');
const path = require('path');
const fs = require('fs');
const compression = require('compression');
const cors = require('cors');
const { execFile } = require('child_process');

const app = express();
const server = http.createServer(app);
const wss = new WebSocket.Server({ server, path: '/ws' });

const PORT = process.env.PORT || 3000;
const GATEWAY_PORT = 18789;
const GATEWAY_WS = `ws://127.0.0.1:${GATEWAY_PORT}`;
const OPENCLAW_DIR = path.join(process.env.HOME, '.openclaw');
const CONFIG_PATH = path.join(OPENCLAW_DIR, 'openclaw.json');
const LOG_PATH = path.join(OPENCLAW_DIR, 'logs', 'gateway.log');
const ERR_LOG_PATH = path.join(OPENCLAW_DIR, 'logs', 'gateway.err.log');
const SESSIONS_DIR = path.join(OPENCLAW_DIR, 'agents', 'main', 'sessions');
const CRON_PATH = path.join(OPENCLAW_DIR, 'cron', 'jobs.json');

app.use(compression());
app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

let metricsCache = { lastUpdate: null };
let gatewayToken = null;
let gatewayWs = null;
let reconnectTimer = null;

function loadGatewayToken() {
  try {
    const config = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
    gatewayToken = config.gateway?.auth?.token || null;
  } catch {}
}

function readJson(p) {
  try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch { return null; }
}

function tailFile(filePath, n = 30) {
  try {
    const lines = fs.readFileSync(filePath, 'utf8').trim().split('\n');
    return lines.slice(-n);
  } catch { return []; }
}

function readCronJobs() {
  const data = readJson(CRON_PATH);
  if (!data?.jobs) return [];

  const sessIndex = readJson(path.join(SESSIONS_DIR, 'sessions.json')) || {};
  const cronSessions = {};
  for (const [key, val] of Object.entries(sessIndex)) {
    if (key.includes(':cron:') && val.label) {
      const jobName = val.label.replace(/^Cron:\s*/, '');
      if (!cronSessions[jobName]) cronSessions[jobName] = [];
      cronSessions[jobName].push({ key, model: val.model, sessionId: val.sessionId });
    }
  }

  return data.jobs.map(j => {
    const activity = [];
    const sessions = cronSessions[j.name] || [];
    for (const sess of sessions.slice(-1)) {
      const files = fs.readdirSync(SESSIONS_DIR).filter(f => f.endsWith('.jsonl') && !f.includes('.reset.'));
      for (const file of files) {
        try {
          const fp = path.join(SESSIONS_DIR, file);
          const stat = fs.statSync(fp);
          if ((Date.now() - stat.mtimeMs) / 3600000 > 6) continue;
          const lines = fs.readFileSync(fp, 'utf8').trim().split('\n');
          const first = JSON.parse(lines[0]);
          if (first.id !== sess.sessionId && !lines[0].includes(j.id)) continue;
          for (const line of lines.slice(-20)) {
            try {
              const d = JSON.parse(line);
              const msg = d.message || {};
              if (msg.role === 'assistant' && msg.model !== 'delivery-mirror') {
                const content = typeof msg.content === 'string' ? msg.content.slice(0, 80) : '';
                activity.push({ type: 'response', model: msg.model, cost: msg.usage?.cost?.total || 0, ts: msg.timestamp || d.timestamp, preview: content });
              } else if (msg.role === 'toolResult') {
                activity.push({ type: 'tool', name: msg.name || msg.toolName || '?', ts: msg.timestamp || d.timestamp });
              }
            } catch {}
          }
        } catch {}
      }
    }

    return {
      id: j.id,
      name: j.name,
      enabled: j.enabled !== false,
      model: j.payload?.model || 'default',
      schedule: j.schedule?.kind === 'cron' ? j.schedule.expr : `every ${Math.round((j.schedule?.everyMs || 0) / 60000)}m`,
      tz: j.schedule?.tz || 'UTC',
      lastStatus: j.state?.lastStatus || 'pending',
      lastError: j.state?.lastError || null,
      lastDurationMs: j.state?.lastDurationMs || null,
      lastRunAt: j.state?.lastRunAtMs || null,
      nextRunAt: j.state?.nextRunAtMs || null,
      consecutiveErrors: j.state?.consecutiveErrors || 0,
      activity: activity.slice(-5)
    };
  });
}

function readSessionCosts() {
  const totals = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0, messages: 0 };
  try {
    const files = fs.readdirSync(SESSIONS_DIR).filter(f => f.endsWith('.jsonl') && !f.includes('.reset.'));
    const today = new Date().toISOString().slice(0, 10);

    for (const file of files) {
      const fp = path.join(SESSIONS_DIR, file);
      const stat = fs.statSync(fp);
      if (stat.mtime.toISOString().slice(0, 10) !== today) continue;

      const lines = fs.readFileSync(fp, 'utf8').trim().split('\n');
      for (const line of lines) {
        try {
          const d = JSON.parse(line);
          if (d.type !== 'message') continue;
          const msg = d.message;
          if (!msg || msg.role !== 'assistant') continue;
          const cost = msg.usage?.cost;
          if (!cost) continue;
          totals.input += cost.input || 0;
          totals.output += cost.output || 0;
          totals.cacheRead += cost.cacheRead || 0;
          totals.cacheWrite += cost.cacheWrite || 0;
          totals.total += cost.total || 0;
          totals.messages++;
        } catch {}
      }
    }
  } catch {}
  return totals;
}

function readModelStatus() {
  const cronJobs = readCronJobs();
  const statuses = {};

  const config = readJson(CONFIG_PATH);
  const models = config?.agents?.defaults?.models || {};
  for (const [id, m] of Object.entries(models)) {
    statuses[id] = { id, alias: m.alias || id.split('/').pop(), status: 'ready', detail: null };
  }

  for (const job of cronJobs) {
    if (job.lastError?.includes('rate_limit')) {
      const matches = job.lastError.match(/([a-z]+\/[\w.-]+)/g) || [];
      for (const m of matches) {
        if (statuses[m]) {
          statuses[m].status = 'rate_limited';
          statuses[m].detail = 'Rate limited (from cron)';
        }
      }
    }
    if (job.lastError?.includes('model not allowed')) {
      const match = job.lastError.match(/model not allowed: (.+)/);
      if (match) {
        const m = match[1];
        statuses[m] = statuses[m] || { id: m, alias: m.split('/').pop(), status: 'not_allowed', detail: null };
        statuses[m].status = 'not_allowed';
        statuses[m].detail = 'Not in model catalog';
      }
    }
  }

  return Object.values(statuses);
}

function toEpochMs(v) {
  if (!v) return 0;
  if (typeof v === 'number') return v > 1e12 ? v : v * 1000;
  const ms = new Date(v).getTime();
  return isNaN(ms) ? 0 : ms;
}

function readActivityFeed() {
  const feed = [];
  try {
    const files = fs.readdirSync(SESSIONS_DIR).filter(f => f.endsWith('.jsonl') && !f.includes('.reset.'));
    for (const file of files) {
      const fp = path.join(SESSIONS_DIR, file);
      const stat = fs.statSync(fp);
      const ageH = (Date.now() - stat.mtimeMs) / 3600000;
      if (ageH > 12) continue;

      const lines = fs.readFileSync(fp, 'utf8').trim().split('\n');
      for (const line of lines.slice(-80)) {
        try {
          const d = JSON.parse(line);
          const msg = d.message || {};
          const ts = toEpochMs(msg.timestamp || d.timestamp);
          if (d.type === 'message') {
            if (msg.role === 'assistant' && msg.model !== 'delivery-mirror') {
              feed.push({ kind: 'response', model: msg.model || '?', provider: msg.provider || '?', cost: msg.usage?.cost?.total || 0, ts });
            } else if (msg.role === 'user') {
              let preview = msg.content || '';
              if (Array.isArray(preview)) preview = (preview[0]?.text || '').slice(0, 80);
              else preview = String(preview).slice(0, 80);
              if (preview.startsWith('Conversation info')) preview = preview.replace(/^Conversation info[^:]*:\s*```json[\s\S]*?```\s*/m, '').slice(0, 80);
              feed.push({ kind: 'user', preview: preview.slice(0, 80), ts });
            } else if (msg.role === 'toolResult') {
              feed.push({ kind: 'tool', name: msg.name || msg.toolName || '?', ts });
            }
          } else if (d.type === 'custom') {
            if (d.customType === 'model-snapshot') feed.push({ kind: 'model_switch', ts });
            else if (d.customType === 'openclaw.cache-ttl') feed.push({ kind: 'cache_prune', ts });
          }
        } catch {}
      }
    }
  } catch {}

  const errLines = tailFile(ERR_LOG_PATH, 40);
  for (const line of errLines) {
    if (line.includes('error') || line.includes('fail') || line.includes('Error')) {
      const tsMatch = line.match(/^(\d{4}-\d{2}-\d{2}T[\d:.Z+-]+)/);
      const msg = line.replace(/^\d{4}-\d{2}-\d{2}T[\d:.Z+-]+\s*/, '').slice(0, 120);
      feed.push({ kind: 'error', message: msg, ts: tsMatch ? toEpochMs(tsMatch[1]) : 0 });
    }
  }

  const gwLines = tailFile(LOG_PATH, 40);
  for (const line of gwLines) {
    const tsMatch = line.match(/^(\d{4}-\d{2}-\d{2}T[\d:.Z+-]+)/);
    if (!tsMatch) continue;
    const ts = toEpochMs(tsMatch[1]);
    const body = line.replace(/^\d{4}-\d{2}-\d{2}T[\d:.Z+-]+\s*/, '');
    if (body.includes('[heartbeat]')) {
      feed.push({ kind: 'cron', message: body.slice(0, 100), ts });
    } else if (body.includes('[cron]')) {
      feed.push({ kind: 'cron', message: body.slice(0, 100), ts });
    } else if (body.includes('[reload]')) {
      feed.push({ kind: 'model_switch', message: body.slice(0, 100), ts });
    } else if (body.includes('errorCode=UNAVAILABLE') || body.includes('models failed')) {
      feed.push({ kind: 'error', message: body.replace(/.*errorMessage=/, '').slice(0, 120), ts });
    }
  }

  feed.sort((a, b) => b.ts - a.ts);

  const deduped = [];
  for (const item of feed) {
    const last = deduped[deduped.length - 1];
    if (last && last.kind === item.kind && Math.abs(last.ts - item.ts) < 3000) continue;
    deduped.push(item);
  }

  const cutoff = Date.now() - 4 * 3600000;
  return deduped.filter(e => e.ts > cutoff).slice(0, 60);
}

function readQueuedEvents() {
  try {
    const { execFileSync } = require('child_process');
    const out = execFileSync('openclaw', ['status', '--json'], { timeout: 8000, encoding: 'utf8' });
    const data = JSON.parse(out);
    return data.queuedSystemEvents || [];
  } catch {
    return [];
  }
}

function buildMetrics() {
  const config = readJson(CONFIG_PATH);
  if (!config) return;

  const defaults = config.agents?.defaults || {};
  const models = defaults.models || {};
  const heartbeat = defaults.heartbeat || {};
  const pruning = defaults.contextPruning || {};

  const modelList = Object.entries(models).map(([id, m]) => ({
    id, alias: m.alias || id.split('/').pop(), cacheRetention: m.params?.cacheRetention || 'none'
  }));

  const optimizations = [];
  if (Object.values(models).some(m => m.params?.cacheRetention))
    optimizations.push({ name: 'Prompt Caching', status: 'active', impact: '~35-40%' });
  if (pruning.mode === 'cache-ttl')
    optimizations.push({ name: 'Context Pruning', status: 'active', impact: '~5-8%' });
  if (defaults.bootstrapMaxChars && defaults.bootstrapMaxChars < 20000)
    optimizations.push({ name: 'Bootstrap Caps', status: 'active', impact: '~2-3%' });
  if (defaults.compaction?.mode === 'safeguard')
    optimizations.push({ name: 'Safeguard Compaction', status: 'active', impact: 'overflow protection' });

  const channels = Object.entries(config.channels || {}).map(([name, ch]) => ({
    name, enabled: ch.enabled !== false, streamMode: ch.streamMode || 'default'
  }));

  const cronJobs = readCronJobs();
  const costs = readSessionCosts();
  const modelStatuses = readModelStatus();
  const queue = readQueuedEvents();

  const recentLogs = tailFile(LOG_PATH, 25).filter(l =>
    l.includes('[heartbeat]') || l.includes('[reload]') || l.includes('[agent') ||
    l.includes('[telegram]') || l.includes('[gateway]') || l.includes('[cron]')
  ).slice(-15);

  const recentErrors = tailFile(ERR_LOG_PATH, 15).filter(l =>
    l.includes('error') || l.includes('fail') || l.includes('Error')
  ).slice(-10);

  const sessionFiles = [];
  try {
    const files = fs.readdirSync(SESSIONS_DIR).filter(f => f.endsWith('.jsonl') && !f.includes('.reset.'));
    for (const f of files) {
      const stat = fs.statSync(path.join(SESSIONS_DIR, f));
      sessionFiles.push({ id: f.replace('.jsonl', ''), size: stat.size, modified: stat.mtime.toISOString() });
    }
    sessionFiles.sort((a, b) => b.modified.localeCompare(a.modified));
  } catch {}

  metricsCache = {
    config: {
      primary: defaults.model?.primary || 'unknown',
      fallbacks: defaults.model?.fallbacks || [],
      bootstrapMaxChars: defaults.bootstrapMaxChars || 20000,
      bootstrapTotalMaxChars: defaults.bootstrapTotalMaxChars || 24000,
      maxConcurrent: defaults.maxConcurrent || 1
    },
    gateway: {
      connected: gatewayWs?.readyState === WebSocket.OPEN,
      port: config.gateway?.port || GATEWAY_PORT,
      mode: config.gateway?.mode || 'local'
    },
    agents: { model: defaults.model?.primary || 'unknown', fallbacks: defaults.model?.fallbacks || [], models: modelList },
    heartbeat: { every: heartbeat.every || 'off', model: heartbeat.model || defaults.model?.primary || 'unknown', target: heartbeat.target || 'none' },
    contextPruning: { mode: pruning.mode || 'off', ttl: pruning.ttl || 'n/a', keepLastAssistants: pruning.keepLastAssistants || 0 },
    channels,
    cronJobs,
    costs,
    modelStatuses,
    queue,
    optimizations,
    sessions: { count: sessionFiles.length, files: sessionFiles.slice(0, 8) },
    recentLogs,
    recentErrors,
    activityFeed: readActivityFeed(),
    lastUpdate: Date.now()
  };
}

function connectToGateway() {
  if (gatewayWs && gatewayWs.readyState === WebSocket.OPEN) return;
  loadGatewayToken();
  if (!gatewayToken) return;

  const url = `${GATEWAY_WS}?token=${gatewayToken}`;
  console.log('[Gateway] Connecting to OpenClaw gateway...');
  try {
    gatewayWs = new WebSocket(url, { headers: { 'Authorization': `Bearer ${gatewayToken}` } });
  } catch { scheduleReconnect(); return; }

  gatewayWs.on('open', () => console.log('[Gateway] Connected to OpenClaw gateway'));
  gatewayWs.on('error', () => {});
  gatewayWs.on('close', (code) => {
    console.log(`[Gateway] Connection closed (code: ${code}). Reconnecting in 30s...`);
    gatewayWs = null;
    scheduleReconnect();
  });
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connectToGateway, 30000);
}

function broadcastToClients(message) {
  const payload = JSON.stringify(message);
  wss.clients.forEach(c => { if (c.readyState === WebSocket.OPEN) c.send(payload); });
}

wss.on('connection', (ws) => {
  console.log('[Dashboard] Client connected');
  ws.send(JSON.stringify({ type: 'metrics', data: metricsCache }));
  ws.on('message', (data) => {
    try {
      const msg = JSON.parse(data.toString());
      if (msg.action === 'refresh') { buildMetrics(); ws.send(JSON.stringify({ type: 'metrics', data: metricsCache })); }
    } catch {}
  });
  ws.on('close', () => console.log('[Dashboard] Client disconnected'));
});

app.get('/api/metrics', (req, res) => { buildMetrics(); res.json(metricsCache); });
app.get('/api/health', (req, res) => res.json({ status: 'ok', gateway: gatewayWs?.readyState === WebSocket.OPEN ? 'connected' : 'filesystem', clients: wss.clients.size, uptime: process.uptime() }));
app.get('*', (req, res) => res.sendFile(path.join(__dirname, 'public', 'index.html')));

buildMetrics();
connectToGateway();

setInterval(() => {
  if (metricsCache) {
    metricsCache.activityFeed = readActivityFeed();
    broadcastToClients({ type: 'metrics', data: metricsCache });
  }
}, 1000);

setInterval(() => { buildMetrics(); broadcastToClients({ type: 'metrics', data: metricsCache }); }, 15000);

server.listen(PORT, () => {
  console.log(`[Server] Rivas Dashboard running on http://localhost:${PORT}`);
  console.log(`[Server] WebSocket endpoint: ws://localhost:${PORT}/ws`);
});

process.on('SIGINT', () => {
  console.log('\n[Server] Shutting down gracefully...');
  clearTimeout(reconnectTimer);
  if (gatewayWs) gatewayWs.close();
  wss.clients.forEach(c => c.close());
  server.close(() => { console.log('[Server] Closed'); process.exit(0); });
});
