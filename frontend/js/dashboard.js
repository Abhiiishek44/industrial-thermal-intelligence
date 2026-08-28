/**
 * dashboard.js — Bottom dashboard (no ES modules)
 * Exposes: window.Dashboard
 */
(function() {

  function v(val, decimals, unit) {
    if (val == null || val === undefined) return '<span style="opacity:.4">—</span>';
    const n = (typeof val === 'number') ? val.toFixed(decimals || 0) : val;
    return unit ? (n + ' <small style="opacity:.7">' + unit + '</small>') : String(n);
  }

  function vn(val) {
    if (val == null || val === undefined) return '<span style="opacity:.4">—</span>';
    return Number(val).toLocaleString();
  }

  function text(val) {
    if (val == null || val === undefined || val === '') return '—';
    return String(val).replace(/[&<>"']/g, function(ch) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch];
    });
  }

  function fwiBar(label, value, max, color) {
    const pct = (value != null) ? Math.min(100, (value / max) * 100).toFixed(0) : 0;
    const display = (value != null) ? Number(value).toFixed(1) : '—';
    return '<div class="fwi-row">' +
      '<span class="fwi-label">' + label + '</span>' +
      '<div class="fwi-bar-bg"><div class="fwi-bar-fill" style="width:' + pct + '%;background:' + color + '"></div></div>' +
      '<span class="fwi-val">' + display + '</span>' +
      '</div>';
  }

  function windSparkline(forecast) {
    if (!forecast || !forecast.length) return '<span style="opacity:.4;font-size:10px">No forecast data</span>';
    // Support both wind_forecast format {speed_kmh} and weather format {wind_speed_kmh}
    const speeds = forecast.map(f => f.wind_speed_kmh || f.speed_kmh || 0);
    const maxS   = Math.max.apply(null, speeds.concat([1]));
    const W = 200, H = 38, pad = 4;
    const n = Math.max(speeds.length - 1, 1);
    const pts = speeds.map((s, i) => {
      const x = pad + (i / n) * (W - 2 * pad);
      const y = H - pad - (s / maxS) * (H - 2 * pad);
      return x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');
    const arrows = forecast.filter((_, i) => i % 3 === 0).map((f, idx) => {
      const i = idx * 3;
      const x = pad + (i / n) * (W - 2 * pad);
      const y = H - pad - (speeds[i] / maxS) * (H - 2 * pad);
      return '<g transform="translate(' + x.toFixed(0) + ',' + y.toFixed(0) + ') rotate(' + ((f.wind_dir || 0) + 180) + ')">' +
        '<polygon points="0,-4 1.5,2 0,1 -1.5,2" fill="#ff8c00" opacity=".8"/></g>';
    }).join('');
    return '<svg width="100%" viewBox="0 0 ' + W + ' ' + H + '" style="display:block;overflow:visible">' +
      '<polyline points="' + pts + '" fill="none" stroke="#4fc3f7" stroke-width="1.5" stroke-linejoin="round"/>' +
      arrows + '</svg>';
  }

  function windDirectionLabel(degrees) {
    if (degrees == null || !Number.isFinite(Number(degrees))) return '—';
    return ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(Number(degrees) / 45) % 8];
  }

  function weatherSummary(record) {
    if (!record) return '<span style="opacity:.55;font-size:10px">Weather unavailable</span>';
    const speed = record.wind_speed_kmh != null ? record.wind_speed_kmh : record.speed_kmh;
    return '<table class="stat-table">' +
      '<tr><td>Temperature</td><td>' + v(record.temp_c, 1, '°C') + '</td></tr>' +
      '<tr><td>Humidity</td><td>' + v(record.rh, 0, '%') + '</td></tr>' +
      '<tr><td>Wind</td><td>' + v(speed, 1, 'km/h') + ' ' + windDirectionLabel(record.wind_dir) + '</td></tr>' +
      '<tr><td>Valid time</td><td>' + text(record.valid_time || 'Current observation') + '</td></tr>' +
      '<tr><td>Source</td><td>' + text(record.source || 'Weather provider') + '</td></tr>' +
    '</table>';
  }

  function renderThermalDashboard(el, fireCtx, weatherForecast) {
    const fire = fireCtx.fire || {};
    const thermal = fireCtx.thermal || {};
    const wf = weatherForecast || [];
    const landcover = thermal.landcover_group_counts || {};
    const confidence = thermal.confidence_counts || {};
    const industries = thermal.nearest_industries || [];
    const viewLabel = thermal.view_mode === 'classification' ? 'Source Classification'
      : thermal.view_mode === 'persistent' ? 'Persistent Sources'
      : thermal.view_mode === '30d' ? '30-Day Activity'
      : thermal.view_mode === '5d' ? '5-Day Activity'
      : 'Observation Replay';
    const classified = thermal.view_mode === 'classification';
    const persistent = thermal.view_mode === 'persistent' || classified;

    const countList = function(values) {
      const entries = Object.keys(values).map(function(key) {
        return text(key) + ': <b>' + vn(values[key]) + '</b>';
      });
      return entries.length ? entries.join('<br>') : '—';
    };

    el.innerHTML =
      '<div class="dash-card">' +
        '<div class="dash-card-title">Thermal detections</div>' +
        '<div style="font-size:10px;color:var(--text2);margin-bottom:5px">' + viewLabel + '</div>' +
        '<table class="stat-table">' +
          (persistent ? '<tr><td>Persistent sources</td><td>' + v(thermal.persistent_source_count, 0) + '</td></tr>' : '') +
          '<tr><td>Aggregated detections</td><td>' + v(thermal.detection_count != null ? thermal.detection_count : fire.n_hotspots, 0) + '</td></tr>' +
          '<tr><td>Raw observations</td><td>' + v(thermal.raw_observation_count != null ? thermal.raw_observation_count : thermal.detection_count, 0) + '</td></tr>' +
          '<tr><td>FRP total</td><td>' + v(fire.frp_sum, 2, 'MW') + '</td></tr>' +
          '<tr><td>FRP mean</td><td>' + v(thermal.frp_mean_mw, 2, 'MW') + '</td></tr>' +
          '<tr><td>FRP maximum</td><td>' + v(thermal.frp_max_mw, 2, 'MW') + '</td></tr>' +
          '<tr><td>Max brightness</td><td>' + v(thermal.brightness_ti4_max_k, 1, 'K') + '</td></tr>' +
        '</table>' +
      '</div>' +

      (persistent ? '<div class="dash-card">' +
        '<div class="dash-card-title">Persistence</div>' +
        '<table class="stat-table">' +
          '<tr><td>Levels</td><td>' + countList(thermal.persistence_level_counts || {}) + '</td></tr>' +
          '<tr><td>Highest active days</td><td>' + v(thermal.highest_active_days, 0) + '</td></tr>' +
          '<tr><td>Longest duration</td><td>' + v(thermal.longest_duration_days, 1, 'days') + '</td></tr>' +
          '<tr><td>Highest night recurrence</td><td>' + v(thermal.highest_night_ratio != null ? thermal.highest_night_ratio * 100 : null, 0, '%') + '</td></tr>' +
        '</table>' +
      '</div>' : '') +

      '<div class="dash-card">' +
        '<div class="dash-card-title">Industrial context</div>' +
        '<table class="stat-table">' +
          '<tr><td>Inside industrial zone</td><td>' + vn(thermal.inside_industrial_area_count) + '</td></tr>' +
          '<tr><td>Near facility</td><td>' + vn(thermal.near_industrial_facility_count) + '</td></tr>' +
          '<tr><td>Nearest industry</td><td>' + text(industries.join(', ')) + '</td></tr>' +
        '</table>' +
      '</div>' +

      '<div class="dash-card">' +
        '<div class="dash-card-title">Observation context</div>' +
        '<table class="stat-table">' +
          '<tr><td>Land cover</td><td>' + countList(landcover) + '</td></tr>' +
          '<tr><td>Confidence</td><td>' + countList(confidence) + '</td></tr>' +
          '<tr><td>Satellite</td><td>' + text((thermal.satellites || []).join(', ')) + '</td></tr>' +
        '</table>' +
      '</div>' +

      '<div class="dash-card">' +
        '<div class="dash-card-title">Source classification</div>' +
        (classified
          ? '<table class="stat-table">' +
              '<tr><td>Classes</td><td>' + countList(thermal.classification_counts || {}) + '</td></tr>' +
              '<tr><td>Industrial-fire alerts</td><td>' + vn(thermal.emergency_candidate_count) + '</td></tr>' +
              '<tr><td>Mean confidence</td><td>' + v(thermal.classification_mean_confidence != null ? thermal.classification_mean_confidence * 100 : null, 0, '%') + '</td></tr>' +
              '<tr><td>Method</td><td>' + text((thermal.classification_method || 'explainable_rules_v2').replaceAll('_', ' ')) + '</td></tr>' +
            '</table>' +
            '<div style="font-size:10px;opacity:.6;margin-top:5px">Rule-based baseline, not a trained ML model.</div>'
          : '<div style="font-size:12px;line-height:1.5">' +
              '<b>Open Source Classification view</b><br>' +
              '<span style="opacity:.65">Classifies persistent sources and short-lived fire episodes using facility, temporal and land-cover evidence.</span>' +
            '</div>') +
      '</div>' +

      '<div class="dash-card">' +
        '<div class="dash-card-title">Weather</div>' +
        '<div id="fcast-weather" class="fcast-weather">' +
          (wf.length ? weatherSummary(wf[0]) : '<span style="opacity:.4;font-size:10px">Loading…</span>') +
        '</div>' +
      '</div>' +

      '<div class="dash-card dash-card-wide">' +
        '<div class="dash-card-title">Wind Forecast +12h</div>' +
        '<div id="dash-wind-sparkline">' + windSparkline(wf) + '</div>' +
        '<div id="dash-wind-labels" class="forecast-labels">' +
          wf.filter((_, i) => i % 3 === 0).map(f => {
            const spd = (f.wind_speed_kmh || f.speed_kmh);
            return '<span>+' + f.hour + 'h<br><b>' + (spd != null ? spd.toFixed(0) : '—') + '</b></span>';
          }).join('') +
        '</div>' +
      '</div>';
  }

  function renderDashboard(analysis, fireCtx, weatherForecast) {
    const el = document.getElementById('dashboard-content');
    if (!el) return;

    // Safely extract nested data
    const fire = (fireCtx && fireCtx.fire)   || {};
    const fwi  = (fireCtx && fireCtx.fwi_t1) || {};
    const wf   = weatherForecast             || [];   // from weather/forecast.json
    const pop  = analysis || {};

    // Validate we have some real data
    if (!fireCtx && !analysis) {
      el.innerHTML = '<div class="dash-empty">No data available for this timestep</div>';
      return;
    }

    if (fireCtx && fireCtx.analysis_mode === 'thermal_monitoring') {
      renderThermalDashboard(el, fireCtx, weatherForecast);
      return;
    }

    el.innerHTML =

      // ── Weather (updates with Forecast Horizon slider) ──
      '<div class="dash-card">' +
        '<div class="dash-card-title">Weather</div>' +
        '<div id="fcast-weather" class="fcast-weather">' +
          '<span style="opacity:.4;font-size:10px">Loading…</span>' +
        '</div>' +
      '</div>' +

      // ── Population ──
      '<div class="dash-card">' +
        '<div class="dash-card-title">Population</div>' +
        '<div class="pop-affected">' +
          '<div class="pop-affected-num">' + vn(pop.affected_population) + '</div>' +
          '<div class="pop-affected-lbl">Affected · in perimeter</div>' +
        '</div>' +
        '<div class="pop-risk-section">' +
          '<div class="pop-risk-title">At risk</div>' +
          '<div class="pop-risk-row">' +
            '<div class="pop-stat risk3"><div class="pop-num">'  + vn(pop.at_risk_3h)  + '</div><div class="pop-label">+3h</div></div>' +
            '<div class="pop-stat risk6"><div class="pop-num">'  + vn(pop.at_risk_6h)  + '</div><div class="pop-label">+6h</div></div>' +
            '<div class="pop-stat risk12"><div class="pop-num">' + vn(pop.at_risk_12h) + '</div><div class="pop-label">+12h</div></div>' +
          '</div>' +
        '</div>' +
      '</div>' +

      // ── Fire ──
      '<div class="dash-card">' +
        '<div class="dash-card-title">Fire</div>' +
        '<table class="stat-table">' +
          '<tr><td>Burned</td><td>' + v(fire.burned_area_km2, 1, 'km²')     + '</td></tr>' +
          '<tr><td>New area</td><td>' + v(fire.new_area_km2, 1, 'km²')       + '</td></tr>' +
          '<tr><td>Growth</td><td>' + v(fire.growth_rate_km2h, 2, 'km²/h')  + '</td></tr>' +
          '<tr><td>Hotspots</td><td>' + v(fire.n_hotspots, 0)                + '</td></tr>' +
          '<tr><td>FRP sum</td><td>' + v(fire.frp_sum, 0, 'MW')             + '</td></tr>' +
        '</table>' +
      '</div>' +

      // ── FWI ──
      '<div class="dash-card">' +
        '<div class="dash-card-title">FWI</div>' +
        '<div class="fwi-stack">' +
          fwiBar('FFMC',    fwi.ffmc,       101,  '#ff6b35') +
          fwiBar('ISI',     fwi.isi,        30,   '#ff4444') +
          fwiBar('ROS avg', fwi.ros_mean_mh, 800,  '#ffd700') +
          fwiBar('ROS max', fwi.ros_max_mh,  2000, '#ff2222') +
        '</div>' +
      '</div>' +

      // ── Wind Forecast ──
      '<div class="dash-card dash-card-wide">' +
        '<div class="dash-card-title">Wind Forecast +12h</div>' +
        '<div id="dash-wind-sparkline">' + windSparkline(wf) + '</div>' +
        '<div id="dash-wind-labels" class="forecast-labels">' +
          wf.filter((_, i) => i % 3 === 0).map(f => {
            const spd = (f.wind_speed_kmh || f.speed_kmh);
            const max = f.max_wind_speed_kmh;
            return '<span>+' + f.hour + 'h<br><b>' + (spd != null ? spd.toFixed(0) : '—') + '</b>' +
              (max != null ? '<br><small style="opacity:.55">↑' + max.toFixed(0) + '</small>' : '') + '</span>';
          }).join('') +
        '</div>' +
      '</div>';
  }

  // Pending state: weather/wind forecast shown immediately;
  // prediction-dependent cards show a spinner until Stage 1 completes.
  function renderDashboardPending(weatherForecast) {
    const el = document.getElementById('dashboard-content');
    if (!el) return;
    const wf = weatherForecast || [];

    const loadingCard = function(title) {
      return '<div class="dash-card">' +
        '<div class="dash-card-title">' + title + '</div>' +
        '<div class="dash-card-loading"><div class="dash-loading-spinner"></div>Building prediction…</div>' +
      '</div>';
    };

    el.innerHTML =

      // Weather — available immediately from ERA5 (same element as Forecast Horizon)
      '<div class="dash-card">' +
        '<div class="dash-card-title">Weather</div>' +
        '<div id="fcast-weather" class="fcast-weather">' +
          (wf.length ? '' : '<span style="opacity:.4;font-size:10px">Loading…</span>') +
        '</div>' +
      '</div>' +

      loadingCard('Population') +
      loadingCard('Fire') +
      loadingCard('FWI') +

      // Wind Forecast — available immediately from ERA5
      '<div class="dash-card dash-card-wide">' +
        '<div class="dash-card-title">Wind Forecast +12h</div>' +
        '<div id="dash-wind-sparkline">' +
          (wf.length ? windSparkline(wf) : '<span style="opacity:.4;font-size:10px">Loading…</span>') +
        '</div>' +
        '<div id="dash-wind-labels" class="forecast-labels">' +
          wf.filter((_, i) => i % 3 === 0).map(f => {
            const spd = (f.wind_speed_kmh || f.speed_kmh);
            const max = f.max_wind_speed_kmh;
            return '<span>+' + f.hour + 'h<br><b>' + (spd != null ? spd.toFixed(0) : '—') + '</b>' +
              (max != null ? '<br><small style="opacity:.55">↑' + max.toFixed(0) + '</small>' : '') + '</span>';
          }).join('') +
        '</div>' +
      '</div>';
  }

  function clearDashboard() {
    const el = document.getElementById('dashboard-content');
    if (el) el.innerHTML = '<div class="dash-empty">Select a timestep to view data</div>';
  }

  // Called when weather/forecast.json arrives (async, after renderDashboard)
  function updateWeather(weatherForecast, _attempt) {
    const sparkEl = document.getElementById('dash-wind-sparkline');
    const labsEl  = document.getElementById('dash-wind-labels');
    const weatherEl = document.getElementById('fcast-weather');
    if (!weatherForecast || !weatherForecast.length) {
      if (weatherEl) weatherEl.innerHTML = '<span style="opacity:.55;font-size:10px">Weather unavailable</span>';
      if (sparkEl) sparkEl.innerHTML = '<span style="opacity:.55;font-size:10px">Wind forecast unavailable</span>';
      if (labsEl) labsEl.innerHTML = '';
      return;
    }
    if (!sparkEl || !labsEl) {
      if ((_attempt || 0) < 15) setTimeout(function() { updateWeather(weatherForecast, (_attempt || 0) + 1); }, 100);
      return;
    }
    if (weatherEl) weatherEl.innerHTML = weatherSummary(weatherForecast[0]);
    sparkEl.innerHTML = windSparkline(weatherForecast);
    labsEl.innerHTML  = weatherForecast.filter((_, i) => i % 3 === 0).map(f => {
      const spd = (f.wind_speed_kmh || f.speed_kmh);
      const max = f.max_wind_speed_kmh;
      return '<span>+' + f.hour + 'h<br><b>' + (spd != null ? spd.toFixed(0) : '—') + '</b>' +
        (max != null ? '<br><small style="opacity:.55">↑' + max.toFixed(0) + '</small>' : '') + '</span>';
    }).join('');
  }

  window.Dashboard = { renderDashboard, renderDashboardPending, clearDashboard, updateWeather };
})();
