const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const script = fs.readFileSync(path.join(__dirname, '../scripts/checkout_snapshot.js'), 'utf8');

function snapshot(component) {
  const document = {
    querySelector: selector => selector === '#app' ? {__vue__: component} : null,
    createElement: () => ({innerHTML: '', textContent: '预订条款'}),
  };
  return vm.runInNewContext(`(${script})()`, {document});
}

test('selection observation excludes account, tokens and private fields', () => {
  const result = snapshot({$options: {name: 'apointmentDetails'}, $children: [],
    userInfo: {name: 'PRIVATE-NAME'}, token: 'PRIVATE-TOKEN',
    allSun: '8', clause: '<p>预订条款</p>', clauseTitle: '预订须知', dialogVisible: true,
    buyLists: [{subSiteId: 'field2', subSitename: '八球台2', scheduleTime: '12:00-13:00',
      venuePrice: '8', venueNum: 1, status: 1, userId: 'PRIVATE-ID'}],
  });
  assert.equal(result.selected[0].subSiteId, 'field2');
  assert.equal(result.terms_text, '预订条款');
  assert.equal(JSON.stringify(result).includes('PRIVATE-'), false);
});

test('order observation excludes accompanying people and profile fields', () => {
  const result = snapshot({$options: {name: 'orderDetails'}, $children: [], datas: {
    venueName: '学生中心', orderStateId: '0', countPrice: '8', price: '8',
    applicantName: 'PRIVATE-APPLICANT', accompanyings: [{name: 'PRIVATE-PERSON', mobile: 'PRIVATE-PHONE'}],
    spaces: [{venueName: '台球', scheduleDate: '2026-10-11', scheduleTime: '12:00-13:00',
      subSitename: '八球台2', venuePrice: '8', userId: 'PRIVATE-ID'}],
  }});
  assert.equal(result.order.orderStateId, '0');
  assert.equal(result.spaces[0].scheduleDate, '2026-10-11');
  assert.equal(JSON.stringify(result).includes('PRIVATE-'), false);
});
