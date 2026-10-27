const {test}=require('node:test'),assert=require('node:assert/strict'),C=require('../season-download.user.js');
const base=()=>C.taskFromRow({name:'测试剧 (2026) [tmdbid=123]',season:1,key:'broadcast',data:{'来源登记集数':100,'缺失集号':'43-100'}});
const media={type:'电视剧',tmdb_id:123,status:'Returning Series'};
const ep=(n,date,season=1)=>({episode_number:n,season_number:season,air_date:date});
const contexts=()=>[{media_info:media,meta_info:{begin_season:1,season_list:[1],episode_list:[1,3,42]},torrent_info:{title:'Test.S01E42',seeders:1,enclosure:'https://example.invalid',size:100}}];
test('42/100仅是疑似；同一季已播及未来集目才确认连载，下一季不能作为依据',()=>{
 const task=base(),assess=(e)=>C.Broadcast.assess(task,[1,3,42],e,'2026-10-22');
 const unknown=assess({media,episodes:[ep(42,'2026-10-21')]});assert.equal(unknown.confirmed,false);assert.equal(unknown.status,'疑似连载，待核实');assert.deepEqual(unknown.coverage,[1,3,42]);
 const yes=assess({media,episodes:[ep(42,'2026-10-21'),ep(43,'2026-10-23')]});assert.equal(yes.status,'连载中');assert.equal(yes.confirmed,true);assert.deepEqual(yes.exclude,[43]);
 const other=assess({media:{...media,next_episode_to_air:ep(1,'2026-10-23',2)},episodes:[ep(42,'2026-10-21')]});assert.equal(other.confirmed,false);
});
test('来源已完结且完整集目均已播，只判资源不全；待播、状态矛盾和季号错误不标蓝',()=>{
 const task=base(),run=e=>C.Broadcast.assess(task,[42],e,'2026-10-22');
 const ended=run({media:{...media,status:'Ended'},episodes:Array.from({length:100},(_,i)=>ep(i+1,'2026-10-01'))});assert.equal(ended.status,'已播完，资源不全');
 for(const e of [{media,episodes:[ep(1,'2026-10-23')]},{media:{...media,status:'Ended'},episodes:[ep(42,'2026-10-21'),ep(43,'2026-10-23')]},{media,episodes:[ep(42,'2026-10-21',2)]}])assert.notEqual(run(e).status,'连载中');
});
test('复核失败保留明确的原报告结论，并写明本轮未刷新来源',()=>{
 const task=base();task.data['播出状态']='连载中';assert.equal(C.Broadcast.assess(task,[42],{error:'接口不可用'}).status,'连载中');task.data['播出状态']='已播完';assert.equal(C.Broadcast.assess(task,[42],{error:'接口不可用'}).status,'已播完，资源不全');
});
async function process({task=base(),search=contexts(),evidence,active=[]}={}){let checked=0,submitted=0;const r=await C.processTask(task,{search:async()=>search,active:async()=>active,broadcast:async()=>{checked++;return evidence;},submit:async()=>{submitted++;return {success:true,data:{download_id:'new'}};},load:()=>[],save(){},checkpoint(){}});return {r,checked,submitted};}
test('无合格下载但来源确认连载标蓝；仅42/100不标蓝',async()=>{
 const yes=await process({evidence:{media,episodes:[ep(42,'2020-01-01'),ep(43,'2099-01-01')]}});assert.equal(yes.checked,1);assert.equal(yes.submitted,0);assert.equal(yes.r.color,'blue');assert.equal(yes.r.resourceAudit.maximum,42);
 const no=await process({evidence:{media,episodes:[ep(42,'2020-01-01')]}});assert.notEqual(no.r.color,'blue');assert.equal(no.r.resourceAudit.confirmed,false);
});
test('已经提交的任务优先黄色；已知未来集被排除，不按资源站最高号填连续集数',async()=>{
 const task=base();task.needed=[42,43];const search=contexts();search[0].meta_info.episode_list=[42];search[0].torrent_info.seeders=5;
 const out=await process({task,search,evidence:{media,episodes:[ep(42,'2020-01-01'),ep(43,'2099-01-01')]}});assert.equal(out.submitted,1);assert.equal(out.r.color,'yellow');assert.equal(out.r.resourceAudit.status,'连载中');assert.deepEqual(out.r.covered,[42]);assert.deepEqual(out.r.remaining,[]);
});
test('错误媒体和其他季资源不计覆盖；来源身份不一致不得标蓝',async()=>{
 const search=contexts();search.push({...search[0],media_info:{type:'电视剧',tmdb_id:999},meta_info:{begin_season:1,episode_list:[99]}});
 const out=await process({search,evidence:{media:{...media,tmdb_id:999},episodes:[ep(42,'2020-01-01'),ep(43,'2099-01-01')]}});assert.equal(out.r.resourceAudit.maximum,42);assert.notEqual(out.r.color,'blue');assert.match(out.r.resourceAudit.basis,/身份/);
});
test('MoviePilot播出查询使用媒体详情与精确季接口，读取失败不发送额外写请求',async()=>{
 const calls=[],api={defaults:{baseURL:'/api/v1/'},get:async(p,o)=>{calls.push([p,o.params]);return p.startsWith('media/')?media:[ep(42,'2020-01-01')];},post:()=>assert.fail('不得提交')};
 const result=await C.createAdapter(api,'https://moviepilot.example').broadcast(base());assert.equal(result.episodes.length,1);assert.equal(calls[0][0],'media/tmdb%3A123');assert.equal(calls[1][0],'tmdb/123/1');assert.equal(calls[0][1].type_name,'电视剧');
});
