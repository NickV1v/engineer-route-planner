import { useEffect, useRef, useState } from 'react';
import type { DayEvent, GeoPoint, Preview, Run } from './types';
import { time, engineerName, requestAddress } from './format';
import { AddressSuggestions } from './AddressSuggestions';
import { CalculationTiming } from './CalculationTiming';

const changeNames: Record<string, string> = {
  added: 'Добавлена',
  cancelled: 'Отменена',
  assigned: 'Назначена',
  unassigned: 'Без назначения',
  reassigned: 'Другой инженер',
  rescheduled: 'Другое время',
  reordered: 'Другой порядок',
};
const seconds = (value: string) => {
  const [h, m, s = 0] = value.split(':').map(Number);
  return h * 3600 + m * 60 + s;
};

export function EventPanel({
  run,
  busy,
  onApply,
  preview,
  onCommit,
  onDiscard,
  requestedType: type,
  targetId,
  blocked = false,
}: {
  run: Run;
  busy: boolean;
  onApply: (event: DayEvent) => Promise<void>;
  preview: Preview | null;
  onCommit: () => void;
  onDiscard: () => void;
  requestedType: DayEvent['type'];
  targetId?: string;
  blocked?: boolean;
}) {
  const requiredStaff = run.plan.routes.filter((r) => r.visits.length).map((r) => r.engineer_id);
  const [mode, setMode] = useState<'preserve' | 'flexible'>('flexible');
  const [delay, setDelay] = useState('15');
  const initialTime = Math.max(run.event_at_s ?? 0, 43200);
  // Preserve exact seconds of the previous event; HTML time's default step is one minute.
  const inputTime = (s: number) => `${time(s)}:${String(s % 60).padStart(2, '0')}`;
  const [at, setAt] = useState(inputTime(initialTime));
  const timeInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!targetId) return;
    timeInput.current?.focus({ preventScroll: true });
    timeInput.current?.closest('.tool-content')?.scrollTo({ top: 0 });
  }, [targetId]);
  const [id, setId] = useState(() => `urgent-${crypto.randomUUID().slice(0, 8)}`);
  const [address, setAddress] = useState('');
  const [addressSelection, setAddressSelection] = useState<{
    point: GeoPoint;
    original: string;
  } | null>(null);
  const [kind, setKind] = useState('Дозаказ');
  const [start, setStart] = useState(inputTime(initialTime));
  const [end, setEnd] = useState(inputTime(Math.max(initialTime, 72000)));
  const [profile, setProfile] = useState('');
  const retry = useRef<{ signature: string; id: string } | null>(null);
  const frozen = new Set(run.frozen_request_ids ?? []);
  const currentAt = seconds(at);
  const committedNow = new Set(
    run.plan.routes.flatMap((r) =>
      r.visits
        .filter(
          (v) =>
            frozen.has(v.request_id) ||
            v.arrival_s - v.travel_s < currentAt ||
            v.start_s <= currentAt,
        )
        .map((v) => v.request_id),
    ),
  );
  const request = run.scenario.requests.find((r) => r.id === targetId);
  const engineer = run.engineers.find((e) => e.id === targetId);
  const roster = run.staffing?.status === 'active' ? run.staffing.engineer_ids : requiredStaff;
  const targetError =
    type === 'cancel'
      ? !request
        ? 'Заявка не найдена.'
        : committedNow.has(request.id)
          ? 'К этому времени выезд уже начат или работа выполнена. Отменить заявку нельзя.'
          : ''
      : type === 'engineer_unavailable'
        ? !engineer || !roster.includes(engineer.id)
          ? 'Инженер не входит в смену.'
          : run.unavailable_engineers?.includes(engineer.id)
            ? 'Инженер уже недоступен.'
            : ''
        : '';

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy || blocked || preview || targetError) return;
    const payload = {
      type,
      mode,
      max_delay_s: Number(delay) * 60,
      at_s: currentAt,
      expected_version: run.version,
      ...(type === 'add_urgent'
        ? {
            request: {
              id,
              address: address.trim(),
              kind,
              window_start_s: seconds(start),
              window_end_s: seconds(end),
              required_profile: profile || null,
              ...(addressSelection
                ? {
                    coordinates: addressSelection.point,
                    original_address: addressSelection.original,
                  }
                : {}),
            },
          }
        : type === 'cancel'
          ? { request_id: targetId }
          : { engineer_id: targetId }),
    };
    const signature = JSON.stringify(payload);
    if (retry.current?.signature !== signature)
      retry.current = { signature, id: crypto.randomUUID() };
    await onApply({ ...payload, event_id: retry.current.id });
  }

  return (
    <section className="panel events" aria-label="События рабочего дня">
      <div className="event-compose">
        <form onSubmit={submit}>
          <fieldset disabled={busy || blocked || !!preview} className="event-fields">
            {type !== 'add_urgent' && (
              <div className="event-target">
                <strong>
                  {type === 'cancel'
                    ? `#${request?.id.split(':').pop() ?? targetId}`
                    : engineerName(engineer)}
                </strong>
                <span>
                  {type === 'cancel' && request
                    ? requestAddress(request)
                    : engineer
                      ? `${time(engineer.shift_start_s)}–${time(engineer.shift_end_s)}`
                      : ''}
                </span>
              </div>
            )}
            <label>
              Время события
              <input
                ref={timeInput}
                type="time"
                step="1"
                required
                value={at}
                min={inputTime(run.event_at_s ?? 0)}
                onChange={(e) => setAt(e.target.value)}
              />
            </label>
            {type === 'add_urgent' ? (
              <>
                <label>
                  ID новой заявки
                  <input
                    required
                    maxLength={64}
                    pattern="[A-Za-z0-9_\-]+"
                    value={id}
                    onChange={(e) => setId(e.target.value)}
                  />
                </label>
                <label>
                  Тип работы
                  <select value={kind} onChange={(e) => setKind(e.target.value)}>
                    {['Подключение', 'Дозаказ', 'Локальная заявка', 'Глобальная проблема'].map(
                      (name) => (
                        <option key={name} value={name}>
                          {name === 'Глобальная проблема' && run.scenario.objective_policy
                            ? 'Авария'
                            : name}
                        </option>
                      ),
                    )}
                  </select>
                </label>
                <div className="event-address">
                  <AddressSuggestions
                    label="Адрес новой заявки"
                    required
                    value={address}
                    selectedPoint={addressSelection?.point}
                    autoResolve={!addressSelection}
                    previewMap
                    disabled={busy}
                    onChange={(value) => {
                      setAddress(value);
                      setAddressSelection(null);
                    }}
                    onSelect={(item) => {
                      setAddressSelection({ point: item.point!, original: address.trim() });
                      setAddress(item.label);
                    }}
                  />
                </div>
                <label>
                  Окно начала: с
                  <input
                    required
                    type="time"
                    step="1"
                    value={start}
                    onChange={(e) => setStart(e.target.value)}
                  />
                </label>
                <label>
                  Окно начала: до
                  <input
                    required
                    type="time"
                    step="1"
                    min={inputTime(Math.max(seconds(start) || 0, currentAt || 0))}
                    value={end}
                    onChange={(e) => setEnd(e.target.value)}
                  />
                </label>
                <label>
                  Требуемый транспорт
                  <select value={profile} onChange={(e) => setProfile(e.target.value)}>
                    <option value="">Любой</option>
                    <option value="car">Автомобиль</option>
                    <option value="walk">Пешком</option>
                    <option value="bicycle">Велосипед</option>
                    <option value="public_transport_approx">Общ. транспорт</option>
                  </select>
                </label>
              </>
            ) : null}
            <label>
              Режим перепланирования
              <select
                value={mode}
                onChange={(e) => setMode(e.target.value as 'preserve' | 'flexible')}
              >
                <option value="flexible">Разрешить перестановки</option>
                <option value="preserve">Сохранить расписание</option>
              </select>
            </label>
            {mode === 'preserve' && (
              <label>
                Допустимый сдвиг позже, мин
                <input
                  type="number"
                  min="0"
                  max="120"
                  step="1"
                  required
                  value={delay}
                  onChange={(e) => setDelay(e.target.value)}
                />
              </label>
            )}

            {targetError && (
              <p className="event-target-error" role="status">
                {targetError}
              </p>
            )}
            <div className="event-submit">
              <button type="submit" className="primary" disabled={!!targetError}>
                {busy ? 'Перепланирование…' : 'Рассчитать вариант'}
              </button>
            </div>
          </fieldset>
        </form>
      </div>
      {preview && (
        <div className="proposal" role="region" aria-label="Вариант для принятия">
          <div className="eyebrow">ПРЕДЛОЖЕНИЕ · ПЛАН ЕЩЁ НЕ ИЗМЕНЁН</div>
          <h3>
            {preview.result.event?.mode === 'flexible'
              ? 'Вариант с перестановками'
              : 'Вариант с сохранением расписания'}
          </h3>
          <p>
            Состав смены: {run.staffing?.scheduled} чел. Назначено: {run.plan.metrics.assigned} →{' '}
            {preview.result.plan.metrics.assigned}. Без назначения:{' '}
            {preview.result.plan.metrics.unassigned}.
          </p>
          <p>
            Другой инженер:{' '}
            {preview.result.changes?.filter((c) => c.changes.includes('reassigned')).length ?? 0} ·
            другое время:{' '}
            {preview.result.changes?.filter((c) => c.changes.includes('rescheduled')).length ?? 0} ·
            потеряли назначение:{' '}
            {preview.result.changes?.filter((c) => c.changes.includes('unassigned')).length ?? 0}.
          </p>
          {preview.result.scenario.objective_policy && (
            <p aria-label="Неназначенные по приоритету">
              Без назначения в варианте:{' '}
              {[
                ['emergency', 'аварии'],
                ['installation', 'подключения'],
                ['regular', 'остальные работы'],
              ].map(([group, label], i) => {
                const ids = new Set(preview.result.plan.unassigned.map((r) => r.request_id));
                const count = preview.result.scenario.requests.filter(
                  (r) =>
                    ids.has(r.id) &&
                    (group === 'regular'
                      ? r.work_type !== 'emergency' && r.work_type !== 'installation'
                      : r.work_type === group),
                ).length;
                return (
                  <span key={group}>
                    {i > 0 ? ' · ' : ''}
                    {label}: {count}
                  </span>
                );
              })}
            </p>
          )}
          <ChangeDetails run={preview.result} />
          <CalculationTiming performance={preview.result.performance} />
          <div className="version-actions">
            <button className="primary" disabled={busy} onClick={onCommit}>
              Принять вариант
            </button>
            <button
              className="secondary"
              disabled={busy}
              onClick={() => {
                retry.current = null;
                onDiscard();
              }}
            >
              Отклонить вариант
            </button>
          </div>
        </div>
      )}
      <ChangeDetails run={run} />
    </section>
  );
}

function ChangeDetails({ run }: { run: Run }) {
  return (
    <>
      {run.changes && (
        <details className="event-changes" open>
          <summary>
            Изменения плана <span className="counter">{run.changes.length}</span>
          </summary>
          {run.changes.length === 0 ? (
            <p>Назначения и время работ не изменились.</p>
          ) : (
            <ul>
              {run.changes.map((change) => (
                <li key={change.request_id}>
                  <div>
                    <strong>{change.request_id}</strong>
                    <small>{change.address}</small>
                  </div>
                  <div>
                    {change.changes.map((c) => changeNames[c]).join(' · ')}
                    <small>
                      {change.before
                        ? `${engineerName(run.engineers.find((e) => e.id === change.before!.engineer_id))}, ${time(change.before.start_s)}`
                        : 'Без назначения'}{' '}
                      →{' '}
                      {change.after
                        ? `${engineerName(run.engineers.find((e) => e.id === change.after!.engineer_id))}, ${time(change.after.start_s)}`
                        : change.changes.includes('cancelled')
                          ? 'Отменена'
                          : 'Без назначения'}
                    </small>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </details>
      )}
    </>
  );
}
