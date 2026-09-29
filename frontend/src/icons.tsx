const paths = {
  lock: 'M6 10h12v11H6z M8 10V6a4 4 0 0 1 8 0v4',
  route: 'M5 3h5v5H5z M14 15h5v5h-5z M10 5.5h6a3 3 0 0 1 0 6H8a3 3 0 0 0 0 6h6',
  plus: 'M12 5v14 M5 12h14',
  engineer: 'M9 5a3 3 0 1 0 6 0a3 3 0 1 0-6 0 M6 21v-3a6 6 0 0 1 12 0v3',
  list: 'M9 6h11 M9 12h11 M9 18h11 M4 6h.1 M4 12h.1 M4 18h.1',
  cancel: 'M7 4h10v16H5V6h2 M9 10l6 6 M15 10l-6 6',
  clock: 'M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18 M12 7v5l3 2',
  pin: 'M19 10c0 5-7 11-7 11S5 15 5 10a7 7 0 1 1 14 0 M12 7a3 3 0 1 0 0 6a3 3 0 1 0 0-6',
  office:
    'M4 21V3h12v18 M2 21h20 M16 9h4v12 M8 7h1 M12 7h1 M8 11h1 M12 11h1 M8 15h1 M12 15h1 M9 21v-3h3v3',
  car: 'M3 15v-4l2-6h14l2 6v7H3v-3 M3 11h18 M6 15h2 M16 15h2 M5 18v3 M19 18v3',
  walk: 'M13 3a2 2 0 1 0 0 4a2 2 0 1 0 0-4 M9 22l2-7-2-3 2-4 4 4h4 M4 13l4-3 3-2 M11 15l5 3 1 4',
  transit:
    'M5 17V5c0-3 14-3 14 0v12l-3 3H8z M5 11h14 M12 4v7 M8 15h.1 M16 15h.1 M8 20l-2 2 M16 20l2 2',
  bicycle:
    'M5 14a4 4 0 1 0 0 8a4 4 0 1 0 0-8 M19 14a4 4 0 1 0 0 8a4 4 0 1 0 0-8 M5 18l5-9 5 9H5 M10 9h6 M8 6h4 M10 6v3 M15 4h3l1 14',
  eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12 M12 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6',
  eyeOff: 'M3 3l18 18 M10 5c7-1 12 7 12 7l-3 4 M6 6l-4 6s4 7 10 7l4-1 M10 10a3 3 0 0 0 4 4',
  download: 'M12 3v12 M7 10l5 5 5-5 M4 17v4h16v-4',
  close: 'M6 6l12 12 M18 6L6 18',
  panelClose: 'M3 4h18v16H3z M9 4v16 M16 9l-3 3 3 3',
  panelOpen: 'M3 4h18v16H3z M9 4v16 M14 9l3 3-3 3',
  check: 'M5 12l4 4L19 6',
  focus: 'M8 3H3v5 M16 3h5v5 M21 16v5h-5 M8 21H3v-5 M12 8a4 4 0 1 0 0 8a4 4 0 1 0 0-8',
  back: 'M19 12H5 M11 6l-6 6 6 6',
} as const;
export type IconName = keyof typeof paths;

export function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name]} />
    </svg>
  );
}
export function iconMarkup(name: IconName) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name]}"/></svg>`;
}
export const profileIcons: Record<string, IconName> = {
  car: 'car',
  walk: 'walk',
  public_transport_approx: 'transit',
  bicycle: 'bicycle',
};
export const profileNames: Record<string, string> = {
  car: 'Автомобиль',
  walk: 'Пешком',
  public_transport_approx: 'Общественный транспорт',
  bicycle: 'Велосипед',
};
const colors = [
  '#2876dc',
  '#16a06a',
  '#8752cc',
  '#d47b21',
  '#d14e79',
  '#1695a6',
  '#6875ce',
  '#8c7145',
  '#c44040',
  '#527d24',
  '#a447ad',
  '#346b87',
  '#a85b32',
  '#517fae',
  '#c33f95',
  '#477b69',
  '#6145a0',
  '#aa7b15',
  '#227d86',
  '#a74c63',
  '#57669b',
  '#5f882e',
  '#9a573f',
  '#865f99',
  '#207e57',
  '#b6628d',
  '#7a702a',
  '#426dba',
  '#ae6120',
  '#8b466f',
];
export const engineerColor = (index: number) => colors[Math.max(0, index) % colors.length];
