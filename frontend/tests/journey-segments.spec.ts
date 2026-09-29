import { expect, test } from '@playwright/test';
import { journeySegments } from '../src/journeySegments';
import type { JourneyLeg } from '../src/types';

const points = Array.from({ length: 5 }, (_, i) => ({ lat: 55.7 + i * 0.01, lon: 37.6 }));
const edge = (i: number, mode: JourneyLeg['mode'], line: string | null): JourneyLeg => ({
  mode,
  line,
  from_id: `${i}`,
  to_id: `${i + 1}`,
  from_label: `Точка ${i}`,
  to_label: `Точка ${i + 1}`,
  duration_s: 120,
  distance_m: 300,
  geometry: points.slice(i, i + 2),
  quality: 'estimated',
  geometry_quality: 'network',
});

test('passenger legs join metro, bus and walking edges without changing archived geometry or totals', () => {
  for (const [mode, line] of [
    ['metro', 'Метро 5'],
    ['bus', 'Автобус 7'],
    ['walk', null],
  ] as const) {
    const legs = [edge(0, mode, line), edge(1, mode, line), edge(2, mode, line)];
    if (mode === 'walk') legs[1].geometry_quality = 'estimated';
    const original = structuredClone(legs);
    const segments = journeySegments(legs);
    expect(segments).toHaveLength(1);
    expect(segments[0]).toMatchObject({
      from_id: '0',
      to_id: '3',
      from_label: 'Точка 0',
      to_label: 'Точка 3',
      duration_s: 360,
      distance_m: 900,
      geometry: points.slice(0, 4),
      geometry_quality: mode === 'walk' ? 'estimated' : 'network',
    });
    expect(legs).toEqual(original);
  }
});

test('transfers, waits and disconnected journeys remain separate', () => {
  const wait: JourneyLeg = {
    ...edge(1, 'wait', 'Автобус 7'),
    to_id: '1',
    to_label: 'Точка 1',
    geometry: [],
    distance_m: 0,
    duration_s: 0,
  };
  expect(
    journeySegments([edge(0, 'bus', 'Автобус 7'), wait, edge(1, 'bus', 'Автобус 7')]),
  ).toHaveLength(3);
  expect(
    journeySegments([
      edge(0, 'metro', 'Метро 5'),
      edge(1, 'walk', null),
      edge(2, 'metro', 'Метро 5'),
    ]),
  ).toHaveLength(3);
  expect(journeySegments([edge(0, 'bus', 'Автобус 7'), edge(1, 'bus', 'Автобус 8')])).toHaveLength(
    2,
  );
  expect(journeySegments([edge(0, 'walk', null), edge(2, 'walk', null)])).toHaveLength(2);
});
