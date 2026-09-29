import L from 'leaflet';

/** Keep Leaflet's live provider credits inside a compact, keyboard-accessible control. */
export function compactAttribution(map: L.Map) {
  const attribution = map.attributionControl;
  attribution.setPrefix(false);
  const content = attribution.getContainer()!;
  const details = document.createElement('details');
  details.className = 'leaflet-control map-credits';
  details.open = true;
  const summary = document.createElement('summary');
  summary.setAttribute('aria-label', 'Источники карты');
  summary.textContent = 'ⓘ';
  content.before(details);
  details.append(summary, content);
  L.DomEvent.disableClickPropagation(details);
  L.DomEvent.disableScrollPropagation(details);

  let timer: ReturnType<typeof setTimeout> | undefined;
  const stopAutomaticCollapse = () => {
    clearTimeout(timer);
    observer.disconnect();
  };
  // Show asynchronously loaded credits for five seconds before collapsing them.
  // Once the user opens or focuses the control, keep it open until dismissed.
  const scheduleCollapse = () => {
    clearTimeout(timer);
    if (content.textContent?.trim()) {
      timer = setTimeout(() => {
        stopAutomaticCollapse();
        details.open = false;
      }, 5000);
    }
  };
  const observer = new MutationObserver(scheduleCollapse);
  observer.observe(content, { childList: true, subtree: true, characterData: true });
  scheduleCollapse();
  summary.addEventListener('click', stopAutomaticCollapse);
  details.addEventListener('focusin', stopAutomaticCollapse);
  details.addEventListener('pointerenter', stopAutomaticCollapse);
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === 'Escape' && details.open) {
      stopAutomaticCollapse();
      details.open = false;
      summary.focus();
      event.stopPropagation();
    }
  };
  details.addEventListener('keydown', onKeyDown);
  return () => {
    stopAutomaticCollapse();
    summary.removeEventListener('click', stopAutomaticCollapse);
    details.removeEventListener('focusin', stopAutomaticCollapse);
    details.removeEventListener('pointerenter', stopAutomaticCollapse);
    details.removeEventListener('keydown', onKeyDown);
    details.replaceWith(content);
  };
}
