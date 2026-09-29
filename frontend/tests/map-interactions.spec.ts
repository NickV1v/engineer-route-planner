import { uploadSample, uploadEngineers } from './upload-fixture';
import { expect, test, type Page } from '@playwright/test';
import type { Run, Visit } from '../src/types';
import { withMapCoordinates } from './map-fixture';

async function baseline(page: Page) {
  await page.setViewportSize({ width: 1440, height: 1000 });
  let geography: Run['scenario']['geography'] = {};
  // Use the same fixture for the plan and address catalog. Edits from earlier
  // tests must not insert a late coordinate-warning banner and move the map.
  await page.route(/\/api\/sessions\/[^/]+\/geography\?version=\d+$/, (route) =>
    route.fulfill({ json: geography }),
  );
  await page.route('**/optimize', async (route) => {
    const response = await route.fetch();
    const run = withMapCoordinates(await response.json());
    const assigned = run.scenario.requests.find(
      (job) => job.id === run.plan.routes[0].visits[0].request_id,
    )!;
    const unassigned = run.scenario.requests.find(
      (job) =>
        run.plan.unassigned.some((item) => item.request_id === job.id) &&
        job.location_id !== assigned.location_id,
    )!;
    // An unassigned request shares a pin with an assigned request.
    run.scenario.geography![unassigned.location_id].point = {
      ...run.scenario.geography![assigned.location_id].point!,
    };
    geography = run.scenario.geography;
    await route.fulfill({ response, json: run });
  });
  await page.goto('/');
  await uploadSample(page);
  await page.getByRole('button', { name: 'Рассчитать план' }).waitFor({ state: 'visible' });
  await expect(page.getByRole('button', { name: 'Рассчитать план' })).toBeEnabled();
  await uploadEngineers(page, 3);
  const response = page.waitForResponse((r) => r.url().endsWith('/optimize'));
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  const run: Run = await (await response).json();
  await expect(page.locator('.route-overview').first()).toBeVisible();
  return run;
}

test('unassigned visibility preserves shared assigned pins, office, request list and plan', async ({
  page,
}) => {
  const run = await baseline(page);
  expect(run.plan.metrics.unassigned).toBeGreaterThan(0);
  const original = await page.locator('.map-stop').count();
  const writes: string[] = [];
  page.on('request', (request) => {
    if (request.method() !== 'GET') writes.push(request.url());
  });
  const assignedIds = new Set(
    run.plan.routes.flatMap((route) => route.visits.map((visit) => visit.request_id)),
  );
  const assignedPoints = new Set(
    run.scenario.requests
      .filter((job) => assignedIds.has(job.id))
      .map((job) => {
        const point = run.scenario.geography![job.location_id].point!;
        return `${point.lat}:${point.lon}`;
      }),
  );
  const sharedJob = run.scenario.requests.find(
    (job) => job.id === run.plan.routes[0].visits[0].request_id,
  )!;
  const sharedPin = page
    .locator('.map-stop')
    .and(page.getByLabel(sharedJob.address, { exact: true }));
  await expect(sharedPin).toBeVisible();
  await expect(sharedPin).toContainText('+');
  const sharedAddress = await sharedPin.getAttribute('aria-label');
  const geometry = await page
    .locator('.route-overview')
    .evaluateAll((paths) => paths.map((path) => path.getAttribute('d')));
  await page.getByRole('button', { name: 'Скрыть неназначенные заявки на карте' }).click();
  await expect(page.locator('.map-stop')).toHaveCount(assignedPoints.size);
  expect(assignedPoints.size).toBeLessThan(original);
  await expect(page.locator('.map-stop-unassigned')).toHaveCount(0);
  await expect(
    page.locator('.map-stop').and(page.getByLabel(sharedAddress!, { exact: true })),
  ).toBeVisible();
  await expect(page.locator('.map-office')).toBeVisible();
  expect(
    await page
      .locator('.route-overview')
      .evaluateAll((paths) => paths.map((path) => path.getAttribute('d'))),
  ).toEqual(geometry);
  await page.getByRole('button', { name: 'Неназначенные заявки', exact: true }).click();
  await expect(page.locator('.request-cards > li')).toHaveCount(run.plan.metrics.unassigned);
  await page.getByRole('button', { name: 'Скрыть все', exact: true }).click();
  await expect(page.locator('.map-stop')).toHaveCount(0);
  await expect(page.locator('.map-office')).toBeVisible();
  await page.getByRole('button', { name: 'Показать все', exact: true }).click();
  await expect(page.locator('.map-stop')).toHaveCount(assignedPoints.size);
  await page.getByRole('button', { name: 'Показать неназначенные заявки на карте' }).click();
  await expect(page.locator('.map-stop')).toHaveCount(original);
  expect(writes).toEqual([]);
});

