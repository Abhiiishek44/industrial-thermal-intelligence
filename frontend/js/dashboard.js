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

  function formatTime(value) {
    if (!value) return 'Awaiting data';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return text(value);
    return parsed.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  function updateHud(fireCtx, event, viewMode) {
    const thermal = (fireCtx && fireCtx.thermal) || {};
    const viewLabels = {
      '5d': '5-Day Activity',
      '30d': '30-Day Activity',
      replay: 'Observation Replay',
      persistent: 'Persistent Sources',
      classification: 'Source Classification',
    };
    const viewLabel = viewLabels[viewMode || thermal.view_mode] || 'Thermal Monitoring';
    const eventLabel = event && (event.name || event.region_name) ? (event.name || event.region_name) : 'Monitoring Region';
    const tactical = document.getElementById('tactical-hud');
    const observationTime = thermal.window_end || (fireCtx && fireCtx.observation_time);
    if (tactical) {
      tactical.querySelector('.hud-label').textContent = eventLabel + ' / ' + viewLabel;
      tactical.setAttribute('aria-label', eventLabel + ', ' + viewLabel + ', observation ' + formatTime(observationTime));
    }
  }

  function breakdownBars(values, labels, emptyLabel) {
    const entries = Object.keys(values || {}).map(function(key) {
      return { key: key, label: labels[key] || key.replace(/_/g, ' '), value: Number(values[key]) || 0 };
    }).filter(function(item) { return item.value > 0; }).sort(function(a, b) { return b.value - a.value; });
    const total = entries.reduce(function(sum, item) { return sum + item.value; }, 0);
    if (!entries.length) return '<div style="font-size:11px;color:var(--text2)">' + text(emptyLabel) + '</div>';
    return entries.map(function(item, index) {
      const pct = total ? Math.max(4, Math.round(item.value / total * 100)) : 0;
      return '<div class="thermal-breakdown-row" aria-label="' + text(item.label) + ': ' + vn(item.value) + '">' +
        '<div class="thermal-breakdown-label"><span>' + text(item.label) + '</span><b>' + vn(item.value) + '</b></div>' +
        '<div class="thermal-breakdown-track"><span class="thermal-breakdown-fill thermal-breakdown-fill-' + ((index % 6) + 1) + '" style="width:' + pct + '%"></span></div>' +
      '</div>';
    }).join('');
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
    const classLabels = {
      industrial_fire: 'Industrial fire',
      gas_flare: 'Gas flare',
      agricultural_burning: 'Crop burn',
      mining_activity: 'Mining',
      wildfire: 'Wildfire',
      industrial_process_heat: 'Process heat',
      unknown: 'Uncertain',
    };
    const persistenceLabels = { HIGH: 'High', MEDIUM: 'Medium', LOW: 'Low', UNKNOWN: 'Unknown' };

    const frpSumDisplay = fire.frp_sum != null ? Number(fire.frp_sum).toLocaleString(undefined, {minimumFractionDigits:1, maximumFractionDigits:2}) : '—';
    const frpMeanDisplay = thermal.frp_mean_mw != null ? Number(thermal.frp_mean_mw).toFixed(2) : '—';
    const detCount = thermal.detection_count != null ? thermal.detection_count : (fire.n_hotspots || 0);

    const insideCount = thermal.inside_industrial_area_count != null ? thermal.inside_industrial_area_count : 0;
    const nearCount = thermal.near_industrial_facility_count != null ? thermal.near_industrial_facility_count : 0;
    const totalHits = insideCount + nearCount;

    // Landcover distribution
    const lcBare = landcover['bare'] || landcover['Bare land'] || 0;
    const lcVeg = (landcover['cropland'] || 0) + (landcover['forest'] || 0) + (landcover['shrubland'] || 0);
    const lcBuilt = landcover['built_up'] || landcover['Built-up'] || 0;
    const lcTotal = Math.max(1, lcBare + lcVeg + lcBuilt);

    const pctBare = Math.round((lcBare / lcTotal) * 100);
    const pctVeg = Math.round((lcVeg / lcTotal) * 100);
    const pctBuilt = Math.round((lcBuilt / lcTotal) * 100);

    el.innerHTML =
      '<!-- Tile 1: Thermal Activity -->' +
      '<div class="data-card p-5 flex flex-col gap-3 relative overflow-hidden" style="min-width:240px">' +
        '<div class="font-label-caps text-label-caps text-on-surface-variant uppercase tracking-wider" style="font-size:10px;color:var(--text2);font-weight:700">Detections (' + viewLabel + ')</div>' +
        '<div class="flex items-end justify-between mt-1" style="display:flex;align-items:baseline;justify-content:space-between;margin-top:4px">' +
          '<span class="font-data-lg text-[32px] leading-none text-on-background font-bold tracking-tight" style="font-size:26px;font-weight:800;font-family:\'JetBrains Mono\',monospace;color:var(--text)">' + vn(detCount) + '<span class="text-[14px] text-on-surface-variant ml-1 font-normal" style="font-size:12px;color:var(--text2);margin-left:4px">sources</span></span>' +
        '</div>' +
        '<div class="mt-4 flex justify-between text-on-surface-variant pt-4 border-t border-outline-variant/50" style="display:flex;justify-content:space-between;border-top:1px solid var(--border);padding-top:8px;margin-top:8px">' +
          '<div class="flex flex-col"><span class="font-label-caps text-[10px] uppercase" style="font-size:9px;color:var(--text2)">TOTAL FRP</span><span class="font-data-sm text-data-sm text-on-background font-medium" style="font-size:11px;font-weight:700;font-family:\'JetBrains Mono\',monospace">' + frpSumDisplay + ' MW</span></div>' +
          '<div class="flex flex-col text-right"><span class="font-label-caps text-[10px] uppercase" style="font-size:9px;color:var(--text2)">PEAK FRP</span><span class="font-data-sm text-data-sm text-on-background font-medium" style="font-size:11px;font-weight:700;font-family:\'JetBrains Mono\',monospace">' + v(thermal.frp_max_mw, 1, 'MW') + '</span></div>' +
        '</div>' +
      '</div>' +

      '<!-- Tile 2: Industrial Context -->' +
      '<div class="data-card p-5 flex flex-col gap-3 relative overflow-hidden" style="min-width:210px">' +
        '<div class="font-label-caps text-label-caps text-on-surface-variant uppercase tracking-wider" style="font-size:10px;color:var(--text2);font-weight:700">Proximity Impact</div>' +
        '<div class="flex items-end justify-between mt-1" style="display:flex;align-items:baseline;justify-content:space-between;margin-top:4px">' +
          '<span class="font-data-lg text-[32px] leading-none text-on-background font-bold tracking-tight" style="font-size:26px;font-weight:800;font-family:\'JetBrains Mono\',monospace;color:var(--text)">' + vn(totalHits) + '<span class="text-[14px] text-on-surface-variant ml-1 font-normal" style="font-size:12px;color:var(--text2);margin-left:4px">Hits</span></span>' +
        '</div>' +
        '<div class="mt-4 flex flex-col gap-2 pt-4 border-t border-outline-variant/50" style="border-top:1px solid var(--border);padding-top:8px;margin-top:8px">' +
          '<div style="display:flex;justify-content:space-between;font-size:11px"><span style="color:var(--text2)">Inside Zone</span><span style="font-weight:700;font-family:\'JetBrains Mono\',monospace">' + vn(insideCount) + '</span></div>' +
          '<div style="display:flex;justify-content:space-between;font-size:11px"><span style="color:var(--text2)">Near Facility</span><span style="font-weight:700;font-family:\'JetBrains Mono\',monospace">' + vn(nearCount) + '</span></div>' +
        '</div>' +
      '</div>' +

      '<!-- Tile 3: Spread Distribution -->' +
      '<div class="data-card p-5 flex flex-col gap-3 relative overflow-hidden" style="min-width:220px">' +
        '<div class="font-label-caps text-label-caps text-on-surface-variant uppercase tracking-wider" style="font-size:10px;color:var(--text2);font-weight:700">Spread Distribution</div>' +
        '<div style="display:flex;flex-direction:column;gap:6px;margin-top:6px">' +
          '<div><div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:2px"><span>Bare Land</span><span style="font-family:\'JetBrains Mono\',monospace;color:var(--text2)">' + vn(lcBare) + '</span></div><div style="width:100%;height:4px;background:var(--border);border-radius:99px;overflow:hidden"><div style="width:' + pctBare + '%;height:100%;background:var(--warn);border-radius:99px"></div></div></div>' +
          '<div><div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:2px"><span>Vegetation</span><span style="font-family:\'JetBrains Mono\',monospace;color:var(--text2)">' + vn(lcVeg) + '</span></div><div style="width:100%;height:4px;background:var(--border);border-radius:99px;overflow:hidden"><div style="width:' + pctVeg + '%;height:100%;background:var(--accent);border-radius:99px"></div></div></div>' +
          '<div><div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:2px"><span>Built Up</span><span style="font-family:\'JetBrains Mono\',monospace;color:var(--text2)">' + vn(lcBuilt) + '</span></div><div style="width:100%;height:4px;background:var(--border);border-radius:99px;overflow:hidden"><div style="width:' + pctBuilt + '%;height:100%;background:var(--danger);border-radius:99px"></div></div></div>' +
        '</div>' +
      '</div>' +

      '<!-- Tile 4: Source Classification -->' +
      '<div class="data-card p-5 flex flex-col gap-3 relative overflow-hidden" style="min-width:220px">' +
        '<div class="font-label-caps text-label-caps text-on-surface-variant uppercase tracking-wider" style="font-size:10px;color:var(--text2);font-weight:700">Source Classification</div>' +
        '<div class="thermal-breakdown" role="img" aria-label="Source class breakdown">' +
          breakdownBars(thermal.classification_counts, classLabels, 'Classification counts unavailable') +
        '</div>' +
      '</div>' +

      '<!-- Tile 5: Persistence and alerts -->' +
      '<div class="data-card p-5 flex flex-col gap-3 relative overflow-hidden" style="min-width:220px">' +
        '<div class="font-label-caps text-label-caps text-on-surface-variant uppercase tracking-wider" style="font-size:10px;color:var(--text2);font-weight:700">Persistence & Alerts</div>' +
        '<div class="thermal-breakdown" role="img" aria-label="Persistence level breakdown">' +
          breakdownBars(thermal.persistence_level_counts, persistenceLabels, 'Persistence counts unavailable') +
        '</div>' +
        '<div class="thermal-summary-row"><span>Emergency candidates</span><b>' + vn(thermal.emergency_candidate_count != null ? thermal.emergency_candidate_count : 0) + '</b></div>' +
        '<div class="thermal-summary-row"><span>Longest active</span><b>' + v(thermal.longest_duration_days, 1, 'days') + '</b></div>' +
      '</div>' +

      '<!-- Tile 6: Weather System Health -->' +
      '<div class="data-card" style="min-width:210px">' +
        '<div class="dash-card-title">System Weather</div>' +
        '<div id="fcast-weather" class="fcast-weather">' +
          (wf.length ? weatherSummary(wf[0]) : '<span style="opacity:.4;font-size:10px">Loading…</span>') +
        '</div>' +
      '</div>' +

      '<!-- Tile 7: Wind Forecast -->' +
      '<div class="data-card dash-card-wide">' +
        '<div class="dash-card-title">Wind Forecast +12h</div>' +
        '<div id="dash-wind-sparkline">' + windSparkline(wf) + '</div>' +
        '<div id="dash-wind-labels" class="forecast-labels">' +
          wf.filter((_, i) => i % 3 === 0).map(f => {
            const spd = (f.wind_speed_kmh || f.speed_kmh);
            return '<span>+' + f.hour + 'h<br><b style="font-family:\'JetBrains Mono\',monospace">' + (spd != null ? spd.toFixed(0) : '—') + '</b></span>';
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
        (pop.data_available === false
          ? '<div class="dash-empty">Population exposure unavailable<br><small>' +
              text(pop.reason || 'No population source configured') + '</small></div>'
          : '<div class="pop-affected">' +
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
            '</div>') +
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

  window.Dashboard = { renderDashboard, renderDashboardPending, clearDashboard, updateWeather, updateHud };
})();
