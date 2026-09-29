import type { JourneyLeg } from './types';

// Collapse graph edges into passenger legs, without crossing waits, transfers or visits.
// Keep the archived journey immutable: it is also used by the comparison and export.
export function journeySegments(legs: JourneyLeg[]): JourneyLeg[] {
  const result: JourneyLeg[] = [];
  for (const leg of legs) {
    const previous = result.at(-1);
    if (
      previous &&
      previous.mode === leg.mode &&
      previous.line === leg.line &&
      previous.to_id === leg.from_id
    ) {
      const last = previous.geometry.at(-1);
      const first = leg.geometry[0];
      const sharedPoint = last && first && last.lat === first.lat && last.lon === first.lon;
      previous.geometry = previous.geometry.concat(leg.geometry.slice(sharedPoint ? 1 : 0));
      previous.to_id = leg.to_id;
      previous.to_label = leg.to_label;
      previous.duration_s += leg.duration_s;
      previous.distance_m += leg.distance_m;
      if (leg.geometry_quality === 'estimated') previous.geometry_quality = 'estimated';
      if (leg.quality === 'estimated') previous.quality = 'estimated';
    } else result.push({ ...leg, geometry: [...leg.geometry] });
  }
  return result;
}
