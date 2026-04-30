'use client';
import { useEffect, useState } from 'react';
import { api } from '../../lib/api';

const STEPS = ['GitHub', 'Infrastructure', 'Safety', 'Validate'];

export default function OnboardPage() {
  const [step, setStep] = useState(0);
  const [config, setConfig] = useState({
    name: 'Default Project',
    github_token: '',
    github_repo: '',
    prometheus_url: '',
    target_namespace: 'autofixops',
    target_manifest_path: 'kubernetes_integration/target_app/deployment.yaml',
    shadow_mode: 'true',
    confidence_threshold: 0.8,
    allowed_chaos_namespaces: ['staging', 'test', 'dev', 'default', 'autofixops'],
    max_resource_scale_factor: 2.0,
  });
  const [existing, setExisting] = useState(null);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState(null);
  const [validation, setValidation] = useState({});
  const [systemMode, setSystemMode] = useState('ACTIVE');

  useEffect(() => {
    api.getConfig().then((data) => {
      if (data.configured) {
        setExisting(data);
        setConfig((c) => ({
          ...c,
          name: data.name || 'Default Project',
          github_repo: data.github_repo || '',
          prometheus_url: data.prometheus_url || '',
          target_namespace: data.target_namespace || 'autofixops',
          target_manifest_path: data.target_manifest_path || '',
          shadow_mode: data.shadow_mode || 'true',
          confidence_threshold: data.confidence_threshold || 0.8,
          allowed_chaos_namespaces: data.allowed_chaos_namespaces || ['staging', 'test', 'dev', 'default', 'autofixops'],
          max_resource_scale_factor: data.max_resource_scale_factor || 2.0,
        }));
      }
    }).catch(() => {});

    api.getSystemMode().then((data) => setSystemMode(data.system_mode)).catch(() => {});
  }, []);

  const update = (key, val) => setConfig((c) => ({ ...c, [key]: val }));

  const handleSave = async () => {
    setSaving(true);
    try {
      await api.saveConfig(config);
      setToast({ type: 'success', msg: 'Configuration saved securely' });
      setStep(3);
    } catch (e) {
      setToast({ type: 'error', msg: e.message });
    }
    setSaving(false);
  };

  const validate = async () => {
    setValidation({ testing: true });
    const results = {};

    if (config.github_repo) {
      try {
        const res = await fetch(`https://api.github.com/repos/${config.github_repo}`, {
          headers: config.github_token ? { Authorization: `token ${config.github_token}` } : {},
        });
        results.github = res.ok ? 'pass' : 'fail';
      } catch { results.github = 'fail'; }
    } else {
      results.github = 'skip';
    }

    if (config.prometheus_url) {
      try {
        const res = await fetch(`${config.prometheus_url}/-/healthy`, { signal: AbortSignal.timeout(5000) });
        results.prometheus = res.ok ? 'pass' : 'fail';
      } catch { results.prometheus = 'fail'; }
    } else {
      results.prometheus = 'skip';
    }

    try {
      await api.getStatus();
      results.backend = 'pass';
    } catch { results.backend = 'fail'; }

    setValidation(results);
  };

  const chaosNsStr = (config.allowed_chaos_namespaces || []).join(', ');

  return (
    <div className="page">
      <div className="page-header">
        <h2>⚙️ Project Configuration</h2>
        <p>{existing ? 'Update your project settings' : 'Connect your infrastructure to AutoFixOps'}</p>
      </div>

      {/* Wizard Steps */}
      <div className="wizard-steps">
        {STEPS.map((s, i) => (
          <div
            key={s}
            className={`wizard-step ${i === step ? 'active' : i < step ? 'done' : ''}`}
            onClick={() => setStep(i)}
            style={{ cursor: 'pointer' }}
          >
            {i < step ? '✓ ' : ''}{s}
          </div>
        ))}
      </div>

      <div className="card">
        {/* Step 0: GitHub */}
        {step === 0 && (
          <>
            <h3 style={{ marginBottom: 20, fontSize: 16 }}>GitHub Connection</h3>
            <div className="form-group">
              <label>Project Name</label>
              <input
                type="text"
                value={config.name}
                onChange={(e) => update('name', e.target.value)}
                placeholder="My Production App"
              />
            </div>
            <div className="form-group">
              <label>Repository (owner/repo)</label>
              <input
                type="text"
                value={config.github_repo}
                onChange={(e) => update('github_repo', e.target.value)}
                placeholder="vanshitahujaa/Auto_fix_Ops"
              />
            </div>
            <div className="form-group">
              <label>Personal Access Token</label>
              <input
                type="password"
                value={config.github_token}
                onChange={(e) => update('github_token', e.target.value)}
                placeholder={existing ? `Current: ${existing.github_token}` : 'ghp_xxxxxxxxxxxx'}
              />
              <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                🔒 Encrypted at rest with AES-256. Never stored in plaintext.
              </span>
            </div>
            <button className="btn btn-primary" onClick={() => setStep(1)}>
              Next → Infrastructure
            </button>
          </>
        )}

        {/* Step 1: Infrastructure */}
        {step === 1 && (
          <>
            <h3 style={{ marginBottom: 20, fontSize: 16 }}>Infrastructure</h3>
            <div className="form-group">
              <label>Prometheus URL</label>
              <input
                type="text"
                value={config.prometheus_url}
                onChange={(e) => update('prometheus_url', e.target.value)}
                placeholder="http://prometheus:9090"
              />
            </div>
            <div className="form-group">
              <label>Target Namespace</label>
              <input
                type="text"
                value={config.target_namespace}
                onChange={(e) => update('target_namespace', e.target.value)}
                placeholder="autofixops"
              />
            </div>
            <div className="form-group">
              <label>Manifest Path (relative to repo root)</label>
              <input
                type="text"
                value={config.target_manifest_path}
                onChange={(e) => update('target_manifest_path', e.target.value)}
                placeholder="kubernetes_integration/target_app/deployment.yaml"
              />
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <button className="btn btn-outline" onClick={() => setStep(0)}>← Back</button>
              <button className="btn btn-primary" onClick={() => setStep(2)}>Next → Safety</button>
            </div>
          </>
        )}

        {/* Step 2: Safety */}
        {step === 2 && (
          <>
            <h3 style={{ marginBottom: 20, fontSize: 16 }}>Safety Settings</h3>
            <div className="form-group">
              <label>Execution Mode</label>
              <select
                value={config.shadow_mode}
                onChange={(e) => update('shadow_mode', e.target.value)}
              >
                <option value="true">🛡 Shadow Mode (PRs are drafts, no auto-merge)</option>
                <option value="false">🟢 Live Mode (PRs are real, auto-merge enabled)</option>
              </select>
            </div>
            <div className="form-group">
              <label>Confidence Threshold ({(config.confidence_threshold * 100).toFixed(0)}%)</label>
              <input
                type="range"
                min="0.5"
                max="1.0"
                step="0.05"
                value={config.confidence_threshold}
                onChange={(e) => update('confidence_threshold', parseFloat(e.target.value))}
                style={{ background: 'transparent' }}
              />
              <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                Minimum confidence required for policy approval. Below this → escalate to human.
              </span>
            </div>
            <div className="form-group">
              <label>Max Resource Scale Factor ({config.max_resource_scale_factor}x)</label>
              <input
                type="range"
                min="1.5"
                max="5.0"
                step="0.5"
                value={config.max_resource_scale_factor}
                onChange={(e) => update('max_resource_scale_factor', parseFloat(e.target.value))}
                style={{ background: 'transparent' }}
              />
              <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                Maximum multiplier for resource patches (e.g. 2x = 128Mi → max 256Mi). Hard cap: 4Gi / 4 CPU.
              </span>
            </div>
            <div className="form-group">
              <label>Allowed Chaos Namespaces</label>
              <input
                type="text"
                value={chaosNsStr}
                onChange={(e) => update('allowed_chaos_namespaces', e.target.value.split(',').map(s => s.trim()).filter(Boolean))}
                placeholder="staging, test, dev, default"
              />
              <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                Comma-separated list. Chaos injection is BLOCKED for any namespace not listed here.
              </span>
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <button className="btn btn-outline" onClick={() => setStep(1)}>← Back</button>
              <button className="btn btn-primary" onClick={handleSave} disabled={saving}>
                {saving ? <span className="loading-spinner" /> : '💾 Save Configuration'}
              </button>
            </div>
          </>
        )}

        {/* Step 3: Validate */}
        {step === 3 && (
          <>
            <h3 style={{ marginBottom: 20, fontSize: 16 }}>Connection Validation</h3>
            {!validation.testing && !validation.backend && (
              <button className="btn btn-primary" onClick={validate}>
                🔍 Run Connectivity Tests
              </button>
            )}
            {validation.testing && (
              <div className="empty-state"><span className="loading-spinner" /> Testing connections...</div>
            )}
            {validation.backend && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 16 }}>
                {[
                  { label: 'Backend API', status: validation.backend },
                  { label: 'GitHub API', status: validation.github },
                  { label: 'Prometheus', status: validation.prometheus },
                ].map((t) => (
                  <div key={t.label} className="policy-gate">
                    <span className="gate-icon">
                      {t.status === 'pass' ? '✅' : t.status === 'skip' ? '⏭️' : '❌'}
                    </span>
                    {t.label}: {t.status === 'pass' ? 'Connected' : t.status === 'skip' ? 'Skipped' : 'Failed'}
                  </div>
                ))}
              </div>
            )}

            {/* System Mode Control */}
            <div style={{ marginTop: 24, paddingTop: 24, borderTop: '1px solid var(--border)' }}>
              <h3 style={{ marginBottom: 12, fontSize: 14, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
                System Mode Control
              </h3>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                {['ACTIVE', 'SHADOW', 'DISABLED'].map((mode) => (
                  <button
                    key={mode}
                    className={`btn ${
                      mode === 'DISABLED' ? 'btn-danger'
                        : mode === 'SHADOW' ? 'btn-outline'
                          : 'btn-success'
                    }`}
                    style={{
                      opacity: systemMode === mode ? 1 : 0.4,
                      fontSize: 12,
                      padding: '8px 16px',
                      border: systemMode === mode ? '2px solid' : undefined,
                    }}
                    onClick={async () => {
                      try {
                        await api.setSystemMode(mode, mode === 'DISABLED' ? 'Manual from settings page' : '');
                        setSystemMode(mode);
                        setToast({ type: 'success', msg: `System mode → ${mode}` });
                      } catch (e) {
                        setToast({ type: 'error', msg: e.message });
                      }
                    }}
                  >
                    {mode === 'ACTIVE' ? '🟢' : mode === 'SHADOW' ? '🛡' : '🛑'} {mode}
                  </button>
                ))}
              </div>
              <span style={{ fontSize: 11, color: 'var(--text-muted)', display: 'block', marginTop: 8 }}>
                DISABLED = all execution halted. SHADOW = PRs are drafts. ACTIVE = full autonomous operation.
              </span>
            </div>
          </>
        )}
      </div>

      {toast && <div className={`toast ${toast.type}`}>{toast.msg}</div>}
    </div>
  );
}
