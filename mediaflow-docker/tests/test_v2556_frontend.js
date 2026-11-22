const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../frontend/index.html'), 'utf8');
for (const m of html.matchAll(/<script\b[^>]*>([^]*?)<\/script>/g)) if (m[1].trim()) new vm.Script(m[1]);
const elements = new Map();
const node = id => {
  if (!elements.has(id)) elements.set(id, {innerHTML:'', textContent:'', checked:false, disabled:false,
    classList:{add(){},remove(){}}, setAttribute(k,v){this[k]=v;},removeAttribute(k){delete this[k];},focus(){}});
  return elements.get(id);
};
let checked = [];
let errors = [];
const storage = new Map();
const escape = s => String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const ctx = {console:{error:(...args)=>errors.push(args),warn(){}}, escapeHtml:escape,escapeJsAttr:escape,
  cleanSeasonStatusNote:s=>s, currentTaskId:'t', currentCategory:'download', currentGapView:'todo',
  currentGapSubFilter:'all', configRoots:{}, activeAnomalyTab:'all',batchDiscardExecuting:false,
  batchDiscardPreview:null,batchDiscardSequence:0,
  document:{getElementById:node,querySelectorAll:()=>checked,querySelector:()=>({content:'2.5.56',focus(){}})},
  sessionStorage:{setItem:(k,v)=>storage.set(k,v),getItem:k=>storage.get(k)},
  updateGapFillSitesToolbarBadge(){},renderDirectoryGroupsPanel(){}, openModal(){},alert(){},
  loadTasks:async()=>{},selectTask:async()=>{},
};
vm.createContext(ctx);
for(const name of ['getGapFillLists','gapFillCountLabel','renderEpisodesList','switchGapFillTab',
  'handleGapFillTabKeydown','updateSelection','renderBatchDiscardPreview','renderBatchDiscardResult',
  'updateBatchDiscardConsent','openBatchDiscardPreview','executeBatchDiscard','checkAppVersion']) {
  const match = html.match(new RegExp('(async )?function '+name+'\\([^]*?\\n    \\}'));
  assert(match,name); vm.runInContext(match[0],ctx);
}
function item(id, bucket, outcome) {
  return {id,show:'作品'+id,season:'S01',statusText:'缺第2集',mainCategory:'download',actionType:'download',
    pathA:'/fixture/'+id+'/Season 1',pathB:'',note:'',dest:'',raw:{status:'download_needed'},
    gapFillState:{view_bucket:bucket,last_run:outcome?{outcome_status:outcome}:null,actionable_missing_episodes:[2],pushed_episodes:[]}};
}
const all = [item('未搜','todo'),item('无源','no_resource','zero_hits'),item('故障','todo','search_error'),item('中止','todo','aborted_unstarted')];
const pending = item('在途','pushed_pending','accepted');pending.actionType='inspect_gap_detail';pending.gapFillState.pushed_episodes=[2];
const partial=item('部分','todo','accepted');partial.gapFillState.pushed_episodes=[1];all.push(partial);
ctx.taskList=[{id:'t',status:'completed',counts:{total:6},categories:{download:all,download_pushed:[pending]}}];
assert.equal(ctx.getGapFillLists(ctx.taskList[0]).todo.length,4);
assert.equal(ctx.getGapFillLists(ctx.taskList[0]).no_resource.length,1);
const countLabel = ctx.gapFillCountLabel(5,1,1);
const countText = countLabel.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();
assert.equal(countText, '4 季待搜索 1 季无合格资源 1 季待回执');
assert(ctx.gapFillCountLabel(5,1,null).includes('待加载'));
for(const [tab,filter,want,absent] of [
 ['todo','all','作品未搜','作品无源'],['todo','unsearched','作品中止','作品故障'],
 ['todo','error','作品故障','作品未搜'],['todo','partial','作品部分','作品故障'],
 ['no_resource','all','作品无源','作品故障'],['pushed_pending','all','作品在途','作品无源']]) {
 ctx.currentGapView=tab;ctx.currentGapSubFilter=filter;errors=[];ctx.renderEpisodesList();
 assert.equal(errors.length,0,JSON.stringify(errors));
 assert(node('episodeItemContainer').innerHTML.includes(want),tab+':'+filter);
 assert(!node('episodeItemContainer').innerHTML.includes(absent));
 if(tab==='no_resource') {
   assert(node('batchActionContainer').innerHTML.includes('batchDiscardButton'));
   assert(node('episodeItemContainer').innerHTML.includes('删除源端（本季）'));
 } else if(tab==='pushed_pending') assert(!node('episodeItemContainer').innerHTML.includes('openDeleteSourceSeasonModal'));
}
ctx.currentGapView='todo';
ctx.handleGapFillTabKeydown({key:'ArrowRight',preventDefault(){}});assert.equal(ctx.currentGapView,'no_resource');
ctx.handleGapFillTabKeydown({key:'ArrowRight',preventDefault(){}});assert.equal(ctx.currentGapView,'pushed_pending');
const data={preview_token:'fixture-token',message:'可执行1季',items:[{title:'<img>',season_record_id:'无源',season_path:'/fixture/Season 1',
  file_stats:{video_count:1,sub_count:0,other_count:0,total_size_display:'1 GiB'},files:[{path:'<script>',size:1}]}],blocked:[]};
