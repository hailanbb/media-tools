const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const C=require('../season-inspect.user.js');
const source=fs.readFileSync(require('node:path').join(__dirname,'../season-inspect.user.js'),'utf8');
const show={title:'测试剧',year:'2025',tmdb:'123'};
const series={id:123,name:'测试剧',status:'Returning Series',seasons:[{season_number:1,episode_count:2}]};
const episodes=[{season_number:1,episode_number:1,air_date:'2025-01-01',episode_type:'standard'},{season_number:1,episode_number:2,air_date:'2025-01-02',episode_type:'finale'}];
const parsed=(s=series,e=episodes)=>C.parseTmdbSeason(s,{season_number:1,episodes:e},show,1,'2026-10-21T01:00:00.000Z');
const files=[1,2].map(n=>({name:`测试剧 - S01E0${n} - 0${n}.mkv`,directory:false}));
const douban={source:'douban',total:2,url:'https://movie.douban.com/subject/123/',title:'测试剧 (2025)'};
function row(audit,extra={}){const r={...audit,...extra};return {...r,status:C.resultStatus(r)};}
function scanAdapter({list,query,review=async()=>douban}) {
  const code=source.slice(source.indexOf('  function blankResult('),source.indexOf('  async function exclusive('));
  const context={...C,job:{category:'日本剧集'},now:()=>new Date().toISOString(),checkpoint(){},message(){},
    listing:list,querySources:query,queryDoubanRemote:review,supplementOptions:null,MEDIA:/\.(mkv|mp4|strm)$/i,Paused:class extends Error{}};
  vm.runInNewContext(code+'\nthis.scan=scanShow;',context);return context.scan;
}
test('续订电视剧已经播完的第一季可绿；今天或将来播出的当季仅标蓝',()=>{
  assert.equal(parsed().broadcast,'已播完');
  assert.equal(C.resultColor(row(C.auditSource(files,1,parsed()))).fill,'C6EFCE');
  const ongoing=parsed(series,[episodes[0],{...episodes[1],air_date:'2026-10-21'}]);
  assert.equal(ongoing.broadcast,'连载中');
  const audit=C.auditSource(files.slice(0,1),1,ongoing);
  assert.equal(audit.integrity,'已播齐全');assert.deepEqual(audit.missing,[]);assert.deepEqual(audit.uncollected,[2]);
  assert.equal(C.resultColor(row(audit)).fill,'BDD7EE');
});
test('缺已播集时仍用蓝色表示播出状态，但缺集列和本地判定不会消失',()=>{
  const db=parsed(series,[episodes[0],{...episodes[1],air_date:'2026-10-22'}]);
  const audit=C.auditSource([],1,db);
  assert.deepEqual(audit.missing,[1]);assert.equal(audit.integrity,'缺集');
  assert.equal(audit.airedIntegrity,'已播缺集');assert.equal(C.resultColor(row(audit)).fill,'BDD7EE');
});
test('未播和没有本季状态证据不能误标蓝或绿；豆瓣数量不证明播完',()=>{
  const upcoming=parsed(series,episodes.map(e=>({...e,air_date:'2026-11-01'})));
  assert.equal(upcoming.broadcast,'待播');assert.equal(C.resultColor(row(C.auditSource(files,1,upcoming))),null);
  const unknown=parsed(series,episodes.map(e=>({...e,episode_type:'standard'})));
  assert.equal(unknown.broadcast,'未确认');assert.equal(C.resultColor(row(C.auditSource(files,1,unknown))),null);
  assert.equal(C.resultColor(row(C.auditSource(files,1,douban))),null);
  assert.equal(C.parseDoubanBroadcast('集数: 2\n首播: 2000-01-01\n剧情简介: 已完结').broadcast,'未确认');
  assert.equal(C.parseDoubanBroadcast('集数: 2\n播出状态: 已完结\n').broadcast,'已播完');
});
test('解析午夜日期使用北京时间，播期未知也不能标绿',()=>{
  const db=C.parseTmdbSeason({...series,status:'Ended'},{season_number:1,episodes:[episodes[0],{...episodes[1],air_date:'2026-10-20'}]},show,1,'2026-10-20T16:30:00.000Z');
  assert.equal(db.broadcast,'已播完');
  const undated=parsed({...series,status:'Ended'},[episodes[0],{...episodes[1],air_date:null}]);
  assert.equal(undated.broadcast,'未确认');assert.equal(C.resultColor(row(C.auditSource(files,1,undated))),null);
});
test('重复版本不能靠视频数量判齐全；重复集号单列',()=>{
  const audit=C.auditSource([...files,{name:'S01E01.other.mkv'}],1,parsed());
  assert.equal(audit.current,2);assert.equal(audit.videoCount,3);assert.deepEqual(audit.duplicates,[1]);
  assert.equal(C.resultColor(row(audit)),null);
});
test('已完结且本地齐全不额外请求豆瓣；状态未知或连载需要复核',async()=>{
  await C.reviewIncomplete(files,1,parsed(),async()=>assert.fail('不应查询'));
  for(const db of [parsed(series,episodes.map(e=>({...e,episode_type:'standard'}))),parsed(series,[episodes[0],{...episodes[1],air_date:'2026-10-22'}])]){
    let called=0;const checked=await C.reviewIncomplete(files,1,db,async()=>{called++;return douban;});
    assert.equal(called,1);assert.equal(checked.db.total,2);assert.equal(checked.db.broadcast,db.broadcast);
  }
});
test('TMDB 已确认播完时空季不额外查豆瓣，保留 0/0 和来源数量',async()=>{
  let called=0;
  const result=await C.reviewIncomplete([],1,parsed(),async()=>{called++;return douban;});
  assert.equal(called,0);assert.equal(result.audit.videoCount,0);assert.equal(result.audit.current,0);assert.equal(result.db.total,2);
  assert.equal(result.audit.integrity,'缺集');
});
test('TMDB 失败后空季也可取豆瓣集数；无效来源和暂停不伪装成功',async()=>{
  const db=await C.preferTmdb(async()=>{throw Error('404');},async()=>douban);
  assert.equal(db.total,2);assert.equal(db.fallbackReason,'404');
  const bad=await C.preferTmdb(async()=>{throw Error('404');},async()=>({...douban,error:'详情季号不匹配'}));
  assert.equal(bad.total,null);
  await assert.rejects(()=>C.preferTmdb(async()=>{const e=Error('暂停');e.name='Paused';throw e;},async()=>assert.fail()),/暂停/);
});
test('复核失败保留 TMDB 集数；数量变化和播出冲突不能沿用播完状态',async()=>{
  const failed=await C.reviewIncomplete(files.slice(0,1),1,{...parsed(),completionConfirmed:false},async()=>({error:'无结果'}));
  assert.equal(failed.db.total,2);assert.equal(failed.db.source,'tmdb');assert.equal(failed.audit.integrity,'待核实');
  const different=await C.reviewIncomplete(files.slice(0,1),1,{...parsed(),completionConfirmed:false},async()=>({...douban,total:1}));
  assert.equal(different.db.broadcast,'未确认');assert.equal(different.audit.integrity,'齐全');assert.equal(C.resultColor(row(different.audit)),null);
  const conflict=await C.reviewIncomplete([],1,{...parsed(),completionConfirmed:false},async()=>({...douban,broadcast:'连载中'}));
  assert.equal(conflict.db.broadcast,'未确认');
});
test('排序严格使用有效修改时间，从旧到新，时间相同按季号',()=>{
  const rows=[{name:'Season 1',modified:'2026-01-02 00:00'},{name:'Season 10',modified:'2026-01-01 00:00:00'},{name:'Season 2',modified:'2026-01-01 00:00'}];
  assert.deepEqual(C.sortModified(rows).map(r=>r.name),['Season 2','Season 10','Season 1']);
  for(const modified of ['昨天','2026-02-30 00:00','2026-01-01 24:00',''])assert.throws(()=>C.sortModified([{modified}]));
  assert.equal(rows[0].name,'Season 1');
});
test('真实扫描按季修改时间，保留稳定行标识，完全不查询归档目录',async()=>{
  const showPath=C.SOURCE+'/日本剧集/测试剧 (2025) [tmdbid=123]';
  const visits=[];
  const scan=scanAdapter({list:async p=>{
    visits.push(p);
    if(p===showPath)return [{name:'Season 1',path:p+'/Season 1',directory:true,modified:'2026-01-02 00:00'},{name:'Season 2',path:p+'/Season 2',directory:true,modified:'2026-01-01 00:00'}];
    return [];
  },query:async()=>parsed()});
  const result=await scan({name:'测试剧 (2025) [tmdbid=123]',path:showPath});
  assert.deepEqual(Array.from(result,r=>r.season),[2,1]);assert.equal(visits.length,3);
  for(const r of result){assert.equal(r.total,2);assert.equal(r.current,0);assert.equal(r.videoCount,0);assert.equal(r.reportVersion,'tv-workflow/1');assert.equal(r.tmdbId,'123');assert.equal(JSON.parse(r.recordId)[2],r.path);}
});
test('豆瓣限速待补查时扫描后续季，保留 TMDB 数量和空目录 0/0，不标绿',async()=>{
  let calls=0;
  const path=C.SOURCE+'/日本剧集/测试剧 (2025)',primary={...parsed(),completionConfirmed:false};
  const scan=scanAdapter({list:async p=>p===path?[1,2].map(n=>({name:'Season '+n,path:p+'/Season '+n,directory:true,modified:'2026-01-01 00:00'})):[],query:async()=>primary,
    review:async()=>{calls++;return {deferred:true,error:'等待额度',retryAt:Date.now()+45000};}});
  const result=await scan({name:'测试剧 (2025)',path});assert.equal(result.length,2);assert.equal(calls,2);
  for(const r of result){assert.equal(r.pendingDouban,true);assert.equal(r.videoCount,0);assert.equal(r.current,0);assert.equal(r.total,2);assert.equal(r.status,'豆瓣待补查');assert.equal(C.resultColor(r),null);}
});
test('未识别的季不猜季号，读取失败不伪造零视频；来源异常保留扫描数量',async()=>{
  const showRow={name:'测试剧',path:'/show'};
  const empty=scanAdapter({list:async()=>[],query:async()=>assert.fail()});
  const [none]=await empty(showRow);assert.equal(none.videoCount,0);assert.equal(none.total,null);assert.equal(none.season,'');
  for(const failListing of [true,false]) {
    const scan=scanAdapter({list:async p=>{
      if(p==='/show')return [{name:'Season 1',path:'/show/Season 1',directory:true,modified:'2026-01-01 00:00'}];
      if(failListing)throw Error('读取失败');return files;
    },query:async()=>{throw Error('来源失败');}});
    const [r]=await scan(showRow);assert.equal(r.videoCount,failListing?null:2);assert.equal(r.status,'读取失败');assert.equal(C.resultColor(r),null);
  }
});
test('Excel 只给剧名格上色，协议字段和中文超链接保真，输出可解析字节',()=>{
  const rows=[row(C.auditSource(files,1,parsed())),row(C.auditSource(files,1,parsed(series,[episodes[0],{...episodes[1],air_date:'2026-10-22'}]))),row(C.auditSource(files,1,douban))]
    .map((r,i)=>({...r,showName:i?'测试剧':'=中文',season:1,total:2,path:C.SOURCE+'/日本剧集/测试剧/Season 1',tmdbId:'123',recordId:'行'+i,reportVersion:C.REPORT_VERSION,stage:'初检'}));
  const book=C.resultWorkbook(rows),sheet=book.Sheets['剧集检查'];
  assert.equal(sheet.B2.s.fill.fgColor.rgb,'C6EFCE');assert.equal(sheet.B3.s.fill.fgColor.rgb,'BDD7EE');assert.equal(sheet.B4.s.fill,undefined);
  assert.equal(sheet.A2.s.fill,undefined);assert.equal(sheet.B2.t,'s');assert.equal(sheet.B2.f,undefined);
  assert.equal(new URL(sheet.D2.l.Target).searchParams.get('path'),rows[0].path);
  assert.equal(sheet['!autofilter'].ref,'A1:AG4');
  const bytes=Buffer.from(C.resultXlsx(rows));assert.equal(bytes.subarray(0,2).toString(),'PK');
  const bundle=fs.readFileSync(require('node:path').join(__dirname,'../vendor/xlsx-js-style/package/dist/xlsx.bundle.js'),'utf8');
  const X=new Function('const exports={};const module=undefined,require=undefined,window=undefined,define=undefined;'+bundle+';return exports;')();
  const reloaded=X.read(bytes,{type:'buffer',cellStyles:true}).Sheets['剧集检查'];
  assert.equal(reloaded.B2.v,'=中文');assert.equal(reloaded.B2.f,undefined);
  assert.equal(reloaded.B2.s.fgColor.rgb,'C6EFCE');assert.equal(reloaded.B3.s.fgColor.rgb,'BDD7EE');
  assert.equal(reloaded.AD2.v,'行0');assert.equal(reloaded.AE2.v,'tv-workflow/1');
  assert.ok(C.resultCsv(rows).includes('tv-workflow/1'));
});

