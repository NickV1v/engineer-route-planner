import type { Engineer, GeoLocation, WorkRequest } from './types';

export function engineerName(engineer?: Pick<Engineer, 'id' | 'name'>) {
  if (!engineer) return 'Инженер';
  const legacy = /:engineer-(\d+)$/.exec(engineer.id);
  return engineer.name || (legacy ? `Инженер ${Number(legacy[1])}` : engineer.id);
}

export function requestAddress(request: WorkRequest, geography?: Record<string, GeoLocation>) {
  const record = geography?.[request.location_id];
  return record?.point && (record.status === 'manual' || record.address.startsWith('Координаты '))
    ? locationAddress(record)
    : request.display_address || request.address;
}
export const locationAddress = (record: GeoLocation) =>
  record.status === 'manual' || record.address.startsWith('Координаты ')
    ? record.point?.label.trim() || record.address
    : record.address;

export const time = (seconds: number) =>
  `${Math.floor(seconds / 3600)
    .toString()
    .padStart(2, '0')}:${Math.floor((seconds % 3600) / 60)
    .toString()
    .padStart(2, '0')}`;
