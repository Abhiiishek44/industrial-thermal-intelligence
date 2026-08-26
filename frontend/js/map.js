/**
 * map.js — Home map (FIRMS + events) and Event map (layers)
 * Exposes: window.HomeMap, window.EventMap
 */
(function() {

  // Yesterday's date for GIBS tiles (today may not be processed yet)
  function gibs_date() {
    const d = new Date();
    d.setDate(d.getDate() - 1);
    return d.toISOString().split('T')[0];
  }

  const TILES = {
    dark:      { url: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
                 attr: '&copy; OSM &copy; CARTO' },
    light:     { url: 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
                 attr: '&copy; OSM &copy; CARTO' },
    satellite: { url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
                 attr: '&copy; Esri' },
    topo:      { url: 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
                 attr: '&copy; OpenTopoMap' },
    osm:       { url: 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
                 attr: '&copy; OpenStreetMap' },
  };

  const FIRMS_TILES = {
    url:  'https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/VIIRS_SNPP_Thermal_Anomalies_375m_Day/default/' + gibs_date() + '/GoogleMapsCompatible/{z}/{y}/{x}.jpg',
    attr: 'NASA GIBS · VIIRS SNPP Thermal Anomalies 24h',
    maxZoom: 8,   // GIBS layer only has tiles up to zoom 8
    opacity: 0.8,
  };

  // Road colours for dark basemaps (Dark, Satellite, Sentinel-2)
  const ROAD_COLORS_DARK = {
    burning:     '#ff0066',
    burned:      '#cc0000',
    at_risk_3h:  '#ff3333',
    at_risk_6h:  '#ff8c00',
    at_risk_12h: '#ffd700',
    clear:       '#44dd44',
  };

  // Road colours for light basemaps (Light, OSM, Topo) — darker + higher contrast
  const ROAD_COLORS_LIGHT = {
    burning:     '#cc0055',
    burned:      '#7a0000',
    at_risk_3h:  '#b30000',
    at_risk_6h:  '#a04400',
    at_risk_12h: '#7a6200',
    clear:       '#1a6b1a',
  };

  const _LIGHT_BASES = new Set(['Light', 'OSM', 'Topo']);

  function roadColors(darkBase) {
    return darkBase ? ROAD_COLORS_DARK : ROAD_COLORS_LIGHT;
  }

  const RISK_LEVEL_STYLES = {
    high:   { color: '#ff2222', fillColor: '#ff2222', fillOpacity: 0.35, weight: 1.8, opacity: 0.95 },
    medium: { color: '#ff8c00', fillColor: '#ff8c00', fillOpacity: 0.25, weight: 1.4, opacity: 0.85 },
    low:    { color: '#ffd700', fillColor: '#ffd700', fillOpacity: 0.16, weight: 1.0, opacity: 0.70 },
  };

  // Wind-driven prediction — blue shades (deep → mid → light)
  const RISK_WIND_STYLES = {
    high:   { color: '#0d47a1', fillColor: '#1565c0', fillOpacity: 0.38, weight: 1.8, opacity: 0.95 },
    medium: { color: '#1565c0', fillColor: '#1e88e5', fillOpacity: 0.26, weight: 1.4, opacity: 0.85 },
    low:    { color: '#1976d2', fillColor: '#90caf9', fillOpacity: 0.16, weight: 1.0, opacity: 0.70 },
  };

  // ── HOME MAP ────────────────────────────────────────────────────────────────

  class HomeMap {
    constructor(containerId, onEventClick) {
      this.onEventClick = onEventClick;
      this._dark = true;

      this.map = L.map(containerId, { zoomControl: false, attributionControl: true });
      L.control.zoom({ position: 'bottomright' }).addTo(this.map);

      this._baseTile = L.tileLayer(TILES.dark.url, { attribution: TILES.dark.attr, maxZoom: 18 }).addTo(this.map);

      // GIBS VIIRS thermal anomalies (real-time FIRMS-like)
      this._firmsTile = L.tileLayer(FIRMS_TILES.url, {
        attribution: FIRMS_TILES.attr,
        opacity: FIRMS_TILES.opacity,
        maxNativeZoom: FIRMS_TILES.maxZoom,
        maxZoom: 18,
        errorTileUrl: '',   // suppress 404 tile errors silently
      }).addTo(this.map);

      this._eventLayer = L.layerGroup().addTo(this.map);
      this._firmsLayer = L.layerGroup().addTo(this.map);
      // Dedicated canvas renderer for FIRMS hotspots — draws all points on one
      // <canvas> instead of one SVG node per point. Survives the ~100k Canada-wide
      // hotspots during fire season that would otherwise freeze the tab.
      this._firmsCanvas = L.canvas({ padding: 0.5 });
      this.map.setView([56.5, -111.5], 5);
    }

    setTheme(dark) {
      this._dark = dark;
      const t = dark ? TILES.dark : TILES.light;
      this._baseTile.setUrl(t.url);
    }

    renderFirms(fc) {
      this._firmsLayer.clearLayers();
      if (!fc || !fc.features || !fc.features.length) return;
      const layer = this._firmsLayer;
      const renderer = this._firmsCanvas;
      fc.features.forEach(function(f) {
        const [lon, lat] = f.geometry.coordinates;
        const p = f.properties;
        const conf = (p.confidence || '').toLowerCase();
        const color = conf === 'h' || conf === 'high' ? '#ff2200'
                    : conf === 'n' || conf === 'nominal' ? '#ff6600'
                    : '#ffcc00';
        const r = p.frp ? Math.min(8, Math.max(3, Math.sqrt(p.frp) * 0.7)) : 4;
        const m = L.circleMarker([lat, lon], {
          radius: r, color: color, weight: 1,
          fillColor: color, fillOpacity: 0.75,
          renderer: renderer,
        });
        const tip = '<div style="font-size:11px;line-height:1.5">' +
          (p.acq_date ? '<b>' + p.acq_date + ' ' + (p.acq_time || '') + '</b><br>' : '') +
          (p.frp != null ? 'FRP: ' + p.frp + ' MW<br>' : '') +
          'Conf: ' + (p.confidence || '—') + '</div>';
        m.bindTooltip(tip, { sticky: true });
        layer.addLayer(m);
      });
    }

    renderEvents(events) {
      this._eventLayer.clearLayers();
      const self = this;
      events.forEach(ev => {
        if (!ev.bbox) return;
        const [minLon, minLat, maxLon, maxLat] = ev.bbox;
        const rect = L.rectangle([[minLat, minLon], [maxLat, maxLon]], {
          color: '#ff6b35', weight: 2.5,
          fillColor: '#ff6b35', fillOpacity: 0.06,
          dashArray: '7 4',
          className: 'event-rect',
          interactive: true,
          bubblingMouseEvents: false,
        });
        rect.bindTooltip(
          '<div style="font-weight:700;font-size:13px">' + ev.name + '</div>' +
          '<div style="font-size:11px;opacity:.7">' + ev.year + ' · Click to open</div>',
          { sticky: true, className: 'event-tooltip' }
        );
        rect.on('click', () => self.onEventClick(ev));
        // pulsing marker at bbox center
        const clat = (minLat + maxLat) / 2;
        const clon = (minLon + maxLon) / 2;
        const marker = L.circleMarker([clat, clon], {
          radius: 12, color: '#ff6b35', weight: 2.5,
          fillColor: '#ff4500', fillOpacity: 0.7,
        });
        marker.bindTooltip(
          '<div style="font-weight:700;font-size:13px">' + ev.name + '</div>' +
          '<div style="font-size:11px;opacity:.7">' + ev.year + ' · Click to open</div>',
          { sticky: true }
        );
        marker.on('click', () => self.onEventClick(ev));
        self._eventLayer.addLayer(rect);
        self._eventLayer.addLayer(marker);
      });
    }
  }

  // ── EVENT MAP ───────────────────────────────────────────────────────────────

  class EventMap {
    constructor(containerId) {
      this._dark = true;
      this.map = L.map(containerId, { zoomControl: false });
      L.control.zoom({ position: 'bottomright' }).addTo(this.map);

      // Custom pane for Sentinel-2: sits above basemap tiles (z=200) but below vectors (z=400)
      this.map.createPane('sentinelPane');
      this.map.getPane('sentinelPane').style.zIndex = 250;

      this._baseTiles = {
        'Dark':      L.tileLayer(TILES.dark.url,      { attribution: TILES.dark.attr,      maxZoom: 18 }),
        'Light':     L.tileLayer(TILES.light.url,     { attribution: TILES.light.attr,     maxZoom: 18 }),
        'Satellite': L.tileLayer(TILES.satellite.url, { attribution: TILES.satellite.attr, maxZoom: 18 }),
        'Topo':      L.tileLayer(TILES.topo.url,      { attribution: TILES.topo.attr,      maxZoom: 17 }),
        'OSM':       L.tileLayer(TILES.osm.url,       { attribution: TILES.osm.attr,       maxZoom: 19 }),
      };
      this._baseTile = this._baseTiles['Dark'];
      this._baseTile.addTo(this.map);
      this.map.setView([56.5, -111.5], 8);

      this._layers = {
        risk3h:    L.layerGroup().addTo(this.map),
        risk6h:    L.layerGroup().addTo(this.map),
        risk12h:   L.layerGroup().addTo(this.map),
        perimeter: L.layerGroup().addTo(this.map),
        roads:     L.layerGroup().addTo(this.map),
        hotspots:  L.layerGroup().addTo(this.map),
      };
      // Wind-driven risk zone layers (separate from ML layers)
      this._windLayers = {
        wRisk3h:  L.layerGroup().addTo(this.map),
        wRisk6h:  L.layerGroup().addTo(this.map),
        wRisk12h: L.layerGroup().addTo(this.map),
      };
      // Actual (ground-truth) perimeter — one layer group per horizon
      this._actualPerimLayers = {
        '+0h':  L.layerGroup().addTo(this.map),
        '+3h':  L.layerGroup().addTo(this.map),
        '+6h':  L.layerGroup().addTo(this.map),
        '+12h': L.layerGroup().addTo(this.map),
      };

      this._velocityLayer = null;
      this._windFieldHours = [];
      this._windFieldGroup = L.layerGroup().addTo(this.map);

      // MODIS Terra true-color daily mosaic (NASA GIBS), 250 m/px.
      // Pinned to sentinelPane (z=250) so it sits above the basemap but below
      // vector overlays. Daily coverage with no gaps — great for replay since
      // smoke plumes are visible across the AOI on every slot date.
      // (Sentinel-2 isn't available as a GIBS tile layer; for higher detail
      // switch the basemap to "Satellite" — Esri imagery, not date-aware.)
      this._satelliteTile = L.tileLayer('', {
        attribution: 'Imagery © NASA EOSDIS GIBS',
        maxNativeZoom: 9,
        maxZoom: 18,
        opacity: 0.9,
        pane: 'sentinelPane',
      });
      this._satelliteTile.addTo(this.map);

      // Basemap selector (radio buttons)
      this._basemapControl = L.control.layers(this._baseTiles, {}, {
        position: 'topright', collapsed: true,
      }).addTo(this.map);
      this._basemapControl.getContainer().classList.add('basemap-control');

      this._darkBase     = true;   // tracks whether current basemap is dark
      this._roadsGeoJSON = null;   // cached for re-render on basemap change

      this.map.on('baselayerchange', (e) => {
        this._darkBase = !_LIGHT_BASES.has(e.name);
        if (this._roadsGeoJSON) this.renderRoads(this._roadsGeoJSON);
      });

      // Overlay layers (checkboxes)
      this._overlayControl = L.control.layers({}, {
        'MODIS Terra (250 m)': this._satelliteTile,
        'Perimeter':           this._layers.perimeter,
        'Roads':               this._layers.roads,
        'Hotspots':            this._layers.hotspots,
        'Wind Field':          this._windFieldGroup,
      }, { position: 'topright', collapsed: true }).addTo(this.map);
    }

    addOverlay(label, layer) {
      if (this._overlayControl) this._overlayControl.addOverlay(layer, label);
      layer.addTo(this.map);
    }

    removeOverlay(layer) {
      if (this._overlayControl) this._overlayControl.removeLayer(layer);
      layer.remove();
    }

    setTheme(dark) {
      this._dark = dark;
      const t = dark ? TILES.dark : TILES.light;
      this._baseTile.setUrl(t.url);
    }

    setSatelliteDate(dateStr) {
      if (!dateStr) return;
      const url = 'https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/' +
        'MODIS_Terra_CorrectedReflectance_TrueColor/default/' + dateStr +
        '/GoogleMapsCompatible_Level9/{z}/{y}/{x}.jpg';
      this._satelliteTile.setUrl(url);
      if (this.map.hasLayer(this._satelliteTile)) this._satelliteTile.redraw();
    }

    fitToBbox(bbox) {
      const [minLon, minLat, maxLon, maxLat] = bbox;
      this.map.fitBounds([[minLat, minLon], [maxLat, maxLon]], { padding: [30, 30] });
    }

    fitToAoi(geojson) {
      try {
        const bounds = L.geoJSON(geojson).getBounds();
        if (bounds.isValid()) {
          const zoom = this.map.getBoundsZoom(bounds) + 1;
          this.map.setView(bounds.getCenter(), zoom);
        }
      } catch(e) {}
    }

    panToGeojson(geojson) {
      try {
        const bounds = L.geoJSON(geojson).getBounds();
        if (bounds.isValid()) this.map.panTo(bounds.getCenter());
      } catch(e) {}
    }

    clearLayers() {
      Object.values(this._layers).forEach(lg => lg.clearLayers());
      Object.values(this._windLayers).forEach(lg => lg.clearLayers());
      Object.values(this._actualPerimLayers).forEach(lg => lg.clearLayers());
    }

    setRiskVisible(horizon, visible) {
      const key = 'risk' + horizon.replace('+', '');
      const lg = this._layers[key];
      if (!lg) return;
      if (visible) this.map.addLayer(lg);
      else         this.map.removeLayer(lg);
    }

    renderPerimeter(geojson) {
      this._layers.perimeter.clearLayers();
      if (!geojson?.features?.length) return;
      L.geoJSON(geojson, {
        style: { color: '#ff4444', weight: 2.5, fillColor: '#cc2200', fillOpacity: 0.40 },
        onEachFeature(f, layer) {
          const p = f.properties || {};
          layer.bindPopup('<b>Fire Perimeter</b><br>Area: ' +
            (p.area_km2 != null ? p.area_km2.toFixed(1) + ' km²' : 'N/A'));
        },
      }).addTo(this._layers.perimeter);
    }

    renderHotspots(geojson) {
      this._layers.hotspots.clearLayers();
      if (!geojson?.features?.length) return;
      L.geoJSON(geojson, {
        pointToLayer(f, latlng) {
          const frp = f.properties?.frp || 0;
          const r = Math.max(4, Math.min(13, 4 + frp / 70));
          return L.circleMarker(latlng, {
            radius: r, color: '#ff6600', fillColor: '#ff2200', fillOpacity: 0.85, weight: 1,
          });
        },
        onEachFeature(f, layer) {
          const p = f.properties || {};
          layer.bindPopup('<b>Hotspot</b><br>FRP: ' +
            (p.frp != null ? p.frp.toFixed(1) + ' MW' : 'N/A') +
            '<br>Confidence: ' + (p.confidence || 'N/A'));
        },
      }).addTo(this._layers.hotspots);
    }

    renderRiskZones(geojson) {
      this._layers.risk3h.clearLayers();
      this._layers.risk6h.clearLayers();
      this._layers.risk12h.clearLayers();
      if (!geojson?.features?.length) return;
      const byH = { '12h': [], '6h': [], '3h': [] };
      geojson.features.forEach(f => { const h = f.properties?.horizon; if (byH[h]) byH[h].push(f); });

      // Add features to layer groups; start all hidden — forecast slider controls visibility
      ['12h', '6h', '3h'].forEach(h => {
        if (!byH[h].length) return;
        const layerKey = 'risk' + h;
        L.geoJSON({ type: 'FeatureCollection', features: byH[h] }, {
          style(f) { return RISK_LEVEL_STYLES[f.properties?.risk_level] || RISK_LEVEL_STYLES.low; },
          onEachFeature(f, layer) {
            const p = f.properties || {};
            layer.bindPopup('<b>Risk Zone +' + p.horizon + '</b><br>' +
              'Level: ' + (p.risk_level || 'N/A') + '<br>' +
              'P(spread) max: ' + (p.prob_max != null ? (p.prob_max * 100).toFixed(1) + '%' : 'N/A'));
          },
        }).addTo(this._layers[layerKey]);
        this.map.removeLayer(this._layers[layerKey]);   // hidden until slider activates it
      });

    }

    loadWindField(hoursData) {
      if (this._velocityLayer) {
        this._velocityLayer.remove();
        this._velocityLayer = null;
      }
      this._windFieldGroup.clearLayers();
      this._windFieldHours = hoursData || [];
    }

    setWeatherGridHour(h) {
      clearTimeout(this._windDebounce);
      this._windDebounce = setTimeout(() => {
        this._applyWindHour(h);
      }, 150);
    }

    _applyWindHour(h) {
      if (!this._windFieldHours.length || typeof L.velocityLayer === 'undefined') return;

      const entry = this._windFieldHours.find(function(d) { return d.hour === h; })
                 || this._windFieldHours[0];
      if (!entry) return;

      if (this._velocityLayer) {
        this._velocityLayer.remove();
        this._velocityLayer = null;
      }
      this._windFieldGroup.clearLayers();
      this._velocityLayer = L.velocityLayer({
        displayValues:      false,
        displayOptions:     { velocityType: 'Wind', position: 'bottomleft', emptyString: '', angleConvention: 'bearingCW', speedUnit: 'km/h' },
        data:               entry.data,
        maxVelocity:        25,
        colorScale:         ['#aaddff', '#55bbff', '#ff8c00', '#ff3300'],
        lineWidth:          1.5,
        particleAge:        60,
        particleMultiplier: 0.0015,
      });
      this._windFieldGroup.addLayer(this._velocityLayer);
    }

    // ── Wind-driven risk zones ────────────────────────────────────────────────

    renderRiskZonesWind(geojson) {
      Object.values(this._windLayers).forEach(lg => lg.clearLayers());
      if (!geojson?.features?.length) return;
      const byH = { '12h': [], '6h': [], '3h': [] };
      geojson.features.forEach(function(f) {
        const h = f.properties?.horizon;
        if (byH[h]) byH[h].push(f);
      });
      ['12h', '6h', '3h'].forEach(function(h) {
        if (!byH[h].length) return;
        const key = 'wRisk' + h;
        L.geoJSON({ type: 'FeatureCollection', features: byH[h] }, {
          style(f) { return RISK_WIND_STYLES[f.properties?.risk_level] || RISK_WIND_STYLES.low; },
          onEachFeature(f, layer) {
            const p = f.properties || {};
            layer.bindPopup('<b>Wind-driven Risk +' + p.horizon + '</b><br>' +
              'Level: ' + (p.risk_level || 'N/A') + '<br>' +
              'P(spread) max: ' + (p.prob_max != null ? (p.prob_max * 100).toFixed(1) + '%' : 'N/A'));
          },
        }).addTo(this._windLayers[key]);
        this.map.removeLayer(this._windLayers[key]);   // hidden until slider activates it
      }, this);
    }

    setWindRiskVisible(horizon, visible) {
      const key = 'wRisk' + horizon.replace('+', '');
      const lg = this._windLayers[key];
      if (!lg) return;
      if (visible) this.map.addLayer(lg);
      else         this.map.removeLayer(lg);
    }

    // ── Actual perimeter (DEV ground-truth overlay) ───────────────────────────

    renderActualPerimeter(geojson) {
      Object.values(this._actualPerimLayers).forEach(lg => lg.clearLayers());
      if (!geojson?.features?.length) return;
      const horizonColor = { '+0h': '#e0e0e0', '+3h': '#a0c4ff', '+6h': '#74b9ff', '+12h': '#0984e3' };
      geojson.features.forEach(f => {
        const p   = f.properties || {};
        const key = p.horizon || '+0h';
        const lg  = this._actualPerimLayers[key];
        if (!lg) return;
        const c = horizonColor[key] || '#e0e0e0';
        const layer = L.geoJSON(f, {
          style: { color: c, weight: 2, fillColor: c, fillOpacity: 0.10, dashArray: '8 4', opacity: 0.85 },
        });
        const date = p.date || p.maxdate || p.lastdate || '';
        layer.bindPopup(
          '<b>Actual Perimeter ' + key + '</b>' +
          (date ? '<br><span style="font-size:11px;opacity:.7">' + date + '</span>' : '') +
          '<br><span style="font-size:10px;opacity:.6">Ground truth</span>'
        );
        layer.addTo(lg);
        this.map.removeLayer(lg);   // hidden until slider activates it
      });
    }

    setActualPerimVisible(horizon, visible) {
      const lg = this._actualPerimLayers[horizon];
      if (!lg) return;
      if (visible) this.map.addLayer(lg);
      else         this.map.removeLayer(lg);
    }

    clearActualPerimeter() {
      Object.values(this._actualPerimLayers).forEach(lg => {
        lg.clearLayers();
        this.map.removeLayer(lg);
      });
    }

    // ── Roads ─────────────────────────────────────────────────────────────────

    renderRoads(geojson) {
      this._roadsGeoJSON = geojson;
      this._layers.roads.clearLayers();
      if (!geojson?.features?.length) return;
      const colors = roadColors(this._darkBase);
      L.geoJSON(geojson, {
        style(f) {
          const s = f.properties?.status || 'clear';
          const w = s === 'clear' ? 2 : s === 'burning' ? 4.5 : 3.5;
          const o = s === 'clear' ? 0.5 : 0.9;
          return { color: colors[s] || '#888', weight: w, opacity: o };
        },
        onEachFeature(f, layer) {
          const p = f.properties || {};
          const statusLabel = p.status || 'N/A';
          let html = '<b>' + (p.road_name || 'Road') + '</b><br>Status: <b>' + statusLabel + '</b>';
          const sections = Array.isArray(p.sections) ? p.sections
                         : (typeof p.sections === 'string' ? JSON.parse(p.sections) : []);
          if (sections.length) {
            html += '<br>Affected sections:<ul style="margin:3px 0 0 12px;padding:0">' +
              sections.map(s => '<li>' + s.from + ' → ' + s.to + '</li>').join('') + '</ul>';
          }
          layer.bindPopup(html);
        },
      }).addTo(this._layers.roads);
    }
  }

  window.HomeMap  = HomeMap;
  window.EventMap = EventMap;
})();
