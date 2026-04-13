import { test, expect } from '@playwright/test';
import path from 'path';

const SCREENSHOT_DIR = path.join(__dirname, '..', 'screenshot');

// Wait for React + Babel standalone to finish rendering
async function waitForReactReady(page) {
  await page.waitForSelector('#root h1', { timeout: 15_000 });
}

// --- Page Load ---

test.describe('Page Load', () => {
  test('dashboard loads with correct title', async ({ page }) => {
    await page.goto('/');
    await waitForReactReady(page);

    await expect(page).toHaveTitle('鑽孔機稼動監控');
    await expect(page.locator('#root h1')).toHaveText('鑽孔機稼動監控');
  });
});

// --- Tab Navigation ---

test.describe('Tab Navigation', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await waitForReactReady(page);
  });

  test('four tabs are visible', async ({ page }) => {
    const tabs = ['機台總覽', '稼動排行', '稼動分析', '作業細節'];
    for (const label of tabs) {
      await expect(page.getByRole('button', { name: label })).toBeVisible();
    }
  });

  test('clicking tabs switches content', async ({ page }) => {
    // Default: overview tab active
    await expect(page.getByRole('button', { name: '機台總覽' })).toHaveCSS('font-weight', '700');

    // Switch to ranking
    await page.getByRole('button', { name: '稼動排行' }).click();
    await expect(page.getByText('期間平均稼動率')).toBeVisible();

    // Switch to analysis
    await page.getByRole('button', { name: '稼動分析' }).click();
    await expect(page.getByText('24 小時稼動熱力圖')).toBeVisible();

    // Switch to detail
    await page.getByRole('button', { name: '作業細節' }).click();
    await expect(page.getByText('稼動中').first()).toBeVisible();
  });
});

// --- Overview Tab ---

test.describe('Overview Tab', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await waitForReactReady(page);
  });

  test('summary cards show states', async ({ page }) => {
    const labels = ['稼動中', '閒置', '停機 / 離線'];
    for (const label of labels) {
      await expect(page.getByText(label, { exact: true }).first()).toBeVisible();
    }
    // Each card should have a "台" unit
    const unitCount = await page.getByText('台').count();
    expect(unitCount).toBeGreaterThanOrEqual(3);
  });

  test('machine cards grid renders', async ({ page }) => {
    // Machine IDs like M01, M02...
    await expect(page.getByText(/^M\d{2}$/).first()).toBeVisible();
    const machineCards = await page.getByText(/^M\d{2}$/).count();
    expect(machineCards).toBeGreaterThanOrEqual(1);
  });

  test('machine cards show state labels', async ({ page }) => {
    const stateLabels = ['稼動中', '閒置', '停機', '離線'];
    let found = 0;
    for (const label of stateLabels) {
      const count = await page.getByText(label).count();
      found += count;
    }
    expect(found).toBeGreaterThanOrEqual(1);
  });
});

// --- Ranking Tab ---

test.describe('Ranking Tab', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await waitForReactReady(page);
    await page.getByRole('button', { name: '稼動排行' }).click();
  });

  test('shows fleet average utilization', async ({ page }) => {
    await expect(page.getByText('期間平均稼動率')).toBeVisible();
    // The big percentage number
    await expect(page.getByText('%').first()).toBeVisible();
  });

  test('shows drill-down chart', async ({ page }) => {
    await expect(page.getByText('年度總覽')).toBeVisible();
    // Year view shows monthly bars with label
    await expect(page.getByText('各月平均稼動率')).toBeVisible();
  });

  test('shows type compare cards', async ({ page }) => {
    await expect(page.getByText('機鑽平均')).toBeVisible();
  });
});

// --- Analysis Tab ---

test.describe('Analysis Tab', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await waitForReactReady(page);
    await page.getByRole('button', { name: '稼動分析' }).click();
  });

  test('heatmap renders with 24 hour labels', async ({ page }) => {
    await expect(page.getByText('24 小時稼動熱力圖')).toBeVisible();
    // Hour labels 00 and 23 should be visible
    await expect(page.getByText('00', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('23', { exact: true })).toBeVisible();
  });

  test('shift summary cards visible', async ({ page }) => {
    await expect(page.getByText('日班平均（08–20）')).toBeVisible();
    await expect(page.getByText('夜班平均（20–08）')).toBeVisible();
  });

  test('filter buttons work', async ({ page }) => {
    const filterBtn = page.getByRole('button', { name: '< 25%' });
    await filterBtn.click();
    // Filter button should now be active (has colored background)
    await expect(filterBtn).toHaveCSS('color', 'rgb(255, 255, 255)');

    // Reset
    await page.getByRole('button', { name: '全部顯示' }).click();
  });
});

// --- Detail Tab ---

test.describe('Detail Tab', () => {
  test('shows running and not-running sections', async ({ page }) => {
    await page.goto('/');
    await waitForReactReady(page);
    await page.getByRole('button', { name: '作業細節' }).click();
    await expect(page.getByText(/稼動中（\d+ 台）/)).toBeVisible();
    await expect(page.getByText(/未稼動（\d+ 台）/)).toBeVisible();
  });
});

// --- Screenshot Captures ---

test.describe('Screenshot Captures', () => {
  test('capture all tabs', async ({ page }) => {
    await page.goto('/');
    await waitForReactReady(page);

    // Overview
    await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'tab-overview.png'), fullPage: true });

    // Ranking
    await page.getByRole('button', { name: '稼動排行' }).click();
    await page.waitForSelector('text=期間平均稼動率', { timeout: 5000 });
    await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'tab-ranking.png'), fullPage: true });

    // Analysis
    await page.getByRole('button', { name: '稼動分析' }).click();
    await page.waitForSelector('text=24 小時稼動熱力圖', { timeout: 5000 });
    await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'tab-analysis.png'), fullPage: true });

    // Detail
    await page.getByRole('button', { name: '作業細節' }).click();
    await page.waitForSelector('text=稼動中', { timeout: 5000 });
    await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'tab-detail.png'), fullPage: true });
  });
});
