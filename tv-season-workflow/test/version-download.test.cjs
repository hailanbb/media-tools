// 同版本补缺集成测试：真实分组、候选排序与提交状态机；所有下载均为内存模拟。
const {test}=require('node:test'),assert=require('node:assert/strict'),C=require('../season-download.user.js'),I=require('../season-inspect.user.js');
const dir=I.SOURCE+'/日本剧集/测试剧 (2025) [tmdbid=123]/Season 1';
function files(group,eps,modified='2026-10-20 10:00'){return eps.map(n=>{const name='Test.S01E'+String(n).padStart(2,'0')+'.1080p.WEB-DL.x265-'+group+'.mkv';return {name,path:dir+'/'+name,size:'1 GB',modified,directory:false};});}
function task(rows,extra={}){return C.taskFromRow({name:'测试剧 (2025) [tmdbid=123]',season:1,path:dir,key:'mixed',data:{'来源登记集数':4,'缺失集号':'无','版本文件清单':JSON.stringify({schema:1,path:dir,files:rows}),...extra}});}
function resource(group,eps,seeds=5){return {media_info:{type:'电视剧',tmdb_id:123},meta_info:{begin_season:1,season_list:[1],episode_list:eps},torrent_info:{title:'Test.S01.1080p.WEB-DL.x265-'+group,enclosure:'https://example.invalid/torrent',seeders:seeds,size:100,site:1,site_name:'测试站'}};}
async function run(t,contexts,active=[]){const calls=[],saved=[];const result=await C.processTask(t,{search:async()=>contexts,active:async()=>active,submit:async c=>{calls.push(c);return {success:true,data:{download_id:'id'+calls.length}};},load:()=>[],save:(_k,r)=>saved.push(structuredClone(r)),checkpoint(){}});return {result,calls,saved};}
test('混版合计齐全仍按独立版本缺集补齐；可补齐版本中取云盘修改时间最新',async()=>{
 const t=task([...files('A',[1,2]),...files('B',[3,4],'2026-10-21 10:00')]);const r=await run(t,[resource('A',[3,4]),resource('B',[1,2])]);assert.equal(r.calls.length,1);assert.match(r.calls[0].torrent_info.title,/-B$/);assert.match(r.result.note,/拟保留版本/);assert.match(r.result.records[0].selectionReason,/云盘最新修改时间：2026-10-21/);assert.deepEqual(r.result.remaining,[]);
});
test('可完整补齐优先于更新但只能部分补齐的版本；不选择无关发布组',async()=>{
 const t=task([...files('A',[1,2]),...files('B',[3],'2026-10-21 10:00')]);const r=await run(t,[resource('A',[3,4]),resource('B',[1]),resource('C',[1,2,3,4],100)]);assert.equal(r.calls.length,1);assert.match(r.calls[0].torrent_info.title,/-A$/);
});
test('已有一个独立完整版本不补下载，多个完整版本交人工',async()=>{
 const one=await run(task([...files('A',[1,2,3,4]),...files('B',[1])]),[]);assert.equal(one.calls.length,0);assert.match(one.result.state,/已有完整版本/);assert.equal(one.result.color,'yellow');
 const many=await run(task([...files('A',[1,2,3,4]),...files('B',[1,2,3,4])]),[]);assert.equal(many.calls.length,0);assert.match(many.result.note,/多个完整版本/);
});
test('只提供整季包且会重复拟保留已有集时不自动替换，缺少同版本资源不换组',async()=>{
 const r=await run(task([...files('A',[1,2]),...files('B',[3,4])]),[resource('A',[1,2,3,4]),resource('C',[1,2,3,4])]);assert.equal(r.calls.length,0);assert.match(r.result.note,/没有与现有版本/);
});
test('可补齐版本修改时间相同交人工选择，缺少发布组也不猜',async()=>{
 const rows=[...files('A',[1,2]),...files('B',[3,4])];const r=await run(task(rows),[resource('A',[3,4]),resource('B',[1,2])]);assert.equal(r.calls.length,0);assert.match(r.result.note,/时间相同/);
 const plain=rows.map(f=>({...f,name:f.name.replace(/-[AB]/,''),path:f.path.replace(/-[AB]/,'')}));plain[0].name='01.mkv';plain[0].path=dir+'/01.mkv';const bad=await run(task(plain),[]);assert.match(bad.result.note,/标识|版本/);
});
test('混版已有在下载的本季任务先等待复核，不把其他版本覆盖当作拟保留版已齐全',async()=>{
 const r=await run(task([...files('A',[1,2]),...files('B',[3,4])]),[resource('B',[1,2])],[{hash:'old',media:{type:'电视剧',tmdb_id:123},season_episode:'S01E01',title:'其他版本'}]);assert.equal(r.calls.length,0);assert.match(r.result.state,/版本待复核/);
});
test('旧报告明确重复集号但无版本清单时提示重检，不盲目下载',async()=>{
 const t=task([],{版本文件清单:'',重复集号:'2',缺失集号:'4'}),r=await run(t,[resource('A',[4])]);assert.equal(r.calls.length,0);assert.match(r.result.note,/缺少版本清单/);
});
test('第三脚本复核记录可提供版本文件，优先于旧初检快照',async()=>{
 const rows=[...files('A',[1,2]),...files('B',[3,4],'2026-10-21 10:00')];const t=task(files('A',[1,2,3,4]),{复核清理记录:JSON.stringify({groups:[{files:rows}],issues:[]})});const r=await run(t,[resource('B',[1,2])]);assert.equal(r.calls.length,1);assert.match(r.result.note,/拟保留版本/);
});
test('文件清单路径越界、时间无效或未知集号不能产生补缺计划',async()=>{
 for(const override of [{path:'/other'},{modified:'unknown'},{name:'unknown.mkv',path:dir+'/unknown.mkv'}]){const rows=[...files('A',[1,2]),...files('B',[3,4])];Object.assign(rows[0],override);const r=await run(task(rows),[resource('B',[1,2])]);assert.equal(r.calls.length,0);assert.equal(r.result.state,'版本待核实');}
});
test('初检Excel长版本清单分片并可重组，不覆盖已有列',()=>{
 const versionSnapshot=JSON.stringify({schema:1,path:dir,files:files('A',Array.from({length:400},(_,i)=>i+1))});assert.ok(versionSnapshot.length>30000);
 const s=I.resultWorkbook([{category:'日本剧集',showName:'测试剧',season:1,path:dir,versionSnapshot}]).Sheets['剧集检查'];const cols=Object.entries(s).filter(([k,c])=>/^[A-Z]+1$/.test(k)&&/^版本文件清单/.test(c.v)).map(([k,c])=>[c.v,s[k.replace(/1$/,'2')].v]);cols.sort((a,b)=>Number(a[0].replace('版本文件清单续','')||0)-Number(b[0].replace('版本文件清单续','')||0));const raw=cols.map(c=>c[1]).join('');assert.equal(raw,versionSnapshot);assert.ok(cols.every(c=>c[1].length<=30000));assert.equal(s.B1.v,'剧集目录');
});
