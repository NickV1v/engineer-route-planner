import type { Journey, JourneyLeg } from './types';
import { journeySegments } from './journeySegments';
import { journeyStyle } from './journeyStyle';

export const modeNames: Record<JourneyLeg['mode'], string> = {
  car: 'Автомобиль',
  walk: 'Пешком',
  bicycle: 'Велосипед',
  metro: 'Метро',
  bus: 'Автобус',
  train: 'Поезд',
  tram: 'Трамвай',
  wait: 'Ожидание',
};

export function JourneyDetails({ journey }: { journey: Journey }) {
  return (
    <details className="journey-details">
      <summary>
        Переезд {Math.ceil(journey.duration_s / 60)} мин · {(journey.distance_m / 1000).toFixed(1)}{' '}
        км
      </summary>
      <ol aria-label="Участки поездки">
        {journeySegments(journey.legs)
          .filter((leg) => leg.duration_s > 0)
          .map((leg, i) => (
            <li
              key={i}
              style={{
                borderLeftColor: journeyStyle(leg).color,
                borderLeftStyle: journeyStyle(leg).dashArray ? 'dashed' : 'solid',
              }}
            >
              <strong>
                {leg.mode === 'wait' ? `Ожидание · ${leg.line}` : leg.line || modeNames[leg.mode]}
              </strong>
              <small>
                {leg.from_label || (i === 0 ? 'От адреса' : '')}
                {leg.to_label && leg.to_label !== leg.from_label ? ` → ${leg.to_label}` : ''}
              </small>
              <small>
                {Math.ceil(leg.duration_s / 60)} мин
                {leg.distance_m > 0 ? ` · ${(leg.distance_m / 1000).toFixed(2)} км` : ''}
              </small>
            </li>
          ))}
      </ol>
    </details>
  );
}
