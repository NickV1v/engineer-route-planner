import { useEffect, useRef, useState } from 'react';
import type { Run } from './types';
import { Icon } from './icons';
import './ExportMenu.css';

export function ExportMenu({
  run,
  disabled,
  visibleEngineers,
}: {
  run: Run | null;
  disabled: boolean;
  visibleEngineers: string[];
}) {
  const [open, setOpen] = useState(false);
  const [partial, setPartial] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const restoreFocus = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const total = run?.plan.routes.filter((route) => route.visits.length).length ?? 0;
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {
    if (restoreFocus.current && !open && !loading && !disabled) {
      trigger.current?.focus();
      restoreFocus.current = false;
    }
  }, [open, loading, disabled]);
  useEffect(() => {
    setOpen(false);
    setPartial(false);
    setError('');
  }, [run?.session_id, run?.version]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open]);
  function close() {
    restoreFocus.current = true;
    setOpen(false);
  }
  async function download(kind: 'excel' | 'waybills' | 'json') {
    if (!run || loading || disabled) return;
    const request = new AbortController();
    controller.current = request;
    setLoading(true);
    setError('');
    try {
      const params = new URLSearchParams({ version: String(run.version) });
      if (kind !== 'json') {
        params.set('format', kind);
        params.set('scope', partial ? 'visible' : 'all');
        if (partial) visibleEngineers.forEach((id) => params.append('engineer_id', id));
      }
      const response = await fetch(
        `/api/sessions/${run.session_id}/${kind === 'json' ? 'snapshot' : 'export'}?${params}`,
        { signal: request.signal },
      );
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        throw new Error(
          typeof body?.detail === 'string'
            ? body.detail
            : 'Не удалось скачать план. Повторите попытку.',
        );
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      const title =
        kind === 'excel' ? 'План' : kind === 'waybills' ? 'Маршрутные_листы' : 'Полный_план';
      link.download = `${title}_${run.scenario.date}${partial && kind !== 'json' ? '_выбранные_маршруты' : ''}.${kind === 'json' ? 'json' : 'xlsx'}`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      close();
    } catch (error) {
      if (!request.signal.aborted) setError((error as Error).message);
    } finally {
      if (!request.signal.aborted) setLoading(false);
    }
  }
  const noVisible = partial && !visibleEngineers.length;
  return (
    <div
      className="export-control"
      ref={root}
      onKeyDown={(event) => {
        if (event.key === 'Escape' && open) {
          event.stopPropagation();
          close();
        }
      }}
      onBlur={(event) => {
        if (
          event.relatedTarget instanceof Node &&
          !event.currentTarget.contains(event.relatedTarget)
        )
          setOpen(false);
      }}
    >
      <button
        ref={trigger}
        className="export-button"
        aria-label="Экспорт плана"
        aria-expanded={open}
        aria-controls="export-options"
        disabled={!run || disabled || loading}
        onClick={() => setOpen((value) => !value)}
      >
        <Icon name="download" size={17} />
        <span>{loading ? 'Скачиваем…' : 'Экспорт плана'}</span>
      </button>
      {open && (
        <div
          id="export-options"
          className="export-options"
          role="region"
          aria-label="Форматы экспорта"
          aria-busy={loading}
        >
          <label className="export-scope">
            <input
              type="checkbox"
              checked={partial}
              disabled={loading || disabled}
              onChange={(event) => setPartial(event.target.checked)}
            />
            <span>Только видимые маршруты</span>
            <b>
              {visibleEngineers.length}/{total}
            </b>
          </label>
          <button
            className="export-option"
            disabled={disabled || loading || noVisible}
            onClick={() => void download('excel')}
          >
            <strong>План в Excel</strong>
            <span>Маршруты, метрики и неназначенные заявки</span>
            <b>XLSX</b>
          </button>
          <button
            className="export-option"
            disabled={disabled || loading || noVisible || !total}
            onClick={() => void download('waybills')}
          >
            <strong>Маршрутные листы</strong>
            <span>Отдельный лист для каждого инженера</span>
            <b>XLSX</b>
          </button>
          {noVisible && <p className="export-empty">Покажите нужные маршруты на карте.</p>}
          {error && (
            <p className="export-error" role="alert">
              {error}
            </p>
          )}
          <button
            className="export-option export-json"
            disabled={disabled || loading}
            onClick={() => void download('json')}
          >
            <strong>Полный план · JSON</strong>
            <span>Все маршруты и исходные данные</span>
          </button>
        </div>
      )}
    </div>
  );
}