test('timeline hover fades other routes without moving the map or revealing hidden engineers', async ({
  page,
}) => {
  const run = await baseline(page);
  const [first, second] = run.plan.routes.filter((route) => route.visits.length);
  const firstIndex = run.engineers.findIndex((engineer) => engineer.id === first.engineer_id);
  const secondIndex = run.engineers.findIndex((engineer) => engineer.id === second.engineer_id);
  const firstLine = page.locator(`.engineer-route-${firstIndex}`).first();
  const secondLine = page.locator(`.engineer-route-${secondIndex}`).first();
  const row = page.locator(`.engineer-row[data-engineer-id="${first.engineer_id}"]`);
  const pathNode = await firstLine.elementHandle();
  const path = await firstLine.getAttribute('d');
  await row.locator('.transport-icon').hover();
  await expect(row).toHaveClass(/route-highlighted/);
  await expect(row.locator('.engineer-info')).toHaveCSS(
    'background-color',
    await row.evaluate((element) => getComputedStyle(element).backgroundColor),
  );
  await expect(firstLine).toHaveAttribute('stroke-opacity', '1');
  await expect(secondLine).toHaveAttribute('stroke-opacity', '0.14');
  expect(await pathNode!.evaluate((node) => node.isConnected)).toBe(true);
  await expect(firstLine).toHaveAttribute('d', path!);
  await page.mouse.move(0, 0);
  await expect(row).not.toHaveClass(/route-highlighted/);
  await expect(firstLine).toHaveAttribute('stroke-opacity', '0.8');
  await expect(secondLine).toHaveAttribute('stroke-opacity', '0.8');
  await row.locator('.engineer-focus').focus();
  await expect(secondLine).toHaveAttribute('stroke-opacity', '0.14');
  await row
    .getByRole('button', { name: `Скрыть маршрут инженера ${firstIndex + 1}`, exact: true })
    .click();
  await row.locator('.transport-icon').hover();
  await expect(page.locator(`.engineer-route-${firstIndex}`)).toHaveCount(0);
  await expect(secondLine).toHaveAttribute('stroke-opacity', '0.8');
});

