import { uploadSample } from './upload-fixture';
import { expect, test, type Page } from '@playwright/test';
import { mapCoordinates, withMapCoordinates } from './map-fixture';

test.use({ deviceScaleFactor: 2 });

async function mockMap(page: Page, failStyle = false) {
  await page.route('**/api/uploads/*/geography', async (route) => {
    const response = await route.fetch();
    const catalog = mapCoordinates(await response.json());
    // A few separated pins keep the click assertion independent of marker overlap.
    for (const record of Object.values(catalog).slice(4)) {
      record.point = null;
      record.candidates = [];
    }
    await route.fulfill({ response, json: catalog });
  });
  await page.route('**/api/map-config', (route) =>
    route.fulfill({
      json: {
        style_url: '/test-map-style.json',
        tile_url: '/test-map-tiles/{z}/{x}/{y}.svg',
        attribution: 'Test map',
        attribution_url: 'https://example.test',
      },
    }),
  );
  await page.route('**/test-map-style.json', (route) =>
    route.fulfill(
      failStyle
        ? { status: 503 }
        : {
            json: {
              version: 8,
              sources: {
                landmarks: {
                  type: 'geojson',
                  data: {
                    type: 'FeatureCollection',
                    features: Array.from({ length: 70 }, (_, i) => ({
                      type: 'Feature',
                      properties: {},
                      geometry: {
                        type: 'Point',
                        coordinates: [37.6 + Math.floor(i / 10) * 0.002, 55.7 + (i % 10) * 0.002],
                      },
                    })),
                  },
                },
                land: {
                  type: 'geojson',
                  attribution: '<a href="https://example.test/vector">Test vector map</a>',
                  data: {
                    type: 'Feature',
                    properties: {},
                    geometry: {
                      type: 'Polygon',
                      coordinates: [
                        [
                          [36, 54],
                          [40, 54],
                          [40, 57],
                          [36, 57],
                          [36, 54],
                        ],
                      ],
                    },
                  },
                },
              },
              layers: [
                { id: 'background', type: 'background', paint: { 'background-color': '#d8e9ee' } },
                { id: 'land', type: 'fill', source: 'land', paint: { 'fill-color': '#ecede6' } },
                {
                  id: 'landmarks',
                  type: 'circle',
                  source: 'landmarks',
                  paint: { 'circle-color': '#ff00ff', 'circle-radius': 6 },
                },
              ],
            },
          },
    ),
  );
  await page.route('**/test-map-tiles/**', (route) =>
    route.fulfill({
      contentType: 'image/svg+xml',
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><path fill="#ecede6" d="M0 0h256v256H0z"/></svg>',
    }),
  );
}

