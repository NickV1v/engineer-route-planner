import { useState, type CSSProperties } from 'react';
import type { Run } from './types';
import { engineerName, time } from './format';
import { engineerColor, Icon, profileIcons, profileNames } from './icons';

const skills: Record<string, string> = {
  installation: 'Подключения и дозаказы',
  local: 'Локальные работы',
  emergency: 'Аварийные работы',
};

export function EngineerList({
  run,
  disabled,
  onUnavailable,
  onFocus,
}: {
  run: Run;
  disabled: boolean;
  onUnavailable: (id: string) => void;
  onFocus: (id: string) => void;
}) {
  const [search, setSearch] = useState('');
  const roster = new Set(
    run.staffing?.status === 'active'
      ? run.staffing.engineer_ids
      : run.plan.routes.filter((r) => r.visits.length).map((r) => r.engineer_id),
  );
  const unavailable = new Set(run.unavailable_engineers ?? []);
  const routes = new Map(run.plan.routes.map((r) => [r.engineer_id, r]));
  const engineers = run.engineers
    .map((engineer, index) => ({ engineer, index }))
    .filter(
      ({ engineer }) =>
        roster.has(engineer.id) &&
        `${engineerName(engineer)} ${engineer.id}`
          .toLocaleLowerCase('ru')
          .includes(search.trim().toLocaleLowerCase('ru')),
    );
  return (
    <>
      <input
        className="search"
        aria-label="Поиск инженера"
        placeholder="Найти по имени или ID"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />
      <div className="list-summary">
        <span>На смене: {roster.size}</span>
      </div>
      <ul className="engineer-cards" aria-label="Инженеры смены">
        {engineers.map(({ engineer, index }) => (
          <li
            key={engineer.id}
            data-engineer-id={engineer.id}
            style={{ '--engineer-color': engineerColor(index) } as CSSProperties}
          >
            <div className="engineer-card-heading">
              <button
                className="engineer-card-name"
                onClick={() => onFocus(engineer.id)}
                title="Показать маршрут"
              >
                <span className="engineer-card-icon">
                  <Icon name={profileIcons[engineer.profile]} size={18} />
                </span>
                <strong>{engineerName(engineer)}</strong>
              </button>
              <span className={`badge ${unavailable.has(engineer.id) ? 'rejected' : 'assigned'}`}>
                {unavailable.has(engineer.id) ? 'Недоступен' : 'Доступен'}
              </span>
            </div>
            <p>
              {profileNames[engineer.profile]} · {time(engineer.shift_start_s)}–
              {time(engineer.shift_end_s)}
            </p>
            <p>{engineer.skills.map((skill) => skills[skill] ?? skill).join(', ')}</p>
            <div className="list-item-actions">
              <span>Заявок: {routes.get(engineer.id)?.visits.length ?? 0}</span>
              <button
                className="list-action danger-action"
                disabled={disabled || unavailable.has(engineer.id)}
                onClick={() => onUnavailable(engineer.id)}
              >
                {unavailable.has(engineer.id) ? 'Уже недоступен' : 'Сделать недоступным'}
              </button>
            </div>
          </li>
        ))}
      </ul>
      {!engineers.length && (
        <p className="no-results">
          {roster.size ? 'Инженеры не найдены' : 'В этом плане нет инженеров на смене'}
        </p>
      )}
    </>
  );
}