test('overview route clicks frame the engineer route and pin or replace synchronized selection', async ({
  page,
}, testInfo) => {
  const run = await baseline(page);
  const [first, second] = run.plan.routes.filter((route) => route.visits.length);
  const index = (id: string) => run.engineers.findIndex((engineer) => engineer.id === id);
  const lines = (id: string) => page.locator(`.route-overview.engineer-route-${index(id)}`);
  const row = (id: string) => page.locator(`.engineer-row[data-engineer-id="${id}"]`);
  const beforeSecondFocus = await lines(second.engineer_id).first().boundingBox();
  const geometry = await page
    .locator('.route-overview')
    .evaluateAll((paths) => paths.map((path) => path.getAttribute('d')));
  const point = async (id: string) => {
    const hit = await lines(id).evaluateAll((paths) => {
      for (const element of paths) {
        const path = element as SVGPathElement;
        for (const ratio of [0.5, 0.25, 0.75, 0.1, 0.9]) {
          const p = path.getPointAtLength(path.getTotalLength() * ratio);
          const screen = new DOMPoint(p.x, p.y).matrixTransform(path.getScreenCTM()!);
          if (document.elementFromPoint(screen.x, screen.y) === path)
            return { x: screen.x, y: screen.y };
        }
      }
      return null;
    });
    expect(hit, 'A visible part of the engineer route can be hit with the mouse').not.toBeNull();
    return hit!;
  };
  const firstPoint = await point(first.engineer_id);
  await page.mouse.move(firstPoint.x, firstPoint.y);
  await expect(row(first.engineer_id)).toHaveClass(/route-highlighted/);
  await expect(lines(first.engineer_id).first()).toHaveAttribute('stroke-opacity', '1');
  await expect(lines(second.engineer_id).first()).toHaveAttribute('stroke-opacity', '0.14');
  await page.mouse.move(0, 0);
  await expect(row(first.engineer_id)).not.toHaveClass(/route-highlighted/);
  await expect(lines(second.engineer_id).first()).toHaveAttribute('stroke-opacity', '0.8');

  expect(
    await page
      .locator('.route-overview')
      .evaluateAll((paths) => paths.map((path) => path.getAttribute('d'))),
  ).toEqual(geometry);

  // Settle the SVG hover style before clicking the narrow path.
  await page.mouse.move(firstPoint.x, firstPoint.y);
  await expect(row(first.engineer_id)).toHaveClass(/route-highlighted/);
  await page.mouse.click(firstPoint.x, firstPoint.y);
  await page.mouse.move(0, 0);
  await expect(lines(first.engineer_id).first()).toHaveAttribute('aria-pressed', 'true');
  await expect(row(first.engineer_id)).toHaveClass(/route-highlighted/);
  await expect(page.locator('.journey-tooltip')).toHaveCount(0);
  const selectedBounds = await lines(first.engineer_id).evaluateAll((paths) => {
    const bounds = paths.map((path) => path.getBoundingClientRect());
    const map = document.querySelector('.route-map')!.getBoundingClientRect();
    return {
      left: Math.min(...bounds.map((b) => b.left)) - map.left,
      right: map.right - Math.max(...bounds.map((b) => b.right)),
      top: Math.min(...bounds.map((b) => b.top)) - map.top,
      bottom: map.bottom - Math.max(...bounds.map((b) => b.bottom)),
    };
  });
  for (const margin of Object.values(selectedBounds)) expect(margin).toBeGreaterThanOrEqual(25);
  const secondPoint = await point(second.engineer_id);
  await page.mouse.move(secondPoint.x, secondPoint.y);
  await expect(row(first.engineer_id)).toHaveClass(/route-highlighted/);
  await expect(row(second.engineer_id)).not.toHaveClass(/route-highlighted/);
  await page.mouse.click(secondPoint.x, secondPoint.y);
  await page.mouse.move(0, 0);
  await expect(row(first.engineer_id)).not.toHaveClass(/route-highlighted/);
  await expect(row(second.engineer_id)).toHaveClass(/route-highlighted/);
  await expect(lines(second.engineer_id).first()).toHaveAttribute('aria-pressed', 'true');
  await page.screenshot({ path: testInfo.outputPath('pinned-overview-route.png') });
  await expect
    .poll(() => lines(second.engineer_id).first().boundingBox())
    .not.toEqual(beforeSecondFocus);
  const surface = (await page.locator('.route-map').boundingBox())!;
  await page.mouse.move(surface.x + surface.width - 15, surface.y + 15);
  await page.mouse.down();
  await page.mouse.move(surface.x + surface.width - 40, surface.y + 40, { steps: 5 });
  await page.mouse.up();
  await expect(lines(second.engineer_id).first()).toHaveAttribute('aria-pressed', 'true');
  await expect(row(second.engineer_id)).toHaveClass(/route-highlighted/);
  await page.mouse.click(surface.x + surface.width - 15, surface.y + 15);
  await expect(page.locator('.route-emphasized')).toHaveCount(0);
  await expect(page.locator('.engineer-row.route-highlighted')).toHaveCount(0);
  await expect(lines(second.engineer_id).first()).toHaveAttribute('aria-pressed', 'false');
});

test('visit cards wait 1500 ms and never open over travel or waiting segments', async ({
  page,
}) => {
  const run = await baseline(page);
  const visit = run.plan.routes
    .flatMap((route) => route.visits)
    .find((visit) => visit.waiting_s > 0 && visit.travel_s > 0)!;
  const button = page.getByRole('button', {
    name: `Открыть заявку ${visit.request_id}`,
    exact: true,
  });
  const group = button.locator('..');
  const tooltip = page.locator('.visit-summary');
  await button.scrollIntoViewIfNeeded();
  await page.clock.install({ time: new Date('2026-09-27T12:00:00Z') });
  await page.clock.pauseAt(new Date('2026-09-27T12:00:01Z'));

  for (const segment of ['.travel-segment', '.waiting-segment']) {
    await group.locator(segment).hover();
    await page.clock.runFor(2000);
    await expect(tooltip).toHaveCount(0);
  }
  await button.hover();
  await page.clock.runFor(1499);
  await expect(tooltip).toHaveCount(0);
  await group.locator('.travel-segment').hover();
  await page.clock.runFor(2000);
  await expect(tooltip).toHaveCount(0);
  await button.hover();
  await page.clock.runFor(1499);
  await expect(tooltip).toHaveCount(0);
  await page.clock.runFor(1);
  await expect(tooltip).toBeVisible();
  await group.locator('.waiting-segment').hover();
  await page.clock.runFor(2000);
  await expect(tooltip).toHaveCount(0);

  await button.hover();
  await page.clock.runFor(500);
  await page.keyboard.press('Escape');
  await page.clock.runFor(2000);
  await expect(tooltip).toHaveCount(0);
  await page.mouse.move(0, 0);
  await button.hover();
  await page.clock.runFor(500);
  await page.getByRole('tab', { name: 'Метрики', exact: true }).click();
  await page.clock.runFor(2000);
  await expect(tooltip).toHaveCount(0);
});

