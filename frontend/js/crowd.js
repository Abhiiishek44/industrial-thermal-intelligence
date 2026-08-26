/**
 * crowd.js — Field intelligence panel (submit reports, view reports + themes)
 * Exposes: window.CrowdPanel
 * Depends on: window.API
 *
 * Marker behaviour:
 *   - request_help  → full-size emoji icon
 *   - info / offer_help / fire_report → fixed 12 px dot (colour by type/intensity)
 *
 * Reports are filtered server-side to the 24 h window ending at the virtual replay time.
 * Clicking any marker opens a shared modal with like / comment / flag / Maps.
 */
(function() {
  let _eventId     = null;
  let _eventMap    = null;
  let _pickingLoc  = false;
  let _modalReport = null;    // report object currently shown in modal
  let _virtualTime = null;    // ISO string — replay clock position (null = no filter)
  const _likedComments = new Set();  // comment IDs liked this session (for toggle)

  const _layers = {
    fire_report:  null,
    info:         null,
    request_help: null,
    offer_help:   null,
  };

  const INTENSITY_COLORS = {
    low:  '#4caf50',
    mid:  '#ff9800',
    high: '#f44336',
  };

  const POST_LABELS = {
    fire_report:  '🔥 Fire Report',
    info:         'ℹ️ Info',
    request_help: '🆘 Need Help',
    offer_help:   '🤝 Offer Help',
  };

  const POST_ICONS = {
    fire_report:  '🔥',
    info:         'ℹ️',
    request_help: '🆘',
    offer_help:   '🤝',
  };

  // Type → dot fill colour
  const DOT_COLORS = {
    fire_report: '#ff6b35',
    info:        '#4da6ff',
    offer_help:  '#3fb950',
  };

  // ── Icon factories ─────────────────────────────────────────────────────────

  var _DOT_SIZE = 12;

  function _makeDotIcon(report) {
    var col = DOT_COLORS[report.post_type] || '#8b949e';
    var sz  = _DOT_SIZE;
    return L.divIcon({
      html: '<div style="width:' + sz + 'px;height:' + sz + 'px;border-radius:50%;' +
            'background:' + col + ';opacity:.85;box-shadow:0 0 4px rgba(0,0,0,.5);' +
            'border:1.5px solid rgba(255,255,255,.4)"></div>',
      className: '',
      iconSize:   [sz, sz],
      iconAnchor: [sz / 2, sz / 2],
      popupAnchor:[0, -(sz / 2 + 4)],
    });
  }

  function _makeFullIcon(report) {
    var emoji  = POST_ICONS[report.post_type] || '📍';
    var border = '#e53935';
    return L.divIcon({
      html: '<div style="font-size:18px;line-height:28px;text-align:center;' +
            'width:28px;height:28px;border-radius:50%;' +
            'background:rgba(0,0,0,.65);border:2px solid ' + border + ';' +
            'box-shadow:0 2px 6px rgba(0,0,0,.7)">' + emoji + '</div>',
      className: '',
      iconSize:   [28, 28],
      iconAnchor: [14, 14],
      popupAnchor:[0, -16],
    });
  }

  function _makeIcon(report) {
    return _makeFullIcon(report);
  }

  // ── Init ──────────────────────────────────────────────────────────────────

  function init() {
    document.getElementById('crowd-panel-close').addEventListener('click', close);
    document.getElementById('crowd-panel-overlay').addEventListener('click', close);
    // Tab switching
    document.querySelectorAll('.crowd-tab').forEach(function(btn) {
      btn.addEventListener('click', function() {
        document.querySelectorAll('.crowd-tab').forEach(function(b) { b.classList.remove('active'); });
        document.querySelectorAll('.crowd-tab-content').forEach(function(c) { c.classList.add('hidden'); });
        btn.classList.add('active');
        var content = document.getElementById('crowd-tab-' + btn.dataset.tab);
        if (content) content.classList.remove('hidden');
        if (btn.dataset.tab === 'reports') _loadReports();
      });
    });

    // Map location pick
    document.getElementById('crowd-pick-btn').addEventListener('click', function() {
      _pickingLoc = !_pickingLoc;
      this.classList.toggle('active', _pickingLoc);
      if (_eventMap) _eventMap.map.getContainer().style.cursor = _pickingLoc ? 'crosshair' : '';
    });

    // Form submit
    document.getElementById('crowd-submit-form').addEventListener('submit', function(e) {
      e.preventDefault();
      _submitReport();
    });

    // Report modal close
    document.getElementById('cr-modal-overlay').addEventListener('click', _closeModal);
    document.getElementById('cr-modal-close').addEventListener('click', _closeModal);
  }

  function setEvent(eventId, eventMap) {
    _eventId  = eventId;
    _eventMap = eventMap;

    if (eventMap) {
      eventMap.map.on('click', function(e) {
        if (!_pickingLoc) return;
        document.getElementById('crowd-lat').value = e.latlng.lat.toFixed(5);
        document.getElementById('crowd-lon').value = e.latlng.lng.toFixed(5);
        _pickingLoc = false;
        document.getElementById('crowd-pick-btn').classList.remove('active');
        eventMap.map.getContainer().style.cursor = '';
      });

      Object.keys(_layers).forEach(function(type) {
        if (_layers[type]) eventMap.removeOverlay(_layers[type]);
        _layers[type] = L.layerGroup();
        eventMap.addOverlay(POST_LABELS[type], _layers[type]);
      });
    }

    _refreshCount();
  }

  function clearEvent() {
    _eventId  = null;
    if (_eventMap) {
      Object.keys(_layers).forEach(function(type) {
        if (_layers[type]) { _eventMap.removeOverlay(_layers[type]); _layers[type] = null; }
      });
    }
    _eventMap = null;
    close();
    _closeModal();
  }

  // ── Open / Close panel ────────────────────────────────────────────────────

  function open() {
    document.getElementById('crowd-panel').classList.remove('hidden');
    document.getElementById('crowd-panel-overlay').classList.remove('hidden');
  }

  function close() {
    document.getElementById('crowd-panel').classList.add('hidden');
    document.getElementById('crowd-panel-overlay').classList.add('hidden');
  }

  // ── Report modal ──────────────────────────────────────────────────────────

  function _openModal(report) {
    _modalReport = report;
    var modal = document.getElementById('cr-modal');
    var icon  = POST_ICONS[report.post_type] || '📍';
    document.getElementById('cr-modal-icon').textContent  = icon;
    document.getElementById('cr-modal-type').textContent  =
      (report.post_type || '').replace(/_/g, ' ');
    document.getElementById('cr-modal-time').textContent  = _fmtTime(report.created_at);
    document.getElementById('cr-modal-desc').textContent  = report.description || '—';

    var intensityEl = document.getElementById('cr-modal-intensity');
    if (intensityEl) intensityEl.classList.add('hidden');

    // Coords + Maps link
    document.getElementById('cr-modal-coords').textContent =
      report.lat.toFixed(5) + ', ' + report.lon.toFixed(5);
    document.getElementById('cr-modal-maps-link').href =
      'https://www.google.com/maps?q=' + report.lat + ',' + report.lon;

    // Like button
    var likeBtn = document.getElementById('cr-modal-like');
    likeBtn.textContent = '♥ ' + (report.like_count || 0);
    likeBtn.dataset.reportId = report.id;
    likeBtn.onclick = async function() {
      try {
        var res = await window.API.likeReport(_eventId, report.id);
        report.like_count = res.like_count;
        likeBtn.textContent = '♥ ' + res.like_count;
        likeBtn.classList.add('liked');
        likeBtn.disabled = true;
      } catch(e) {}
    };
    likeBtn.classList.remove('liked');
    likeBtn.disabled = false;

    // Flag button
    var flagBtn = document.getElementById('cr-modal-flag');
    flagBtn.dataset.reportId = report.id;
    flagBtn.classList.remove('flagged');
    flagBtn.disabled = false;
    flagBtn.onclick = async function() {
      if (!confirm('Report this post as inappropriate?')) return;
      try {
        await window.API.flagReport(_eventId, report.id);
        flagBtn.textContent = '⚑ Reported';
        flagBtn.classList.add('flagged');
        flagBtn.disabled = true;
      } catch(e) {}
    };

    // Comments
    _loadModalComments(report.id);

    // Comment submit
    document.getElementById('cr-modal-comment-form').onsubmit = async function(e) {
      e.preventDefault();
      var input = document.getElementById('cr-modal-comment-input');
      var text  = input.value.trim();
      if (!text) return;
      try {
        var c = await window.API.addReportComment(_eventId, report.id, text);
        input.value = '';
        _appendComment(c);
      } catch(e) {}
    };

    document.getElementById('cr-modal-overlay').classList.remove('hidden');
    modal.classList.remove('hidden');
  }

  async function _loadModalComments(reportId) {
    var list = document.getElementById('cr-modal-comments-list');
    list.innerHTML = '<div class="cr-comments-empty">Loading…</div>';
    try {
      var comments = await window.API.getReportComments(_eventId, reportId);
      if (!comments.length) {
        list.innerHTML = '<div class="cr-comments-empty">No comments yet</div>';
        return;
      }
      list.innerHTML = '';
      comments.forEach(_appendComment);
    } catch(e) {
      list.innerHTML = '<div class="cr-comments-empty">Failed to load</div>';
    }
  }

  function _appendComment(c) {
    var list = document.getElementById('cr-modal-comments-list');
    var placeholder = list.querySelector('.cr-comments-empty');
    if (placeholder) placeholder.remove();

    var liked     = _likedComments.has(c.id);
    var likeCount = c.like_count || 0;

    var el = document.createElement('div');
    el.className = 'cr-comment-item';
    el.innerHTML =
      '<span class="cr-comment-content">' + _esc(c.content) + '</span>' +
      '<span class="cr-comment-meta">' +
        '<span class="cr-comment-time">' + _fmtTime(c.created_at) + '</span>' +
        '<button class="cr-comment-like-btn' + (liked ? ' liked' : '') + '" data-cid="' + c.id + '">' +
          '♥ <span class="cr-comment-like-count">' + likeCount + '</span>' +
        '</button>' +
      '</span>';

    var btn = el.querySelector('.cr-comment-like-btn');
    btn.addEventListener('click', async function() {
      var cid = parseInt(btn.dataset.cid);
      var rid = _modalReport && _modalReport.id;
      if (!rid) return;
      try {
        var res;
        if (_likedComments.has(cid)) {
          res = await window.API.unlikeComment(_eventId, rid, cid);
          _likedComments.delete(cid);
          btn.classList.remove('liked');
        } else {
          res = await window.API.likeComment(_eventId, rid, cid);
          _likedComments.add(cid);
          btn.classList.add('liked');
        }
        btn.querySelector('.cr-comment-like-count').textContent = res.like_count;
      } catch(e) {}
    });

    list.appendChild(el);
    list.scrollTop = list.scrollHeight;
  }

  function _closeModal() {
    document.getElementById('cr-modal-overlay').classList.add('hidden');
    document.getElementById('cr-modal').classList.add('hidden');
    _modalReport = null;
  }

  // ── Submit ────────────────────────────────────────────────────────────────

  async function _submitReport() {
    if (!_eventId) return;

    var postType    = document.getElementById('crowd-post-type').value;
    var description = document.getElementById('crowd-description').value.trim();
    var lat         = parseFloat(document.getElementById('crowd-lat').value);
    var lon         = parseFloat(document.getElementById('crowd-lon').value);
    var photoFile   = document.getElementById('crowd-photo').files[0];
    var resultEl    = document.getElementById('crowd-submit-result');

    if (isNaN(lat) || isNaN(lon)) {
      _showResult(resultEl, 'error', 'Lat and Lon are required.');
      return;
    }

    var btn = document.getElementById('crowd-submit-btn');
    btn.disabled = true;
    btn.textContent = 'Submitting…';
    resultEl.classList.add('hidden');

    try {
      var report;
      if (photoFile) {
        var fd = new FormData();
        fd.append('post_type',   postType);
        fd.append('description', description);
        fd.append('lat',         lat);
        fd.append('lon',         lon);
        fd.append('photo',       photoFile);
        report = await window.API.submitFieldReport(_eventId, fd);
      } else {
        report = await window.API.submitFieldReport(_eventId,
          { post_type: postType, description: description, lat: lat, lon: lon });
      }

      _showResult(resultEl, 'success', 'Submitted — AI assessment running in background.');
      _addReportMarker({ id: report.id, post_type: postType, lat: lat, lon: lon,
                         description: description, like_count: 0, created_at: new Date().toISOString() });
      _refreshCount();

      document.getElementById('crowd-description').value = '';
      document.getElementById('crowd-lat').value         = '';
      document.getElementById('crowd-lon').value         = '';
      document.getElementById('crowd-photo').value       = '';
    } catch(e) {
      _showResult(resultEl, 'error', 'Error: ' + e.message);
    } finally {
      btn.disabled    = false;
      btn.textContent = 'Submit Report';
    }
  }

  function _showResult(el, type, msg) {
    el.className   = 'crowd-result ' + type;
    el.textContent = msg;
  }

  // ── Reports tab ───────────────────────────────────────────────────────────

  async function _loadReports() {
    if (!_eventId) return;
    var el = document.getElementById('crowd-reports-list');
    el.innerHTML = '<div class="crowd-empty">Loading…</div>';
    try {
      var reports = await window.API.getFieldReports(_eventId, _virtualTime);
      if (!reports.length) { el.innerHTML = '<div class="crowd-empty">No reports yet</div>'; return; }

      el.innerHTML = reports.map(function(r) {
        var icon = POST_ICONS[r.post_type] || '📍';
        return '<div class="crowd-report-item" data-report-id="' + r.id + '">' +
          '<div class="crowd-report-header">' +
            '<span class="crowd-report-icon">' + icon + '</span>' +
            '<span class="crowd-report-type">' + (r.post_type || '').replace(/_/g, ' ') + '</span>' +
            '<span class="crowd-report-time">' + _fmtTime(r.created_at) + '</span>' +
          '</div>' +
          (r.description ? '<div class="crowd-report-desc">' + _esc(r.description) + '</div>' : '') +
          '<div class="crowd-report-footer">' +
            '<span class="crowd-report-coords">' + r.lat.toFixed(4) + ', ' + r.lon.toFixed(4) + '</span>' +
            '<span class="crowd-report-likes">♥ ' + (r.like_count || 0) + '</span>' +
          '</div>' +
        '</div>';
      }).join('');

      // Click on list item → open modal
      el.querySelectorAll('.crowd-report-item').forEach(function(item) {
        item.addEventListener('click', function() {
          var rid = parseInt(item.dataset.reportId);
          var r   = reports.find(function(x) { return x.id === rid; });
          if (r) _openModal(r);
        });
      });

      _renderReportMarkers(reports);
    } catch(e) {
      el.innerHTML = '<div class="crowd-empty">Failed: ' + _esc(e.message) + '</div>';
    }
  }

  function _renderReportMarkers(reports) {
    Object.keys(_layers).forEach(function(t) { if (_layers[t]) _layers[t].clearLayers(); });
    reports.forEach(function(r) { _addReportMarker(r); });
  }

  function _addReportMarker(r) {
    var type  = r.post_type || 'info';
    var layer = _layers[type] || _layers['info'];
    if (!layer) return;
    var m = L.marker([r.lat, r.lon], { icon: _makeIcon(r) });
    m.on('click', function() { _openModal(r); });
    layer.addLayer(m);
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

  async function _refreshCount() {
    if (!_eventId) return;
    try {
      var reports = await window.API.getFieldReports(_eventId, _virtualTime);
      var badge   = document.getElementById('crowd-report-count');
      if (badge) {
        badge.textContent = reports.length;
        badge.classList.toggle('hidden', reports.length === 0);
      }
      _renderReportMarkers(reports);
    } catch(e) {}
  }

  function _esc(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function _fmtTime(iso) {
    try { return new Date(iso).toLocaleString(undefined, { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' }); }
    catch(e) { return iso || ''; }
  }

  window.CrowdPanel = { init: init, setEvent: setEvent, clearEvent: clearEvent, open: open, close: close,
                        refresh: function(virtualTimeIso) {
                          if (virtualTimeIso) _virtualTime = virtualTimeIso;
                          _loadReports(); _refreshCount();
                        } };
})();
