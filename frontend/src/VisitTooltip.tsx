import { useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import type { Visit, WorkRequest } from './types';
import { time, requestAddress } from './format';
import { engineerColor } from './icons';

export interface VisitSummary {
  anchor: HTMLElement;
  job: WorkRequest;
  visit: Visit;
  engineerId: string;
  engineerIndex: number;
  engineerName: string;
}

export function VisitTooltip({
  id,
  summary,
  onEnter,
  onLeave,
}: {
  id: string;
  summary: VisitSummary;
  onEnter: () => void;
  onLeave: () => void;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null);
  const { job, visit, engineerIndex, anchor } = summary;
  useLayoutEffect(() => {
    if (!panel.current) return;
    const target = anchor.getBoundingClientRect();
    const box = panel.current.getBoundingClientRect();
    const gap = 8;
    const left = Math.max(gap, Math.min(target.left, window.innerWidth - box.width - gap));
    const preferredTop = target.top - box.height - gap;
    const top = Math.max(
      gap,
      Math.min(
        preferredTop >= gap ? preferredTop : target.bottom + gap,
        window.innerHeight - box.height - gap,
      ),
    );
    setPosition({ left, top });
  }, [anchor]);
  return createPortal(
    <div
      ref={panel}
      id={id}
      role="tooltip"
      className="visit-summary"
      style={
        {
          ...position,
          visibility: position ? 'visible' : 'hidden',
          '--route-color': engineerColor(engineerIndex),
        } as CSSProperties
      }
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
    >
      <strong>Заявка {job.id}</strong>
      <p className="visit-summary-address">{requestAddress(job)}</p>
      <p className="visit-summary-engineer">
        {summary.engineerName} · {job.kind}
      </p>
      <dl>
        <div>
          <dt>В пути</dt>
          <dd>{Math.ceil(visit.travel_s / 60)} мин</dd>
        </div>
        <div>
          <dt>Расстояние</dt>
          <dd>{(visit.distance_m / 1000).toFixed(2)} км</dd>
        </div>
        <div>
          <dt>Ожидание у клиента</dt>
          <dd>{Math.ceil(visit.waiting_s / 60)} мин</dd>
        </div>
        <div>
          <dt>Прибытие</dt>
          <dd>{time(visit.arrival_s)}</dd>
        </div>
        <div>
          <dt>Начало работ</dt>
          <dd>{time(visit.start_s)}</dd>
        </div>
        <div>
          <dt>Окончание работ</dt>
          <dd>{time(visit.finish_s)}</dd>
        </div>
      </dl>
      <small>По выбранному плану · переезд до этой заявки</small>
    </div>,
    document.body,
  );
}
