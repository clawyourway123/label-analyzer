import React, { useState, useEffect, useRef } from 'react';
import './App.css';

function App() {
  const [metrics, setMetrics] = useState(null);
  const [status, setStatus] = useState('connecting');
  const wsRef = useRef(null);
  const retryRef = useRef(null);
  const feedScrollRef = useRef(null);
  const userScrolledUp = useRef(false);

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

  useEffect(() => {
    const el = feedScrollRef.current;
    if (!el || userScrolledUp.current) return;
    el.scrollTop = el.scrollHeight;
  }, [metrics?.activityFeed]);

  const handleFeedScroll = () => {
    const el = feedScrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    userScrolledUp.current = !atBottom;
  };

  const refresh = () => { if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(JSON.stringify({ action: 'refresh' })); };

  const ago = (ts) => {
    if (!ts) return '';
    const ms = typeof ts === 'number' ? ts : new Date(ts).getTime();
    const s = Math.floor((Date.now() - ms) / 1000);
    if (s < 0) return 'just now';
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  };

  const timeStr = (ts) => {
    if (!ts || ts === 0) return '';
    const d = new Date(ts);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true });
  };

  const usd = (v) => v != null ? `$${v.toFixed(4)}` : '\u2014';
  const statusColor = (s) => s === 'ready' ? 'var(--accent-primary)' : s === 'rate_limited' ? '#ffa500' : '#ef4444';

  const feedIcon = (kind) => {
    switch (kind) {
      case 'response': return '\u2190';
      case 'user': return '\u2192';
      case 'tool': return '\u2699';
      case 'cache_prune': return '\u2702';
      case 'model_switch': return '\u21C4';
      case 'error': return '\u26A0';
      case 'cron': return '\u23F0';
      default: return '\u2022';
    }
  };

  const feedLabel = (item) => {
    switch (item.kind) {
      case 'response': return 'Response';
      case 'user': return 'User Message';
      case 'tool': return `Tool: ${item.name}`;
      case 'cache_prune': return 'Cache Prune';
      case 'model_switch': return 'Model Switch';
      case 'error': return 'Error';
      case 'cron': return 'Cron';
      default: return item.kind;
    }
  };

  const renderFeedItem = (item, i) => {
    const isError = item.kind === 'error';
    return (
      <div key={i} className={`feed-item${isError ? ' error-item' : ''}`}>
        <div className={`feed-bar ${item.kind}`} />
        <div className="feed-body">
          <div className="feed-top">
            <span className="feed-icon">{feedIcon(item.kind)}</span>
            <span className="feed-label">{feedLabel(item)}</span>
            <span className="feed-time">{timeStr(item.ts)}</span>
          </div>
          <div className="feed-detail">
            {item.kind === 'response' && (
              <>
                <span className="model-tag">{item.model}</span>
                <span className="provider-tag">{item.provider}</span>
                {item.cost > 0 && <span className="cost-tag">${item.cost.toFixed(4)}</span>}
              </>
            )}
            {item.kind === 'user' && item.preview}
            {item.kind === 'tool' && <span className="provider-tag">{item.name}</span>}
            {item.kind === 'error' && item.message}
            {item.kind === 'cache_prune' && 'Context TTL cleanup'}
            {item.kind === 'model_switch' && 'Model snapshot updated'}
          </div>
        </div>
      </div>
    );
  };

  const feed = metrics?.activityFeed || [];
  const errorCount = feed.filter(f => f.kind === 'error').length;

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
            <div className="section"><p style={{ textAlign: 'center', color: 'var(--text-secondary)', padding: '3rem' }}>Connecting to OpenClaw gateway...</p></div>
          ) : (
            <>
              {/* Cost Overview */}
              <div className="section overview">
                <div className="section-title">Today's Cost</div>
                <div className="stats-grid">
                  <div className="stat-card">
                    <div className="stat-label">Total Spend</div>
                    <div className="stat-value">{usd(metrics.costs?.total)}</div>
                  </div>
                  <div className="stat-card">
                    <div className="stat-label">Cache Reads</div>
                    <div className="stat-value" style={{ color: 'var(--accent-blue)' }}>{usd(metrics.costs?.cacheRead)}</div>
                  </div>
                  <div className="stat-card">
                    <div className="stat-label">Cache Writes</div>
                    <div className="stat-value" style={{ fontSize: '1.5rem' }}>{usd(metrics.costs?.cacheWrite)}</div>
                  </div>
                  <div className="stat-card">
                    <div className="stat-label">Output</div>
                    <div className="stat-value" style={{ fontSize: '1.5rem' }}>{usd(metrics.costs?.output)}</div>
                  </div>
                  <div className="stat-card">
                    <div className="stat-label">Input</div>
                    <div className="stat-value" style={{ fontSize: '1.5rem' }}>{usd(metrics.costs?.input)}</div>
                  </div>
                  <div className="stat-card">
                    <div className="stat-label">Messages Today</div>
                    <div className="stat-value">{metrics.costs?.messages || 0}</div>
                  </div>
                </div>
              </div>

              {/* Overview Stats */}
              <div className="section">
                <div className="section-title">System</div>
                <div className="stats-grid">
                  <div className="stat-card">
                    <div className="stat-label">Primary Model</div>
                    <div className="stat-value" style={{ fontSize: '1.2rem' }}>{metrics.config?.primary?.split('/').pop() || '\u2014'}</div>
                  </div>
                  <div className="stat-card">
                    <div className="stat-label">Fallbacks</div>
                    <div className="stat-value" style={{ fontSize: '1rem' }}>{metrics.config?.fallbacks?.map(f => f.split('/').pop()).join(' \u2192 ') || 'none'}</div>
                  </div>
                  <div className="stat-card">
                    <div className="stat-label">Heartbeat</div>
                    <div className="stat-value" style={{ fontSize: '1.2rem' }}>{metrics.heartbeat?.every || 'off'}</div>
                  </div>
                  <div className="stat-card">
                    <div className="stat-label">Sessions</div>
                    <div className="stat-value">{metrics.sessions?.count || 0}</div>
                  </div>
                </div>
              </div>

              {/* Model Status */}
              <div className="section">
                <div className="section-title">Model Status</div>
                <div className="table-container">
                  <table className="data-table">
                    <thead><tr><th>Model</th><th>Alias</th><th>Status</th><th>Cache</th><th>Role</th></tr></thead>
                    <tbody>
                      {metrics.modelStatuses?.length > 0 ? metrics.modelStatuses.map((m, i) => (
                        <tr key={i}>
                          <td className="font-mono">{m.id}</td>
                          <td className="font-medium">{m.alias}</td>
                          <td>
                            <span className="priority-badge" style={{ background: `${statusColor(m.status)}15`, color: statusColor(m.status), borderColor: statusColor(m.status) }}>
                              {m.status === 'ready' ? '\u25CF Ready' : m.status === 'rate_limited' ? '\u26A0 Rate Limited' : `\u2717 ${m.status}`}
                            </span>
                          </td>
                          <td>{metrics.agents?.models?.find(x => x.id === m.id)?.cacheRetention !== 'none' ? <span style={{ color: 'var(--accent-primary)' }}>{'\u2713'} long</span> : <span style={{ color: 'var(--text-secondary)' }}>off</span>}</td>
                          <td>{m.id === metrics.config?.primary ? 'Primary' : metrics.config?.fallbacks?.includes(m.id) ? 'Fallback' : 'On-demand'}</td>
                        </tr>
                      )) : <tr><td colSpan="5" className="empty-state">No models</td></tr>}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* Cron Jobs */}
              <div className="section">
                <div className="section-title">Cron Jobs ({metrics.cronJobs?.length || 0})</div>
                <div className="table-container">
                  <table className="data-table">
                    <thead><tr><th>Name</th><th>Model</th><th>Schedule</th><th>Status</th><th>Errors</th><th>Next Run</th></tr></thead>
                    <tbody>
                      {metrics.cronJobs?.length > 0 ? metrics.cronJobs.map((j, i) => (
                        <tr key={i}>
                          <td className="font-medium">{j.name}</td>
                          <td className="font-mono" style={{ fontSize: '0.75rem' }}>{j.model?.split('/').pop()}</td>
                          <td className="font-mono">{j.schedule}</td>
                          <td>
                            <span className="priority-badge" style={{
                              background: j.lastStatus === 'success' ? 'rgba(16,163,127,0.1)' : j.lastStatus === 'error' ? 'rgba(239,68,68,0.1)' : 'rgba(79,159,255,0.1)',
                              color: j.lastStatus === 'success' ? 'var(--accent-primary)' : j.lastStatus === 'error' ? '#ef4444' : 'var(--accent-blue)',
                              borderColor: j.lastStatus === 'success' ? 'var(--accent-primary)' : j.lastStatus === 'error' ? '#ef4444' : 'var(--accent-blue)'
                            }}>
                              {j.lastStatus || 'pending'}
                            </span>
                          </td>
                          <td style={{ color: j.consecutiveErrors > 0 ? '#ef4444' : 'var(--text-secondary)' }}>
                            {j.consecutiveErrors > 0 ? `${j.consecutiveErrors}x` : '\u2014'}
                          </td>
                          <td style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>{j.nextRunAt ? ago(j.nextRunAt) : '\u2014'}</td>
                        </tr>
                      )) : <tr><td colSpan="6" className="empty-state">No cron jobs</td></tr>}
                    </tbody>
                  </table>
                </div>
                {metrics.cronJobs?.filter(j => j.lastError).map((j, i) => (
                  <div key={i} style={{ marginTop: '0.75rem', padding: '0.75rem', background: 'rgba(239,68,68,0.05)', border: '1px solid rgba(239,68,68,0.2)', borderRadius: '0.5rem' }}>
                    <div className="font-medium" style={{ fontSize: '0.8rem', color: '#ef4444', marginBottom: '0.25rem' }}>{j.name}</div>
                    <div className="font-mono" style={{ fontSize: '0.7rem', color: 'var(--text-secondary)', wordBreak: 'break-all' }}>{j.lastError}</div>
                  </div>
                ))}
              </div>

              {/* Queue */}
              <div className="section">
                <div className="section-title">Queue</div>
                {metrics.queue?.length > 0 ? (
                  <div className="table-container">
                    <table className="data-table">
                      <thead><tr><th>Event</th><th>Details</th></tr></thead>
                      <tbody>{metrics.queue.map((q, i) => <tr key={i}><td className="font-medium">{q.type || 'event'}</td><td className="font-mono">{JSON.stringify(q)}</td></tr>)}</tbody>
                    </table>
                  </div>
                ) : (
                  <p style={{ textAlign: 'center', color: 'var(--text-secondary)', padding: '1.5rem', fontStyle: 'italic' }}>Queue empty</p>
                )}
              </div>

              {/* Optimizations + Config */}
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '2rem' }}>
                <div className="section">
                  <div className="section-title">Optimizations</div>
                  <div className="table-container">
                    <table className="data-table">
                      <thead><tr><th>Name</th><th>Status</th><th>Impact</th></tr></thead>
                      <tbody>
                        {metrics.optimizations?.map((o, i) => (
                          <tr key={i}>
                            <td className="font-medium">{o.name}</td>
                            <td><span className="priority-badge" style={{ background: 'rgba(16,163,127,0.1)', color: 'var(--accent-primary)', borderColor: 'var(--accent-primary)' }}>{o.status}</span></td>
                            <td className="font-mono">{o.impact}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>

                <div className="section">
                  <div className="section-title">Config Limits</div>
                  <div className="stats-grid" style={{ gridTemplateColumns: '1fr' }}>
                    <div className="stat-card"><div className="stat-label">Bootstrap Max/File</div><div className="stat-value" style={{ fontSize: '1.5rem' }}>{(metrics.config?.bootstrapMaxChars || 20000).toLocaleString()}</div></div>
                    <div className="stat-card"><div className="stat-label">Bootstrap Total Max</div><div className="stat-value" style={{ fontSize: '1.5rem' }}>{(metrics.config?.bootstrapTotalMaxChars || 24000).toLocaleString()}</div></div>
                    <div className="stat-card"><div className="stat-label">Context Pruning</div><div className="stat-value" style={{ fontSize: '1.2rem' }}>{metrics.contextPruning?.mode} ({metrics.contextPruning?.ttl})</div></div>
                  </div>
                </div>
              </div>

              {/* Channels */}
              <div className="section">
                <div className="section-title">Channels</div>
                <div className="table-container">
                  <table className="data-table">
                    <thead><tr><th>Channel</th><th>Status</th><th>Stream Mode</th></tr></thead>
                    <tbody>
                      {metrics.channels?.map((ch, i) => (
                        <tr key={i}>
                          <td className="font-medium" style={{ textTransform: 'capitalize' }}>{ch.name}</td>
                          <td><span className={`status-badge ${ch.enabled ? 'connected' : 'disconnected'}`}>{ch.enabled ? 'Active' : 'Disabled'}</span></td>
                          <td className="font-mono">{ch.streamMode}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}
        </main>

        {/* Activity Feed Sidebar */}
        <aside className="activity-sidebar">
          <div className="sidebar-header">
            <h2>Activity Feed</h2>
            <div style={{ display: 'flex', gap: '0.4rem', alignItems: 'center' }}>
              {errorCount > 0 && (
                <span className="sidebar-count" style={{ background: 'rgba(239,68,68,0.15)', color: '#ef4444' }}>
                  {errorCount} error{errorCount !== 1 ? 's' : ''}
                </span>
              )}
              <span className="sidebar-count">{feed.length} events</span>
            </div>
          </div>
          <div className="feed-scroll" ref={feedScrollRef} onScroll={handleFeedScroll}>
            {feed.length > 0 ? (
              <>
                {feed.map((item, i) => renderFeedItem(item, i))}
              </>
            ) : (
              <div style={{ padding: '2rem', textAlign: 'center', color: 'var(--text-secondary)', fontStyle: 'italic', fontSize: '0.85rem' }}>
                No activity yet
              </div>
            )}
          </div>
        </aside>
      </div>

      <footer className="footer">
        <div className="footer-content">
          <span>Rivas Dashboard v1.0 &mdash; OpenClaw Monitor</span>
          <span>Port {metrics?.gateway?.port || '18789'} &middot; {metrics?.gateway?.mode || 'local'} &middot; Updated {ago(metrics?.lastUpdate)}</span>
        </div>
      </footer>
    </div>
  );
}

export default App;
