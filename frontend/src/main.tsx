import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from 'react';
import { createRoot } from 'react-dom/client';
import type {
  CalculationProgress,
  AddressReview,
  EngineerInput,
  DayEvent,
  GeoLocation,
  Preview,
  Run,
  Scenario,
  WorkRequest,
  UploadedData,
  EngineerFile,
} from './types';
import { FileInput, EngineerFileInput } from './FileInput';
import {
  TeamEditor,
  draftEngineer,
  engineerInput,
  validEngineer,
  readSavedTeam,
  savedTeamKey,
  type EngineerDraft,
} from './TeamEditor';
import { RequestDetails } from './RequestDetails';
import { AddressReviewDialog } from './AddressReviewDialog';
import { ExportMenu } from './ExportMenu';
import { CalculationTiming } from './CalculationTiming';
import { EventPanel } from './EventPanel';
import { MapPanel } from './MapPanel';
import { time, requestAddress, locationAddress, engineerName } from './format';
import { EngineerTimeline } from './EngineerTimeline';
import { EngineerList } from './EngineerList';
import type { RouteHighlight } from './routeHighlight';
import { WorkspaceDivider } from './WorkspaceDivider';
import { Icon, type IconName } from './icons';
import './style.css';
import './workspace.css';
import './planning.css';

class ApiError extends Error {
  constructor(public body: { detail?: string; code?: string; review?: AddressReview }) {
    super(typeof body.detail === 'string' ? body.detail : 'Не удалось выполнить запрос');
  }
}

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) throw new ApiError(body);
  return body;
}

const savedSessionKey = 'dispatch-session';
const savedUploadKey = 'dispatch-upload';
function rememberSession(id: string | null) {
  try {
    if (id) localStorage.setItem(savedSessionKey, id);
    else localStorage.removeItem(savedSessionKey);
  } catch {
    /* A disabled browser store must not affect the accepted plan. */
  }
}

const emptyScenario: Scenario = {
  id: '',
  date: '',
  office_address: '',
  requests: [],
  geography: {},
  import_report: { accepted: 0, rejected: 0, source_file: '', source_sha256: '' },
};

const workspaceTabs: { id: string; label: string; icon: IconName }[] = [
  { id: 'plan', label: 'План', icon: 'route' },
  { id: 'urgent', label: 'Заявка', icon: 'plus' },
  { id: 'requests', label: 'Заявки', icon: 'list' },
  { id: 'engineers', label: 'Инженеры', icon: 'engineer' },
];

