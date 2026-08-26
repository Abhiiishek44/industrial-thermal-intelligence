/**
 * api.js — Wildfire Decision Support API client (no ES modules)
 * Exposes: window.API
 */
(function() {
  const API_BASE = window.location.origin;
  const ACCESS_TOKEN_KEY = 'wf_access_token';
  const REFRESH_TOKEN_KEY = 'wf_refresh_token';
  let refreshPromise = null;

  function _storeSession(data) {
    localStorage.setItem(ACCESS_TOKEN_KEY, data.access_token);
    localStorage.setItem(REFRESH_TOKEN_KEY, data.refresh_token);
    return data;
  }

  function _clearSession() {
    localStorage.removeItem(ACCESS_TOKEN_KEY);
    localStorage.removeItem(REFRESH_TOKEN_KEY);
  }

  async function _jsonRequest(path, body) {
    const res = await fetch(API_BASE + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: res.statusText }));
      throw new Error(err.message || err.error || 'HTTP ' + res.status);
    }
    return res.status === 204 ? null : res.json();
  }

  async function _refreshSession() {
    if (refreshPromise) return refreshPromise;
    const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY);
    if (!refreshToken) throw new Error('Session expired. Please sign in again.');

    refreshPromise = _jsonRequest('/auth/refresh', { refresh_token: refreshToken })
      .then(_storeSession)
      .catch(function(error) {
        _clearSession();
        throw error;
      })
      .finally(function() { refreshPromise = null; });
    return refreshPromise;
  }

  async function _fetchWithAuth(path, opts, canRetry) {
    opts = opts || {};
    const headers = new Headers(opts.headers || {});
    const accessToken = localStorage.getItem(ACCESS_TOKEN_KEY);
    if (accessToken) headers.set('Authorization', 'Bearer ' + accessToken);
    if (opts.body && !(opts.body instanceof FormData) && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }
    const requestOpts = Object.assign({}, opts, { headers: headers });
    const res = await fetch(API_BASE + path, requestOpts);
    if (res.status === 401 && canRetry !== false && localStorage.getItem(REFRESH_TOKEN_KEY)) {
      await _refreshSession();
      return _fetchWithAuth(path, opts, false);
    }
    return res;
  }

  async function apiFetch(path, opts) {
    const res = await _fetchWithAuth(path, opts, true);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: res.statusText }));
      throw new Error(err.message || err.error || 'HTTP ' + res.status);
    }
    return res.status === 204 ? null : res.json();
  }

  window.API = {
    BASE: API_BASE,

    async register(username, password, email) {
      return _jsonRequest('/auth/register', {
        username: username,
        password: password,
        email: email || undefined,
      }).then(_storeSession);
    },
    async login(username, password) {
      return _jsonRequest('/auth/login', { username: username, password: password })
        .then(_storeSession);
    },
    async me() { return apiFetch('/auth/me'); },
    async logout() {
      const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY);
      try {
        if (refreshToken) await _jsonRequest('/auth/logout', { refresh_token: refreshToken });
      } finally {
        _clearSession();
      }
    },
    hasSession() { return !!localStorage.getItem(REFRESH_TOKEN_KEY); },
    getAccessToken() { return localStorage.getItem(ACCESS_TOKEN_KEY); },
    authorizedFetch(path, opts) { return _fetchWithAuth(path, opts, true); },

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
      const headers = isForm ? {} : { 'Content-Type': 'application/json' };
      const res = await _fetchWithAuth('/api/events/' + eid + '/field-reports', {
        method: 'POST', headers: headers,
        body: isForm ? dataOrFormData : JSON.stringify(dataOrFormData),
      }, true);
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: res.statusText }));
        throw new Error(err.message || err.error || 'HTTP ' + res.status);
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
      _fetchWithAuth('/api/events/' + eventId + '/chat', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload), signal: controller.signal,
      }, true).then(async res => {
        if (!res.ok) {
          const e = await res.json().catch(() => ({ error: res.statusText }));
          onError(e.message || e.error || 'HTTP ' + res.status);
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