test('vector basemap renders at Retina resolution and preserves clickable request markers', async ({
  page,
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await mockMap(page);
  const worker = page.waitForResponse(
    (response) => response.url().includes('maplibre-gl-worker') && response.ok(),
  );
  await page.goto('/');
  await uploadSample(page);
  const region = page.getByLabel('Карта заявок');
  const canvas = region.locator('.maplibregl-canvas');
  await expect(canvas).toBeVisible();
  await worker;
  await expect
    .poll(() =>
      canvas.evaluate((c: HTMLCanvasElement) => c.width / c.getBoundingClientRect().width),
    )
    .toBeCloseTo(2, 1);
  await region.locator('.map-stop').first().click();
  await expect(page.getByRole('region', { name: 'Детали заявки' })).toBeVisible();
  await page.getByRole('button', { name: 'К списку заявок' }).click();
  await region.locator('.leaflet-control-zoom-in').click();
  await expect(region.locator('.leaflet-tile')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('map credits collapse after initial display and reopen with the keyboard', async ({
  page,
}) => {
  await mockMap(page);
  await page.goto('/');
  await uploadSample(page);
  const map = page.locator('.route-map');
  const credits = map.locator('.leaflet-control-attribution');
  const toggle = map.getByLabel('Источники карты');
  const provider = credits.getByRole('link', { name: 'Test vector map' });
  await expect(provider).toBeVisible();
  await expect(credits.getByRole('link', { name: 'Leaflet', exact: true })).toHaveCount(0);
  await expect(credits).toBeHidden({ timeout: 12_000 });
  await expect(toggle).toBeVisible();
  await toggle.focus();
  await toggle.press('Enter');
  await expect(provider).toBeVisible();
  await expect(credits.getByRole('link', { name: 'OpenStreetMap contributors' })).toHaveAttribute(
    'href',
    'https://www.openstreetmap.org/copyright',
  );
  await page.clock.install();
  await page.clock.fastForward(10_000);
  await expect(provider).toBeVisible();
  await provider.focus();
  await provider.press('Escape');
  await expect(credits).toBeHidden();
  await expect(toggle).toBeFocused();
  await toggle.click();
  await expect(provider).toBeVisible();
});

test('vector landmarks and route markers stay aligned during panel resizing and collapse', async ({
  page,
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 1000 });
  // Preserve pixels only in the test so we can inspect the actual rendered canvas.
  await page.addInitScript(() => {
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (type: string, options?: object) {
      return original.call(
        this,
        type as '2d',
        type.includes('webgl') ? { ...options, preserveDrawingBuffer: true } : options,
      );
    } as typeof original;
  });
  await mockMap(page);
  await page.route('**/optimize', async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: withMapCoordinates(await response.json()) });
  });
  await page.goto('/');
  await uploadSample(page);
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  await expect(page.locator('.route-overview').first()).toBeVisible();
  await expect(page.locator('.maplibregl-canvas')).toBeVisible();
  await page.evaluate(() => {
    const map = document.querySelector('.route-map')!;
    const canvas = map.querySelector<HTMLCanvasElement>('.maplibregl-canvas')!;
    const marker = [...map.querySelectorAll<HTMLElement>('.map-stop')].sort(
      (a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top,
    )[0];
    const samples: { aligned: boolean; x: number; y: number; zoomOffset: number }[] = [];
    const read = () => {
      const box = canvas.getBoundingClientRect();
      const mapBox = map.getBoundingClientRect();
      const zoomBox = map.querySelector('.leaflet-control-zoom')!.getBoundingClientRect();
      const pin = marker.getBoundingClientRect();
      const x = pin.left + pin.width / 2;
      const y = pin.bottom;
      const gl = canvas.getContext('webgl2')!;
      const pixels = new Uint8Array(7 * 7 * 4);
      gl.readPixels(
        Math.round(((x - box.left) * canvas.width) / box.width) - 3,
        Math.round(canvas.height - ((y - box.top) * canvas.height) / box.height) - 3,
        7,
        7,
        gl.RGBA,
        gl.UNSIGNED_BYTE,
        pixels,
      );
      return {
        aligned: pixels.some(
          (value, i) => i % 4 === 0 && value > 240 && pixels[i + 1] < 30 && pixels[i + 2] > 240,
        ),
        x: x - mapBox.x,
        y: y - mapBox.y,
        zoomOffset: zoomBox.y + zoomBox.height / 2 - (mapBox.y + mapBox.height / 2),
      };
    };
    const observer = new ResizeObserver(() => samples.push(read()));
    observer.observe(map);
    Object.assign(window, { resizeProbe: { read, samples, observer } });
  });
  const sample = () =>
    page.evaluate(() =>
      (
        window as unknown as {
          resizeProbe: { read: () => { aligned: boolean; x: number; y: number } };
        }
      ).resizeProbe.read(),
    );
  await expect.poll(async () => (await sample()).aligned).toBe(true);
  const before = await sample();
  await page.evaluate(() => {
    (window as unknown as { resizeProbe: { samples: unknown[] } }).resizeProbe.samples.length = 0;
  });
  const divider = page.getByRole('separator', { name: 'Высота панели инженеров' });
  const box = (await divider.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + 4);
  await page.mouse.down();
  for (const delta of [-25, -75, -125, -175, -225, -100, 0, 75, 120, 0]) {
    await page.mouse.move(box.x + box.width / 2, box.y + 4 + delta);
    await page.evaluate(
      () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
    );
  }
  await page.mouse.up();
  const sidebar = page.getByRole('separator', { name: 'Ширина левой панели' });
  const sideBox = (await sidebar.boundingBox())!;
  await page.mouse.move(sideBox.x + sideBox.width / 2, sideBox.y + sideBox.height / 2);
  await page.mouse.down();
  for (const delta of [30, 70, 120, 200, 100, 0, -50, -220, -250, 0]) {
    await page.mouse.move(sideBox.x + sideBox.width / 2 + delta, sideBox.y + sideBox.height / 2);
    await page.evaluate(
      () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
    );
  }
  await page.mouse.up();
  await sidebar.press('Home');
  await expect(page.locator('#tools-content')).toBeHidden();
  await expect(page.locator('.tool-rail')).toBeVisible();
  await expect.poll(async () => (await sample()).aligned).toBe(true);
  await page.getByRole('tab', { name: 'План', exact: true }).click();
  await expect(sidebar).toBeVisible();
  await expect.poll(async () => (await sample()).aligned).toBe(true);
  const samples = await page.evaluate(() => {
    const probe = (
      window as unknown as {
        resizeProbe: {
          samples: { aligned: boolean; x: number; y: number; zoomOffset: number }[];
          observer: ResizeObserver;
        };
      }
    ).resizeProbe;
    probe.observer.disconnect();
    return probe.samples;
  });
  expect(samples.length).toBeGreaterThanOrEqual(18);
  for (const result of samples) {
    expect(result.aligned).toBe(true);
    expect(result.x).toBeCloseTo(before.x, 0);
    expect(result.y).toBeCloseTo(before.y, 0);
    expect(result.zoomOffset).toBeCloseTo(0, 0);
  }
  expect(errors).toEqual([]);
});

test('address preview uses the main map style and keeps its pin aligned when resized', async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.addInitScript(() => {
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (type: string, options?: object) {
      return original.call(
        this,
        type as '2d',
        type.includes('webgl') ? { ...options, preserveDrawingBuffer: true } : options,
      );
    } as typeof original;
  });
  await mockMap(page);
  const house = {
    id: 'a'.repeat(32),
    label: 'Москва, улица Тестовая, 12',
    provider: 'catalog',
    precision: 'house',
    house: '12',
    point: {
      lat: 55.71,
      lon: 37.606,
      label: 'Москва, улица Тестовая, 12',
      precision: 'house',
      source: 'test',
    },
  };
  await page.route('**/api/address-suggestions', (route) =>
    route.fulfill({ json: { items: [house], live_available: false, notice: '' } }),
  );
  await page.route('**/api/address-suggestions/resolve', (route) => route.fulfill({ json: house }));
  await page.goto('/');
  await uploadSample(page);
  await page.getByRole('button', { name: 'Рассчитать план' }).click();
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  await page.getByRole('combobox', { name: 'Адрес новой заявки' }).fill(house.label);
  await page.getByRole('option', { name: /Москва, улица Тестовая/ }).click();
  const preview = page.getByLabel('Точка выбранного адреса');
  const canvas = preview.locator('.maplibregl-canvas');
  await expect(canvas).toBeVisible();
  await expect(preview.locator('.location-pin')).toHaveCSS('background-color', 'rgb(40, 118, 220)');
  await expect(preview.getByLabel('Источники карты')).toBeVisible();
  await expect
    .poll(() =>
      canvas.evaluate((c: HTMLCanvasElement) => c.width / c.getBoundingClientRect().width),
    )
    .toBeCloseTo(2, 1);
  const aligned = () =>
    preview.evaluate((map) => {
      const canvas = map.querySelector<HTMLCanvasElement>('.maplibregl-canvas')!;
      const box = canvas.getBoundingClientRect();
      const mapBox = map.getBoundingClientRect();
      const pin = map.querySelector('.address-point-pin')!.getBoundingClientRect();
      const zoom = map.querySelector('.leaflet-control-zoom')!.getBoundingClientRect();
      const x = pin.left + pin.width / 2;
      const y = pin.bottom;
      const gl = canvas.getContext('webgl2')!;
      const pixels = new Uint8Array(7 * 7 * 4);
      gl.readPixels(
        Math.round(((x - box.left) * canvas.width) / box.width) - 3,
        Math.round(canvas.height - ((y - box.top) * canvas.height) / box.height) - 3,
        7,
        7,
        gl.RGBA,
        gl.UNSIGNED_BYTE,
        pixels,
      );
      return (
        pixels.some(
          (value, i) => i % 4 === 0 && value > 240 && pixels[i + 1] < 30 && pixels[i + 2] > 240,
        ) &&
        Math.abs(x - mapBox.x - mapBox.width / 2) <= 1 &&
        Math.abs(y - mapBox.y - mapBox.height / 2) <= 1 &&
        Math.abs(zoom.y + zoom.height / 2 - mapBox.y - mapBox.height / 2) <= 1
      );
    });
  await expect.poll(aligned).toBe(true);
  const sidebar = page.getByRole('separator', { name: 'Ширина левой панели' });
  await sidebar.press('End');
  await expect.poll(aligned).toBe(true);
  await page
    .locator('.event-address')
    .screenshot({ path: testInfo.outputPath('address-map-wide.png') });
  await sidebar.press('Home');
  await expect(preview).toBeHidden();
  await page.getByRole('tab', { name: 'Заявка', exact: true }).click();
  await expect(preview).toBeVisible();
  await expect.poll(aligned).toBe(true);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(aligned).toBe(true);
  await preview.locator('.leaflet-control-zoom-in').click();
  await expect.poll(aligned).toBe(true);
  await page
    .locator('.event-address')
    .screenshot({ path: testInfo.outputPath('address-map-mobile.png') });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});

