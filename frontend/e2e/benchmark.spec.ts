import { test, expect } from '@playwright/test';

test('benchmark page loads', async ({ page }) => {
  await page.goto('/benchmark/test_video_e2e');
  await expect(page.locator('h1')).toContainText('Benchmark Review');
});

test('video player exists', async ({ page }) => {
  await page.goto('/benchmark/test_video_e2e');
  const video = page.locator('video');
  await expect(video).toBeVisible();
});

test('annotation form exists', async ({ page }) => {
  await page.goto('/benchmark/test_video_e2e');
  await expect(page.locator('input[placeholder="Title"]')).toBeVisible();
  await expect(page.locator('button', { hasText: 'Save Annotation' })).toBeVisible();
});
