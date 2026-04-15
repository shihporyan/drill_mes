import { test, expect } from '@playwright/test';

const today = new Date().toISOString().slice(0, 10);

test.describe('API Contract', () => {
  test('GET /api/drilling/overview returns valid structure', async ({ request }) => {
    const resp = await request.get('/api/drilling/overview');
    expect(resp.status()).toBe(200);

    const data = await resp.json();
    expect(data).toHaveProperty('machines');
    expect(data).toHaveProperty('summary');
    expect(data).toHaveProperty('timestamp');
    expect(Array.isArray(data.machines)).toBe(true);
    expect(data.summary).toHaveProperty('running');
    expect(data.summary).toHaveProperty('idle');
    expect(data.summary).toHaveProperty('stopped');
    expect(data.summary).toHaveProperty('offline');
    expect(data.summary).toHaveProperty('total');

    for (const machine of data.machines) {
      expect(typeof machine.duration_minutes).toBe('number');
      expect(machine.duration_minutes).toBeGreaterThanOrEqual(0);
    }
  });

  test('GET /api/drilling/utilization returns valid structure', async ({ request }) => {
    const resp = await request.get(`/api/drilling/utilization?period=day&date=${today}`);
    expect(resp.status()).toBe(200);

    const data = await resp.json();
    expect(data).toHaveProperty('machines');
    expect(Array.isArray(data.machines)).toBe(true);
    expect(data).toHaveProperty('fleet_average');
    expect(data).toHaveProperty('target');
    expect(typeof data.fleet_average).toBe('number');
  });

  test('GET /api/drilling/heatmap returns valid structure', async ({ request }) => {
    const resp = await request.get(`/api/drilling/heatmap?date=${today}`);
    expect(resp.status()).toBe(200);

    const data = await resp.json();
    expect(data).toHaveProperty('machines');
    expect(Array.isArray(data.machines)).toBe(true);

    if (data.machines.length > 0) {
      const machine = data.machines[0];
      expect(machine).toHaveProperty('id');
      expect(machine).toHaveProperty('hours');
      expect(Array.isArray(machine.hours)).toBe(true);
    }
  });

  test('GET /api/drilling/transitions returns valid structure', async ({ request }) => {
    const resp = await request.get(`/api/drilling/transitions?machine=M01&date=${today}`);
    expect(resp.status()).toBe(200);

    const data = await resp.json();
    expect(data).toHaveProperty('machine_id');
    expect(data).toHaveProperty('date');
    expect(data).toHaveProperty('transitions');
    expect(Array.isArray(data.transitions)).toBe(true);
  });
});
