import { expect, test } from '@playwright/test';

const jobs = {
  name: 'Участок.csv',
  mimeType: 'text/csv',
  buffer: Buffer.from(
    'ID;Тип;Адрес;Начало;Окончание;Широта;Долгота\n1;Подключение;Дом;17.08.2026 10:00;17.08.2026 18:00;55.75;37.62\nАдрес офиса;Офис;55.76;37.61\n',
  ),
};
const teamFile = (secondId = 'beta') => ({
  name: 'Команда.csv',
  mimeType: 'text/csv',
  buffer: Buffer.from(
    `ID;Имя;Навыки;Транспорт;Начало смены;Конец смены\nalpha;Ирина Петрова;installation | local;Автомобиль;09:00;18:00\n${secondId};Борис Соколов;emergency;Велосипед;09:00;18:00\n`,
  ),
});

test('engineer CSV is required, office is shared, names and exclusions survive reload and validation', async ({
  page,
}, info) => {
  await page.goto('/');
  await page.getByLabel('Файл заявок').setInputFiles(jobs);
  await page.getByRole('button', { name: 'Проверить файл', exact: true }).click();
  const calculate = page.getByRole('button', { name: 'Рассчитать план', exact: true });
  await expect(calculate).toBeDisabled();
  const writes: string[] = [];
  page.on('request', (r) => {
    if (/\/(optimize|baseline|events\/preview)$/.test(r.url())) writes.push(r.url());
  });
  await page.getByLabel('Файл инженеров').setInputFiles(teamFile());
  await page.getByRole('button', { name: 'Проверить инженеров' }).click();
  await expect(page.locator('.team-count')).toHaveText('Доступны сегодня 2 из 2');
  expect(writes).toHaveLength(0);
  await expect(page.getByRole('spinbutton', { name: 'Инженеров в штате' })).toHaveCount(0);
  await page.locator('.team-settings > summary').click();
  await page.getByLabel('Доступен: Борис Соколов', { exact: true }).uncheck();
  await page.locator('.engineer-settings > summary').first().click();
  const editor = page.getByRole('group', { name: 'Параметры инженера 1' });
  await editor.getByLabel('Конец смены').fill('19:00');
  await expect(page.getByLabel('Стартовая точка')).toHaveCount(0);
  const response = page.waitForResponse((r) => r.url().endsWith('/optimize'));
  await calculate.click();
  const run = await (await response).json();
  expect(run.engineers).toHaveLength(1);
  expect(run.engineers[0]).toMatchObject({
    id: 'alpha',
    name: 'Ирина Петрова',
    shift_end_s: 68400,
    start_location_id: run.scenario.office_location_id,
  });
  await expect(page.locator('.engineer-row')).toHaveCount(1);
  await expect(page.locator('.engineer-row .engineer-focus')).toContainText('Ирина Петрова');
  await page.locator('.engineer-row .engineer-focus').click();
  await expect(page.locator('.map-route-heading')).toContainText('Ирина Петрова');
  await page.screenshot({ path: info.outputPath('imported-engineers.png'), fullPage: true });
  await page.getByRole('tab', { name: 'Инженеры', exact: true }).click();
  await expect(page.locator('.engineer-cards > li[data-engineer-id="alpha"]')).toContainText(
    'Ирина Петрова',
  );
  await expect(page.getByRole('list', { name: 'Инженеры смены' })).not.toContainText('NaN');

  await page.reload();
  await expect(page.locator('.engineer-row')).toHaveCount(1);
  await expect(page.locator('.team-count')).toContainText('1 из 2');
  await page.locator('.team-settings > summary').click();
  await expect(page.getByLabel('Доступен: Борис Соколов', { exact: true })).not.toBeChecked();
  await page.locator('.engineer-settings > summary').first().click();
  await expect(editor.getByLabel('Конец смены')).toHaveValue('19:00');
  const session = await page.evaluate(() => localStorage.getItem('dispatch-session'));
  await page.getByLabel('Файл инженеров').setInputFiles(teamFile());
  await page.getByRole('button', { name: 'Проверить инженеров' }).click();
  await expect(calculate).toBeEnabled();
  await expect(editor.getByLabel('Конец смены')).toHaveValue('19:00');
  await expect(page.locator('.team-count')).toContainText('1 из 2');
  expect(await page.evaluate(() => localStorage.getItem('dispatch-session'))).toBe(session);
  await expect(page.locator('.visit')).toHaveCount(1);
  expect(writes).toHaveLength(1);

  await page.getByLabel('Файл инженеров').setInputFiles(teamFile('alpha'));
  await page.getByRole('button', { name: 'Проверить инженеров' }).click();
  await expect(
    page.getByRole('region', { name: 'Загрузка инженеров' }).getByRole('alert'),
  ).toContainText('Строка 3, ID');
  await expect(
    page.getByRole('region', { name: 'Загрузка инженеров' }).getByRole('alert'),
  ).toContainText('повторяется');
  await expect(calculate).toBeDisabled();
  await expect(page.locator('.visit')).toHaveCount(1);
  expect(await page.evaluate(() => localStorage.getItem('dispatch-session'))).toBe(session);
  expect(writes).toHaveLength(1);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByLabel('Файл инженеров')).toBeVisible();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
  ).toBeTruthy();
  await page.screenshot({ path: info.outputPath('engineer-errors-mobile.png'), fullPage: true });
});

