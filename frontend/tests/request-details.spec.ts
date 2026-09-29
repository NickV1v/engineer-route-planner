import { expect, test } from '@playwright/test';
import type { Run } from '../src/types';
import { withMapCoordinates } from './map-fixture';
import { uploadSample } from './upload-fixture';

test('request details stay in the sidebar, keep the camera and cancel the selected job', async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  let run: Run;
  await page.route('**/optimize', async (route) => {
    const response = await route.fetch();
    run = withMapCoordinates(await response.json());
    await route.fulfill({ response, json: run });
  });
  await page.route(/\/api\/sessions\/[^/]+\/geography\?version=\d+$/, (route) =>
    route.fulfill({ json: run.scenario.geography }),
  );
  await page.goto('/');
  await uploadSample(page);
  await page.getByRole('button', { name: 'Рассчитать план', exact: true }).click();
  await expect(page.locator('.visit').first()).toBeVisible();
  const jobId = run!.plan.routes.flatMap((r) => r.visits)[0].request_id;
  const office = page.locator('.map-office');
  const camera = () => office.getAttribute('style');
  const initial = await camera();
  await page.getByRole('tab', { name: 'Заявки', exact: true }).click();
  await page.getByLabel('Поиск заявки', { exact: true }).fill(jobId);
  await page.locator('.request-link').click();
  const details = page.getByRole('region', { name: 'Детали заявки' });
  await expect(
    page
      .getByRole('tabpanel', { name: 'Заявки', exact: true })
      .getByRole('region', { name: 'Детали заявки' }),
  ).toBeVisible();
  await expect(page.locator('.drawer-backdrop, [aria-modal="true"]')).toHaveCount(0);
  expect(await camera()).toBe(initial);
  await page.getByRole('button', { name: 'Показать на карте', exact: true }).click();
  await expect.poll(camera).not.toBe(initial);
  await expect(details).toBeVisible();
  const located = await camera();
  await page.locator('.visit').nth(1).click();
  await expect(details).toBeVisible();
  expect(await camera()).toBe(located);
  await page.getByRole('button', { name: 'К списку заявок' }).click();
  await expect(page.getByLabel('Поиск заявки', { exact: true })).toHaveValue(jobId);
  await page.getByRole('button', { name: 'Показать все точки на карте' }).click();
  const overview = await camera();
  await page.locator('.map-stop').first().click();
  await expect(details).toBeVisible();
  expect(await camera()).toBe(overview);
  // Other controls remain usable while details are open.
  await page.locator('.leaflet-control-zoom-out').click();
  await expect(details).toBeVisible();
  await page.getByRole('button', { name: 'Свернуть левую панель' }).click();
  await expect(details).toBeHidden();
  await page.locator('.visit').first().click();
  await expect(details).toBeVisible();
  await expect(page.getByRole('tab', { name: 'Заявки', exact: true })).toHaveAttribute(
    'aria-selected',
    'true',
  );
  await page.screenshot({ path: info.outputPath('request-left-desktop.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: info.outputPath('request-left-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await details.getByRole('button', { name: 'Отменить заявку', exact: true }).click();
  await page.getByLabel('Время события').filter({ visible: true }).fill('08:00:00');
  const previewResponse = page.waitForResponse((r) => r.url().endsWith('/events/preview'));
  await page.getByRole('button', { name: 'Рассчитать вариант' }).click();
  const proposal = await (await previewResponse).json();
  expect(proposal.result.event.request_id).toBe(jobId);
  // Inspecting another job must not lose or replace the pending cancellation.
  await page.locator('.visit').nth(1).click();
  await expect(
    details.getByRole('button', { name: 'Отменить заявку', exact: true }),
  ).toBeDisabled();
  await page.getByRole('button', { name: 'К варианту изменений' }).click();
  await expect(page.getByRole('region', { name: 'Вариант для принятия' })).toBeVisible();
  const committed = page.waitForResponse((r) => r.url().endsWith('/events/commit'));
  await page.getByRole('button', { name: 'Принять вариант' }).click();
  const result: Run = await (await committed).json();
  expect(result.scenario.requests.some((r) => r.id === jobId)).toBe(false);
  await expect(page.getByLabel('Поиск заявки', { exact: true })).toBeVisible();
  await expect(details).toHaveCount(0);
  expect(errors).toEqual([]);
});