test('visit summary shows planned metrics, fits the viewport and supports hover, focus and dismissal', async ({
  page,
}, testInfo) => {
  const run = await baseline(page);
  const route = run.plan.routes.find((route) => route.visits.some((visit) => visit.waiting_s > 0))!;
  const visit = route.visits.find((visit) => visit.waiting_s > 0)!;
  const index = run.engineers.findIndex((engineer) => engineer.id === route.engineer_id);
  const job = run.scenario.requests.find((job) => job.id === visit.request_id)!;
  const button = page.getByRole('button', { name: `Открыть заявку ${job.id}`, exact: true });
  const tooltip = page.getByRole('tooltip');
  const clock = (seconds: number) =>
    `${String(Math.floor(seconds / 3600)).padStart(2, '0')}:${String(Math.floor(seconds / 60) % 60).padStart(2, '0')}`;
  async function metrics(expected: Visit) {
    await expect(tooltip).toContainText(`Заявка ${expected.request_id}`);
    for (const [label, value] of [
      ['В пути', `${Math.ceil(expected.travel_s / 60)} мин`],
      ['Расстояние', `${(expected.distance_m / 1000).toFixed(2)} км`],
      ['Ожидание у клиента', `${Math.ceil(expected.waiting_s / 60)} мин`],
      ['Прибытие', clock(expected.arrival_s)],
      ['Начало работ', clock(expected.start_s)],
      ['Окончание работ', clock(expected.finish_s)],
    ]) {
      await expect(
        tooltip
          .locator('dl > div')
          .filter({ has: page.getByText(label, { exact: true }) })
          .locator('dd'),
      ).toHaveText(value);
    }
  }
  await button.hover();
  await metrics(visit);
  await expect(tooltip).toContainText(job.address);
  await expect(tooltip).toContainText(`Инженер ${index + 1}`);
  await expect(button).not.toHaveAttribute('title');
  const box = (await tooltip.boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(8);
  expect(box.y).toBeGreaterThanOrEqual(8);
  expect(box.x + box.width).toBeLessThanOrEqual(1432);
  expect(box.y + box.height).toBeLessThanOrEqual(992);
  await tooltip.hover();
  await page.screenshot({ path: testInfo.outputPath('visit-summary.png') });
  await expect(tooltip).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(tooltip).toHaveCount(0);
  await page.setViewportSize({ width: 1440, height: 700 });
  await page.getByRole('separator', { name: 'Высота панели инженеров' }).press('Home');
  await button.scrollIntoViewIfNeeded();
  await button.focus();
  await metrics(visit);
  await expect(button).toHaveAttribute(
    'aria-describedby',
    (await tooltip.getAttribute('id')) as string,
  );
  await page.locator('#engineers-timeline').evaluate((element) => {
    element.scrollTop = element.scrollTop ? 0 : 35;
  });
  await expect(tooltip).toHaveCount(0);
  await page.getByRole('separator', { name: 'Высота панели инженеров' }).press('End');
  const zeroWait = run.plan.routes
    .flatMap((route) => route.visits)
    .find((visit) => visit.waiting_s === 0)!;
  await page
    .getByRole('button', { name: `Открыть заявку ${zeroWait.request_id}`, exact: true })
    .hover();
  await metrics(zeroWait);
  await page.getByRole('tab', { name: 'Метрики', exact: true }).click();
  await expect(tooltip).toHaveCount(0);
  await page.getByRole('tab', { name: 'Таймлайн', exact: true }).click();
  await button.click();
  await expect(page.getByRole('region', { name: 'Детали заявки' })).toBeVisible();
  await expect(tooltip).toHaveCount(0);
});
