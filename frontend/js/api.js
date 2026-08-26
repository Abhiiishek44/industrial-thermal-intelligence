/**
 * api.js — Wildfire Decision Support API client (no ES modules)
 * Exposes: window.API
 */
(function() {
  const API_BASE = window.location.origin;

  function _headers() {
    const token = localStorage.getItem('wf_token');
    const h = { 'Content-Type': 'application/json' };
    if (token) h['Authorization'] = 'Bearer ' + token;
    return h;
  }

  async function apiFetch(path, opts = {}) {
    const res = await fetch(API_BASE + path, { headers: _headers(), ...opts });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: res.statusText }));
      throw new Error(err.error || 'HTTP ' + res.status);
    }
    return res.json();
  }

  window.API = {
    BASE: API_BASE,

    githubLoginUrl() { return API_BASE + '/api/auth/github/login'; },

    captureTokenFromHash() {
      const m = window.location.hash.match(/[#&]token=([^&]+)/);
      if (!m) return null;
      const token = decodeURIComponent(m[1]);
      localStorage.setItem('wf_token', token);
      history.replaceState(null, '', window.location.pathname + window.location.search);
      return token;
    },

    async verifyToken() { return apiFetch('/api/auth/verify'); },
    logout() { localStorage.removeItem('wf_token'); },

    async getReplayTime(eid)     { return apiFetch('/api/events/' + eid + '/replay-time'); },
    async setReplayTime(eid, ms, speed) { return apiFetch('/api/events/' + eid + '/replay-time', { method: 'POST', body: JSON.stringify({ ms, speed: speed || 1 }) }); },

    async getEvents()        { return apiFetch('/api/events/'); },
    async getAoi(eid)        { return apiFetch('/api/events/' + eid + '/layers/aoi'); },
    async getRealtimeFirms(hours) { return apiFetch('/api/firms/realtime' + (hours ? '?hours=' + hours : '')); },
    async getEvent(id)       { return apiFetch('/api/events/' + id); },
    async getTimesteps(id)   { return apiFetch('/api/events/' + id + '/timesteps'); },

    async getPerimeter(eid, tsid, crowd)  { return apiFetch('/api/events/' + eid + '/timesteps/' + tsid + '/perimeter' + (crowd ? '?crowd=true' : '')); },
    async getHotspots(eid, tsid, crowd)  { return apiFetch('/api/events/' + eid + '/timesteps/' + tsid + '/hotspots' + (crowd ? '?crowd=true' : '')); },
    async getRiskZones(eid, tsid, crowd) { return apiFetch('/api/events/' + eid + '/timesteps/' + tsid + '/risk-zones' + (crowd ? '?crowd=true' : '')); },
    async getRoads(eid, tsid, crowd)     { return apiFetch('/api/events/' + eid + '/timesteps/' + tsid + '/roads' + (crowd ? '?crowd=true' : '')); },
    async getAnalysis(eid, tsid, crowd) { return apiFetch('/api/events/' + eid + '/timesteps/' + tsid + '/population' + (crowd ? '?crowd=true' : '')); },
    async getFireContext(eid, tsid){ return apiFetch('/api/events/' + eid + '/timesteps/' + tsid + '/fire-context'); },
    async getWeather(eid, tsid)   { return apiFetch('/api/events/' + eid + '/timesteps/' + tsid + '/weather'); },
    async getWindField(eid, tsid) { return apiFetch('/api/events/' + eid + '/timesteps/' + tsid + '/wind-field'); },

async generateReport(eid, tsid, force) {
      return apiFetch('/api/events/' + eid + '/timesteps/' + tsid + '/report', {
        method: 'POST', body: force ? JSON.stringify({ force: true }) : undefined,
      });
    },
    async generateReportWithCrowd(eid, tsid, force) {
      return apiFetch('/api/events/' + eid + '/timesteps/' + tsid + '/report-with-crowd', {
        method: 'POST', body: force ? JSON.stringify({ force: true }) : undefined,
      });
    },

    async getWindRiskZones(eid, tsid) {
      return apiFetch('/api/events/' + eid + '/timesteps/' + tsid + '/risk-zones-wind');
    },

    async getActualPerimeter(eid, tsid) {
      return apiFetch('/api/events/' + eid + '/timesteps/' + tsid + '/actual-perimeter');
    },

    async runPredictionStep(eid, tsid) {
      return apiFetch('/api/events/' + eid + '/timesteps/' + tsid + '/run-prediction', { method: 'POST' });
    },
    async rerunPredictionStep(eid, tsid) {
      return apiFetch('/api/events/' + eid + '/timesteps/' + tsid + '/run-prediction', {
        method: 'POST',
        body: JSON.stringify({ force: true }),
      });
    },
    async rerunCrowdPredictionStep(eid, tsid) {
      return apiFetch('/api/events/' + eid + '/timesteps/' + tsid + '/run-prediction', {
        method: 'POST',
        body: JSON.stringify({ crowd: true, force: true }),
      });
    },
    async simulateFieldReports(eid, n, hints, tsId, virtualTime) {
      return apiFetch('/api/events/' + eid + '/field-reports/simulate', {
        method: 'POST',
        body: JSON.stringify({ n: n, hints: hints, ts_id: tsId || null, virtual_time: virtualTime || null }),
      });
    },
    async getTsStatus(eid, tsid) {
      return apiFetch('/api/events/' + eid + '/timesteps/' + tsid + '/status');
    },

    // Crowd intelligence
    async submitFieldReport(eid, dataOrFormData) {
      const isForm  = dataOrFormData instanceof FormData;
      const token   = localStorage.getItem('wf_token');
      const headers = isForm ? {} : { 'Content-Type': 'application/json' };
      if (token) headers['Authorization'] = 'Bearer ' + token;
      const res = await fetch(API_BASE + '/api/events/' + eid + '/field-reports', {
        method: 'POST', headers: headers,
        body: isForm ? dataOrFormData : JSON.stringify(dataOrFormData),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: res.statusText }));
        throw new Error(err.error || 'HTTP ' + res.status);
      }
      return res.json();
    },

    async getFieldReports(eid, before)  { return apiFetch('/api/events/' + eid + '/field-reports' + (before ? '?before=' + encodeURIComponent(before) : '')); },
    async clearFieldReports(eid)       { return apiFetch('/api/events/' + eid + '/field-reports/clear', { method: 'POST' }); },
    async likeReport(eid, rid)         { return apiFetch('/api/events/' + eid + '/field-reports/' + rid + '/like', { method: 'POST' }); },
    async flagReport(eid, rid)         { return apiFetch('/api/events/' + eid + '/field-reports/' + rid + '/flag', { method: 'POST' }); },
    async getReportComments(eid, rid)  { return apiFetch('/api/events/' + eid + '/field-reports/' + rid + '/comments'); },
    async addReportComment(eid, rid, c) {
      return apiFetch('/api/events/' + eid + '/field-reports/' + rid + '/comments', {
        method: 'POST', body: JSON.stringify({ content: c }),
      });
    },
    async likeComment(eid, rid, cid)   { return apiFetch('/api/events/' + eid + '/field-reports/' + rid + '/comments/' + cid + '/like',   { method: 'POST' }); },
    async unlikeComment(eid, rid, cid) { return apiFetch('/api/events/' + eid + '/field-reports/' + rid + '/comments/' + cid + '/unlike', { method: 'POST' }); },

    streamChat(eventId, payload, onChunk, onDone, onError) {
      const controller = new AbortController();
      fetch(API_BASE + '/api/events/' + eventId + '/chat', {
        method: 'POST', headers: _headers(),
        body: JSON.stringify(payload), signal: controller.signal,
      }).then(async res => {
        if (!res.ok) {
          const e = await res.json().catch(() => ({ error: res.statusText }));
          onError(e.error || 'HTTP ' + res.status);
          return;
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let done = false;
        while (!done) {
          const { value, done: d } = await reader.read();
          done = d;
          if (value) onChunk(decoder.decode(value));
        }
        onDone();
      }).catch(err => { if (err.name !== 'AbortError') onError(err.message); });
      return controller;
    },
  };
})();
