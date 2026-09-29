import type { JourneyLeg } from './types';

// Fixed display palette, independent of engineers and of OSM direction variants.
const metroColors: Readonly<Record<string, string>> = {
  '1': '#E42313', // Сокольническая
  '2': '#4FB04F', // Замоскворецкая
  '3': '#0072BA', // Арбатско-Покровская
  '4': '#1EBCEF', // Филёвская
  '4A': '#1EBCEF',
  '5': '#A35539', // Кольцевая
  '6': '#F07E23', // Калужско-Рижская
  '7': '#943E90', // Таганско-Краснопресненская
  '8': '#FFCD1C', // Калининская
  '8A': '#FFCD1C', // Солнцевская (Latin and Cyrillic А are normalized below)
  '9': '#ADACAC', // Серпуховско-Тимирязевская
  '10': '#BED12C', // Люблинско-Дмитровская
  '11': '#78C7C9', // Большая кольцевая
  '11A': '#78C7C9',
  '12': '#BAC8E8', // Бутовская
  '14': '#E42313', // МЦК
  '15': '#F088B6', // Некрасовская
  '16': '#007763', // Троицкая
  '17': '#474A51', // Рублёво-Архангельская
};
const diameterColors: Readonly<Record<string, string>> = {
  D1: '#ED9F2D',
  D2: '#DF477C',
  D3: '#E15D29',
  D4: '#3FB485',
  D4A: '#3FB485',
};
const neutral = '#748194';

function railColor(mode: 'metro' | 'train', line: string | null): string {
  const ref = (line ?? '').trim().toUpperCase().replaceAll('А', 'A');
  if (mode === 'metro') return metroColors[ref.replace(/^МЕТРО\s+/, '')] ?? neutral;
  if (ref === 'МЦК') return metroColors['14'];
  const trainRef = ref
    .replace(/^МЦК\/МЦД\s+/, '')
    .replace(/^МЦД[\s-]*/, 'D')
    .replace(/^Д/, 'D');
  return (trainRef === '14' ? metroColors['14'] : diameterColors[trainRef]) ?? neutral;
}

export function journeyStyle(leg: Pick<JourneyLeg, 'mode' | 'line' | 'geometry_quality'>) {
  const color =
    leg.mode === 'metro' || leg.mode === 'train'
      ? railColor(leg.mode, leg.line)
      : {
          walk: '#64B5F6',
          car: '#2563C9',
          bicycle: '#16A6B6',
          bus: '#2FA34F',
          tram: '#E58B32',
          wait: neutral,
        }[leg.mode];
  return {
    color,
    weight: leg.mode === 'walk' ? 2.5 : 3.5,
    dashArray: leg.mode === 'walk' || leg.geometry_quality === 'estimated' ? '4 5' : undefined,
  };
}
