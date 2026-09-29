import { useEffect, useRef, useState } from 'react';
import type { AddressReview, GeoLocation, GeoPoint } from './types';
import { AddressSuggestions } from './AddressSuggestions';
import { AddressPointMap } from './AddressPointMap';
import { Icon } from './icons';
import { locationAddress } from './format';
import './AddressReviewDialog.css';

function AddressEditor({
  record,
  center,
  onSaved,
  onBusy,
  onDirty,
}: {
  record: GeoLocation;
  center: GeoPoint | null;
  onSaved: (record: GeoLocation) => void;
  onBusy: (busy: boolean) => void;
  onDirty: (dirty: boolean) => void;
}) {
  const [query, setQuery] = useState(record.point?.label ?? record.address);
  const [lat, setLat] = useState(record.point ? String(record.point.lat) : '');
  const [lon, setLon] = useState(record.point ? String(record.point.lon) : '');
  const [selection, setSelection] = useState<GeoPoint | null>(record.point);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  const valid =
    lat.trim() !== '' &&
    lon.trim() !== '' &&
    Number.isFinite(Number(lat)) &&
    Number.isFinite(Number(lon)) &&
    Math.abs(Number(lat)) <= 85 &&
    Math.abs(Number(lon)) <= 180;
  const point = valid
    ? (selection ?? {
        lat: Number(lat),
        lon: Number(lon),
        label: query.trim() || record.address,
        precision: 'manual' as const,
        source: 'dispatcher',
      })
    : null;
  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!valid || saving) return;
    setSaving(true);
    onBusy(true);
    setError('');
    const request = new AbortController();
    controller.current = request;
    try {
      const response = await fetch(`/api/geography/${encodeURIComponent(record.location_id)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        signal: request.signal,
        body: JSON.stringify({
          address: record.address,
          expected_revision: record.revision,
          lat: Number(lat),
          lon: Number(lon),
          selected_point: point,
          note: record.note,
        }),
      });
      const result = await response.json();
      if (!response.ok)
        throw new Error(
          typeof result.detail === 'string' ? result.detail : 'Не удалось сохранить адрес.',
        );
      onDirty(false);
      onSaved(result);
    } catch (e) {
      if (!request.signal.aborted) setError((e as Error).message);
    } finally {
      if (!request.signal.aborted) {
        setSaving(false);
        onBusy(false);
      }
    }
  }
  return (
    <form className="address-review-editor" onSubmit={save}>
      <div className="address-review-fields">
        <AddressSuggestions
          label="Правильный адрес"
          value={query}
          originalAddress={record.address}
          autoSearch={!record.point}
          autoResolve={!record.point && !lat && !lon}
          selectedPoint={selection}
          showSelection={false}
          disabled={saving}
          onChange={(value) => {
            setQuery(value);
            setSelection(null);
            setLat('');
            setLon('');
            onDirty(true);
          }}
          onSelect={(item) => {
            setQuery(item.label);
            setSelection(item.point);
            setLat(String(item.point!.lat));
            setLon(String(item.point!.lon));
            onDirty(true);
          }}
        />
        <p className="address-map-instruction">
          Выберите дом в подсказках или отметьте точку на карте.
        </p>
        <AddressPointMap
          point={point}
          center={record.candidates[0] ?? center}
          disabled={saving}
          onPick={(latitude, longitude) => {
            setLat(latitude.toFixed(6));
            setLon(longitude.toFixed(6));
            setSelection(null);
            onDirty(true);
          }}
        />
        <details className="address-manual-coordinates">
          <summary>Ввести координаты</summary>
          <div className="coordinate-inputs">
            <label>
              Широта
              <input
                type="number"
                step="any"
                min="-85"
                max="85"
                value={lat}
                disabled={saving}
                onChange={(e) => {
                  setLat(e.target.value);
                  setSelection(null);
                  onDirty(true);
                }}
              />
            </label>
            <label>
              Долгота
              <input
                type="number"
                step="any"
                min="-180"
                max="180"
                value={lon}
                disabled={saving}
                onChange={(e) => {
                  setLon(e.target.value);
                  setSelection(null);
                  onDirty(true);
                }}
              />
            </label>
          </div>
        </details>
      </div>
      {error && (
        <p className="map-error" role="alert">
          {error}
        </p>
      )}
      <button type="submit" className="primary address-review-save" disabled={!valid || saving}>
        {saving ? 'Сохраняем…' : 'Сохранить адрес и перейти дальше'}
      </button>
    </form>
  );
}

export function AddressReviewDialog({
  review,
  onContinue,
  onCancel,
  onSaved,
  returnFocus,
}: {
  review: AddressReview;
  onContinue: (excluded: string[]) => void;
  onCancel: () => void;
  onSaved: () => void;
  returnFocus: HTMLElement | null;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [records, setRecords] = useState(review.geography);
  const [excluded, setExcluded] = useState(new Set(review.excluded_request_ids));
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const jobsAt = (key: string) => review.requests.filter((job) => job.location_id === key);
  const [keys] = useState(() =>
    Object.keys(review.geography).filter(
      (key) =>
        !review.geography[key].point ||
        review.requests.some((job) => job.location_id === key && excluded.has(job.id)),
    ),
  );
  const required = (key: string, locations = records, skipped = excluded) =>
    !locations[key].point &&
    (key === review.office_location_id || jobsAt(key).some((job) => !skipped.has(job.id)));
  const remaining = keys.filter((key) => required(key));
  const [selected, setSelected] = useState<string | null>(
    () => keys.find((key) => required(key)) ?? null,
  );
  const current = selected ? records[selected] : null;
  const isOffice = selected === review.office_location_id;
  const currentJobs = selected ? jobsAt(selected) : [];
  const discarded = !isOffice && currentJobs.every((job) => excluded.has(job.id));
  const keptCount = review.requests.length - excluded.size;
  useEffect(() => {
    const element = dialog.current!;
    const opener = returnFocus;
    element.showModal();
    return () => {
      element.close();
      requestAnimationFrame(() => {
        if (opener?.isConnected) opener.focus({ preventScroll: true });
      });
    };
  }, []);
  function advance(locations: typeof records, skipped: Set<string>) {
    setDirty(false);
    setSelected(keys.find((key) => required(key, locations, skipped)) ?? null);
  }
  function toggle(id: string) {
    if (saving) return;
    const next = new Set(excluded);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setExcluded(next);
    if (selected && !required(selected, records, next)) advance(records, next);
  }
  return (
    <dialog
      ref={dialog}
      className="address-review-dialog"
      aria-labelledby="address-review-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!saving) onCancel();
      }}
    >
      <header className="address-review-heading">
        <div>
          <h2 id="address-review-title">Уточните адреса</h2>
          <p aria-live="polite">
            {remaining.length
              ? `Осталось проверить: ${remaining.length}`
              : 'Все адреса готовы к расчёту'}
          </p>
        </div>
        <button
          className="address-review-close"
          aria-label="Закрыть проверку адресов"
          disabled={saving}
          onClick={onCancel}
        >
          <Icon name="close" />
        </button>
      </header>
      <div className="address-review-body">
        <ul className="address-review-list" aria-label="Адреса для уточнения">
          {keys.map((key) => {
            const office = key === review.office_location_id;
            const jobs = jobsAt(key);
            const omitted = !office && jobs.every((job) => excluded.has(job.id));
            return (
              <li key={key}>
                <button
                  disabled={saving}
                  aria-pressed={selected === key}
                  onClick={() => {
                    setDirty(false);
                    setSelected(key);
                  }}
                >
                  <span className="address-review-kind">
                    {office ? 'Офис' : jobs.map((job) => `#${job.id.split(':').pop()}`).join(', ')}
                  </span>
                  <strong>{locationAddress(records[key])}</strong>
                  <span
                    className={`address-review-state ${omitted ? 'excluded' : records[key].point ? 'ready' : ''}`}
                  >
                    <Icon
                      name={omitted ? 'cancel' : records[key].point ? 'check' : 'pin'}
                      size={14}
                    />
                    {omitted
                      ? 'Исключено из расчёта'
                      : records[key].point
                        ? 'Адрес уточнён'
                        : 'Нужно уточнить'}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
        <div className="address-review-detail">
          {current ? (
            <>
              <div className="address-review-current">
                <span>
                  {isOffice ? 'Адрес офиса' : current.point ? 'Адрес заявки' : 'Адрес из файла'}
                </span>
                <h3>{locationAddress(current)}</h3>
              </div>
              {!discarded && (
                <AddressEditor
                  key={`${current.location_id}:${current.revision}`}
                  record={current}
                  center={records[review.office_location_id]?.point ?? null}
                  onBusy={setSaving}
                  onDirty={setDirty}
                  onSaved={(record) => {
                    const updated = { ...records, [record.location_id]: record };
                    setRecords(updated);
                    setSaving(false);
                    advance(updated, excluded);
                    onSaved();
                  }}
                />
              )}
              {!isOffice && (
                <div className="address-review-exclusions">
                  {currentJobs.map((job) => (
                    <button
                      key={job.id}
                      className="secondary"
                      disabled={saving}
                      onClick={() => toggle(job.id)}
                    >
                      <Icon name={excluded.has(job.id) ? 'back' : 'cancel'} size={16} />
                      {excluded.has(job.id) ? 'Вернуть заявку' : 'Исключить заявку'} #
                      {job.id.split(':').pop()}
                    </button>
                  ))}
                </div>
              )}
            </>
          ) : (
            <div className="address-review-ready">
              <Icon name="check" size={36} />
              <h3>Можно строить план</h3>
              <p>
                В расчёте: {keptCount} · Исключено: {excluded.size}
              </p>
            </div>
          )}
        </div>
      </div>
      <footer className="address-review-footer">
        <button className="secondary" disabled={saving} onClick={onCancel}>
          Вернуться без расчёта
        </button>
        <button
          className="primary"
          disabled={remaining.length > 0 || saving || dirty}
          onClick={() => onContinue([...excluded])}
        >
          Продолжить расчёт
        </button>
      </footer>
    </dialog>
  );
}
