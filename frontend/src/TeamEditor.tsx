import type { EngineerInput, EngineerFile } from './types';
import { Icon, profileIcons, profileNames } from './icons';

const skills = {
  installation: 'Подключения и дозаказы',
  local: 'Локальные работы',
  emergency: 'Аварийные работы',
};
export interface EngineerDraft {
  id: string;
  name: string;
  enabled: boolean;
  skills: string[];
  profile: string;
  start: string;
  end: string;
}
export interface TeamDraft {
  team: EngineerDraft[];
  source: EngineerFile['report'] | null;
}
export const savedTeamKey = 'dispatch-team-v1';
export function readSavedTeam(): TeamDraft {
  try {
    const draft = JSON.parse(localStorage.getItem(savedTeamKey) ?? 'null');
    if (
      draft &&
      Array.isArray(draft.team) &&
      draft.team.length <= 30 &&
      draft.team.every(
        (e: EngineerDraft) =>
          e &&
          typeof e.id === 'string' &&
          typeof e.name === 'string' &&
          typeof e.enabled === 'boolean' &&
          typeof e.start === 'string' &&
          typeof e.end === 'string' &&
          typeof e.profile === 'string' &&
          Array.isArray(e.skills) &&
          e.skills.every((s) => typeof s === 'string'),
      )
    ) {
      return {
        team: draft.team,
        source:
          typeof draft.source?.source_file === 'string' &&
          typeof draft.source?.source_sha256 === 'string'
            ? draft.source
            : null,
      };
    }
  } catch {
    /* Browser storage is optional. */
  }
  return { team: [], source: null };
}
const clock = (seconds: number) =>
  `${String(Math.floor(seconds / 3600)).padStart(2, '0')}:${String(Math.floor((seconds % 3600) / 60)).padStart(2, '0')}`;
const seconds = (value: string) =>
  Number(value.slice(0, 2)) * 3600 + Number(value.slice(3, 5)) * 60;
export const draftEngineer = (e: EngineerInput): EngineerDraft => ({
  id: e.id,
  name: e.name ?? '',
  enabled: true,
  skills: [...e.skills],
  profile: e.profile,
  start: clock(e.shift_start_s),
  end: clock(e.shift_end_s),
});
export const engineerInput = (e: EngineerDraft): EngineerInput => ({
  id: e.id,
  name: e.name.trim() || null,
  skills: e.skills,
  profile: e.profile,
  shift_start_s: seconds(e.start),
  shift_end_s: seconds(e.end),
});
export function validEngineer(e: EngineerDraft) {
  const input = engineerInput(e);
  return (
    e.id.trim().length > 0 &&
    e.id.length <= 150 &&
    e.name.length <= 150 &&
    e.skills.length > 0 &&
    e.skills.length <= 3 &&
    new Set(e.skills).size === e.skills.length &&
    e.skills.every((s) => s in skills) &&
    e.profile in profileNames &&
    /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(e.start) &&
    /^(?:(?:[01]\d|2[0-3]):[0-5]\d|24:00)$/.test(e.end) &&
    input.shift_start_s < input.shift_end_s &&
    input.shift_end_s <= 86400
  );
}

export function TeamEditor({
  team,
  disabled,
  office,
  onChange,
}: {
  team: EngineerDraft[];
  disabled: boolean;
  office: string;
  onChange: (team: EngineerDraft[]) => void;
}) {
  function edit(i: number, changes: Partial<EngineerDraft>) {
    onChange(team.map((e, n) => (n === i ? { ...e, ...changes } : e)));
  }
  const count = team.filter((e) => e.enabled).length;
  if (!team.length) return null;
  return (
    <section className="team-editor" aria-label="Доступные инженеры">
      <div className="team-count" role="status">
        Доступны сегодня{' '}
        <strong>
          {count} из {team.length}
        </strong>
      </div>
      {office && (
        <p className="office-address">
          <Icon name="office" size={16} />
          <span>Старт из офиса: {office}</span>
        </p>
      )}
      <details className="team-settings">
        <summary>
          Настроить инженеров <span className="counter">{team.length}</span>
        </summary>
        <div className="team-actions">
          <button
            className="secondary"
            disabled={disabled || count === team.length}
            onClick={() => onChange(team.map((e) => ({ ...e, enabled: true })))}
          >
            Выбрать всех
          </button>
          <button
            className="secondary"
            disabled={disabled || count === 0}
            onClick={() => onChange(team.map((e) => ({ ...e, enabled: false })))}
          >
            Снять выбор
          </button>
        </div>
        {team.map((engineer, i) => (
          <div
            className={`engineer-setting-row${engineer.enabled ? '' : ' excluded'}`}
            key={engineer.id}
          >
            <input
              className="engineer-available"
              type="checkbox"
              aria-label={`Доступен: ${engineer.name || engineer.id}`}
              checked={engineer.enabled}
              disabled={disabled}
              onChange={(e) => edit(i, { enabled: e.target.checked })}
            />
            <details className="engineer-settings">
              <summary>
                <Icon name={profileIcons[engineer.profile] ?? 'walk'} size={17} />
                <span className="engineer-setting-name">{engineer.name || engineer.id}</span>
                <span>
                  {engineer.start}–{engineer.end}
                </span>
              </summary>
              <fieldset
                disabled={disabled || !engineer.enabled}
                aria-label={`Параметры инженера ${i + 1}`}
              >
                <label>
                  Имя
                  <input
                    value={engineer.name}
                    maxLength={150}
                    placeholder={engineer.id}
                    onChange={(e) => edit(i, { name: e.target.value })}
                  />
                </label>
                <label>
                  Транспорт
                  <select
                    value={engineer.profile}
                    onChange={(e) => edit(i, { profile: e.target.value })}
                  >
                    {Object.entries(profileNames).map(([value, label]) => (
                      <option key={value} value={value}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>
                <div className="shift-fields">
                  <label>
                    Начало смены
                    <input
                      type="time"
                      required
                      value={engineer.start}
                      onChange={(e) => edit(i, { start: e.target.value })}
                    />
                  </label>
                  <label>
                    Конец смены
                    <input
                      type="text"
                      inputMode="numeric"
                      placeholder="18:00"
                      required
                      value={engineer.end}
                      onChange={(e) => edit(i, { end: e.target.value })}
                    />
                  </label>
                </div>
                <div className="skill-fields" role="group" aria-label="Навыки">
                  {Object.entries(skills).map(([value, label]) => (
                    <label key={value}>
                      <input
                        type="checkbox"
                        checked={engineer.skills.includes(value)}
                        onChange={(e) =>
                          edit(i, {
                            skills: e.target.checked
                              ? [...engineer.skills, value]
                              : engineer.skills.filter((s) => s !== value),
                          })
                        }
                      />
                      {label}
                    </label>
                  ))}
                </div>
                {engineer.enabled && !validEngineer(engineer) && (
                  <p className="field-error">Проверьте смену и выберите хотя бы один навык.</p>
                )}
              </fieldset>
            </details>
          </div>
        ))}
      </details>
    </section>
  );
}
