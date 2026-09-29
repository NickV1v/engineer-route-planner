import { uploadSample } from './upload-fixture';
import { expect, test } from '@playwright/test';
import { journeyStyle } from '../src/journeyStyle';
import { withMapCoordinates } from './map-fixture';
import type { JourneyLeg, Run } from '../src/types';

test('rail identifiers distinguish metro, MCC and diameters without confusing bus numbers', () => {
  const color = (mode: JourneyLeg['mode'], line: string | null) =>
    journeyStyle({ mode, line, geometry_quality: 'network' }).color;
  for (const label of ['Метро 8А', 'Метро 8A', '  метро 8а ', '8А']) {
    expect(color('metro', label)).toBe('#FFCD1C');
  }
  expect(color('metro', 'Метро 1')).toBe('#E42313');
  expect(color('metro', 'Метро 11')).toBe('#78C7C9');
  expect(color('metro', 'Метро 16')).toBe('#007763');
  expect(color('metro', 'Метро 17')).toBe('#474A51');
  expect(color('train', 'МЦК/МЦД 14')).toBe('#E42313');
  expect(color('train', 'МЦК/МЦД D1')).toBe('#ED9F2D');
  expect(color('train', 'МЦД-2')).toBe('#DF477C');
  expect(color('train', 'МЦК/МЦД D3')).toBe('#E15D29');
  expect(color('train', 'МЦК/МЦД D4А')).toBe('#3FB485');
  expect(color('bus', 'Автобус 8')).toBe('#2FA34F');
  for (const label of [null, 'Метро 81', 'Метро неизвестно', 'МЦК/МЦД D10']) {
    expect(color('metro', label)).toBe('#748194');
  }
});

test('detailed transport colors preserve engineer pins, overview colors and hover reset', async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const modes: [JourneyLeg['mode'], string | null, string][] = [
    ['walk', null, '#64B5F6'],
    ['car', null, '#2563C9'],
    ['bicycle', null, '#16A6B6'],
    ['bus', 'Автобус 5', '#2FA34F'],
    ['metro', 'Метро 1', '#E42313'],
    ['metro', 'Метро 5', '#A35539'],
    ['metro', 'Метро 8А', '#FFCD1C'],
    ['metro', 'Метро 11', '#78C7C9'],
    ['train', 'МЦК/МЦД D2', '#DF477C'],
  ];
  let shown: Run;
  await page.route('**/optimize', async (route) => {
    const response = await route.fetch();
    const run = withMapCoordinates(await response.json());
    run.manifest.transport = 'routing_static_v1';
    run.journeys = { plan: {} };
    const engineerRoute = run.plan.routes.find((route) => route.visits.length)!;
    run.journeys.plan[engineerRoute.engineer_id] = engineerRoute.visits.map((visit, index) => ({
      request_id: visit.request_id,
      from_id: 'origin',
      to_id: 'destination',
      status: 'ok',
      duration_s: visit.travel_s,
      distance_m: visit.distance_m,
      walking_s: 60,
      boarding_wait_s: 0,
      // Deterministic geometry exercises all map styles; no live routing APIs.
      legs: modes.map(([mode, line], i) => ({
        mode,
        line,
        from_id: `${i}`,
        to_id: `${i + 1}`,
        from_label: `Остановка ${i + 1}`,
        to_label: `Остановка ${i + 2}`,
        duration_s: 60,
        distance_m: 300,
        geometry: [
          { lat: 55.7 + index * 0.004, lon: 37.6 + i * 0.006 },
          { lat: 55.7 + index * 0.004, lon: 37.6 + (i + 1) * 0.006 },
        ],
        quality: 'estimated',
        geometry_quality: 'network',
      })),
    }));
    shown = run;
    await route.fulfill({ response, json: run });
  });
  await page.goto('/');
  await uploadSample(page);
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  await expect(page.locator('.route-overview').first()).toBeVisible();
  const route = shown!.plan.routes.find((route) => route.visits.length)!;
  const index = shown!.engineers.findIndex((engineer) => engineer.id === route.engineer_id);
  const engineerColor = await page
    .locator(`.route-overview.engineer-route-${index}`)
    .first()
    .getAttribute('stroke');
  const writes: string[] = [];
  page.on('request', (request) => {
    if (request.method() !== 'GET') writes.push(request.url());
  });
  await page.getByRole('button', { name: new RegExp(`^Инженер ${index + 1} `) }).click();
  const requestId = route.visits[0].request_id;
  const paths = page.locator(`.journey-path[data-request-id="${requestId}"]`);
  await expect(paths).toHaveCount(modes.length);
  for (const [i, [mode, , color]] of modes.entries()) {
    await expect(paths.nth(i)).toHaveAttribute('stroke', color);
    if (mode === 'walk') await expect(paths.nth(i)).toHaveAttribute('stroke-dasharray', '4 5');
    else await expect(paths.nth(i)).not.toHaveAttribute('stroke-dasharray');
  }
  for (const pin of await page.locator('.location-pin').all()) {
    expect(
      await pin.evaluate((element) =>
        (element as HTMLElement).style.getPropertyValue('--pin-color'),
      ),
    ).toBe(engineerColor);
  }
  await page.locator('.journey-details summary').first().click();
  const details = page.locator('.journey-details').first();
  for (const [i, [, , color]] of modes.entries()) {
    const rgb = color
      .slice(1)
      .match(/../g)!
      .map((value) => parseInt(value, 16));
    await expect(details.getByRole('listitem').nth(i)).toHaveCSS(
      'border-left-color',
      `rgb(${rgb.join(', ')})`,
    );
  }
  await details.hover();
  await expect(page.locator('.map-route-details')).toHaveCSS('--route-color', engineerColor!);
  await expect(page.locator('.journey-emphasized')).not.toHaveCSS(
    'background-color',
    'rgb(255, 247, 220)',
  );
  for (const [i, [, , color]] of modes.entries()) {
    await expect(paths.nth(i)).toHaveAttribute('stroke', color);
    await expect(paths.nth(i)).toHaveAttribute('stroke-opacity', '1');
  }
  await page.mouse.move(0, 0);
  await expect(paths.first()).toHaveAttribute('stroke-opacity', '0.88');
  await expect(page.locator('.journey-tooltip')).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('transport-colors.png') });
  await page.getByRole('button', { name: 'Показать все', exact: true }).click();
  await expect(page.locator('.journey-path')).toHaveCount(0);
  await expect(page.locator(`.route-overview.engineer-route-${index}`).first()).toHaveAttribute(
    'stroke',
    engineerColor!,
  );
  expect(writes).toEqual([]);
});
