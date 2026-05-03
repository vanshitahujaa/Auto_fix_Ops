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
  // System
  getStatus: () => fetchAPI('/api/v1/status'),
  getSystemMode: () => fetchAPI('/api/v1/system/mode'),
  setSystemMode: (mode, reason) => fetchAPI('/api/v1/system/mode', {
    method: 'POST', body: JSON.stringify({ mode, reason })
  }),

  // Config
  getConfig: () => fetchAPI('/api/v1/config'),
  saveConfig: (data) => fetchAPI('/api/v1/config', { method: 'POST', body: JSON.stringify(data) }),

  // Incidents
  getIncidents: (status) => fetchAPI(`/api/v1/incidents${status ? `?status=${status}` : ''}`),
  getIncidentContext: (id) => fetchAPI(`/api/v1/incidents/${id}/context`),
  approveEscalation: (id) => fetchAPI(`/api/v1/escalations/${id}/approve`, { method: 'POST' }),
  rollbackIncident: (id) => fetchAPI(`/api/v1/incidents/${id}/rollback`, { method: 'POST' }),

  // Metrics
  getMetrics: () => fetchAPI('/api/v1/metrics'),

  // Chaos
  injectChaos: (data) => fetchAPI('/api/v1/chaos/inject', { method: 'POST', body: JSON.stringify(data) }),

  // Service Account
  getServiceAccount: () => fetchAPI('/api/v1/service-account'),
  saveServiceAccount: (data) => fetchAPI('/api/v1/service-account', { method: 'POST', body: JSON.stringify(data) }),
  deleteServiceAccount: () => fetchAPI('/api/v1/service-account', { method: 'DELETE' }),

  // WebSocket
  listenEvents: (onEvent) => {
    const wsUrl = API_BASE.replace(/^http/, 'ws') + '/api/v1/events/ws';
    const ws = new WebSocket(wsUrl);
    ws.onmessage = (e) => {
      try {
        const event = JSON.parse(e.data);
        onEvent(event);
      } catch (err) {
        console.error('Failed to parse WS event:', err);
      }
    };
    ws.onerror = (e) => console.error('WS Error:', e);
    return () => ws.close();
  },
};
