import { useEffect, useRef, useState } from 'react';
import type { UploadedData, EngineerFile } from './types';
import { Icon } from './icons';

interface Issue {
  line: number | null;
  column: string;
  message: string;
}

function rowCount(count: number, engineers: boolean) {
  const forms = engineers ? ['инженер', 'инженера', 'инженеров'] : ['заявка', 'заявки', 'заявок'];
  const form =
    count % 100 >= 11 && count % 100 <= 14
      ? 2
      : count % 10 === 1
        ? 0
        : count % 10 >= 2 && count % 10 <= 4
          ? 1
          : 2;
  return `${count} ${forms[form]}`;
}

function CsvFileInput<T>({
  busy,
  filename,
  onReady,
  onPending,
  kind,
  acceptedCount,
}: {
  busy: boolean;
  filename?: string;
  onReady: (data: T) => void;
  onPending: () => void;
  kind: 'requests' | 'engineers';
  acceptedCount: (data: T) => number;
}) {
  const engineers = kind === 'engineers';
  const [file, setFile] = useState<File | null>(null);
  const [checking, setChecking] = useState(false);
  const [issues, setIssues] = useState<Issue[]>([]);
  const [accepted, setAccepted] = useState<number | null>(null);
  const generation = useRef(0);
  useEffect(() => {
    return () => {
      generation.current++;
    };
  }, []);
  async function validate() {
    if (!file) return;
    const token = ++generation.current;
    setChecking(true);
    setIssues([]);
    setAccepted(null);
    onPending();
    try {
      const response = await fetch(
        `/api/${engineers ? 'engineers' : 'uploads'}/validate?filename=${encodeURIComponent(file.name)}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/octet-stream' },
          body: file,
        },
      );
      const data = await response.json();
      if (generation.current !== token) return;
      if (!response.ok)
        throw new Error(
          typeof data.detail === 'string'
            ? data.detail
            : 'Не удалось проверить файл. Попробуйте ещё раз.',
        );
      if (!data.valid) setIssues(data.errors);
      else {
        setAccepted(acceptedCount(data));
        onReady(data);
      }
    } catch (error) {
      if (generation.current === token)
        setIssues([{ line: null, column: '', message: (error as Error).message }]);
    } finally {
      if (generation.current === token) setChecking(false);
    }
  }
  return (
    <section
      className="file-input"
      aria-label={engineers ? 'Загрузка инженеров' : 'Загрузка заявок'}
    >
      <label className="file-picker">
        <Icon name={engineers ? 'engineer' : 'list'} size={24} />
        <span>
          {file?.name ??
            filename ??
            (engineers ? 'Выбрать CSV с инженерами' : 'Выбрать CSV с заявками')}
        </span>
        <input
          aria-label={engineers ? 'Файл инженеров' : 'Файл заявок'}
          type="file"
          accept=".csv,text/csv"
          disabled={busy || checking}
          onChange={(e) => {
            const next = e.target.files?.[0];
            if (!next) return;
            generation.current++;
            setFile(next);
            setIssues([]);
            setAccepted(null);
            onPending();
          }}
        />
      </label>
      <button
        className="secondary validate-file"
        disabled={!file || busy || checking}
        onClick={validate}
      >
        {checking ? 'Проверка файла…' : engineers ? 'Проверить инженеров' : 'Проверить файл'}
      </button>
      {accepted !== null && (
        <p className="file-valid" role="status">
          ✓ Файл проверен · {rowCount(accepted, engineers)}
        </p>
      )}
      {issues.length > 0 && (
        <div className="file-errors" role="alert">
          <strong>Исправьте файл и загрузите снова</strong>
          <ul>
            {issues.map((issue, i) => (
              <li key={i}>
                {issue.line !== null && (
                  <b>
                    Строка {issue.line}
                    {issue.column ? `, ${issue.column}` : ''}:{' '}
                  </b>
                )}
                {issue.message}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

export function FileInput(props: {
  busy: boolean;
  filename?: string;
  onReady: (data: UploadedData) => void;
  onPending: () => void;
}) {
  return (
    <CsvFileInput
      {...props}
      kind="requests"
      acceptedCount={(data) => data.scenario.requests.length}
    />
  );
}

export function EngineerFileInput(props: {
  busy: boolean;
  filename?: string;
  onReady: (data: EngineerFile) => void;
  onPending: () => void;
}) {
  return (
    <CsvFileInput {...props} kind="engineers" acceptedCount={(data) => data.engineers.length} />
  );
}