test('实际分类任务暂停后只重试当前剧，保留已完成结果且不会读取目标目录',async()=>{
  let paused=true;
  const visits=[],calls=[];
  const items=[{name:'较新',path:'/new',modified:'2026-01-02 00:00'},{name:'较旧',path:'/old',modified:'2026-01-01 00:00'}].map(r=>({...r,directory:true}));
  class Paused extends Error{}
  const context={...C,job:{category:'日本剧集',index:0,results:[],queue:null},tmdbSeriesCache:new Map(),
    save(){},message(){},checkpoint(){},now:()=>new Date().toISOString(),Paused,navigate:async()=>{},recordResult:r=>context.job.results.push(r),
    listing:async p=>{visits.push(p);return items;},scanShow:async r=>{
      calls.push(r.name);if(r.name==='较新'&&paused)throw new Paused('等待人工验证');return [{showName:r.name,status:'齐全'}];
    }};
  const code=source.slice(source.indexOf('  async function runJob()'),source.indexOf('  function el('));
  vm.runInNewContext('let tmdbDisabledReason="";'+code+'\nthis.run=runJob;',context);
  await assert.rejects(()=>context.run(),/等待人工验证/);assert.equal(context.job.index,1);assert.equal(context.job.results.length,1);
  paused=false;await context.run();
  assert.deepEqual(calls,['较旧','较新','较新']);assert.equal(context.job.results.length,2);assert.equal(context.job.status,'已完成');
  assert.deepEqual(visits,[C.SOURCE+'/日本剧集']);
});

test('初检安装文件严格 UTF-8、无云端写请求；独立元数据和构建无远程代码',()=>{
  const bytes=fs.readFileSync(require('node:path').join(__dirname,'../season-inspect.user.js'));
  assert.notEqual(bytes.subarray(0,3).toString('hex'),'efbbbf');new TextDecoder('utf-8',{fatal:true}).decode(bytes);
  assert.match(source,/\/\/ @namespace\s+local\.tv-season-inspect/);assert.match(source,/\/\/ @version\s+1\.2\.0/);
  assert.ok(!/@require|GM_cookie|\bfetch\s*\(/.test(source));
  assert.deepEqual([...source.matchAll(/^\/\/ @connect\s+(.+)$/gm)].map(m=>m[1]),['api.themoviedb.org']);
  const own=fs.readFileSync(require('node:path').join(__dirname,'../src/season-inspect.js'),'utf8');
  assert.ok(!/method:\s*['"](?:POST|PUT|PATCH|DELETE)|querySelector\([^\n]*(?:移动|删除|重命名)/i.test(own));
});
