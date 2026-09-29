import { expect, test } from '@playwright/test';
import { uploadSample } from './upload-fixture';
import { withMapCoordinates } from './map-fixture';
import type { Run, WorkRequest } from '../src/types';
import { explainRejection } from '../src/requestReasons';
import { engineerColor } from '../src/icons';

test('map counters open request filters, sorting composes with filters and does not change the plan', async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.route('**/optimize', async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: withMapCoordinates(await response.json()) });
  });
  await page.goto('/');
  await uploadSample(page);
  const result = page.waitForResponse((r) => r.url().endsWith('/optimize'));
  await page.getByRole('button', { name: 'Рассчитать план', exact: true }).click();
  const run: Run = await (await result).json();
  await expect(page.locator('.visit')).toHaveCount(run.plan.metrics.assigned);
  const geometry = () =>
    page
      .locator('.route-overview')
      .evaluateAll((paths) => paths.map((path) => path.getAttribute('d')));
  const before = await geometry();
  const writes: string[] = [];
  page.on('request', (r) => {
    if (r.method() !== 'GET') writes.push(r.url());
  });
  const strip = page.getByLabel('Метрики плана');
  const cards = page.locator('.request-cards > li');
  await strip.getByRole('button', { name: 'Неназначенные заявки', exact: true }).click();
  await expect(cards).toHaveCount(run.plan.metrics.unassigned);
  await expect(strip.locator('.unassigned-map-controls')).toContainText('без назначения');
  await expect(
    strip
      .locator('.unassigned-map-controls')
      .getByRole('button', { name: 'Скрыть неназначенные заявки на карте' }),
  ).toBeVisible();
  await strip.getByRole('button', { name: 'Назначенные заявки', exact: true }).click();
  await expect(cards).toHaveCount(run.plan.metrics.assigned);
  const route = run.plan.routes.find((r) => r.visits.length > 1)!;
  const jobs = run.scenario.requests.filter((r) => route.visits.some((v) => v.request_id === r.id));
  await page.getByLabel('Фильтр по инженеру').selectOption(route.engineer_id);
  await expect(cards).toHaveCount(jobs.length);
  await page.getByLabel('Тип заявки', { exact: true }).selectOption(jobs[0].kind);
  const filtered = jobs.filter((r) => r.kind === jobs[0].kind);
  await expect(cards).toHaveCount(filtered.length);
  await page.getByLabel('Сортировка заявок').selectOption('id');
  await expect(cards.locator('.request-link')).toHaveText(
    filtered
      .sort((a, b) => a.id.localeCompare(b.id, 'ru', { numeric: true }))
      .map((r) => `#${r.id.split(':').pop()}`),
  );
  await page.getByLabel('Поиск заявки', { exact: true }).fill('Нет такого заказа');
  await expect(cards).toHaveCount(0);
  await strip.getByRole('button', { name: 'Все заявки', exact: true }).click();
  await expect(cards).toHaveCount(run.scenario.requests.length);
  await expect(page.getByLabel('Поиск заявки', { exact: true })).toHaveValue('');
  expect(await geometry()).toEqual(before);
  await page.screenshot({ path: info.outputPath('requests-filtered.png') });
  await strip.getByRole('button', { name: 'Неназначенные заявки', exact: true }).click();
  await cards.locator('.request-link').first().click();
  const dialog = page.getByRole('region', { name: 'Детали заявки' });
  await expect(dialog.locator('.request-rejection h3')).not.toBeEmpty();
  await expect(dialog).not.toContainText('вставки');
  await dialog.getByText('Причины по инженерам', { exact: true }).click();
  await expect(dialog.locator('.request-rejection-checks li')).toHaveCount(run.engineers.length);
  await page.screenshot({ path: info.outputPath('unassigned-request.png') });
  await page.keyboard.press('Escape');
  expect(writes).toEqual([]);
});

