/**
 * chat.js — AI Copilot modal (report + chat)
 * Exposes: window.AIModal
 *
 * Flow:
 *   app.js calls AIModal.setContext(eid, tsid) on every timestep select.
 *   User clicks "🤖 AI Analysis" button → AIModal.open():
 *     - if cached report exists  → renders immediately
 *     - if not               → calls POST /report (generates + caches) then renders
 *   Chat is always live (streaming) inside the same modal.
 */
(function() {
  let _eid = null;
  let _tsid = null;
  let _history = [];
  let _streaming = false;
  let _reportLoaded = false;  // has the current tsid been rendered?
  let _cardState = 'idle';    // 'idle' | 'loading' | 'done'
  let _genId = 0;             // generation counter to discard stale responses

  let _reportData      = null;   // standard report cache
  let _reportDataCrowd = null;   // crowd-enhanced report cache
  let _viewingCrowd    = false;  // which version is currently displayed

  function _generateInitialQuestions(report) {
    const qs = [];

    if (report.risk_level && report.risk_level !== 'Unknown') {
      qs.push('Why is the risk level classified as ' + report.risk_level + '?');
    }

    if (report.key_points && report.key_points.length) {
      report.key_points.slice(0, 2).forEach(function(pt) {
        qs.push('Tell me more about: ' + pt.replace(/\.$/, ''));
      });
    }

    const evac = report.evacuation || {};
    if (evac.top_route && evac.top_route.path && evac.top_route.path.length) {
      qs.push('What is the recommended primary evacuation route?');
    }
    if (evac.alternative_route && evac.alternative_route.window) {
      qs.push('How long do we have before evacuation routes are compromised?');
    }

    const impact = report.impact || {};
    if (impact.communities_affected && impact.communities_affected.length) {
      qs.push('Which communities have the highest population at risk?');
    }

    const risk = report.risk || {};
    if (risk.weather_drivers) {
      qs.push('What weather conditions are driving fire spread right now?');
    }

    const crowd = report.crowd || {};
    if (crowd.urgent_help && crowd.urgent_help.length) {
      qs.push('Are there any urgent help requests from the public?');
    }

    return qs.slice(0, 5);
  }

  // ── Public API ───────────────────────────────────────────────────────────────

  function init() {
    document.getElementById('ai-modal-close').addEventListener('click', close);
    document.getElementById('ai-modal-overlay').addEventListener('click', function(e) {
      if (e.target === document.getElementById('ai-modal-overlay')) close();
    });
    document.getElementById('chat-send-btn').addEventListener('click', _send);
    document.getElementById('chat-input').addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _send(); }
    });
    document.getElementById('ai-enhance-crowd-btn')?.addEventListener('click', function() {
      if (this.disabled) return;
      if (_viewingCrowd) {
        // Toggle back to standard report
        _viewingCrowd = false;
        _renderReport(_reportData);
        _updateEnhanceBtn();
      } else if (_reportDataCrowd) {
        // Already cached — instant switch
        _viewingCrowd = true;
        _renderReport(_reportDataCrowd);
        _updateEnhanceBtn();
      } else {
        // Generate for the first time
        _startGenerateWithCrowd();
      }
    });

    // Inject limit message element (shown when non-admin hits CHAT_LIMIT)
    const chatCol = document.getElementById('ai-chat-col');
    if (chatCol && !document.getElementById('chat-lock-msg')) {
      const lock = document.createElement('div');
      lock.id        = 'chat-lock-msg';
      lock.className = 'chat-lock-msg';
      lock.style.display = 'none';
      chatCol.appendChild(lock);
    }
    _applyChatLock();
  }

  let _isAdmin   = false;
  let _chatCount = 0;         // messages sent this session (non-admin only)
  const CHAT_LIMIT = 2;

  function setAdmin(v) {
    _isAdmin = !!v;
    _applyChatLock();
  }

  let _crowdAvailable = false;

  function setCrowdAvailable(available) {
    _crowdAvailable = !!available;
    _updateEnhanceBtn();
  }

  function _updateEnhanceBtn() {
    const btn = document.getElementById('ai-enhance-crowd-btn');
    if (!btn) return;

    if (_viewingCrowd) {
      // Active crowd mode — clicking reverts to standard
      btn.disabled   = false;
      btn.textContent = '↩ View Standard Report';
      btn.classList.add('ai-enhance-btn--active');
      btn.title = 'Switch back to standard (no crowd) report';
    } else {
      btn.textContent = '⚡ Enhance with Crowd Data';
      btn.classList.remove('ai-enhance-btn--active');
      if (!_isAdmin) {
        btn.disabled = true;
        btn.title    = 'Admin access required';
      } else if (!_crowdAvailable) {
        btn.disabled = true;
        btn.title    = 'Requires crowd prediction to be run first';
      } else if (_reportDataCrowd) {
        // Already generated — instant toggle
        btn.disabled = false;
        btn.title    = 'Switch to crowd-enhanced report';
      } else {
        // Need to generate
        btn.disabled = false;
        btn.title    = 'Re-run AI report using latest crowd field reports';
      }
    }
  }

  function _applyChatLock() {
    const inputRow = document.querySelector('.chat-input-row');
    const lockMsg  = document.getElementById('chat-lock-msg');
    if (!inputRow) return;
    const limited = !_isAdmin && _chatCount >= CHAT_LIMIT;
    inputRow.style.display = limited ? 'none' : '';
    if (lockMsg) {
      lockMsg.style.display = limited ? '' : 'none';
      if (limited) lockMsg.textContent = 'Chat limit reached (' + CHAT_LIMIT + ' questions per session).';
    }
  }

  /** Called by app.js whenever a new timestep is selected. */
  function setContext(eid, tsid) {
    const changed = (eid !== _eid || tsid !== _tsid);
    _eid  = eid;
    _tsid = tsid;
    if (changed) {
      _reportLoaded    = false;
      _cardState       = 'idle';
      _history         = [];
      _reportData      = null;
      _reportDataCrowd = null;
      _viewingCrowd    = false;
      _clearChat();
      _updateEnhanceBtn();
    }
    // Update badge whether modal is open or not
    const badge = document.getElementById('ai-modal-badge');
    if (badge) badge.textContent = (eid && tsid) ? 'Event ' + eid + ' · TS ' + tsid : '';
  }

  /** Open the modal. Only opens when a report has already been generated. */
  function open() {
    if (!_eid || !_tsid || !_reportLoaded) return;
    document.getElementById('ai-modal-overlay').classList.add('visible');
  }

  // ── AI Analysis card ─────────────────────────────────────────────────────────

  /**
   * Try to load cached reports from server (standard + crowd if available).
   * Silent — 403 means no cache yet, card stays idle.
   */
  async function _tryLoadCached() {
    if (!_eid || !_tsid) return;
    const myGenId = ++_genId;
    try {
      const report = await window.API.generateReport(_eid, _tsid);
      if (myGenId !== _genId) return;
      _reportData   = report;
      _viewingCrowd = false;
      _renderReport(report);
      _renderInitialSuggestions();
      _reportLoaded = true;
      _cardState = 'done';
      _updateCard();
      _updateEnhanceBtn();
      // Silently fetch crowd cache if server says it exists
      if (report.has_crowd && !_reportDataCrowd) {
        window.API.generateReportWithCrowd(_eid, _tsid).then(function(cr) {
          _reportDataCrowd = cr;
          _updateEnhanceBtn();
        }).catch(function() {});
      }
    } catch(e) {
      // 403 = not yet generated (non-admin): card stays idle — expected
      if (myGenId !== _genId) return;
    }
  }

  /** Append AI Analysis card to #dashboard-content (call after renderDashboard). */
  function renderCard() {
    const content = document.getElementById('dashboard-content');
    if (!content || !_eid || !_tsid) return;
    const existing = document.getElementById('dash-ai-card');
    if (existing) existing.remove();

    const card = document.createElement('div');
    card.className = 'dash-card dash-card-ai';
    card.id = 'dash-ai-card';
    card.innerHTML =
      '<div class="dash-card-title">AI Analysis</div>' +
      '<div class="ai-card-body" id="ai-card-body"></div>';
    content.appendChild(card);
    _updateCard();
    // Main card click (not on the crowd button)
    card.addEventListener('click', function(e) {
      if (e.target.closest('#ai-crowd-btn')) return;
      _onCardClick();
    });
    // Auto-load from cache (all users — 403 silently ignored for non-admins)
    _tryLoadCached();
  }

  function _updateCard() {
    const body = document.getElementById('ai-card-body');
    if (!body) return;
    if (_cardState === 'idle') {
      body.className = 'ai-card-body idle';
      body.innerHTML = '<div class="ai-card-prompt">Generate AI report</div>';
    } else if (_cardState === 'loading') {
      body.className = 'ai-card-body loading';
      body.innerHTML = '<div class="spinner-sm"></div><div class="ai-card-status">Generating…</div>';
    } else if (_cardState === 'done') {
      body.className = 'ai-card-body done';
      body.innerHTML =
        '<div class="ai-card-ready">Report ready</div>' +
        '<div class="ai-card-view">Click to view →</div>';
    } else if (_cardState === 'crowd-loading') {
      body.className = 'ai-card-body loading';
      body.innerHTML = '<div class="spinner-sm"></div><div class="ai-card-status">Updating with crowd data…</div>';
    }
  }

  function _onCardClick() {
    if (_cardState === 'idle') {
      _startGenerate();
    } else if (_cardState === 'done') {
      open();
    }
    // loading / crowd-loading: ignore
  }

  async function _startGenerate(force) {
    if (!_eid || !_tsid) return;
    _cardState = 'loading';
    _updateCard();
    const myGenId = ++_genId;
    try {
      const report = await window.API.generateReport(_eid, _tsid, force || false);
      if (myGenId !== _genId) return;
      _reportData   = report;
      _viewingCrowd = false;
      _renderReport(report);
      _reportLoaded = true;
      _renderInitialSuggestions();
      _cardState = 'done';
      _updateCard();
      _updateEnhanceBtn();
      _showAIToast();
      // If crowd report is cached on server, load it silently
      if (report.has_crowd && !_reportDataCrowd) {
        window.API.generateReportWithCrowd(_eid, _tsid).then(function(cr) {
          _reportDataCrowd = cr;
          _updateEnhanceBtn();
        }).catch(function() {});
      }
    } catch(e) {
      if (myGenId !== _genId) return;
      _cardState = 'idle';
      _updateCard();
      const t = document.createElement('div');
      t.className = 'toast error';
      t.textContent = 'AI analysis failed: ' + _escHtml(e.message);
      document.body.appendChild(t);
      setTimeout(function() { t.remove(); }, 4000);
    }
  }

  async function _startGenerateWithCrowd(force) {
    if (!_eid || !_tsid) return;
    _cardState = 'crowd-loading';
    _updateCard();
    const enhBtn = document.getElementById('ai-enhance-crowd-btn');
    if (enhBtn) { enhBtn.disabled = true; enhBtn.textContent = '⏳ Enhancing…'; }
    const myGenId = ++_genId;
    try {
      const report = await window.API.generateReportWithCrowd(_eid, _tsid, force || false);
      if (myGenId !== _genId) return;
      _reportDataCrowd = report;
      _viewingCrowd    = true;
      _renderReport(report);
      _reportLoaded = true;
      _cardState = 'done';
      _updateCard();
      _updateEnhanceBtn();
      _showAIToast();
      open();
    } catch(e) {
      if (myGenId !== _genId) return;
      _cardState = 'done';
      _updateCard();
      _updateEnhanceBtn();
      const t = document.createElement('div');
      t.className = 'toast error';
      t.textContent = 'Crowd update failed: ' + _escHtml(e.message);
      document.body.appendChild(t);
      setTimeout(function() { t.remove(); }, 4000);
    }
  }

  function _showAIToast() {
    const existing = document.getElementById('ai-toast');
    if (existing) existing.remove();
    const toast = document.createElement('div');
    toast.className = 'ai-toast';
    toast.id = 'ai-toast';
    toast.innerHTML = '<span class="ai-toast-msg">AI Analysis ready</span><span class="ai-toast-action">View →</span>';
    toast.addEventListener('click', function() {
      open();
      toast.remove();
    });
    document.body.appendChild(toast);
    setTimeout(function() {
      toast.classList.add('dismissing');
      setTimeout(function() { if (toast.parentNode) toast.remove(); }, 400);
    }, 6000);
  }

  function close() {
    document.getElementById('ai-modal-overlay').classList.remove('visible');
  }

  // ── Report ───────────────────────────────────────────────────────────────────

  async function _loadReport() {
    _showLoading(true);
    try {
      const report = await window.API.generateReport(_eid, _tsid);
      _renderReport(report);
      _reportLoaded = true;
      _renderInitialSuggestions();
    } catch(e) {
      _showLoading(false);
      document.getElementById('ai-report-loading').innerHTML =
        '<div style="padding:20px;color:var(--danger);font-size:12px">Failed: ' + _escHtml(e.message) + '</div>';
    }
  }

  function _showLoading(on) {
    document.getElementById('ai-report-loading').classList.toggle('hidden', !on);
    document.getElementById('ai-report-ready').classList.toggle('hidden', on);
  }

  function _renderReport(report) {
    const tabs = [
      { id: 'overview',   label: 'Overview' },
      { id: 'risk',       label: 'Risk' },
      { id: 'impact',     label: 'Impact' },
      { id: 'evacuation', label: 'Evacuation' },
    ];
    if (report.crowd) {
      tabs.push({ id: 'crowd', label: 'Crowd' });
    }

    const tabsEl   = document.getElementById('ai-report-tabs');
    const panelsEl = document.getElementById('ai-report-panels');

    tabsEl.innerHTML = tabs.map(function(t, i) {
      return '<button class="report-tab' + (i === 0 ? ' active' : '') + '" data-tab="' + t.id + '">' + t.label + '</button>';
    }).join('');

    const renderers = {
      overview:   function() { return _renderOverviewPanel(report); },
      risk:       function() { return _renderRiskPanel(report.risk); },
      impact:     function() { return _renderImpactPanel(report.impact); },
      evacuation: function() { return _renderEvacPanel(report.evacuation); },
      crowd:      function() { return _renderCrowdPanel(report.crowd); },
    };

    panelsEl.innerHTML = tabs.map(function(t, i) {
      const content = renderers[t.id] ? renderers[t.id]() : '<div class="report-na">Not available</div>';
      return '<div class="report-panel' + (i === 0 ? ' active' : '') + '" id="panel-' + t.id + '">' + content + '</div>';
    }).join('');

    tabsEl.querySelectorAll('.report-tab').forEach(function(btn) {
      btn.addEventListener('click', function() {
        tabsEl.querySelectorAll('.report-tab').forEach(function(b) { b.classList.remove('active'); });
        panelsEl.querySelectorAll('.report-panel').forEach(function(p) { p.classList.remove('active'); });
        btn.classList.add('active');
        document.getElementById('panel-' + btn.dataset.tab).classList.add('active');
      });
    });

    _showLoading(false);
  }

  // ── Structured panel renderers ───────────────────────────────────────────────

  function _card(title, bodyHtml) {
    return '<div class="rpt-json-card"><div class="rpt-json-card-title">' + _escHtml(title) + '</div>' +
           '<div class="rpt-json-card-body">' + bodyHtml + '</div></div>';
  }

  function _kv(label, value) {
    if (!value && value !== 0) return '';
    return '<div class="rpt-kv"><span class="rpt-key">' + _escHtml(label) + '</span>' +
           '<span class="rpt-val">' + _escHtml(String(value)) + '</span></div>';
  }

  function _tagList(items) {
    if (!items || !items.length) return '';
    return '<div class="rpt-tag-list">' +
      items.map(function(s) { return '<span class="rpt-tag">' + _escHtml(s) + '</span>'; }).join('') +
      '</div>';
  }

  function _renderRiskPanel(risk) {
    if (!risk) return '<div class="report-na">Not available</div>';
    let html = '';
    if (risk.overall_assessment) {
      html += _card('Overall Assessment', '<p class="rpt-p">' + _escHtml(risk.overall_assessment) + '</p>');
    }
    if (risk.fire_behaviour) {
      html += _card('Fire Behaviour', '<p class="rpt-p">' + _escHtml(risk.fire_behaviour) + '</p>');
    }
    if (risk.growth_trajectory) {
      html += _card('Growth Trajectory', '<p class="rpt-p">' + _escHtml(risk.growth_trajectory) + '</p>');
    }
    if (risk.weather_drivers) {
      html += _card('Weather Drivers', '<p class="rpt-p">' + _escHtml(risk.weather_drivers) + '</p>');
    }
    if (risk.risk_factors && risk.risk_factors.length) {
      html += _card('Risk Factors', _tagList(risk.risk_factors));
    }
    return html || '<div class="report-na">Not available</div>';
  }

  function _renderImpactPanel(impact) {
    if (!impact) return '<div class="report-na">Not available</div>';
    let html = '';

    // Population counts
    const pop = impact.population || {};
    if (Object.keys(pop).length) {
      let popHtml = '';
      if (pop.within_perimeter != null) popHtml += _kv('Within perimeter', pop.within_perimeter.toLocaleString());
      if (pop.at_risk_3h  != null) popHtml += _kv('At risk +3h',  pop.at_risk_3h.toLocaleString());
      if (pop.at_risk_6h  != null) popHtml += _kv('At risk +6h',  pop.at_risk_6h.toLocaleString());
      if (pop.at_risk_12h != null) popHtml += _kv('At risk +12h', pop.at_risk_12h.toLocaleString());
      if (popHtml) html += _card('Population Exposure', popHtml);
    }

    // Communities
    if (impact.communities_affected && impact.communities_affected.length) {
      const rows = impact.communities_affected.map(function(c) {
        const sev = c.severity ? '<span class="rpt-tag sev-' + c.severity + '">' + c.severity + '</span>' : '';
        return '<div class="rpt-community">' +
          '<span class="rpt-community-name">' + _escHtml(c.name || '') + '</span>' + sev +
          (c.exposure ? '<span class="rpt-community-desc">' + _escHtml(c.exposure) + '</span>' : '') +
          '</div>';
      }).join('');
      html += _card('Communities Affected', rows);
    }

    if (impact.impact_summary) {
      html += _card('Impact Summary', '<p class="rpt-p">' + _escHtml(impact.impact_summary) + '</p>');
    }

    if (impact.worsening_factors && impact.worsening_factors.length) {
      html += _card('Worsening Factors', _tagList(impact.worsening_factors));
    }

    return html || '<div class="report-na">Not available</div>';
  }

  function _renderEvacPanel(evac) {
    if (!evac) return '<div class="report-na">Not available</div>';
    let html = '';

    function _routeCard(title, route) {
      if (!route) return '';
      let inner = '';
      if (route.path && route.path.length) {
        inner += '<div class="rpt-route-path">' +
          route.path.map(function(s) { return '<span class="rpt-waypoint">' + _escHtml(s) + '</span>'; }).join('<span class="rpt-arrow">→</span>') +
          '</div>';
      }
      if (route.status)    inner += _kv('Status',    route.status);
      if (route.window)    inner += _kv('Window',    route.window);
      if (route.reasoning) inner += '<p class="rpt-p rpt-reasoning">' + _escHtml(route.reasoning) + '</p>';
      return _card(title, inner);
    }

    html += _routeCard('Top Route', evac.top_route);
    html += _routeCard('Alternative Route', evac.alternative_route);

    if (evac.road_warnings && evac.road_warnings.length) {
      html += _card('Road Warnings', _tagList(evac.road_warnings));
    }

    return html || '<div class="report-na">Not available</div>';
  }

  function _renderCrowdPanel(crowd) {
    if (!crowd) return '<div class="report-na">Not available</div>';
    let html = '';

    // Report counts
    const counts = crowd.report_counts || {};
    if (counts.total) {
      let countsHtml = '';
      countsHtml += _kv('Total reports', counts.total);
      if (counts.fire_report)   countsHtml += _kv('Fire reports',    counts.fire_report);
      if (counts.info)          countsHtml += _kv('Info reports',    counts.info);
      if (counts.request_help)  countsHtml += _kv('Help requests',   counts.request_help);
      if (counts.need_help)     countsHtml += _kv('Urgent help',     counts.need_help);
      if (counts.offer_help)    countsHtml += _kv('Offers of help',  counts.offer_help);
      html += _card('Signal Summary', countsHtml);
    }

    if (crowd.urgent_help && crowd.urgent_help.length) {
      const urgentHtml = crowd.urgent_help.map(function(s) {
        return '<div class="rpt-urgent-item">⚠ ' + _escHtml(s) + '</div>';
      }).join('');
      html += _card('Urgent Help Requests', urgentHtml);
    }

    if (crowd.fire_observations && !/No crowd reports/i.test(crowd.fire_observations)) {
      html += _card('Fire Observations', '<p class="rpt-p">' + _escHtml(crowd.fire_observations) + '</p>');
    }

    if (crowd.situational_info) {
      html += _card('Situational Information', '<p class="rpt-p">' + _escHtml(crowd.situational_info) + '</p>');
    }

    if (crowd.notable_patterns) {
      html += _card('Notable Patterns', '<p class="rpt-p">' + _escHtml(crowd.notable_patterns) + '</p>');
    }

    if (!html) {
      html = '<div class="report-na">No crowd reports available for this timestep.</div>';
    }

    return html;
  }

  function _renderOverviewPanel(report) {
    let html = '';

    // Risk level badge
    if (report.risk_level && report.risk_level !== 'Unknown') {
      const lvlMap = { critical: 'critical', high: 'high', moderate: 'moderate', low: 'low' };
      const cls = lvlMap[report.risk_level.toLowerCase()] || 'high';
      html += '<div class="risk-badge ' + cls + '">' +
        '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" style="flex-shrink:0">' +
        '<path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/></svg>' +
        _escHtml(report.risk_level) + ' Risk' +
        '</div>';
    }

    // Key points
    if (report.key_points && report.key_points.length) {
      html += '<div class="report-key-points">' +
        '<div class="kp-title">Key Points</div>' +
        '<ul class="kp-list">' +
        report.key_points.map(function(p) {
          return '<li>' + _escHtml(p) + '</li>';
        }).join('') +
        '</ul></div>';
    }

    // Stat tiles — pulled from structured specialist data
    const tiles = [];

    const impact = report.impact || {};
    const pop = impact.population || {};
    const atRisk12 = pop.at_risk_12h;
    if (atRisk12 != null) {
      tiles.push({ icon: '👥', label: 'At risk +12h', value: Number(atRisk12).toLocaleString() });
    }
    if (pop.within_perimeter != null) {
      tiles.push({ icon: '🔥', label: 'Within perimeter', value: Number(pop.within_perimeter).toLocaleString() });
    }

    const evac = report.evacuation || {};
    if (evac.top_route && evac.top_route.window) {
      tiles.push({ icon: '🛣', label: 'Top route window', value: evac.top_route.window });
    } else if (evac.top_route && evac.top_route.path && evac.top_route.path.length) {
      tiles.push({ icon: '🛣', label: 'Top route', value: evac.top_route.path[0] + ' → ' + evac.top_route.path[evac.top_route.path.length - 1] });
    }

    const crowd = report.crowd;
    if (crowd && crowd.report_counts) {
      const total = crowd.report_counts.total || 0;
      const urgent = crowd.report_counts.need_help || 0;
      const label = urgent ? total + ' reports (' + urgent + ' urgent)' : total + ' reports';
      tiles.push({ icon: '📍', label: 'Crowd reports', value: label });
    }

    if (tiles.length) {
      html += '<div class="ov-stat-row">' +
        tiles.map(function(t) {
          return '<div class="ov-stat-tile">' +
            '<span class="ov-stat-icon">' + t.icon + '</span>' +
            '<span class="ov-stat-val">' + _escHtml(t.value) + '</span>' +
            '<span class="ov-stat-label">' + _escHtml(t.label) + '</span>' +
            '</div>';
        }).join('') +
        '</div>';
    }

    // Briefing sections
    const hasBriefing = report.situation || report.key_risks || report.immediate_actions;
    if (hasBriefing) {
      html += '<div class="report-briefing-label"><span class="briefing-line"></span><span class="briefing-title">Briefing</span><span class="briefing-line"></span></div>';
      // If only situation is filled (LLM returned full text in one field), label it generically
      const threeFields = report.key_risks || report.immediate_actions;
      if (report.situation) {
        html += _card(threeFields ? 'Situation' : 'Executive Briefing',
          '<p class="rpt-p">' + _escHtml(report.situation) + '</p>');
      }
      if (report.key_risks) {
        html += _card('Key Risks', '<p class="rpt-p">' + _escHtml(report.key_risks) + '</p>');
      }
      if (report.immediate_actions) {
        html += _card('Immediate Actions', '<p class="rpt-p">' + _escHtml(report.immediate_actions) + '</p>');
      }
    }

    return html || '<div class="report-na">Not available</div>';
  }

  // ── Chat ─────────────────────────────────────────────────────────────────────

  function _clearChat() {
    const msgs = document.getElementById('chat-messages');
    if (msgs) msgs.innerHTML = '<div class="chat-welcome">Ask anything about this fire event and the AI analysis above.</div>';
  }

  function _renderInitialSuggestions() {
    const msgs = document.getElementById('chat-messages');
    if (!msgs) return;
    msgs.querySelectorAll('.suggested-qs.initial-qs').forEach(function(el) { el.remove(); });
    const qs = _reportData ? _generateInitialQuestions(_reportData) : [];
    if (!qs.length) return;
    const div = document.createElement('div');
    div.className = 'suggested-qs initial-qs';
    div.innerHTML = '<div class="sq-label">Suggested questions</div>' +
      qs.map(function(q) {
        return '<button class="sq-btn" onclick="window.__askQ(this)">' + _escHtml(q) + '</button>';
      }).join('');
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
  }

  function _escHtml(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function _fmtInline(s) {
    return s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  }

  function _fmt(text) {
    const lines = text.split('\n');
    let html = '';
    let firstHead = true;

    for (let i = 0; i < lines.length; i++) {
      const raw  = lines[i];
      const line = raw.trim();
      const esc  = _escHtml(line);

      if (!line) {
        html += '<div class="rpt-spacer"></div>';
        continue;
      }

      // ALL-CAPS standalone line → section header (TOP ROUTE, ALTERNATIVE ROUTE, etc.)
      if (/^[A-Z][A-Z\s\-]{3,}$/.test(line)) {
        const mt = firstHead ? ' rpt-head-first' : '';
        html += '<div class="rpt-section-head' + mt + '">' + esc + '</div>';
        firstHead = false;
        continue;
      }

      // "Situation →", "Key Risks →", "Immediate Actions →"
      const arrowMatch = line.match(/^([A-Z][^→\n]{2,30})\s*→\s*(.*)/);
      if (arrowMatch) {
        const mt = firstHead ? ' rpt-head-first' : '';
        html += '<div class="rpt-section-head' + mt + '">' + _escHtml(arrowMatch[1].trim()) + '</div>';
        firstHead = false;
        if (arrowMatch[2].trim()) {
          html += '<p class="rpt-p">' + _fmtInline(_escHtml(arrowMatch[2].trim())) + '</p>';
        }
        continue;
      }

      // "- Key: value" key-value row
      const kvMatch = line.match(/^[-•]\s*([A-Za-z][A-Za-z\s]{1,20}):\s+(.*)/);
      if (kvMatch) {
        html += '<div class="rpt-kv">' +
          '<span class="rpt-key">' + _escHtml(kvMatch[1]) + '</span>' +
          '<span class="rpt-val">' + _fmtInline(_escHtml(kvMatch[2])) + '</span>' +
          '</div>';
        continue;
      }

      // "- bullet" / "• bullet"
      if (/^[-•]\s+/.test(line)) {
        html += '<div class="rpt-bullet">▸ ' + _fmtInline(esc.replace(/^[-•]\s+/, '')) + '</div>';
        continue;
      }

      // Numbered list "1. ..."
      if (/^\d+\.\s+/.test(line)) {
        html += '<div class="rpt-bullet">' + _fmtInline(esc) + '</div>';
        continue;
      }

      // Normal paragraph
      html += '<p class="rpt-p">' + _fmtInline(esc) + '</p>';
    }

    return html;
  }

  function _renderMsg(text) {
    const parts = text.split(/\n+Suggested questions:\s*/i);
    let html = '<div class="msg-body">' + _fmt(parts[0]) + '</div>';
    if (parts[1]) {
      const qs = parts[1].split('\n').map(function(l) { return l.replace(/^\d+\.\s*/, '').trim(); }).filter(Boolean);
      if (qs.length) {
        html += '<div class="suggested-qs"><div class="sq-label">Suggested questions</div>' +
          qs.map(function(q) { return '<button class="sq-btn" onclick="window.__askQ(this)">' + _escHtml(q) + '</button>'; }).join('') +
          '</div>';
      }
    }
    return html;
  }

  window.__askQ = function(btn) {
    document.getElementById('chat-input').value = btn.textContent;
    _send();
  };

  function _appendMsg(role, content, id) {
    const msgs = document.getElementById('chat-messages');
    const div  = document.createElement('div');
    div.className = 'chat-msg-' + role;
    if (id) div.id = id;
    div.innerHTML = _renderMsg(content);
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
    return div;
  }

  function _send() {
    if (_streaming || !_eid) return;
    if (!_isAdmin && _chatCount >= CHAT_LIMIT) return;
    const input = document.getElementById('chat-input');
    const msg   = input.value.trim();
    if (!msg) return;
    input.value = '';
    if (!_isAdmin) {
      _chatCount++;
      _applyChatLock();
    }

    _appendMsg('user', msg);
    _history.push({ role: 'user', content: msg });

    const aId  = 'amsg-' + Date.now();
    const aDiv = _appendMsg('assistant', '…', aId);

    _streaming = true;
    document.getElementById('chat-send-btn').style.opacity = '.4';

    let full = '';
    const prevHistory = _history.slice(0, -1);

    window.API.streamChat(
      _eid,
      { message: msg, timestep_id: _tsid, history: prevHistory },
      function(chunk) {
        full += chunk;
        aDiv.innerHTML = _renderMsg(full + '▌');
        document.getElementById('chat-messages').scrollTop = 999999;
      },
      function() {
        aDiv.innerHTML = _renderMsg(full);
        _history.push({ role: 'assistant', content: full });
        _streaming = false;
        document.getElementById('chat-send-btn').style.opacity = '';
        document.getElementById('chat-messages').scrollTop = 999999;
      },
      function(err) {
        aDiv.innerHTML = '<div class="msg-body error-msg">Error: ' + _escHtml(err) + '</div>';
        _streaming = false;
        document.getElementById('chat-send-btn').style.opacity = '';
      }
    );
  }

  window.AIModal = { init, setContext, setAdmin, setCrowdAvailable, open, close, renderCard };
})();
