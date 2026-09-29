import { expect, test } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { uploadSample } from './upload-fixture';
import type { Run } from '../src/types';

test('Excel and printable sheets export visible routes from the accepted version', async ({
  page,
}, info) => {
  await page.goto('/');
  const trigger = page.getByRole('button', { name: 'Экспорт плана', exact: true });
  await expect(trigger).toBeDisabled();
  await uploadSample(page);
  const calculated = page.waitForResponse((r) => r.url().endsWith('/optimize') && r.ok());
  await page.getByRole('button', { name: 'Рассчитать план', exact: true }).click();
  const run: Run = await (await calculated).json();
  const active = run.plan.routes.filter((r) => r.visits.length);
  expect(active.length).toBeGreaterThan(1);
  await trigger.click();
  const menu = page.getByRole('region', { name: 'Форматы экспорта' });
  await expect(menu).toBeVisible();
  await page.screenshot({ path: info.outputPath('export-desktop.png') });
  await trigger.press('Escape');
  await expect(menu).toBeHidden();
  await expect(trigger).toBeFocused();
  const hidden = active[0].engineer_id;
  await page.locator(`.engineer-row[data-engineer-id="${hidden}"] .eye-button`).click();
  await trigger.click();
  await page.getByLabel('Только видимые маршруты').check();
  const response = page.waitForResponse((r) => r.url().includes('/export?'));
  const downloading = page.waitForEvent('download');
  await page.getByRole('button', { name: 'План в Excel' }).click();
  const result = await response;
  const url = new URL(result.url());
  expect(url.searchParams.get('version')).toBe(String(run.version));
  expect(url.searchParams.get('scope')).toBe('visible');
  expect(url.searchParams.getAll('engineer_id')).toEqual(
    active.filter((r) => r.engineer_id !== hidden).map((r) => r.engineer_id),
  );
  const download = await downloading;
  expect(download.suggestedFilename()).toMatch(/\.xlsx$/);
  await download.saveAs(info.outputPath('selected-plan.xlsx'));
  expect((await readFile(info.outputPath('selected-plan.xlsx'))).subarray(0, 2).toString()).toBe(
    'PK',
  );
  await expect(menu).toBeHidden();
  // In detail mode only the focused engineer is visible on the map.
  await expect(trigger).toBeFocused();
  await page
    .locator(`.engineer-row[data-engineer-id="${active[1].engineer_id}"] .engineer-focus`)
    .click();
  await trigger.click();
  const sheets = page.waitForResponse((r) => r.url().includes('format=waybills'));
  const waybills = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Маршрутные листы' }).click();
  expect(new URL((await sheets).url()).searchParams.getAll('engineer_id')).toEqual([
    active[1].engineer_id,
  ]);
  await (await waybills).saveAs(info.outputPath('engineer-route.xlsx'));
  expect(await (await page.request.get(`/api/sessions/${run.session_id}`)).json()).toEqual(run);
  await page.setViewportSize({ width: 390, height: 844 });
  await trigger.click();
  await expect(menu).toBeVisible();
  const box = (await menu.boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(390);
  await page.screenshot({ path: info.outputPath('export-mobile.png') });
});

test('empty selection and export failures preserve the plan and allow retry', async ({ page }) => {
  await page.goto('/');
  await uploadSample(page);
  await page.getByRole('button', { name: 'Рассчитать план', exact: true }).click();
  await expect(page.locator('.visit').first()).toBeVisible();
  const before = await page.evaluate(() => localStorage.getItem('dispatch-session'));
  await page.getByRole('button', { name: 'Скрыть все', exact: true }).click();
  const trigger = page.getByRole('button', { name: 'Экспорт плана', exact: true });
  await trigger.click();
  await page.getByLabel('Только видимые маршруты').check();
  await expect(page.getByRole('button', { name: 'План в Excel' })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Маршрутные листы' })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Полный план · JSON' })).toBeEnabled();
  await page.getByLabel('Только видимые маршруты').uncheck();
  await page.route('**/export?*', (route) =>
    route.fulfill({ status: 503, json: { detail: 'Не удалось сформировать файл' } }),
  );
  await page.getByRole('button', { name: 'План в Excel' }).click();
  await expect(page.getByRole('alert')).toContainText('Не удалось сформировать файл');
  expect(await page.evaluate(() => localStorage.getItem('dispatch-session'))).toBe(before);
  await page.unroute('**/export?*');
  const downloading = page.waitForEvent('download');
  await page.getByRole('button', { name: 'План в Excel' }).click();
  expect((await downloading).suggestedFilename()).toMatch(/\.xlsx$/);
  await expect(page.getByRole('alert')).toHaveCount(0);
  await expect(page.locator('.visit').first()).toBeVisible();
});
