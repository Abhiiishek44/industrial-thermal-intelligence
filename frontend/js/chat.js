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

  function _positionChatLauncher() {
    const launcher = document.getElementById('ai-chat-launcher');
    const controlStack = document.querySelector('#event-map .leaflet-top.leaflet-right');
    if (!launcher || !controlStack) return;
    const bounds = controlStack.getBoundingClientRect();
    if (!bounds.height || !bounds.width) return;
    launcher.style.top = Math.ceil(bounds.bottom + 8) + 'px';
    launcher.style.bottom = 'auto';
    launcher.style.transform = 'none';
  }

  function _generateInitialQuestions(report) {
    const qs = [];
    const thermal = report.report_mode === 'thermal_monitoring';

    if (thermal) {
      qs.push('What evidence supports the thermal source assessment?');
      qs.push('What uncertainties require ground verification?');
    } else if (report.risk_level && report.risk_level !== 'Unknown') {
      qs.push('Why is the risk level classified as ' + report.risk_level + '?');
    }

    if (report.key_points && report.key_points.length) {
      report.key_points.slice(0, 2).forEach(function(pt) {
        qs.push('Tell me more about: ' + pt.replace(/\.$/, ''));
      });
    }

    const evac = report.evacuation || {};
    if (!thermal && evac.data_available !== false && evac.top_route && evac.top_route.path && evac.top_route.path.length) {
      qs.push('What is the recommended primary evacuation route?');
    }
    if (!thermal && evac.data_available !== false && evac.alternative_route && evac.alternative_route.window) {
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
    document.getElementById('ai-analysis-btn')?.addEventListener('click', _onCardClick);
    document.getElementById('ai-chat-launcher')?.addEventListener('click', openChat);
    document.getElementById('ai-chat-drawer-close')?.addEventListener('click', closeChat);
    document.getElementById('ai-chat-drawer-backdrop')?.addEventListener('click', closeChat);
    document.getElementById('ai-modal-close').addEventListener('click', close);
    document.getElementById('ai-modal-overlay').addEventListener('click', function(e) {
      if (e.target === document.getElementById('ai-modal-overlay')) close();
    });
    document.getElementById('chat-send-btn').addEventListener('click', _send);
    document.getElementById('chat-input').addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _send(); }
    });
    document.addEventListener('keydown', function(e) {
      if (e.key === 'Escape') {
        closeChat();
        close();
      }
    });
    window.addEventListener('resize', _positionChatLauncher);
    document.addEventListener('click', function(e) {
      if (e.target.closest('#event-map .leaflet-top.leaflet-right')) {
        setTimeout(_positionChatLauncher, 0);
      }
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
    const chatDrawer = document.getElementById('ai-chat-drawer');
    if (chatDrawer && !document.getElementById('chat-lock-msg')) {
      const lock = document.createElement('div');
      lock.id        = 'chat-lock-msg';
      lock.className = 'chat-lock-msg';
      lock.style.display = 'none';
      chatDrawer.insertBefore(lock, chatDrawer.querySelector('.chat-input-shell'));
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
    const launcher = document.getElementById('ai-chat-launcher');
    if (launcher) {
      launcher.classList.toggle('hidden', !(eid && tsid));
      launcher.setAttribute('aria-expanded', document.getElementById('ai-chat-drawer')?.classList.contains('open') ? 'true' : 'false');
    }
    const contextTitle = document.getElementById('ai-chat-context-title');
    const contextMeta = document.getElementById('ai-chat-context-meta');
    const breadcrumb = document.getElementById('breadcrumb')?.textContent || '';
    if (contextTitle) contextTitle.textContent = breadcrumb ? breadcrumb.split(' · ')[0] : 'Current observation';
    if (contextMeta) contextMeta.textContent = (eid && tsid) ? 'Event ' + eid + ' · Timestep ' + tsid : 'Select a timestep to begin';
    if (!(eid && tsid)) closeChat();
    _updateCard();
    requestAnimationFrame(_positionChatLauncher);
    setTimeout(_positionChatLauncher, 100);
  }

  /** Open the modal. Only opens when a report has already been generated. */
  function open() {
    if (!_eid || !_tsid || !_reportLoaded) return;
    document.getElementById('ai-modal-overlay').classList.add('visible');
  }

  function openChat() {
    if (!_eid || !_tsid) return;
    // On compact layouts the launcher lives inside the controls sheet. Close
    // that sheet before revealing the assistant so closing chat returns to the
    // map, not to another overlay.
    window._mobileClosePanel?.();
    const drawer = document.getElementById('ai-chat-drawer');
    const backdrop = document.getElementById('ai-chat-drawer-backdrop');
    const launcher = document.getElementById('ai-chat-launcher');
    drawer?.classList.add('open');
    backdrop?.classList.add('visible');
    drawer?.setAttribute('aria-hidden', 'false');
    launcher?.setAttribute('aria-expanded', 'true');
    setTimeout(function() { document.getElementById('chat-input')?.focus(); }, 180);
  }

  function closeChat() {
    const drawer = document.getElementById('ai-chat-drawer');
    const backdrop = document.getElementById('ai-chat-drawer-backdrop');
    const launcher = document.getElementById('ai-chat-launcher');
    drawer?.classList.remove('open');
    backdrop?.classList.remove('visible');
    drawer?.setAttribute('aria-hidden', 'true');
    launcher?.setAttribute('aria-expanded', 'false');
  }

  // ── AI Analysis top action ───────────────────────────────────────────────────

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

  /** Refresh the top action and load any cached report for the selected timestep. */
  function renderCard() {
    const existing = document.getElementById('dash-ai-card');
    if (existing) existing.remove();
    _updateCard();
    if (!_eid || !_tsid) return;
    // Auto-load from cache (all users — 403 silently ignored for non-admins)
    _tryLoadCached();
  }

  function _updateCard() {
    const button = document.getElementById('ai-analysis-btn');
    if (!button) return;
    const available = !!(_eid && _tsid);
    button.classList.toggle('hidden', !available);
    button.dataset.state = _cardState;
    button.disabled = !available || _cardState === 'loading' || _cardState === 'crowd-loading';
    const label = button.querySelector('.ai-trigger-label');
    if (!label) return;
    if (_cardState === 'idle') {
      label.textContent = 'Create report';
      button.title = 'Generate situational awareness report';
    } else if (_cardState === 'loading') {
      label.textContent = 'Generating…';
      button.title = 'Generating situational awareness report';
    } else if (_cardState === 'done') {
      label.textContent = 'View report';
      button.title = 'Open situational awareness report';
    } else if (_cardState === 'crowd-loading') {
      label.textContent = 'Updating…';
      button.title = 'Updating report with crowd data';
    }
    button.setAttribute('aria-label', button.title);
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
    toast.innerHTML = '<span class="ai-toast-msg">Report ready</span><span class="ai-toast-action">View →</span>';
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
    const thermal = report.report_mode === 'thermal_monitoring';
    const tabs = [
      { id: 'overview', label: 'Overview', hint: 'Decision brief' },
      { id: 'risk', label: thermal ? 'Thermal analysis' : 'Risk analysis', hint: thermal ? 'Source intelligence' : 'Fire behaviour' },
    ];
    if (!thermal) {
      tabs.push({ id: 'impact', label: 'Impact', hint: 'People & places' });
    }
    if (!thermal && (!report.evacuation || report.evacuation.data_available !== false)) {
      tabs.push({ id: 'evacuation', label: 'Evacuation', hint: 'Routes & access' });
    }
    if (!thermal && report.crowd) {
      tabs.push({ id: 'crowd', label: 'Field reports', hint: 'Community signals' });
    }

    const tabsEl   = document.getElementById('ai-report-tabs');
    const panelsEl = document.getElementById('ai-report-panels');
    const metadata = report.metadata || {};
    const region = metadata.region || {};
    const badge = document.getElementById('ai-modal-badge');
    if (badge) badge.textContent = thermal ? 'Thermal monitoring' : 'Wildfire response';
    const chatTitle = document.getElementById('ai-chat-context-title');
    if (chatTitle && region.name) chatTitle.textContent = region.name;

    tabsEl.innerHTML = tabs.map(function(t, i) {
      return '<button class="report-tab' + (i === 0 ? ' active' : '') + '" data-tab="' + t.id + '" aria-selected="' + (i === 0 ? 'true' : 'false') + '">' +
        '<span class="report-tab-icon" aria-hidden="true">' + _tabIcon(t.id) + '</span>' +
        '<span class="report-tab-copy"><strong>' + _escHtml(t.label) + '</strong><small>' + _escHtml(t.hint) + '</small></span>' +
        '<span class="report-tab-arrow" aria-hidden="true">›</span></button>';
    }).join('');

    const renderers = {
      overview:   function() { return _renderOverviewPanel(report); },
      risk:       function() { return thermal ? _renderThermalPanel(report.risk) : _renderRiskPanel(report.risk); },
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
        tabsEl.querySelectorAll('.report-tab').forEach(function(b) { b.classList.remove('active'); b.setAttribute('aria-selected', 'false'); });
        panelsEl.querySelectorAll('.report-panel').forEach(function(p) { p.classList.remove('active'); });
        btn.classList.add('active');
        btn.setAttribute('aria-selected', 'true');
        document.getElementById('panel-' + btn.dataset.tab).classList.add('active');
        panelsEl.scrollTop = 0;
        const modalBody = document.querySelector('.ai-modal-body');
        if (modalBody) modalBody.scrollTop = 0;
      });
    });

    _showLoading(false);
  }

  // ── Structured panel renderers ───────────────────────────────────────────────

  function _tabIcon(id) {
    const icons = {
      overview: '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/></svg>',
      risk: '<svg viewBox="0 0 24 24"><path d="M12 3a9 9 0 1 0 9 9"/><path d="M12 7v5l3 2"/><path d="M16 3h5v5"/></svg>',
      impact: '<svg viewBox="0 0 24 24"><circle cx="9" cy="8" r="3"/><path d="M3.5 19c.6-4 2.4-6 5.5-6s4.9 2 5.5 6"/><path d="M15 6.5a3 3 0 0 1 0 5.8M16 14c2.5.5 4 2.1 4.5 5"/></svg>',
      evacuation: '<svg viewBox="0 0 24 24"><path d="M4 19V5l7 3 9-3v14l-9 3-7-3Z"/><path d="M11 8v14M15 7l2 2-2 2"/></svg>',
      crowd: '<svg viewBox="0 0 24 24"><path d="M4 5h16v11H8l-4 4V5Z"/><path d="M8 9h8M8 12h5"/></svg>',
    };
    return icons[id] || icons.overview;
  }

  function _metricIcon(id) {
    const icons = {
      users: '<svg viewBox="0 0 24 24"><circle cx="9" cy="8" r="3"/><path d="M3.5 19c.6-4 2.4-6 5.5-6s4.9 2 5.5 6M15 6.5a3 3 0 0 1 0 5.8M16 14c2.5.5 4 2.1 4.5 5"/></svg>',
      unavailable: '<svg viewBox="0 0 24 24"><ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3.1 3 7 3 1 0 2-.1 2.8-.3M5 12v5c0 1.7 3.1 3 7 3M17 16l4 4m0-4-4 4"/></svg>',
      fire: '<svg viewBox="0 0 24 24"><path d="M13.5 3s.7 3.1-1.5 4.8C9.9 9.4 9.2 11 10 13c-2-1-3-3-2-5-3 2.4-4 5-3 8a7.2 7.2 0 0 0 14 0c1-4-1.3-9-5.5-13Z"/></svg>',
      route: '<svg viewBox="0 0 24 24"><path d="M5 19c0-7 14-3 14-10"/><circle cx="5" cy="19" r="2"/><circle cx="19" cy="7" r="2"/></svg>',
      reports: '<svg viewBox="0 0 24 24"><path d="M4 5h16v12H8l-4 4V5Z"/><path d="M8 9h8M8 13h5"/></svg>',
    };
    return icons[id] || icons.fire;
  }

  function _panelHead(eyebrow, title, description, status) {
    return '<header class="panel-page-head"><div><span>' + _escHtml(eyebrow) + '</span><h3>' + _escHtml(title) + '</h3>' +
      (description ? '<p>' + _escHtml(description) + '</p>' : '') + '</div>' +
      (status ? '<span class="panel-status"><i></i>' + _escHtml(status) + '</span>' : '') + '</header>';
  }

  function _sectionTitle(title, description) {
    return '<div class="report-section-heading"><div><h4>' + _escHtml(title) + '</h4>' +
      (description ? '<p>' + _escHtml(description) + '</p>' : '') + '</div></div>';
  }

  function _emptyState(title, description, reason) {
    return '<div class="report-empty-state"><div class="report-empty-icon">' +
      '<svg viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="7" ry="3"/><path d="M5 5v6c0 1.7 3.1 3 7 3s7-1.3 7-3V5M5 11v6c0 1.7 3.1 3 7 3 1.2 0 2.4-.1 3.3-.4"/><path d="m17 16 4 4m0-4-4 4"/></svg></div>' +
      '<div class="report-empty-copy"><span>Data connection required</span><h4>' + _escHtml(title) + '</h4><p>' + _escHtml(description) + '</p></div>' +
      '<div class="report-empty-note"><strong>Why this is unavailable</strong><p>' + _escHtml(reason) + '</p></div></div>';
  }

  function _card(title, bodyHtml, extraClass) {
    return '<div class="rpt-json-card ' + (extraClass || '') + '"><div class="rpt-json-card-title">' + _escHtml(title) + '</div>' +
           '<div class="rpt-json-card-body">' + bodyHtml + '</div></div>';
  }

  function _kv(label, value) {
    if (!value && value !== 0) return '';
    return '<div class="rpt-kv"><span class="rpt-key">' + _escHtml(label) + '</span>' +
           '<span class="rpt-val">' + _escHtml(String(value)) + '</span></div>';
  }

  function _tagList(items, tone) {
    if (!items || !items.length) return '';
    return '<div class="rpt-list ' + (tone || '') + '">' +
      items.map(function(s) { return '<div class="rpt-list-item"><span class="rpt-list-icon">' + (tone === 'action' ? '✓' : tone === 'warning' ? '!' : '•') + '</span><span>' + _escHtml(s) + '</span></div>'; }).join('') +
      '</div>';
  }

  function _compactText(value, maxLength) {
    const text = String(value || '').replace(/\s+/g, ' ').trim();
    const limit = maxLength || 190;
    if (text.length <= limit) return text;
    const sentence = text.slice(0, limit + 1).match(/^(.{60,}?[.!?])(?:\s|$)/);
    if (sentence) return sentence[1];
    const clipped = text.slice(0, limit);
    const boundary = clipped.lastIndexOf(' ');
    return clipped.slice(0, boundary > 110 ? boundary : limit).replace(/[,:;\s]+$/, '') + '…';
  }

  function _briefCard(title, value, tone) {
    if (!value) return '';
    const full = String(value).replace(/\s+/g, ' ').trim();
    const summary = _compactText(full);
    const details = summary !== full
      ? '<details class="brief-details"><summary>View complete analysis</summary><p>' + _escHtml(full) + '</p></details>'
      : '';
    const marks = { signal: '◉', danger: '!', warning: '?', weather: '↗', action: '✓' };
    return '<article class="brief-card ' + (tone || '') + '">' +
      '<div class="brief-card-head"><span class="brief-card-icon">' + (marks[tone] || '•') + '</span><div class="brief-card-title">' + _escHtml(title) + '</div></div>' +
      '<p class="brief-card-summary">' + _escHtml(summary) + '</p>' + details + '</article>';
  }

  function _renderRiskPanel(risk) {
    if (!risk) return '<div class="report-na">Not available</div>';
    let html = '';
    if (risk.overall_assessment) {
      html += _briefCard('Overall assessment', risk.overall_assessment, 'signal');
    }
    if (risk.fire_behaviour) {
      html += _briefCard('Fire behaviour', risk.fire_behaviour, 'danger');
    }
    if (risk.growth_trajectory) {
      html += _briefCard('Growth trajectory', risk.growth_trajectory, 'warning');
    }
    if (risk.weather_drivers) {
      html += _briefCard('Weather drivers', risk.weather_drivers, 'weather');
    }
    if (risk.risk_factors && risk.risk_factors.length) {
      html += _card('Risk factors', _tagList(risk.risk_factors, 'warning'), 'span-full');
    }
    return _panelHead('Operational intelligence', 'Wildfire risk analysis', 'Current behaviour, growth potential, and the conditions influencing spread.', 'Model assessed') +
      (html ? '<div class="report-card-grid">' + html + '</div>' : '<div class="report-na">Not available</div>');
  }

  function _renderThermalPanel(analysis) {
    if (!analysis) return '<div class="report-na">Not available</div>';
    let summary = '';
    let details = '';
    if (analysis.detection_summary) {
      summary += _briefCard('Observed activity', analysis.detection_summary, 'signal');
    }
    if (analysis.source_assessment) {
      summary += _briefCard('Likely source', analysis.source_assessment, 'warning');
    }
    if (analysis.persistence_assessment) {
      summary += _briefCard('Persistence', analysis.persistence_assessment, 'weather');
    }
    if (analysis.context_factors && analysis.context_factors.length) {
      details += _card('Supporting evidence', _tagList(analysis.context_factors, 'evidence'));
    }
    if (analysis.uncertainties && analysis.uncertainties.length) {
      details += _card('Known uncertainties', _tagList(analysis.uncertainties, 'warning'));
    }
    if (analysis.recommended_checks && analysis.recommended_checks.length) {
      details += _card('Recommended verification', _tagList(analysis.recommended_checks, 'action'), 'span-full');
    }
    const body = (summary ? '<div class="analysis-summary-grid">' + summary + '</div>' : '') +
      (details ? _sectionTitle('Evidence & verification', 'What supports the assessment, what remains unknown, and what to check next.') + '<div class="analysis-detail-grid">' + details + '</div>' : '');
    return _panelHead('Satellite intelligence', 'Thermal source analysis', 'A structured assessment of observed heat signatures—not a validated wildfire-spread forecast.', 'Satellite observed') +
      (body || '<div class="report-na">Not available</div>');
  }

  function _renderImpactPanel(impact) {
    if (!impact) return '<div class="report-na">Not available</div>';
    let html = '';

    // Population counts
    const pop = impact.population || {};
    if (pop.data_available === false) {
      return _panelHead('Exposure intelligence', 'Population & community exposure', 'Estimated population and community context around the observed thermal activity.', 'Awaiting data') +
        _emptyState(
          'Population exposure is not available yet',
          'The report is withholding numeric estimates instead of showing misleading zeros. Connect a population raster to calculate proximity exposure for this region.',
          pop.reason || 'No population source is configured for this region.'
        );
    } else if (Object.keys(pop).length) {
      let popHtml = '';
      if (pop.exposure_mode === 'proximity_buffers') {
        if (pop.within_1km != null) popHtml += '<div class="exposure-metric"><strong>' + pop.within_1km.toLocaleString() + '</strong><span>Within 1 km</span></div>';
        if (pop.within_3km != null) popHtml += '<div class="exposure-metric"><strong>' + pop.within_3km.toLocaleString() + '</strong><span>Within 3 km</span></div>';
        if (pop.within_5km != null) popHtml += '<div class="exposure-metric"><strong>' + pop.within_5km.toLocaleString() + '</strong><span>Within 5 km</span></div>';
      } else {
        if (pop.affected_population != null) popHtml += '<div class="exposure-metric"><strong>' + pop.affected_population.toLocaleString() + '</strong><span>Within perimeter</span></div>';
        if (pop.at_risk_3h  != null) popHtml += '<div class="exposure-metric"><strong>' + pop.at_risk_3h.toLocaleString() + '</strong><span>At risk +3h</span></div>';
        if (pop.at_risk_6h  != null) popHtml += '<div class="exposure-metric"><strong>' + pop.at_risk_6h.toLocaleString() + '</strong><span>At risk +6h</span></div>';
        if (pop.at_risk_12h != null) popHtml += '<div class="exposure-metric"><strong>' + pop.at_risk_12h.toLocaleString() + '</strong><span>At risk +12h</span></div>';
      }
      if (popHtml) html += _card('Population exposure', '<div class="exposure-metric-grid">' + popHtml + '</div>', 'span-full');
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
      html += _briefCard('Impact summary', impact.impact_summary, 'signal');
    }

    if (impact.worsening_factors && impact.worsening_factors.length) {
      html += _card('Worsening factors', _tagList(impact.worsening_factors, 'warning'));
    }

    return _panelHead('Exposure intelligence', 'Population & community exposure', 'Estimated population and community context around the current incident footprint.', 'Data available') +
      (html ? '<div class="report-card-grid">' + html + '</div>' : '<div class="report-na">Not available</div>');
  }

  function _renderEvacPanel(evac) {
    if (!evac) return '<div class="report-na">Not available</div>';
    if (evac.data_available === false) {
      return _panelHead('Mobility intelligence', 'Evacuation routes', 'Route availability and access windows for exposed communities.', 'Awaiting data') +
        _emptyState('Road-network analysis is unavailable', 'Routes are intentionally omitted until a valid road-network source is connected.', evac.reason || 'No road-network source is configured.');
    }
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
      html += _card('Road warnings', _tagList(evac.road_warnings, 'warning'), 'span-full');
    }

    return _panelHead('Mobility intelligence', 'Evacuation routes', 'Route availability, access windows, and current road constraints.', 'Routes assessed') +
      (html ? '<div class="report-card-grid">' + html + '</div>' : '<div class="report-na">Not available</div>');
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
      html += _briefCard('Fire observations', crowd.fire_observations, 'danger');
    }

    if (crowd.situational_info) {
      html += _briefCard('Situational information', crowd.situational_info, 'signal');
    }

    if (crowd.notable_patterns) {
      html += _briefCard('Notable patterns', crowd.notable_patterns, 'weather');
    }

    if (!html) {
      html = '<div class="report-na">No crowd reports available for this timestep.</div>';
    }

    return _panelHead('Community intelligence', 'Field reports', 'Public observations and requests that can supplement modelled conditions.', counts.total ? counts.total + ' reports' : 'No reports') +
      '<div class="report-card-grid">' + html + '</div>';
  }

  function _renderOverviewPanel(report) {
    let html = '';
    const metadata = report.metadata || {};
    const region = metadata.region || {};
    const thermal = report.report_mode === 'thermal_monitoring';
    let status = '';

    if (report.assessment_level) {
      status = '<span class="risk-badge moderate">' + _escHtml(report.assessment_level) + '</span>';
    } else if (report.risk_level && report.risk_level !== 'Unknown') {
      const lvlMap = { critical: 'critical', high: 'high', moderate: 'moderate', low: 'low' };
      const cls = lvlMap[report.risk_level.toLowerCase()] || 'high';
      status = '<span class="risk-badge ' + cls + '">' +
        '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" style="flex-shrink:0">' +
        '<path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/></svg>' +
        _escHtml(report.risk_level) + ' Risk' +
        '</span>';
    }

    const commandMeta = [];
    if (metadata.observation_time) commandMeta.push('Observed ' + metadata.observation_time);
    if (metadata.provider) commandMeta.push('AI narrative · ' + metadata.provider);
    html += '<section class="report-command-head"><div class="report-command-icon">' +
      '<svg viewBox="0 0 24 24"><path d="M4 13h3l2-6 4 11 2-5h5"/></svg></div>' +
      '<div class="report-command-copy"><span class="report-command-kicker">' +
      (thermal ? 'Thermal monitoring' : 'Wildfire operations') + '</span>' +
      '<h2>' + _escHtml(region.name || 'Current operational picture') + '</h2>' +
      (commandMeta.length ? '<div class="report-command-meta">' + commandMeta.map(function(item) {
        return '<span>' + _escHtml(item) + '</span>';
      }).join('') + '</div>' : '') + '</div>' + status + '</section>';

    if (report.key_points && report.key_points.length) {
      html += '<div class="report-key-points">' +
        '<div class="kp-title"><div><h4>Priority signals</h4><p>The most decision-relevant findings in this observation.</p></div><span>' + report.key_points.slice(0, 4).length + ' signals</span></div>' +
        '<div class="kp-grid">' +
        report.key_points.slice(0, 4).map(function(p, index) {
          return '<article class="kp-item"><span>0' + (index + 1) + '</span><p>' + _escHtml(_compactText(p, 145)) + '</p></article>';
        }).join('') +
        '</div></div>';
    }

    // Stat tiles — pulled from structured specialist data
    const tiles = [];

    const impact = report.impact || {};
    const pop = impact.population || {};
    if (!thermal) {
      if (pop.data_available === false) {
        tiles.push({ icon: 'unavailable', label: 'Population data', value: 'Not connected' });
      } else {
        if (pop.exposure_mode === 'proximity_buffers' && pop.within_5km != null) {
          tiles.push({ icon: 'users', label: 'Within 5 km', value: Number(pop.within_5km).toLocaleString() });
        } else {
          const atRisk12 = pop.at_risk_12h;
          if (atRisk12 != null) {
            tiles.push({ icon: 'users', label: 'At risk +12h', value: Number(atRisk12).toLocaleString() });
          }
          if (pop.affected_population != null) {
            tiles.push({ icon: 'fire', label: 'Within perimeter', value: Number(pop.affected_population).toLocaleString() });
          }
        }
      }
    }

    const evac = report.evacuation || {};
    if (evac.top_route && evac.top_route.window) {
      tiles.push({ icon: 'route', label: 'Top route window', value: evac.top_route.window });
    } else if (evac.top_route && evac.top_route.path && evac.top_route.path.length) {
      tiles.push({ icon: 'route', label: 'Top route', value: evac.top_route.path[0] + ' → ' + evac.top_route.path[evac.top_route.path.length - 1] });
    }

    const crowd = report.crowd;
    if (crowd && crowd.report_counts) {
      const total = crowd.report_counts.total || 0;
      const urgent = crowd.report_counts.need_help || 0;
      const label = urgent ? total + ' reports (' + urgent + ' urgent)' : total + ' reports';
      tiles.push({ icon: 'reports', label: 'Crowd reports', value: label });
    }

    if (tiles.length) {
      html += '<div class="ov-stat-row">' +
        tiles.map(function(t) {
          return '<div class="ov-stat-tile">' +
            '<span class="ov-stat-icon">' + _metricIcon(t.icon) + '</span>' +
            '<span class="ov-stat-val">' + _escHtml(t.value) + '</span>' +
            '<span class="ov-stat-label">' + _escHtml(t.label) + '</span>' +
            '</div>';
        }).join('') +
        '</div>';
    }

    const hasBriefing = report.situation || report.key_risks || report.immediate_actions;
    if (hasBriefing) {
      html += _sectionTitle('Decision briefing', 'A concise operational interpretation with recommended next steps.');
      const threeFields = report.key_risks || report.immediate_actions;
      html += '<div class="brief-grid">';
      if (report.situation) {
        html += _briefCard(threeFields ? 'Situation now' : 'Executive briefing', report.situation, 'signal');
      }
      if (report.key_risks) {
        html += _briefCard('Key risks', report.key_risks, 'danger');
      }
      if (report.immediate_actions) {
        html += _briefCard('Immediate actions', report.immediate_actions, 'action');
      }
      html += '</div>';
    }

    let provenance = '';
    if (region.name) provenance += _kv('Region', region.name);
    if (metadata.observation_time) provenance += _kv('Observation', metadata.observation_time);
    if (metadata.provider && metadata.model) provenance += _kv('Narrative model', metadata.provider + ' · ' + metadata.model);
    if (metadata.generated_at) provenance += _kv('Generated', metadata.generated_at);
    const sources = metadata.data_sources || {};
    const sourceNames = Object.keys(sources).filter(function(key) { return sources[key]; }).map(function(key) {
      const value = sources[key];
      const label = key.replaceAll('_', ' ');
      if (typeof value === 'object') return label + ': ' + (value.provider || 'configured source');
      return label + ': ' + value;
    });
    if (sourceNames.length) provenance += _kv('Evidence sources', sourceNames.join(' · '));
    if (provenance) {
      html += '<details class="report-provenance"><summary>Data sources & report details</summary><div>' + provenance + '</div></details>';
    }

    return html || '<div class="report-na">Not available</div>';
  }

  // ── Chat ─────────────────────────────────────────────────────────────────────

  function _clearChat() {
    const msgs = document.getElementById('chat-messages');
    if (msgs) msgs.innerHTML = '<div class="chat-welcome"><span class="chat-welcome-mark assistant-star" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 2.8c.6 5.8 3.4 8.6 9.2 9.2-5.8.6-8.6 3.4-9.2 9.2-.6-5.8-3.4-8.6-9.2-9.2 5.8-.6 8.6-3.4 9.2-9.2Z"/></svg></span>' +
      '<strong>How can I help?</strong><p>Ask about the current observation, evidence, exposure, or recommended actions.</p></div>';
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

  window.AIModal = { init, setContext, setAdmin, setCrowdAvailable, open, close, openChat, closeChat, renderCard };
})();
