import { uploadSample } from './upload-fixture';
import { expect, test } from '@playwright/test';
import { withMapCoordinates } from './map-fixture';

test('three panels, transport icons and independent route visibility preserve the plan', async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.route('**/optimize', async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: withMapCoordinates(await response.json()) });
  });
  await page.goto('/');
  await uploadSample(page);
  const response = page.waitForResponse((r) => r.url().endsWith('/optimize'));
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  const run = await (await response).json();
  const tools = await page.getByLabel('Панель диспетчера').boundingBox();
  const map = await page.locator('.dispatch-map-area').boundingBox();
  const schedule = await page
    .getByRole('region', { name: 'Расписание инженеров', exact: true })
    .boundingBox();
  expect(map!.x).toBeGreaterThanOrEqual(tools!.x + tools!.width);
  expect(schedule!.x).toBe(map!.x);
  expect(schedule!.y).toBeGreaterThanOrEqual(map!.y + map!.height - 1);
  expect(schedule!.y + schedule!.height).toBeLessThanOrEqual(1001);
  await expect(page.getByLabel('Маршрут на карте')).toHaveCount(0);
  const mapSurface = (await page.locator('.route-map').boundingBox())!;
  const separator = (await page
    .getByRole('separator', { name: 'Высота панели инженеров' })
    .boundingBox())!;
  expect(mapSurface.y + mapSurface.height).toBeCloseTo(separator.y, 0);
  const zoomOffset = await page.locator('.route-map').evaluate((element) => {
    const map = element.getBoundingClientRect();
    const zoom = element.querySelector('.leaflet-control-zoom')!.getBoundingClientRect();
    return { x: zoom.x - map.x, y: zoom.y + zoom.height / 2 - (map.y + map.height / 2) };
  });
  expect(zoomOffset.x).toBe(10);
  expect(zoomOffset.y).toBeCloseTo(0, 0);
  await expect(page.locator('.route-map .leaflet-control-attribution')).toContainText(
    'Адресные данные',
  );
  await expect(page.locator('.map-office svg')).toHaveCount(1);
  await expect(page.locator('.map-office')).not.toContainText('О');
  const pin = page.locator('.location-pin').first();
  await expect(pin).toHaveCSS('width', '24px');
  await expect(pin).toHaveCSS('border-radius', '6px');
  for (const marker of [pin, page.locator('.office-pin')]) {
    const offset = await marker.evaluate((element) => {
      const style = getComputedStyle(element);
      const tail = getComputedStyle(element, '::after');
      const matrix = new DOMMatrix(tail.transform);
      return (
        parseFloat(style.borderLeftWidth) +
        parseFloat(tail.left) +
        parseFloat(tail.width) / 2 +
        matrix.e -
        element.getBoundingClientRect().width / 2
      );
    });
    expect(Math.abs(offset)).toBeLessThan(0.1);
  }
  const marker = page.locator('.map-stop').first();
  await expect(page.locator('.map-stop[title], .map-office[title]')).toHaveCount(0);
  await expect(marker).toHaveAttribute('aria-label', /.+/);
  await marker.hover();
  await expect(page.locator('.leaflet-tooltip')).toHaveCount(1);
  await page.mouse.move(0, 0);
  for (const profile of ['Автомобиль', 'Пешком', 'Общественный транспорт', 'Велосипед']) {
    await expect(page.getByRole('img', { name: profile, exact: true }).first()).toBeVisible();
  }
  // Choose two nonempty routes from the seeded display fixture.
  const routeIndexes = await page
    .locator('.leaflet-overlay-pane path')
    .evaluateAll((paths) =>
      [
        ...new Set(
          paths.flatMap((path) =>
            [...path.classList].filter((name) => name.startsWith('engineer-route-')),
          ),
        ),
      ].map((name) => Number(name.split('-').pop())),
    );
  expect(routeIndexes.length).toBeGreaterThanOrEqual(2);
  const [firstIndex, secondIndex] = routeIndexes;
  const first = page.locator(`.engineer-route-${firstIndex}`);
  const second = page.locator(`.engineer-route-${secondIndex}`);
  await expect(first.first()).toBeVisible();
  const secondCount = await second.count();
  expect(secondCount).toBeGreaterThan(0);
  const writes: string[] = [];
  page.on('request', (request) => {
    if (request.method() === 'POST') writes.push(request.url());
  });
  await page
    .getByRole('button', { name: `Скрыть маршрут инженера ${firstIndex + 1}`, exact: true })
    .click();
  await expect(first).toHaveCount(0);
  await expect(second).toHaveCount(secondCount);
  await expect(
    page.getByRole('button', { name: `Показать маршрут инженера ${firstIndex + 1}`, exact: true }),
  ).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('.visit')).toHaveCount(run.plan.metrics.assigned);
  await page.getByRole('button', { name: 'Скрыть все', exact: true }).click();
  await expect(page.locator('.leaflet-overlay-pane path')).toHaveCount(0);
  await expect(page.locator('.map-office')).toBeVisible();
  await page
    .getByRole('button', { name: `Показать маршрут инженера ${firstIndex + 1}`, exact: true })
    .click();
  await page
    .getByRole('button', { name: `Показать маршрут инженера ${secondIndex + 1}`, exact: true })
    .click();
  await expect(first.first()).toBeVisible();
  await expect(second).toHaveCount(secondCount);
  await expect(
    page.locator(
      `.leaflet-overlay-pane path:not(.engineer-route-${firstIndex}):not(.engineer-route-${secondIndex})`,
    ),
  ).toHaveCount(0);
  await page.getByRole('button', { name: 'Показать все', exact: true }).click();
  expect(writes).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath('workspace-desktop.png') });
  await page.getByRole('tab', { name: 'Заявки', exact: true }).click();
  await expect(page.getByRole('tabpanel', { name: 'Заявки', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Без назначения', exact: true }).click();
  await expect(page.locator('.request-cards > li')).toHaveCount(run.plan.metrics.unassigned);
  await page.getByLabel('Поиск заявки').fill('нет такого адреса');
  await expect(page.getByText('Нет заявок по выбранным условиям')).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    .toBe(true);
  await page.screenshot({ path: testInfo.outputPath('workspace-mobile.png'), fullPage: true });
  expect(errors).toEqual([]);
});

test('engineer panel resizes within bounds and separates timeline from accurate metrics', async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await uploadSample(page);
  const response = page.waitForResponse((r) => r.url().endsWith('/optimize'));
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  const run = await (await response).json();
  const writes: string[] = [];
  page.on('request', (request) => {
    if (request.method() === 'POST') writes.push(request.url());
  });
  const panel = page.getByRole('region', { name: 'Расписание инженеров', exact: true });
  const divider = page.getByRole('separator', { name: 'Высота панели инженеров' });
  const initial = (await panel.boundingBox())!;
  const handle = (await divider.boundingBox())!;
  await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2);
  await page.mouse.down();
  await page.mouse.move(handle.x + handle.width / 2, handle.y - 145, { steps: 5 });
  await page.mouse.up();
  await expect
    .poll(async () => (await panel.boundingBox())!.height)
    .toBeGreaterThan(initial.height + 140);
  const grown = (await panel.boundingBox())!.height;
  await divider.press('ArrowDown');
  await expect.poll(async () => (await panel.boundingBox())!.height).toBe(grown - 32);
  await divider.press('End');
  expect((await page.locator('.dispatch-map-area').boundingBox())!.height).toBeGreaterThanOrEqual(
    180,
  );
  await divider.press('Home');
  await expect.poll(async () => (await panel.boundingBox())!.height).toBe(180);
  await divider.dblclick();
  await expect.poll(async () => (await panel.boundingBox())!.height).toBe(initial.height);
  const timeline = page.getByRole('tabpanel', { name: 'Таймлайн', exact: true });
  await expect(timeline).toBeVisible();
  await expect(timeline.getByText('Путь, км', { exact: true })).toHaveCount(0);
  await page.getByRole('tab', { name: 'Метрики', exact: true }).click();
  await expect(timeline).toBeHidden();
  const metrics = page.getByRole('table', { name: 'Метрики инженеров' });
  const cells = metrics.locator('tbody tr').first().locator('td');
  const route = run.plan.routes.find(
    (item: { engineer_id: string }) => item.engineer_id === run.engineers[0].id,
  );
  await expect(cells.nth(0)).toHaveText(String(route.visits.length));
  await expect(cells.nth(1)).toHaveText((route.distance_m / 1000).toFixed(1));
  await expect(cells.nth(2)).toHaveText(
    String(
      Math.round(
        route.visits.reduce((sum: number, visit: { travel_s: number }) => sum + visit.travel_s, 0) /
          60,
      ),
    ),
  );
  await metrics.getByRole('button', { name: 'Скрыть маршрут инженера 1', exact: true }).click();
  await expect(page.locator('.engineer-route-0')).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('engineer-metrics.png') });
  await page.getByRole('tab', { name: 'Метрики', exact: true }).press('ArrowLeft');
  await expect(page.getByRole('tab', { name: 'Таймлайн', exact: true })).toBeFocused();
  await expect(
    timeline.getByRole('button', { name: 'Показать маршрут инженера 1', exact: true }),
  ).toBeVisible();
  await expect(page.locator('.visit')).toHaveCount(run.plan.metrics.assigned);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    .toBe(true);
  await divider.press('End');
  expect((await panel.boundingBox())!.height).toBeLessThanOrEqual(844 * 0.75);
  await page.screenshot({
    path: testInfo.outputPath('engineers-resized-mobile.png'),
    fullPage: true,
  });
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});

