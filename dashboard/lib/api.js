const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

async function fetchAPI(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `API error ${res.status}`);
  }
  return res.json();
}

export const api = {
  getStatus: () => fetchAPI('/api/v1/status'),
  getConfig: () => fetchAPI('/api/v1/config'),
  saveConfig: (data) => fetchAPI('/api/v1/config', { method: 'POST', body: JSON.stringify(data) }),
  getIncidents: (status) => fetchAPI(`/api/v1/incidents${status ? `?status=${status}` : ''}`),
  getIncidentContext: (id) => fetchAPI(`/api/v1/incidents/${id}/context`),
  getMetrics: () => fetchAPI('/api/v1/metrics'),
  approveEscalation: (id) => fetchAPI(`/api/v1/escalations/${id}/approve`, { method: 'POST' }),
  injectChaos: (data) => fetchAPI('/api/v1/chaos/inject', { method: 'POST', body: JSON.stringify(data) }),
};