test('a reusable engineer file can be loaded first and is retained when requests are replaced', async ({
  page,
}) => {
  await page.goto('/');
  await page.getByLabel('Файл инженеров').setInputFiles(teamFile());
  await page.getByRole('button', { name: 'Проверить инженеров' }).click();
  await expect(
    page.getByRole('region', { name: 'Загрузка инженеров' }).getByRole('status'),
  ).toContainText('2 инженера');
  await page.reload();
  for (const name of ['Участок.csv', 'Следующий участок.csv']) {
    await page.getByLabel('Файл заявок').setInputFiles({ ...jobs, name });
    await page.getByRole('button', { name: 'Проверить файл', exact: true }).click();
    await expect(page.locator('.team-count')).toContainText('2 из 2');
    await expect(page.getByRole('button', { name: 'Рассчитать план', exact: true })).toBeEnabled();
    await expect(page.getByRole('region', { name: 'Загрузка инженеров' })).toContainText(
      'Команда.csv',
    );
  }
});

for (const id of ['all', 'unassigned']) {
  test(`engineer ID ${id} does not collide with map views`, async ({ page }) => {
    await page.goto('/');
    await page.getByLabel('Файл заявок').setInputFiles(jobs);
    await page.getByRole('button', { name: 'Проверить файл', exact: true }).click();
    await page.getByLabel('Файл инженеров').setInputFiles({
      name: 'Команда.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from(
        `ID;Навыки;Транспорт;Начало смены;Конец смены\n${id};installation;car;09:00;18:00\n`,
      ),
    });
    await page.getByRole('button', { name: 'Проверить инженеров' }).click();
    await page.getByRole('button', { name: 'Рассчитать план', exact: true }).click();
    await expect(page.locator('.engineer-row')).toHaveCount(1);
    await expect(page.locator('.map-route-heading')).toHaveCount(0);
    await page.locator('.engineer-row .engineer-focus').click();
    await expect(page.locator('.map-route-heading')).toContainText(id);
    await expect(page.locator('.route-overview')).toHaveCount(1);
    await page.getByRole('button', { name: 'Показать маршруты всех инженеров' }).click();
    await expect(page.locator('.map-route-heading')).toHaveCount(0);
    await expect(page.locator('.route-overview')).toHaveCount(1);
    await page.getByRole('tab', { name: 'Заявки', exact: true }).click();
    await page.getByLabel('Фильтр по инженеру').selectOption(id);
    await expect(page.getByLabel('Фильтр по инженеру')).toHaveValue(id);
    await expect(page.locator('.request-cards > li')).toHaveCount(1);
  });
}
