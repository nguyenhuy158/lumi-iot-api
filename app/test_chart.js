const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
new vm.Script(html.split('<script>')[1].split('</script>')[0]);
const nodes = Object.fromEntries(['chart', 'chartFrom', 'chartTo', 'chartNote', 'sampleCount', 'chartLegend', 'chartDetail'].map(id => [id, {clientWidth: 390, clientHeight: 175, setAttribute() {}, getBoundingClientRect: () => ({left: 0})}]));
const context = vm.createContext({
 $: id => nodes[id],
 units: {voltage_v: 'V', current_a: 'A', power_w: 'W'},
 names: {voltage_v: 'Điện áp', current_a: 'Dòng điện', power_w: 'Công suất'},
 colors: {voltage_v: '#79b8ff', current_a: '#86d3a1', power_w: '#dec78c'},
 number: value => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : '—',
 stamp: String,
 selected: 'all',
 chartData: {points: [
  {ts: 1, voltage_v: 210, current_a: 0, power_w: 0},
  {ts: 2, voltage_v: 220, current_a: 0.2, power_w: 20},
  {ts: 3, voltage_v: null, current_a: null, power_w: null},
  {ts: 4, voltage_v: 215, current_a: 0.1, power_w: 10}
 ], first_sample_at: 1, to: 4, bucket_seconds: 10, sample_count: 4}
});
vm.runInContext(html.slice(html.indexOf('function drawChart(){'), html.indexOf('function renderEvents()')) + '\ndrawChart();', context);
assert.equal((nodes.chart.innerHTML.match(/<polyline/g) || []).length, 3);
assert.equal((nodes.chart.innerHTML.match(/<circle/g) || []).length, 3);
for (const color of Object.values(context.colors)) assert(nodes.chart.innerHTML.includes(color));
assert(nodes.chartNote.textContent.includes('thang đo riêng'));
nodes.chart.onpointerdown({clientX: 8});
assert(nodes.chartDetail.textContent.includes('210.00 V · 0.00 A · 0.00 W'));
nodes.chart.onpointermove({clientX: 257});
assert(nodes.chartDetail.textContent.includes('— V · — A · — W'));
context.selected = 'power_w';
vm.runInContext('drawChart()', context);
assert.equal((nodes.chart.innerHTML.match(/<polyline/g) || []).length, 1);
assert(!nodes.chart.innerHTML.includes('#79b8ff'));
context.selected = 'all';
context.chartData.points = [{ts: 5, voltage_v: null, current_a: NaN, power_w: Infinity}];
vm.runInContext('drawChart()', context);
assert.equal(nodes.chart.innerHTML, '');
assert.equal(nodes.chart.onpointermove, null);
context.chartData.points = [{ts: 6, voltage_v: 220, current_a: null, power_w: 0}];
vm.runInContext('drawChart()', context);
assert.equal((nodes.chart.innerHTML.match(/<circle/g) || []).length, 2);
assert(!nodes.chart.innerHTML.includes('NaN'));
console.log('PASS: three scales/colors, gaps, zero, pointer/touch detail, single metric, missing/non-finite data');
nodes.hostState = {};
nodes.hostChecked = {};
vm.runInContext(html.slice(html.indexOf('function renderHostStatus(){'), html.indexOf('function renderStatus(){')), context);
for (const [state, label] of Object.entries({online:'Online', checking:'Đang kiểm tra lại', unreachable:'Mất kết nối', unknown:'Chưa rõ'})) {
 context.latest = {nixostail: {state, checked_at: Date.now()/1000, stale: false}};
 vm.runInContext('renderHostStatus()', context);
 assert.equal(nodes.hostState.textContent, label);
}
context.latest.nixostail = {state:'online', checked_at:Date.now()/1000-31, stale:false};
vm.runInContext('renderHostStatus()', context);
assert.equal(nodes.hostState.textContent, 'Chưa rõ');
context.latest = null;
vm.runInContext('renderHostStatus()', context);
assert.equal(nodes.hostState.textContent, 'Chưa rõ');
console.log('PASS: host labels and stale browser data');
