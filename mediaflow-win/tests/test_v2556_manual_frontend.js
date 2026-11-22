const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const html = fs.readFileSync('frontend/index.html', 'utf8');
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {value:'', checked:false, disabled:false, innerHTML:'', textContent:'', isConnected:true});
  return nodes.get(id);
}
let alerts = [], calls = [], allow = true, previewFailure = false, refreshFailure = false;
let plan = {has_file_changes:true, summary_text:'修正 3 个文件', new_season_path:'/fixture/new',
  correction_fingerprint:'fixture-proof', file_changes:[{old_name:'Old',new_name:'New'}]};
const controls = ['mcIdentityCorrect','mcTargetTmdbId','mcTargetSeason','mcTargetTitle','mcDoubanUrl','mcTotalEpisodes','mcBroadcastStatus','mcNote'].map(node);
node('mcIdentityCorrect').checked = true;
node('mcTotalEpisodes').value = '3';
node('mcBroadcastStatus').value = 'finished';
const ctx = {manualReviewBusy:false, currentTaskId:'t', currentManualConfirmRecId:'r', manualConfirmPreviewSequence:0,
  anomalyReviewTaskId:null, taskList:[{id:'t'}], console,
  document:{getElementById:node,querySelectorAll:()=>controls},
  findItemByRecordId:()=>({pathA:'/fixture/old', raw:{recognized_episodes:[1,2,3]}}),
  escapeHtml:s=>String(s??'').replaceAll('<','&lt;'), alert:s=>alerts.push(s), confirm:()=>allow,
  setTimeout:()=>1, clearTimeout(){}, closeModal(){}, openNextAnomalyReview(){},
  loadTasks:async()=>{if(refreshFailure) throw new Error('refresh unavailable');}, selectTask:async()=>{},
  api:async(url, opts)=>{
    calls.push([url,opts]);
    if (url.endsWith('/preview')) {
      if(previewFailure) throw new Error('directory unavailable');
      return {ok:true, correction_plan:plan, predicted_category_label:'齐全可归档', predicted_status:'archive_ready'};
    }
    return {ok:true,message:'已保存至齐全可归档'};
  },
};
vm.createContext(ctx);
for (const name of ['updateManualConfirmPreview','submitManualConfirmation']) {
  const source = html.match(new RegExp('async function '+name+'\\([^]*?\\n    \\}'));
  assert(source,name);vm.runInContext(source[0],ctx);
}
function reset(){calls=[];alerts=[];node('mcSubmitBtn').disabled=false;}
(async()=>{
  await ctx.submitManualConfirmation();
  assert.equal(calls.length,2);
  let body=JSON.parse(calls[1][1].body);
  assert.equal(body.correction_fingerprint,'fixture-proof');
  assert.equal(body.apply_file_changes,true);
  assert(node('mcPreviewBox').innerHTML.includes('Old → New'));
  assert(node('mcFileChangeHint').textContent.includes('修正 3 个文件'));
  assert(controls.every(x=>!x.disabled));
  reset();allow=false;await ctx.submitManualConfirmation();assert.equal(calls.length,1);
  reset();allow=true;previewFailure=true;await ctx.submitManualConfirmation();
  assert.equal(calls.length,1);assert(alerts[0].includes('没有提交保存'));
  assert(node('mcFileChangeHint').textContent.includes('预览失败'));
  reset();previewFailure=false;refreshFailure=true;await ctx.submitManualConfirmation();
  assert.equal(calls.length,2);assert(alerts[0].includes('已保存至齐全可归档'));
  assert(alerts[0].includes('列表刷新失败'));assert(alerts[0].includes('不要重复保存'));
  reset();refreshFailure=false;plan={has_file_changes:false};await ctx.submitManualConfirmation();
  body=JSON.parse(calls[1][1].body);assert.equal(body.apply_file_changes,false);
  reset();ctx.api=async(url)=>{
    if(url.endsWith('/preview')) return {ok:true,correction_plan:plan};
    throw new Error('connection lost');
  };
  await ctx.submitManualConfirmation();assert(alerts[0].includes('未取得保存回执'));
  assert(alerts[0].includes('不要直接重复提交'));
  console.log('PASS: accurate file preview, explicit consent, preview failure blocks save, fingerprint, metadata-only save, receipt vs refresh failure, uncertain response');
})().catch(e=>{console.error(e);process.exitCode=1;});
