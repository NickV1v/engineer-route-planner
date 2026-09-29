import { expect, test } from '@playwright/test';
import type { Run } from '../src/types';
import { uploadSample } from './upload-fixture';
import { openCancellation, openUnavailability } from './sidebar-fixture';

test('list actions preview, reject and commit without losing the accepted day', async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await expect(
    page.getByRole('tablist', { name: 'Разделы диспетчера' }).getByRole('tab'),
  ).toHaveText(['План', 'Заявка', 'Заявки', 'Инженеры']);
  await uploadSample(page);
  await page.getByRole('tab', { name: 'Заявки', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Отменить', exact: true }).first()).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Проверить адреса', exact: true })).toHaveCount(0);
  await expect(page.getByLabel('Поиск заявки')).toBeVisible();
  await page.getByRole('tab', { name: 'План', exact: true }).click();
  const calculated = page.waitForResponse((r) => r.url().endsWith('/optimize'));
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  const initial: Run = await (await calculated).json();
  const visit = initial.plan.routes.flatMap((r) => r.visits).find((v) => v.start_s < 23 * 3600)!;
  await openCancellation(page, visit.request_id);
  await expect(page.getByLabel('Режим перепланирования').filter({ visible: true })).toHaveValue(
    'flexible',
  );
  const at = page.getByLabel('Время события').filter({ visible: true });
  await at.fill('23:00:00');
  await expect(page.getByText('К этому времени выезд уже начат', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Рассчитать вариант' })).toBeDisabled();
  await at.fill('08:00:00');
  const previewResponse = page.waitForResponse((r) => r.url().endsWith('/events/preview'));
  await page.getByRole('button', { name: 'Рассчитать вариант' }).click();
  const proposal = await (await previewResponse).json();
  expect(proposal.result.event.request_id).toBe(visit.request_id);
  const stored = async () => (await page.request.get(`/api/sessions/${initial.session_id}`)).json();
  expect((await stored()).version).toBe(1);
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Рассчитать вариант' })).toBeDisabled();
  await expect(page.locator('#tab-requests .pending-proposal')).toBeVisible();
  await page.getByRole('tab', { name: 'Инженеры', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Сделать недоступным' }).first()).toBeDisabled();
  await page.getByRole('tab', { name: 'Заявки', exact: true }).click();
  await page.getByRole('button', { name: 'Отклонить вариант' }).click();
  expect((await stored()).version).toBe(1);
  await page.getByRole('button', { name: 'К заявкам', exact: true }).click();
  await page.screenshot({ path: info.outputPath('requests-desktop.png') });
  await openCancellation(page, visit.request_id);
  await at.fill('08:00:00');
  await page.getByRole('button', { name: 'Рассчитать вариант' }).click();
  await expect(page.getByRole('region', { name: 'Вариант для принятия' })).toBeVisible();
  await page.getByRole('button', { name: 'Принять вариант' }).click();
  await expect(page.getByLabel('Поиск заявки')).toBeVisible();
  await expect(
    page.locator(`.request-cards > li[data-request-id=${JSON.stringify(visit.request_id)}]`),
  ).toHaveCount(0);
  expect((await stored()).version).toBe(2);
  const id = initial.staffing!.engineer_ids[0];
  await openUnavailability(page, id);
  await at.fill('08:00:00');
  await page.getByRole('button', { name: 'Рассчитать вариант' }).click();
  await expect(page.getByRole('region', { name: 'Вариант для принятия' })).toBeVisible();
  expect((await stored()).unavailable_engineers ?? []).not.toContain(id);
  await page.getByRole('button', { name: 'Принять вариант' }).click();
  const engineers = page.getByRole('list', { name: 'Инженеры смены' });
  await expect(engineers).toBeVisible();
  const row = page.locator(`.engineer-cards > li[data-engineer-id=${JSON.stringify(id)}]`);
  await expect(row).toContainText('Недоступен');
  await expect(row.getByRole('button', { name: 'Уже недоступен' })).toBeDisabled();
  await page.screenshot({ path: info.outputPath('engineers-desktop.png') });
  await page.reload();
  await page.getByRole('tab', { name: 'Инженеры', exact: true }).click();
  await expect(row).toContainText('Недоступен');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: info.outputPath('engineers-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});