function App() {
  const [engineersHeight, setEngineersHeight] = useState<number | null>(null);
  const [toolsWidth, setToolsWidth] = useState<number | null>(null);
  const [toolsCollapsed, setToolsCollapsed] = useState(false);
  const [tab, setTab] = useState('plan');
  const [addressReview, setAddressReview] = useState<{
    data: AddressReview;
    engineers: EngineerInput[];
  } | null>(null);
  const [excludedRequests, setExcludedRequests] = useState<string[]>([]);
  const [catalogVersion, setCatalogVersion] = useState(0);
  const planningButton = useRef<HTMLButtonElement>(null);
  const [cancelTarget, setCancelTarget] = useState<string | null>(null);
  const [unavailableTarget, setUnavailableTarget] = useState<string | null>(null);
  const toolsContent = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    toolsContent.current?.scrollTo({ top: 0 });
  }, [tab, cancelTarget, unavailableTarget]);
  const [mapRoute, setMapRoute] = useState<string | null>(null);
  const [showUnassigned, setShowUnassigned] = useState(true);
  const [routeHighlight, setRouteHighlight] = useState<RouteHighlight | null>(null);
  const [pinnedRoute, setPinnedRoute] = useState<RouteHighlight | null>(null);
  const [visibility, setVisibility] = useState<{ session: string; hidden: string[] }>({
    session: '',
    hidden: [],
  });
  const [uploadId, setUploadId] = useState<string | null>(null);
  const [inputReady, setInputReady] = useState(false);
  const restoreEpoch = useRef(0);
  const teamRestoreEpoch = useRef(0);
  const [teamDraft, setTeamDraft] = useState(readSavedTeam);
  const { team, source: teamSource } = teamDraft;
  const [teamReady, setTeamReady] = useState(team.length > 0);
  const setTeam = (updated: EngineerDraft[]) => setTeamDraft((old) => ({ ...old, team: updated }));
  useEffect(() => {
    try {
      localStorage.setItem(savedTeamKey, JSON.stringify(teamDraft));
    } catch {
      /* optional */
    }
  }, [teamDraft]);
  const [scenario, setScenario] = useState<Scenario | null>(null);
  const availableTeam = team.filter((e) => e.enabled);
  const count = availableTeam.length;
  const validTeam =
    teamReady &&
    team.length > 0 &&
    team.length <= 30 &&
    new Set(team.map((e) => e.id)).size === team.length &&
    availableTeam.every(validEngineer);
  const [run, setRun] = useState<Run | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<WorkRequest | null>(null);
  const [catalog, setCatalog] = useState<Record<string, GeoLocation>>({});
  const [filter, setFilter] = useState('all');
  const [search, setSearch] = useState('');
  const [kindFilter, setKindFilter] = useState('all');
  const [engineerFilter, setEngineerFilter] = useState('');
  const [requestSort, setRequestSort] = useState('window');
  const [mapFocus, setMapFocus] = useState<{ requestId: string; token: number } | null>(null);
  const requestOpener = useRef<HTMLElement | null>(null);
  const selectRequest = useCallback(
    (job: WorkRequest) => {
      requestOpener.current =
        document.activeElement instanceof HTMLElement ? document.activeElement : null;
      setSelected(job);
      setMapFocus(null);
      setTab('requests');
      setToolsCollapsed(false);
      if (!busy && !preview) setCancelTarget(null);
    },
    [busy, preview],
  );
  function closeRequest() {
    setSelected(null);
    requestAnimationFrame(() => {
      const opener = requestOpener.current;
      if (opener?.isConnected && opener.getClientRects().length)
        opener.focus({ preventScroll: true });
      else
        document
          .querySelector<HTMLElement>('#pane-requests .search')
          ?.focus({ preventScroll: true });
    });
  }
  const [calculationProgress, setCalculationProgress] = useState<CalculationProgress | null>(null);
  useEffect(() => {
    if (!busy || !calculationProgress?.id || calculationProgress.status !== 'running') return;
    let live = true;
    const id = calculationProgress.id;
    const update = () =>
      api<CalculationProgress>(`/api/calculations/${id}`)
        .then((value) => {
          if (live)
            setCalculationProgress((old) =>
              old?.id === id ? { ...value, percent: Math.max(old.percent, value.percent) } : old,
            );
        })
        .catch(() => {});
    update();
    const timer = setInterval(update, 600);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [busy, calculationProgress?.id, calculationProgress?.status]);
  const isUrgent = (r?: WorkRequest) =>
    scenario?.objective_policy ? r?.work_type === 'emergency' : Boolean(r?.urgent);

  function acceptRun(result: Run) {
    setExcludedRequests(result.manifest.excluded_request_ids ?? []);
    setAddressReview(null);
    setPreview(null);
    setCancelTarget(null);
    setUnavailableTarget(null);
    setRun(result);
    setScenario(result.scenario);
    setSelected(null);
    setFilter('all');
    setSearch('');
    setKindFilter('all');
    setEngineerFilter('');
    setMapFocus(null);
    rememberSession(result.session_id);
  }

  function acceptUpload(data: UploadedData) {
    setInputReady(true);
    const source = data.scenario.import_report;
    // Validation creates a new upload ID even for the same file. Keep the active
    // plan, its original upload and edited settings when only rechecking it.
    if (
      uploadId &&
      source.source_sha256 &&
      source.source_sha256 === scenario?.import_report.source_sha256 &&
      source.source_file === scenario.import_report.source_file
    )
      return;
    setUploadId(data.upload_id);
    setExcludedRequests([]);
    setAddressReview(null);
    setScenario(data.scenario);
    setRun(null);
    setPreview(null);
    setCancelTarget(null);
    setUnavailableTarget(null);
    setSelected(null);
    setError('');
    setFilter('all');
    setSearch('');
    setKindFilter('all');
    setEngineerFilter('');
    setMapFocus(null);
    setCalculationProgress(null);
    rememberSession(null);
    try {
      localStorage.setItem(savedUploadKey, data.upload_id);
    } catch {
      /* optional */
    }
  }

  function acceptEngineers(data: EngineerFile) {
    setTeamReady(true);
    if (
      data.report.source_sha256 === teamSource?.source_sha256 &&
      data.report.source_file === teamSource.source_file
    )
      return;
    setTeamDraft({ team: data.engineers.map(draftEngineer), source: data.report });
  }

  function startCalculation() {
    const id = crypto.randomUUID();
    setCalculationProgress({ id, stage: 'Подготовка расчёта', percent: 0, status: 'running' });
    return id;
  }
  function finishCalculation(success: boolean) {
    setCalculationProgress((old) =>
      old
        ? {
            ...old,
            stage: success ? 'Готово' : 'Расчёт не завершён',
            percent: success ? 100 : old.percent,
            status: success ? 'completed' : 'failed',
          }
        : null,
    );
  }

  useEffect(() => {
    let live = true;
    const epoch = restoreEpoch.current;
    const teamEpoch = teamRestoreEpoch.current;
    const current = () => live && restoreEpoch.current === epoch;
    async function restore() {
      let session: string | null = null,
        upload: string | null = null;
      try {
        session = localStorage.getItem(savedSessionKey);
        upload = localStorage.getItem(savedUploadKey);
      } catch {
        /* optional */
      }
      if (session) {
        try {
          const previous = await api<Run>(`/api/sessions/${encodeURIComponent(session)}`);
          if (!current()) return;
          acceptRun(previous);
          upload = previous.scenario.upload_id ?? null;
          if (upload) {
            await api<UploadedData>(`/api/uploads/${upload}`);
            if (!current()) return;
            setUploadId(upload);
            setInputReady(true);
            if (!team.length && teamRestoreEpoch.current === teamEpoch) {
              const inputs = previous.manifest.engineer_inputs ?? previous.engineers;
              setTeam(inputs.map(draftEngineer));
              setTeamReady(inputs.length > 0);
            }
          }
          return;
        } catch {
          if (!current()) return;
          rememberSession(null);
        }
      }
      if (upload) {
        try {
          const data = await api<UploadedData>(`/api/uploads/${upload}`);
          if (current()) acceptUpload(data);
        } catch (e) {
          if (current()) setError((e as Error).message);
        }
      }
    }
    restore();
    return () => {
      live = false;
    };
  }, []);

  async function calculate(
    excluded = excludedRequests,
    chosenEngineers = availableTeam.map(engineerInput),
  ) {
    if (!inputReady || !uploadId || !validTeam) return;
    setAddressReview(null);
    setExcludedRequests(excluded);
    const calculationId = startCalculation();
    setBusy(true);
    setError('');
    setSelected(null);
    try {
      const result = await api<Run>(`/api/uploads/${uploadId}/optimize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Calculation-ID': calculationId },
        body: JSON.stringify({ engineers: chosenEngineers, excluded_request_ids: excluded }),
      });
      acceptRun(result);
      finishCalculation(true);
    } catch (e) {
      if (e instanceof ApiError && e.body.code === 'address_review_required' && e.body.review) {
        setCalculationProgress(null);
        setAddressReview({ data: e.body.review, engineers: chosenEngineers });
        setCatalogVersion((v) => v + 1);
      } else {
        setError((e as Error).message);
        finishCalculation(false);
      }
    } finally {
      setBusy(false);
    }
  }

  async function applyEvent(event: DayEvent) {
    if (!run) return;
    setBusy(true);
    setError('');
    setPreview(null);
    const calculationId = startCalculation();
    try {
      const result = await api<Preview>(`/api/sessions/${run.session_id}/events/preview`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Calculation-ID': calculationId },
        body: JSON.stringify(event),
      });
      setPreview(result);
      finishCalculation(true);
    } catch (e) {
      setError((e as Error).message);
      finishCalculation(false);
    } finally {
      setBusy(false);
    }
  }

  async function commitDayAction(path: string, payload: object) {
    if (!run) return;
    setBusy(true);
    setError('');
    try {
      acceptRun(
        await api<Run>(`/api/sessions/${run.session_id}/${path}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        }),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const requests = scenario?.requests ?? [];
  const displayGeography = (run ? scenario?.geography : catalog) ?? {};
  const address = (request: WorkRequest) => requestAddress(request, displayGeography);
  const officeRecord = scenario?.office_location_id
    ? displayGeography[scenario.office_location_id]
    : undefined;
  const activePlan = run?.plan;
  const assigned = new Map(
    activePlan?.routes.flatMap((route) =>
      route.visits.map(
        (visit) => [visit.request_id, { visit, engineer: route.engineer_id }] as const,
      ),
    ),
  );
  const rejected = new Map(activePlan?.unassigned.map((r) => [r.request_id, r]));
  const visible = requests
    .filter(
      (r) =>
        (filter === 'all' || (filter === 'unassigned' ? rejected.has(r.id) : assigned.has(r.id))) &&
        (kindFilter === 'all' || r.kind === kindFilter) &&
        (engineerFilter === '' || assigned.get(r.id)?.engineer === engineerFilter) &&
        `${r.id} ${address(r)}`
          .toLocaleLowerCase('ru')
          .includes(search.trim().toLocaleLowerCase('ru')),
    )
    .sort((a, b) => {
      if (requestSort === 'address')
        return address(a).localeCompare(address(b), 'ru', { numeric: true });
      if (requestSort === 'id') return a.id.localeCompare(b.id, 'ru', { numeric: true });
      if (requestSort === 'priority') {
        const priority = (r: WorkRequest) =>
          r.work_type === 'emergency' || (!scenario?.objective_policy && r.urgent)
            ? 0
            : r.work_type === 'installation'
              ? 1
              : 2;
        return priority(a) - priority(b) || a.window_start_s - b.window_start_s;
      }
      return a.window_start_s - b.window_start_s || a.source_order - b.source_order;
    });
  const metrics = activePlan?.metrics;

  const hiddenEngineers = useMemo(
    () => new Set(visibility.session === run?.session_id ? visibility.hidden : []),
    [visibility, run?.session_id],
  );
  const activeRouteHighlight = pinnedRoute ?? routeHighlight;
  useEffect(() => {
    setRouteHighlight(null);
    setPinnedRoute(null);
  }, [activePlan, mapRoute, hiddenEngineers]);
  useEffect(() => {
    const clear = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setPinnedRoute(null);
        setRouteHighlight(null);
      }
    };
    window.addEventListener('keydown', clear);
    return () => window.removeEventListener('keydown', clear);
  }, []);
  useEffect(() => {
    setMapRoute(null);
  }, [run?.session_id, scenario?.id]);
  function focusRoute(id: string) {
    setMapFocus(null);
    setMapRoute(id);
    if (run) {
      setVisibility({
        session: run.session_id,
        hidden: run.engineers.filter((e) => e.id !== id).map((e) => e.id),
      });
    }
  }
  function toggleRoute(id: string) {
    setMapFocus(null);
    const hidden = new Set(hiddenEngineers);
    if (hidden.has(id)) hidden.delete(id);
    else hidden.add(id);
    setVisibility({ session: run!.session_id, hidden: [...hidden] });
    setMapRoute(null);
  }
  const previewTab =
    preview?.result.event?.type === 'cancel'
      ? 'requests'
      : preview?.result.event?.type === 'engineer_unavailable'
        ? 'engineers'
        : 'urgent';
  const eventPanelProps = run
    ? {
        run,
        busy,
        onApply: applyEvent,
        onDiscard: () => setPreview(null),
        onCommit: () =>
          preview && commitDayAction('events/commit', { preview_id: preview.preview_id }),
      }
    : null;
  const actionLocked = busy || !!preview || !!addressReview;
  const frozenRequests = new Set(run?.frozen_request_ids ?? []);
  const showAllRoutes = useCallback(() => {
    setMapFocus(null);
    setVisibility({ session: run?.session_id ?? '', hidden: [] });
    setMapRoute(null);
  }, [run?.session_id]);
  function showRequests(status: string) {
    openTool('requests');
    setSelected(null);
    if (!actionLocked) setCancelTarget(null);
    setFilter(status);
    setSearch('');
    setKindFilter('all');
    setEngineerFilter('');
  }
  function openTool(id: string) {
    setToolsCollapsed(false);
    setTab(id);
    if (preview && previewTab === 'requests' && id === 'requests') {
      setSelected(null);
    }
  }
  return (
    <div
      className={`dispatch-shell${toolsCollapsed ? ' tools-collapsed' : ''}`}
      onClickCapture={(event) => {
        // Leaflet distinguishes a map click from dragging before clearing a selection.
        if (event.target instanceof Element && event.target.closest('.route-map')) return;
        setPinnedRoute(null);
      }}
      style={
        {
          '--engineers-height': engineersHeight === null ? undefined : `${engineersHeight}px`,
          '--tools-width': toolsWidth === null ? undefined : `${toolsWidth}px`,
        } as CSSProperties
      }
    >
      <header className="dispatch-header">
        <button
          className="tools-toggle"
          aria-label={toolsCollapsed ? 'Развернуть левую панель' : 'Свернуть левую панель'}
          title={toolsCollapsed ? 'Развернуть левую панель' : 'Свернуть левую панель'}
          aria-expanded={!toolsCollapsed}
          aria-controls="tools-content"
          onClick={() => setToolsCollapsed((collapsed) => !collapsed)}
        >
          <Icon name={toolsCollapsed ? 'panelOpen' : 'panelClose'} size={21} />
        </button>
        <a className="dispatch-brand" href="/" aria-label="Выезд — главная">
          <span>
            <Icon name="route" size={22} />
          </span>
          выезд<span className="brand-period">.</span>
        </a>
        <div className="workspace-title">
          <h1>Диспетчерская</h1>
          <span>
            {scenario?.id ?? 'Новый план'} {scenario ? '·' : ''}{' '}
            {scenario
              ? new Date(`${scenario.date}T12:00:00`).toLocaleDateString('ru-RU', {
                  day: 'numeric',
                  month: 'long',
                })
              : ''}
          </span>
        </div>
        <ExportMenu
          run={run}
          disabled={busy || !!addressReview}
          visibleEngineers={(run?.plan.routes ?? [])
            .filter(
              (route) =>
                route.visits.length > 0 &&
                !hiddenEngineers.has(route.engineer_id) &&
                (mapRoute === null || route.engineer_id === mapRoute),
            )
            .map((route) => route.engineer_id)}
        />
      </header>
      <aside id="dispatch-tools" className="dispatch-tools" aria-label="Панель диспетчера">
        <nav
          className="tool-rail"
          role="tablist"
          aria-label="Разделы диспетчера"
          aria-orientation="vertical"
        >
          {workspaceTabs.map((item, index) => (
            <button
              key={item.id}
              id={`tab-${item.id}`}
              role="tab"
              aria-label={item.label}
              title={item.label}
              aria-selected={!toolsCollapsed && tab === item.id}
              aria-controls={`pane-${item.id}`}
              tabIndex={tab === item.id ? 0 : -1}
              onClick={() => openTool(item.id)}
              onKeyDown={(e) => {
                const step =
                  e.key === 'ArrowDown' || e.key === 'ArrowRight'
                    ? 1
                    : e.key === 'ArrowUp' || e.key === 'ArrowLeft'
                      ? -1
                      : 0;
                const next =
                  e.key === 'Home'
                    ? 0
                    : e.key === 'End'
                      ? workspaceTabs.length - 1
                      : (index + step + workspaceTabs.length) % workspaceTabs.length;
                if (step || e.key === 'Home' || e.key === 'End') {
                  e.preventDefault();
                  openTool(workspaceTabs[next].id);
                  document.getElementById(`tab-${workspaceTabs[next].id}`)?.focus();
                }
              }}
            >
              <Icon name={item.icon} size={21} />
              <span>{item.label}</span>
              {preview && previewTab === item.id && (
                <i className="pending-proposal" aria-label="Есть вариант для принятия" />
              )}
            </button>
          ))}
        </nav>
        <div id="tools-content" ref={toolsContent} className="tool-content" hidden={toolsCollapsed}>
          {preview && (tab !== previewTab || (previewTab === 'requests' && selected)) && (
            <button className="list-back" onClick={() => openTool(previewTab)}>
              <Icon name="back" size={16} /> К варианту изменений
            </button>
          )}
          <section
            id="pane-plan"
            role="tabpanel"
            aria-labelledby="tab-plan"
            hidden={tab !== 'plan'}
          >
            <div className="tool-heading">
              <h2>Планирование</h2>
            </div>
            <FileInput
              busy={actionLocked}
              filename={scenario?.import_report.source_file}
              onReady={acceptUpload}
              onPending={() => {
                restoreEpoch.current++;
                setInputReady(false);
              }}
            />
            <EngineerFileInput
              busy={actionLocked}
              filename={teamSource?.source_file}
              onReady={acceptEngineers}
              onPending={() => {
                teamRestoreEpoch.current++;
                setTeamReady(false);
              }}
            />
            {scenario && (
              <section className="toolbar" aria-label="Параметры расчёта">
                <TeamEditor
                  team={team}
                  disabled={actionLocked || !inputReady || !teamReady}
                  office={officeRecord ? locationAddress(officeRecord) : scenario.office_address}
                  onChange={setTeam}
                />
                <button
                  className="primary"
                  aria-label="Рассчитать план"
                  ref={planningButton}
                  disabled={actionLocked || !inputReady || !validTeam}
                  onClick={() => void calculate()}
                >
                  {busy ? 'Выполняется расчёт…' : 'Рассчитать план'}
                </button>
                {excludedRequests.length > 0 && (
                  <div className="planning-exclusions">
                    <span>Исключено заявок: {excludedRequests.length}</span>
                    <button disabled={actionLocked} onClick={() => setExcludedRequests([])}>
                      Вернуть в расчёт
                    </button>
                  </div>
                )}
              </section>
            )}
            {run && <CalculationTiming performance={run.performance} />}
          </section>
          <section
            id="pane-urgent"
            role="tabpanel"
            aria-labelledby="tab-urgent"
            hidden={tab !== 'urgent'}
          >
            <div className="tool-heading">
              <h2>Добавить заявку</h2>
            </div>
            {run && eventPanelProps ? (
              <EventPanel
                key={`${run.session_id}:${run.version}`}
                {...eventPanelProps}
                preview={previewTab === 'urgent' ? preview : null}
                blocked={!!preview && previewTab !== 'urgent'}
                requestedType="add_urgent"
              />
            ) : (
              <div className="tool-empty">
                <Icon name="clock" size={28} />
                <p>Сначала рассчитайте план.</p>
                <button className="secondary" onClick={() => setTab('plan')}>
                  Перейти к планированию
                </button>
              </div>
            )}
          </section>
          <section
            id="pane-requests"
            role="tabpanel"
            aria-labelledby="tab-requests"
            hidden={tab !== 'requests'}
          >
            <div hidden={cancelTarget !== null || selected !== null}>
              <div className="tool-heading list-heading">
                <h2>
                  Заявки <span className="counter">{requests.length}</span>
                </h2>
              </div>
              <input
                className="search"
                aria-label="Поиск заявки"
                placeholder="Найти по адресу или ID"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <div className="request-filters">
                {[
                  ['all', 'Все заявки'],
                  ['assigned', 'Назначенные'],
                  ['unassigned', 'Без назначения'],
                ].map(([value, text]) => (
                  <button
                    key={value}
                    disabled={value !== 'all' && !run}
                    aria-pressed={filter === value}
                    onClick={() => setFilter(value)}
                  >
                    {text}
                  </button>
                ))}
              </div>
              <div className="list-filters">
                <label>
                  Тип работы
                  <select
                    aria-label="Тип заявки"
                    value={kindFilter}
                    onChange={(e) => setKindFilter(e.target.value)}
                  >
                    <option value="all">Все типы</option>
                    {[...new Set(requests.map((r) => r.kind))].sort().map((kind) => (
                      <option key={kind}>{kind}</option>
                    ))}
                  </select>
                </label>
                <label>
                  Инженер
                  <select
                    aria-label="Фильтр по инженеру"
                    disabled={!run}
                    value={engineerFilter}
                    onChange={(e) => setEngineerFilter(e.target.value)}
                  >
                    <option value="">Все инженеры</option>
                    {run?.engineers.map((e) => (
                      <option key={e.id} value={e.id}>
                        {engineerName(e)}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="list-sort">
                  Порядок
                  <select
                    aria-label="Сортировка заявок"
                    value={requestSort}
                    onChange={(e) => setRequestSort(e.target.value)}
                  >
                    <option value="window">По времени клиента</option>
                    <option value="priority">По приоритету</option>
                    <option value="address">По адресу</option>
                    <option value="id">По номеру</option>
                  </select>
                </label>
              </div>
              <div className="list-summary">
                <span>{visible.length} в списке</span>
                {(search || kindFilter !== 'all' || engineerFilter !== '' || filter !== 'all') && (
                  <button onClick={() => showRequests('all')}>Сбросить фильтры</button>
                )}
              </div>
              <ul className="request-cards">
                {visible.map((r) => (
                  <li key={r.id} data-request-id={r.id}>
                    <div>
                      <button className="request-link" onClick={() => selectRequest(r)}>
                        #{r.id.split(':').pop()}
                        {isUrgent(r) && <span className="urgent-dot" />}
                      </button>
                      <span
                        className={`badge ${assigned.has(r.id) ? 'assigned' : rejected.has(r.id) ? 'rejected' : ''}`}
                      >
                        {assigned.has(r.id)
                          ? 'Назначена'
                          : rejected.has(r.id)
                            ? 'Без назначения'
                            : 'Ожидает расчёта'}
                      </span>
                    </div>
                    <button className="request-address" onClick={() => selectRequest(r)}>
                      {address(r)}
                    </button>
                    <small>
                      {r.kind} · {r.service_s / 60} мин
                    </small>
                    <small>
                      <Icon name="clock" size={12} />
                      {time(r.window_start_s)}–{time(r.window_end_s)}
                      {assigned.has(r.id) &&
                        ` · ${engineerName(run?.engineers.find((e) => e.id === assigned.get(r.id)!.engineer))}`}
                    </small>
                    {scenario?.objective_policy && !r.work_type && (
                      <small>Тип требует уточнения</small>
                    )}
                    <div className="list-item-actions">
                      <button
                        className="list-action danger-action"
                        disabled={actionLocked || !run || frozenRequests.has(r.id)}
                        title={
                          !run
                            ? 'Сначала рассчитайте план'
                            : frozenRequests.has(r.id)
                              ? 'Выезд уже начат или работа выполнена'
                              : undefined
                        }
                        onClick={() => {
                          setSelected(null);
                          setCancelTarget(r.id);
                        }}
                      >
                        <Icon name="cancel" size={14} /> Отменить
                      </button>
                      {frozenRequests.has(r.id) && <span>Выезд уже начат</span>}
                    </div>
                  </li>
                ))}
              </ul>
              {!visible.length && <p className="no-results">Нет заявок по выбранным условиям</p>}
            </div>
            {selected && scenario && (
              <div>
                <RequestDetails
                  busy={actionLocked}
                  job={selected}
                  scenario={scenario}
                  run={run}
                  geography={run ? (scenario.geography ?? {}) : catalog}
                  onClose={closeRequest}
                  onCancel={() => {
                    setCancelTarget(selected.id);
                    setSelected(null);
                  }}
                  onLocate={() => {
                    setMapFocus({ requestId: selected.id, token: Date.now() });
                  }}
                />
              </div>
            )}
            {cancelTarget && run && eventPanelProps && (
              <div hidden={selected !== null}>
                <button
                  className="list-back"
                  disabled={actionLocked}
                  onClick={() => setCancelTarget(null)}
                >
                  <Icon name="back" size={16} /> К заявкам
                </button>
                <div className="tool-heading">
                  <h2>Отмена заявки</h2>
                </div>
                <EventPanel
                  key={`${run.session_id}:${run.version}:${cancelTarget}`}
                  {...eventPanelProps}
                  targetId={cancelTarget}
                  requestedType="cancel"
                  preview={previewTab === 'requests' ? preview : null}
                  blocked={!!preview && previewTab !== 'requests'}
                />
              </div>
            )}
          </section>
          <section
            id="pane-engineers"
            role="tabpanel"
            aria-labelledby="tab-engineers"
            hidden={tab !== 'engineers'}
          >
            <div hidden={unavailableTarget !== null}>
              <div className="tool-heading">
                <h2>Инженеры</h2>
              </div>
              {run ? (
                <EngineerList
                  run={run}
                  disabled={actionLocked}
                  onUnavailable={setUnavailableTarget}
                  onFocus={focusRoute}
                />
              ) : (
                <div className="tool-empty">
                  <p>Сначала рассчитайте план.</p>
                  <button className="secondary" onClick={() => openTool('plan')}>
                    Перейти к планированию
                  </button>
                </div>
              )}
            </div>
            {unavailableTarget && run && eventPanelProps && (
              <div>
                <button
                  className="list-back"
                  disabled={actionLocked}
                  onClick={() => setUnavailableTarget(null)}
                >
                  <Icon name="back" size={16} /> К инженерам
                </button>
                <div className="tool-heading">
                  <h2>Инженер недоступен</h2>
                </div>
                <EventPanel
                  key={`${run.session_id}:${run.version}:${unavailableTarget}`}
                  {...eventPanelProps}
                  targetId={unavailableTarget}
                  requestedType="engineer_unavailable"
                  preview={previewTab === 'engineers' ? preview : null}
                  blocked={!!preview && previewTab !== 'engineers'}
                />
              </div>
            )}
          </section>
        </div>
      </aside>
      <WorkspaceDivider
        orientation="vertical"
        collapsed={toolsCollapsed}
        onCollapsedChange={setToolsCollapsed}
        onResize={setToolsWidth}
      />
      {addressReview && (
        <AddressReviewDialog
          review={addressReview.data}
          returnFocus={planningButton.current}
          onCancel={() => setAddressReview(null)}
          onSaved={() => setCatalogVersion((v) => v + 1)}
          onContinue={(excluded) => void calculate(excluded, addressReview.engineers)}
        />
      )}
      <main className="dispatch-map-area">
        <div className="map-status-strip" aria-label="Метрики плана">
          <button
            onClick={() => showRequests('all')}
            aria-label="Все заявки"
            aria-pressed={!toolsCollapsed && tab === 'requests' && filter === 'all'}
          >
            <strong>{requests.length}</strong> заявок
          </button>
          <button
            disabled={!run}
            onClick={() => showRequests('assigned')}
            aria-label="Назначенные заявки"
            aria-pressed={!toolsCollapsed && tab === 'requests' && filter === 'assigned'}
          >
            <i className="metric-dot assigned-dot" />
            <strong>{metrics?.assigned ?? '—'}</strong> назначено
          </button>
          <div className="unassigned-map-controls">
            <button
              disabled={!run}
              onClick={() => showRequests('unassigned')}
              aria-label="Неназначенные заявки"
              aria-pressed={!toolsCollapsed && tab === 'requests' && filter === 'unassigned'}
            >
              <i className="metric-dot unassigned-dot" />
              <strong>{metrics?.unassigned ?? '—'}</strong> без назначения
            </button>
            <button
              className="unassigned-visibility"
              aria-label={`${showUnassigned ? 'Скрыть' : 'Показать'} неназначенные заявки на карте`}
              title={`${showUnassigned ? 'Скрыть' : 'Показать'} неназначенные заявки на карте`}
              aria-pressed={showUnassigned}
              onClick={() => {
                setMapFocus(null);
                setShowUnassigned((value) => !value);
              }}
              disabled={!run}
            >
              <Icon name={showUnassigned ? 'eye' : 'eyeOff'} size={16} />
              <span>На карте</span>
            </button>
          </div>
          <span className="map-roster-total">
            {run?.staffing?.status === 'active'
              ? `На смене: ${run.staffing.scheduled}`
              : `Инженеров: ${metrics?.active_engineers ?? '—'} / ${run?.engineers.length ?? count ?? '—'}`}
          </span>
        </div>
        {busy && (
          <div className="workspace-progress" role="status">
            <div className="progress-content">
              <div>
                <span>
                  {calculationProgress?.status === 'running'
                    ? calculationProgress.stage
                    : 'Загрузка…'}
                </span>
                {calculationProgress?.status === 'running' && (
                  <strong>{calculationProgress.percent}%</strong>
                )}
              </div>
              {calculationProgress?.status === 'running' && (
                <div
                  className="calculation-progress-bar"
                  role="progressbar"
                  aria-label="Расчёт плана"
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={calculationProgress.percent}
                >
                  <span style={{ width: `${calculationProgress.percent}%` }} />
                </div>
              )}
            </div>
          </div>
        )}
        <MapPanel
          key={scenario?.upload_id ?? scenario?.id ?? 'empty'}
          scenario={scenario ?? emptyScenario}
          run={run}
          plan={activePlan}
          onSelect={selectRequest}
          route={mapRoute}
          hiddenEngineers={hiddenEngineers}
          showUnassigned={showUnassigned}
          highlight={activeRouteHighlight}
          pinnedHighlight={pinnedRoute}
          onHighlight={setRouteHighlight}
          onPin={setPinnedRoute}
          onCatalog={setCatalog}
          catalogVersion={catalogVersion}
          focusRequest={mapFocus}
          selectedRequestId={selected?.id}
          onOverview={showAllRoutes}
        />
        {error && (
          <div className="workspace-error" role="alert">
            {error}
            <button aria-label="Скрыть ошибку" onClick={() => setError('')}>
              <Icon name="close" size={16} />
            </button>
          </div>
        )}
      </main>
      <WorkspaceDivider onResize={setEngineersHeight} />
      <EngineerTimeline
        run={run}
        plan={activePlan}
        hidden={hiddenEngineers}
        highlight={activeRouteHighlight}
        onHighlight={setRouteHighlight}
        onToggle={toggleRoute}
        onFocus={focusRoute}
        onSelect={selectRequest}
        onShowAll={showAllRoutes}
        onHideAll={() => {
          setMapFocus(null);
          setVisibility({
            session: run?.session_id ?? '',
            hidden: run?.engineers.map((e) => e.id) ?? [],
          });
          setMapRoute(null);
        }}
      />
    </div>
  );
}

createRoot(document.getElementById('root')!).render(<App />);
