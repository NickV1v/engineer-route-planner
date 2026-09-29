import { useEffect, useId, useRef, useState, type CSSProperties } from 'react';
import type { Engineer, Plan, Run, WorkRequest } from './types';
import { time, engineerName } from './format';
import { engineerColor, Icon, profileIcons, profileNames } from './icons';
import type { RouteHighlight } from './routeHighlight';
import { VisitTooltip, type VisitSummary } from './VisitTooltip';

export function EngineerTimeline({
  run,
  plan,
  hidden,
  highlight,
  onToggle,
  onShowAll,
  onHideAll,
  onFocus,
  onSelect,
  onHighlight,
}: {
  run: Run | null;
  plan?: Plan;
  hidden: Set<string>;
  highlight: RouteHighlight | null;
  onToggle: (id: string) => void;
  onShowAll: () => void;
  onHideAll: () => void;
  onFocus: (id: string) => void;
  onSelect: (request: WorkRequest) => void;
  onHighlight: (highlight: RouteHighlight | null) => void;
}) {
  const [view, setView] = useState<'timeline' | 'metrics'>('timeline');
  const [summary, setSummary] = useState<VisitSummary | null>(null);
  const tooltipId = useId();
  const closeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const openTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const cancelClose = () => clearTimeout(closeTimer.current);
  const closeSummary = () => {
    clearTimeout(openTimer.current);
    cancelClose();
    setSummary(null);
  };
  const leaveSummary = () => {
    clearTimeout(openTimer.current);
    cancelClose();
    closeTimer.current = setTimeout(() => setSummary(null), 140);
  };
  useEffect(() => {
    const clear = () => {
      clearTimeout(openTimer.current);
      clearTimeout(closeTimer.current);
      setSummary(null);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        clear();
        onHighlight(null);
      }
    };
    const scroll = (event: Event) => {
      if (
        event.target instanceof Node &&
        document.getElementById(tooltipId)?.contains(event.target)
      )
        return;
      clear();
      if (
        !(event.target instanceof Node) ||
        document.getElementById('engineers-panel')?.contains(event.target)
      )
        onHighlight(null);
    };
    clear();
    onHighlight(null);
    window.addEventListener('resize', clear);
    window.addEventListener('blur', clear);
    window.addEventListener('scroll', scroll, true);
    window.addEventListener('keydown', escape);
    return () => {
      clearTimeout(openTimer.current);
      clearTimeout(closeTimer.current);
      window.removeEventListener('resize', clear);
      window.removeEventListener('blur', clear);
      window.removeEventListener('scroll', scroll, true);
      window.removeEventListener('keydown', escape);
      onHighlight(null);
    };
  }, [plan, view, hidden, onHighlight, tooltipId]);
  const morningBaseline = plan === run?.baseline && run?.comparison?.scope === 'morning';
  const displayedIds = new Set(
    morningBaseline
      ? plan?.routes.filter((r) => r.visits.length).map((r) => r.engineer_id)
      : run?.staffing?.status === 'active'
        ? run.staffing.engineer_ids
        : run?.plan.routes.filter((r) => r.visits.length).map((r) => r.engineer_id),
  );
  // Keep original indices so names, colors and map routes agree after filtering.
  const engineerRows = (run?.engineers ?? [])
    .map((engineer, index) => ({ engineer, index }))
    .filter(({ engineer }) => displayedIds.has(engineer.id));
  const start =
    Math.floor(Math.min(...(run?.engineers.map((e) => e.shift_start_s) ?? []), 32400) / 3600) *
    3600;
  const end =
    Math.ceil(Math.max(...(run?.engineers.map((e) => e.shift_end_s) ?? []), 79200) / 3600) * 3600;
  const span = end - start;
  const hours = span / 3600;
  const byId = new Map(run?.scenario.requests.map((r) => [r.id, r]));
  const frozen = new Set(run?.frozen_request_ids ?? []);
  const position = (at: number, duration: number) => ({
    left: `${((at - start) / span) * 100}%`,
    width: `${(duration / span) * 100}%`,
  });
  const identity = (engineer: Engineer, index: number, visitCount: number) => {
    const label = engineerName(engineer);
    const owner = /^Инженер \d+$/.test(label) ? label.replace('Инженер', 'инженера') : `— ${label}`;
    const unavailable = run?.unavailable_engineers?.includes(engineer.id);
    const offShift =
      run?.staffing?.status === 'active' && !run.staffing.engineer_ids.includes(engineer.id);
    return (
      <div className="engineer-info">
        <button
          className="eye-button"
          aria-label={`${hidden.has(engineer.id) ? 'Показать' : 'Скрыть'} маршрут ${owner}`}
          aria-pressed={!hidden.has(engineer.id)}
          title={hidden.has(engineer.id) ? 'Показать на карте' : 'Скрыть с карты'}
          onClick={() => onToggle(engineer.id)}
        >
          <Icon name={hidden.has(engineer.id) ? 'eyeOff' : 'eye'} size={18} />
        </button>
        <span
          className="transport-icon"
          role="img"
          aria-label={profileNames[engineer.profile]}
          title={profileNames[engineer.profile]}
        >
          <Icon name={profileIcons[engineer.profile] ?? 'walk'} />
        </span>
        <button
          className="engineer-focus"
          onClick={() => onFocus(engineer.id)}
          title="Показать только этот маршрут"
        >
          <strong>{engineerName(engineer)}</strong>
          <small>
            {time(engineer.shift_start_s)}–{time(engineer.shift_end_s)}
            {unavailable
              ? ' · Недоступен'
              : offShift
                ? ' · Только в базовом плане'
                : !visitCount
                  ? ' · Без работ'
                  : ''}
          </small>
        </button>
      </div>
    );
  };
  return (
    <section id="engineers-panel" className="engineers-panel" aria-label="Расписание инженеров">
      <div className="engineers-heading">
        <h2>
          Инженеры <span className="counter">{run ? engineerRows.length : '—'}</span>
        </h2>
        <div className="engineer-tabs" role="tablist" aria-label="Вид панели инженеров">
          {(['timeline', 'metrics'] as const).map((value) => (
            <button
              key={value}
              id={`tab-engineers-${value}`}
              role="tab"
              aria-selected={view === value}
              aria-controls={`engineers-${value}`}
              tabIndex={view === value ? 0 : -1}
              onClick={() => setView(value)}
              onKeyDown={(event) => {
                if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
                event.preventDefault();
                const next =
                  event.key === 'Home'
                    ? 'timeline'
                    : event.key === 'End'
                      ? 'metrics'
                      : value === 'timeline'
                        ? 'metrics'
                        : 'timeline';
                setView(next);
                document.getElementById(`tab-engineers-${next}`)?.focus();
              }}
            >
              {value === 'timeline' ? 'Таймлайн' : 'Метрики'}
            </button>
          ))}
        </div>
        <div className="route-visibility-actions">
          <button onClick={onShowAll} disabled={!run}>
            Показать все
          </button>
          <button onClick={onHideAll} disabled={!run}>
            Скрыть все
          </button>
        </div>
        <div className="schedule-legend" hidden={view !== 'timeline'}>
          <span>
            <i className="work-key" />
            Работа
          </span>
          <span>
            <i className="travel-key" />В пути
          </span>
          <span>
            <i className="waiting-key" />
            Ожидание
          </span>
        </div>
      </div>
      {!run || !plan ? (
        <div className="schedule-empty">
          <Icon name="route" size={32} />
          <h3>План ещё не построен</h3>
        </div>
      ) : (
        <>
          <div
            className="engineer-scroll"
            role="tabpanel"
            id="engineers-timeline"
            aria-labelledby="tab-engineers-timeline"
            hidden={view !== 'timeline'}
          >
            <div className="engineer-grid">
              <div className="engineer-axis">
                <div>Инженер / смена</div>
                <div className="hour-axis">
                  {Array.from({ length: hours + 1 }, (_, i) => (
                    <span key={i} style={{ left: `${(i / hours) * 100}%` }}>
                      {time(start + i * 3600)}
                    </span>
                  ))}
                </div>
              </div>
              {engineerRows.map(({ engineer, index }) => {
                const route = plan.routes.find((r) => r.engineer_id === engineer.id);
                const visits = route?.visits ?? [];
                const color = engineerColor(index);
                return (
                  <div
                    className={`engineer-row ${hidden.has(engineer.id) ? 'route-hidden' : ''} ${highlight?.engineerId === engineer.id ? 'route-highlighted' : ''}`}
                    key={engineer.id}
                    data-engineer-id={engineer.id}
                    style={{ '--route-color': color } as CSSProperties}
                    onMouseEnter={() => onHighlight({ engineerId: engineer.id })}
                    onMouseLeave={() => onHighlight(null)}
                    onFocus={() => onHighlight({ engineerId: engineer.id })}
                    onBlur={(event) => {
                      if (!event.currentTarget.contains(event.relatedTarget)) onHighlight(null);
                    }}
                  >
                    {identity(engineer, index, visits.length)}
                    <div className="track" style={{ backgroundSize: `${100 / hours}% 100%` }}>
                      <div
                        className="shift"
                        style={position(
                          engineer.shift_start_s,
                          engineer.shift_end_s - engineer.shift_start_s,
                        )}
                      />
                      {run.event_at_s !== undefined &&
                        run.event_at_s >= start &&
                        run.event_at_s <= end && (
                          <div
                            className="event-line"
                            title={`Событие в ${time(run.event_at_s)}`}
                            style={{ left: `${((run.event_at_s - start) / span) * 100}%` }}
                          />
                        )}
                      {visits.map((visit, i) => {
                        const job = byId.get(visit.request_id);
                        if (!job) return null;
                        const urgent = run.scenario.objective_policy
                          ? job.work_type === 'emergency'
                          : job.urgent;
                        const showSummary = (anchor: HTMLElement, immediate = false) => {
                          closeSummary();
                          const next = {
                            anchor,
                            job,
                            visit,
                            engineerId: engineer.id,
                            engineerIndex: index,
                            engineerName: engineerName(engineer),
                          };
                          if (immediate) setSummary(next);
                          else openTimer.current = setTimeout(() => setSummary(next), 1500);
                          onHighlight({ engineerId: engineer.id, requestId: job.id });
                        };
                        return (
                          <div key={visit.request_id}>
                            {visit.travel_s > 0 && (
                              <span
                                className="travel-segment"
                                style={position(visit.arrival_s - visit.travel_s, visit.travel_s)}
                              />
                            )}
                            {visit.waiting_s > 0 && (
                              <span
                                className="waiting-segment"
                                style={position(visit.arrival_s, visit.waiting_s)}
                              />
                            )}
                            <button
                              className={`visit ${urgent ? 'urgent' : ''} ${frozen.has(job.id) ? 'frozen' : ''}`}
                              style={position(visit.start_s, visit.finish_s - visit.start_s)}
                              onClick={() => {
                                closeSummary();
                                onHighlight(null);
                                onSelect(job);
                              }}
                              onMouseEnter={(event) => showSummary(event.currentTarget)}
                              onMouseLeave={() => {
                                leaveSummary();
                                onHighlight({ engineerId: engineer.id });
                              }}
                              onFocus={(event) => {
                                event.stopPropagation();
                                showSummary(event.currentTarget, true);
                              }}
                              onBlur={() => {
                                closeSummary();
                                onHighlight(null);
                              }}
                              aria-describedby={summary?.job.id === job.id ? tooltipId : undefined}
                              aria-label={`Открыть заявку ${job.id}`}
                            >
                              {i + 1}
                            </button>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
          <div
            className="engineer-scroll"
            role="tabpanel"
            id="engineers-metrics"
            aria-labelledby="tab-engineers-metrics"
            hidden={view !== 'metrics'}
          >
            <table className="engineer-metrics-table" aria-label="Метрики инженеров">
              <thead>
                <tr>
                  <th scope="col">Инженер / смена</th>
                  <th scope="col">Работ</th>
                  <th scope="col">Путь, км</th>
                  <th scope="col">В пути, мин</th>
                  <th scope="col">Работа, мин</th>
                  <th scope="col">Ожидание, мин</th>
                  <th scope="col" className="equipment-column">
                    {run.scenario.equipment_policy && !run.scenario.equipment_issued
                      ? 'К выдаче: роутеры'
                      : 'Выдано роутеров'}
                  </th>
                  <th scope="col" className="equipment-column">
                    {run.scenario.equipment_policy && !run.scenario.equipment_issued
                      ? 'К выдаче: ТВ-приставки'
                      : 'Выдано ТВ-приставок'}
                  </th>
                </tr>
              </thead>
              <tbody>
                {engineerRows.map(({ engineer, index }) => {
                  const route = plan.routes.find((item) => item.engineer_id === engineer.id);
                  const visits = route?.visits ?? [];
                  // Issuance belongs to the accepted day, even when viewing the baseline.
                  const issued = run.scenario.equipment_issued?.[engineer.id];
                  const acceptedVisits =
                    run.plan.routes.find((item) => item.engineer_id === engineer.id)?.visits ?? [];
                  const routers =
                    issued?.routers ??
                    (run.scenario.equipment_policy
                      ? acceptedVisits.filter(
                          (visit) => byId.get(visit.request_id)?.kind === 'Подключение',
                        ).length
                      : '—');
                  const boxes =
                    issued?.set_top_boxes ??
                    (run.scenario.equipment_policy
                      ? acceptedVisits.filter(
                          (visit) => byId.get(visit.request_id)?.kind === 'Дозаказ',
                        ).length
                      : '—');
                  return (
                    <tr
                      key={engineer.id}
                      data-engineer-id={engineer.id}
                      className={hidden.has(engineer.id) ? 'route-hidden' : ''}
                      style={{ '--route-color': engineerColor(index) } as CSSProperties}
                    >
                      <th scope="row">{identity(engineer, index, visits.length)}</th>
                      <td>{visits.length}</td>
                      <td>{((route?.distance_m ?? 0) / 1000).toFixed(1)}</td>
                      <td>
                        {Math.round(visits.reduce((sum, visit) => sum + visit.travel_s, 0) / 60)}
                      </td>
                      <td>
                        {Math.round(
                          visits.reduce((sum, visit) => sum + visit.finish_s - visit.start_s, 0) /
                            60,
                        )}
                      </td>
                      <td>
                        {Math.round(visits.reduce((sum, visit) => sum + visit.waiting_s, 0) / 60)}
                      </td>
                      <td data-equipment="routers">{routers}</td>
                      <td data-equipment="set_top_boxes">{boxes}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
      {summary && view === 'timeline' && (
        <VisitTooltip
          id={tooltipId}
          summary={summary}
          onEnter={() => {
            cancelClose();
            onHighlight({ engineerId: summary.engineerId, requestId: summary.job.id });
          }}
          onLeave={() => {
            leaveSummary();
            onHighlight(null);
          }}
        />
      )}
    </section>
  );
}
