import { useEffect, useId, useRef, useState } from 'react';
import type { AddressSuggestion, GeoPoint } from './types';
import { AddressPointMap } from './AddressPointMap';

async function post<T>(path: string, body: unknown, signal: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });
  const result = await response.json();
  if (!response.ok)
    throw new Error(typeof result.detail === 'string' ? result.detail : 'Не удалось найти адрес');
  return result;
}

export function AddressSuggestions({
  value,
  label,
  originalAddress = '',
  disabled = false,
  required = false,
  autoSearch = false,
  autoResolve = false,
  selectedPoint = null,
  previewMap = false,
  showSelection = true,
  onChange,
  onSelect,
}: {
  value: string;
  label: string;
  originalAddress?: string;
  disabled?: boolean;
  required?: boolean;
  autoSearch?: boolean;
  autoResolve?: boolean;
  selectedPoint?: GeoPoint | null;
  previewMap?: boolean;
  showSelection?: boolean;
  onChange: (value: string) => void;
  onSelect: (suggestion: AddressSuggestion) => void;
}) {
  const inputId = useId();
  const listId = `${inputId}-options`;
  const input = useRef<HTMLInputElement>(null);
  const sequence = useRef(0);
  const request = useRef<AbortController | null>(null);
  const [active, setActive] = useState(autoSearch);
  const [open, setOpen] = useState(false);
  const [highlighted, setHighlighted] = useState(-1);
  const [retry, setRetry] = useState(0);
  const [items, setItems] = useState<AddressSuggestion[]>([]);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [liveAvailable, setLiveAvailable] = useState(false);
  const [searched, setSearched] = useState(false);
  const [automatic, setAutomatic] = useState(false);
  const showOptions = open && !disabled && items.length > 0;
  const onSelectRef = useRef(onSelect);
  useEffect(() => {
    onSelectRef.current = onSelect;
  }, [onSelect]);

  useEffect(() => {
    if (!showOptions || highlighted < 0) return;
    // Scroll the dropdown only, without moving the sidebar or the page.
    const option = document.getElementById(`${listId}-${highlighted}`);
    const list = option?.parentElement;
    if (!option || !list) return;
    if (option.offsetTop < list.scrollTop) list.scrollTop = option.offsetTop;
    else if (option.offsetTop + option.offsetHeight > list.scrollTop + list.clientHeight)
      list.scrollTop = option.offsetTop + option.offsetHeight - list.clientHeight;
  }, [highlighted, showOptions, listId]);

  useEffect(
    () => () => {
      sequence.current++;
      request.current?.abort();
    },
    [],
  );

  useEffect(() => {
    const version = ++sequence.current;
    request.current?.abort();
    if (!active || disabled || value.trim().length < 3 || value.trim().length > 300) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    const timer = setTimeout(() => {
      post<{
        items: AddressSuggestion[];
        notice: string;
        live_available: boolean;
        automatic?: AddressSuggestion | null;
      }>(
        '/api/address-suggestions',
        {
          query: value.trim(),
          original_address: originalAddress,
          auto_resolve: autoResolve,
        },
        controller.signal,
      )
        .then((result) => {
          if (version !== sequence.current) return;
          setItems(result.items.filter((item) => item.precision === 'house'));
          setHighlighted(-1);
          setNotice(result.notice);
          setLiveAvailable(result.live_available);
          setSearched(true);
          setError('');
          if (autoResolve && result.automatic?.precision === 'house' && result.automatic.point) {
            setAutomatic(true);
            setItems([]);
            setActive(false);
            setOpen(false);
            setNotice('');
            onSelectRef.current(result.automatic);
          }
        })
        .catch((e) => {
          if (version === sequence.current && e.name !== 'AbortError')
            setError(e.message || 'Не удалось выполнить поиск');
        })
        .finally(() => {
          if (version === sequence.current) setLoading(false);
        });
    }, 600);
    return () => {
      clearTimeout(timer);
      controller.abort();
      sequence.current++;
    };
  }, [value, originalAddress, active, disabled, retry, autoResolve]);

  function change(next: string) {
    sequence.current++;
    request.current?.abort();
    setItems([]);
    setHighlighted(-1);
    setOpen(true);
    setSearched(false);
    setError('');
    setNotice('');
    setAutomatic(false);
    setActive(true);
    onChange(next);
  }

  async function select(item: AddressSuggestion) {
    if (disabled || loading || item.precision !== 'house') return;
    setOpen(false);
    const version = ++sequence.current;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError('');
    try {
      const result = await post<AddressSuggestion>(
        '/api/address-suggestions/resolve',
        { suggestion_id: item.id },
        controller.signal,
      );
      if (version !== sequence.current) return;
      if (result.precision !== 'house' || !result.point)
        throw new Error(result.notice || 'Точные координаты дома не подтверждены.');
      setItems([]);
      setActive(false);
      setNotice('');
      setAutomatic(false);
      onSelect(result);
    } catch (e) {
      if (version === sequence.current && (e as Error).name !== 'AbortError')
        setError((e as Error).message);
    } finally {
      if (version === sequence.current) setLoading(false);
    }
  }

  return (
    <div className="address-suggestions">
      <label htmlFor={inputId}>{label}</label>
      <div className="address-search-row">
        <div className="address-input-wrap">
          <input
            id={inputId}
            ref={input}
            value={value}
            required={required}
            minLength={3}
            maxLength={500}
            disabled={disabled}
            autoComplete="off"
            placeholder="Город, улица, дом"
            role="combobox"
            aria-autocomplete="list"
            aria-expanded={showOptions}
            aria-controls={showOptions ? listId : undefined}
            aria-activedescendant={
              showOptions && highlighted >= 0 ? `${listId}-${highlighted}` : undefined
            }
            onFocus={() => setOpen(true)}
            onBlur={() => {
              setOpen(false);
              setHighlighted(-1);
            }}
            onChange={(event) => change(event.target.value)}
            onKeyDown={(event) => {
              if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && items.length) {
                event.preventDefault();
                setOpen(true);
                const next =
                  event.key === 'ArrowDown'
                    ? (highlighted + 1) % items.length
                    : (highlighted <= 0 ? items.length : highlighted) - 1;
                setHighlighted(next);
              }
              if (event.key === 'Enter') {
                event.preventDefault();
                if (showOptions && highlighted >= 0) {
                  void select(items[highlighted]);
                  return;
                }
                setOpen(true);
                setActive(true);
                setRetry((v) => v + 1);
              }
              if (event.key === 'Escape') {
                event.preventDefault();
                event.stopPropagation();
                sequence.current++;
                request.current?.abort();
                setActive(false);
                setItems([]);
                setOpen(false);
                setHighlighted(-1);
                setLoading(false);
              }
            }}
          />
          {showOptions && (
            <ul
              id={listId}
              className="address-options"
              role="listbox"
              aria-label="Подсказки адреса"
            >
              {items.map((item, index) => (
                <li
                  key={item.id}
                  id={`${listId}-${index}`}
                  role="option"
                  aria-selected={highlighted === index}
                  aria-disabled={loading}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => void select(item)}
                >
                  <strong>{item.label}</strong>
                  <span>
                    Дом подтверждён · {item.provider === 'dadata' ? 'DaData' : 'Справочник'}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <button
          type="button"
          className="secondary"
          disabled={disabled || loading || value.trim().length < 3 || value.trim().length > 300}
          onClick={() => {
            input.current?.focus();
            setOpen(true);
            setActive(true);
            setRetry((v) => v + 1);
          }}
        >
          Найти адрес
        </button>
      </div>
      <div className="address-feedback" aria-live="polite">
        {loading && <p>Ищем адрес…</p>}
        {value.length > 300 && <p>Для поиска сократите адрес до 300 символов.</p>}
        {notice && <p>{notice}</p>}
        {searched && !items.length && !loading && !selectedPoint && (
          <p>Подтверждённых домов не найдено. Уточните город, улицу и номер дома.</p>
        )}
      </div>
      {error && (
        <p className="map-error" role="alert">
          {error}
        </p>
      )}
      {selectedPoint && showSelection && (
        <div className="address-selected">
          <strong>
            {automatic ? 'Точка найдена автоматически: ' : 'Выбран дом: '}
            {selectedPoint.label}
          </strong>
          {automatic && <span>Город, улица и полный номер дома совпали.</span>}
          <span>Проверьте выбранную точку перед сохранением.</span>
          {previewMap && <AddressPointMap point={selectedPoint} />}
        </div>
      )}
      {(liveAvailable || selectedPoint?.source.startsWith('DaData:')) && (
        <p className="address-credit">
          Подсказки{' '}
          <a href="https://dadata.ru/suggestions/" target="_blank" rel="noopener noreferrer">
            DaData
          </a>
          {' · '}Координаты © OpenStreetMap contributors
        </p>
      )}
    </div>
  );
}