test('sidebar event tabs retain input and support keyboard navigation', async ({ page }) => {
  await page.goto('/');
  await uploadSample(page);
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  await page.getByLabel('Адрес новой заявки').fill('Тестовый адрес 12');
  await page.getByRole('button', { name: 'Свернуть левую панель' }).click();
  await expect(page.getByLabel('Адрес новой заявки')).toBeHidden();
  await page.getByRole('tab', { name: 'Заявка', exact: true }).press('Enter');
  await expect(page.getByRole('tab', { name: 'Заявка', exact: true })).toHaveAttribute(
    'aria-selected',
    'true',
  );
  await expect(page.getByLabel('Адрес новой заявки')).toHaveValue('Тестовый адрес 12');
  await page.getByRole('tab', { name: 'Инженеры', exact: true }).click();
  await expect(page.getByRole('list', { name: 'Инженеры смены' })).toBeVisible();
  await expect(page.getByLabel('Адрес новой заявки')).toBeHidden();
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  await expect(page.getByLabel('Адрес новой заявки')).toHaveValue('Тестовый адрес 12');
  const tab = page.getByRole('tab', { name: 'Заявка', exact: true });
  await tab.focus();
  await page.keyboard.press('ArrowDown');
  await expect(page.getByRole('tab', { name: 'Заявки', exact: true })).toBeFocused();
  await expect(page.getByLabel('Поиск заявки')).toBeVisible();
  await page.keyboard.press('End');
  await expect(page.getByRole('tab', { name: 'Инженеры', exact: true })).toBeFocused();
  await expect(page.getByRole('list', { name: 'Инженеры смены' })).toBeVisible();
});

