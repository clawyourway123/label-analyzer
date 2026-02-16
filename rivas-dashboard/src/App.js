import React, { useState, useEffect, useRef } from 'react';
import './App.css';

function App() {
  const [metrics, setMetrics] = useState(null);
  const [status, setStatus] = useState('connecting');
  const wsRef = useRef(null);
  const retryRef = useRef(null);

  useEffect(() => {
    let mounted = true;
    function connect() {
      if (!mounted) return;
      const ws = new WebSocket(`ws://${window.location.host}/ws`);
      wsRef.current = ws;
      ws.onopen = () => { if (mounted) setStatus('live'); };
      ws.onclose = () => { if (!mounted) return; setStatus(p => p === 'live' ? 'reconnecting' : p); retryRef.current = setTimeout(connect, 3000); };
      ws.onerror = () => {};
      ws.onmessage = (e) => { try { const m = JSON.parse(e.data); if (m.type === 'metrics' && mounted) { setMetrics(m.data); setStatus('live'); } } catch {} };
    }
    connect();
    return () => { mounted = false; clearTimeout(retryRef.current); if (wsRef.current) wsRef.current.close(); };
  }, []);

  const refresh = () => { if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(JSON.stringify({ action: 'refresh' })); };

  const ago = (ts) => {
    if (!ts) return '\u2014';
    const ms = typeof ts === 'number' ? ts : new Date(ts).getTime();
    const s = Math.floor((Date.now() - ms) / 1000);
    if (s < 0) return 'now';
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m ago`;
  };

  const timeStr = (ts) => {
    if (!ts || ts === 0) return '\u2014';
    const d = new Date(ts);
    if (isNaN(d.getTime())) return '\u2014';
    return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
  };

  const usd = (v) => v != null ? `$${v.toFixed(4)}` : '\u2014';
  const dur = (ms) => { if (!ms) return '\u2014'; if (ms < 60000) return `${(ms/1000).toFixed(0)}s`; return `${(ms/60000).toFixed(1)}m`; };

  const statusBadge = (s) => {
    if (s === 'ok' || s === 'success' || s === 'ready') return 'badge-ok';
    if (s === 'error') return 'badge-err';
    if (s === 'rate_limited') return 'badge-warn';
    return 'badge-pending';
  };

  const feedIcon = (k) => ({ response: '\u2190', user: '\u2192', tool: '\u2699', cache_prune: '\u2702', model_switch: '\u21C4', error: '\u26A0', cron: '\u23F0' }[k] || '\u2022');
  const feedLabel = (item) => ({ response: 'Response', user: 'User Message', cache_prune: 'Cache Prune', model_switch: 'Model Switch', error: 'Error', cron: 'Cron' }[item.kind] || (item.kind === 'tool' ? `Tool: ${item.name}` : item.kind));

  const renderFeedItem = (item, i) => (
    <div key={i} className={`feed-item${item.kind === 'error' ? ' error-item' : ''}`}>
      <div className={`feed-bar ${item.kind}`} />
      <div className="feed-body">
        <div className="feed-top">
          <span className="feed-icon">{feedIcon(item.kind)}</span>
          <span className="feed-label">{feedLabel(item)}</span>
          <span className="feed-time">{timeStr(item.ts)}</span>
        </div>
        <div className="feed-detail">
          {item.kind === 'response' && <><span className="model-tag">{item.model}</span><span className="provider-tag">{item.provider}</span>{item.cost > 0 && <span className="cost-tag">${item.cost.toFixed(4)}</span>}</>}
          {item.kind === 'user' && item.preview}
          {item.kind === 'tool' && <span className="provider-tag">{item.name}</span>}
          {item.kind === 'error' && item.message}
          {item.kind === 'cache_prune' && 'Context TTL cleanup'}
          {item.kind === 'model_switch' && (item.message || 'Model snapshot updated')}
          {item.kind === 'cron' && (item.message || 'Cron event')}
        </div>
      </div>
    </div>
  );

  const feed = metrics?.activityFeed || [];
  const errorCount = feed.filter(f => f.kind === 'error').length;
  const modelColor = (s) => s === 'ready' ? '#10a37f' : s === 'rate_limited' ? '#ffa500' : '#ef4444';

  return (
    <div className="app">
      <header className="header">
        <div className="header-content">
          <div className="title"><span className="icon">&#x1F99E;</span> Rivas Dashboard</div>
          <div className="header-actions">
            <span className={`status-badge ${status === 'live' ? 'connected' : 'disconnected'}`}>
              {status === 'live' ? 'Live' : status === 'connecting' ? 'Connecting...' : 'Reconnecting...'}
            </span>
            <button className="refresh-btn" onClick={refresh}>Refresh</button>
          </div>
        </div>
      </header>

      <div className="layout">
        <main className="main-content">
          {!metrics ? (
            <div className="section" style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <span style={{ color: 'var(--text-secondary)' }}>Connecting to OpenClaw...</span>
            </div>
          ) : (
            <>
              {/* Cost Bar */}
              <div className="section">
                <div className="cost-bar">
                  <div className="cost-item"><span className="cost-label">Spend</span><span className="cost-value">{usd(metrics.costs?.total)}</span></div>
                  <div className="cost-item"><span className="cost-label">Cache Read</span><span className="cost-value secondary blue">{usd(metrics.costs?.cacheRead)}</span></div>
                  <div className="cost-item"><span className="cost-label">Cache Write</span><span className="cost-value secondary">{usd(metrics.costs?.cacheWrite)}</span></div>
                  <div className="cost-item"><span className="cost-label">Input</span><span className="cost-value secondary">{usd(metrics.costs?.input)}</span></div>
                  <div className="cost-item"><span className="cost-label">Output</span><span className="cost-value secondary">{usd(metrics.costs?.output)}</span></div>
                  <div className="cost-item"><span className="cost-label">Msgs</span><span className="cost-value secondary">{metrics.costs?.messages || 0}</span></div>
                </div>
              </div>

              {/* System + Models */}
              <div className="section">
                <div className="section-title">System</div>
                <div className="system-bar" style={{ marginBottom: '0.4rem' }}>
                  <div className="sys-item"><span className="sys-label">Primary</span><span className="sys-value mono">{metrics.config?.primary?.split('/').pop()}</span></div>
                  <div className="sys-item"><span className="sys-label">Fallbacks</span><span className="sys-value mono">{metrics.config?.fallbacks?.map(f => f.split('/').pop()).join(' \u2192 ')}</span></div>
                  <div className="sys-item"><span className="sys-label">Heartbeat</span><span className="sys-value">{metrics.heartbeat?.every || 'off'}</span></div>
                  <div className="sys-item"><span className="sys-label">Sessions</span><span className="sys-value">{metrics.sessions?.count || 0}</span></div>
                  <div className="sys-item"><span className="sys-label">Concurrent</span><span className="sys-value">{metrics.config?.maxConcurrent || 1}</span></div>
                  <div className="sys-item"><span className="sys-label">Pruning</span><span className="sys-value">{metrics.contextPruning?.mode} ({metrics.contextPruning?.ttl})</span></div>
                </div>
                <div className="models-row">
                  {metrics.modelStatuses?.map((m, i) => (
                    <div className="model-chip" key={i}>
                      <div className="dot" style={{ background: modelColor(m.status) }} />
                      <span className="name">{m.alias}</span>
                      <span className="role">{m.id === metrics.config?.primary ? 'PRI' : metrics.config?.fallbacks?.includes(m.id) ? 'FB' : ''}</span>
                    </div>
                  ))}
                </div>
              </div>

              {/* Optimizations row */}
              <div className="section" style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap', padding: '0.35rem 0.75rem' }}>
                {metrics.optimizations?.map((o, i) => (
                  <span key={i} style={{ fontSize: '0.62rem', display: 'flex', alignItems: 'center', gap: '0.25rem' }}>
                    <span style={{ color: 'var(--accent-primary)' }}>{'\u2713'}</span>
                    <span style={{ color: 'var(--text-secondary)' }}>{o.name}</span>
                    <span className="font-mono" style={{ fontSize: '0.58rem' }}>{o.impact}</span>
                  </span>
                ))}
                {metrics.channels?.filter(c => c.enabled).map((ch, i) => (
                  <span key={`ch-${i}`} style={{ fontSize: '0.62rem', display: 'flex', alignItems: 'center', gap: '0.25rem' }}>
                    <span style={{ color: 'var(--accent-blue)' }}>{'\u25CF'}</span>
                    <span style={{ color: 'var(--text-secondary)', textTransform: 'capitalize' }}>{ch.name}</span>
                  </span>
                ))}
              </div>

              {/* Cron Jobs - fills remaining space */}
              <div className="cron-section">
                <div className="section-title">Cron Jobs ({metrics.cronJobs?.length || 0})</div>
                <div className="cron-grid">
                  {metrics.cronJobs?.length > 0 ? metrics.cronJobs.map((j, i) => (
                    <div className="cron-card" key={i}>
                      <div className="cron-header">
                        <span className="cron-name">{j.name}</span>
                        <span className={`badge ${statusBadge(j.lastStatus)}`}>{j.lastStatus || 'pending'}</span>
                      </div>
                      <div className="cron-meta">
                        <span>Model <span className="val">{j.model?.split('/').pop()}</span></span>
                        <span>Schedule <span className="val">{j.schedule}</span></span>
                        <span>Last <span className="val">{j.lastRunAt ? timeStr(j.lastRunAt) : '\u2014'}</span></span>
                        <span>Next <span className="val">{j.nextRunAt ? timeStr(j.nextRunAt) : '\u2014'}</span></span>
                        <span>Duration <span className="val">{dur(j.lastDurationMs)}</span></span>
                        {j.consecutiveErrors > 0 && <span style={{ color: '#ef4444' }}>{j.consecutiveErrors}x errors</span>}
                      </div>
                      {j.activity?.length > 0 && (
                        <div className="cron-activity">
                          {j.activity.map((a, ai) => (
                            <div className="cron-act-item" key={ai}>
                              <span className="act-icon">{a.type === 'tool' ? '\u2699' : '\u2190'}</span>
                              <span style={{ color: 'var(--text-secondary)', fontSize: '0.57rem', minWidth: '52px', flexShrink: 0, fontVariantNumeric: 'tabular-nums' }}>{timeStr(a.ts)}</span>
                              {a.type === 'tool' ? (
                                <span>Tool: <span className="act-model">{a.name}</span></span>
                              ) : (
                                <><span className="act-model">{a.model}</span>{a.cost > 0 && <span className="act-cost">${a.cost.toFixed(4)}</span>}{a.preview && <span style={{ marginLeft: '0.3rem', opacity: 0.7 }}>{a.preview.slice(0, 50)}</span>}</>
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                      {j.lastError && <div className="cron-error">{j.lastError}</div>}
                    </div>
                  )) : (
                    <div style={{ textAlign: 'center', color: 'var(--text-secondary)', padding: '1rem', fontStyle: 'italic', fontSize: '0.75rem' }}>No cron jobs</div>
                  )}
                </div>
              </div>
            </>
          )}
        </main>

        {/* Activity Feed Sidebar - UNCHANGED size/position */}
        <aside className="activity-sidebar">
          <div className="sidebar-header">
            <h2>Activity Feed</h2>
            <div style={{ display: 'flex', gap: '0.3rem', alignItems: 'center' }}>
              {errorCount > 0 && <span className="sidebar-count" style={{ background: 'rgba(239,68,68,0.15)', color: '#ef4444' }}>{errorCount} err</span>}
              <span className="sidebar-count">{feed.length}</span>
            </div>
          </div>
          <div className="feed-scroll">
            {feed.length > 0 ? feed.map((item, i) => renderFeedItem(item, i)) : (
              <div style={{ padding: '2rem', textAlign: 'center', color: 'var(--text-secondary)', fontStyle: 'italic', fontSize: '0.8rem' }}>No activity yet</div>
            )}
          </div>
        </aside>
      </div>

      <div className="footer">
        <span>Rivas Dashboard &mdash; OpenClaw Monitor</span>
        <span>Port {metrics?.gateway?.port || '18789'} &middot; {metrics?.gateway?.mode || 'local'} &middot; Updated {ago(metrics?.lastUpdate)}</span>
      </div>
    </div>
  );
}

export default App;
