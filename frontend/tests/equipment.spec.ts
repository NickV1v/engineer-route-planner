import { openCancellation } from './sidebar-fixture';
import { uploadSample } from './upload-fixture';
import { expect, test } from '@playwright/test';
import type { Run } from '../src/types';

test('client devices are issued once and remain visible after cancellation and reload', async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await uploadSample(page);
  const calculation = page.waitForResponse((r) => r.url().endsWith('/optimize'));
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  const initial: Run = await (await calculation).json();
  expect(initial.scenario.equipment_policy).toBe('client_devices_v1');
  expect(initial.staffing?.status).toBe('active');
  expect(initial.version).toBe(1);
  await page.getByRole('tab', { name: 'Метрики', exact: true }).click();
  const metrics = page.getByRole('table', { name: 'Метрики инженеров' });
  await expect(metrics.getByRole('columnheader', { name: 'Выдано роутеров' })).toBeVisible();
  const expected = Object.fromEntries(
    initial.plan.routes.map((route) => {
      const kinds = route.visits.map(
        (v) => initial.scenario.requests.find((r) => r.id === v.request_id)!.kind,
      );
      return [
        route.engineer_id,
        {
          routers: kinds.filter((kind) => kind === 'Подключение').length,
          set_top_boxes: kinds.filter((kind) => kind === 'Дозаказ').length,
        },
      ];
    }),
  );
  const checkCounts = async () => {
    for (const [id, stock] of Object.entries(expected)) {
      const row = metrics.locator(`tbody tr[data-engineer-id="${id}"]`);
      if (!initial.staffing!.engineer_ids.includes(id)) {
        await expect(row).toHaveCount(0);
        continue;
      }
      await expect(row.locator('[data-equipment="routers"]')).toHaveText(String(stock.routers));
      await expect(row.locator('[data-equipment="set_top_boxes"]')).toHaveText(
        String(stock.set_top_boxes),
      );
    }
  };
  await checkCounts();
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  await expect(page.getByRole('button', { name: /Утвердить/ })).toHaveCount(0);
  expect(initial.scenario.equipment_issued).toEqual(expected);
  await expect(metrics.getByRole('columnheader', { name: 'Выдано роутеров' })).toBeVisible();
  await checkCounts();
  const route = initial.plan.routes.find((r) => expected[r.engineer_id].routers > 0)!;
  const cancelled = route.visits.find(
    (v) => initial.scenario.requests.find((r) => r.id === v.request_id)?.kind === 'Подключение',
  )!;
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  await openCancellation(page, cancelled.request_id);
  await page
    .getByLabel('Режим перепланирования')
    .filter({ visible: true })
    .selectOption('preserve');
  await page.getByLabel('Время события').filter({ visible: true }).fill('08:00:00');
  const previewResponse = page.waitForResponse((r) => r.url().endsWith('/events/preview'));
  await page.getByRole('button', { name: 'Рассчитать вариант' }).click();
  const preview = await (await previewResponse).json();
  expect(preview.result.scenario.equipment_issued).toEqual(expected);
  await expect(page.getByRole('region', { name: 'Вариант для принятия' })).toBeVisible();
  await checkCounts();
  const committedResponse = page.waitForResponse((r) => r.url().endsWith('/events/commit'));
  await page.getByRole('button', { name: 'Принять вариант' }).click();
  const committed: Run = await (await committedResponse).json();
  expect(committed.scenario.equipment_issued).toEqual(expected);
  expect(
    committed.plan.routes.find((r) => r.engineer_id === route.engineer_id)!.visits,
  ).toHaveLength(route.visits.length - 1);
  await checkCounts();
  await page.screenshot({ path: testInfo.outputPath('equipment-issued.png'), fullPage: true });
  const lastCell = await metrics.locator('tbody tr').first().locator('td').last().boundingBox();
  expect(lastCell!.x + lastCell!.width).toBeLessThanOrEqual(1440);
  await page.reload();
  await page.getByRole('tab', { name: 'Метрики', exact: true }).click();
  await expect(metrics.getByRole('columnheader', { name: 'Выдано ТВ-приставок' })).toBeVisible();
  await checkCounts();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});
