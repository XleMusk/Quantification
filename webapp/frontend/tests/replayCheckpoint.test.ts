import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';
import type { Bar, Dataset } from '../src/types';
import { readReplayCheckpoint, replayCheckpointKey, type ReplayCheckpoint } from '../src/pages/chartReviewState';

const dataset = { id: 'pinned-snapshot', adjustment: 'qfq' } as Dataset;
const symbol = '000001.SZ';
const session = '12345678-1234-4234-8234-123456789abc';
const otherSession = 'abcdef01-1234-4234-9234-123456789abc';
const bars: Bar[] = Array.from({ length: 120 }, (_, index) => ({
  date: new Date(Date.UTC(2025, 0, index + 1)).toISOString().slice(0, 10),
  symbol, open: 10, high: 11, low: 9, close: 10, volume: 100,
}));
const checkpoint = (cursor: number, patch: Partial<ReplayCheckpoint> = {}): ReplayCheckpoint => ({
  version: 1, session, startDate: '2025-01-01', cursor, ...patch,
});
let stored: Map<string, string>;
const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');

beforeEach(() => {
  stored = new Map();
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
    getItem: (key: string) => stored.get(key) ?? null,
    setItem: (key: string, value: string) => stored.set(key, value),
  } });
});
afterEach(() => {
  if (originalStorage) Object.defineProperty(globalThis, 'localStorage', originalStorage);
  else Reflect.deleteProperty(globalThis, 'localStorage');
});
const save = (period: string, value: unknown) => stored.set(replayCheckpointKey(dataset, symbol, period), JSON.stringify(value));

test('stale W83 checkpoint resumes the same session at D100', () => {
  save('W', checkpoint(83)); save('D', checkpoint(100)); save('M', checkpoint(90));
  assert.deepEqual(readReplayCheckpoint(dataset, symbol, bars, 'W'), checkpoint(100));
  assert.deepEqual(readReplayCheckpoint(dataset, symbol, bars, 'M'), checkpoint(100));
  assert.deepEqual(readReplayCheckpoint(dataset, symbol, bars, 'D'), checkpoint(100));
});

test('different replay sessions never merge their progress', () => {
  save('W', checkpoint(83)); save('D', checkpoint(110, { session: otherSession })); save('M', checkpoint(90));
  assert.deepEqual(readReplayCheckpoint(dataset, symbol, bars, 'W'), checkpoint(90));
  assert.deepEqual(readReplayCheckpoint(dataset, symbol, bars, 'D'), checkpoint(110, { session: otherSession }));
});

test('each candidate cursor, start date and session must be valid', () => {
  const invalid: unknown[] = [
    checkpoint(0), checkpoint(-1), checkpoint(83.5), checkpoint(121), checkpoint(NaN),
    { ...checkpoint(100), cursor: '100' }, checkpoint(100, { startDate: '2025-02-29' }),
    checkpoint(100, { startDate: '2025-02-30' }), checkpoint(100, { startDate: '2024-12-31' }),
    checkpoint(100, { startDate: '2025-05-01' }), checkpoint(10, { startDate: '2025-02-01' }),
    checkpoint(100, { startDate: '2025-01-01T00:00:00Z' }),
    { ...checkpoint(100), startDate: null }, checkpoint(100, { session: 'bad-session' }),
    { ...checkpoint(100), version: 2 }, null, [], 'invalid',
  ];
  for (const value of invalid) {
    save('W', checkpoint(83)); save('D', value);
    assert.deepEqual(readReplayCheckpoint(dataset, symbol, bars, 'W'), checkpoint(83), JSON.stringify(value));
    assert.equal(readReplayCheckpoint(dataset, symbol, bars, 'D'), null, JSON.stringify(value));
  }
  stored.set(replayCheckpointKey(dataset, symbol, 'D'), '{broken JSON');
  assert.deepEqual(readReplayCheckpoint(dataset, symbol, bars, 'W'), checkpoint(83));
  assert.equal(readReplayCheckpoint(dataset, symbol, bars, 'D'), null);
});

test('current period must have a legal checkpoint before checking other periods', () => {
  save('D', checkpoint(100));
  assert.equal(readReplayCheckpoint(dataset, symbol, bars, 'W'), null);
  assert.equal(readReplayCheckpoint(dataset, symbol, [], 'D'), null);
});

test('legacy daily version 1 records keep their key, default period and date semantics', () => {
  const old = checkpoint(40, { startDate: '2025-02-01' });
  save('D', old);
  assert.equal(replayCheckpointKey(dataset, symbol), replayCheckpointKey(dataset, symbol, 'D'));
  assert.deepEqual(readReplayCheckpoint(dataset, symbol, bars), old);
  assert.deepEqual(readReplayCheckpoint(dataset, symbol, bars.map(bar => ({ ...bar, date: `${bar.date}T00:00:00.000` }))), old);
  assert.deepEqual(readReplayCheckpoint(dataset, symbol, bars), old, 'reading does not migrate by writing or mutate the record');
  assert.deepEqual(JSON.parse(stored.get(replayCheckpointKey(dataset, symbol))!), old);
});
