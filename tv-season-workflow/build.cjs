// 构建独立安装包：嵌入已保存的 OpenCC、Excel 库及各自业务模块，不依赖运行时 CDN。
// src 为可维护源码；请勿直接编辑生成的 *.user.js。第三方原始许可随包保留。
const fs = require('node:fs');
const path = require('node:path');
const root = __dirname;
const read = p => fs.readFileSync(path.join(root,p), 'utf8');
const marker = '/* build:opencc */ null';
const license = ['OpenCC-js 1.4.2 — https://github.com/nk2028/opencc-js',
  read('vendor/package/LICENSE'), read('vendor/package/THIRD_PARTY_LICENSES.md'), read('vendor/package/LICENSES/Apache-2.0.txt')].join('\n');
const bundle = read('vendor/package/dist/umd/t2cn.js');
const embedded = `(() => {\n/*\n${license.replace(/\*\//g,'* /')}\n*/\nconst exports = {}; const module = { exports };\n${bundle}\nreturn exports.Converter({from:'tw',to:'cn'});\n})()`;
const excelMarker='/* build:xlsx */ null';
const excelLicense=read('vendor/xlsx-js-style/package/LICENSE')+'\n'+read('vendor/xlsx-js-style/package/dist/LICENSE');
const excelBundle=read('vendor/xlsx-js-style/package/dist/xlsx.bundle.js').replace(/\/\/# sourceMappingURL=.*$/gm,'');
const excelEmbedded=`(() => {\n/* xlsx-js-style 1.2.0, https://github.com/gitbrent/xlsx-js-style\n${excelLicense.replace(/\*\//g,'* /')}\n*/\nconst exports={}; const module=undefined,require=undefined,window=undefined,define=undefined;\n${excelBundle}\nreturn exports;\n})()`;
// 第三阶段复用已经验证的文件名/豆瓣匹配逻辑；只截取明确的纯函数区段，不嵌入初检面板或 TMDB 查询。
const inspectSource=read('src/season-inspect.js');
function section(start,end) {
  if(inspectSource.split(start).length!==2||inspectSource.split(end).length!==2)throw Error('Shared source boundary changed: '+start);
  const a=inspectSource.indexOf(start),b=inspectSource.indexOf(end,a);if(b<a)throw Error('Invalid shared source boundary');return inspectSource.slice(a,b);
}
const cloudCore='(function createCloudCore(toSimplified) {\n'+[
  section('  const MEDIA =','  function auditEpisodes('),
  section('  function parseDoubanBroadcast(','  function parseTmdbSeason('),
  section('  function parseTotal(','  function csvCell('),
  section('  function modifiedTime(','  function sortModified(')
].join('\n')+'\nreturn {norm,parent,base,mapKey,showInfo,titleIdentity,candidateMatches,chooseCandidate,matchFailure,episodeFromName,validSubjectUrl,parseTotal,parseDoubanBroadcast,doubanAccessProblem,validateListing,createListingSettler,modifiedTime};\n})';
// 下载仅共享文本标准化，身份仍由媒体 ID / 完整名称和年份判断；不复用豆瓣的候选放宽规则。
const titleTextCore='(function createTitleText(toSimplified) {\n'+section('  const norm =','  const parent =')+section('  function cleanTitleText(','  function sameTitle(')+'\nreturn {norm,cleanTitleText};\n})';
const doubanWorker=section('  function blockedDouban()','  if (location.hostname !==')
  .replaceAll('tv_audit','tv_review').replace('releaseYears, matchNote, candidates:','releaseYears, aliases, japanese, matchNote, candidates:');
// 归档仅复用列表读取区段，明确限定两个允许的目录树，不携带删除函数。
const reviewClient=read('src/cloud-review-client.js');
const listingStart='  const sleep=ms=>',listingEnd='  // 标准词条读取逻辑';
if(reviewClient.split(listingStart).length!==2||reviewClient.split(listingEnd).length!==2)throw Error('Cloud listing boundary changed');
const cloudListing=reviewClient.slice(reviewClient.indexOf(listingStart),reviewClient.indexOf(listingEnd))
  .replace("return path.startsWith(SOURCE+'/')&&!path.split('/').some(s=>s==='.'||s==='..');","return [SOURCE,DEST].some(root=>path===root||path.startsWith(root+'/'))&&!path.split('/').some(s=>s==='.'||s==='..')&&!/[\\\\\\x00-\\x1f]/.test(path);")
  .replaceAll('停止清理','停止归档').replaceAll('数量或删除结果','数量或移动结果');
// 共享第三阶段 versionOf 的原函数，避免两套版本分组规则漂移；不嵌入删除状态机。
const cleanupSource=read('src/cleanup-core.js');
const versionFunction=cleanupSource.slice(cleanupSource.indexOf('  function versionOf('),cleanupSource.indexOf('  function sidecarOwners('));
if(!versionFunction.startsWith('  function versionOf('))throw Error('Version helper boundary changed');
const versionHelpers='(function(C){const stem=name=>name.replace(/\\.[^.]+$/,\'\');'+versionFunction+';return {versionOf};})';
for(const name of ['season-inspect','season-download','season-review','season-archive']) {
  const src=read(`src/${name}.js`);
  for(const required of [marker,excelMarker])if(src.split(required).length!==2)throw Error(`${name}: expected one ${required}`);
  let output=src.replace(marker,()=>embedded).replace(excelMarker,()=>excelEmbedded);
  // 可选模块只进入需要它的脚本，初检版不会因此带入下载或报告修改功能。
  for(const [marker,file] of [['/* build:panel-layout */ null','src/panel-layout.js'],['/* build:douban-policy */ null','src/douban-policy.js'],['/* build:douban-query */ null','src/douban-query.js']])if(output.includes(marker))output=output.replace(marker,()=>read(file));
  for(const [marker,file] of [['/* build:repair-state */ null','src/repair-state.js'],['/* build:transfer-repair */ null','src/transfer-repair.js'],['/* build:broadcast-audit */ null','src/broadcast-audit.js'],['/* build:download-core */ null','src/download-core.js'],['/* build:workflow-report */ null','src/workflow-report.js'],['/* build:review-stage-report */ null','src/review-stage-report.js'],['/* build:cleanup-core */ null','src/cleanup-core.js'],['/* build:cloud-client */ null','src/cloud-review-client.js'],['/* build:archive-core */ null','src/archive-core.js'],['/* build:archive-client */ null','src/cloud-archive-client.js']]) {
    if(output.includes(marker)) {
      if(output.split(marker).length!==2)throw Error(`Expected one ${marker}`);
      output=output.replace(marker,()=>read(file));
    }
  }
  output=output.replaceAll('/* build:repair-state */ null',()=>read('src/repair-state.js'));
  output=output.replace('/* build:version-helpers */ null',()=>versionHelpers).replace('/* build:version-download */ null',()=>read('src/version-download.js'));
  output=output.replace('/* build:cloud-core */ null',()=>cloudCore).replace('/* build:douban-worker */',()=>doubanWorker).replace('/* build:cloud-listing */',()=>cloudListing).replace('/* build:title-text */ null',()=>titleTextCore);
  fs.writeFileSync(path.join(root,`${name}.user.js`),output,'utf8');
  console.log(`Built ${name}.user.js with offline OpenCC-js and Excel support`);
}
