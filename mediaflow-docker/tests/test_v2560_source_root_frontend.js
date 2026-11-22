const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../frontend/index.html'), 'utf8');
for (const m of html.matchAll(/<script\b[^>]*>([^]*?)<\/script>/g)) if (m[1].trim()) new vm.Script(m[1]);
const logs = [];
let response, result;
const ctx = {
  currentActionRecordId: 'fixture', closeModal(){}, findShowDisplayName: () => 'Show',
  escapeHtml: value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),
  getNowTimestamp: () => '00:00:00',
  api: async () => response,
  runBatchPipeline: async options => {result = await options.processItem(options.items[0], 0, 1, value => logs.push(value));},
};
vm.createContext(ctx);
const code = html.match(/async function confirmCleanRedundantDone\([^]*?\n    \}/)[0];
vm.runInContext(code, ctx);
(async () => {
  response = {ok: true, message: '本季已清理，剧集外层保留：Season 2 <fixture>'};
  await ctx.confirmCleanRedundantDone();
  assert.equal(result.success, true);
  assert(logs.at(-1).includes('剧集外层保留：Season 2 &lt;fixture&gt;'));
  assert(!logs.at(-1).includes('残留目录已安全清除'));
  response = {ok: false, message: '本季已清理，剧集外层待处理：附件读取失败'};
  await ctx.confirmCleanRedundantDone();
  assert.equal(result.success, false);
  assert(logs.at(-1).includes('清理未全部完成'));
  assert(logs.at(-1).includes(response.message));
  console.log('PASS: source cleanup displays actual root receipt and escapes messages');
})().catch(e => {console.error(e); process.exitCode = 1;});
