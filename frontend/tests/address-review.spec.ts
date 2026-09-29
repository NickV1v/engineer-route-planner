import { expect, test } from '@playwright/test';
import { addressDemo, house } from './address-review-fixture';

test('review lists only missing addresses, saves a confirmed house, discards reversibly and resumes', async ({
  page,
}, info) => {
  const demo = await addressDemo(page);
  await page.route('**/api/address-suggestions', (route) =>
    route.fulfill({
      json: {
        items: [
          { ...house, id: 'b'.repeat(32), precision: 'street', point: null, label: 'Только улица' },
          house,
        ],
        live_available: true,
        notice: '',
      },
    }),
  );
  await page.route('**/api/address-suggestions/resolve', (route) => route.fulfill({ json: house }));
  await page.getByRole('tab', { name: 'Заявки', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Проверить адреса', exact: true })).toHaveCount(0);
  await demo.start();
  const dialog = page.getByRole('dialog', { name: 'Уточните адреса' });
  const list = dialog.getByRole('list', { name: 'Адреса для уточнения' });
  await expect(list.getByRole('button')).toHaveCount(2);
  await expect(list).not.toContainText('Известный дом');
  const proceed = dialog.getByRole('button', { name: 'Продолжить расчёт' });
  await expect(proceed).toBeDisabled();
  const input = dialog.getByRole('combobox', { name: 'Правильный адрес' });
  await input.fill('Москва Проверочная 5/1');
  const choices = dialog.getByRole('listbox', { name: 'Подсказки адреса' });
  await expect(choices.getByRole('option')).toHaveCount(1);
  const a = (await input.boundingBox())!,
    b = (await choices.boundingBox())!;
  expect(b.x).toBeCloseTo(a.x, 0);
  expect(b.width).toBeCloseTo(a.width, 0);
  expect(b.y - a.y - a.height).toBeCloseTo(4, 0);
  await input.press('Tab');
  await expect(choices).toBeHidden();
  await input.focus();
  await expect(choices).toBeVisible();
  await input.press('Escape');
  await expect(choices).toBeHidden();
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: 'Найти адрес', exact: true }).click();
  await expect(choices.getByRole('option')).toHaveCount(1);
  await input.press('ArrowDown');
  await input.press('Enter');
  await expect(choices).toBeHidden();
  await expect(dialog.locator('.address-point-pin')).toBeVisible();
  const key = demo.loaded.scenario.requests.find((r) => r.address === demo.first)!.location_id;
  const getGeo = async () =>
    (await page.request.get(`/api/uploads/${demo.loaded.upload_id}/geography`)).json();
  expect((await getGeo())[key].point).toBeNull();
  await dialog.screenshot({ path: info.outputPath('address-review-desktop.png') });
  await dialog.getByRole('button', { name: 'Сохранить адрес и перейти дальше' }).click();
  await expect(dialog.locator('.address-review-current')).toContainText(demo.second);
  expect((await getGeo())[key].point).toEqual(house.point);
  expect((await getGeo())[key].address).toBe(demo.first);
  await expect(list.getByRole('button', { name: /missing-one/ })).toContainText(house.label);
  await expect(list).not.toContainText(demo.first);
  await list.getByRole('button', { name: /missing-one/ }).click();
  await expect(dialog.locator('.address-review-current h3')).toHaveText(house.label);
  await expect(input).toHaveValue(house.label);
  await list.getByRole('button', { name: /missing-two/ }).click();
  await dialog.getByRole('button', { name: 'Исключить заявку #missing-two', exact: true }).click();
  await expect(proceed).toBeEnabled();
  await list.getByRole('button', { name: /missing-two/ }).click();
  await dialog.getByRole('button', { name: 'Вернуть заявку #missing-two' }).click();
  await expect(proceed).toBeDisabled();
  await dialog.getByRole('button', { name: 'Исключить заявку #missing-two', exact: true }).click();
  expect(demo.calls).toHaveLength(1);
  const runResponse = page.waitForResponse((r) => r.url().endsWith('/optimize') && r.ok());
  await proceed.click();
  const run = await (await runResponse).json();
  await expect(dialog).toBeHidden();
  expect(demo.calls).toHaveLength(2);
  expect(demo.calls[1].engineers).toEqual(demo.calls[0].engineers);
  expect(run.scenario.requests).toHaveLength(2);
  expect(run.manifest.excluded_requests[0].id).toContain('missing-two');
  expect(
    run.scenario.requests.find((r: { location_id: string }) => r.location_id === key)
      .display_address,
  ).toBe(house.label);
  await expect(page.locator(`.map-stop[aria-label="${house.label}"]`)).toBeVisible();
  await page.reload();
  await expect(page.getByText('Исключено заявок: 1')).toBeVisible();
  await page.getByRole('tab', { name: 'Заявки', exact: true }).click();
  await page.getByLabel('Поиск заявки', { exact: true }).fill('Проверочная');
  await expect(page.locator('.request-link')).toHaveCount(1);
  await expect(page.locator('.request-address')).toContainText(house.label);
  await page.locator('.request-link').click();
  await expect(page.locator('.request-detail-address')).toHaveText(house.label);
  await page.getByRole('tab', { name: 'План', exact: true }).click();
  await page.getByRole('button', { name: 'Вернуть в расчёт' }).click();
  await demo.start();
  await expect(list.getByRole('button')).toHaveCount(1);
  await page.setViewportSize({ width: 390, height: 844 });
  await dialog.screenshot({ path: info.outputPath('address-review-mobile.png') });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('a manually corrected address is saved with its point and shown before calculation', async ({
  page,
}) => {
  const demo = await addressDemo(page, { officeMissing: true });
  await demo.start();
  const dialog = page.getByRole('dialog', { name: 'Уточните адреса' });
  const input = dialog.getByRole('combobox', { name: 'Правильный адрес' });
  const office = 'Москва, Новый адрес офиса, 10';
  await input.fill(office);
  await dialog.getByLabel('Карта уточнения адреса').click({ position: { x: 160, y: 110 } });
  await dialog.getByRole('button', { name: 'Сохранить адрес и перейти дальше' }).click();
  await expect(dialog.locator('.address-review-current')).toContainText(demo.first);
  const corrected = 'Москва, Уточнённая улица, 11';
  await input.fill(corrected);
  await dialog.getByText('Ввести координаты', { exact: true }).click();
  await dialog.getByLabel('Широта', { exact: true }).fill('55.799');
  await dialog.getByLabel('Долгота', { exact: true }).fill('37.699');
  await dialog.getByRole('button', { name: 'Сохранить адрес и перейти дальше' }).click();
  await expect(dialog.locator('.address-review-current')).toContainText(demo.second);
  await dialog.getByRole('button', { name: 'Вернуться без расчёта' }).click();
  await expect(page.locator('.map-office')).toHaveAttribute('aria-label', `Офис · ${office}`);
  await expect(page.locator(`.map-stop[aria-label="${corrected}"]`)).toBeVisible();
  await page.getByRole('tab', { name: 'Заявки', exact: true }).click();
  await page.getByLabel('Поиск заявки', { exact: true }).fill('Уточнённая');
  await expect(page.locator('.request-link')).toHaveCount(1);
  await page.locator('.request-link').click();
  await expect(page.locator('.request-detail-address')).toHaveText(corrected);
  await page.reload();
  await page.getByRole('tab', { name: 'Заявки', exact: true }).click();
  await page.getByLabel('Поиск заявки', { exact: true }).fill('Уточнённая');
  await expect(page.locator('.request-address')).toContainText(corrected);
  expect(demo.calls).toHaveLength(1);
});

test('office cannot be discarded and shared addresses retain requests until each is excluded', async ({
  page,
}) => {
  const demo = await addressDemo(page, { officeMissing: true, shared: true });
  await demo.start();
  const dialog = page.getByRole('dialog', { name: 'Уточните адреса' });
  await expect(dialog.locator('.address-review-current')).toContainText('Адрес офиса');
  await expect(dialog.getByRole('button', { name: /Исключить заявку/ })).toHaveCount(0);
  const list = dialog.getByRole('list', { name: 'Адреса для уточнения' });
  await list.getByRole('button', { name: /missing-one/ }).click();
  await dialog.getByRole('button', { name: 'Исключить заявку #missing-one', exact: true }).click();
  await expect(dialog.locator('.address-review-current')).toContainText(demo.first);
  await dialog.getByRole('button', { name: 'Исключить заявку #shared', exact: true }).click();
  await list.getByRole('button', { name: /missing-two/ }).click();
  await dialog.getByRole('button', { name: 'Исключить заявку #missing-two', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Продолжить расчёт' })).toBeDisabled();
  await expect(dialog.locator('.address-review-current')).toContainText('Адрес офиса');
  await dialog.getByLabel('Карта уточнения адреса').click({ position: { x: 160, y: 110 } });
  await dialog.getByRole('button', { name: 'Сохранить адрес и перейти дальше' }).click();
  await expect(dialog.getByRole('button', { name: 'Продолжить расчёт' })).toBeEnabled();
});

test('closing review preserves the accepted plan and restores keyboard focus', async ({ page }) => {
  const demo = await addressDemo(page);
  // A historical synthetic plan can contain unlocated jobs; review must not overwrite it.
  const old = await (
    await page.request.post(`/api/uploads/${demo.loaded.upload_id}/baseline`, {
      data: { engineers: demo.loaded.engineers.slice(0, 1) },
    })
  ).json();
  await page.evaluate((id) => localStorage.setItem('dispatch-session', id), old.session_id);
  await page.reload();
  await expect(page.locator('.visit').first()).toBeVisible();
  await demo.start();
  await page.getByRole('button', { name: 'Закрыть проверку адресов' }).press('Escape');
  await expect(page.getByRole('dialog')).toBeHidden();
  await expect(page.getByRole('button', { name: 'Рассчитать план', exact: true })).toBeFocused();
  expect(await (await page.request.get(`/api/sessions/${old.session_id}`)).json()).toEqual(old);
  expect(await page.evaluate(() => localStorage.getItem('dispatch-session'))).toBe(old.session_id);
});

test('late automatic and old query responses cannot overwrite manually entered coordinates', async ({
  page,
}) => {
  const demo = await addressDemo(page);
  let release = () => {};
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route('**/api/address-suggestions', async (route) => {
    const body = route.request().postDataJSON();
    if (body.auto_resolve) await pending;
    await route
      .fulfill({
        json: {
          items: [house],
          automatic: body.auto_resolve ? house : null,
          live_available: true,
          notice: '',
        },
      })
      .catch(() => {});
  });
  const started = page.waitForRequest(
    (r) => r.url().endsWith('/api/address-suggestions') && r.postDataJSON().auto_resolve,
  );
  await demo.start();
  await started;
  const dialog = page.getByRole('dialog');
  await dialog.getByText('Ввести координаты', { exact: true }).click();
  await dialog.getByLabel('Широта', { exact: true }).fill('55.799');
  const response = page.waitForResponse(
    (r) => r.url().endsWith('/api/address-suggestions') && !r.request().postDataJSON().auto_resolve,
  );
  await dialog.getByLabel('Долгота', { exact: true }).fill('37.699');
  release();
  await response;
  await expect(dialog.getByRole('listbox')).toBeHidden();
  await expect(dialog.getByLabel('Широта', { exact: true })).toHaveValue('55.799');
  await expect(dialog.getByLabel('Долгота', { exact: true })).toHaveValue('37.699');
  await dialog.getByRole('button', { name: 'Сохранить адрес и перейти дальше' }).click();
  await expect(dialog.locator('.address-review-current')).toContainText(demo.second);
  const key = demo.loaded.scenario.requests.find((r) => r.address === demo.first)!.location_id;
  const geo = await (
    await page.request.get(`/api/uploads/${demo.loaded.upload_id}/geography`)
  ).json();
  expect(geo[key].point.lat).toBe(55.799);
});

test('an automatic exact match remains a draft until the dispatcher saves it', async ({ page }) => {
  const demo = await addressDemo(page);
  await page.route('**/api/address-suggestions', (route) =>
    route.fulfill({ json: { items: [house], automatic: house, live_available: true, notice: '' } }),
  );
  await demo.start();
  const dialog = page.getByRole('dialog');
  await expect(dialog.locator('.address-point-pin')).toBeVisible();
  await expect(dialog.getByRole('listbox')).toBeHidden();
  await expect(dialog.getByRole('button', { name: 'Продолжить расчёт' })).toBeDisabled();
  const geo = await (
    await page.request.get(`/api/uploads/${demo.loaded.upload_id}/geography`)
  ).json();
  const key = demo.loaded.scenario.requests.find((r) => r.address === demo.first)!.location_id;
  expect(geo[key].point).toBeNull();
});

test('outdated search responses do not replace the edited address in review', async ({ page }) => {
  const demo = await addressDemo(page);
  let release = () => {};
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route('**/api/address-suggestions', async (route) => {
    const old = route.request().postDataJSON().query === 'Старый адрес';
    if (old) await pending;
    await route
      .fulfill({
        json: {
          items: [{ ...house, label: old ? 'Старый результат' : 'Новый результат' }],
          live_available: true,
          notice: '',
        },
      })
      .catch(() => {});
  });
  await demo.start();
  const input = page.getByRole('combobox', { name: 'Правильный адрес' });
  const old = page.waitForRequest(
    (r) =>
      r.url().endsWith('/api/address-suggestions') && r.postDataJSON().query === 'Старый адрес',
  );
  await input.fill('Старый адрес');
  await old;
  await input.fill('Новый адрес');
  await expect(page.getByRole('listbox')).toContainText('Новый результат');
  release();
  await expect(page.getByRole('listbox')).not.toContainText('Старый результат');
});
