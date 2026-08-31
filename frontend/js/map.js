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

  const THERMAL_CLASS_COLORS = {
    industrial_fire: '#dc2626',
    gas_flare: '#a855f7',
    agricultural_burning: '#eab308',
    mining_activity: '#8b5e3c',
    wildfire: '#16a34a',
    industrial_process_heat: '#f97316',
    unknown: '#6b7280',
  };

  function thermalClass(value) {
    return { industrial: 'industrial_process_heat', natural: 'wildfire' }[value] ||
      value || 'unknown';
  }

  function thermalClassLabel(value) {
    return thermalClass(value).replaceAll('_', ' ').replace(/\b\w/g, function(letter) {
      return letter.toUpperCase();
    });
  }

  // ── HOME MAP ────────────────────────────────────────────────────────────────

  class HomeMap {
    constructor(containerId, onEventClick) {
      this.onEventClick = onEventClick;
      this._dark = true;

      this.map = L.map(containerId, { zoomControl: false, attributionControl: true });
      L.control.zoom({ position: 'bottomright' }).addTo(this.map);

      this._baseTile = L.tileLayer(TILES.osm.url, { attribution: TILES.osm.attr, maxZoom: 19 }).addTo(this.map);

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
      this._overviewLayer = L.layerGroup().addTo(this.map);
      // Dedicated canvas renderer for FIRMS hotspots — draws all points on one
      // <canvas> instead of one SVG node per point. Survives the ~100k Canada-wide
      // hotspots during fire season that would otherwise freeze the tab.
      this._firmsCanvas = L.canvas({ padding: 0.5 });
      this._overviewCanvas = L.canvas({ padding: 0.5 });
      this.map.setView([22.5, 80.5], 5);
    }

    setTheme(dark) {
      this._dark = dark;
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
      const catalogBounds = [];
      events.forEach(ev => {
        if (!ev.bbox) return;
        const [minLon, minLat, maxLon, maxLat] = ev.bbox;
        const forest = ev.monitoring_focus === 'forest';
        const eventColor = forest ? '#2eaa58' : '#ff6b35';
        const focusLabel = forest ? 'Forest-fire monitoring' : 'Industrial thermal monitoring';
        catalogBounds.push([minLat, minLon], [maxLat, maxLon]);
        const rect = L.rectangle([[minLat, minLon], [maxLat, maxLon]], {
          color: eventColor, weight: 2.5,
          fillColor: eventColor, fillOpacity: 0.06,
          dashArray: '7 4',
          className: 'event-rect',
          interactive: true,
          bubblingMouseEvents: false,
        });
        rect.bindTooltip(
          '<div style="font-weight:700;font-size:13px">' + ev.name + '</div>' +
          '<div style="font-size:11px;opacity:.7">' + focusLabel + ' · Click to focus</div>',
          { sticky: true, className: 'event-tooltip' }
        );
        rect.on('click', () => self.onEventClick(ev));
        // pulsing marker at bbox center
        const clat = (minLat + maxLat) / 2;
        const clon = (minLon + maxLon) / 2;
        const marker = L.circleMarker([clat, clon], {
          radius: 10, color: eventColor, weight: 2.5,
          fillColor: eventColor, fillOpacity: 0.7,
        });
        marker.bindTooltip(
          '<div style="font-weight:700;font-size:13px">' + ev.name + '</div>' +
          '<div style="font-size:11px;opacity:.7">' + focusLabel + ' · Click to focus</div>',
          { sticky: true }
        );
        marker.on('click', () => self.onEventClick(ev));
        self._eventLayer.addLayer(rect);
        self._eventLayer.addLayer(marker);
      });
      if (catalogBounds.length) {
        this.map.fitBounds(catalogBounds, { padding: [28, 28], maxZoom: 6 });
      }
    }

    renderIndiaOverview(geojson) {
      this._overviewLayer.clearLayers();
      if (!geojson || !Array.isArray(geojson.features)) return;
      const renderer = this._overviewCanvas;
      L.geoJSON(geojson, {
        pointToLayer(feature, latlng) {
          const properties = feature.properties || {};
          const sourceClass = thermalClass(properties.source_class);
          const detections = Number(properties.detection_count || 1);
          const color = THERMAL_CLASS_COLORS[sourceClass] || THERMAL_CLASS_COLORS.unknown;
          return L.circleMarker(latlng, {
            radius: Math.max(4, Math.min(11, 3 + Math.sqrt(detections))),
            color: color,
            fillColor: color,
            fillOpacity: 0.78,
            weight: 1,
            renderer: renderer,
          });
        },
        onEachFeature(feature, layer) {
          const p = feature.properties || {};
          layer.bindPopup(
            '<b>' + (p.region_name || 'India thermal source') + '</b><br>' +
            (p.state ? p.state + '<br>' : '') +
            'Class: <b>' + thermalClassLabel(p.source_class) + '</b><br>' +
            'Detections: ' + (p.detection_count || 0) + '<br>' +
            'Active days: ' + (p.unique_active_days || 0) + '<br>' +
            'Maximum FRP: ' + (p.max_frp != null ? Number(p.max_frp).toFixed(1) + ' MW' : 'N/A')
          );
        },
      }).addTo(this._overviewLayer);
    }

    focusEvent(ev) {
      if (!ev || !ev.bbox) return;
      const [minLon, minLat, maxLon, maxLat] = ev.bbox;
      this.map.fitBounds([[minLat, minLon], [maxLat, maxLon]], {
        padding: [45, 45],
        maxZoom: 9,
      });
    }

    fitIndia(events) {
      const bounds = [];
      (events || []).forEach(function(ev) {
        if (!ev.bbox) return;
        bounds.push([ev.bbox[1], ev.bbox[0]], [ev.bbox[3], ev.bbox[2]]);
      });
      if (bounds.length) this.map.fitBounds(bounds, { padding: [28, 28], maxZoom: 6 });
    }
  }

  // ── EVENT MAP ───────────────────────────────────────────────────────────────

  class EventMap {
    constructor(containerId) {
      this._dark = true;
      this._monitoringFocus = 'industrial';
      this.map = L.map(containerId, { zoomControl: false });
      L.control.zoom({ position: 'bottomright' }).addTo(this.map);

      // Custom pane for Sentinel-2: sits above basemap tiles (z=200) but below vectors (z=400)
      this.map.createPane('sentinelPane');
      this.map.getPane('sentinelPane').style.zIndex = 250;

      this._baseTiles = {
        'OSM':       L.tileLayer(TILES.osm.url,       { attribution: TILES.osm.attr,       maxZoom: 19 }),
        'Satellite': L.tileLayer(TILES.satellite.url, { attribution: TILES.satellite.attr, maxZoom: 18 }),
        'Topo':      L.tileLayer(TILES.topo.url,      { attribution: TILES.topo.attr,      maxZoom: 17 }),
      };
      this._baseTile = this._baseTiles['OSM'];
      this._baseTile.addTo(this.map);
      this.map.setView([22.5, 80.5], 6);

      this._layers = {
        risk3h:    L.layerGroup().addTo(this.map),
        risk6h:    L.layerGroup().addTo(this.map),
        risk12h:   L.layerGroup().addTo(this.map),
        perimeter: L.layerGroup().addTo(this.map),
        roads:     L.layerGroup().addTo(this.map),
        hotspots:  L.layerGroup().addTo(this.map),
      };
      // Forest fire-season windows can contain thousands of FIRMS points.
      // Canvas avoids creating one SVG DOM node per detection.
      this._hotspotCanvas = L.canvas({ padding: 0.5 });
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

      // Basemap selector (radio buttons)
      this._basemapControl = L.control.layers(this._baseTiles, {}, {
        position: 'topright', collapsed: true,
      }).addTo(this.map);
      this._basemapControl.getContainer().classList.add('basemap-control');

      this._darkBase     = false;  // OSM is the default light basemap
      this._roadsGeoJSON = null;   // cached for re-render on basemap change

      this.map.on('baselayerchange', (e) => {
        this._darkBase = !_LIGHT_BASES.has(e.name);
        if (this._roadsGeoJSON) this.renderRoads(this._roadsGeoJSON);
      });

      // Overlay layers (checkboxes)
      this._overlayControl = L.control.layers({}, {
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
    }

    setMonitoringFocus(focus) {
      this._monitoringFocus = focus === 'forest' ? 'forest' : 'industrial';
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
      const renderer = this._hotspotCanvas;
      const forest = this._monitoringFocus === 'forest';
      const strokeColor = forest ? '#6d28d9' : '#ff6600';
      const fillColor = forest ? '#a855f7' : '#ff2200';
      L.geoJSON(geojson, {
        pointToLayer(f, latlng) {
          const frp = f.properties?.frp || 0;
          const r = Math.max(4, Math.min(13, 4 + frp / 70));
          return L.circleMarker(latlng, {
            radius: r, color: strokeColor, fillColor: fillColor,
            fillOpacity: 0.82, weight: forest ? 1.8 : 1,
            renderer: renderer,
          });
        },
        onEachFeature(f, layer) {
          const p = f.properties || {};
          layer.bindPopup('<b>' + (forest ? 'Forest-area thermal hotspot' : 'Industrial thermal detection') + '</b><br>FRP: ' +
            (p.frp != null ? p.frp.toFixed(1) + ' MW' : 'N/A') +
            '<br>Confidence: ' + (p.confidence || 'N/A'));
        },
      }).addTo(this._layers.hotspots);
    }

    renderPersistentSources(geojson) {
      this._layers.hotspots.clearLayers();
      if (!geojson?.features?.length) return;
      const colors = this._monitoringFocus === 'forest'
        ? { HIGH: '#6d28d9', MEDIUM: '#a855f7', LOW: '#d8b4fe' }
        : { HIGH: '#ff5500', MEDIUM: '#ff9900', LOW: '#ffd166' };
      const renderer = this._hotspotCanvas;
      L.geoJSON(geojson, {
        pointToLayer(f, latlng) {
          const p = f.properties || {};
          const count = Number(p.detection_count || 1);
          const color = colors[p.persistence_level] || '#999999';
          return L.circleMarker(latlng, {
            radius: Math.max(7, Math.min(18, 6 + Math.sqrt(count) * 1.5)),
            color: color,
            fillColor: color,
            fillOpacity: 0.72,
            weight: 2,
            renderer: renderer,
          });
        },
        onEachFeature(f, layer) {
          const p = f.properties || {};
          const distance = p.distance_to_nearest_industry_m != null
            ? Number(p.distance_to_nearest_industry_m).toFixed(0) + ' m'
            : 'N/A';
          layer.bindPopup(
            '<b>' + (p.cluster_id || 'Persistent source') + '</b><br>' +
            'Persistence: ' + (p.persistence_level || 'N/A') + '<br>' +
            'Detections: ' + (p.detection_count || 0) + '<br>' +
            'Active days: ' + (p.unique_active_days || 0) + '<br>' +
            'Mean / max FRP: ' +
              (p.mean_frp != null ? Number(p.mean_frp).toFixed(1) : 'N/A') + ' / ' +
              (p.max_frp != null ? Number(p.max_frp).toFixed(1) : 'N/A') + ' MW<br>' +
            'Night ratio: ' + (p.night_ratio != null ? Math.round(Number(p.night_ratio) * 100) + '%' : 'N/A') + '<br>' +
            'Nearest industry: ' + (p.nearest_industry_name || 'Unknown') + ' (' + distance + ')'
          );
        },
      }).addTo(this._layers.hotspots);
    }

    renderClassifiedSources(geojson) {
      this._layers.hotspots.clearLayers();
      if (!geojson?.features?.length) return;
      const renderer = this._hotspotCanvas;
      L.geoJSON(geojson, {
        pointToLayer(f, latlng) {
          const p = f.properties || {};
          const count = Number(p.detection_count || 1);
          const color = THERMAL_CLASS_COLORS[thermalClass(p.source_class)] ||
            THERMAL_CLASS_COLORS.unknown;
          return L.circleMarker(latlng, {
            radius: Math.max(8, Math.min(19, 7 + Math.sqrt(count) * 1.5)),
            color: color,
            fillColor: color,
            fillOpacity: 0.78,
            weight: 2.2,
            renderer: renderer,
          });
        },
        onEachFeature(f, layer) {
          const p = f.properties || {};
          const evidence = Array.isArray(p.classification_evidence)
            ? p.classification_evidence
            : [];
          layer.bindPopup(
            '<b>' + (p.cluster_id || 'Thermal source') + '</b><br>' +
            'Classification: <b>' + thermalClassLabel(p.source_class) + '</b><br>' +
            'State: ' + (p.operational_state || 'N/A').replaceAll('_', ' ') + '<br>' +
            'Alert: <b>' + (p.alert_level || 'N/A') + '</b><br>' +
            'Subtype: ' + (p.source_subtype || 'N/A').replaceAll('_', ' ') + '<br>' +
            'Confidence: ' + (p.classification_confidence != null
              ? Math.round(Number(p.classification_confidence) * 100) + '%'
              : 'N/A') + '<br>' +
            'Detections / active days: ' + (p.detection_count || 0) + ' / ' +
              (p.unique_active_days || 0) + '<br>' +
            '<b>Evidence</b><br>' + (evidence.length ? evidence.map(function(item) {
              return '• ' + item;
            }).join('<br>') : 'No decisive evidence')
          );
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
