/**
 * app.js — Two-view SPA (Home → Event)
 * Depends on: window.API, window.HomeMap, window.EventMap,
 *             window.Dashboard, window.AIModal
 */
(function() {
  let homeMap  = null;
  let eventMap = null;
  // Light mode is the predictable default for every fresh load.
  let darkMode = false;
  let allEvents = [];
  let currentEvent = null;
  let currentWeather = [];   // [{hour, temp_c, rh, wind_speed_kmh, wind_dir}]

  let predictionType      = 'ml';   // 'ml' | 'wind' | 'crowd'
  let _timestepsDone      = [];     // full list of done timesteps for current event
  let _currentTsIndex     = -1;     // index of currently selected timestep in _timestepsDone
  let _replayVirtualTime  = 0;      // current virtual clock ms (shared with DEV controls)
  let _replayIdx          = -1;     // which done[] entry the clock is currently on
  let _replaySpeed        = 1;      // clock multiplier: 1 or 60
  let _isAdmin            = false;  // set after authentication
  let _syncPushInterval   = null;   // admin: pushes virtual time to server every 10s
  let _initialReplayFloor = null;   // protects the richer event-1 default from a stale saved clock
  let _thermalViewMode    = 'classification';   // source classification is the monitoring default
  let _thermalRefreshPoll = null;
  let _thermalLastObservedMs = null;
  const THERMAL_STATUS_POLL_MS = 5 * 60 * 1000;

  // ── Boot ─────────────────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', async function() {
    initTheme();
    initAuth();

    homeMap  = new window.HomeMap('home-map', openEvent);
    eventMap = new window.EventMap('event-map');
    homeMap.setTheme(darkMode);
    eventMap.setTheme(darkMode);

    // Forecast slider wiring
    document.addEventListener('input', function(e) {
      if (e.target.id === 'fcast-slider') {
        _setForecastHour(+e.target.value);
      }
    });

    // Dashboard horizontal scroll via mouse wheel
    var dashEl = document.getElementById('dashboard-content');
    if (dashEl) {
      dashEl.addEventListener('wheel', function(e) {
        var maxScrollLeft = Math.max(0, dashEl.scrollWidth - dashEl.clientWidth);
        if (!maxScrollLeft) return;

        // Trackpads may report horizontal motion in deltaX; ordinary mouse
        // wheels report it in deltaY. Convert line/page units to pixels so
        // both inputs move the tray at a useful, consistent speed.
        var rawDelta = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
        if (!rawDelta) return;
        var unit = e.deltaMode === 1
          ? 32
          : e.deltaMode === 2
          ? Math.max(120, dashEl.clientWidth * 0.8)
          : 1;
        var nextScrollLeft = Math.max(
          0,
          Math.min(maxScrollLeft, dashEl.scrollLeft + rawDelta * unit),
        );
        if (nextScrollLeft === dashEl.scrollLeft) return;

        e.preventDefault();
        e.stopPropagation();
        dashEl.scrollLeft = nextScrollLeft;
      }, { passive: false, capture: true });
    }

    window.AIModal.init();
    window.Dashboard.clearDashboard();
    if (window.CrowdPanel) window.CrowdPanel.init();
    initMobileFAB();
    initLeftPanelCollapse();

    document.getElementById('nav-home-btn')?.addEventListener('click', function() { goHome(); });

    document.getElementById('dash-collapse-btn')?.addEventListener('click', function() {
      document.getElementById('bottom-panel')?.classList.toggle('collapsed');
    });

    initPredType();
    initThermalViewMode();
    initDevWindow();

    allEvents = await loadEvents();
    renderRegionSelector(allEvents);

    // Deep-link: /demo?event_id=<id>
    var _urlParams = new URLSearchParams(window.location.search);
    var _urlEventId = _urlParams.get('event_id');
    if (_urlEventId) {
      var _deepEvent = allEvents.find(function(e) { return String(e.id) === _urlEventId; });
      if (_deepEvent) { openEvent(_deepEvent, true); }
      else { openDefaultRegion(); }
    } else {
      openDefaultRegion();
    }

    window.addEventListener('popstate', function(e) {
      if (e.state && e.state.eventId) {
        var ev = allEvents.find(function(e2) { return e2.id === e.state.eventId; });
        if (ev) { openEvent(ev, true); return; }
      }
      openDefaultRegion();
    });
  });

  // ── Views ─────────────────────────────────────────────────────────────────────

  function showView(name, afterResize) {
    document.getElementById('home-view').classList.toggle('hidden', name !== 'home');
    document.getElementById('event-view').classList.toggle('hidden', name !== 'event');
    document.getElementById('breadcrumb').classList.toggle('hidden', name !== 'event');
    setTimeout(function() {
      if (name === 'home')  homeMap  && homeMap.map.invalidateSize();
      else                  eventMap && eventMap.map.invalidateSize();
      if (afterResize) afterResize();
    }, 60);
  }

  function goHome(fromPopstate) {
    if (currentEvent) openEvent(currentEvent, true);
    else openDefaultRegion();
  }

  // ── Events ────────────────────────────────────────────────────────────────────

  async function loadEvents() {
    try {
      const events = await window.API.getEvents();
      homeMap.renderEvents(events);
      renderHomeSidebar(events);
      return events;
    } catch(e) {
      showToast('Failed to load events: ' + e.message);
      return [];
    }
  }

  function renderHomeSidebar(events) {
    const el = document.getElementById('home-event-list');
    if (!el) return;
    if (!events.length) { el.innerHTML = '<div class="empty-msg">No events</div>'; return; }
    el.innerHTML = events.map(function(ev) {
      var focus = ev.monitoring_focus === 'forest' ? 'Forest' : 'Industrial';
      var location = ev.state ? ' · ' + escHtml(ev.state) : '';
      var readiness = ev.data_ready ? 'Ready' : 'Preparing data';
      return '<div class="hs-event-item" data-id="' + ev.id + '">' +
        '<div class="hs-event-name">' + escHtml(ev.name) + '</div>' +
        '<div class="hs-event-meta">' + focus + location + ' · ' + readiness + '</div>' +
        '</div>';
    }).join('');
    el.querySelectorAll('.hs-event-item').forEach(function(item) {
      item.addEventListener('click', function() {
        const ev = events.find(function(e) { return String(e.id) === item.dataset.id; });
        if (ev) openEvent(ev);
      });
    });
  }

  function openDefaultRegion() {
    if (!allEvents.length) {
      showView('home');
      return;
    }
    var savedEventId = null;
    try { savedEventId = window.localStorage.getItem('wildfire:selected-event-id'); } catch (e) {}
    var savedEvent = allEvents.find(function(ev) { return String(ev.id) === savedEventId; });
    var preferred = allEvents.find(function(ev) { return ev.region_id === 'vijayanagar'; });
    openEvent(savedEvent || preferred || allEvents[0], true);
  }

  function renderRegionSelector(events) {
    var selector = document.getElementById('region-selector');
    if (!selector) return;
    var optionsFor = function(focus) {
      return events.filter(function(ev) { return ev.monitoring_focus === focus; })
        .sort(function(a, b) { return Number(a.id) - Number(b.id); })
        .map(function(ev) {
        var label = ev.name.replace(
          / (Industrial Region|Industrial Corridor|Energy Corridor|Power and Industrial Region|Refinery Corridor|Forest Landscape) Monitoring$/,
          ''
        );
        return '<option value="' + ev.id + '">' + escHtml(label) + '</option>';
      }).join('');
    };
    selector.innerHTML =
      '<optgroup label="Industrial thermal">' + optionsFor('industrial') + '</optgroup>' +
      '<optgroup label="Forest fire">' + optionsFor('forest') + '</optgroup>';
    selector.onchange = function() {
      var selected = events.find(function(ev) { return String(ev.id) === selector.value; });
      if (selected) openEvent(selected);
    };
  }

  function _showEventLoading(label) {
    var ov = document.getElementById('event-loading-overlay');
    var lb = document.getElementById('event-loading-label');
    if (lb) lb.textContent = label || 'Loading event…';
    if (ov) ov.classList.remove('hidden');
  }

  function _hideEventLoading() {
    var ov = document.getElementById('event-loading-overlay');
    if (ov) ov.classList.add('hidden');
  }

  async function openEvent(ev, fromPopstate) {
    if (!ev) return;
    _stopThermalRefreshPolling();
    Object.keys(_pollIntervals || {}).forEach(function(id) {
      clearInterval(_pollIntervals[id]);
      delete _pollIntervals[id];
    });
    Object.keys(_pollCrowdIntervals || {}).forEach(function(id) {
      clearInterval(_pollCrowdIntervals[id]);
      delete _pollCrowdIntervals[id];
    });
    if (_syncPushInterval) { clearInterval(_syncPushInterval); _syncPushInterval = null; }
    if (window._syncRefetchInterval) { clearInterval(window._syncRefetchInterval); window._syncRefetchInterval = null; }
    currentEvent = ev;
    eventMap.setMonitoringFocus(ev.monitoring_focus);
    _applyAnalysisMode(ev);
    try { window.localStorage.setItem('wildfire:selected-event-id', String(ev.id)); } catch (e) {}
    var eventUrl = new URL(window.location.href);
    eventUrl.searchParams.set('event_id', String(ev.id));
    history.replaceState({ eventId: ev.id }, '', eventUrl.pathname + eventUrl.search + eventUrl.hash);

    var selector = document.getElementById('region-selector');
    var selectorMeta = document.getElementById('region-selector-meta');
    if (selector) {
      selector.value = String(ev.id);
      selector.disabled = true;
    }
    if (selectorMeta) {
      var focus = ev.monitoring_focus === 'forest' ? 'Forest fire monitoring' : 'Industrial thermal monitoring';
      selectorMeta.textContent = focus + (ev.state ? ' · ' + ev.state : '');
    }

    document.getElementById('breadcrumb').textContent = ev.name + ' · ' + ev.year;

    eventMap.clearLayers();
    if (window.CrowdPanel) window.CrowdPanel.setEvent(ev.id, eventMap);
    showView('event', function() {
      if (ev.bbox) eventMap.fitToBbox(ev.bbox);
      window.API.getAoi(ev.id).then(function(aoi) {
        eventMap.fitToAoi(aoi);
      }).catch(function() {});
    });
    window.Dashboard.clearDashboard();
    window.AIModal.setContext(ev.id, null);

    _showEventLoading('Loading ' + ev.name + '…');
    try {
      await loadTimesteps(ev.id);
    } finally {
      _hideEventLoading();
      if (selector) selector.disabled = false;
      _refreshDevWindowState();
    }
    if (ev.analysis_mode === 'thermal_monitoring') {
      _startThermalRefreshPolling(ev.id);
    }

    // Sync replay clock with server
    // Both admin and non-admin pull once on load to restore saved position.
    window.API.getReplayTime(ev.id).then(function(d) {
      if (d.ms && _timestepsDone.length && (!_initialReplayFloor || d.ms >= _initialReplayFloor)) {
        _initialReplayFloor = null;
        _replayVirtualTime = d.ms;
        _devApplyReplayTime();
      }
    }).catch(function(){});

    if (_isAdmin) {
      // Admin: push virtual time + speed to server every 10s
      if (_syncPushInterval) clearInterval(_syncPushInterval);
      _syncPushInterval = setInterval(function() {
        if (currentEvent) window.API.setReplayTime(currentEvent.id, _replayVirtualTime, _replaySpeed).catch(function(){});
      }, 10000);
    } else {
      // Non-admin: get reference point from server, then interpolate locally every second.
      // Re-sync reference every 15s to correct drift.
      if (_syncPushInterval) clearInterval(_syncPushInterval);
      var _syncRef = null; // {ms, pushed_at, speed}
      function _applySyncRef(d) {
        if (!d || !d.ms || !d.pushed_at) return;
        if (_initialReplayFloor && d.ms < _initialReplayFloor) return;
        _initialReplayFloor = null;
        _syncRef = d;
      }
      function _syncTick() {
        if (!_syncRef || !_timestepsDone.length) return;
        var elapsed  = Date.now() - _syncRef.pushed_at;
        var computed = _syncRef.ms + elapsed * (_syncRef.speed || 1);
        if (Math.abs(computed - _replayVirtualTime) < 500) return;
        _replayVirtualTime = computed;

        // Update label
        var label = document.getElementById('ts-label');
        if (label) label.textContent = fmtDateTime(new Date(_replayVirtualTime).toISOString());

        // Find which timestep we should be on
        var newIdx = 0;
        for (var i = 0; i < _timestepsDone.length; i++) {
          if (new Date(_timestepsDone[i].slot_time).getTime() <= _replayVirtualTime) newIdx = i;
          else break;
        }
        // Only selectTimestep when the index actually changes
        if (newIdx !== _replayIdx) {
          _replayIdx = newIdx;
          _currentTsIndex = newIdx;
          setGapBadge(_timestepsDone[newIdx]);
          _highlightTick(newIdx);
          selectTimestep(_timestepsDone[newIdx]);
        }
      }
      // Initial fetch
      if (currentEvent) {
        window.API.getReplayTime(currentEvent.id).then(_applySyncRef).catch(function(){});
      }
      // Tick every second for smooth updates
      _syncPushInterval = setInterval(function() {
        _syncTick();
      }, 1000);
      // Re-fetch reference every 15s
      var _syncRefetchInterval = setInterval(function() {
        if (!currentEvent) return;
        window.API.getReplayTime(currentEvent.id).then(_applySyncRef).catch(function(){});
      }, 15000);
      // Store refetch interval so it gets cleared on event change
      window._syncRefetchInterval = _syncRefetchInterval;
    }
  }

  function _applyAnalysisMode(ev) {
    var thermal = ev && ev.analysis_mode === 'thermal_monitoring';
    var predTitle = document.getElementById('pred-type-title');
    var predSection = document.getElementById('pred-type-section');
    if (predTitle) predTitle.style.display = thermal ? 'none' : '';
    if (predSection) predSection.style.display = thermal ? 'none' : '';
    document.getElementById('thermal-view-title')?.classList.toggle('hidden', !thermal);
    document.getElementById('thermal-view-section')?.classList.toggle('hidden', !thermal);
    if (thermal) {
      _thermalViewMode = 'classification';
      var defaultView = document.querySelector('input[name="thermal-view"][value="classification"]');
      if (defaultView) defaultView.checked = true;
      _renderThermalLegend('classification');
    }

    var dashboardTitle = document.querySelector('#bottom-header .bottom-title');
    if (dashboardTitle) {
      dashboardTitle.textContent = thermal ? 'Thermal Monitoring Dashboard' : 'Situation Dashboard';
    }

    var legend = document.getElementById('event-legend');
    if (!legend) return;
    if (!thermal) legend.innerHTML =
        '<div class="leg-row"><span class="leg-swatch" style="background:#cc2200;opacity:.7"></span>Fire perimeter</div>' +
        '<div class="leg-row" id="legend-risk-row"><span class="leg-swatch" style="background:#ff2222;opacity:.7"></span>High risk zone (ML)</div>' +
        '<div class="leg-row" id="legend-actual-row" style="display:none"><span class="leg-swatch" style="background:#888;opacity:.5;border:1px dashed #aaa"></span>Actual perimeter</div>' +
        '<div class="leg-row"><span class="leg-line" style="background:#cc0000"></span>Burned road</div>' +
        '<div class="leg-row"><span class="leg-line" style="background:#ff8c00"></span>At-risk road</div>' +
        '<div class="leg-row"><span class="leg-line" style="background:#44dd44"></span>Clear road</div>';
  }

  function _renderThermalLegend(mode) {
    var legend = document.getElementById('event-legend');
    if (!legend) return;
    var forest = currentEvent && currentEvent.monitoring_focus === 'forest';
    if (mode === 'classification') {
      legend.innerHTML =
        '<div class="leg-row"><span class="leg-swatch" style="background:#ef4444;opacity:.9;border:1px solid #991b1b"></span>FIRMS observation</div>' +
        '<div class="leg-row"><span class="leg-swatch" style="background:rgba(239,68,68,.24);border:2px solid #dc2626"></span>Generator unit</div>' +
        '<div class="leg-row"><span class="leg-swatch" style="background:#22d3ee;opacity:.9;border:1px solid #075985"></span>Power plant</div>' +
        '<div class="leg-row"><span class="leg-swatch" style="background:#dc2626;opacity:.8"></span>Industrial fire</div>' +
        '<div class="leg-row"><span class="leg-swatch" style="background:#a855f7;opacity:.8"></span>Gas flare</div>' +
        '<div class="leg-row"><span class="leg-swatch" style="background:#eab308;opacity:.8"></span>Agricultural burning</div>' +
        '<div class="leg-row"><span class="leg-swatch" style="background:#8b5e3c;opacity:.8"></span>Mining activity</div>' +
        '<div class="leg-row"><span class="leg-swatch" style="background:#16a34a;opacity:.8"></span>Wildfire</div>' +
        '<div class="leg-row"><span class="leg-swatch" style="background:#f97316;opacity:.8"></span>Industrial process heat</div>' +
        '<div class="leg-row"><span class="leg-swatch" style="background:#6b7280;opacity:.8"></span>Unknown / review</div>';
    } else if (mode === 'persistent') {
      var persistentColors = forest
        ? ['#6d28d9', '#a855f7', '#d8b4fe']
        : ['#ff5500', '#ff9900', '#ffd166'];
      legend.innerHTML =
        '<div class="leg-row"><span class="leg-swatch" style="background:' + persistentColors[0] + ';opacity:.8"></span>High persistence</div>' +
        '<div class="leg-row"><span class="leg-swatch" style="background:' + persistentColors[1] + ';opacity:.8"></span>Medium persistence</div>' +
        '<div class="leg-row"><span class="leg-swatch" style="background:' + persistentColors[2] + ';opacity:.8"></span>Low persistence</div>';
    } else {
      legend.innerHTML = forest
        ? '<div class="leg-row"><span class="leg-swatch" style="background:#a855f7;opacity:.85;border:1px solid #6d28d9"></span>Forest-area thermal hotspot (FIRMS)</div>'
        : '<div class="leg-row"><span class="leg-swatch" style="background:#ff6600;opacity:.8"></span>Industrial thermal detection</div>';
    }
  }

  function initThermalViewMode() {
    document.querySelectorAll('input[name="thermal-view"]').forEach(function(input) {
      input.addEventListener('change', function() {
        if (!input.checked) return;
        _thermalViewMode = input.value;
        _renderThermalLegend(_thermalViewMode);
        if (_currentTsIndex >= 0 && _timestepsDone[_currentTsIndex]) {
          selectTimestep(_timestepsDone[_currentTsIndex], false);
        }
      });
    });
  }

  function _renderThermalRefreshStatus(status) {
    var el = document.getElementById('thermal-refresh-status');
    if (!el) return;
    var state = status && status.status;
    var observed = status && status.last_observed_at;
    var refreshed = status && status.last_success_at;
    var nextRefresh = status && status.next_refresh_at;
    var intervalMs = Number(status && status.interval_hours || 4) * 3600000;
    var stale = refreshed && Date.now() - new Date(refreshed).getTime() > intervalMs * 2;
    var observationAge = observed && Date.now() - new Date(observed).getTime() > intervalMs * 2;

    if (state === 'running') {
      el.className = 'thermal-refresh-status updating';
      el.textContent = '↻ Updating FIRMS data…';
    } else if (state === 'failed' || stale) {
      el.className = 'thermal-refresh-status stale';
      var latest = observed ? 'latest local detection ' + fmtDateTime(observed) : 'no local detection available';
      var retry = nextRefresh ? ' · retry scheduled ' + fmtDateTime(nextRefresh) : ' · retrying automatically';
      el.textContent = '⚠ NASA FIRMS refresh delayed · ' + latest + retry;
    } else if (state === 'succeeded' && status && status.data_changed === false) {
      el.className = 'thermal-refresh-status ' + (observationAge ? 'stale' : 'live');
      var checked = refreshed ? 'checked ' + fmtDateTime(refreshed) + ' · ' : '';
      el.textContent = '● NRT · ' + checked + 'no newer observations · latest ' + (observed ? fmtDateTime(observed) : 'unavailable');
    } else if (state === 'succeeded') {
      el.className = 'thermal-refresh-status live';
      var refreshLabel = refreshed ? 'refreshed ' + fmtDateTime(refreshed) + ' · ' : '';
      el.textContent = '● NRT · ' + refreshLabel + (observed ? 'latest detection ' + fmtDateTime(observed) : 'no detections in the latest refresh');
    } else if (status && status.enabled === false) {
      el.className = 'thermal-refresh-status stale';
      el.textContent = 'Historical data · automatic refresh disabled';
    } else {
      el.className = 'thermal-refresh-status';
      el.textContent = 'NASA FIRMS near-real-time · awaiting scheduled refresh';
    }
  }

  function _stopThermalRefreshPolling() {
    if (_thermalRefreshPoll) clearInterval(_thermalRefreshPoll);
    _thermalRefreshPoll = null;
    _thermalLastObservedMs = null;
  }

  function _startThermalRefreshPolling(eventId) {
    _stopThermalRefreshPolling();
    var latest = _timestepsDone.length ? _timestepsDone[_timestepsDone.length - 1] : null;
    _thermalLastObservedMs = latest
      ? new Date(latest.nearest_t1 || latest.slot_time).getTime()
      : null;

    var poll = async function() {
      if (!currentEvent || currentEvent.id !== eventId) return;
      try {
        var status = await window.API.getThermalRefreshStatus(eventId);
        _renderThermalRefreshStatus(status);
        var observedMs = status.last_observed_at
          ? new Date(status.last_observed_at).getTime()
          : null;
        if (
          observedMs != null
          && _thermalLastObservedMs != null
          && observedMs !== _thermalLastObservedMs
        ) {
          _thermalLastObservedMs = observedMs;
          await loadTimesteps(eventId);
          _renderThermalRefreshStatus(status);
        } else if (observedMs != null) {
          _thermalLastObservedMs = observedMs;
        }
      } catch (error) {
        _renderThermalRefreshStatus({ status: 'failed' });
      }
    };

    poll();
    _thermalRefreshPoll = setInterval(poll, THERMAL_STATUS_POLL_MS);
  }

  function _mergeThermalActivity(fireCtx, geojson) {
    if (!fireCtx || !geojson || !Array.isArray(geojson.features)) return fireCtx;
    var features = geojson.features;
    var frpValues = features.map(function(feature) {
      var properties = feature.properties || {};
      return Number(properties.frp != null ? properties.frp : properties.mean_frp);
    }).filter(Number.isFinite);
    var brightnessValues = features.map(function(feature) {
      var properties = feature.properties || {};
      return Number(properties.bright_ti4 != null ? properties.bright_ti4 : properties.brightness);
    }).filter(Number.isFinite);
    var footprintAreas = features.map(function(feature) {
      return Number((feature.properties || {}).thermal_footprint_area_km2);
    }).filter(Number.isFinite);
    var totalFrp = frpValues.reduce(function(total, value) { return total + value; }, 0);
    var classified = geojson.metadata && geojson.metadata.view === 'classification';
    var persistent = geojson.metadata && geojson.metadata.view === 'persistent';
    var sourceView = persistent || classified;
    var countTruthy = function(property) {
      return features.filter(function(feature) { return Boolean((feature.properties || {})[property]); }).length;
    };
    var countValues = function(property) {
      return features.reduce(function(counts, feature) {
        var value = (feature.properties || {})[property];
        if (value != null && value !== '') counts[String(value)] = (counts[String(value)] || 0) + 1;
        return counts;
      }, {});
    };
    var uniqueValues = function(property) {
      return Array.from(new Set(features.map(function(feature) {
        return (feature.properties || {})[property];
      }).filter(function(value) { return value != null && value !== ''; }))).sort();
    };
    fireCtx.fire = Object.assign({}, fireCtx.fire || {}, {
      n_hotspots: features.length,
      frp_sum: Number(totalFrp.toFixed(3)),
    });
    fireCtx.thermal = Object.assign({}, fireCtx.thermal || {}, {
      detection_count: sourceView ? features.reduce(function(total, feature) {
        return total + Number((feature.properties || {}).detection_count || 0);
      }, 0) : features.length,
      raw_observation_count: features.reduce(function(total, feature) {
        return total + Number((feature.properties || {}).raw_observation_count || 1);
      }, 0),
      frp_mean_mw: frpValues.length ? Number((totalFrp / frpValues.length).toFixed(3)) : null,
      frp_max_mw: frpValues.length ? Math.max.apply(null, frpValues) : null,
      brightness_ti4_max_k: brightnessValues.length ? Math.max.apply(null, brightnessValues) : null,
      inside_industrial_area_count: countTruthy('inside_industrial_polygon'),
      near_industrial_facility_count: countTruthy('near_industrial_facility'),
      confidence_counts: countValues('confidence'),
      landcover_group_counts: countValues('landcover_group'),
      satellites: uniqueValues('satellite'),
      nearest_industries: uniqueValues('nearest_industry_name'),
      view_mode: (geojson.metadata && geojson.metadata.view) || _thermalViewMode,
      window_start: geojson.metadata && geojson.metadata.start,
      window_end: geojson.metadata && geojson.metadata.end,
      persistent_source_count: sourceView ? features.length : null,
      persistence_level_counts: sourceView ? countValues('persistence_level') : (fireCtx.thermal || {}).persistence_level_counts || {},
      highest_active_days: sourceView ? Math.max.apply(null, features.map(function(feature) {
        return Number((feature.properties || {}).unique_active_days || 0);
      }).concat([0])) : null,
      longest_duration_days: sourceView ? Math.max.apply(null, features.map(function(feature) {
        return Number((feature.properties || {}).active_duration_days || 0);
      }).concat([0])) : null,
      highest_night_ratio: sourceView ? Math.max.apply(null, features.map(function(feature) {
        return Number((feature.properties || {}).night_ratio || 0);
      }).concat([0])) : null,
      classification_counts: classified ? countValues('source_class') : (fireCtx.thermal || {}).classification_counts || {},
      emergency_candidate_count: classified
        ? countTruthy('is_emergency_candidate')
        : ((fireCtx.thermal || {}).emergency_candidate_count != null ? (fireCtx.thermal || {}).emergency_candidate_count : null),
      classification_mean_confidence: classified && features.length
        ? features.reduce(function(total, feature) {
            return total + Number((feature.properties || {}).classification_confidence || 0);
          }, 0) / features.length
        : ((fireCtx.thermal || {}).classification_mean_confidence != null ? (fireCtx.thermal || {}).classification_mean_confidence : null),
      classification_method: classified && geojson.metadata
        ? geojson.metadata.method
        : null,
      largest_thermal_footprint_km2: footprintAreas.length
        ? Math.max.apply(null, footprintAreas)
        : null,
    });
    return fireCtx;
  }

  // ── Timesteps ─────────────────────────────────────────────────────────────────

  async function loadTimesteps(eventId) {
    const container = document.getElementById('timestep-slider-section');
    container.innerHTML = '<div class="empty-msg">Loading…</div>';

    let timesteps;
    try {
      timesteps = await window.API.getTimesteps(eventId);
    } catch(e) {
      container.innerHTML = '<div class="empty-msg">Failed to load timesteps</div>';
      return;
    }

    // Use all slots (pending ones trigger on-demand build when selected)
    const done = timesteps;
    _timestepsDone  = done;
    if (!done.length) {
      container.innerHTML = '<div class="empty-msg">No timesteps yet</div>';
      return;
    }

    // Event 1 is more representative late in its progression. Select the
    // latest real May 10–11 row whose source overpass is no more than 1h old.
    var initialIdx = 0;
    if (currentEvent && currentEvent.analysis_mode === 'thermal_monitoring') {
      // Monitoring events should open on the newest satellite observation.
      // Starting at index zero makes the cumulative views look almost empty
      // even when the complete monitoring window is already available.
      initialIdx = done.length - 1;
    } else if (+eventId === 1) {
      done.forEach(function(ts, idx) {
        var day = (ts.slot_time || '').slice(0, 10);
        if (day >= '2016-05-10' && day <= '2016-05-11' && ts.gap_hours <= 1) initialIdx = idx;
      });
    }
    _currentTsIndex = initialIdx;

    const tickBar = done.map(function(ts) {
      let cls = ts.prediction_status !== 'done' ? 'pending'
              : ts.spatial_analysis_status === 'done' ? 'full' : 'partial';
      if (ts.data_gap_warn) cls += ' gap';
      return '<div class="ts-tick ' + cls + '" title="' + fmtDateTime(ts.slot_time) + '"></div>';
    }).join('');

    container.innerHTML =
      '<div class="ts-slider-wrap">' +
        '<div class="ts-status-bar" id="ts-tick-bar">' + tickBar + '</div>' +
        '<div class="ts-label-row">' +
          '<span id="ts-label">' + fmtDateTime(done[initialIdx].slot_time) + '</span>' +
          '<span id="ts-live-badge" class="ts-live-badge">● LIVE</span>' +
        '</div>' +
        '<div class="ts-meta-row">' +
          '<div class="ts-meta-item">' +
            '<span class="ts-meta-lbl">T1 hotspot</span>' +
            '<span class="ts-meta-val" id="ts-t1-label">—</span>' +
          '</div>' +
          '<div class="ts-meta-item">' +
            '<span class="ts-meta-lbl">Observation</span>' +
            '<span class="ts-meta-val" id="ts-sat-label">—</span>' +
          '</div>' +
          '<div class="ts-meta-item">' +
            '<span class="ts-meta-lbl">Data gap</span>' +
            '<span id="ts-gap-badge" class="ts-gap"></span>' +
          '</div>' +
        '</div>' +
        (currentEvent && currentEvent.analysis_mode === 'thermal_monitoring'
          ? '<div id="thermal-refresh-status" class="thermal-refresh-status">NASA FIRMS near-real-time · checking status…</div>'
          : '') +
      '</div>';

    setGapBadge(done[initialIdx]);
    _highlightTick(initialIdx);

    // ── Virtual real-time clock ───────────────────────────────────────────────
    // Uses module-level _replayVirtualTime, _replayIdx, _replaySpeed so DEV
    // controls can jump/speed the clock without touching each other's state.
    _replayVirtualTime = new Date(done[initialIdx].slot_time).getTime();
    _replayIdx = initialIdx;
    _initialReplayFloor = (+eventId === 1 ||
      (currentEvent && currentEvent.analysis_mode === 'thermal_monitoring'))
      ? _replayVirtualTime
      : null;
    var _replayInterval = null;

    function stopPlay() {
      clearInterval(_replayInterval);
      _replayInterval = null;
      var badge = document.getElementById('ts-live-badge');
      if (badge) badge.style.display = 'none';
    }

    function startPlay() {
      var badge = document.getElementById('ts-live-badge');
      if (badge) badge.style.display = '';
      _replayInterval = setInterval(function() {
        _replayVirtualTime += 1000 * _replaySpeed;
        var label = document.getElementById('ts-label');
        if (label) label.textContent = fmtDateTime(new Date(_replayVirtualTime).toISOString());

        var nextIdx = _replayIdx + 1;
        if (nextIdx < done.length) {
          var nextBoundary = new Date(done[nextIdx].slot_time).getTime();
          if (_replayVirtualTime >= nextBoundary) {
            _replayIdx = nextIdx;
            _currentTsIndex = nextIdx;
            setGapBadge(done[nextIdx]);
            selectTimestep(done[nextIdx]);
            _highlightTick(nextIdx);
          }
        } else if (_isAdmin) {
          // Admin: loop back to first timestep
          _replayVirtualTime = new Date(done[0].slot_time).getTime();
          _replayIdx = 0;
          _currentTsIndex = 0;
          setGapBadge(done[0]);
          selectTimestep(done[0]);
          _highlightTick(0);
        } else {
          stopPlay();
        }
      }, 1000);
    }

    selectTimestep(done[initialIdx]);
    if (currentEvent && currentEvent.analysis_mode === 'thermal_monitoring') {
      // Keep the monitoring dashboard on the latest data. Replay remains
      // available through the timestep controls when the user requests it.
      stopPlay();
    } else {
      startPlay();
    }
  }

  function _highlightTick(idx) {
    var ticks = document.querySelectorAll('#ts-tick-bar .ts-tick');
    ticks.forEach(function(t, i) { t.classList.toggle('active', i === idx); });
  }

  function setGapBadge(ts) {
    const el = document.getElementById('ts-gap-badge');
    if (el) {
      const hrs = ts.gap_hours != null ? ts.gap_hours.toFixed(1) : '?';
      if (ts.data_gap_warn) {
        el.textContent = hrs + 'h ago ⚠';
        el.className   = 'ts-gap warn';
      } else {
        el.textContent = hrs + 'h ago';
        el.className   = 'ts-gap ok';
      }
    }
    const t1El = document.getElementById('ts-t1-label');
    if (t1El) t1El.textContent = ts.nearest_t1 ? fmtDateTime(ts.nearest_t1) : '—';
  }

  // ── Forecast Slider ───────────────────────────────────────────────────────────

  function _windArrow(deg) {
    if (deg == null) return '';
    return '<svg width="18" height="18" viewBox="-9 -9 18 18" style="vertical-align:middle;flex-shrink:0">' +
      '<g transform="rotate(' + (deg + 180) + ')">' +
      '<polygon points="0,-6 2.5,3 0,2 -2.5,3" fill="#ff8c00"/>' +
      '</g></svg>';
  }

  function _windDirLabel(deg) {
    if (deg == null) return '';
    return ['N','NE','E','SE','S','SW','W','NW'][Math.round(deg / 45) % 8];
  }

  function _renderFcastWeather(wx) {
    var el = document.getElementById('fcast-weather');
    if (!el) return;
    if (!wx) { el.innerHTML = '<span style="opacity:.4;font-size:10px">No data</span>'; return; }
    el.innerHTML =
      '<div class="fcast-wx-wind">' +
        _windArrow(wx.wind_dir) +
        '<span class="fcast-wx-speed">' + (wx.wind_speed_kmh != null ? wx.wind_speed_kmh.toFixed(0) : '—') + '</span>' +
        '<span class="fcast-wx-unit"> km/h</span>' +
        '<span class="fcast-wx-dir"> ' + _windDirLabel(wx.wind_dir) + '</span>' +
      '</div>' +
      '<div class="fcast-wx-row">' +
        '<span class="fcast-wx-label">Temp</span><span class="fcast-wx-val">' + (wx.temp_c != null ? wx.temp_c.toFixed(1) + ' °C' : '—') + '</span>' +
        '<span class="fcast-wx-label" style="margin-left:8px">RH</span><span class="fcast-wx-val">' + (wx.rh != null ? wx.rh.toFixed(0) + '%' : '—') + '</span>' +
      '</div>';
  }

  function _setForecastHour(h) {
    var badge = document.getElementById('fcast-badge');
    var hlabel = document.getElementById('fcast-h-label');
    if (hlabel) hlabel.textContent = '+' + h + 'h';

    if (badge) {
      if (h <= 2) {
        badge.textContent = 'NOW';
        badge.className = 'fcast-badge now';
      } else if (h <= 5) {
        badge.textContent = '+3h';
        badge.className = 'fcast-badge h3';
      } else if (h <= 11) {
        badge.textContent = '+6h';
        badge.className = 'fcast-badge h6';
      } else {
        badge.textContent = '+12h';
        badge.className = 'fcast-badge h12';
      }
    }

    // Risk zone visibility — gated by prediction type
    if (eventMap) {
      const useML   = predictionType === 'ml' || predictionType === 'crowd';
      const useWind = predictionType === 'wind';
      eventMap.setRiskVisible('3h',      useML   && h >= 3  && h <= 5);
      eventMap.setRiskVisible('6h',      useML   && h >= 6  && h <= 11);
      eventMap.setRiskVisible('12h',     useML   && h >= 12);
      eventMap.setWindRiskVisible('3h',  useWind && h >= 3  && h <= 5);
      eventMap.setWindRiskVisible('6h',  useWind && h >= 6  && h <= 11);
      eventMap.setWindRiskVisible('12h', useWind && h >= 12);
      eventMap.setWeatherGridHour(h);
      var actualOn = document.getElementById('dev-actual-toggle')?.checked;
      if (actualOn) {
        eventMap.setActualPerimVisible('+0h',  h <= 2);
        eventMap.setActualPerimVisible('+3h',  h >= 3  && h <= 5);
        eventMap.setActualPerimVisible('+6h',  h >= 6  && h <= 11);
        eventMap.setActualPerimVisible('+12h', h >= 12);
      }
    }

    // Weather mini panel
    if (currentWeather && currentWeather.length) {
      var wx = currentWeather.find(function(r) { return r.hour === h; });
      if (!wx) wx = currentWeather.reduce(function(a, b) {
        return Math.abs(b.hour - h) < Math.abs(a.hour - h) ? b : a;
      });
      _renderFcastWeather(wx);
    }
  }

  function _initForecastSlider(weatherData) {
    // Forecast horizons belong to the wildfire spread workflow. Thermal
    // monitoring replays observed detections and does not currently generate
    // future source classifications, so showing this control would imply a
    // forecasting capability that is not present.
    if (currentEvent && currentEvent.analysis_mode === 'thermal_monitoring') {
      _hideForecastSlider();
      return;
    }

    currentWeather = weatherData || [];
    var section = document.getElementById('fcast-section');
    var titleEl = document.getElementById('fcast-section-title');
    if (section) section.style.display = '';
    if (titleEl) titleEl.style.display = '';
    var slider = document.getElementById('fcast-slider');
    if (slider) {
      slider.value = 3;
      slider.disabled = !currentWeather.length;
    }
    if (!currentWeather.length) {
      _renderFcastWeather(null);
      return;
    }
    _setForecastHour(3);
  }

  function _hideForecastSlider() {
    var section = document.getElementById('fcast-section');
    var titleEl = document.getElementById('fcast-section-title');
    if (section) section.style.display = 'none';
    if (titleEl) titleEl.style.display = 'none';
    currentWeather = [];
  }

  // Poll a timestep until prediction_status === 'done', then reload layers.
  function _updateSimBtnState() {
    var simBtn = document.getElementById('dev-sim-btn');
    if (!simBtn) return;
    var ts = (_currentTsIndex >= 0 && _timestepsDone.length) ? _timestepsDone[_currentTsIndex] : null;
    var isDone = ts && ts.prediction_status === 'done';
    simBtn.disabled = !isDone;
    simBtn.title = isDone ? '' : 'Prediction must complete before simulating reports';
  }

  var _pollIntervals = {};
  var _pollCrowdIntervals = {};
  var _crowdMode = false;

  function _pollCrowdUntilDone(ts) {
    var key = 'crowd_' + ts.id;
    if (_pollCrowdIntervals[key]) return;
    var startedAt = Date.now();
    _pollCrowdIntervals[key] = setInterval(async function() {
      try {
        var s = await window.API.getTsStatus(currentEvent.id, ts.id);
        var failed = s.crowd_prediction_status === 'failed' || s.spatial_crowd_status === 'failed';
        if (failed || Date.now() - startedAt > 10 * 60 * 1000) {
          clearInterval(_pollCrowdIntervals[key]);
          delete _pollCrowdIntervals[key];
          _hidePredStatus();
          showToast(failed ? 'Crowd processing failed.' : 'Crowd processing timed out.', 'error');
          return;
        }
        var crowdDone = s.crowd_prediction_status === 'done' && s.spatial_crowd_status === 'done';
        if (crowdDone) {
          clearInterval(_pollCrowdIntervals[key]);
          delete _pollCrowdIntervals[key];
          _hidePredStatus();
          // Enable the ML + Crowd radio and enhance button
          _setCrowdRadio(true);
          window.AIModal?.setCrowdAvailable(true);
          var crowdRadio = document.getElementById('pred-type-crowd');
          if (crowdRadio) {
            crowdRadio.checked = true;
            crowdRadio.dispatchEvent(new Event('change', { bubbles: true }));
          }
        }
      } catch(e) {}
    }, 2000);
  }

  function _pollUntilDone(ts) {
    if (_pollIntervals[ts.id]) return;   // already polling
    _pollIntervals[ts.id] = setInterval(async function() {
      try {
        var s = await window.API.getTsStatus(currentEvent.id, ts.id);
        var idx = _timestepsDone.findIndex(function(t) { return t.id === ts.id; });

        var failed = s.prediction_status === 'failed' || s.spatial_analysis_status === 'failed';
        if (failed) {
          clearInterval(_pollIntervals[ts.id]);
          delete _pollIntervals[ts.id];
          if (idx >= 0) {
            _timestepsDone[idx].prediction_status       = s.prediction_status;
            _timestepsDone[idx].spatial_analysis_status = s.spatial_analysis_status;
          }
          _hidePredStatus();
          showToast(s.spatial_analysis_status === 'failed' ?
            'Spatial analysis failed. Retry the timestep to try again.' :
            'Prediction failed. Retry the timestep to try again.', 'error');
          return;
        }

        // Keep status bar visible while any stage is still running
        if (_currentTsIndex === idx && (s.prediction_status !== 'done' || s.spatial_analysis_status !== 'done')) {
          _showPredStatus();
        }

        var allDone = s.prediction_status === 'done' && s.spatial_analysis_status === 'done';
        if (allDone) {
          clearInterval(_pollIntervals[ts.id]);
          delete _pollIntervals[ts.id];
          if (idx >= 0) {
            _timestepsDone[idx].prediction_status       = s.prediction_status;
            _timestepsDone[idx].spatial_analysis_status = s.spatial_analysis_status;
            var tick = document.querySelectorAll('#ts-tick-bar .ts-tick')[idx];
            if (tick) tick.className = 'ts-tick full';
            _updateSimBtnState();
            if (_currentTsIndex === idx) {
              _hidePredStatus();
              _crowdMode = false;  // standard prediction completed — revert to normal layers
              selectTimestep(_timestepsDone[idx], false);
            }
          }
        }
      } catch(e) {}
    }, 2000);
  }

  async function selectTimestep(ts, notifyBackend) {
    if (!currentEvent) return;
    const eid  = currentEvent.id;
    const tsid = ts.id;

    // On mobile, close the left-panel sheet after picking a timestep
    if (window._mobileClosePanel) window._mobileClosePanel();

    _updateSimBtnState();
    eventMap.clearLayers();
    _hideForecastSlider();

    // Cancel all stale polls (only the current timestep matters)
    Object.keys(_pollIntervals).forEach(function(id) {
      if (+id !== tsid) {
        clearInterval(_pollIntervals[id]);
        delete _pollIntervals[id];
      }
    });
    Object.keys(_pollCrowdIntervals).forEach(function(key) {
      if (key !== 'crowd_' + tsid) {
        clearInterval(_pollCrowdIntervals[key]);
        delete _pollCrowdIntervals[key];
      }
    });

    // Selecting a timestep always notifies the backend. Completed outputs stay
    // cached; unfinished stages are atomically claimed and built on demand.
    var timestepDone = ts.prediction_status === 'done' && ts.spatial_analysis_status === 'done';
    if (notifyBackend !== false) {
      window.API.runPredictionStep(eid, tsid).catch(function(err) {
        if (timestepDone) return;
        clearInterval(_pollIntervals[tsid]);
        delete _pollIntervals[tsid];
        _hidePredStatus();
        showToast('Build failed to start: ' + (err.message || 'unknown error'), 'error');
      });
    }

    // Show status bar and poll only while this timestep is unfinished.
    if (!timestepDone) {
      var retrySpatial = ts.prediction_status === 'done' && ts.spatial_analysis_status === 'failed';
      var buildLabel = currentEvent.analysis_mode === 'thermal_monitoring'
        ? 'Building thermal monitoring view…'
        : 'Building prediction…';
      _showPredStatus(retrySpatial ? 'Retrying spatial analysis…' : buildLabel);
      _pollUntilDone(ts);
    } else {
      _hidePredStatus();
    }

    // Enable/disable crowd radio based on whether crowd prediction exists for this timestep
    window.API.getTsStatus(eid, tsid).then(function(s) {
      var crowdReady = s.crowd_prediction_status === 'done' && s.spatial_crowd_status === 'done';
      _setCrowdRadio(crowdReady);
      window.AIModal?.setCrowdAvailable(crowdReady);
      // If we're in crowd mode but crowd isn't ready for this timestep, fall back to ML
      if (predictionType === 'crowd' && !crowdReady) {
        predictionType = 'ml';
        _crowdMode = false;
        var mlRadio = document.querySelector('input[name="pred-type"][value="ml"]');
        if (mlRadio) mlRadio.checked = true;
        _updateRiskLegend();
      }
    }).catch(function() {
      _setCrowdRadio(false);
      window.AIModal?.setCrowdAvailable(false);
    });

    // Keep the selected observation date visible in the replay metadata.
    const satEl = document.getElementById('ts-sat-label');
    if (ts.slot_time) {
      const satDate = new Date(ts.nearest_t1 || ts.slot_time).toLocaleDateString('en-CA');
      if (satEl) satEl.textContent = satDate;
    }

    // Update AI context (does NOT open modal or load report yet)
    window.AIModal.setContext(eid, tsid);
    _refreshDevWindowState();

    // Update crowd panel to show only reports up to this timestep's slot time
    if (window.CrowdPanel && ts.slot_time) {
      window.CrowdPanel.refresh(ts.slot_time);
    }

    var thermalActivityRequest = null;
    if (currentEvent.analysis_mode === 'thermal_monitoring' && _thermalViewMode !== 'replay') {
      thermalActivityRequest = _thermalViewMode === 'persistent'
        ? window.API.getPersistentThermalSources(eid, 30, ts.slot_time)
        : _thermalViewMode === 'classification'
        ? window.API.getThermalClassifications(eid, 30, ts.slot_time)
        : window.API.getThermalDetections(
            eid,
            _thermalViewMode === '30d' ? 30 : 5,
            ts.slot_time,
          );
    }
    var hotspotRequest = thermalActivityRequest || window.API.getHotspots(eid, tsid, _crowdMode);
    var classificationObservationsRequest =
      currentEvent.analysis_mode === 'thermal_monitoring' && _thermalViewMode === 'classification'
        ? window.API.getThermalDetections(eid, 30, ts.slot_time)
        : Promise.resolve({ type: 'FeatureCollection', features: [] });
    var industrialFacilitiesRequest = currentEvent.analysis_mode === 'thermal_monitoring'
      ? window.API.getIndustrialFacilities(eid)
      : Promise.resolve({ type: 'FeatureCollection', features: [] });

    // Map layers (non-blocking)
    Promise.allSettled([
      window.API.getPerimeter(eid, tsid, _crowdMode),
      hotspotRequest,
      window.API.getRiskZones(eid, tsid, _crowdMode),
      window.API.getRoads(eid, tsid),
      window.API.getWindRiskZones(eid, tsid),
      classificationObservationsRequest,
      industrialFacilitiesRequest,
    ]).then(function(r) {
      if (r[0].status === 'fulfilled') eventMap.renderPerimeter(r[0].value);
      if (r[1].status === 'fulfilled') {
        if (_thermalViewMode === 'persistent') eventMap.renderPersistentSources(r[1].value);
        else if (_thermalViewMode === 'classification') eventMap.renderClassifiedSources(
          r[1].value,
          r[5].status === 'fulfilled' ? r[5].value : null,
        );
        else eventMap.renderHotspots(r[1].value);
      }
      if (r[2].status === 'fulfilled') eventMap.renderRiskZones(r[2].value);
      if (r[3].status === 'fulfilled') eventMap.renderRoads(r[3].value);
      if (r[4].status === 'fulfilled') eventMap.renderRiskZonesWind(r[4].value);
      if (r[6].status === 'fulfilled') eventMap.renderIndustrialFacilities(r[6].value);
    });

    // If actual perimeter overlay is active, reload it for the new timestep
    var actualToggle = document.getElementById('dev-actual-toggle');
    if (actualToggle && actualToggle.checked) {
      _loadActualPerimeter(ts);
    }

    // Dashboard + Weather: all together so weather renders into already-created DOM
    Promise.allSettled([
      window.API.getAnalysis(eid, tsid),
      window.API.getFireContext(eid, tsid),
      window.API.getWeather(eid, tsid),
      window.API.getWindField(eid, tsid),
      hotspotRequest,
      industrialFacilitiesRequest,
    ]).then(function(r) {
      var forecast  = r[2].status === 'fulfilled' ? r[2].value : [];
      var windHours = r[3].status === 'fulfilled' ? r[3].value : [];
      // renderDashboard first (creates the DOM elements), then update weather into them
      var fireContext = r[1].status === 'fulfilled' ? r[1].value : null;
      if (thermalActivityRequest && r[4].status === 'fulfilled') {
        fireContext = _mergeThermalActivity(fireContext, r[4].value);
      }
      window.Dashboard.renderDashboard(
        r[0].status === 'fulfilled' ? r[0].value : null,
        fireContext,
        forecast,
        r[5].status === 'fulfilled' ? r[5].value : null,
      );
      window.Dashboard.updateHud(fireContext, currentEvent, _thermalViewMode);
      window.Dashboard.updateWeather(forecast);
      eventMap && eventMap.loadWindField(windHours);
      _initForecastSlider(forecast);
      window.AIModal.renderCard();
    });
  }

  // ── Left panel collapse ───────────────────────────────────────────────────────

  function initLeftPanelCollapse() {
    var btn   = document.getElementById('lp-collapse-btn');
    var panel = document.getElementById('left-panel');
    if (!btn || !panel) return;
    btn.addEventListener('click', function() {
      panel.classList.toggle('lp-collapsed');
      // Trigger map resize so Leaflet redraws to the new width
      setTimeout(function() { eventMap && eventMap.map.invalidateSize(); }, 240);
    });
  }

  // ── Mobile FAB ───────────────────────────────────────────────────────────────

  function initMobileFAB() {
    var fab   = document.getElementById('mobile-fab');
    var panel = document.getElementById('left-panel');
    if (!fab || !panel) return;

    function _openPanel() {
      panel.classList.add('mobile-open');
      fab.classList.add('panel-open');
    }
    function _closePanel() {
      panel.classList.remove('mobile-open');
      fab.classList.remove('panel-open');
    }
    function _togglePanel() {
      if (panel.classList.contains('mobile-open')) _closePanel();
      else _openPanel();
    }

    fab.addEventListener('click', _togglePanel);

    // Close panel when tapping the map area
    var mapWrap = document.getElementById('event-map-wrap');
    if (mapWrap) mapWrap.addEventListener('click', function() {
      if (panel.classList.contains('mobile-open')) _closePanel();
    });

    // Expose so selectTimestep can close the panel after picking a slot
    window._mobileClosePanel = _closePanel;
  }

  // ── Theme ─────────────────────────────────────────────────────────────────────

  function initTheme() {
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    document.body.classList.toggle('light', !darkMode);
    document.body.classList.toggle('dark', darkMode);
    btn.innerHTML = darkMode
      ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="5.64"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/></svg>'
      : '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
    btn.addEventListener('click', function() {
      darkMode = !darkMode;
      document.body.classList.toggle('light', !darkMode);
      document.body.classList.toggle('dark',   darkMode);
      btn.innerHTML = darkMode
        ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>'
        : '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
      homeMap  && homeMap.setTheme(darkMode);
      eventMap && eventMap.setTheme(darkMode);
    });
  }

  // ── Auth (JWT access + rotating refresh tokens) ───────────────────────────────

  function initAuth() {
    var demoMode = window.location.pathname === '/demo' || window.location.pathname.startsWith('/demo/') ||
      window.location.pathname === '/dashboard' || window.location.pathname.startsWith('/dashboard/');
    if (demoMode) {
      hideAuthModal();
      updateAuthUI('Demo', false);
      document.getElementById('auth-logout-btn')?.addEventListener('click', function() {
        try {
          window.localStorage.removeItem('wf_access_token');
          window.localStorage.removeItem('wf_refresh_token');
        } catch (e) {}
        window.location.replace('/');
      });
      return;
    }

    var authMode = 'login';
    var form = document.getElementById('auth-form');
    var toggle = document.getElementById('auth-mode-toggle');
    var error = document.getElementById('auth-error');

    function setMode(mode) {
      authMode = mode;
      var registering = mode === 'register';
      document.getElementById('auth-modal-title').textContent = registering ? 'Create an account' : 'Sign in to continue';
      document.getElementById('auth-submit-btn').textContent = registering ? 'Create account' : 'Sign in';
      document.getElementById('auth-email-row').classList.toggle('hidden', !registering);
      document.getElementById('auth-password').setAttribute('autocomplete', registering ? 'new-password' : 'current-password');
      toggle.textContent = registering ? 'Already have an account? Sign in' : 'Need an account? Register';
      if (error) error.textContent = '';
    }

    toggle?.addEventListener('click', function() {
      setMode(authMode === 'login' ? 'register' : 'login');
    });

    form?.addEventListener('submit', function(event) {
      event.preventDefault();
      var username = document.getElementById('auth-username').value;
      var password = document.getElementById('auth-password').value;
      var email = document.getElementById('auth-email').value;
      var submit = document.getElementById('auth-submit-btn');
      if (error) error.textContent = '';
      submit.disabled = true;

      var action = authMode === 'register'
        ? window.API.register(username, password, email)
        : window.API.login(username, password);
      action.then(function(data) {
        updateAuthUI(data.user.username, data.user.is_admin);
        form.reset();
        hideAuthModal();
      }).catch(function(err) {
        if (error) error.textContent = err.message || 'Authentication failed.';
      }).finally(function() { submit.disabled = false; });
    });

    document.getElementById('auth-logout-btn')?.addEventListener('click', function() {
      window.API.logout().finally(function() {
        updateAuthUI(null);
        showAuthModal();
      });
    });

    if (window.API.hasSession()) {
      window.API.me()
        .then(function(d) { updateAuthUI(d.user.username, d.user.is_admin); hideAuthModal(); })
        .catch(function() { updateAuthUI(null); showAuthModal(); });
    } else {
      showAuthModal();
    }
  }

  // The auth modal is non-dismissible because authenticated APIs gate the site.
  function showAuthModal() { document.getElementById('auth-modal-overlay').classList.add('visible'); }
  function hideAuthModal() { document.getElementById('auth-modal-overlay').classList.remove('visible'); }

  function updateAuthUI(username, isAdmin) {
    const logoutBtn = document.getElementById('auth-logout-btn');
    const userLabel = document.getElementById('auth-user-label');
    const devBtn    = document.getElementById('dev-toggle-btn');
    if (username) {
      _isAdmin = !!isAdmin;
      logoutBtn?.classList.remove('hidden');
      if (userLabel) userLabel.textContent = username + (isAdmin ? ' (admin)' : '');
      if (devBtn) devBtn.style.display = _isAdmin ? '' : 'none';
    } else {
      _isAdmin = false;
      logoutBtn?.classList.add('hidden');
      if (userLabel) userLabel.textContent = '';
      if (devBtn) devBtn.style.display = 'none';
    }
    window.AIModal?.setAdmin(_isAdmin);
  }

  // ── Helpers ────────────────────────────────────────────────────────────────────

  function escHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

  function fmtDateTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return d.toLocaleDateString('en-CA') + ' ' +
           d.toLocaleTimeString('en-CA', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  }

  function showToast(msg, type) {
    const t = document.createElement('div');
    t.className = 'toast ' + (type || 'error');
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(function() { t.remove(); }, 4000);
  }

  // ── Prediction Type ───────────────────────────────────────────────────────────

  function initPredType() {
    document.addEventListener('change', function(e) {
      if (e.target.name !== 'pred-type') return;
      predictionType = e.target.value;
      var isCrowd = predictionType === 'crowd';
      _crowdMode = isCrowd;

      var h = +(document.getElementById('fcast-slider')?.value || 0);
      _setForecastHour(h);
      _updateRiskLegend();

      // Reload spatial layers for the active timestep
      if (!currentEvent) return;
      var ts = _timestepsDone[_currentTsIndex];
      if (!ts) return;
      var eid  = currentEvent.id;
      var tsid = ts.id;
      Promise.allSettled([
        window.API.getPerimeter(eid, tsid, isCrowd),
        window.API.getHotspots(eid, tsid, isCrowd),
        window.API.getRiskZones(eid, tsid, isCrowd),
        window.API.getRoads(eid, tsid, isCrowd),
      ]).then(function(r) {
        if (r[0].status === 'fulfilled') eventMap.renderPerimeter(r[0].value);
        if (r[1].status === 'fulfilled') eventMap.renderHotspots(r[1].value);
        if (r[2].status === 'fulfilled') eventMap.renderRiskZones(r[2].value);
        if (r[3].status === 'fulfilled') eventMap.renderRoads(r[3].value);
      });
    });
  }

  function _setCrowdRadio(enabled) {
    var radio = document.getElementById('pred-type-crowd');
    var hint  = document.getElementById('pred-type-crowd-hint');
    var label = document.getElementById('pred-type-crowd-label');
    if (!radio) return;
    radio.disabled = !enabled;
    if (label) label.style.opacity = enabled ? '1' : '0.45';
    if (hint)  hint.textContent   = enabled ? 'ML augmented with field reports' : 'Awaiting crowd data…';
  }

  function _updateRiskLegend() {
    var row = document.getElementById('legend-risk-row');
    if (!row) return;
    if (predictionType === 'wind') {
      row.innerHTML = '<span class="leg-swatch" style="background:#1e88e5;opacity:.75"></span>High risk zone (Wind)';
    } else if (predictionType === 'crowd') {
      row.innerHTML = '<span class="leg-swatch" style="background:#9c27b0;opacity:.8"></span>High risk zone (ML + Crowd)';
    } else {
      row.innerHTML = '<span class="leg-swatch" style="background:#ff2222;opacity:.7"></span>High risk zone (ML)';
    }
  }

  // ── DEV Window ────────────────────────────────────────────────────────────────

  function _refreshDevWindowState() {
    var win = document.getElementById('dev-window');
    if (!win) return;
    var ts = (_currentTsIndex >= 0 && _timestepsDone.length) ? _timestepsDone[_currentTsIndex] : null;
    var hasEvent = !!currentEvent;
    var hasTs = !!ts;
    var thermal = hasEvent && currentEvent.analysis_mode === 'thermal_monitoring';

    var regionEl = document.getElementById('dev-context-region');
    var timestepEl = document.getElementById('dev-context-timestep');
    var modeEl = document.getElementById('dev-context-mode');
    if (regionEl) regionEl.textContent = hasEvent ? currentEvent.name : 'No region selected';
    if (timestepEl) timestepEl.textContent = hasTs ? fmtDateTime(ts.slot_time) : 'Unavailable';
    if (modeEl) modeEl.textContent = thermal ? 'Industrial thermal monitoring' : (hasEvent ? 'Wildfire prediction' : 'Unknown');

    win.querySelectorAll('.dev-time-btn').forEach(function(btn) { btn.disabled = !_timestepsDone.length; });
    var runBtn = document.getElementById('dev-run-pred-btn');
    var engineTitle = document.getElementById('dev-engine-title');
    var rerunBtn = document.getElementById('dev-rerun-pred-btn');
    var rerunRptBtn = document.getElementById('dev-rerun-report-btn');
    var rerunRptCrwBtn = document.getElementById('dev-rerun-report-crowd-btn');
    var buildAllBtn = document.getElementById('dev-build-all-btn');
    if (runBtn) {
      runBtn.disabled = !hasTs;
      runBtn.textContent = thermal ? '▶ Refresh Observation' : '▶ Run Prediction';
    }
    if (engineTitle) engineTitle.textContent = thermal ? 'Processing Engine' : 'Prediction Engine';
    if (rerunBtn) {
      rerunBtn.disabled = !hasTs || thermal;
      rerunBtn.classList.toggle('hidden', thermal);
    }
    if (rerunRptBtn) rerunRptBtn.disabled = !hasTs;
    if (rerunRptCrwBtn) {
      rerunRptCrwBtn.disabled = !hasTs || thermal;
      rerunRptCrwBtn.classList.toggle('hidden', thermal);
    }
    if (buildAllBtn) {
      buildAllBtn.disabled = !hasEvent;
      buildAllBtn.textContent = thermal ? '▶ Refresh All Observations' : '▶ Run All Slots';
    }

    var gtTitle = document.getElementById('dev-ground-truth-title');
    var gtSection = document.getElementById('dev-ground-truth-section');
    if (gtTitle) gtTitle.classList.toggle('hidden', thermal);
    if (gtSection) gtSection.classList.toggle('hidden', thermal);
    if (thermal) {
      var actualToggle = document.getElementById('dev-actual-toggle');
      if (actualToggle && actualToggle.checked) {
        actualToggle.checked = false;
        eventMap && eventMap.clearActualPerimeter();
      }
    }

    var simulatorTab = win.querySelector('.dev-tab[data-tab="dev-tab-simulator"]');
    if (simulatorTab) simulatorTab.classList.toggle('hidden', thermal);
    if (thermal && simulatorTab && simulatorTab.classList.contains('active')) {
      simulatorTab.classList.remove('active');
      simulatorTab.setAttribute('aria-selected', 'false');
      document.getElementById('dev-tab-simulator')?.classList.add('hidden');
      var controlsTab = win.querySelector('.dev-tab[data-tab="dev-tab-controls"]');
      controlsTab?.classList.add('active');
      controlsTab?.setAttribute('aria-selected', 'true');
      document.getElementById('dev-tab-controls')?.classList.remove('hidden');
    }
    _updateSimBtnState();
  }

  function initDevWindow() {
    var win     = document.getElementById('dev-window');
    var togBtn  = document.getElementById('dev-toggle-btn');
    var closeBtn = document.getElementById('dev-window-close');

    if (!win) return;

    // Toggle visibility
    togBtn && togBtn.addEventListener('click', function() {
      win.classList.toggle('hidden');
      var isOpen = !win.classList.contains('hidden');
      togBtn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
      win.setAttribute('aria-hidden', isOpen ? 'false' : 'true');
      if (isOpen) _refreshDevWindowState();
    });
    closeBtn && closeBtn.addEventListener('click', function() {
      win.classList.add('hidden');
      win.setAttribute('aria-hidden', 'true');
      togBtn?.setAttribute('aria-expanded', 'false');
      togBtn?.focus();
    });
    document.addEventListener('keydown', function(e) {
      if (e.key === 'Escape' && !win.classList.contains('hidden')) closeBtn?.click();
    });

    // Tab switching
    win.addEventListener('click', function(e) {
      var tab = e.target.closest('.dev-tab');
      if (!tab) return;
      var targetId = tab.dataset.tab;
      win.querySelectorAll('.dev-tab').forEach(function(t) { t.classList.remove('active'); });
      win.querySelectorAll('.dev-tab').forEach(function(t) { t.setAttribute('aria-selected', 'false'); });
      win.querySelectorAll('.dev-tab-panel').forEach(function(p) { p.classList.add('hidden'); });
      tab.classList.add('active');
      tab.setAttribute('aria-selected', 'true');
      var panel = document.getElementById(targetId);
      if (panel) panel.classList.remove('hidden');
    });

    // Drag handle
    var header   = document.getElementById('dev-window-header');
    var dragging = false, startX, startY, origLeft, origTop;
    header && header.addEventListener('mousedown', function(e) {
      if (e.target === closeBtn) return;
      dragging = true;
      var r = win.getBoundingClientRect();
      startX = e.clientX; startY = e.clientY;
      origLeft = r.left;  origTop = r.top;
      e.preventDefault();
    });
    document.addEventListener('mousemove', function(e) {
      if (!dragging) return;
      win.style.right  = 'auto';
      win.style.bottom = 'auto';
      var maxLeft = Math.max(0, window.innerWidth - win.offsetWidth);
      var maxTop = Math.max(0, window.innerHeight - 44);
      win.style.left   = Math.max(0, Math.min(maxLeft, origLeft + e.clientX - startX)) + 'px';
      win.style.top    = Math.max(0, Math.min(maxTop, origTop + e.clientY - startY)) + 'px';
    });
    document.addEventListener('mouseup', function() { dragging = false; });

    // ── Time Control buttons — accumulate clicks, apply after 400ms idle ────
    var _devPendingMs  = 0;   // accumulated hour shifts (ms)
    var _devPendingDay = 0;   // accumulated day jumps
    var _devApplyTimer = null;

    var _devPendingLabel = document.getElementById('dev-pending-label');

    function _devUpdatePendingLabel() {
      if (!_devPendingLabel) return;
      var parts = [];
      if (_devPendingDay !== 0) parts.push((_devPendingDay > 0 ? '+' : '') + _devPendingDay + 'd');
      if (_devPendingMs !== 0) {
        var totalH = _devPendingMs / 3600000;
        var d = Math.trunc(totalH / 24);
        var h = totalH % 24;
        if (d !== 0) parts.push((d > 0 ? '+' : '') + d + 'd');
        if (h !== 0) parts.push((h > 0 ? '+' : '') + h + 'h');
      }
      if (parts.length) {
        _devPendingLabel.textContent = 'queued: ' + parts.join(' ');
        _devPendingLabel.style.display = '';
      } else {
        _devPendingLabel.style.display = 'none';
      }
    }

    function _devFlushPending() {
      var msToApply  = _devPendingMs;
      var dayToApply = _devPendingDay;
      _devPendingMs  = 0;
      _devPendingDay = 0;
      _devUpdatePendingLabel();   // clear label first regardless of outcome
      if (msToApply !== 0) _devShiftReplayTime(msToApply);
      if (dayToApply !== 0) {
        var d = new Date(_replayVirtualTime);
        var target = new Date(d.getFullYear(), d.getMonth(), d.getDate() + dayToApply, 12, 0, 0);
        _replayVirtualTime = target.getTime();
        _devApplyReplayTime();
      }
    }

    function _devScheduleFlush() {
      clearTimeout(_devApplyTimer);
      _devApplyTimer = setTimeout(_devFlushPending, 600);
    }

    function _devQueueHr(delta) {
      _devPendingMs += delta;
      _devUpdatePendingLabel();
      _devScheduleFlush();
    }

    function _devQueueDay(delta) {
      _devPendingDay += delta;
      _devUpdatePendingLabel();
      _devScheduleFlush();
    }

    document.getElementById('dev-ts-hr-minus')  && document.getElementById('dev-ts-hr-minus').addEventListener('click',  function() { _devQueueHr(-3600000); });
    document.getElementById('dev-ts-hr-plus')   && document.getElementById('dev-ts-hr-plus').addEventListener('click',   function() { _devQueueHr(3600000);  });
    document.getElementById('dev-ts-day-minus') && document.getElementById('dev-ts-day-minus').addEventListener('click', function() { _devQueueDay(-1); });
    document.getElementById('dev-ts-day-plus')  && document.getElementById('dev-ts-day-plus').addEventListener('click',  function() { _devQueueDay(+1); });
    document.getElementById('dev-ts-speed') && document.getElementById('dev-ts-speed').addEventListener('click', function() {
      _replaySpeed = _replaySpeed === 1 ? 60 : 1;
      this.textContent = 'x' + _replaySpeed;
      this.classList.toggle('dev-speed-active', _replaySpeed !== 1);
    });

    // ── Run Prediction ───────────────────────────────────────────────────────
    document.getElementById('dev-run-pred-btn') && document.getElementById('dev-run-pred-btn').addEventListener('click', function() {
      if (!currentEvent || _currentTsIndex < 0 || !_timestepsDone.length) return;
      var ts  = _timestepsDone[_currentTsIndex];
      var btn = this;
      btn.disabled = true;
      _crowdMode = false;  // revert immediately — standard prediction supersedes crowd layers
      window.API.rerunPredictionStep(currentEvent.id, ts.id)
        .then(function() {
          selectTimestep(ts);   // reload map with standard layers right away
          _showPredStatus();
          _pollUntilDone(ts);
          btn.disabled = false;
        })
        .catch(function(err) {
          btn.disabled = false;
          _hidePredStatus();
          showToast('Processing failed to start: ' + (err.message || 'unknown error'), 'error');
        });
    });

    // ── Rerun Prediction (force + crowd data) ────────────────────────────────
    document.getElementById('dev-rerun-pred-btn') && document.getElementById('dev-rerun-pred-btn').addEventListener('click', function() {
      if (!currentEvent || _currentTsIndex < 0 || !_timestepsDone.length) return;
      var ts  = _timestepsDone[_currentTsIndex];
      var btn = this;
      btn.disabled = true;

      window.API.rerunCrowdPredictionStep(currentEvent.id, ts.id)
        .then(function() {
          _showPredStatus();
          _pollCrowdUntilDone(ts);
          btn.disabled = false;
        })
        .catch(function(err) {
          btn.disabled = false;
          _hidePredStatus();
          showToast('Crowd processing failed to start: ' + (err.message || 'unknown error'), 'error');
        });
    });

    // ── Re-run AI Report ────────────────────────────────────────────────────
    document.getElementById('dev-rerun-report-btn') && document.getElementById('dev-rerun-report-btn').addEventListener('click', function() {
      if (!currentEvent || _currentTsIndex < 0 || !_timestepsDone.length) return;
      var ts  = _timestepsDone[_currentTsIndex];
      var btn = this;
      btn.disabled = true;
      window.API.generateReport(currentEvent.id, ts.id, true)
        .then(function() {
          btn.disabled = false;
          showToast('AI Report regenerated', 'success');
          if (window.AIModal) {
            window.AIModal.setContext(currentEvent.id, ts.id);
            window.AIModal.renderCard();
          }
        })
        .catch(function(err) {
          btn.disabled = false;
          showToast('Re-run failed: ' + (err.message || 'unknown'), 'error');
        });
    });

    document.getElementById('dev-rerun-report-crowd-btn') && document.getElementById('dev-rerun-report-crowd-btn').addEventListener('click', function() {
      if (!currentEvent || _currentTsIndex < 0 || !_timestepsDone.length) return;
      var ts  = _timestepsDone[_currentTsIndex];
      var btn = this;
      btn.disabled = true;
      window.API.generateReportWithCrowd(currentEvent.id, ts.id, true)
        .then(function() {
          btn.disabled = false;
          showToast('AI Report (Crowd) regenerated', 'success');
          if (window.AIModal) {
            window.AIModal.setContext(currentEvent.id, ts.id);
            window.AIModal.renderCard();
          }
        })
        .catch(function(err) {
          btn.disabled = false;
          showToast('Re-run failed: ' + (err.message || 'unknown'), 'error');
        });
    });

    // ── Actual Perimeter toggle ──────────────────────────────────────────────
    document.getElementById('dev-actual-toggle') && document.getElementById('dev-actual-toggle').addEventListener('change', function() {
      var legendRow = document.getElementById('legend-actual-row');
      if (this.checked) {
        if (legendRow) legendRow.style.display = '';
        if (_currentTsIndex >= 0 && _timestepsDone.length) {
          _loadActualPerimeter(_timestepsDone[_currentTsIndex]);
        }
      } else {
        if (legendRow) legendRow.style.display = 'none';
        eventMap && eventMap.clearActualPerimeter();
      }
    });

    // ── User Simulator ───────────────────────────────────────────────────────
    document.getElementById('dev-sim-btn') && document.getElementById('dev-sim-btn').addEventListener('click', function() {
      if (!currentEvent) { showToast('No event selected', 'error'); return; }
      var btn    = this;
      var status = document.getElementById('dev-sim-status');
      var n      = parseInt(document.getElementById('dev-sim-count')?.value) || 5;
      var hints  = (document.getElementById('dev-sim-hint')?.value || '').trim();

      btn.disabled = true;
      if (status) status.textContent = 'Generating ' + n + ' report(s)…';

      var _simTs   = (_currentTsIndex >= 0 && _timestepsDone.length) ? _timestepsDone[_currentTsIndex] : null;
      var _simTsId = _simTs ? _simTs.id : null;
      var _simVirtualTime = new Date(_replayVirtualTime).toISOString();
      window.API.simulateFieldReports(currentEvent.id, n, hints, _simTsId, _simVirtualTime)
        .then(function(reports) {
          if (status) status.textContent = '✓ ' + reports.length + ' report(s) created';
          btn.disabled = false;
          if (window.CrowdPanel) window.CrowdPanel.refresh(new Date(_replayVirtualTime).toISOString());
        })
        .catch(function(err) {
          if (status) status.textContent = 'Error: ' + (err.message || 'unknown');
          btn.disabled = false;
        });
    });

    document.getElementById('dev-sim-clear-btn') && document.getElementById('dev-sim-clear-btn').addEventListener('click', function() {
      if (!currentEvent) { showToast('No event selected', 'error'); return; }
      if (!confirm('Delete ALL field reports for this event? This cannot be undone.')) return;
      var btn    = this;
      var status = document.getElementById('dev-sim-status');
      btn.disabled = true;
      if (status) status.textContent = 'Clearing…';
      window.API.clearFieldReports(currentEvent.id)
        .then(function(res) {
          if (status) status.textContent = '✓ Deleted ' + (res.deleted || 0) + ' report(s)';
          btn.disabled = false;
          if (window.CrowdPanel) window.CrowdPanel.refresh(new Date(_replayVirtualTime).toISOString());
        })
        .catch(function(err) {
          if (status) status.textContent = 'Error: ' + (err.message || 'unknown');
          btn.disabled = false;
        });
    });

    // ── Build All Slots ──────────────────────────────────────────────────────
    document.getElementById('dev-build-all-btn') && document.getElementById('dev-build-all-btn').addEventListener('click', function() {
      if (!currentEvent) { showToast('No event selected', 'error'); return; }
      var btn      = this;
      var progress = document.getElementById('dev-build-all-progress');
      var fill     = document.getElementById('dev-build-all-fill');
      var label    = document.getElementById('dev-build-all-label');

      btn.disabled = true;
      if (progress) progress.classList.remove('hidden');
      if (label)    label.textContent = 'Loading…';
      if (fill)     fill.style.width  = '0%';

      window.API.authorizedFetch('/api/events/' + currentEvent.id + '/build-all', {
        method: 'POST',
      }).then(function(res) {
        if (!res.ok) { throw new Error('HTTP ' + res.status); }
        var reader  = res.body.getReader();
        var decoder = new TextDecoder();
        var buf     = '';

        function _read() {
          return reader.read().then(function(chunk) {
            if (chunk.done) {
              btn.disabled = false;
              showToast('All slots built', 'success');
              return;
            }
            buf += decoder.decode(chunk.value, { stream: true });
            var lines = buf.split('\n');
            buf = lines.pop();
            lines.forEach(function(line) {
              if (!line.startsWith('data:')) return;
              try {
                var d = JSON.parse(line.slice(5).trim());
                if (d.status === 'error') {
                  if (label) label.textContent = 'Error: ' + (d.error || 'unknown');
                  btn.disabled = false;
                  return;
                }
                var pct = d.total > 0 ? Math.round((d.done / d.total) * 100) : 0;
                if (fill)  fill.style.width  = pct + '%';
                if (label) {
                  if (d.status === 'done') {
                    label.textContent = 'Done — ' + d.total + ' slot(s) processed';
                  } else if (d.status === 'loading') {
                    label.textContent = 'Loading assets…';
                  } else {
                    label.textContent = (d.done || 0) + ' / ' + (d.total || '?') + (d.current ? '  ' + d.current : '');
                  }
                }
              } catch(e) {}
            });
            return _read();
          });
        }
        return _read();
      }).catch(function(err) {
        if (label) label.textContent = 'Error: ' + (err.message || 'unknown');
        btn.disabled = false;
      });
    });
  }


  // Shift the replay clock by deltaMs, snap _replayIdx to the correct timestep.
  function _showPredStatus(message) {
    var bar = document.getElementById('prediction-status-bar');
    var text = document.getElementById('prediction-status-text');
    if (text) text.textContent = message || 'Building prediction…';
    if (bar) bar.classList.remove('hidden');
  }

  function _hidePredStatus() {
    var bar = document.getElementById('prediction-status-bar');
    if (bar) bar.classList.add('hidden');
  }

  function _devShiftReplayTime(deltaMs) {
    if (!_timestepsDone.length) return;
    _replayVirtualTime += deltaMs;
    _devApplyReplayTime();
  }

  // Jump to 12:00:00 local time of the next (+1) or previous (-1) calendar day.
  function _devJumpDay(delta) {
    if (!_timestepsDone.length) return;
    var d = new Date(_replayVirtualTime);
    var noon = new Date(d.getFullYear(), d.getMonth(), d.getDate() + delta, 12, 0, 0);
    _replayVirtualTime = noon.getTime();
    _devApplyReplayTime();
  }

  function _devApplyReplayTime() {
    var first = new Date(_timestepsDone[0].slot_time).getTime();
    var last  = new Date(_timestepsDone[_timestepsDone.length - 1].slot_time).getTime();
    _replayVirtualTime = Math.max(first, Math.min(last, _replayVirtualTime));

    // Find the correct _replayIdx for this virtual time
    _replayIdx = 0;
    for (var i = 0; i < _timestepsDone.length; i++) {
      if (new Date(_timestepsDone[i].slot_time).getTime() <= _replayVirtualTime) {
        _replayIdx = i;
      } else { break; }
    }
    _currentTsIndex = _replayIdx;

    var label = document.getElementById('ts-label');
    if (label) label.textContent = fmtDateTime(new Date(_replayVirtualTime).toISOString());
    setGapBadge(_timestepsDone[_replayIdx]);
    _highlightTick(_replayIdx);
    selectTimestep(_timestepsDone[_replayIdx]);

    // Immediately persist so a page refresh restores this position
    if (_isAdmin && currentEvent) {
      window.API.setReplayTime(currentEvent.id, _replayVirtualTime).catch(function(){});
    }
  }

  function _loadActualPerimeter(ts) {
    if (!currentEvent || !eventMap || !ts) return;
    window.API.getActualPerimeter(currentEvent.id, ts.id)
      .then(function(geo) {
        eventMap.renderActualPerimeter(geo);
        // Apply current slider position immediately after render
        var h = +(document.getElementById('fcast-slider')?.value || 0);
        eventMap.setActualPerimVisible('+0h',  h <= 2);
        eventMap.setActualPerimVisible('+3h',  h >= 3  && h <= 5);
        eventMap.setActualPerimVisible('+6h',  h >= 6  && h <= 11);
        eventMap.setActualPerimVisible('+12h', h >= 12);
      })
      .catch(function() { eventMap.clearActualPerimeter(); });
  }

})();
