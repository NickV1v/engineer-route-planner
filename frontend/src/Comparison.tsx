import type { Run } from './types';

interface Props {
  run: Run;
  mode: 'baseline' | 'optimized';
  onSelect: (mode: 'baseline' | 'optimized') => void;
  showSwitcher?: boolean;
}

const signed = (n: number, digits = 0) =>
  `${n > 0 ? '+' : n < 0 ? '−' : ''}${Math.abs(n).toFixed(digits)}`;

export function Comparison({ run, mode, onSelect, showSwitcher = true }: Props) {
  if (!run.baseline || !run.comparison || !run.search) return null;
  const before = run.baseline.metrics;
  const after = run.plan.metrics;
  const c = run.comparison;
  const priority = c.objective_components?.includes('emergency_unassigned');
  const components = c.objective_components ?? [
    'unassigned',
    'active_engineers',
    'urgent_unassigned',
    'urgent_delay_s',
    'distance_m',
    'travel_s',
  ];
  const coverageRows = priority
    ? [
        ['emergency_unassigned', 'Неназначенных аварий'],
        ['installation_unassigned', 'Неназначенных подключений'],
        ['regular_unassigned', 'Неназначенных остальных работ'],
      ]
    : [['urgent_unassigned', 'Неназначенных срочных']];
  const urgentDelay = components.indexOf(priority ? 'emergency_delay_s' : 'urgent_delay_s');
  return (
    <section className="comparison" aria-label="Сравнение алгоритмов">
      <div className="comparison-intro">
        <div>
          <h2>{c.improved ? 'Найдено лучшее распределение' : 'Улучшение пока не найдено'}</h2>
        </div>
        <span className="comparison-runtime">
          Поиск: {(run.search.elapsed_ms / 1000).toFixed(2)} с
        </span>
      </div>
      <div className="comparison-table-wrap">
        <table className="comparison-table">
          <thead>
            <tr>
              <th scope="col">Показатель</th>
              <th scope="col">{showSwitcher ? 'Базовый план' : 'База'}</th>
              <th scope="col">{showSwitcher ? 'Оптимизация' : 'План'}</th>
              <th scope="col">{showSwitcher ? 'Изменение' : 'Δ'}</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <th scope="row">Назначено заявок</th>
              <td>
                {before.assigned} / {before.total}
              </td>
              <td>
                {after.assigned} / {after.total}
              </td>
              <td className={c.assigned_delta > 0 ? 'delta-good' : ''}>
                {signed(c.assigned_delta)}
              </td>
            </tr>
            <tr>
              <th scope="row">Задействовано инженеров</th>
              <td>{before.active_engineers}</td>
              <td>{after.active_engineers}</td>
              <td>{signed(c.engineers_delta)}</td>
            </tr>
            {coverageRows.map(([component, label]) => {
              const index = components.indexOf(component);
              return (
                <tr key={component}>
                  <th scope="row">{label}</th>
                  <td>{c.baseline_score[index]}</td>
                  <td>{c.optimized_score[index]}</td>
                  <td>{signed(c.optimized_score[index] - c.baseline_score[index])}</td>
                </tr>
              );
            })}
            <tr>
              <th scope="row">Задержка старта {priority ? 'аварий' : 'срочных'}, мин</th>
              <td>{(c.baseline_score[urgentDelay] / 60).toFixed(1)}</td>
              <td>{(c.optimized_score[urgentDelay] / 60).toFixed(1)}</td>
              <td>
                {signed((c.optimized_score[urgentDelay] - c.baseline_score[urgentDelay]) / 60, 1)}
              </td>
            </tr>
            <tr>
              <th scope="row">
                {run.manifest.transport === 'routing_static_v1'
                  ? 'Расстояние, км'
                  : 'Тестовое расстояние, км'}
              </th>
              <td>{(before.distance_m / 1000).toFixed(1)}</td>
              <td>{(after.distance_m / 1000).toFixed(1)}</td>
              <td>{signed(c.distance_delta_m / 1000, 1)}</td>
            </tr>
          </tbody>
        </table>
      </div>
      {showSwitcher && (
        <div className="plan-switch" role="group" aria-label="Показать план">
          <span>Расписание и заявки:</span>
          <button aria-pressed={mode === 'baseline'} onClick={() => onSelect('baseline')}>
            Базовый план
          </button>
          <button aria-pressed={mode === 'optimized'} onClick={() => onSelect('optimized')}>
            Оптимизированный план
          </button>
        </div>
      )}
    </section>
  );
}
