import { expect, test } from '@playwright/test';
import { uploadSample, uploadEngineers } from './upload-fixture';

test('rechecking an unchanged CSV preserves the plan and settings until its contents change', async ({
  page,
}) => {
  const calculations: string[] = [];
  page.on('request', (r) => {
    if (/\/(baseline|optimize)$/.test(r.url())) calculations.push(r.url());
  });
  const file = (address = 'Первый дом') => ({
    name: 'Мой участок.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from(
      `ID;Тип;Адрес;Начало;Окончание;Широта;Долгота\n1;Подключение;${address};17.08.2026 10:00;17.08.2026 18:00;55.75;37.62\nАдрес офиса;Офис;55.76;37.61\n`,
    ),
  });
  const storedIds = () =>
    page.evaluate(() => ({
      session: localStorage.getItem('dispatch-session'),
      upload: localStorage.getItem('dispatch-upload'),
    }));
  const validate = async () => {
    await page.getByRole('button', { name: 'Проверить файл', exact: true }).click();
    await expect(
      page.getByRole('region', { name: 'Загрузка заявок' }).locator('.file-valid'),
    ).toContainText('Файл проверен · 1 заявка');
    await expect(page.getByRole('button', { name: 'Рассчитать план', exact: true })).toBeEnabled();
  };
  await page.goto('/');
  await page.getByLabel('Файл заявок').setInputFiles(file());
  await uploadEngineers(page, 1);
  await validate();
  await uploadEngineers(page, 1);
  await page.locator('.team-settings > summary').click();
  await page.locator('.engineer-settings > summary').first().click();
  const transport = page
    .getByRole('group', { name: 'Параметры инженера 1', exact: true })
    .getByRole('combobox', { name: 'Транспорт', exact: true });
  await transport.selectOption('bicycle');
  await page.getByRole('button', { name: 'Рассчитать план', exact: true }).click();
  await expect(page.locator('.visit')).toHaveCount(1);
  await expect(page.locator('.route-overview').first()).toBeVisible();
  const original = await storedIds();
  expect(original.session).toBeTruthy();
  expect(original.upload).toBeTruthy();

  await validate();
  expect(await storedIds()).toEqual(original);
  await expect(page.locator('.visit')).toHaveCount(1);
  await expect(page.locator('.route-overview').first()).toBeVisible();
  await expect(page.locator('.team-count')).toContainText('1 из 1');
  await expect(transport).toHaveValue('bicycle');

  // Restoring a saved plan and selecting the same file must also be harmless.
  await page.reload();
  await expect(page.locator('.visit')).toHaveCount(1);
  await page.getByLabel('Файл заявок').setInputFiles(file());
  await validate();
  expect(await storedIds()).toEqual(original);
  await expect(page.locator('.visit')).toHaveCount(1);
  await expect(page.locator('.route-overview').first()).toBeVisible();
  expect(calculations).toHaveLength(1);

  // A genuinely changed file still replaces the input, even with the same name.
  await page.getByLabel('Файл заявок').setInputFiles(file('Второй дом'));
  await validate();
  await expect(page.locator('.visit')).toHaveCount(0);
  await expect(page.locator('.route-overview')).toHaveCount(0);
  await expect(page.locator('.map-stop[aria-label="Второй дом"]')).toBeVisible();
  const replacement = await storedIds();
  expect(replacement.session).toBeNull();
  expect(replacement.upload).toBeTruthy();
  expect(replacement.upload).not.toBe(original.upload);
  expect(calculations).toHaveLength(1);
});

test('coordinate-only requests show the reverse address without replacing their input point', async ({
  page,
}) => {
  const label = 'Рядом: Москва, Симферопольский проезд, 7';
  let original: { lat: number; lon: number } | undefined;
  await page.route('**/api/uploads/*/optimize', async (route) => {
    const response = await route.fetch();
    const run = await response.json();
    const job = run.scenario.requests.find((r: { address: string }) =>
      r.address.startsWith('Координаты '),
    );
    const point = run.scenario.geography[job.location_id].point;
    original = { lat: point.lat, lon: point.lon };
    job.display_address = label;
    point.label = label;
    await route.fulfill({ response, json: run });
  });
  await page.goto('/');
  await uploadSample(page, 'Югоцентр', true);
  await page.getByRole('button', { name: 'Рассчитать план', exact: true }).click();
  const marker = page.locator(`.map-stop[aria-label="${label}"]`);
  await expect(marker).toBeVisible();
  expect(original).toBeDefined();
  await marker.click();
  await expect(page.getByRole('region', { name: 'Детали заявки' })).toContainText(label);
  await page.getByRole('button', { name: 'К списку заявок' }).click();
  await page.getByRole('tab', { name: 'Заявки', exact: true }).click();
  await page.getByLabel('Поиск заявки').fill('Симферопольский');
  await expect(page.locator('.request-cards')).toContainText(label);
});

