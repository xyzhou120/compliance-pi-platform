import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const labelsSource = await readFile(new URL('../frontend/labels.js', import.meta.url), 'utf8');
const labels = await import(`data:text/javascript;base64,${Buffer.from(labelsSource).toString('base64')}`);

test('source quotes are escaped before entering interface markup', () => {
  assert.equal(labels.escapeHtml('<img src=x onerror="alert(1)">'), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
  assert.equal(labels.escapeHtml("a'b&c"), 'a&#39;b&amp;c');
});

test('execution, business and delivery results retain independent labels', () => {
  assert.equal(labels.executionLabels.completed, '已完成');
  assert.equal(labels.resultLabels.pass, '通过');
  assert.equal(labels.resultLabels.pending, '待核验');
  assert.equal(labels.deliveryLabels.delivered, '已送达');
  assert.notEqual(labels.executionLabels.completed, labels.resultLabels.pass);
});

test('exact page and spreadsheet locators stay visible', () => {
  assert.equal(labels.locatorText({ page: 2, bbox: [1,2,3,4] }), '第 2 页 · 区域 [1,2,3,4]');
  assert.equal(labels.locatorText({ sheet: '调度', row: 8, cell: 'C8' }), '工作表 调度 · 第 8 行 · 单元格 C8');
  assert.equal(labels.locatorText({}), '定位待补充');
});

test('empty pagination never claims a nonexistent first record', () => {
  assert.equal(labels.pageInfo(0,0,0), '0 条');
  assert.equal(labels.pageInfo(20,5,25), '21–25 / 25');
});
