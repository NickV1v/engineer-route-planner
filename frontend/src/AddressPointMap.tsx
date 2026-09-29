import { useEffect, useRef, useState } from 'react';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import type { GeoPoint } from './types';
import { mountBasemap } from './basemap';
import { compactAttribution } from './mapAttribution';
import { iconMarkup } from './icons';

export function AddressPointMap({
  point,
  center,
  onPick,
  disabled = false,
}: {
  point: GeoPoint | null;
  center?: GeoPoint | null;
  onPick?: (lat: number, lon: number) => void;
  disabled?: boolean;
}) {
  const host = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const marker = useRef<L.Marker | null>(null);
  const pick = useRef(onPick);
  const locked = useRef(disabled);
  pick.current = onPick;
  locked.current = disabled;
  const initial = useRef(point ?? center);
  const [tileError, setTileError] = useState(false);
  useEffect(() => {
    if (!host.current) return;
    const start = initial.current;
    const instance = L.map(host.current, {
      scrollWheelZoom: true,
      minZoom: 1,
      maxZoom: 19,
    }).setView(start ? [start.lat, start.lon] : [55.75, 37.62], start ? 16 : 10);
    map.current = instance;
    instance.on('click', (event: L.LeafletMouseEvent) => {
      if (!locked.current) pick.current?.(event.latlng.lat, event.latlng.lng);
    });
    const restoreAttribution = compactAttribution(instance);
    const unmountBasemap = mountBasemap(instance, setTileError);
    const observer = new ResizeObserver(() => {
      if (host.current?.clientWidth && host.current.clientHeight)
        instance.invalidateSize({ pan: true, animate: false });
    });
    observer.observe(host.current);
    return () => {
      observer.disconnect();
      unmountBasemap();
      restoreAttribution();
      instance.remove();
      map.current = null;
      marker.current = null;
    };
  }, []);
  useEffect(() => {
    marker.current?.remove();
    marker.current = null;
    const instance = map.current;
    if (!instance || !point) return;
    const text = document.createElement('span');
    text.textContent = point.label;
    const pin = L.marker([point.lat, point.lon], {
      icon: L.divIcon({
        className: 'map-stop address-point-pin',
        html: `<span class="location-pin" style="--pin-color:#2876dc">${iconMarkup('check')}</span>`,
        iconSize: [24, 30],
        iconAnchor: [12, 30],
      }),
      keyboard: false,
      draggable: Boolean(pick.current) && !locked.current,
    })
      .addTo(instance)
      .bindTooltip(text);
    pin.on('dragend', () => {
      const position = pin.getLatLng();
      if (!locked.current) pick.current?.(position.lat, position.lng);
    });
    marker.current = pin;
    instance.setView([point.lat, point.lon], Math.max(17, instance.getZoom()), { animate: false });
  }, [point?.lat, point?.lon, point?.label]);
  useEffect(() => {
    if (disabled || !onPick) marker.current?.dragging?.disable();
    else marker.current?.dragging?.enable();
  }, [disabled, onPick]);
  return (
    <>
      <div
        ref={host}
        className={`address-point-map${onPick && !disabled ? ' address-point-picker' : ''}`}
        aria-label={onPick ? 'Карта уточнения адреса' : 'Точка выбранного адреса'}
      />
      {point && (
        <p className="address-point-coordinates">
          {point.lat.toFixed(6)}, {point.lon.toFixed(6)}
        </p>
      )}
      {tileError && (
        <p className="map-error">Подложка недоступна. Выберите подсказку или введите координаты.</p>
      )}
    </>
  );
}