for (const failure of ['service', 'webgl'] as const) {
  test(`basemap falls back to Retina raster when ${failure} is unavailable`, async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await mockMap(page, failure === 'service');
    if (failure === 'webgl')
      await page.addInitScript(() => {
        const original = HTMLCanvasElement.prototype.getContext;
        HTMLCanvasElement.prototype.getContext = function (type: string, options?: unknown) {
          if (type.includes('webgl')) return null;
          return original.call(this, type as '2d', options);
        } as typeof original;
      });
    await page.goto('/');
    await uploadSample(page);
    const region = page.getByLabel('Карта заявок');
    const tile = region.locator('.leaflet-tile-loaded').first();
    await expect(tile).toBeVisible();
    await expect(region.locator('.maplibregl-canvas')).toHaveCount(0);
    await expect
      .poll(() => tile.evaluate((img: HTMLImageElement) => img.naturalWidth / img.width))
      .toBe(2);
    await expect(region.locator('.leaflet-control-attribution')).toContainText('Test map');
    // At maximum zoom, Retina must overzoom existing tiles instead of leaving a blank map.
    const zoomIn = region.locator('.leaflet-control-zoom-in');
    for (let i = 0; i < 9; i++) {
      if ((await zoomIn.getAttribute('aria-disabled')) === 'true') break;
      await zoomIn.click();
      await page.waitForTimeout(300);
    }
    await expect(zoomIn).toHaveClass(/leaflet-disabled/);
    await expect(tile).toBeVisible();
    expect(errors).toEqual([]);
  });
}
