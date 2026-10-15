import { describe, expect, it } from 'vitest';
import { MessageTimePipe } from './message-time.pipe';

describe('MessageTimePipe', () => {
  const pipe = new MessageTimePipe();

  it.each([
    [new Date(2026, 0, 2, 3, 4, 5), '2026-01-02 03:04:05'],
    [new Date(2026, 10, 12, 13, 14, 15), '2026-11-12 13:14:15'],
    [new Date(2026, 0, 1, 0, 0, 0), '2026-01-01 00:00:00'],
    [new Date(2025, 11, 31, 23, 59, 59), '2025-12-31 23:59:59'],
    [new Date(2026, 6, 1, 23, 5, 9), '2026-07-01 23:05:09'],
  ] as const)('formats %s in local time as %s', (localDate, expected) => {
    expect(pipe.transform(localDate.toISOString())).toBe(expected);
  });

  it('converts explicit offsets to the same local time as the equivalent UTC instant', () => {
    const localTime = pipe.transform('2026-01-02T03:04:05+08:00');
    expect(localTime).toBe(pipe.transform('2026-01-01T19:04:05.000Z'));
    expect(localTime).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  });

  it.each([undefined, null, '', 'not-a-timestamp', '10:11', 'Infinity'])(
    'leaves missing or invalid historical timestamps undated: %s',
    (value) => {
      expect(pipe.transform(value)).toBe('');
    },
  );
});
