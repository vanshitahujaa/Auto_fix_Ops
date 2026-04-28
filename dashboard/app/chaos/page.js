'use client';
import { useState } from 'react';
import { api } from '../../lib/api';

const FAULTS = [
  {
    id: 'memory_leak',
    name: 'Memory Leak',
    icon: '🧠',
    desc: 'Triggers /leak endpoint. Allocates memory until OOMKilled.',
    severity: 'HIGH',
  },
  {
    id: 'cpu_spike',
    name: 'CPU Spike',
    icon: '🔥',
    desc: 'Triggers /cpu endpoint. Burns CPU for 60s via tight loops.',
    severity: 'MEDIUM',
  },
  {
    id: 'crash_loop',
    name: 'Crash Loop',
    icon: '💀',
    desc: 'Triggers /crash endpoint. Forces process exit → CrashLoopBackOff.',
    severity: 'HIGH',
  },
];

export default function ChaosPage() {
  const [targetUrl, setTargetUrl] = useState('http://localhost:8080');
  const [confirms, setConfirms] = useState({});
  const [results, setResults] = useState({});
  const [loading, setLoading] = useState({});

  const handleInject = async (faultId) => {
    if (confirms[faultId] !== 'CONFIRM') {
      setResults((r) => ({ ...r, [faultId]: { type: 'error', msg: 'Type CONFIRM to proceed' } }));
      return;
    }

    setLoading((l) => ({ ...l, [faultId]: true }));
    try {
      const res = await api.injectChaos({
        fault_type: faultId,
        target_url: targetUrl,
        confirmation: 'CONFIRM',
      });
      setResults((r) => ({ ...r, [faultId]: { type: 'success', msg: `Injected: ${res.target} → ${res.response_code}` } }));
      setConfirms((c) => ({ ...c, [faultId]: '' }));
    } catch (e) {
      setResults((r) => ({ ...r, [faultId]: { type: 'error', msg: e.message } }));
    }
    setLoading((l) => ({ ...l, [faultId]: false }));
  };

  return (
    <div className="page">
      <div className="page-header">
        <h2 style={{ color: 'var(--error)' }}>⚠️ Chaos Injection</h2>
        <p>Controlled fault injection for testing the remediation pipeline</p>
      </div>

      {/* Warning Banner */}
      <div style={{
        background: 'rgba(220,38,38,0.08)',
        border: '1px solid rgba(220,38,38,0.3)',
        borderRadius: 'var(--radius)',
        padding: '14px 18px',
        marginBottom: 24,
        fontSize: 13,
        color: 'var(--error)',
        display: 'flex',
        alignItems: 'center',
        gap: 10,
      }}>
        <span style={{ fontSize: 18 }}>🚨</span>
        <div>
          <strong>DANGER ZONE</strong> — These actions inject real faults into the target application.
          Only allowed in staging/test namespaces. Each injection is rate-limited to 1 per 30 seconds.
        </div>
      </div>

      {/* Target URL */}
      <div className="card mb-16">
        <div className="form-group" style={{ marginBottom: 0 }}>
          <label>Target Application URL</label>
          <input
            type="text"
            value={targetUrl}
            onChange={(e) => setTargetUrl(e.target.value)}
            placeholder="http://target-app.autofixops.svc:8000"
            style={{ borderColor: 'var(--danger-red)' }}
          />
        </div>
      </div>

      {/* Chaos Cards */}
      <div className="chaos-zone">
        <div className="chaos-header">
          <span>💥</span> Fault Injectors
        </div>
        <div className="chaos-cards">
          {FAULTS.map((fault) => (
            <div className="chaos-card" key={fault.id}>
              <h4>{fault.icon} {fault.name}</h4>
              <p>{fault.desc}</p>
              <div className="severity-row">
                Severity: <span style={{ color: fault.severity === 'HIGH' ? 'var(--error)' : 'var(--warning)', fontWeight: 600 }}>
                  {fault.severity}
                </span>
              </div>
              <div className="confirm-input">
                <input
                  type="text"
                  placeholder='Type "CONFIRM" to unlock'
                  value={confirms[fault.id] || ''}
                  onChange={(e) => setConfirms((c) => ({ ...c, [fault.id]: e.target.value }))}
                />
                <button
                  className="btn btn-danger"
                  disabled={loading[fault.id] || confirms[fault.id] !== 'CONFIRM'}
                  onClick={() => handleInject(fault.id)}
                >
                  {loading[fault.id] ? <span className="loading-spinner" /> : '⚡ Inject Fault'}
                </button>
              </div>
              {results[fault.id] && (
                <div style={{
                  marginTop: 12,
                  padding: '10px 14px',
                  borderRadius: 'var(--radius)',
                  fontSize: 12,
                  fontFamily: 'var(--font-mono)',
                  background: results[fault.id].type === 'success' ? 'var(--success-bg)' : 'var(--error-bg)',
                  color: results[fault.id].type === 'success' ? 'var(--success)' : 'var(--error)',
                  border: `1px solid ${results[fault.id].type === 'success' ? 'rgba(16,185,129,0.2)' : 'rgba(239,68,68,0.2)'}`,
                }}>
                  {results[fault.id].msg}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
