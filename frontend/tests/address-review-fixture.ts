import { expect, type Page } from '@playwright/test';
import type { UploadedData, AddressReview, EngineerInput } from '../src/types';
import { uploadEngineers } from './upload-fixture';

export const house = {
  id: 'a'.repeat(32),
  label: 'Москва, улица Проверочная, 5 корпус 1',
  provider: 'dadata',
  precision: 'house',
  house: '5 корпус 1',
  notice: '',
  point: {
    lat: 55.712345,
    lon: 37.612345,
    label: 'Москва, улица Проверочная, 5 корпус 1',
    precision: 'house',
    source: 'DaData:fixture; OpenStreetMap',
  },
};

export async function addressDemo(
  page: Page,
  options: { officeMissing?: boolean; shared?: boolean } = {},
) {
  const suffix = crypto.randomUUID().slice(0, 8);
  const first = `Москва, Тестовая улица ${suffix}, д 999`;
  const second = `Москва, Тестовая улица ${suffix}, д 998`;
  const rows = [
    'ID;Тип;Адрес;Начало;Окончание;Длительность, мин;Широта;Долгота',
    'known;Дозаказ;Известный дом;29.09.2026 09:00;29.09.2026 17:00;30;55.71;37.61',
    `missing-one;Дозаказ;${first};29.09.2026 09:00;29.09.2026 17:00;30;;`,
    `missing-two;Дозаказ;${second};29.09.2026 09:00;29.09.2026 17:00;30;;`,
    ...(options.shared ? [`shared;Дозаказ;${first};29.09.2026 09:00;29.09.2026 17:00;30;;`] : []),
    `Адрес офиса;Офис ${suffix}${options.officeMissing ? '' : ';55.7;37.6'}`,
  ];
  await page.route('**/api/address-suggestions', (route) =>
    route.fulfill({ json: { items: [], live_available: false, notice: '' } }),
  );
  await page.goto('/');
  await page.getByLabel('Файл заявок').setInputFiles({
    name: 'Проверка адресов.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from(rows.join('\n')),
  });
  const response = page.waitForResponse((r) => r.url().includes('/api/uploads/validate'));
  await page.getByRole('button', { name: 'Проверить файл', exact: true }).click();
  const loaded: UploadedData = await (await response).json();
  await uploadEngineers(page, 1);
  const calls: { engineers: EngineerInput[]; excluded_request_ids: string[] }[] = [];
  // Model the real preflight response without loading live graphs or geocoders.
  // Backend tests independently enforce this gate before both routing and solving.
  await page.route('**/api/uploads/*/optimize', async (route) => {
    const body = route.request().postDataJSON();
    calls.push(body);
    const data: UploadedData = await (
      await page.request.get(`/api/uploads/${loaded.upload_id}`)
    ).json();
    const review: AddressReview = {
      upload_id: loaded.upload_id,
      requests: data.scenario.requests,
      office_location_id: data.scenario.office_location_id!,
      geography: data.scenario.geography!,
      excluded_request_ids: body.excluded_request_ids ?? [],
    };
    const needed = new Set([
      review.office_location_id,
      ...review.requests
        .filter((r) => !review.excluded_request_ids.includes(r.id))
        .map((r) => r.location_id),
    ]);
    if ([...needed].some((key) => !review.geography[key].point)) {
      await route.fulfill({
        status: 409,
        json: { code: 'address_review_required', detail: 'Уточните адреса', review },
      });
    } else await route.fallback();
  });
  const start = async () => {
    await page.getByRole('tab', { name: 'План', exact: true }).click();
    await page.getByRole('button', { name: 'Рассчитать план', exact: true }).click();
    await expect(page.getByRole('dialog', { name: 'Уточните адреса' })).toBeVisible();
  };
  return { loaded, first, second, calls, start };
}
