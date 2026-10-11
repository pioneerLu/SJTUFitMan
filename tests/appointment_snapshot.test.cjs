const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const script = fs.readFileSync(path.join(__dirname, '../scripts/appointment_snapshot.js'), 'utf8');

test('appointment snapshot excludes account and internal personal data', () => {
  const component = {
    $options: { name: 'apointmentDetails' }, $children: [],
    detailedData: { venueId: 'example-id', venueName: '测试健身房', venueAddr: '测试地址', secret: 'EXAMPLE-SECRET' },
    userInfo: { loginName: 'EXAMPLE-USER', phonenumber: 'EXAMPLE-PHONE' },
    form: { applicantName: 'EXAMPLE-NAME' },
    tabList: [{ id: 'gym', name: '健身', tension: 1 }],
    weekList: [{ isdate: '2026-10-17', dateId: 'date', viewStr: '10月17日' }],
    topSite: [{ name: '健身房', fieldId: 'field' }],
    seatArray: [[{ status: 0, price: 5, time: '18:00', privateMember: 'EXAMPLE-MEMBER' }]],
    period: () => '18:00-19:00',
  };
  const document = { querySelector: () => ({ __vue__: component }) };
  const snapshot = vm.runInNewContext(`(${script})()`, { document });
  assert.equal(snapshot.venue.venueName, '测试健身房');
  assert.equal(snapshot.dates[0].isdate, '2026-10-17');
  assert.equal(snapshot.slots[0].entries[0].status, 0);
  assert.equal(snapshot.slots[0].entries[0].time_range, '18:00-19:00');
  assert.equal(snapshot.slots[0].entries[0].can_select, true);
  assert.equal(JSON.stringify(snapshot).includes('EXAMPLE-'), false);
});

test('a login page cannot be mistaken for an appointment snapshot', () => {
  const document = { querySelector: () => null };
  assert.equal(vm.runInNewContext(`(${script})()`, { document }), null);
});