test('a replacement file with the same name refreshes requests and fits their new coordinates', async ({
  page,
}) => {
  await page.goto('/');
  for (const [address, lat, lon] of [
    ['Первый дом', 55.75, 37.62],
    ['Второй дом', 55.55, 37.85],
  ] as const) {
    await page.getByLabel('Файл заявок').setInputFiles({
      name: 'Мой участок.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from(
        `ID;Тип;Адрес;Начало;Окончание;Широта;Долгота\n1;Подключение;${address};17.08.2026 10:00;17.08.2026 18:00;${lat};${lon}\nАдрес офиса;Офис;${lat + 0.01};${lon}\n`,
      ),
    });
    await page.getByRole('button', { name: 'Проверить файл', exact: true }).click();
    const marker = page.locator(`.map-stop[aria-label="${address}"]`);
    await expect(marker).toBeVisible();
    const map = (await page.locator('.leaflet-container').boundingBox())!;
    await expect(async () => {
      const pin = (await marker.boundingBox())!;
      expect(pin.x).toBeGreaterThanOrEqual(map.x);
      expect(pin.y).toBeGreaterThanOrEqual(map.y);
      expect(pin.x + pin.width).toBeLessThanOrEqual(map.x + map.width);
      expect(pin.y + pin.height).toBeLessThanOrEqual(map.y + map.height);
    }).toPass();
  }
  await expect(page.locator('.map-stop[aria-label="Первый дом"]')).toHaveCount(0);
});

test('CSV validation reports row errors without starting a calculation or replacing a plan', async ({
  page,
}, info) => {
  const calculations: string[] = [];
  page.on('request', (r) => {
    if (/\/(baseline|optimize)$/.test(r.url())) calculations.push(r.url());
  });
  await page.goto('/');
  await expect(page.getByLabel('Файл заявок')).toBeVisible();
  await expect(page.getByRole('combobox')).toHaveCount(0);
  await expect(page.getByText('Тестовые CSV', { exact: true })).toHaveCount(0);
  await uploadSample(page, 'Восток', true);
  expect(calculations).toHaveLength(0);
  await page.getByRole('button', { name: 'Рассчитать план', exact: true }).click();
  await expect(page.locator('.visit').first()).toBeVisible();
  const count = await page.locator('.visit').count();
  await page.getByLabel('Файл заявок').setInputFiles({
    name: 'Новый участок.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from(
      'ID;Тип;Адрес;Начало;Окончание\n42;Подключение;Москва;не дата;17.08.2026 18:00\nАдрес офиса;Москва\n',
    ),
  });
  await expect(page.getByRole('button', { name: 'Рассчитать план', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Проверить файл', exact: true }).click();
  await expect(page.locator('.file-errors')).toContainText('Строка 2');
  await expect(page.locator('.file-errors')).toContainText('Начало');
  await expect(page.locator('.file-errors')).toContainText('дату и время');
  await expect(page.locator('.visit')).toHaveCount(count);
  expect(calculations).toHaveLength(1);
  await page.screenshot({ path: info.outputPath('invalid-upload.png'), fullPage: true });
});

test('editable engineer transport, skills and shift survive calculation and reload; everyone starts at the office', async ({
  page,
}, info) => {
  await page.goto('/');
  await uploadSample(page, 'Восток', true);
  await uploadEngineers(page, 1);
  await page.locator('.team-settings > summary').click();
  await page.locator('.engineer-settings > summary').first().click();
  const editor = page.getByRole('group', { name: 'Параметры инженера 1', exact: true });
  await editor.getByRole('combobox', { name: 'Транспорт', exact: true }).selectOption('bicycle');
  await editor.getByLabel('Начало смены').fill('10:00');
  await editor.getByLabel('Конец смены').fill('19:30');
  for (const checkbox of await editor.getByRole('checkbox').all()) await checkbox.uncheck();
  await expect(page.getByRole('button', { name: 'Рассчитать план', exact: true })).toBeDisabled();
  await editor.getByLabel('Локальные работы', { exact: true }).check();
  await expect(editor.getByLabel('Стартовая точка')).toHaveCount(0);
  await expect(editor.getByLabel('Широта')).toHaveCount(0);
  await page.screenshot({ path: info.outputPath('engineer-settings.png'), fullPage: true });
  const response = page.waitForResponse((r) => r.url().endsWith('/optimize'));
  await page.getByRole('button', { name: 'Рассчитать план', exact: true }).click();
  const run = await (await response).json();
  expect(run.engineers).toHaveLength(1);
  expect(run.engineers[0]).toMatchObject({
    profile: 'bicycle',
    skills: ['local'],
    shift_start_s: 36000,
    shift_end_s: 70200,
  });
  expect(run.engineers[0].start_location_id).toBe(run.scenario.office_location_id);
  await expect(page.locator('.workspace-progress')).toHaveCount(0);
  await page.reload();
  await expect(page.locator('.team-count')).toContainText('1 из 1');
  await page.locator('.team-settings > summary').click();
  await page.locator('.engineer-settings > summary').first().click();
  await expect(editor.getByRole('combobox', { name: 'Транспорт', exact: true })).toHaveValue(
    'bicycle',
  );
  await expect(editor.getByLabel('Конец смены')).toHaveValue('19:30');
  await expect(editor.getByLabel('Локальные работы', { exact: true })).toBeChecked();
  await expect(editor.getByLabel('Стартовая точка')).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByLabel('Файл заявок')).toBeVisible();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
  ).toBeTruthy();
  await page.screenshot({ path: info.outputPath('upload-mobile.png'), fullPage: true });
});

