'use client';
import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { api } from '../../lib/api';

const STATUSES = ['ALL', 'INGESTED', 'CONTEXT_BUILT', 'DIAGNOSED', 'POLICY_APPROVED', 'PENDING_PR_MERGE', 'ESCALATED', 'RESOLVED', 'FAILED'];

function timeAgo(dateStr) {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export default function IncidentsPage() {
  const [incidents, setIncidents] = useState([]);
  const [filter, setFilter] = useState('ALL');
  const [loading, setLoading] = useState(true);
  const router = useRouter();

  const load = () => {
    const status = filter === 'ALL' ? null : filter;
    api.getIncidents(status)
      .then((data) => { setIncidents(data.incidents || []); setLoading(false); })
      .catch(() => setLoading(false));
  };

  useEffect(() => { load(); const id = setInterval(load, 5000); return () => clearInterval(id); }, [filter]);

  return (
    <div className="page">
      <div className="page-header flex justify-between items-center">
        <div>
          <h2>Incidents</h2>
          <p>Live pipeline monitoring — auto-refreshes every 5s</p>
        </div>
        <span className="mono text-muted text-sm">{incidents.length} total</span>
      </div>

      <div className="filter-bar">
        {STATUSES.map((s) => (
          <button
            key={s}
            className={`filter-chip ${filter === s ? 'active' : ''}`}
            onClick={() => setFilter(s)}
          >
            {s.replace(/_/g, ' ')}
          </button>
        ))}
      </div>

      <div className="card">
        {loading ? (
          <div className="empty-state"><span className="loading-spinner" /></div>
        ) : incidents.length === 0 ? (
          <div className="empty-state">
            <div className="icon">📭</div>
            <p>No incidents found</p>
          </div>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Alert</th>
                <th>Status</th>
                <th>Diagnosis</th>
                <th>Confidence</th>
                <th>Engine</th>
                <th>Time</th>
              </tr>
            </thead>
            <tbody>
              {incidents.map((inc) => (
                <tr key={inc.id} onClick={() => router.push(`/incidents/${inc.id}`)}>
                  <td className="mono text-muted">{inc.id.slice(0, 8)}</td>
                  <td>{inc.alert_name}</td>
                  <td>
                    <span className={`badge ${inc.status.toLowerCase()}`}>
                      {inc.status.replace(/_/g, ' ')}
                    </span>
                  </td>
                  <td className="mono">{inc.diagnosis || '—'}</td>
                  <td className="mono">
                    {inc.confidence != null ? `${(inc.confidence * 100).toFixed(0)}%` : '—'}
                  </td>
                  <td className="text-muted">{inc.diagnosed_by || '—'}</td>
                  <td className="text-muted">{timeAgo(inc.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
