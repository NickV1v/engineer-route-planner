import type { Page } from '@playwright/test';

export async function openCancellation(page: Page, id: string) {
  await page.getByRole('tab', { name: 'Заявки', exact: true }).click();
  const row = page.locator(`.request-cards > li[data-request-id=${JSON.stringify(id)}]`);
  await row.getByRole('button', { name: 'Отменить', exact: true }).click();
}

export async function openUnavailability(page: Page, id: string) {
  await page.getByRole('tab', { name: 'Инженеры', exact: true }).click();
  await page
    .locator(`.engineer-cards > li[data-engineer-id=${JSON.stringify(id)}]`)
    .getByRole('button', { name: 'Сделать недоступным', exact: true })
    .click();
}
