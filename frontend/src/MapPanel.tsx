import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import L from 'leaflet';
import { engineerColor, Icon, iconMarkup } from './icons';
import 'leaflet/dist/leaflet.css';
import type { GeoLocation, GeoPoint, Plan, Run, Scenario, WorkRequest } from './types';
import { time, requestAddress, locationAddress, engineerName } from './format';
import { JourneyDetails, modeNames } from './JourneyDetails';
import { mountBasemap } from './basemap';
import { compactAttribution } from './mapAttribution';
import { journeySegments } from './journeySegments';
import type { RouteHighlight } from './routeHighlight';
import { bindJourneyHover, type HoverJourneyLeg } from './journeyHover';
import { journeyStyle } from './journeyStyle';

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok)
    throw new Error(
      typeof body.detail === 'string' ? body.detail : 'Не удалось сохранить координаты',
    );
  return body;
}

export function MapPanel({
  scenario,
  run,
  plan,
  onSelect,
  route,
  hiddenEngineers,
  showUnassigned,
  highlight,
  pinnedHighlight,
  onHighlight,
  onPin,
  onCatalog,
  catalogVersion,
  focusRequest,
  selectedRequestId,
  onOverview,
}: {
  scenario: Scenario;
  run: Run | null;
  plan?: Plan;
  onSelect: (r: WorkRequest) => void;
  route: string | null;
  hiddenEngineers: Set<string>;
  showUnassigned: boolean;
  highlight: RouteHighlight | null;
  pinnedHighlight: RouteHighlight | null;
  onHighlight: (highlight: RouteHighlight | null) => void;
  onPin: (highlight: RouteHighlight | null) => void;
  onCatalog: (catalog: Record<string, GeoLocation>) => void;
  catalogVersion: number;
  focusRequest: { requestId: string; token: number } | null;
  selectedRequestId?: string;
  onOverview: () => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const overlays = useRef<L.LayerGroup | null>(null);
  const routeLines = useRef<
    { line: L.Polyline; engineerId: string; requestId: string; weight: number; opacity: number }[]
  >([]);
  const routeMarkers = useRef<{ marker: L.Marker; jobs: WorkRequest[] }[]>([]);
  const itinerary = useRef<HTMLOListElement>(null);
  const [catalog, setCatalog] = useState<Record<string, GeoLocation>>({});
  useEffect(() => onCatalog(catalog), [catalog, onCatalog]);
  const fittedView = useRef('');
  const [error, setError] = useState('');
  const [tileError, setTileError] = useState(false);
  const [mapReady, setMapReady] = useState(false);
  const visibleBounds = useRef<L.LatLngTuple[]>([]);
  const handledFocus = useRef<object | null>(null);
  const source = run
    ? `/api/sessions/${run.session_id}/geography?version=${run.version}`
    : scenario.upload_id
      ? `/api/uploads/${scenario.upload_id}/geography`
      : scenario.id
        ? `/api/scenarios/${encodeURIComponent(scenario.id)}/geography`
        : '';
  const records = run ? (scenario.geography ?? {}) : catalog;
  const byId = useMemo(() => new Map(scenario.requests.map((r) => [r.id, r])), [scenario.requests]);
  const selectedRoute = plan?.routes.find((r) => r.engineer_id === route);
  const realRoutes = run?.manifest.transport === 'routing_static_v1';
  const journeyLookup = plan === run?.baseline ? run?.journeys?.baseline : run?.journeys?.plan;
  const routeJourneys = route === null ? undefined : journeyLookup?.[route];
  const assignments = useMemo(
    () =>
      new Map(
        plan?.routes.flatMap((r) =>
          r.visits.map(
            (v, i) => [v.request_id, { engineerId: r.engineer_id, number: i + 1 }] as const,
          ),
        ),
      ),
    [plan],
  );
  const pinRoute = useCallback(
    (selection: RouteHighlight | null) => {
      onPin(selection);
      const instance = map.current;
      if (!selection || !instance) return;
      const bounds = L.latLngBounds([]);
      for (const item of routeLines.current) {
        if (
          item.engineerId === selection.engineerId &&
          (!selection.requestId || item.requestId === selection.requestId)
        )
          bounds.extend(item.line.getBounds());
      }
      // An overview can include isolated stops separated by missing geometry.
      if (!selection.requestId) {
        for (const { marker, jobs } of routeMarkers.current) {
          if (jobs.some((job) => assignments.get(job.id)?.engineerId === selection.engineerId))
            bounds.extend(marker.getLatLng());
        }
      }
      if (!bounds.isValid()) return;
      const viewport = instance.getContainer().getBoundingClientRect();
      const details = itinerary.current?.parentElement?.getBoundingClientRect();
      const coveredWidth = details ? Math.max(0, viewport.right - details.left) : 0;
      instance.stop();
      instance.fitBounds(bounds, {
        paddingTopLeft: [32, 32],
        paddingBottomRight: [Math.min(coveredWidth + 32, viewport.width * 0.7), 32],
        maxZoom: 16,
        animate: false,
      });
    },
    [assignments, onPin],
  );
  const displayedRoutes = useMemo(
    () =>
      (selectedRoute ? [selectedRoute] : (plan?.routes ?? [])).filter(
        (r) => !hiddenEngineers.has(r.engineer_id),
      ),
    [selectedRoute, plan, route, hiddenEngineers],
  );
  const visible = useMemo(() => {
    const ids = new Set(displayedRoutes.flatMap((r) => r.visits.map((v) => v.request_id)));
    return scenario.requests.filter(
      (r) =>
        r.id === selectedRequestId ||
        r.id === focusRequest?.requestId ||
        ids.has(r.id) ||
        (showUnassigned && !selectedRoute && !assignments.has(r.id)),
    );
  }, [
    displayedRoutes,
    selectedRoute,
    assignments,
    scenario.requests,
    showUnassigned,
    selectedRequestId,
    focusRequest,
  ]);

  const activeHighlight = displayedRoutes.some(
    (item) => item.engineer_id === highlight?.engineerId && item.visits.length > 0,
  )
    ? highlight
    : null;

  useEffect(() => {
    const list = itinerary.current;
    if (!list || pinnedHighlight?.engineerId !== route || !pinnedHighlight.requestId) return;
    const item = Array.from(list.children).find(
      (child) => (child as HTMLElement).dataset.requestId === pinnedHighlight.requestId,
    );
    if (!item) return;
    const details = item.querySelector<HTMLDetailsElement>('.journey-details');
    if (details) details.open = true;
    // Scroll only this panel: scrollIntoView would also move the workspace/map.
    list.scrollTop += item.getBoundingClientRect().top - list.getBoundingClientRect().top - 8;
  }, [pinnedHighlight, route]);

  useEffect(() => {
    if (!source) {
      setCatalog({});
      return;
    }
    const controller = new AbortController();
    setCatalog({});
    setError('');
    request<Record<string, GeoLocation>>(source, { signal: controller.signal })
      .then((value) => {
        if (controller.signal.aborted) return;
        setCatalog(value);
      })
      .catch((e) => {
        if (e.name !== 'AbortError') setError(e.message);
      });
    return () => controller.abort();
  }, [source, catalogVersion]);

  useEffect(() => {
    if (!host.current) return;
    const instance = L.map(host.current, {
      scrollWheelZoom: true,
      minZoom: 1,
      maxZoom: 19,
    }).setView([55.6, 37.6], 9);
    map.current = instance;
    overlays.current = L.layerGroup().addTo(instance);
    const restoreAttribution = compactAttribution(instance);
    const unmountBasemap = mountBasemap(instance, setTileError);
    // Keep the visible top-left area anchored while the timeline crops/reveals the map.
    const observer = new ResizeObserver(() =>
      instance.invalidateSize({ pan: false, animate: false }),
    );
    observer.observe(host.current);
    setMapReady(true);
    return () => {
      unmountBasemap();
      observer.disconnect();
      restoreAttribution();
      instance.remove();
      map.current = null;
      overlays.current = null;
    };
  }, []);

  const hasDaData = Object.values({ ...records, ...catalog }).some((record) =>
    record.point?.source.startsWith('DaData:'),
  );
  useEffect(() => {
    const control = map.current?.attributionControl;
    if (!mapReady || !control) return;
    const credit =
      'Адресные данные: © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap contributors</a>' +
      (hasDaData
        ? ' · Адреса <a href="https://dadata.ru/suggestions/" target="_blank" rel="noopener noreferrer">DaData</a>'
        : '');
    control.addAttribution(credit);
    return () => {
      control.removeAttribution(credit);
    };
  }, [mapReady, hasDaData]);

  useEffect(() => {
    if (!mapReady || !map.current || !overlays.current) return;
    const layer = overlays.current;
    layer.clearLayers();
    routeLines.current = [];
    routeMarkers.current = [];
    const hoverLegs: HoverJourneyLeg[] = [];
    const bounds: L.LatLngTuple[] = [];
    const groups = new Map<
      string,
      { point: GeoPoint; jobs: WorkRequest[]; numbers: number[]; certain: boolean }
    >();
    visible.forEach((job, i) => {
      const record = records[job.location_id];
      const point = record?.point ?? record?.candidates[0];
      if (!point) return;
      const key = `${point.lat}:${point.lon}`;
      const group = groups.get(key) ?? { point, jobs: [], numbers: [], certain: true };
      group.jobs.push(job);
      group.numbers.push(assignments.get(job.id)?.number ?? i + 1);
      group.certain &&= !!record?.point;
      groups.set(key, group);
    });
    groups.forEach(({ point, jobs, numbers, certain }) => {
      // A shared address must retain its assigned marker when unassigned jobs are hidden.
      const primaryJob = jobs.find((job) => assignments.has(job.id)) ?? jobs[0];
      const assignment = assignments.get(primaryJob.id);
      const color = assignment
        ? engineerColor(run?.engineers.findIndex((e) => e.id === assignment.engineerId) ?? 0)
        : '#718096';
      const label =
        jobs.length > 1
          ? `${assignment?.number ?? numbers[0]}+`
          : plan && !assignment
            ? '!'
            : String(numbers[0]);
      const icon = L.divIcon({
        className: `map-stop ${certain ? '' : 'map-stop-review'} ${plan && !assignment ? 'map-stop-unassigned' : ''}`,
        html: `<span class="location-pin" style="--pin-color:${color}"><b>${label}</b></span>`,
        iconSize: [24, 30],
        iconAnchor: [12, 30],
      });
      const marker = L.marker([point.lat, point.lon], {
        icon,
        keyboard: true,
      }).addTo(layer);
      routeMarkers.current.push({ marker, jobs });
      marker
        .getElement()
        ?.setAttribute(
          'aria-label',
          `${requestAddress(primaryJob, records)}${certain ? '' : ' — нужна проверка'}`,
        );
      const tooltip = document.createElement('span');
      tooltip.textContent = `${requestAddress(primaryJob, records)} · ${jobs.length} заявок${certain ? '' : ' · координаты требуют проверки'}`;
      marker.bindTooltip(tooltip).on('click', () => onSelect(primaryJob));
      bounds.push([point.lat, point.lon]);
    });
    const officeRecord = scenario.office_location_id
      ? records[scenario.office_location_id]
      : Object.values(records).find((r) => r.address === scenario.office_address);
    const office = officeRecord?.point;
    const officeAddress = officeRecord ? locationAddress(officeRecord) : scenario.office_address;
    if (office) {
      const tooltip = document.createElement('span');
      tooltip.textContent = `Офис · ${officeAddress}`;
      const officeMarker = L.marker([office.lat, office.lon], {
        icon: L.divIcon({
          className: 'map-office',
          html: `<span class="office-pin">${iconMarkup('office')}</span>`,
          iconSize: [28, 34],
          iconAnchor: [14, 34],
        }),
        zIndexOffset: 1000,
      })
        .addTo(layer)
        .bindTooltip(tooltip);
      officeMarker.getElement()?.setAttribute('aria-label', `Офис · ${officeAddress}`);
      bounds.push([office.lat, office.lon]);
    }
    for (const engineerRoute of displayedRoutes) {
      const index = run?.engineers.findIndex((e) => e.id === engineerRoute.engineer_id) ?? 0;
      const color = engineerColor(index);
      if (realRoutes && selectedRoute) {
        for (const journey of journeyLookup?.[engineerRoute.engineer_id] ?? []) {
          for (const leg of journeySegments(journey.legs)) {
            if (leg.geometry.length < 2 || leg.distance_m === 0) continue;
            const points: L.LatLngTuple[] = leg.geometry.map((p) => [p.lat, p.lon]);
            const label = `${engineerName(run?.engineers[index])} · ${leg.line || modeNames[leg.mode]} · ${Math.ceil(leg.duration_s / 60)} мин${leg.geometry_quality === 'estimated' ? ' · схема прохода' : ''}`;
            const style = journeyStyle(leg);
            const { weight } = style;
            const line = L.polyline(points, {
              ...style,
              noClip: true,
              opacity: 0.88,
              className: `journey-path journey-${leg.mode} engineer-route-${index}`,
            }).addTo(layer);
            hoverLegs.push({
              line,
              label,
              highlight: { engineerId: engineerRoute.engineer_id, requestId: journey.request_id },
            });
            line.getElement()?.setAttribute('data-request-id', journey.request_id);
            routeLines.current.push({
              line,
              engineerId: engineerRoute.engineer_id,
              requestId: journey.request_id,
              weight,
              opacity: 0.88,
            });
            bounds.push(...points);
          }
        }
      } else {
        const engineer = run?.engineers.find((e) => e.id === engineerRoute.engineer_id);
        let previous: GeoPoint | null = engineer?.start_location_id
          ? (records[engineer.start_location_id]?.point ?? null)
          : (office ?? null);
        for (const visit of engineerRoute.visits) {
          const job = byId.get(visit.request_id);
          const point = job ? (records[job.location_id]?.point ?? null) : null;
          if (point && previous) {
            const line = L.polyline(
              [
                [previous.lat, previous.lon],
                [point.lat, point.lon],
              ],
              {
                color,
                weight: 3,
                opacity: 0.8,
                className: `route-overview engineer-route-${index}`,
              },
            ).addTo(layer);
            hoverLegs.push({
              line,
              label: engineerName(engineer),
              highlight: { engineerId: engineerRoute.engineer_id },
            });
            line.getElement()?.setAttribute('data-request-id', visit.request_id);
            routeLines.current.push({
              line,
              engineerId: engineerRoute.engineer_id,
              requestId: visit.request_id,
              weight: 3,
              opacity: 0.8,
            });
          }
          previous = point;
        }
      }
    }
    const viewKey = `${scenario.upload_id ?? scenario.id}:${run?.session_id}:${route}`;
    visibleBounds.current = bounds;
    if (bounds.length && fittedView.current !== viewKey) {
      map.current.fitBounds(bounds, { padding: [30, 30], maxZoom: 15, animate: false });
      fittedView.current = viewKey;
    }
    return bindJourneyHover(map.current, hoverLegs, onHighlight, pinRoute);
  }, [
    mapReady,
    records,
    visible,
    displayedRoutes,
    selectedRoute,
    assignments,
    route,
    scenario,
    byId,
    run,
    plan,
    onSelect,
    realRoutes,
    journeyLookup,
    onHighlight,
    pinRoute,
  ]);

  useEffect(() => {
    if (!mapReady || !focusRequest || handledFocus.current === focusRequest) return;
    const job = byId.get(focusRequest.requestId);
    const point = job && records[job.location_id]?.point;
    if (!point) return;
    handledFocus.current = focusRequest;
    map.current?.setView([point.lat, point.lon], 16, { animate: false });
  }, [mapReady, focusRequest, records, byId]);

  // Hover only restyles existing layers: it must never rebuild geometry or move the camera.
  // Keep SVG hit targets in place; reordering them can lose mouseout/focusout.
  useEffect(() => {
    for (const item of routeLines.current) {
      const emphasized =
        activeHighlight?.engineerId === item.engineerId &&
        (!selectedRoute ||
          !activeHighlight.requestId ||
          activeHighlight.requestId === item.requestId);
      item.line.setStyle({
        opacity: activeHighlight ? (emphasized ? 1 : 0.14) : item.opacity,
        weight: emphasized ? item.weight + 2 : item.weight,
      });
      item.line.getElement()?.classList.toggle('route-emphasized', emphasized);
      item.line
        .getElement()
        ?.setAttribute(
          'aria-pressed',
          String(
            pinnedHighlight?.engineerId === item.engineerId &&
              (!selectedRoute || pinnedHighlight.requestId === item.requestId),
          ),
        );
    }
    for (const { marker, jobs } of routeMarkers.current) {
      const emphasized = jobs.some(
        (job) => assignments.get(job.id)?.engineerId === activeHighlight?.engineerId,
      );
      marker.setOpacity(activeHighlight && !emphasized ? 0.3 : 1);
    }
  }, [
    activeHighlight,
    pinnedHighlight,
    mapReady,
    visible,
    displayedRoutes,
    selectedRoute,
    records,
    assignments,
    run,
    scenario,
    plan,
    byId,
    route,
    onSelect,
    realRoutes,
    journeyLookup,
    onHighlight,
    onPin,
  ]);

  return (
    <section className="panel geography" aria-label="Карта заявок">
      {error && (
        <p className="map-error" role="alert">
          {error}
        </p>
      )}
      {realRoutes && selectedRoute && !routeJourneys && (
        <p className="map-error" role="alert">
          Геометрия маршрутов отсутствует.
        </p>
      )}
      <div className={`map-layout ${selectedRoute ? 'with-stops' : ''}`}>
        <div className="map-area">
          <div ref={host} className="route-map" aria-label="Интерактивная карта" />
          <button
            className="map-fit-button"
            aria-label="Показать все точки на карте"
            title="Показать все точки на карте"
            onClick={() => {
              if (visibleBounds.current.length)
                map.current?.fitBounds(visibleBounds.current, {
                  padding: [30, 30],
                  maxZoom: 15,
                  animate: false,
                });
            }}
          >
            <Icon name="focus" size={19} />
          </button>
          {tileError && (
            <p className="map-unavailable" role="status">
              Подложка карты недоступна. Точки и список визитов остаются доступны.
            </p>
          )}
        </div>
        {selectedRoute && !hiddenEngineers.has(selectedRoute.engineer_id) && (
          <aside
            className="map-route-details"
            aria-label="Маршрут инженера"
            style={
              {
                '--route-color': engineerColor(
                  run?.engineers.findIndex((e) => e.id === route) ?? 0,
                ),
              } as CSSProperties
            }
          >
            <div className="map-route-heading">
              <button
                onClick={onOverview}
                aria-label="Показать маршруты всех инженеров"
                title="Все инженеры"
              >
                <Icon name="back" size={17} />
              </button>
              <strong>{engineerName(run?.engineers.find((e) => e.id === route))}</strong>
              <span>{selectedRoute.visits.length} заявок</span>
            </div>
            <ol ref={itinerary} className="map-itinerary" aria-label="Порядок визитов">
              {selectedRoute.visits.length === 0 && <li>Назначений нет</li>}
              {selectedRoute.visits.map((visit, i) => {
                const job = byId.get(visit.request_id);
                const journey = routeJourneys?.find((item) => item.request_id === visit.request_id);
                return (
                  job && (
                    <li
                      key={job.id}
                      data-request-id={job.id}
                      className={
                        activeHighlight?.engineerId === route &&
                        activeHighlight.requestId === job.id
                          ? 'journey-emphasized'
                          : ''
                      }
                      onMouseEnter={() =>
                        onHighlight({ engineerId: selectedRoute.engineer_id, requestId: job.id })
                      }
                      onMouseLeave={() => onHighlight(null)}
                      onFocus={() =>
                        onHighlight({ engineerId: selectedRoute.engineer_id, requestId: job.id })
                      }
                      onBlur={(event) => {
                        if (!event.currentTarget.contains(event.relatedTarget)) onHighlight(null);
                      }}
                    >
                      <span>{i + 1}</span>
                      <button className="map-visit-link" onClick={() => onSelect(job)}>
                        <strong>
                          {time(visit.start_s)} · {requestAddress(job, records)}
                        </strong>
                        <small>
                          {job.id} ·{' '}
                          {records[job.location_id]?.point
                            ? 'На карте'
                            : 'Координаты не подтверждены'}
                        </small>
                      </button>
                      {journey && <JourneyDetails journey={journey} />}
                    </li>
                  )
                );
              })}
            </ol>
          </aside>
        )}
      </div>
    </section>
  );
}