test('calculation percentage belongs to this operation and clears after success or failure', async ({
  page,
}, info) => {
  await page.goto('/');
  await uploadSample(page);
  let release = () => {};
  let gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let id = '';
  let fail = false;
  let percent = 42;
  await page.route('**/api/uploads/*/optimize', async (route) => {
    id = route.request().headers()['x-calculation-id'];
    await gate;
    if (fail)
      await route.fulfill({
        status: 503,
        json: { detail: 'Не удалось построить маршруты. Повторите расчёт.' },
      });
    else
      await route.fulfill({
        response: await route.fetch({
          postData: {
            ...route.request().postDataJSON(),
            search: { rounds: 16, max_evaluations: 400000, time_limit_ms: 0 },
          },
        }),
      });
  });
  await page.route('**/api/calculations/*', (route) => {
    expect(route.request().url().split('/').pop()).toBe(id);
    return route.fulfill({ json: { id, stage: 'Матрицы переездов', percent, status: 'running' } });
  });
  const button = page.getByRole('button', { name: 'Рассчитать план', exact: true });
  const progress = page.getByRole('progressbar', { name: 'Расчёт плана' });
  try {
    await button.click();
    await expect(progress).toHaveAttribute('aria-valuenow', '42');
    await expect(page.getByText('42%', { exact: true })).toBeVisible();
    await expect(button).toBeDisabled();
    await expect
      .poll(async () => {
        const track = (await progress.boundingBox())!;
        const fill = (await progress.locator('span').boundingBox())!;
        return Math.round((fill.width / track.width) * 100);
      })
      .toBe(42);
    await page.screenshot({ path: info.outputPath('calculation-progress.png'), fullPage: true });
  } finally {
    release();
  }
  await expect(progress).toHaveCount(0);
  await expect(page.locator('.visit').first()).toBeVisible();
  const visits = await page.locator('.visit').count();
  const previousId = id;
  fail = true;
  percent = 12;
  gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  try {
    await button.click();
    await expect(progress).toHaveAttribute('aria-valuenow', '12');
    expect(id).not.toBe(previousId);
  } finally {
    release();
  }
  await expect(progress).toHaveCount(0);
  await expect(page.getByRole('alert')).toContainText('Не удалось построить маршруты');
  await expect(page.locator('.visit')).toHaveCount(visits);
  await expect(button).toBeEnabled();
});
