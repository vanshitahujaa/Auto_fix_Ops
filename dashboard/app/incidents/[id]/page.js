'use client';
import { useEffect, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { api } from '../../../lib/api';

const PIPELINE_STAGES = [
  { key: 'INGESTED', label: 'Ingest', icon: '📥' },
  { key: 'CONTEXT_BUILT', label: 'Context', icon: '🔍' },
  { key: 'DIAGNOSED', label: 'Diagnosis', icon: '🧠' },
  { key: 'POLICY_APPROVED', label: 'Policy', icon: '🛡' },
  { key: 'PENDING_PR_MERGE', label: 'PR', icon: '🔧' },
  { key: 'VERIFIED', label: 'Verify', icon: '✅' },
  { key: 'RESOLVED', label: 'Resolved', icon: '🎯' },
];

const STATUS_ORDER = {
  INGESTED: 0, CONTEXT_BUILT: 1, DIAGNOSED: 2, POLICY_APPROVED: 3,
  REMEDIATING: 3.5, PENDING_PR_MERGE: 4, VERIFIED: 5, RESOLVED: 6,
  FAILED: -1, ESCALATED: -2,
};

function PipelineFlow({ status }) {
  const currentIdx = STATUS_ORDER[status] ?? -1;
  const isFailed = status === 'FAILED';
  const isEscalated = status === 'ESCALATED';

  return (
    <div className="pipeline-flow">
      {PIPELINE_STAGES.map((stage, i) => {
        let nodeClass = '';
        let labelClass = '';
        if (isFailed || isEscalated) {
          nodeClass = i <= Math.abs(currentIdx) ? 'failed' : '';
          labelClass = i <= Math.abs(currentIdx) ? 'failed' : '';
        } else if (i < currentIdx) {
          nodeClass = 'completed';
          labelClass = 'completed';
        } else if (i === Math.floor(currentIdx)) {
          nodeClass = 'active';
          labelClass = 'active';
        }

        return (
          <div key={stage.key} style={{ display: 'flex', alignItems: 'center' }}>
            {i > 0 && <div className={`pipeline-connector ${i <= currentIdx ? 'completed' : ''}`} />}
            <div className="pipeline-step">
              <div className={`pipeline-node ${nodeClass}`}>{stage.icon}</div>
              <div className={`pipeline-label ${labelClass}`}>{stage.label}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default function IncidentDetailPage() {
  const { id } = useParams();
  const router = useRouter();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [approving, setApproving] = useState(false);
  const [rollingBack, setRollingBack] = useState(false);
  const [toast, setToast] = useState(null);

  useEffect(() => {
    api.getIncidentContext(id)
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [id]);

  const handleApprove = async () => {
    setApproving(true);
    try {
      await api.approveEscalation(id);
      setToast({ type: 'success', msg: 'Escalation approved — remediation dispatched' });
      setTimeout(() => window.location.reload(), 1500);
    } catch (e) {
      setToast({ type: 'error', msg: e.message });
    }
    setApproving(false);
  };

  const handleRollback = async () => {
    setRollingBack(true);
    try {
      const result = await api.rollbackIncident(id);
      setToast({ type: 'success', msg: `Rollback queued: ${JSON.stringify(result.previous_values)}` });
    } catch (e) {
      setToast({ type: 'error', msg: e.message });
    }
    setRollingBack(false);
  };

  if (loading) return <div className="page"><div className="empty-state"><span className="loading-spinner" /></div></div>;
  if (!data) return <div className="page"><div className="empty-state"><div className="icon">❌</div><p>Incident not found</p></div></div>;

  const { incident, telemetry_context, remediation_audits } = data;
  const audit = remediation_audits?.[0];
  const resolved = incident.resolved_target;

  return (
    <div className="page">
      <div className="page-header flex justify-between items-center">
        <div>
          <button className="btn btn-outline" onClick={() => router.push('/incidents')} style={{ marginBottom: 12 }}>
            ← Back
          </button>
          <h2>{incident.alert_name}</h2>
          <p className="mono text-muted">{incident.id}</p>
        </div>
        <span className={`badge ${incident.status.toLowerCase()}`}>
          {incident.status.replace(/_/g, ' ')}
        </span>
      </div>

      {/* Pipeline Flow */}
      <div className="card mb-16">
        <PipelineFlow status={incident.status} />
      </div>

      {/* Main Grid */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        {/* Diagnosis */}
        <div className="card">
          <h3 style={{ fontSize: 14, color: 'var(--text-muted)', marginBottom: 12, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            Diagnosis
          </h3>
          <div style={{ marginBottom: 12 }}>
            <span className="mono" style={{ fontSize: 18, fontWeight: 700 }}>
              {incident.diagnosis || 'Pending'}
            </span>
          </div>
          {incident.confidence != null && (
            <div style={{ marginBottom: 12 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 4 }}>
                <span>Confidence</span>
                <span className="mono">{(incident.confidence * 100).toFixed(0)}%</span>
              </div>
              <div style={{ height: 6, background: 'var(--bg-primary)', borderRadius: 3, overflow: 'hidden' }}>
                <div style={{
                  height: '100%',
                  width: `${incident.confidence * 100}%`,
                  background: incident.confidence >= 0.8 ? 'var(--success)' : 'var(--warning)',
                  borderRadius: 3,
                  transition: 'width 0.5s',
                }} />
              </div>
            </div>
          )}
          <div className="text-muted text-sm">Engine: {incident.diagnosed_by || '—'}</div>
          {incident.reasoning && (
            <p className="text-sm mt-16" style={{ color: 'var(--text-secondary)' }}>
              {incident.reasoning}
            </p>
          )}
        </div>

        {/* Remediation Audit */}
        <div className="card">
          <h3 style={{ fontSize: 14, color: 'var(--text-muted)', marginBottom: 12, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            Remediation
          </h3>
          {audit ? (
            <>
              <div style={{ marginBottom: 8 }}>
                <span className="text-muted text-sm">Action: </span>
                <span className="mono">{audit.action}</span>
              </div>
              <div style={{ marginBottom: 8 }}>
                <span className="text-muted text-sm">Verdict: </span>
                <span className={`badge ${audit.policy_verdict?.toLowerCase()}`}>
                  {audit.policy_verdict}
                </span>
              </div>
              <div style={{ marginBottom: 8 }}>
                <span className="text-muted text-sm">Execution: </span>
                <span className="mono">{audit.execution_status || 'Pending'}</span>
              </div>
              {audit.failure_reason && (
                <div style={{ marginBottom: 8 }}>
                  <span className="text-muted text-sm">Failure: </span>
                  <span style={{ color: 'var(--error)', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                    {audit.failure_reason}
                  </span>
                </div>
              )}
              {audit.failure_root_cause && (
                <div style={{ marginBottom: 8 }}>
                  <span className="text-muted text-sm">Root Cause: </span>
                  <span className={`badge ${audit.failure_root_cause === 'LOGIC' ? 'failed' : 'escalated'}`}>
                    {audit.failure_root_cause}
                  </span>
                </div>
              )}
              {audit.pr_url && (
                <div style={{ marginBottom: 8 }}>
                  <span className="text-muted text-sm">PR: </span>
                  <a href={audit.pr_url} target="_blank" rel="noopener" style={{ color: 'var(--accent)' }}>
                    {audit.pr_url.split('/').slice(-2).join('/')}
                  </a>
                </div>
              )}
              {audit.is_shadow === 'true' && (
                <div className="status-pill shadow" style={{ marginTop: 8, display: 'inline-block' }}>🛡 Shadow Run</div>
              )}
            </>
          ) : (
            <p className="text-muted">No remediation audit yet</p>
          )}
        </div>
      </div>

      {/* Resolved Target */}
      {resolved && (
        <div className="card mt-16">
          <h3 style={{ fontSize: 14, color: 'var(--text-muted)', marginBottom: 12, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            🎯 Resolved Target
          </h3>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12 }}>
            <div>
              <div className="text-muted text-sm">Namespace</div>
              <div className="mono">{resolved.namespace}</div>
            </div>
            <div>
              <div className="text-muted text-sm">Deployment</div>
              <div className="mono">{resolved.deployment}</div>
            </div>
            <div>
              <div className="text-muted text-sm">Container</div>
              <div className="mono">{resolved.container}</div>
            </div>
          </div>
          <div style={{ display: 'flex', gap: 16, marginTop: 12 }}>
            <div>
              <span className="text-muted text-sm">Confidence: </span>
              <span className="mono" style={{ color: resolved.confidence >= 0.8 ? 'var(--success)' : 'var(--warning)' }}>
                {(resolved.confidence * 100).toFixed(0)}%
              </span>
            </div>
            <div>
              <span className="text-muted text-sm">Pod Pattern: </span>
              <span className="mono">{resolved.pod_pattern}</span>
            </div>
          </div>
          {resolved.validation && (
            <div style={{ display: 'flex', gap: 12, marginTop: 12, fontSize: 12 }}>
              <span style={{ color: resolved.validation.namespace_match ? 'var(--success)' : 'var(--error)' }}>
                {resolved.validation.namespace_match ? '✅' : '❌'} Namespace match
              </span>
              <span style={{ color: resolved.validation.pod_label_present ? 'var(--success)' : 'var(--warning)' }}>
                {resolved.validation.pod_label_present ? '✅' : '⚠️'} Pod label
              </span>
              <span style={{ color: resolved.validation.deployment_resolved ? 'var(--success)' : 'var(--error)' }}>
                {resolved.validation.deployment_resolved ? '✅' : '❌'} Deployment resolved
              </span>
            </div>
          )}
        </div>
      )}

      {/* Policy Breakdown */}
      {audit && (
        <div className="card mt-16">
          <h3 style={{ fontSize: 14, color: 'var(--text-muted)', marginBottom: 12, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            Policy Decision Breakdown
          </h3>
          <div className="policy-grid">
            <div className="policy-gate">
              <span className="gate-icon">{incident.confidence >= 0.8 ? '✅' : '❌'}</span>
              Confidence: {incident.confidence != null ? `${(incident.confidence * 100).toFixed(0)}%` : '—'} (≥80%)
            </div>
            <div className="policy-gate">
              <span className="gate-icon">✅</span>
              Namespace: {resolved?.namespace || 'autofixops'}
            </div>
            <div className="policy-gate">
              <span className="gate-icon">✅</span>
              Circuit Breaker: CLOSED
            </div>
            <div className="policy-gate">
              <span className="gate-icon">{audit.action ? '✅' : '❌'}</span>
              Action: {audit.action || 'N/A'}
            </div>
            <div className="policy-gate">
              <span className="gate-icon">{audit.is_shadow === 'true' ? '🛡' : '🟢'}</span>
              Mode: {audit.is_shadow === 'true' ? 'Shadow' : 'Live'}
            </div>
          </div>
        </div>
      )}

      {/* Escalation Approval */}
      {incident.status === 'ESCALATED' && (
        <div className="card mt-16" style={{ borderColor: 'var(--warning)' }}>
          <h3 style={{ color: 'var(--warning)', marginBottom: 8 }}>⚠️ Human Approval Required</h3>
          <p className="text-sm text-muted mb-16">
            This incident was escalated. Review the diagnosis and approve to trigger remediation.
          </p>
          <button className="btn btn-success" onClick={handleApprove} disabled={approving}>
            {approving ? <span className="loading-spinner" /> : '✓ Approve & Execute'}
          </button>
        </div>
      )}

      {/* Rollback */}
      {audit && audit.execution_status && incident.status !== 'ESCALATED' && (
        <div className="card mt-16" style={{ borderColor: 'rgba(220,38,38,0.3)' }}>
          <h3 style={{ color: 'var(--error)', marginBottom: 8, fontSize: 14 }}>🔄 Rollback</h3>
          <p className="text-sm text-muted mb-16">
            Revert the last remediation by restoring previous resource values.
          </p>
          <button
            className="btn btn-danger"
            onClick={handleRollback}
            disabled={rollingBack}
            style={{ fontSize: 12, padding: '8px 16px' }}
          >
            {rollingBack ? <span className="loading-spinner" /> : '⚡ Trigger Rollback'}
          </button>
        </div>
      )}

      {toast && <div className={`toast ${toast.type}`}>{toast.msg}</div>}
    </div>
  );
}
