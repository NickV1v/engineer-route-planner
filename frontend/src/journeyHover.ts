import L from 'leaflet';
import type { RouteHighlight } from './routeHighlight';

export interface HoverJourneyLeg {
  line: L.Polyline;
  label: string;
  highlight: RouteHighlight;
}

// One tooltip and one hover target for all legs. Leaflet's per-layer tooltip
// focus/click handlers can outlive a mouse hover, especially on overlapping paths.
export function bindJourneyHover(
  map: L.Map,
  legs: HoverJourneyLeg[],
  onHighlight: (highlight: RouteHighlight | null) => void,
  onPin: (highlight: RouteHighlight | null) => void,
) {
  const container = map.getContainer();
  const targets = new Map<Element, HoverJourneyLeg>();
  const tooltip = L.tooltip({ className: 'journey-tooltip', direction: 'top', offset: [0, -8] });
  let active: HoverJourneyLeg | null = null;
  let moving = false;
  const clickHandlers: (() => void)[] = [];
  for (const leg of legs) {
    const element = leg.line.getElement();
    if (!element) continue;
    targets.set(element, leg);
    element.setAttribute('tabindex', '0');
    element.setAttribute('role', 'button');
    element.setAttribute('aria-label', leg.label);
    const click = (event: L.LeafletMouseEvent) => {
      if (container.classList.contains('placing')) return;
      L.DomEvent.stopPropagation(event);
      onPin({ ...leg.highlight });
    };
    // Leaflet suppresses these clicks after a drag; a DOM click listener would not.
    leg.line.on('click', click);
    clickHandlers.push(() => leg.line.off('click', click));
  }
  const clear = () => {
    tooltip.remove();
    if (active) onHighlight(null);
    active = null;
  };
  const show = (leg: HoverJourneyLeg, position: L.LatLng) => {
    if (active !== leg) {
      active = leg;
      const label = document.createElement('span');
      label.textContent = leg.label;
      tooltip.setContent(label);
      onHighlight(leg.highlight);
    }
    tooltip.setLatLng(position);
    if (!map.hasLayer(tooltip)) tooltip.addTo(map);
  };
  const move = (event: PointerEvent) => {
    const leg = event.target instanceof Element ? targets.get(event.target) : undefined;
    if (!leg || moving || event.buttons || event.pointerType === 'touch') {
      clear();
      return;
    }
    show(leg, map.mouseEventToLatLng(event));
  };
  const focus = (event: FocusEvent) => {
    const element = event.target;
    if (!(element instanceof Element)) return;
    const leg = targets.get(element);
    if (leg && !moving && element.matches(':focus-visible')) {
      show(leg, leg.line.getCenter());
    }
  };
  const blur = (event: FocusEvent) => {
    if (active?.line.getElement() === event.target) clear();
  };
  const keydown = (event: KeyboardEvent) => {
    if (event.key === 'Escape') clear();
    if (event.key === 'Enter' || event.key === ' ') {
      const leg = event.target instanceof Element ? targets.get(event.target) : undefined;
      if (leg) {
        event.preventDefault();
        onPin({ ...leg.highlight });
      }
    }
  };
  const startMove = () => {
    moving = true;
    clear();
  };
  const endMove = () => {
    moving = false;
  };
  const backgroundClick = () => {
    clear();
    onPin(null);
  };
  container.addEventListener('pointermove', move);
  container.addEventListener('pointerleave', clear);
  container.addEventListener('pointerdown', clear);
  container.addEventListener('pointerup', move);
  container.addEventListener('pointercancel', clear);
  container.addEventListener('focusin', focus);
  container.addEventListener('focusout', blur);
  container.addEventListener('keydown', keydown);
  window.addEventListener('blur', clear);
  map.on('movestart', startMove);
  map.on('moveend', endMove);
  map.on('click', backgroundClick);
  return () => {
    clear();
    clickHandlers.forEach((remove) => remove());
    container.removeEventListener('pointermove', move);
    container.removeEventListener('pointerleave', clear);
    container.removeEventListener('pointerdown', clear);
    container.removeEventListener('pointerup', move);
    container.removeEventListener('pointercancel', clear);
    container.removeEventListener('focusin', focus);
    container.removeEventListener('focusout', blur);
    container.removeEventListener('keydown', keydown);
    window.removeEventListener('blur', clear);
    map.off('movestart', startMove);
    map.off('moveend', endMove);
    map.off('click', backgroundClick);
  };
}
