import { useEffect, useRef, useState } from 'react';

export function WorkspaceDivider({
  onResize,
  orientation = 'horizontal',
  collapsed = false,
  onCollapsedChange,
}: {
  onResize: (size: number | null) => void;
  orientation?: 'horizontal' | 'vertical';
  collapsed?: boolean;
  onCollapsedChange?: (collapsed: boolean) => void;
}) {
  const vertical = orientation === 'vertical';
  const panelId = vertical ? 'dispatch-tools' : 'engineers-panel';
  const minimum = vertical ? 300 : 180;
  const ref = useRef<HTMLDivElement>(null);
  const drag = useRef<{ position: number; size: number } | null>(null);
  const [size, setSize] = useState({ value: vertical ? 362 : 360, max: 600 });
  const currentSize = () => {
    const box = ref.current?.parentElement?.querySelector(`#${panelId}`)?.getBoundingClientRect();
    return (vertical ? box?.width : box?.height) ?? size.value;
  };
  const limits = () => {
    const workspace = ref.current?.parentElement;
    if (vertical) return Math.max(minimum, Math.min(620, (workspace?.clientWidth ?? 1024) - 406));
    return window.matchMedia('(max-width: 780px)').matches
      ? Math.max(180, Math.floor(window.innerHeight * 0.75))
      : Math.max(180, (workspace?.clientHeight ?? 842) - 242);
  };
  const collapsible = vertical && !!onCollapsedChange;
  const collapse = () => {
    // Restore the width from before the squeeze when a tab reopens the content.
    const previous = drag.current?.size ?? currentSize();
    if (!collapsed && previous >= minimum) onResize(previous);
    onCollapsedChange?.(true);
  };
  const resize = (value: number) => {
    // Leave an 80px deliberate squeeze beyond the minimum readable panel width.
    if (collapsible && value <= 220) {
      collapse();
      return;
    }
    onCollapsedChange?.(false);
    onResize(Math.round(Math.max(minimum, Math.min(limits(), value))));
  };
  useEffect(() => {
    const workspace = ref.current?.parentElement;
    const panel = workspace?.querySelector(`#${panelId}`);
    if (!workspace || !panel) return;
    const observer = new ResizeObserver(() => {
      const box = panel.getBoundingClientRect();
      setSize({ value: Math.round(vertical ? box.width : box.height), max: limits() });
    });
    observer.observe(workspace);
    observer.observe(panel);
    return () => observer.disconnect();
  }, [panelId, vertical]);
  return (
    <div
      ref={ref}
      className={`workspace-divider${vertical ? ' tools-divider' : ''}`}
      role="separator"
      tabIndex={0}
      aria-label={vertical ? 'Ширина левой панели' : 'Высота панели инженеров'}
      aria-orientation={orientation}
      aria-controls={vertical ? 'tools-content' : panelId}
      aria-valuemin={collapsible ? 62 : minimum}
      aria-valuemax={size.max}
      aria-valuenow={size.value}
      aria-valuetext={collapsed ? 'Окно панели свёрнуто' : `${size.value} пикселей`}
      title={
        vertical
          ? 'Потяните влево, чтобы сузить и свернуть окно. Стрелки меняют ширину, Enter сворачивает или раскрывает, двойной щелчок сбрасывает ширину.'
          : 'Потяните вверх или вниз. Стрелки меняют высоту, двойной щелчок сбрасывает её.'
      }
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        event.currentTarget.focus();
        event.currentTarget.setPointerCapture(event.pointerId);
        drag.current = {
          position: vertical ? event.clientX : event.clientY,
          size: currentSize(),
        };
      }}
      onPointerMove={(event) => {
        if (drag.current)
          resize(
            drag.current.size +
              (vertical
                ? event.clientX - drag.current.position
                : drag.current.position - event.clientY),
          );
      }}
      onPointerUp={(event) => {
        drag.current = null;
        if (event.currentTarget.hasPointerCapture(event.pointerId))
          event.currentTarget.releasePointerCapture(event.pointerId);
      }}
      onPointerCancel={() => {
        drag.current = null;
      }}
      onLostPointerCapture={() => {
        drag.current = null;
      }}
      onDoubleClick={() => {
        onCollapsedChange?.(false);
        onResize(null);
      }}
      onKeyDown={(event) => {
        // A key press can arrive before ResizeObserver reports the final drag position.
        const value = currentSize();
        if (collapsible) {
          if (
            event.key === 'Home' ||
            (event.key === 'ArrowLeft' && value <= minimum) ||
            (event.key === 'Enter' && !collapsed)
          ) {
            event.preventDefault();
            collapse();
            return;
          }
          if (collapsed && (event.key === 'ArrowRight' || event.key === 'Enter')) {
            event.preventDefault();
            onCollapsedChange?.(false);
            return;
          }
        }
        const sizes: Record<string, number> = {
          [vertical ? 'ArrowRight' : 'ArrowUp']: value + 32,
          [vertical ? 'ArrowLeft' : 'ArrowDown']: value - 32,
          Home: minimum,
          End: limits(),
        };
        if (event.key in sizes) {
          event.preventDefault();
          resize(sizes[event.key]);
        }
      }}
    >
      <span />
    </div>
  );
}
