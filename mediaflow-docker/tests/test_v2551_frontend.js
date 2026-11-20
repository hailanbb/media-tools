const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../frontend/index.html'), 'utf8');
for (const m of html.matchAll(/<script\b[^>]*>([^]*?)<\/script>/g)) {
  if (m[1].trim()) new vm.Script(m[1]);
}
function extract(name) {
  const match = html.match(new RegExp('(?:async )?function ' + name + '\\([^]*?\\n    \\}'));
  assert(match, name);
  return match[0];
}
const opened = [], alerts = [], calls = [];
const context = {
  currentTaskId: 't', anomalyReviewQueue: [], anomalyReviewTaskId: null,
  taskList: [{id:'t', _seasonsLoaded:true}],
  document: {
    querySelectorAll: () => [{value:'a'}, {value:'b'}],
    getElementById: () => ({classList:{add:()=>{}}})
  },
  findItemByRecordId: id => ({id}),
  openManualConfirmModal: (id, fromQueue) => opened.push([id,fromQueue]),
  alert: message => alerts.push(message),
  api: async (url, options) => {calls.push([url,options]); if(url.includes('/b/')) throw Error('fixture failure');},
  loadTasks: async()=>{}, selectTask: async()=>{},
};
vm.createContext(context);
for(const name of ['closeModal','startAnomalyReviewQueue','openNextAnomalyReview','skipAnomalyReview','holdSelectedAnomalies']) {
  vm.runInContext(extract(name), context);
}
context.startAnomalyReviewQueue();
assert.deepEqual(opened,[['a',true]]);
context.skipAnomalyReview();
assert.deepEqual(opened,[['a',true],['b',true]]);
assert.equal(calls.length,0,'跳过核对不得写入任何完成状态');
context.closeModal('manualConfirmModal');
assert.equal(context.anomalyReviewTaskId,null);
assert.equal(context.anomalyReviewQueue.length,0);
context.startAnomalyReviewQueue();
context.currentTaskId='other';
context.openNextAnomalyReview();
assert.equal(opened.length,3,'切换任务后不能打开旧队列下一项');
context.currentTaskId='t';
(async()=>{
  const btn={disabled:false,isConnected:true};
  await context.holdSelectedAnomalies(btn);
  assert.equal(calls.length,2);
  assert.equal(btn.disabled,false);
  assert(alerts.some(a=>a.includes('fixture failure')),'批量保留的部分失败必须显示');
  assert.equal(context.taskList[0]._seasonsLoaded,false);
  console.log('v2.5.51 frontend: syntax, queue, skip, cancel, task isolation, partial hold PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});