test('sidebar content collapses on squeeze and tabs restore it without changing the plan', async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await uploadSample(page);
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  await expect(page.locator('.visit').first()).toBeVisible();
  const visits = await page.locator('.visit').count();
  const writes: string[] = [];
  page.on('request', (request) => {
    if (request.method() === 'POST') writes.push(request.url());
  });
  const panel = page.getByRole('complementary', { name: 'Панель диспетчера' });
  const content = page.locator('#tools-content');
  const divider = page.getByRole('separator', { name: 'Ширина левой панели' });
  const width = async () => (await panel.boundingBox())!.width;
  const initial = await width();
  const handle = (await divider.boundingBox())!;
  await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2);
  await page.mouse.down();
  await page.mouse.move(handle.x + handle.width / 2 + 120, handle.y + handle.height / 2, {
    steps: 8,
  });
  await page.mouse.up();
  await expect.poll(width).toBe(initial + 120);
  await divider.press('ArrowRight');
  await expect.poll(width).toBe(initial + 152);
  await divider.press('ArrowLeft');
  await expect.poll(width).toBe(initial + 120);
  await expect(divider).toHaveAttribute('aria-valuenow', String(initial + 120));
  await divider.press('End');
  await expect.poll(width).toBe(620);
  await divider.press('Home');
  await expect.poll(width).toBe(62);
  await expect(content).toBeHidden();
  await page.getByRole('tab', { name: 'План', exact: true }).click();
  await expect.poll(width).toBe(620);
  await divider.dblclick();
  await expect.poll(width).toBe(initial);
  await divider.press('ArrowRight');
  const expandedWidth = initial + 32;
  await expect.poll(width).toBe(expandedWidth);
  await page.getByRole('tab', { name: 'Заявки', exact: true }).click();
  await page.getByLabel('Поиск заявки').fill('Москва');
  await page.screenshot({ path: testInfo.outputPath('sidebar-resized.png') });
  const collapseHandle = (await divider.boundingBox())!;
  const dragY = collapseHandle.y + collapseHandle.height / 2;
  await page.mouse.move(collapseHandle.x + collapseHandle.width / 2, dragY);
  await page.mouse.down();
  // Minimum readable width does not collapse; squeezing further does.
  await page.mouse.move(283, dragY, { steps: 8 });
  await expect.poll(width).toBe(300);
  await expect(content).toBeVisible();
  await page.mouse.move(183, dragY, { steps: 8 });
  await expect(content).toBeHidden();
  // Capture stays on the separator after it snaps to the rail.
  await page.mouse.move(303, dragY, { steps: 4 });
  await expect(content).toBeVisible();
  await page.mouse.move(183, dragY, { steps: 4 });
  await page.mouse.up();
  await expect(content).toBeHidden();
  await expect(panel).toBeVisible();
  await expect(divider).toBeVisible();
  await expect(divider).toBeFocused();
  await expect.poll(width).toBe(62);
  await expect(divider).toHaveAttribute('aria-valuenow', '62');
  await expect(page.locator('.tool-rail [aria-selected="true"]')).toHaveCount(0);
  const tabs = page.getByRole('tablist', { name: 'Разделы диспетчера' }).getByRole('tab');
  await expect(tabs).toHaveCount(4);
  for (const tab of await tabs.all()) await expect(tab).toBeVisible();
  for (const selector of ['.dispatch-map-area', '#engineers-panel']) {
    const box = (await page.locator(selector).boundingBox())!;
    expect(box.x).toBe(68);
    expect(box.width).toBe(1372);
  }
  await page.screenshot({ path: testInfo.outputPath('sidebar-collapsed.png') });
  await page.getByRole('tab', { name: 'Заявки', exact: true }).press('Enter');
  await expect.poll(width).toBe(expandedWidth);
  await expect(page.getByLabel('Поиск заявки')).toHaveValue('Москва');
  // Every tab, including the currently selected one, can reopen the content.
  for (const tab of await tabs.all()) {
    await divider.press('Enter');
    await expect(content).toBeHidden();
    await tab.click();
    await expect(content).toBeVisible();
    await expect(tab).toHaveAttribute('aria-selected', 'true');
    await expect.poll(width).toBe(expandedWidth);
  }
  await page.getByRole('button', { name: 'Свернуть левую панель' }).click();
  await page.getByRole('tab', { name: 'Заявки', exact: true }).click();
  await expect(page.getByLabel('Поиск заявки')).toBeVisible();
  await expect.poll(width).toBe(expandedWidth);
  await page.getByRole('button', { name: 'Свернуть левую панель' }).click();
  await page
    .locator('.map-status-strip')
    .getByRole('button', { name: 'Неназначенные заявки', exact: true })
    .click();
  // Counters open the complete status subset; ordinary tab changes retain filters.
  await expect(page.getByLabel('Поиск заявки')).toHaveValue('');
  await page.getByLabel('Поиск заявки').fill('Москва');
  await expect(page.getByRole('tab', { name: 'Заявки', exact: true })).toHaveAttribute(
    'aria-selected',
    'true',
  );
  await divider.press('End');
  await page.setViewportSize({ width: 820, height: 1000 });
  await expect.poll(width).toBe(414);
  expect((await page.locator('.dispatch-map-area').boundingBox())!.width).toBe(400);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await expect.poll(width).toBe(620);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(divider).toBeHidden();
  await page.getByRole('button', { name: 'Свернуть левую панель' }).click();
  await expect(content).toBeHidden();
  for (const tab of await tabs.all()) await expect(tab).toBeVisible();
  await expect(page.locator('.route-map')).toBeVisible();
  await page.getByRole('tab', { name: 'Заявки', exact: true }).click();
  await expect(page.getByLabel('Поиск заявки')).toHaveValue('Москва');
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  await expect(page.locator('.visit')).toHaveCount(visits);
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});
