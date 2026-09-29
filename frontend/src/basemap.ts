import L from 'leaflet';
import type { ExpressionSpecification, Map as VectorMap } from 'maplibre-gl';

type MapConfig = {
  style_url?: string;
  tile_url: string;
  attribution: string;
  attribution_url: string;
};

function localizeLabels(map: VectorMap) {
  const style = map.getStyle();
  for (const layer of style.layers) {
    if (layer.type !== 'symbol') continue;
    const field = JSON.stringify(layer.layout?.['text-field'] ?? '');
    if (!field.includes('name')) continue;
    map.setLayoutProperty(layer.id, 'text-field', [
      'coalesce',
      ['get', 'name:ru'],
      ['get', 'name'],
      ['get', 'name:latin'],
    ] satisfies ExpressionSpecification);
  }
  // OpenFreeMap uses the OpenMapTiles schema; keep house numbers separate from names.
  if (style.sources.openmaptiles && !map.getLayer('dispatch-house-numbers')) {
    map.addLayer({
      id: 'dispatch-house-numbers',
      type: 'symbol',
      source: 'openmaptiles',
      'source-layer': 'housenumber',
      minzoom: 16,
      layout: {
        'text-field': ['get', 'housenumber'],
        'text-font': ['Noto Sans Regular'],
        'text-size': 12,
      },
      paint: { 'text-color': '#44505a', 'text-halo-color': '#ffffff', 'text-halo-width': 1 },
    });
  }
}

/** The same high-DPI basemap is used for routes and address previews. */
export function mountBasemap(map: L.Map, onUnavailable: (value: boolean) => void) {
  const controller = new AbortController();
  let layer: L.Layer | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let vectorMap: VectorMap | undefined;
  let fallbackStarted = false;
  const active = () => !controller.signal.aborted;

  function removeLayer() {
    clearTimeout(timer);
    vectorMap?.off('error', onVectorError);
    if (layer) map.removeLayer(layer);
    layer = undefined;
    vectorMap = undefined;
  }

  let fallback: () => void = () => {};
  function onVectorError() {
    // Leave MapLibre's event dispatch before removing its canvas and workers.
    queueMicrotask(() => {
      if (active()) fallback();
    });
  }

  async function load() {
    const response = await fetch('/api/map-config', { signal: controller.signal });
    if (!response.ok) throw new Error('Map configuration unavailable');
    const config: MapConfig = await response.json();
    if (!active()) return;
    fallback = () => {
      if (!active() || fallbackStarted) return;
      fallbackStarted = true;
      removeLayer();
      if (!config.tile_url) {
        onUnavailable(true);
        return;
      }
      const link = document.createElement('a');
      link.textContent = config.attribution;
      if (/^https:\/\//.test(config.attribution_url)) link.href = config.attribution_url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      layer = L.tileLayer(config.tile_url, {
        // Leaflet subtracts one level in Retina mode; OSM serves up to z=19.
        maxZoom: L.Browser.retina ? 20 : 19,
        maxNativeZoom: L.Browser.retina ? 18 : 19,
        detectRetina: true,
        attribution: link.outerHTML,
        updateWhenIdle: true,
      })
        .on('tileerror', () => active() && onUnavailable(true))
        .on('tileload', () => active() && onUnavailable(false))
        .addTo(map);
    };
    if (!config.style_url) return fallback();
    const { maplibreGL } = await import('./vectorBasemap');
    if (!active()) return;
    const vectorOptions = { style: config.style_url, trackResize: false, updateInterval: 0 };
    const vector = maplibreGL(vectorOptions);
    const events = vector.getEvents?.() ?? {};
    // The adapter's default resize waits for a zoom-transition animation frame.
    // Resize and paint the canvas in the same Leaflet event as the SVG overlays.
    vector.getEvents = () => ({
      ...events,
      resize: () => {
        const size = vector.getSize();
        const padding = size.subtract(map.getSize()).divideBy(2);
        const container = vector.getContainer();
        container.style.width = `${size.x}px`;
        container.style.height = `${size.y}px`;
        L.DomUtil.setPosition(
          container,
          map.containerPointToLayerPoint([0, 0]).subtract(padding).round(),
        );
        const gl = vector.getMaplibreMap();
        gl.resize();
        const center = map.getCenter();
        gl.jumpTo({ center: [center.lng, center.lat], zoom: map.getZoom() - 1 });
        gl.redraw();
      },
    });
    layer = vector;
    vector.addTo(map);
    vectorMap = vector.getMaplibreMap();
    vectorMap.on('error', onVectorError);
    vectorMap.once('style.load', () => {
      if (active() && !fallbackStarted && vectorMap) localizeLabels(vectorMap);
    });
    vectorMap.once('load', () => {
      clearTimeout(timer);
      if (active() && !fallbackStarted) onUnavailable(false);
    });
    timer = setTimeout(onVectorError, 20000);
  }
  onUnavailable(false);
  load().catch(() => {
    if (!active()) return;
    if (layer && !vectorMap) {
      // A browser without WebGL can fail before the adapter finishes onAdd.
      const failed = layer as L.MaplibreGL;
      failed.onRemove = () => {
        failed.getContainer()?.remove();
        return failed;
      };
    }
    fallback();
    if (!layer) onUnavailable(true);
  });
  return () => {
    controller.abort();
    removeLayer();
  };
}
