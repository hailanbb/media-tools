const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname,'../frontend/index.html'),'utf8');
for (const m of html.matchAll(/<script\b[^>]*>([^]*?)<\/script>/g)) if (m[1].trim()) new vm.Script(m[1]);
const ctx={escapeHtml:s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),escapeJsAttr:s=>s,cleanSeasonStatusNote:s=>s};
vm.createContext(ctx);
for(const name of ['renderRecommendedRepreview','groupRecommendedSkipped','renderRecommendedSkipped','readableLookupReason','renderDoubanLookup','seasonDecisionSummary']) {
 const match=html.match(new RegExp('function '+name+'\\([^]*?\\n    \\}'));
 assert(match,name);vm.runInContext(match[0],ctx);
}
const skipped=[{group_id:'g',title:'作品甲',reason_type:'file_conflict',reason:'文件季号不一致'},
 {group_id:'g',title:'作品甲 S01',reason_type:'dependency',reason:'等待目录整理'},
 {group_id:'h',title:'作品乙',reason_type:'read_error',reason:'目录读取失败'}];
assert.equal(ctx.groupRecommendedSkipped(skipped).length,2);
const preview=ctx.renderRecommendedSkipped(skipped);
assert(preview.includes('2 个独立事项'));assert(preview.includes('作品甲'));assert(preview.includes('定位作品目录与文件'));
const lookup=ctx.renderDoubanLookup({status:'failed',attempts:[
 {source:'moviepilot_media',subject_id:'1',reason:'详情缺少季集数，切换备用渠道',status:'unavailable'},
 {source:'douban_detail',subject_id:'1',reason:'ValueError',status:'failed'}]},null);
assert(!lookup.includes('ValueError'));assert(!lookup.includes('moviepilot_media'));
assert(lookup.includes('查询情况与诊断'));assert(lookup.includes('逐渠道诊断'));
assert(!/<details[^>]*\bopen\b/.test(lookup));
const unsafe=ctx.renderDoubanLookup({status:'failed',attempts:[{title:'<img src=x>',source:'douban_detail',reason:'<script>',status:'failed'}]},null);
assert(!unsafe.includes('<img'));assert(!unsafe.includes('<script>'));
for (const category of ['review','anomaly','download','archive','airing']) {
 const summary=ctx.seasonDecisionSummary({season:'S12',raw:{main_category:category,status_detail:'原始依据'}},{},{});
 assert(summary.length>0);assert(summary.length<100);
}
assert(ctx.seasonDecisionSummary({season:'S12'},{meta_status:'not_listed'},{}).includes('未收录 S12'));
console.log('v2.5.53 frontend: grouped blockers, dependency dedup, readable diagnostics, escaping, five-module summaries PASS');

const paused=ctx.renderRecommendedRepreview([{show_name:'<作品>',season_number:0,modified_videos:['/src/a.mkv','/src/a.mkv']},{show_name:'读取失败',error:'TimeoutError'}]);
assert(paused.includes('1 个视频信息与预览不一致'));
assert(paused.includes('目录读取未完成'));
assert(paused.includes('S00'));
assert(paused.includes('&lt;作品&gt;'));
assert(!/<details[^>]*\bopen\b/.test(paused));
assert(!paused.includes('尚未执行任何删除'));
assert(paused.includes('更新预览不会自动执行删除'));
console.log('Paused preview: collapsed differences, deduplication, read errors and escaping PASS');

const classified=ctx.renderRecommendedRepreview([{show_name:'示例',modified_videos:['/src/a.mkv'],modified_details:{'/src/a.mkv':['modified','size']}}]);
assert(classified.includes('差异：修改时间、大小'));