test('request focus is explicit, surviving replan does not reset the camera, and all points can be restored', async ({
  page,
}) => {
  let accepted: Run;
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.route('**/optimize', async (route) => {
    const response = await route.fetch();
    accepted = withMapCoordinates(await response.json());
    await route.fulfill({ response, json: accepted });
  });
  await page.route(/\/api\/sessions\/[^/]+\/geography\?version=\d+$/, (route) =>
    route.fulfill({ json: accepted.scenario.geography }),
  );
  await page.goto('/');
  await uploadSample(page);
  await page.getByRole('button', { name: 'Рассчитать план', exact: true }).click();
  await expect(page.locator('.route-overview').first()).toBeVisible();
  const office = page.locator('.map-office');
  const transform = () => office.getAttribute('style');
  const initial = await transform();
  const job = accepted!.scenario.requests.find((r) =>
    accepted!.plan.unassigned.some((u) => u.request_id === r.id),
  )!;
  await page.getByRole('button', { name: 'Скрыть неназначенные заявки на карте' }).click();
  await page
    .getByLabel('Метрики плана')
    .getByRole('button', { name: 'Все заявки', exact: true })
    .click();
  await page.getByLabel('Поиск заявки', { exact: true }).fill(job.id);
  await page.locator('.request-link').click();
  await expect(page.getByRole('region', { name: 'Детали заявки' })).toBeVisible();
  expect(await transform()).toBe(initial);
  await page.getByRole('button', { name: 'Показать на карте', exact: true }).click();
  await expect.poll(transform).not.toBe(initial);
  await expect(page.getByRole('region', { name: 'Детали заявки' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(
    page.locator('.map-stop').and(page.getByLabel(job.address, { exact: true })),
  ).toBeVisible();
  await page.getByRole('button', { name: 'Показать все точки на карте' }).click();
  const refitted = await transform();
  await page.locator('.leaflet-control-zoom-in').click();
  await expect.poll(transform).not.toBe(refitted);
  const zoomed = await transform();
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  await expect(page.getByText('Добавить событие и пересчитать')).toHaveCount(0);
  await page.getByLabel('Адрес новой заявки').fill('Тестовый адрес, дом 10');
  await page.route('**/events/preview', (route) =>
    route.fulfill({
      json: {
        preview_id: 'camera-preview',
        result: { ...accepted!, version: accepted!.version + 1 },
      },
    }),
  );
  await page.route('**/events/commit', (route) =>
    route.fulfill({ json: { ...accepted!, version: accepted!.version + 1 } }),
  );
  await page.getByRole('button', { name: 'Рассчитать вариант', exact: true }).click();
  await page.getByRole('button', { name: 'Принять вариант', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Вариант для принятия' })).toHaveCount(0);
  expect(await transform()).toBe(zoomed);
});

test('rejection summaries separate missing data, incompatible skills, issued stock and search limits', () => {
  const job = { window_start_s: 36000, window_end_s: 43200, service_s: 4200 } as WorkRequest;
  const explain = (
    checks: Record<string, string>,
    missing = false,
    reason = 'no_feasible_insertion',
  ) =>
    explainRejection(
      { request_id: '1', explanation: 'Technical detail', reason, checks },
      job,
      missing,
    );
  expect(explain({ '1': 'skill', '2': 'off_shift' }).title).toContain('квалификацией');
  expect(explain({ '1': 'search_budget' }).title).toContain('Расчёт закончился');
  expect(explain({ '1': 'equipment_routers', '2': 'skill' }).title).toContain('оборудования');
  expect(explain({ '1': 'window', '2': 'unavailable' }).title).toContain('10:00 до 12:00');
  expect(explain({ '1': 'no_feasible_insertion', '2': 'shift' }).title).toContain(
    'текущем расписании',
  );
  expect(explain({}, false, 'no_engineers').title).toBe('Нет доступных инженеров');
  expect(explain({}, true).title).toBe('Нужно уточнить адрес');
  expect(explain({}).title).toContain('текущем расписании');
  expect(new Set(Array.from({ length: 30 }, (_, i) => engineerColor(i))).size).toBe(30);
});
