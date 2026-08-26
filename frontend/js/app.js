/**
 * app.js — Two-view SPA (Home → Event)
 * Depends on: window.API, window.HomeMap, window.EventMap,
 *             window.Dashboard, window.AIModal
 */
(function() {
  let homeMap  = null;
  let eventMap = null;
  let darkMode = true;
  let allEvents = [];
  let currentEvent = null;
  let currentWeather = [];   // [{hour, temp_c, rh, wind_speed_kmh, wind_dir}]

  let predictionType      = 'ml';   // 'ml' | 'wind' | 'crowd'
  let _timestepsDone      = [];     // full list of done timesteps for current event
  let _currentTsIndex     = -1;     // index of currently selected timestep in _timestepsDone
  let _replayVirtualTime  = 0;      // current virtual clock ms (shared with DEV controls)
  let _replayIdx          = -1;     // which done[] entry the clock is currently on
  let _replaySpeed        = 1;      // clock multiplier: 1 or 60
  let _isAdmin            = false;  // set after login/verify
  let _syncPushInterval   = null;   // admin: pushes virtual time to server every 10s

  // ── Boot ─────────────────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', async function() {
    initTheme();
    initAuth();

    homeMap  = new window.HomeMap('home-map', openEvent);
    eventMap = new window.EventMap('event-map');

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
        if (e.deltaY !== 0) {
          e.preventDefault();
          dashEl.scrollLeft += e.deltaY;
        }
      }, { passive: false });
    }

    window.AIModal.init();
    window.Dashboard.clearDashboard();
    if (window.CrowdPanel) window.CrowdPanel.init();
    initMobileFAB();
    initLeftPanelCollapse();
    initChatColCollapse();

    document.getElementById('nav-home-btn')?.addEventListener('click', function() { goHome(); });

    document.getElementById('dash-collapse-btn')?.addEventListener('click', function() {
      document.getElementById('bottom-panel')?.classList.toggle('collapsed');
    });

    initPredType();
    initDevWindow();

    allEvents = await loadEvents();
    window.API.getRealtimeFirms().then(function(fc) {
      homeMap.renderFirms(fc);
    }).catch(function() {});

    // Deep-link: /demo?event_id=<id>
    var _urlParams = new URLSearchParams(window.location.search);
    var _urlEventId = _urlParams.get('event_id');
    if (_urlEventId) {
      var _deepEvent = allEvents.find(function(e) { return String(e.id) === _urlEventId; });
      if (_deepEvent) { openEvent(_deepEvent); } else { showView('home'); }
    } else {
      showView('home');
    }

    window.addEventListener('popstate', function(e) {
      if (e.state && e.state.eventId) {
        var ev = allEvents.find(function(e2) { return e2.id === e.state.eventId; });
        if (ev) { openEvent(ev, true); return; }
      }
      goHome(true);
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
    currentEvent = null;
    if (_syncPushInterval) { clearInterval(_syncPushInterval); _syncPushInterval = null; }
    if (window._syncRefetchInterval) { clearInterval(window._syncRefetchInterval); window._syncRefetchInterval = null; }
    window.Dashboard.clearDashboard();
    window.AIModal.setContext(null, null);
    window.AIModal.close();
    if (window.CrowdPanel) window.CrowdPanel.clearEvent();
    if (!fromPopstate) history.pushState({}, '', '/demo');
    showView('home');
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
      return '<div class="hs-event-item" data-id="' + ev.id + '">' +
        '<div class="hs-event-name">' + escHtml(ev.name) + '</div>' +
        '<div class="hs-event-meta">' + ev.year + ' · Click to open</div>' +
        '</div>';
    }).join('');
    el.querySelectorAll('.hs-event-item').forEach(function(item) {
      item.addEventListener('click', function() {
        const ev = events.find(function(e) { return String(e.id) === item.dataset.id; });
        if (ev) openEvent(ev);
      });
    });
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
    currentEvent = ev;
    if (!fromPopstate) history.pushState({ eventId: ev.id }, '', '/demo?event_id=' + ev.id);

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
    }

    // Sync replay clock with server
    // Both admin and non-admin pull once on load to restore saved position.
    window.API.getReplayTime(ev.id).then(function(d) {
      if (d.ms && _timestepsDone.length) {
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
    _currentTsIndex = 0;
    if (!done.length) {
      container.innerHTML = '<div class="empty-msg">No timesteps yet</div>';
      return;
    }

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
          '<span id="ts-label">' + fmtDateTime(done[0].slot_time) + '</span>' +
          '<span id="ts-live-badge" class="ts-live-badge">● LIVE</span>' +
        '</div>' +
        '<div class="ts-meta-row">' +
          '<div class="ts-meta-item">' +
            '<span class="ts-meta-lbl">T1 hotspot</span>' +
            '<span class="ts-meta-val" id="ts-t1-label">—</span>' +
          '</div>' +
          '<div class="ts-meta-item">' +
            '<span class="ts-meta-lbl">Imagery</span>' +
            '<span class="ts-meta-val" id="ts-sat-label">—</span>' +
          '</div>' +
          '<div class="ts-meta-item">' +
            '<span class="ts-meta-lbl">Data gap</span>' +
            '<span id="ts-gap-badge" class="ts-gap"></span>' +
          '</div>' +
        '</div>' +
      '</div>';

    setGapBadge(done[0]);
    _highlightTick(0);

    // ── Virtual real-time clock ───────────────────────────────────────────────
    // Uses module-level _replayVirtualTime, _replayIdx, _replaySpeed so DEV
    // controls can jump/speed the clock without touching each other's state.
    _replayVirtualTime = new Date(done[0].slot_time).getTime();
    _replayIdx = 0;
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

    selectTimestep(done[0]);
    startPlay();
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
    currentWeather = weatherData || [];
    var section = document.getElementById('fcast-section');
    var titleEl = document.getElementById('fcast-section-title');
    if (section) section.style.display = '';
    if (titleEl) titleEl.style.display = '';
    var slider = document.getElementById('fcast-slider');
    if (slider) { slider.value = 0; }
    _setForecastHour(0);
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
    _pollCrowdIntervals[key] = setInterval(async function() {
      try {
        var s = await window.API.getTsStatus(currentEvent.id, ts.id);
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
              selectTimestep(_timestepsDone[idx]);
            }
          }
        }
      } catch(e) {}
    }, 2000);
  }

  async function selectTimestep(ts) {
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

    // Show status bar and trigger/poll if not fully done
    if (ts.prediction_status !== 'done' || ts.spatial_analysis_status !== 'done') {
      _showPredStatus();
      if (ts.prediction_status === 'pending') {
        window.API.runPredictionStep(eid, tsid).catch(function() {});
      }
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

    // MODIS Terra true-color daily mosaic via NASA GIBS — set straight to slot date
    const satEl = document.getElementById('ts-sat-label');
    if (ts.slot_time) {
      const satDate = ts.slot_time.split('T')[0];
      eventMap.setSatelliteDate(satDate);
      if (satEl) satEl.textContent = satDate;
    }

    // Update AI context (does NOT open modal or load report yet)
    window.AIModal.setContext(eid, tsid);

    // Update crowd panel to show only reports up to this timestep's slot time
    if (window.CrowdPanel && ts.slot_time) {
      window.CrowdPanel.refresh(ts.slot_time);
    }

    // Map layers (non-blocking)
    Promise.allSettled([
      window.API.getPerimeter(eid, tsid, _crowdMode),
      window.API.getHotspots(eid, tsid, _crowdMode),
      window.API.getRiskZones(eid, tsid, _crowdMode),
      window.API.getRoads(eid, tsid),
      window.API.getWindRiskZones(eid, tsid),
    ]).then(function(r) {
      if (r[0].status === 'fulfilled') eventMap.renderPerimeter(r[0].value);
      if (r[1].status === 'fulfilled') eventMap.renderHotspots(r[1].value);
      if (r[2].status === 'fulfilled') eventMap.renderRiskZones(r[2].value);
      if (r[3].status === 'fulfilled') eventMap.renderRoads(r[3].value);
      if (r[4].status === 'fulfilled') eventMap.renderRiskZonesWind(r[4].value);
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
    ]).then(function(r) {
      var forecast  = r[2].status === 'fulfilled' ? r[2].value : [];
      var windHours = r[3].status === 'fulfilled' ? r[3].value : [];
      // renderDashboard first (creates the DOM elements), then update weather into them
      window.Dashboard.renderDashboard(
        r[0].status === 'fulfilled' ? r[0].value : null,
        r[1].status === 'fulfilled' ? r[1].value : null,
        forecast,
      );
      window.Dashboard.updateWeather(forecast);
      eventMap && eventMap.loadWindField(windHours);
      _initForecastSlider(forecast);
      window.AIModal.renderCard();
    });
  }

  // ── Chat column collapse ──────────────────────────────────────────────────────

  function initChatColCollapse() {
    var btn = document.getElementById('chat-col-collapse-btn');
    var col = document.getElementById('ai-chat-col');
    if (!btn || !col) return;
    function _toggle() { col.classList.toggle('chat-collapsed'); }
    btn.addEventListener('click', _toggle);
    // Clicking the title bar also expands when collapsed
    col.querySelector('.chat-col-title').addEventListener('click', function(e) {
      if (col.classList.contains('chat-collapsed') && e.target !== btn && !btn.contains(e.target)) _toggle();
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

  // ── Auth (GitHub OAuth — login wall) ──────────────────────────────────────────

  function initAuth() {
    // 1) If we just came back from GitHub callback, the token is in the URL hash.
    window.API.captureTokenFromHash();

    document.getElementById('auth-logout-btn')?.addEventListener('click', function() {
      window.API.logout();
      updateAuthUI(null);
      showAuthModal();
    });
    document.getElementById('github-login-btn')?.addEventListener('click', function() {
      window.location.href = window.API.githubLoginUrl();
    });

    const token = localStorage.getItem('wf_token');
    if (token) {
      window.API.verifyToken()
        .then(function(d) { updateAuthUI(d.github_login, d.is_admin, d.avatar_url); hideAuthModal(); })
        .catch(function() { updateAuthUI(null); showAuthModal(); });
    } else {
      showAuthModal();
    }
  }

  // The auth modal is non-dismissible — site is gated behind GitHub login.
  function showAuthModal() { document.getElementById('auth-modal-overlay').classList.add('visible'); }
  function hideAuthModal() { document.getElementById('auth-modal-overlay').classList.remove('visible'); }

  function updateAuthUI(login, isAdmin, avatarUrl) {
    const logoutBtn = document.getElementById('auth-logout-btn');
    const userLabel = document.getElementById('auth-user-label');
    const devBtn    = document.getElementById('dev-toggle-btn');
    if (login) {
      _isAdmin = !!isAdmin;
      logoutBtn?.classList.remove('hidden');
      if (userLabel) userLabel.textContent = login + (isAdmin ? ' (admin)' : '');
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

  function initDevWindow() {
    var win     = document.getElementById('dev-window');
    var togBtn  = document.getElementById('dev-toggle-btn');
    var closeBtn = document.getElementById('dev-window-close');

    if (!win) return;

    // Toggle visibility
    togBtn && togBtn.addEventListener('click', function() {
      win.classList.toggle('hidden');
      if (!win.classList.contains('hidden')) {

        var runBtn         = document.getElementById('dev-run-pred-btn');
        var rerunBtn       = document.getElementById('dev-rerun-pred-btn');
        var rerunRptBtn    = document.getElementById('dev-rerun-report-btn');
        var rerunRptCrwBtn = document.getElementById('dev-rerun-report-crowd-btn');
        var buildAllBtn    = document.getElementById('dev-build-all-btn');
        if (runBtn)         runBtn.disabled         = !currentEvent;
        if (rerunBtn)       rerunBtn.disabled       = !currentEvent;
        if (rerunRptBtn)    rerunRptBtn.disabled    = !currentEvent;
        if (rerunRptCrwBtn) rerunRptCrwBtn.disabled = !currentEvent;
        if (buildAllBtn)    buildAllBtn.disabled    = !currentEvent;
        _updateSimBtnState();
      }
    });
    closeBtn && closeBtn.addEventListener('click', function() { win.classList.add('hidden'); });

    // Tab switching
    win.addEventListener('click', function(e) {
      var tab = e.target.closest('.dev-tab');
      if (!tab) return;
      var targetId = tab.dataset.tab;
      win.querySelectorAll('.dev-tab').forEach(function(t) { t.classList.remove('active'); });
      win.querySelectorAll('.dev-tab-panel').forEach(function(p) { p.classList.add('hidden'); });
      tab.classList.add('active');
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
      win.style.left   = (origLeft + e.clientX - startX) + 'px';
      win.style.top    = (origTop  + e.clientY - startY) + 'px';
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
        .catch(function() { btn.disabled = false; });
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

      var token = localStorage.getItem('wf_token');
      fetch(window.API.BASE + '/api/events/' + currentEvent.id + '/build-all', {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + token },
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
  function _showPredStatus() {
    var bar = document.getElementById('prediction-status-bar');
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
