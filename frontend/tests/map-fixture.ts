import type { Run, GeoLocation } from '../src/types';

// Browser-only coordinates: route display tests do not depend on geocoders or
// address corrections made by earlier tests. Never written to the accepted day.
export function withMapCoordinates(run: Run): Run {
  const result = structuredClone(run);
  const addresses = new Map(
    result.scenario.requests.map((request) => [request.location_id, request.address]),
  );
  const office = result.scenario.office_location_id ?? result.engineers[0]?.start_location_id;
  if (office) addresses.set(office, result.scenario.office_address);
  const catalog = Object.fromEntries(
    [...addresses].map(([location_id, address]) => [
      location_id,
      {
        location_id,
        address,
        revision: 0,
        status: 'manual' as const,
        point: null,
        candidates: [],
        query: '',
        updated_at: '',
        note: 'Browser fixture',
      },
    ]),
  );
  result.scenario.geography = mapCoordinates(catalog);
  return result;
}

export function mapCoordinates(catalog: Record<string, GeoLocation>) {
  const result = structuredClone(catalog);
  Object.values(result).forEach((record, i) => {
    record.point = {
      lat: 55.7 + (i % 10) * 0.002,
      lon: 37.6 + Math.floor(i / 10) * 0.002,
      label: record.address,
      source: 'Browser fixture',
      precision: 'house',
    };
  });
  return result;
}
