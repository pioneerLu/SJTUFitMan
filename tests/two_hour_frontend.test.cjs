// Execute the school's cached selection functions; no network or order submission.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const directory = path.join(__dirname, '../.inspection');
const file = fs.existsSync(directory) && fs.readdirSync(directory).find(name => name.startsWith('9.') && name.endsWith('.js'));

test('school gym controls retain two selected hours with one combined total', {skip: !file}, () => {
  const source = fs.readFileSync(path.join(directory, file), 'utf8');
  function extract(name, next) {
    const start = source.indexOf(name + ':function');
    const end = source.indexOf('},' + next + ':function', start);
    assert.ok(start >= 0 && end > start, 'observed school function boundaries must still match');
    return vm.runInNewContext('(' + source.slice(start + name.length + 1, end + 1) + ')',
      {s: () => JSON.stringify}, {timeout: 1000});
  }
  const view = {
    seatCol: 1, seatRow: 15, nerData: '很紧张(签退)', buyLists: [], allSun: 0,
    topSite: [{name: '场地1', fieldId: 'gym-field', fieldDetailStatus: '4'}],
    seatArray: [Array.from({length: 15}, () => ({status: 0, price: '2'}))],
    period: extract('period', 'judgingVenue'),
    judgingVenue: extract('judgingVenue', 'judgingVenueId'),
    judgingVenueId: extract('judgingVenueId', 'handleChooseSeat'),
    addsumCompute: extract('addsumCompute', 'getci'),
  };
  const choose = extract('handleChooseSeat', 'addsumCompute');
  choose.call(view, 0, 11);
  choose.call(view, 0, 12);
  const selected = JSON.parse(JSON.stringify(view.buyLists));
  assert.equal(selected.length, 2);
  assert.deepEqual(selected.map(cell => cell.scheduleTime), ['18:00-19:00', '19:00-20:00']);
  assert.ok(selected.every(cell => cell.subSiteId === 'gym-field' && cell.venueNum === 1));
  assert.equal(view.allSun, '4.00');
});