assert(!ctx.renderBatchDiscardPreview(data).includes('<script>'));
assert(!ctx.renderBatchDiscardPreview(data).includes('<img>'));

(async()=>{
 ctx.currentGapView='no_resource';checked=[{value:'无源'}];const calls=[];
 ctx.api=async (url,opts)=>{calls.push([url,opts]);return url.endsWith('/preview')?data:{ok:true,completed:1,failed:0,message:'删除完成1季',results:[]};};
 await ctx.openBatchDiscardPreview();
 assert.equal(calls.length,1);assert(calls[0][0].endsWith('/preview'));
 assert.equal(node('confirmBatchDiscardBtn').disabled,true);
 await ctx.executeBatchDiscard();assert.equal(calls.length,1,'unchecked confirmation must not execute');
 node('batchDiscardConsent').checked=true;ctx.updateBatchDiscardConsent();assert.equal(node('confirmBatchDiscardBtn').disabled,false);
 ctx.loadTasks=async()=>{throw new Error('fixture refresh failed');};
 await ctx.executeBatchDiscard();
 assert.equal(calls.length,2);assert.equal(JSON.parse(calls[1][1].body).confirm_abandon,true);
 assert(node('batchDiscardBody').innerHTML.includes('以上执行回执仍有效'));
 assert.equal(node('confirmBatchDiscardBtn').disabled,true);
 await ctx.executeBatchDiscard();assert.equal(calls.length,2,'completed preview cannot be resent');
 ctx.api=async(url,opts)=>{assert.equal(url,'/api/system/version');assert.equal(opts.cache,'no-store');return {version:'2.5.56'};};
 await ctx.checkAppVersion();assert.equal(node('appVersionBadge').textContent,'v2.5.56');
 ctx.api=async()=>({version:'2.5.57'});await ctx.checkAppVersion();assert(node('appVersionBadge').textContent.includes('请刷新'));
 assert(!html.includes('>v2.5.51</span>'));
 const reviewing=item('复核中','pushed_pending');
 reviewing.actionType='inspect_gap_detail';reviewing.statusText='已补齐，复核待完成';
 reviewing.gapFillState.review_pending=true;
 reviewing.gapFillState.post_download_review={state:'pending',reason:'目录变化 <img>'};
 reviewing.gapFillState.actionable_missing_episodes=[];reviewing.gapFillState.pushed_episodes=[];
 ctx.taskList=[{id:'t',status:'completed',counts:{total:1},categories:{download:[],download_pushed:[reviewing]}}];
 ctx.currentGapView='pushed_pending';ctx.currentCategory='download';errors=[];ctx.renderEpisodesList();
 assert.equal(errors.length,0,JSON.stringify(errors));
 const pendingHtml=node('episodeItemContainer').innerHTML;
 assert(pendingHtml.includes('补缺后待复核'));
 assert(pendingHtml.includes('&lt;img&gt;'));
 assert(!pendingHtml.includes('openDeleteSourceSeasonModal'));
 assert(!pendingHtml.includes('batchDiscardButton'));
 console.log('PASS: three-tab rendering, all filters, counts, keyboard navigation, per-season delete, batch preview/consent/receipt, refresh failure and version consistency');
})().catch(e=>{console.error(e);process.exitCode=1;});
