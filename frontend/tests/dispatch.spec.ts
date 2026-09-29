import { openCancellation, openUnavailability } from './sidebar-fixture';
import { uploadSample, selectEngineers } from './upload-fixture';
import { expect, test, type Page } from '@playwright/test';
import type { Run, JourneyLeg } from '../src/types';
import { withMapCoordinates } from './map-fixture';

test('real journeys show metro, bus and their saved geometry with synchronized selection', async ({
  page,
}, testInfo) => {
  await page.route('**/optimize', async (route) => {
    const response = await route.fetch();
    const run = withMapCoordinates(await response.json());
    run.manifest.transport = 'routing_static_v1';
    run.performance = {
      cache_scope: 'calculation',
      network_load_ms: 0,
      routing_ms: 12345,
      archive_write_ms: 240,
      solver_ms: 1500,
      queue_wait_ms: 0,
      road_searches: 120,
      public_source_searches: 10,
      cache_hits: 30,
    };
    run.journeys = { plan: {}, baseline: {} };
    for (const key of ['plan', 'baseline'] as const) {
      const plan = run[key]!;
      for (const engineerRoute of plan.routes) {
        run.journeys[key]![engineerRoute.engineer_id] = engineerRoute.visits.map((visit, i) => {
          const offset = key === 'plan' ? 0.03 : 0;
          const points = [
            { lat: 55.7 + offset, lon: 37.6 },
            { lat: 55.71 + offset, lon: 37.6 },
            { lat: 55.71 + offset, lon: 37.64 },
          ];
          const firstTime = Math.floor(visit.travel_s / 2);
          const firstDistance = Math.floor(visit.distance_m / 2);
          return {
            request_id: visit.request_id,
            from_id: 'office',
            to_id: 'request',
            status: 'ok',
            duration_s: visit.travel_s,
            distance_m: visit.distance_m,
            walking_s: 0,
            boarding_wait_s: 0,
            legs: [
              {
                mode: 'metro',
                from_id: 'office',
                to_id: 'transfer',
                from_label: 'Первая станция',
                to_label: 'Пересадочная',
                duration_s: firstTime,
                distance_m: firstDistance,
                geometry:
                  key === 'plan'
                    ? points
                    : [points[0], { lat: 55.705, lon: 37.62 }, ...points.slice(1)],
                line: 'Метро 5',
                quality: 'estimated',
                geometry_quality: 'network',
              },
              {
                mode: 'bus',
                from_id: 'transfer',
                to_id: 'request',
                from_label: 'Пересадочная',
                to_label: 'Конечная остановка',
                duration_s: visit.travel_s - firstTime,
                distance_m: visit.distance_m - firstDistance,
                geometry: [points[2], { lat: 55.73 + offset, lon: 37.66 + i * 0.001 }],
                line: 'Автобус 7',
                quality: 'estimated',
                geometry_quality: 'network',
              },
            ].flatMap((leg) => {
              const last = leg.geometry.at(-1)!;
              const previous = leg.geometry.at(-2)!;
              const middle = {
                lat: (last.lat + previous.lat) / 2,
                lon: (last.lon + previous.lon) / 2,
              };
              const midpoint = `${leg.from_id}-middle`;
              const duration = Math.floor(leg.duration_s / 2);
              const distance = Math.floor(leg.distance_m / 2);
              return [
                {
                  ...leg,
                  to_id: midpoint,
                  to_label: 'Промежуточная остановка',
                  duration_s: duration,
                  distance_m: distance,
                  geometry: [...leg.geometry.slice(0, -1), middle],
                },
                {
                  ...leg,
                  from_id: midpoint,
                  from_label: 'Промежуточная остановка',
                  duration_s: leg.duration_s - duration,
                  distance_m: leg.distance_m - distance,
                  geometry: [middle, last],
                },
              ];
            }) as JourneyLeg[],
          };
        });
      }
    }
    await route.fulfill({ response, json: run });
  });
  await page.goto('/');
  await uploadSample(page);
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  await expect(page.getByText('Маршруты по реальной транспортной сети')).toHaveCount(0);
  await expect(page.getByLabel('Время расчёта')).toContainText('12.345 с');
  await expect(page.getByLabel('Время расчёта')).toContainText('1.500 с');
  await expect(page.getByLabel('Время расчёта')).not.toContainText('Кеш действует');
  await expect(page.locator('.journey-path')).toHaveCount(0);
  await expect(page.locator('.route-overview').first()).toBeVisible();
  for (const path of await page.locator('.route-overview').all()) {
    const geometry = (await path.getAttribute('d'))!;
    expect(geometry.match(/L/g)).toHaveLength(1);
  }
  await page.getByRole('button', { name: /^Инженер 1 / }).click();
  await expect(page.locator('.journey-metro').first()).toBeVisible();
  await expect(page.locator('.journey-bus').first()).toBeVisible();
  await expect(page.locator('.route-overview')).toHaveCount(0);
  const visits = await page
    .getByRole('list', { name: 'Порядок визитов' })
    .locator(':scope > li')
    .count();
  await expect(page.locator('.journey-metro')).toHaveCount(visits);
  await expect(page.locator('.journey-bus')).toHaveCount(visits);
  const firstRequestId = await page
    .locator('.journey-metro')
    .first()
    .getAttribute('data-request-id');
  const metro = page.locator(`.journey-metro[data-request-id="${firstRequestId}"]`);
  await metro.focus();
  await expect(metro).toHaveCSS('outline-style', 'none');
  const hitPoint = (target: typeof metro, ratio = 1 / 3) =>
    target.evaluate((element, ratio) => {
      const path = element as SVGPathElement;
      const point = path.getPointAtLength(path.getTotalLength() * ratio);
      const screen = new DOMPoint(point.x, point.y).matrixTransform(path.getScreenCTM()!);
      return { x: screen.x, y: screen.y };
    }, ratio);
  const position = await hitPoint(metro);
  await page.mouse.click(position.x, position.y);
  await expect(metro).toHaveCSS('outline-style', 'none');
  await page
    .locator('.journey-details')
    .first()
    .evaluate((element: HTMLDetailsElement) => {
      element.open = true;
    });
  await page.getByRole('button', { name: 'Показать все точки на карте' }).click();
  const details = page.locator('.journey-details').first();
  await expect(details).toContainText('Метро 5');
  await expect(details).toContainText('Автобус 7');
  await expect(details).toContainText('Пересадочная');
  await expect(details.getByRole('listitem')).toHaveCount(2);
  await expect(details).not.toContainText('Включено:');
  await expect(details).not.toContainText('Время оценочное, без пробок');
  // Hover a destination to emphasize its entire incoming trip (metro + bus).
  const requestId = (await metro.getAttribute('data-request-id'))!;
  const trip = page.locator(`.journey-path[data-request-id="${requestId}"]`);
  const otherTrips = page.locator(`.journey-path:not([data-request-id="${requestId}"])`);
  expect(await otherTrips.count()).toBeGreaterThan(0);
  await details.locator('summary').hover();
  for (const leg of await trip.all()) await expect(leg).toHaveAttribute('stroke-opacity', '1');
  await expect(otherTrips.first()).toHaveAttribute('stroke-opacity', '0.14');
  const tripGeometry = await trip.evaluateAll((paths) =>
    paths.map((path) => path.getAttribute('d')),
  );
  await page.mouse.move(0, 0);
  await expect(trip.first()).toHaveAttribute('stroke-opacity', '0.88');
  await expect(otherTrips.first()).toHaveAttribute('stroke-opacity', '0.88');
  // Exercise real SVG hit testing on the final bus segment, away from shared metro tracks.
  const bus = page.locator(`.journey-bus:not([data-request-id="${requestId}"])`).last();
  const busRequest = await bus.getAttribute('data-request-id');
  const busPoint = await bus.evaluate((element) => {
    const path = element as SVGPathElement;
    const point = path.getPointAtLength(path.getTotalLength() * 0.9);
    const screen = new DOMPoint(point.x, point.y).matrixTransform(path.getScreenCTM()!);
    return { x: screen.x, y: screen.y };
  });
  await page.mouse.move(busPoint.x, busPoint.y);
  await expect(bus).toHaveAttribute('stroke-opacity', '1');
  await expect(page.locator(`.journey-metro[data-request-id="${busRequest}"]`)).toHaveAttribute(
    'stroke-opacity',
    '1',
  );
  await expect(trip.first()).toHaveAttribute('stroke-opacity', '0.14');
  expect(await trip.evaluateAll((paths) => paths.map((path) => path.getAttribute('d')))).toEqual(
    tripGeometry,
  );
  await page.mouse.move(0, 0);
  await expect(page.locator('.leaflet-tooltip')).toHaveCount(0);
  await expect(bus).toHaveAttribute('stroke-opacity', '0.88');
  await expect(bus).toHaveCSS('stroke-width', '3.5px');
  await expect(page.locator('.route-emphasized')).toHaveCount(0);
  // Clicks pin the whole incoming trip, open its details and scroll only the itinerary.
  const surface = (await page.locator('.route-map').boundingBox())!;
  const blank = { x: surface.x + surface.width - 15, y: surface.y + 15 };
  const itinerary = page.getByRole('list', { name: 'Порядок визитов' });
  for (const path of [metro, bus, metro, bus, bus]) {
    await page.getByRole('button', { name: 'Показать все точки на карте' }).click();
    const beforeFocus = await path.getAttribute('d');
    const target = await hitPoint(path, path === bus ? 0.9 : 1 / 3);
    await itinerary.evaluate((element) => {
      element.scrollTop = 0;
    });
    await page.mouse.move(target.x, target.y);
    await expect(page.locator('.journey-tooltip')).toHaveCount(1);
    await page.mouse.click(target.x, target.y);
    await expect(path).not.toHaveAttribute('d', beforeFocus!);
    await page.mouse.move(blank.x, blank.y);
    await expect(page.locator('.leaflet-tooltip')).toHaveCount(0);
    const pinned = page.locator('.journey-path[aria-pressed="true"]');
    await expect(pinned).toHaveCount(2);
    const pinnedRequest = await pinned.first().getAttribute('data-request-id');
    const item = itinerary.locator(`:scope > li[data-request-id="${pinnedRequest}"]`);
    await expect(item.locator('.journey-details')).toHaveAttribute('open', '');
    await expect(item).toHaveClass('journey-emphasized');
    const tripBounds = await pinned.evaluateAll((paths) => {
      const bounds = paths.map((path) => path.getBoundingClientRect());
      const viewport = document.querySelector('.route-map')!.getBoundingClientRect();
      const panel = document.querySelector('.map-route-details')!.getBoundingClientRect();
      return {
        left: Math.min(...bounds.map((b) => b.left)) - viewport.left,
        right: panel.left - Math.max(...bounds.map((b) => b.right)),
        top: Math.min(...bounds.map((b) => b.top)) - viewport.top,
        bottom: viewport.bottom - Math.max(...bounds.map((b) => b.bottom)),
      };
    });
    for (const margin of Object.values(tripBounds)) expect(margin).toBeGreaterThanOrEqual(25);

    const panelBox = (await itinerary.boundingBox())!;
    const summaryBox = (await item.locator('summary').boundingBox())!;
    expect(summaryBox.y).toBeGreaterThanOrEqual(panelBox.y);
    expect(summaryBox.y + summaryBox.height).toBeLessThanOrEqual(panelBox.y + panelBox.height);
    expect(await page.locator('.route-map').boundingBox()).toEqual(surface);
    await page.mouse.click(blank.x, blank.y);
    await expect(page.locator('.route-emphasized')).toHaveCount(0);
    await expect(bus).toHaveCSS('stroke-width', '3.5px');
  }
  const dragPoint = await hitPoint(bus, 0.9);
  await page.mouse.move(dragPoint.x, dragPoint.y);
  await expect(page.locator('.journey-tooltip')).toHaveCount(1);
  await page.mouse.down();
  await page.mouse.move(dragPoint.x - 15, dragPoint.y - 15, { steps: 3 });
  await page.mouse.up();
  await expect(page.locator('.journey-tooltip')).toHaveCount(0);
  await expect(page.locator('.route-emphasized')).toHaveCount(0);
  await expect(page.locator('.leaflet-pan-anim')).toHaveCount(0);
  await page.mouse.move(blank.x, blank.y);
  await metro.focus();
  await page.keyboard.press('Tab');
  await page.keyboard.press('Shift+Tab');
  await expect(metro).toBeFocused();
  await expect(page.locator('.journey-tooltip')).toHaveCount(1);
  await expect(metro).toHaveAttribute('stroke-opacity', '1');
  const beforeKeyboardFocus = await metro.boundingBox();
  await page.keyboard.press('Enter');
  await expect(metro).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(() => metro.boundingBox()).not.toEqual(beforeKeyboardFocus);
  await page.mouse.move(blank.x, blank.y);
  await expect(metro).toHaveAttribute('stroke-opacity', '1');
  await page.screenshot({ path: testInfo.outputPath('pinned-detail-route.png') });
  await page.keyboard.press('Escape');
  await expect(metro).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('.journey-tooltip')).toHaveCount(0);
  await expect(metro).toHaveCSS('stroke-width', '3.5px');
  await page.screenshot({ path: testInfo.outputPath('journey-hover-cleared.png') });
  const visit = page.getByRole('button', { name: `Открыть заявку ${requestId}`, exact: true });
  await visit.focus();
  await expect(trip.first()).toHaveAttribute('stroke-opacity', '1');
  await expect(otherTrips.first()).toHaveAttribute('stroke-opacity', '0.14');
  await page.getByRole('button', { name: 'Показать маршруты всех инженеров' }).click();
  await expect(page.locator('.journey-metro')).toHaveCount(0);
  await expect(page.locator('.route-overview').first()).toBeVisible();
  await expect(page.locator('.visit-summary')).toHaveCount(0);
});

