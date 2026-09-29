import { openUnavailability } from './sidebar-fixture';
import { uploadSample, uploadEngineers } from './upload-fixture';
import { expect, test } from '@playwright/test';
import type { Run } from '../src/types';

test('calculation keeps only assigned engineers and replanning preserves that roster', async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/');
  await uploadSample(page);
  await uploadEngineers(page, 30);
  const calculation = page.waitForResponse((r) => r.url().endsWith('/optimize'));
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  const run: Run = await (await calculation).json();
  const roster = run.plan.routes.filter((r) => r.visits.length).map((r) => r.engineer_id);
  expect(run.version).toBe(1);
  expect(run.staffing!.engineer_ids).toEqual(roster);
  expect(roster.length).toBeLessThan(run.engineers.length);
  expect(run.comparison?.scope).toBe('morning');
  const rows = page.locator('.engineer-row');
  const displayedIds = () =>
    rows.evaluateAll((elements) => elements.map((el) => el.getAttribute('data-engineer-id')));
  await expect(rows).toHaveCount(roster.length);
  expect(await displayedIds()).toEqual(roster);
  await expect(page.getByRole('button', { name: /Утвердить/ })).toHaveCount(0);
  await expect(page.locator('.map-roster-total')).toHaveText(`На смене: ${roster.length}`);
  await expect(page.getByRole('button', { name: 'Базовый план', exact: true })).toHaveCount(0);
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Экспорт плана' }).click();
  await page.getByRole('button', { name: 'Полный план · JSON' }).click();
  const stream = await (await download).createReadStream();
  const chunks = [];
  for await (const chunk of stream!) chunks.push(chunk);
  const exported = JSON.parse(Buffer.concat(chunks).toString());
  expect(exported.plan).toEqual(run.plan);
  expect(exported.scenario.roster).toEqual(roster);
  await page.getByRole('tab', { name: 'Инженеры', exact: true }).click();
  await expect(page.locator('.engineer-cards > li')).toHaveCount(roster.length);
  await openUnavailability(page, roster[0]);
  await page.getByLabel('Время события').filter({ visible: true }).fill('08:00:00');
  const previewResponse = page.waitForResponse((r) => r.url().endsWith('/events/preview'));
  await page.getByRole('button', { name: 'Рассчитать вариант' }).click();
  const preview = await (await previewResponse).json();
  expect(preview.result.staffing.engineer_ids).toEqual(roster);
  expect(preview.result.scenario.equipment_issued).toEqual(run.scenario.equipment_issued);
  await expect(page.getByRole('region', { name: 'Вариант для принятия' })).toBeVisible();
  await page.getByRole('button', { name: 'Принять вариант' }).click();
  await expect(page.getByRole('region', { name: 'Вариант для принятия' })).toHaveCount(0);
  expect(await displayedIds()).toEqual(roster);
  await expect(rows.filter({ hasText: 'Недоступен' })).toHaveCount(1);
  await page.screenshot({ path: testInfo.outputPath('automatic-shift.png'), fullPage: true });
  await page.reload();
  await expect(rows).toHaveCount(roster.length);
  expect(await displayedIds()).toEqual(roster);
});
