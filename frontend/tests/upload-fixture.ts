import { expect, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';

const configured = new WeakSet<Page>();

// Keep legacy schedule/address fixtures stable using the original synthetic
// addresses. Upload tests with coordinates=true exercise the actual mixed CSV.
export async function uploadSample(page: Page, name = 'Восток', coordinates = false) {
  if (!configured.has(page)) {
    configured.add(page);
    // Browser scenarios exercise interactions, not the production search budget.
    // Keep their solver input deterministic; full-budget quality has separate checks.
    await page.route('**/api/**', async (route) => {
      const request = route.request();
      if (request.method() === 'POST' && /\/(optimize|events\/preview)$/.test(request.url())) {
        const data = request.postDataJSON();
        await route.fallback({
          postData: JSON.stringify({
            ...data,
            search: { rounds: 16, max_evaluations: 400000, time_limit_ms: 0, ...data.search },
          }),
        });
      } else await route.fallback();
    });
  }
  let text = await readFile(new URL(`../../data/samples/${name}.csv`, import.meta.url), 'utf8');
  if (!coordinates) {
    const originals = JSON.parse(
      await readFile(
        new URL('../../tests/fixtures/sample_coordinate_addresses.json', import.meta.url),
        'utf8',
      ),
    );
    text = text
      .split('\n')
      .map((line, i) => {
        if (!i || !line || line.startsWith('Адрес офиса;')) return line;
        const cells = line.split(';');
        cells[6] ||= originals[name][cells[0]];
        cells[cells.length - 2] = cells[cells.length - 1] = '';
        return cells.join(';');
      })
      .join('\n');
  }
  await page.getByRole('tab', { name: 'План', exact: true }).click();
  await page
    .getByLabel('Файл заявок')
    .setInputFiles({ name: `${name}.csv`, mimeType: 'text/csv', buffer: Buffer.from(text) });
  await page.getByRole('button', { name: 'Проверить файл', exact: true }).click();
  await expect(page.getByText(/Файл проверен · \d+ заяв(?:ка|ки|ок)/)).toBeVisible();
  await uploadEngineers(page);
}

export async function uploadEngineers(page: Page, count = 12) {
  const sample = (
    await readFile(new URL('../../data/samples/engineers/Инженеры.csv', import.meta.url), 'utf8')
  )
    .replace(/^\uFEFF/, '')
    .trim()
    .split(/\r?\n/);
  const rows = Array.from({ length: count }, (_, i) => {
    const cells = sample[1 + (i % 12)].split(';');
    cells[0] = `engineer-${String(i + 1).padStart(2, '0')}`;
    cells[1] = `Инженер ${i + 1}`;
    return cells.join(';');
  });
  await page.getByLabel('Файл инженеров').setInputFiles({
    name: 'Инженеры.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from([sample[0], ...rows, ''].join('\n')),
  });
  await page.getByRole('button', { name: 'Проверить инженеров', exact: true }).click();
  await expect(
    page.getByRole('region', { name: 'Загрузка инженеров' }).locator('.file-valid'),
  ).toContainText(`Файл проверен · ${count} `);
}

export async function selectEngineers(page: Page, count: number) {
  const settings = page.locator('.team-settings');
  if (!(await settings.evaluate((el) => (el as HTMLDetailsElement).open)))
    await settings.locator(':scope > summary').click();
  const choices = page.locator('.engineer-available');
  for (let i = 0; i < (await choices.count()); i++) await choices.nth(i).setChecked(i < count);
  await expect(page.locator('.team-count')).toContainText(`${count} из`);
}