const suggestedHouse = {
  id: 'a'.repeat(32),
  label: 'Москва, улица Проверочная, 5 корпус 1',
  provider: 'dadata',
  precision: 'house',
  house: '5 корпус 1',
  notice: 'Найден дом. Проверьте адрес и точку перед подтверждением.',
  point: {
    lat: 55.712345,
    lon: 37.612345,
    label: 'Москва, улица Проверочная, 5 корпус 1',
    precision: 'house',
    source: 'DaData:test-house; OpenStreetMap',
  },
};

async function mockSuggestions(page: Page, house = suggestedHouse) {
  await page.route('**/api/address-suggestions', (route) =>
    route.fulfill({
      json: {
        items: [
          {
            ...house,
            id: 'b'.repeat(32),
            label: 'Москва, улица Проверочная',
            precision: 'street',
            house: '',
            point: null,
            notice: 'Дом не подтверждён в реестре.',
          },
          house,
        ],
        live_available: true,
        notice: '',
      },
    }),
  );
  await page.route('**/api/address-suggestions/resolve', (route) => route.fulfill({ json: house }));
}

test('legacy saved contracts render the accepted plan without comparison controls', async ({
  page,
}) => {
  await page.route('**/optimize', async (route) => {
    const response = await route.fetch();
    const run: Run = await response.json();
    // Emulate the response contract of a saved morning plan predating work types.
    delete run.scenario.objective_policy;
    run.scenario.requests.forEach((r) => delete r.work_type);
    delete run.comparison!.objective_components;
    run.comparison!.baseline_score = [36, 12, 1, 600, 1000, 600];
    run.comparison!.optimized_score = [12, 11, 0, 1200, 2000, 1200];
    await route.fulfill({ response, json: run });
  });
  await page.goto('/');
  await uploadSample(page);
  await expect(page.locator('.request-link')).toHaveCount(66);
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  await expect(page.getByLabel('Политика приоритетов')).toHaveCount(0);
  await expect(page.locator('.visit').first()).toBeVisible();
  await expect(page.getByRole('region', { name: 'Сравнение алгоритмов' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Базовый план', exact: true })).toHaveCount(0);
  await expect(page.locator('.dispatch-shell')).not.toContainText('NaN');
});

test('discarding a proposal starts a fresh calculation for the same event', async ({ page }) => {
  const eventIds: string[] = [];
  await page.route('**/events/preview', async (route) => {
    eventIds.push(route.request().postDataJSON().event_id);
    await route.continue();
  });
  await page.goto('/');
  await uploadSample(page);
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  await expect(page.getByLabel('Режим перепланирования').filter({ visible: true })).toHaveValue(
    'flexible',
  );
  await page.getByLabel('Время события').filter({ visible: true }).fill('12:00:00');
  await page.getByLabel('Адрес новой заявки').fill('Повторная проверка, дом 7');
  await page.getByLabel('Окно начала: с').fill('14:00:00');
  await page.getByLabel('Окно начала: до').fill('20:00:00');
  await page.getByRole('button', { name: 'Рассчитать вариант' }).click();
  await expect(page.getByRole('region', { name: 'Вариант для принятия' })).toBeVisible();
  await page.getByRole('button', { name: 'Отклонить вариант' }).click();
  await page.getByRole('button', { name: 'Рассчитать вариант' }).click();
  await expect(page.getByRole('region', { name: 'Вариант для принятия' })).toBeVisible();
  expect(eventIds).toHaveLength(2);
  expect(eventIds[0]).not.toEqual(eventIds[1]);
  await expect(page.locator('.request-link')).toHaveCount(66);
});

test('engineer count comes from the file and unchecked engineers are not sent to planning', async ({
  page,
}) => {
  await page.goto('/');
  await uploadSample(page);
  await expect(page.getByRole('spinbutton', { name: 'Инженеров в штате' })).toHaveCount(0);
  await selectEngineers(page, 10);
  const request = page.waitForRequest((r) => r.url().endsWith('/optimize'));
  const response = page.waitForResponse((r) => r.url().endsWith('/optimize'));
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  expect((await request).postDataJSON().engineers).toHaveLength(10);
  const result: Run = await (await response).json();
  await expect(page.locator('.engineer-row')).toHaveCount(result.staffing!.scheduled);
  await expect(page.locator('.team-count')).toContainText('10 из 12');
  await page.reload();
  await expect(page.locator('.team-count')).toContainText('10 из 12');
});

test('import, schedule, inspect rejections, export, switch zone and run without engineers', async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.goto('/');
  await uploadSample(page);
  await expect(page.getByRole('heading', { name: 'Планирование', exact: true })).toBeVisible();
  await expect(page.locator('.request-cards > li')).toHaveCount(66);
  const calculated = page.waitForResponse((r) => r.url().endsWith('/optimize'));
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  const run: Run = await (await calculated).json();
  await expect(page.locator('.engineer-row')).toHaveCount(run.staffing!.scheduled);
  await expect(page.getByText('Проверка пройдена')).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('desktop.png'), fullPage: true });
  await page.locator('.visit').first().click();
  await expect(page.getByRole('region', { name: 'Детали заявки' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Детали заявки' })).toContainText('В пути');
  await page.getByRole('button', { name: 'К списку заявок' }).click();
  await page.getByRole('tab', { name: 'Заявки', exact: true }).click();
  await page.getByRole('button', { name: 'Без назначения', exact: true }).click();
  await expect(page.locator('.request-cards > li')).toHaveCount(run.plan.metrics.unassigned);
  await page.locator('.request-link').first().click();
  await expect(
    page
      .getByRole('region', { name: 'Детали заявки' })
      .getByText('Без назначения', { exact: true }),
  ).toBeVisible();
  await page.getByRole('button', { name: 'К списку заявок' }).click();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Экспорт плана' }).click();
  await page.getByRole('button', { name: 'Полный план · JSON' }).click();
  const download = await downloadPromise;
  const stream = await download.createReadStream();
  const chunks = [];
  for await (const chunk of stream!) chunks.push(chunk);
  const exported = JSON.parse(Buffer.concat(chunks).toString());
  expect(exported.plan.metrics.total).toBe(66);
  expect(exported.manifest.transport).toBe('synthetic_fixture_v1');
  await page.getByRole('tab', { name: 'План', exact: true }).click();
  await uploadSample(page, 'Юго-восток');
  await expect(page.locator('.request-cards > li')).toHaveCount(83);
  await expect(page.getByRole('heading', { name: 'План ещё не построен' })).toBeVisible();
  await selectEngineers(page, 0);
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  await expect(page.locator('.dispatch-header')).not.toContainText('На смене:');
  await expect(page.locator('.map-roster-total')).toHaveText('На смене: 0');
  await page.getByRole('tab', { name: 'Заявки', exact: true }).click();
  await page.getByRole('button', { name: 'Без назначения', exact: true }).click();
  await expect(page.locator('.request-cards > li')).toHaveCount(83);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: testInfo.outputPath('mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});

test('accepted plan, accessible request sidebar, export and solver error preserve the workspace', async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await uploadSample(page);
  await expect(page.locator('.request-cards > li')).toHaveCount(66);
  const responsePromise = page.waitForResponse((r) => r.url().endsWith('/optimize'));
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  const result = await (await responsePromise).json();
  await expect(page.getByRole('region', { name: 'Сравнение алгоритмов' })).toHaveCount(0);
  await expect(page.getByRole('tab', { name: 'Смена', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Базовый план', exact: true })).toHaveCount(0);
  await expect(page.locator('.visit')).toHaveCount(result.plan.metrics.assigned);
  await page.screenshot({ path: testInfo.outputPath('plan-desktop.png'), fullPage: true });
  await page.locator('.visit').first().click();
  await expect(page.getByRole('region', { name: 'Детали заявки' })).toContainText('В пути');
  await page.screenshot({ path: testInfo.outputPath('request-sidebar.png') });
  await page.keyboard.press('Tab');
  const locate = page
    .getByRole('region', { name: 'Детали заявки' })
    .getByRole('button', { name: 'Показать на карте' });
  if (await locate.isEnabled()) {
    await expect(locate).toBeFocused();
    await page.keyboard.press('Tab');
  } else await expect(locate).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Отменить заявку', exact: true })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('region', { name: 'Детали заявки' })).toHaveCount(0);
  await expect(page.locator('.visit').first()).toBeFocused();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Экспорт плана' }).click();
  await page.getByRole('button', { name: 'Полный план · JSON' }).click();
  const stream = await (await downloadPromise).createReadStream();
  const chunks = [];
  for await (const chunk of stream!) chunks.push(chunk);
  const exported = JSON.parse(Buffer.concat(chunks).toString());
  expect(exported.plan.algorithm).toBe('cpp_insertion_v1');
  expect(exported.baseline.input_hash).toBe(exported.comparison.input_hash);
  expect(exported.comparison_scenario.roster).toBeNull();
  expect(exported.scenario.roster).toEqual(result.staffing.engineer_ids);
  expect(exported.plan.metrics.assigned).toBeGreaterThan(exported.baseline.metrics.assigned);
  await page.route('**/optimize', (route) =>
    route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Солвер недоступен. Повторите расчёт.' }),
    }),
  );
  await page.getByRole('tab', { name: 'План', exact: true }).click();
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  await expect(page.getByRole('alert')).toContainText('Солвер недоступен');
  await expect(page.locator('.visit')).toHaveCount(result.plan.metrics.assigned);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: testInfo.outputPath('comparison-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});

test('replan, preserve history, reject stale events, restore and export full snapshot', async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.goto('/');
  await uploadSample(page);
  await expect(page.locator('.request-link')).toHaveCount(66);
  const initialResponse = page.waitForResponse((r) => r.url().endsWith('/optimize'));
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  const initial = await (await initialResponse).json();
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  const region = page.getByRole('region', { name: 'События рабочего дня' });
  await expect(page.locator('.visit')).toHaveCount(initial.plan.metrics.assigned);
  expect(initial.staffing.status).toBe('active');
  await expect(page.getByRole('button', { name: /Утвердить/ })).toHaveCount(0);
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  await page.getByLabel('Время события').filter({ visible: true }).fill('12:00:00');
  await page.getByLabel('ID новой заявки').fill('browser-urgent');
  await page.getByLabel('Адрес новой заявки').fill('Тестовая улица, 123');
  await page.getByLabel('Окно начала: с').fill('14:00:00');
  await page.getByLabel('Окно начала: до').fill('20:00:00');
  const nextResponse = page.waitForResponse((r) => r.url().endsWith('/events/preview'));
  await page.getByRole('button', { name: 'Рассчитать вариант' }).click();
  const proposed = await (await nextResponse).json();
  const newRequest = proposed.result.scenario.requests.find(
    (r: { id: string }) => r.id === 'Восток:event-browser-urgent',
  );
  expect(newRequest.work_type).toBe('additional');
  expect(newRequest.urgent).toBe(false);
  await expect(page.locator('.visit')).toHaveCount(initial.plan.metrics.assigned);
  await expect(page.locator('.request-link')).toHaveCount(66);
  const proposal = page.getByRole('region', { name: 'Вариант для принятия' });
  await expect(proposal).toContainText('ПЛАН ЕЩЁ НЕ ИЗМЕНЁН');
  await expect(proposal.getByLabel('Неназначенные по приоритету')).toContainText('аварии:');
  await page.screenshot({ path: testInfo.outputPath('proposal-desktop.png'), fullPage: true });
  const commitResponse = page.waitForResponse((r) => r.url().endsWith('/events/commit'));
  await page.getByRole('button', { name: 'Принять вариант' }).click();
  const next = await (await commitResponse).json();
  expect(next).toEqual(proposed.result);
  expect(next.version).toBe(2);
  expect(next.comparison.objective_version).toBe('work_type_day_stability_v1+compact_v1');
  expect(next.comparison.optimized_score).toHaveLength(11);
  await expect(page.locator('.visit')).toHaveCount(next.plan.metrics.assigned);
  await expect(page.locator('.request-link')).toHaveCount(67);
  await expect(region.locator('.event-changes')).toContainText('Восток:event-browser-urgent');
  expect(next.frozen_request_ids.length).toBeGreaterThan(0);
  for (const route of initial.plan.routes) {
    const committed = route.visits.filter(
      (v: { arrival_s: number; travel_s: number; start_s: number }) =>
        v.arrival_s - v.travel_s < 43200 || v.start_s <= 43200,
    );
    expect(
      next.plan.routes
        .find((r: { engineer_id: string }) => r.engineer_id === route.engineer_id)
        .visits.slice(0, committed.length),
    ).toEqual(committed);
  }
  await expect(page.locator('.visit.frozen')).toHaveCount(next.frozen_request_ids.length);
  await page.screenshot({ path: testInfo.outputPath('events-desktop.png'), fullPage: true });
  // History and concurrency checks remain in the API; version controls are hidden from dispatchers.
  await expect(page.getByRole('button', { name: /Предыдущая версия|Текущая версия/ })).toHaveCount(
    0,
  );
  const previous = await (
    await page.request.get(`/api/sessions/${initial.session_id}?version=1`)
  ).json();
  expect(previous.plan).toEqual(initial.plan);
  const stale = await page.request.post(`/api/sessions/${initial.session_id}/events/preview`, {
    data: {
      type: 'engineer_unavailable',
      engineer_id: initial.staffing.engineer_ids[0],
      event_id: 'browser-stale-event',
      expected_version: initial.version,
      at_s: 43200,
      mode: 'preserve',
      max_delay_s: 900,
    },
  });
  expect(stale.status()).toBe(409);
  expect((await stale.json()).detail).toContain('План уже изменился');
  await expect(page.locator('.request-link')).toHaveCount(67);
  await page.reload();
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  await expect(page.locator('.request-link')).toHaveCount(67);
  await expect(page.locator('.request-link')).toHaveCount(67);
  const downloaded = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Экспорт плана' }).click();
  await page.getByRole('button', { name: 'Полный план · JSON' }).click();
  const stream = await (await downloaded).createReadStream();
  const chunks = [];
  for await (const chunk of stream!) chunks.push(chunk);
  const exported = JSON.parse(Buffer.concat(chunks).toString());
  expect(exported.scenario.planning_state.at_s).toBe(43200);
  expect(exported.scenario.matrices.car.locations.length).toBeGreaterThan(1);
  expect(exported.plan).toEqual(next.plan);
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  await openCancellation(page, 'Восток:event-browser-urgent');
  // A rejected request must leave the last accepted plan in place.
  await page.route('**/events/preview', (route) =>
    route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Ошибка расчёта события' }),
    }),
  );
  await page.getByRole('button', { name: 'Рассчитать вариант' }).click();
  await expect(page.getByRole('alert')).toContainText('Ошибка расчёта события');
  await expect(page.locator('.request-link')).toHaveCount(67);
  await page.unroute('**/events/preview');
  await page.getByRole('button', { name: 'Рассчитать вариант' }).click();
  await expect(page.getByRole('region', { name: 'Вариант для принятия' })).toBeVisible();
  await expect(page.locator('.request-link')).toHaveCount(67);
  await page.getByRole('button', { name: 'Принять вариант' }).click();
  await expect(page.locator('.request-link')).toHaveCount(66);
  await expect(page.locator('.request-link')).toHaveCount(66);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  await openUnavailability(page, initial.staffing.engineer_ids[0]);
  await expect(page.getByLabel('Режим перепланирования').filter({ visible: true })).toHaveValue(
    'flexible',
  );
  await page.getByRole('button', { name: 'Рассчитать вариант' }).click();
  await expect(proposal).toContainText('Вариант с перестановками');
  await page.getByRole('button', { name: 'Отклонить вариант' }).click();
  await expect(page.locator('.request-link')).toHaveCount(66);
  await page
    .getByLabel('Режим перепланирования')
    .filter({ visible: true })
    .selectOption('preserve');
  await page.getByRole('button', { name: 'Рассчитать вариант' }).click();
  await expect(proposal).toContainText('Вариант с сохранением расписания');
  await expect(page.locator('.request-link')).toHaveCount(66);
  await page.screenshot({ path: testInfo.outputPath('events-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});

test('map, address correction, frozen geography versions and export without live tiles', async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.goto('/');
  await uploadSample(page);
  const region = page.getByRole('region', { name: 'Карта заявок', exact: true });
  await expect(region.getByLabel('Интерактивная карта')).toBeVisible();
  await expect(region.getByRole('status')).toContainText('Подложка карты недоступна');
  const scenario = await (await page.request.get('/api/scenarios/Восток')).json();
  const catalog = await (await page.request.get('/api/scenarios/Восток/geography')).json();
  const uploadId = await page.evaluate(() => localStorage.getItem('dispatch-upload'));
  const engineers = await page.evaluate(() => {
    const draft = JSON.parse(localStorage.getItem('dispatch-team-v1')!).team;
    return draft
      .filter((e: { enabled: boolean }) => e.enabled)
      .map(
        (e: {
          id: string;
          name: string;
          skills: string[];
          profile: string;
          start: string;
          end: string;
        }) => ({
          id: e.id,
          name: e.name,
          skills: e.skills,
          profile: e.profile,
          shift_start_s: Number(e.start.slice(0, 2)) * 3600 + Number(e.start.slice(3)) * 60,
          shift_end_s: Number(e.end.slice(0, 2)) * 3600 + Number(e.end.slice(3)) * 60,
        }),
      );
  });
  const preview = await (
    await page.request.post(`/api/uploads/${uploadId}/optimize`, {
      data: {
        engineers,
        search: { rounds: 16, max_evaluations: 400000, time_limit_ms: 0 },
      },
    })
  ).json();
  const engineer = preview.plan.routes.find((r: { visits: unknown[] }) => r.visits.length >= 4);
  const jobs = engineer.visits.map((v: { request_id: string }) =>
    scenario.requests.find((r: { id: string }) => r.id === v.request_id),
  );
  const address = jobs[0].address;
  const key = jobs[0].location_id;
  // Tests set deterministic local coordinates; no map/geocoder calls are made.
  for (const [i, job] of [
    jobs[0],
    jobs[1],
    jobs[3],
    {
      address: scenario.office_address,
      location_id: scenario.office_location_id,
    },
  ].entries()) {
    const seeded = await page.request.put(`/api/geography/${job.location_id}`, {
      data: {
        address: job.address,
        expected_revision: catalog[job.location_id].revision,
        lat: 55.699 + i * 0.005,
        lon: 37.701 + i * 0.006,
        note: 'Browser fixture',
      },
    });
    expect(seeded.ok()).toBeTruthy();
  }
  await page.reload();
  const firstResponse = page.waitForResponse(
    (r) => r.url().endsWith('/optimize') && r.request().method() === 'POST',
  );
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  const first = await (await firstResponse).json();
  await region.getByRole('button', { name: address, exact: true }).click();
  await expect(page.getByRole('region', { name: 'Детали заявки' })).toContainText(address);
  await page.getByRole('button', { name: 'К списку заявок' }).click();
  await page
    .getByRole('tabpanel', { name: 'Таймлайн', exact: true })
    .locator(`[data-engineer-id="${engineer.engineer_id}"] .engineer-focus`)
    .click();
  await expect(region.locator('.map-stop')).toHaveCount(3);
  await expect(region.locator('.map-office')).toHaveCount(1);
  // Only office → first → second; the unknown third stop prevents a line to the fourth.
  await expect(region.locator('.leaflet-overlay-pane path')).toHaveCount(2);
  await expect(region.getByRole('list', { name: 'Порядок визитов' }).locator('li')).toHaveCount(
    engineer.visits.length,
  );
  await region.locator('.map-visit-link').first().click();
  await expect(page.getByRole('region', { name: 'Детали заявки' })).toContainText(
    engineer.visits[0].request_id.split(':').pop(),
  );
  await page.getByRole('button', { name: 'К списку заявок' }).click();
  const latest = await (
    await page.request.get(`/api/sessions/${first.session_id}/geography`)
  ).json();
  const saved = await page.request.put(`/api/geography/${key}`, {
    data: { address, expected_revision: latest[key].revision, lat: 55.701, lon: 37.702 },
  });
  expect(saved.ok()).toBeTruthy();
  expect(await (await page.request.get(`/api/sessions/${first.session_id}`)).json()).toEqual(first);
  const updated = await (
    await page.request.post(`/api/sessions/${first.session_id}/geography/refresh`, {
      data: { operation_id: 'legacy-coordinate-refresh', expected_version: first.version },
    })
  ).json();
  await page.reload();
  expect(updated.version).toBe(2);
  expect(updated.plan.routes).toEqual(first.plan.routes);
  expect(updated.plan.metrics).toEqual(first.plan.metrics);
  expect(updated.scenario.geography[key].point.lat).toBe(55.701);
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  await expect(page.locator('.visit')).toHaveCount(updated.plan.metrics.assigned);
  const old = await (await page.request.get(`/api/sessions/${first.session_id}?version=1`)).json();
  expect(old.scenario.geography[key].point.lat).toBe(55.699);
  const downloaded = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Экспорт плана' }).click();
  await page.getByRole('button', { name: 'Полный план · JSON' }).click();
  const stream = await (await downloaded).createReadStream();
  const chunks = [];
  for await (const chunk of stream!) chunks.push(chunk);
  const exported = JSON.parse(Buffer.concat(chunks).toString());
  expect(exported.scenario.geography[key].point.lat).toBe(55.701);
  await page.setViewportSize({ width: 390, height: 844 });
  await page
    .getByRole('tabpanel', { name: 'Таймлайн', exact: true })
    .locator(`[data-engineer-id="${engineer.engineer_id}"] .engineer-focus`)
    .click();
  await page.screenshot({ path: testInfo.outputPath('map-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});

test('selected address travels with urgent proposal and survives acceptance and reload', async ({
  page,
}, testInfo) => {
  // Keep this point distinct from the earlier catalogue edit: co-located jobs
  // intentionally share one map marker, titled after the first job.
  const house = {
    ...suggestedHouse,
    point: { ...suggestedHouse.point, lat: 55.722345, lon: 37.622345 },
  };
  await mockSuggestions(page, house);
  await page.goto('/');
  await uploadSample(page);
  await expect(page.locator('.request-link')).toHaveCount(66);
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  const events = page.getByRole('region', { name: 'События рабочего дня' });
  await expect(page.locator('.request-link')).toHaveCount(66);
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  await page.getByLabel('ID новой заявки').fill('suggested-address');
  await page.getByLabel('Адрес новой заявки').fill('Москва Проверочная 5/1');
  await page.getByRole('listbox', { name: 'Подсказки адреса' }).getByRole('option').last().click();
  await expect(page.getByLabel('Адрес новой заявки')).toHaveValue(suggestedHouse.label);
  await expect(page.getByLabel('Точка выбранного адреса')).toBeVisible();
  await events
    .locator('.event-address')
    .screenshot({ path: testInfo.outputPath('urgent-address-map.png') });
  const proposalResponse = page.waitForResponse((r) => r.url().endsWith('/events/preview'));
  await page.getByRole('button', { name: 'Рассчитать вариант' }).click();
  const proposed = await (await proposalResponse).json();
  const request = proposed.result.scenario.requests.find((r: { id: string }) =>
    r.id.endsWith('suggested-address'),
  );
  expect(request.address).toBe(suggestedHouse.label);
  expect(request.raw.original_address).toBe('Москва Проверочная 5/1');
  expect(proposed.result.scenario.geography[request.location_id].point).toEqual(house.point);
  await expect(page.locator('.request-link')).toHaveCount(66);
  await page.getByRole('button', { name: 'Принять вариант' }).click();
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  await expect(page.locator('.request-link')).toHaveCount(67);
  await page.reload();
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  await expect(page.locator('.request-link')).toHaveCount(67);
  const region = page.getByRole('region', { name: 'Карта заявок', exact: true });
  await expect(
    region.getByRole('button', { name: suggestedHouse.label, exact: true }),
  ).toBeVisible();
  const catalog = await (
    await page.request.get(`/api/sessions/${proposed.result.session_id}/geography`)
  ).json();
  expect(catalog[request.location_id].point).toEqual(house.point);
});
