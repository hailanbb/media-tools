const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('frontend/index.html', 'utf8');
const nodes = new Map();
const node = id => { if (!nodes.has(id)) nodes.set(id, {id, value:'', checked:false, disabled:false, textContent:'', isConnected:true}); return nodes.get(id); };
let allow = true, response = {ok:true, message:'已核对'}, refreshFail = false;
const calls = [], closed = [], alerts = [];
const controls = [node('mcSubmitBtn'), node('otherButton')];
const ctx = {currentTaskId:'t', currentManualConfirmRecId:'r', manualReviewBusy:false,
  manualReviewInitialDraft:'', manualConfirmPreviewSequence:0, anomalyReviewTaskId:null,
  taskList:[{id:'t'}], document:{getElementById:node, querySelectorAll:selector => selector.includes('button') ? controls : [node('mcNote')]},
  findItemByRecordId:()=>({associatedRecords:[{id:'other'}]}), confirm:()=>allow,
  alert:m=>alerts.push(m), closeModal:id=>closed.push(id), openNextAnomalyReview(){},
  loadTasks:async()=>{if(refreshFail) throw Error('refresh failed');}, selectTask:async()=>{},
  api:async(url,opts)=>{calls.push([url, JSON.parse(opts.body)]);return response;}};
vm.createContext(ctx);
for (const name of ['manualReviewDraft','runManualReviewAction','reconcileSourceCard']) {
  const match = html.match(new RegExp('(?:async )?function '+name+'\\([^]*?\\n    \\}'));
  assert(match); vm.runInContext(match[0],ctx);
}
(async()=>{
  ctx.manualReviewInitialDraft = ctx.manualReviewDraft();
  node('mcNote').value='尚未保存'; allow=false;
  await ctx.runManualReviewAction('refresh',node('otherButton'));
  assert.equal(calls.length,0,'未批准丢弃草稿不能发送请求');
  allow=true;
  await ctx.runManualReviewAction('refresh',node('otherButton'));
  assert.equal(calls[0][0],'/api/tasks/t/refresh-modified');
  assert.deepEqual(calls[0][1].record_ids,['r','other']);
  assert.equal(node('mcSubmitBtn').disabled,false);
  response={ok:false,message:'目录读取失败，保留待办'}; const before=closed.length;
  await ctx.runManualReviewAction('source',node('otherButton'));
  assert.equal(closed.length,before,'失败不能关闭复核面板');
  assert.match(node('mcSaveProgress').textContent,/保留待办/);
  response={ok:true,message:'已暂不处理'}; refreshFail=true;
  await ctx.runManualReviewAction('hold',node('otherButton'));
  assert.match(node('mcSaveProgress').textContent,/已暂不处理.*列表刷新失败/);
  assert.equal(ctx.manualReviewBusy,false);
  refreshFail=false; ctx.manualReviewBusy=true; const count=calls.length;
  await ctx.runManualReviewAction('hold',node('otherButton'));
  assert.equal(calls.length,count,'进行中不能重复提交');
  ctx.manualReviewBusy=false;
  await ctx.reconcileSourceCard('r',node('otherButton'));
  assert.equal(calls.at(-1)[0],'/api/tasks/t/seasons/r/reconcile-source');
  assert(!html.includes('补充本季事实'));
  assert(!html.includes('>重新取证并更新</button>'));
  console.log('PASS: 手动复核草稿保护、关联范围、失败保留、回执与刷新分离、防重入、源目录对账');
})().catch(e=>{console.error(e);process.exitCode=1;});
