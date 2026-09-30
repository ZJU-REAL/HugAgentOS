import dayjs from 'dayjs';
import assert from 'node:assert/strict';
import { formatDateTime, formatDateKey, parseServerTime, dateRangeBoundary } from '../src/utils/date';
import { getHistoryDayDiff } from '../src/utils/history';

const instant = Date.parse('2026-09-28T06:06:15.900Z');
for (const zone of ['UTC', 'Asia/Shanghai', 'America/Los_Angeles']) {
  process.env.TZ = zone;
  assert.equal(parseServerTime('2026-09-28T06:06:15.900'), instant);
  assert.equal(parseServerTime('2026-09-28 06:06:15.900'), instant);
  assert.equal(parseServerTime('2026-09-28T14:06:15.900+08:00'), instant);
  assert.equal(formatDateTime(instant), '2026/09/28 14:06:15');
  assert.equal(formatDateKey('2026-09-28T17:00:00Z'), '2026-09-29');
  assert.equal(getHistoryDayDiff(Date.now()), 0);
  assert.equal(dateRangeBoundary(dayjs('2026-09-28')), '2026-09-27T16:00:00.000Z');
  assert.equal(dateRangeBoundary(dayjs('2026-09-28'), true), '2026-09-28T15:59:59.999Z');
  assert.ok(Number.isNaN(parseServerTime('invalid')));
}
console.log('Timezone parsing and display passed in 3 host zones');
