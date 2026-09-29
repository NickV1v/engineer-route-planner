import type { Run } from './types';

export function CalculationTiming({ performance }: { performance: Run['performance'] }) {
  if (!performance) return null;
  const seconds = (ms: number) => (ms / 1000).toFixed(3);
  return (
    <div className="calculation-timing" aria-label="Время расчёта">
      <p>
        Маршруты: <strong>{seconds(performance.routing_ms)} с</strong> · Распределение и проверка:{' '}
        <strong>{seconds(performance.solver_ms)} с</strong>
      </p>
    </div>
  );
}
