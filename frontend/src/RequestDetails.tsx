import { useEffect, useRef, type CSSProperties } from 'react';
import type { GeoLocation, Run, Scenario, WorkRequest } from './types';
import { requestAddress, time, engineerName } from './format';
import { engineerColor, Icon } from './icons';
import { explainRejection, reasonNames } from './requestReasons';
import './RequestDetails.css';

const skillNames: Record<string, string> = {
  installation: 'Подключения',
  local: 'Локальные работы',
  emergency: 'Аварийные работы',
};
const priorities = {
  emergency: 'Авария · 1',
  installation: 'Подключение · 2',
  repair: 'Ремонт · 3',
  additional: 'Дозаказ · 3',
};

export function RequestDetails({
  job,
  scenario,
  run,
  geography,
  onClose,
  onLocate,
  onCancel,
  busy,
}: {
  job: WorkRequest;
  scenario: Scenario;
  run: Run | null;
  geography: Record<string, GeoLocation>;
  onClose: () => void;
  onLocate: () => void;
  onCancel: () => void;
  busy: boolean;
}) {
  const close = useRef<HTMLButtonElement>(null);
  const route = run?.plan.routes.find((r) => r.visits.some((v) => v.request_id === job.id));
  const visit = route?.visits.find((v) => v.request_id === job.id);
  const engineerIndex = run?.engineers.findIndex((e) => e.id === route?.engineer_id) ?? -1;
  const rejection = run?.plan.unassigned.find((r) => r.request_id === job.id);
  const point = geography[job.location_id]?.point;
  const explanation = rejection ? explainRejection(rejection, job, !point) : null;
  const frozen = run?.frozen_request_ids?.includes(job.id);
  useEffect(() => {
    close.current?.focus({ preventScroll: true });
    close.current?.closest('.tool-content')?.scrollTo({ top: 0 });
  }, [job.id]);
  return (
    <section
      className="request-details"
      role="region"
      aria-label="Детали заявки"
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          e.stopPropagation();
          onClose();
        }
      }}
    >
      <button ref={close} className="list-back" aria-label="К списку заявок" onClick={onClose}>
        <Icon name="back" size={16} /> К заявкам
      </button>
      <header className="request-detail-header">
        <div>
          <span className="request-detail-id">Заявка #{job.id.split(':').pop()}</span>
          <h2 id="request-detail-title">{job.kind}</h2>
        </div>
      </header>
      <div className="request-detail-body">
        <span className={`badge ${visit ? 'assigned' : rejection ? 'rejected' : ''}`}>
          {visit ? 'Назначена' : rejection ? 'Без назначения' : 'Ожидает расчёта'}
        </span>
        <p className="request-detail-address">{requestAddress(job, geography)}</p>
        <div className="request-detail-actions">
          <button className="secondary" onClick={onLocate} disabled={!point}>
            <Icon name="focus" size={16} /> Показать на карте
          </button>
          <button
            className="secondary danger-action"
            disabled={busy || !run || !!frozen}
            title={
              !run
                ? 'Сначала рассчитайте план'
                : frozen
                  ? 'Выезд уже начат или работа выполнена'
                  : undefined
            }
            onClick={onCancel}
          >
            <Icon name="cancel" size={16} /> Отменить заявку
          </button>
        </div>
        <dl className="request-facts">
          <dt>Время у клиента</dt>
          <dd>
            {time(job.window_start_s)}–{time(job.window_end_s)}
          </dd>
          <dt>Работа на месте</dt>
          <dd>{Math.ceil(job.service_s / 60)} мин</dd>
          {job.subtype && (
            <>
              <dt>Подтип</dt>
              <dd>{job.subtype}</dd>
            </>
          )}
          <dt>Квалификация</dt>
          <dd>{skillNames[job.skill] ?? job.skill}</dd>
          <dt>Приоритет</dt>
          <dd>
            {scenario.objective_policy
              ? job.work_type
                ? priorities[job.work_type]
                : 'Обычный · тип не уточнён'
              : job.urgent
                ? 'Срочная'
                : 'Обычная'}
          </dd>
        </dl>
        {visit && (
          <section
            className="request-assignment"
            style={{ '--engineer-color': engineerColor(engineerIndex) } as CSSProperties}
          >
            <h3>
              <span className="engineer-dot" />
              {engineerName(run?.engineers[engineerIndex])}
            </h3>
            <dl className="request-facts">
              <dt>Прибытие</dt>
              <dd>{time(visit.arrival_s)}</dd>
              <dt>Работа</dt>
              <dd>
                {time(visit.start_s)}–{time(visit.finish_s)}
              </dd>
              <dt>В пути</dt>
              <dd>
                {Math.round(visit.travel_s / 60)} мин · {(visit.distance_m / 1000).toFixed(1)} км
              </dd>
              <dt>Ожидание</dt>
              <dd>{Math.round(visit.waiting_s / 60)} мин</dd>
            </dl>
            {frozen && (
              <p className="request-commitment">
                <Icon name="lock" size={14} />
                Выезд уже начат или завершён. Назначение закреплено.
              </p>
            )}
          </section>
        )}
        {explanation && rejection && (
          <section className="request-rejection">
            <h3>{explanation.title}</h3>
            <p>{explanation.action}</p>
            {Object.keys(rejection.checks).length > 0 && (
              <details className="request-rejection-checks">
                <summary>Причины по инженерам</summary>
                <ul>
                  {Object.entries(rejection.checks).map(([id, reason]) => (
                    <li key={id}>
                      <strong>{engineerName(run?.engineers.find((e) => e.id === id))}</strong>
                      <span>
                        {reasonNames[reason] ?? 'Не удалось включить заявку в расписание'}
                      </span>
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </section>
        )}
      </div>
    </section>
  );
}
