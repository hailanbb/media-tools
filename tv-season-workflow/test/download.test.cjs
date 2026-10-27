// 所有下载测试使用内存适配器；不会向真实 MoviePilot 或下载器发请求。
const {test}=require('node:test');
const assert=require('node:assert/strict');
const C=require('../season-download.user.js');
function task(overrides={}) {
  return C.taskFromRow({name:'测试剧 (2025) [tmdbid=123]',season:1,key:'show:1',data:{'来源登记集数':4,'缺失集号':'2、4',...overrides}});
}
function torrent(title='Complete',eps=[1,2,3,4],seeds=5,overrides={}) {
  return {media_info:{type:'电视剧',tmdb_id:123,title:'测试剧',year:'2025'},meta_info:{begin_season:1,end_season:null,season_list:[1],episode_list:eps},
    torrent_info:{site:1,site_name:'测试站',title,enclosure:'https://tracker.invalid/?passkey=DO_NOT_EXPORT',size:100,seeders:seeds,site_cookie:'DO_NOT_EXPORT',...overrides}};
}
function harness(contexts,options={}) {
  const stored=new Map(),calls=[],saves=[];
  const deps={search:async()=>contexts,active:async()=>[],submit:async c=>{calls.push(c);return {success:true,data:{download_id:'id-'+calls.length}};},
    load:k=>stored.get(k)||[],save:(k,r)=>{saves.push(structuredClone(r));stored.set(k,structuredClone(r));},checkpoint(){},...options};
  return {deps,stored,calls,saves};
}
test('自动下载记录持久保存资源类型、查询做种数和响应时间；旧记录不推造类型',async()=>{
  for(const [eps,scope] of [[[1,2,3,4],'season'],[[2],'episode'],[[2,4],'episodes']]) {
    const h=harness([torrent('resource',eps,8)]),r=await C.processTask(task(),h.deps);
    assert.equal(r.records[0].scope,scope);assert.equal(r.records[0].seeds,8);assert.equal(r.records[0].site,'测试站');assert.ok(r.records[0].finishedAt);
    assert.equal(h.saves[0][0].scope,scope);assert.equal(h.saves[0][0].state,'submitting');
    const old={...r.records[0]};delete old.scope;assert.equal(C.safeRecord(old).scope,'unknown');
  }
});
test('解析缺集范围和未来集号；无数量、ID 冲突及恶意范围拒绝下载',()=>{
  assert.deepEqual(C.episodes('E01-E03、5 至 6'.replace(' 至 ','至')),[1,2,3,5,6]);
  assert.deepEqual(task({'待播集号':'4'}).needed,[2]);assert.equal(task({'缺失集号':'未确认'}).needed,null);
  for(const v of ['0','4-1','abc'])assert.throws(()=>C.episodes(v));
  assert.throws(()=>task({'来源登记集数':''}));assert.throws(()=>task({'TMDB ID':456}));assert.throws(()=>task({'缺失集号':5}));
});
test('完整季包优先，即使单集做种更高；完整包之间选最大做种',()=>{
  const p=C.choose(task(),[torrent('pack5',undefined,5),torrent('single20',[2],20),torrent('pack8',undefined,8)]);
  assert.deepEqual(p.selected.map(c=>c.title),['pack8']);assert.deepEqual(p.remaining,[]);
});
test('同类做种均≥5时优先小容量，5是阈值；低做种小容量不抢占高做种资源',()=>{
  const pool=[torrent('large',undefined,50,{size:2000}),torrent('small',undefined,5,{size:1000}),torrent('weak-small',undefined,4,{size:100})];
  for(const entries of [pool,[...pool].reverse()]){const p=C.choose(task(),entries);assert.equal(p.selected[0].title,'small');assert.match(p.selected[0].selectionReason,/总容量/);}
  assert.equal(C.choose(task(),[torrent('small',undefined,3,{size:100}),torrent('large',undefined,4,{size:1000})]).selected[0].title,'large');
});
test('高做种资源容量未知时整组退回做种数排序，不视为零，结果不受输入顺序影响',()=>{
  const pool=[torrent('small',undefined,10,{size:100}),torrent('unknown',undefined,20,{size:null}),torrent('large',undefined,30,{size:500})];
  for(const order of [pool,[...pool].reverse(),[pool[1],pool[2],pool[0]]])assert.equal(C.choose(task(),order).selected[0].title,'large');
  for(const size of [null,0,-1,'unknown','10 GB',Infinity])assert.equal(C.candidate(task(),torrent('x',undefined,10,{size})).sizeBytes,null);
  assert.equal(C.candidate(task(),torrent('x',undefined,10,{size:'1024'})).sizeBytes,1024);
});
test('明确中文标签优先于无标签高做种，支持简繁，但不绕过最少2个做种及整季优先',()=>{
  for(const label of ['中文','中文字幕','繁體字幕','簡中','國語','CHS','CHT']) {
    const p=C.choose(task(),[torrent('no-label',undefined,80),torrent('chinese',undefined,2,{labels:[label]})]);assert.equal(p.selected[0].title,'chinese',label);assert.ok(p.selected[0].chineseMark);
  }
  assert.equal(C.choose(task(),[torrent('pack',undefined,8),torrent('single',[2],50,{labels:['中字']})]).selected[0].title,'pack');
  assert.equal(C.choose(task(),[torrent('pack',undefined,8),torrent('bad',undefined,1,{labels:['中字']})]).selected[0].title,'pack');
});
test('中文片名和否定字幕标签不冒充中文资源；明确标题字幕标记可识别',()=>{
  assert.equal(C.candidate(task(),torrent('犯罪心理')).chineseMark,'');
  for(const text of ['无中文字幕','無中字','没有中文字幕','No Chinese subtitles','No CHS'])assert.equal(C.candidate(task(),torrent('Title',undefined,8,{labels:[text]})).chineseMark,'',text);
  assert.ok(C.candidate(task(),torrent('Title.S01.CHS')).chineseMark);
  assert.ok(C.candidate(task(),torrent('Title',undefined,8,{description:'内封中英字幕'})).chineseMark);
  assert.equal(C.candidate(task(),torrent('Title',undefined,8,{labels:['中字'],description:'无中文字幕'})).chineseMark,'');
});
test('零散多集按每个待补集容量比较；每次选定后重新计算覆盖和成本',()=>{
  const p=C.choose(task(),[torrent('multi',[1,2,4],8,{size:1200}),torrent('single2',[2],10,{size:800}),torrent('single4',[4],12,{size:800})]);
  assert.deepEqual(p.selected.map(c=>c.title),['multi']);assert.equal(p.selected[0].estimatedBytes,600);
  const next=C.choose(task({'来源登记集数':6,'缺失集号':'2、4、6'}),[torrent('pair24',[2,4],10,{size:1200}),torrent('pair46',[4,6],10,{size:1000}),torrent('single2',[2],10,{size:700})]);
  assert.deepEqual(next.selected.map(c=>c.title),['pair46','single2']);assert.deepEqual(next.remaining,[]);
});
test('所选容量、中文标记及选择依据写入持久记录，旧日志保留未知且不含私密参数',async()=>{
  const h=harness([torrent('Chinese',undefined,5,{size:2048,labels:['中字']})]),result=await C.processTask(task(),h.deps),r=result.records[0];
  assert.equal(r.sizeBytes,2048);assert.equal(r.chineseMark,'标签：中字');assert.match(r.selectionReason,/总容量/);assert.equal(h.saves[0][0].sizeBytes,2048);assert.ok(!JSON.stringify(result).includes('DO_NOT_EXPORT'));
  const old={...r};delete old.sizeBytes;delete old.chineseMark;assert.equal(C.safeRecord(old).sizeBytes,null);assert.equal(C.safeRecord(old).chineseMark,'');
});
test('已有任务仅在标题与站点唯一对应时记录本轮做种，缺站点或多候选不张冠李戴',async()=>{
  for(const variant of ['exact','missing-site','wrong-site','ambiguous']) {
    const resources=[torrent('Existing',undefined,13)],item={title:'Existing',hash:'active',site_name:variant==='missing-site'?'':variant==='wrong-site'?'另一站':'测试站'};
    if(variant==='ambiguous')resources.push(torrent('Existing',undefined,99,{size:999}));
    const h=harness(resources,{active:async()=>[item]}),r=await C.processTask(task(),h.deps);
    assert.equal(h.calls.length,0);assert.equal(r.records[0].state,'existing');assert.equal(r.records[0].seeds,variant==='exact'?13:null);
    assert.match(r.records[0].seedNote,variant==='exact'?/非原任务创建时/:/未确认/);
  }
});
test('整季做种不足 2 时按每次剩余缺集选择零散集，不选无关集',()=>{
  const p=C.choose(task(),[torrent('weak',undefined,1),torrent('e2',[2],9),torrent('e4',[4],2),torrent('e3',[3],50),torrent('e2low',[2],3)]);
  assert.deepEqual(p.selected.map(c=>c.title),['e2','e4']);assert.deepEqual(p.remaining,[]);
});
test('错误媒体、电影、混季、默认无季号、做种未知都不自动选取',()=>{
  const variants=[torrent(),torrent(),torrent(),torrent(),torrent()];
  variants[0].media_info.tmdb_id=456;variants[1].media_info.type='电影';variants[2].meta_info.end_season=2;
  variants[3].meta_info.begin_season=null;variants[4].torrent_info.seeders=null;
  for(const c of variants)assert.notEqual(C.candidate(task(),c).kind,'eligible');
});
test('裸 S01 不证明完整；全 N 集数量匹配才视作完整包',()=>{
  assert.equal(C.candidate(task(),torrent('S01',[],10)).kind,'ambiguous');
  assert.equal(C.candidate(task(),torrent('S01',[],10,{description:'全4集'})).full,true);
  assert.equal(C.candidate(task(),torrent('S01',[],10,{description:'全8集'})).kind,'ambiguous');
});
test('资源原始 S02 与识别结果 S01 冲突时不相信错误识别',()=>{
  assert.equal(C.candidate(task(),torrent('Test S02 Complete')).kind,'ambiguous');
  assert.equal(C.candidate(task(),torrent('Test S01-S02 Complete')).kind,'ambiguous');
});
test('待播资源不下载；缺集未确认只允许完整包，不猜单集',()=>{
  assert.equal(C.candidate(task({'待播集号':'4'}),torrent()).kind,'rejected');
  assert.equal(C.choose(task({'缺失集号':'未确认'}),[torrent('e2',[2],10)]).selected.length,0);
  assert.equal(C.choose(task({'缺失集号':'未确认'}),[torrent()]).selected.length,1);
});
test('无 ID 的名称匹配需名称、年份和电视剧类型全部吻合',()=>{
  const t={title:'測試劇',year:'2025'};
  assert.equal(C.sameIdentity(t,{title:'测试剧',year:'2025',type:'电视剧'}),true);
  assert.equal(C.sameIdentity(t,{title:'测试剧',year:'2024',type:'电视剧'}),false);
  assert.equal(C.sameIdentity(t,{title:'测试剧续集',year:'2025',type:'电视剧'}),false);
});
test('下载前先持久化，成功后才标黄和记录任务 ID；不导出私密链接/Cookie',async()=>{
  const h=harness([torrent()]);const r=await C.processTask(task(),h.deps);
  assert.equal(h.calls.length,1);assert.equal(h.saves[0][0].state,'submitting');assert.equal(h.saves[1][0].state,'accepted');
  assert.equal(r.color,'yellow');assert.equal(r.records[0].id,'id-1');assert.ok(!JSON.stringify(r).includes('DO_NOT_EXPORT'));
  assert.ok(!JSON.stringify(h.saves).includes('passkey'));
});
test('下载名称统一简繁、不可见字符、NBSP 与地区标记，不抹掉季号/版本',()=>{
  const t={title:'宇宙Marry Me？\u00a0',year:'2025'};
  assert.equal(C.sameIdentity(t,{title:'宇宙Marry Me?\u200e(台)',year:'2025',type:'电视剧'}),true);
  assert.equal(C.sameIdentity({title:'主妇侦探社',year:'2025'},{title:'主婦偵探社\u200e',year:'2025',type:'电视剧'}),true);
  for(const title of ['宇宙Marry Me? 第二季','宇宙Marry Me?(特别篇)','宇宙Marry Me Again?'])assert.equal(C.sameIdentity(t,{title,year:'2025',type:'电视剧'}),false,title);
  const parsed=C.taskFromRow({name:'宇宙Marry Me？\u200e(台) (2025)',season:1,key:'x',data:{'来源登记集数':12,'缺失集号':'1'}});
  assert.equal(parsed.title,'宇宙Marry Me?');
});
test('ID 匹配优先于任意语言片名，明确冲突 ID 不能靠名称绕过',()=>{
  const t=task();assert.equal(C.sameIdentity(t,{type:'电视剧',tmdb_id:123,title:'完全不同的翻译名'}),true);
  assert.equal(C.sameIdentity(t,{type:'电视剧',tmdb_id:456,title:'测试剧',year:'2025'}),false);
  assert.equal(C.sameIdentity({...t,douban:'9'},{type:'电视剧',tmdb_id:123,douban_id:8}),false);
  const chosen=C.candidate(t,torrent());
  assert.equal(C.activeMatch(t,{media:{tmdb_id:456,type:'电视剧'},title:chosen.title,name:t.title,year:t.year,season_episode:'S01'},[chosen]),null);
});
test('同名同年多个媒体 ID 不凭最高做种选剧，身份不足不误标无资源',async()=>{
  const noID={...task(),tmdb:'',douban:''},a=torrent('packA'),b=torrent('packB',undefined,100);b.media_info.tmdb_id=456;
  const h=harness([a,b]),r=await C.processTask(noID,h.deps);assert.equal(h.calls.length,0);assert.equal(r.color,null);assert.match(r.note,/多个媒体 ID/);
  const alias=torrent();alias.media_info.title='另一译名';alias.media_info.tmdb_id=null;
  for(const t of [noID,task()]){const h=harness([alias]),r=await C.processTask(t,h.deps);assert.equal(h.calls.length,0);assert.equal(r.color,null);assert.match(r.note,/媒体身份/);}
});
test('部分零散集成功仍标黄，清楚保留未覆盖集号',async()=>{
  const h=harness([torrent('e2',[2],10)]),r=await C.processTask(task(),h.deps);
  assert.equal(r.color,'yellow');assert.deepEqual(r.remaining,[4]);assert.deepEqual(r.covered,[2]);
});
test('明确无匹配资源或做种不足才标红；信息不明保留原色',async()=>{
  for(const contexts of [[],[torrent('weak',undefined,1)]]){
    const h=harness(contexts),r=await C.processTask(task(),h.deps);assert.equal(r.color,'red');assert.equal(h.calls.length,0);
  }
  const h=harness([torrent('unknown',[],9)]),r=await C.processTask(task(),h.deps);assert.equal(r.color,null);
});
test('没有已知缺集不下载，不把重复版本/状态未确认标成无资源',async()=>{
  const h=harness([torrent()],{search:async()=>assert.fail('不应搜索')});
  const r=await C.processTask(task({'缺失集号':'无'}),h.deps);assert.equal(r.state,'无需下载');assert.equal(r.color,null);
});
test('下载超时跨刷新仍保持结果不明，不自动二次提交',async()=>{
  let calls=0;const h=harness([torrent()],{submit:async()=>{calls++;throw Error('HTTP request uncertain with token=secret');}});
  const first=await C.processTask(task(),h.deps);assert.equal(first.color,null);assert.equal(first.records[0].state,'uncertain');assert.ok(!JSON.stringify(first).includes('secret'));
  const second=await C.processTask(task(),h.deps);assert.equal(calls,1);assert.match(second.state,/待核实/);
});
test('响应成功但无任务 ID 也保持结果不明；明确失败不误标红',async()=>{
  for(const reply of [{success:true},{success:false}]){
    const h=harness([torrent()],{submit:async()=>reply}),r=await C.processTask(task(),h.deps);
    assert.equal(r.color,null);assert.equal(r.records[0].state,reply.success?'uncertain':'rejected');
  }
});
test('存储失败不能提交下载，现有任务接口失败也不能提交',async()=>{
  let submits=0;
  for(const options of [{save:()=>{throw Error('storage full');}},{active:async()=>{throw Error('login expired');}}]){
    const h=harness([torrent()],{...options,submit:async()=>{submits++;}});await assert.rejects(()=>C.processTask(task(),h.deps));
  }
  assert.equal(submits,0);
});
test('已有本季下载标黄且不重复添加；集号未知也跨运行阻止重复',async()=>{
  const h=harness([torrent()],{active:async()=>[{hash:'old-hash',title:'另一个版本',media:{tmdb_id:123,type:'电视剧'},season_episode:'S01'}]});
  const r=await C.processTask(task(),h.deps);assert.equal(r.color,'yellow');assert.equal(r.records[0].state,'existing');assert.equal(h.calls.length,0);
  h.deps.active=async()=>[];await C.processTask(task(),h.deps);assert.equal(h.calls.length,0);
});
test('导入带已提交记录的旧报告，只补尚未覆盖的零散集',async()=>{
  const h=harness([torrent('e2',[2],10)]);const first=await C.processTask(task(),h.deps);
  const next=task({'下载记录':JSON.stringify(first.records)});h.deps.search=async()=>[torrent('pack'),torrent('e2',[2],10),torrent('e4',[4],3)];
  const r=await C.processTask(next,h.deps);assert.equal(h.calls.length,2);assert.equal(h.calls[1].torrent_info.title,'e4');assert.deepEqual(r.remaining,[]);
});
test('暂停发生在零散集之间时，继续仅补下一集',async()=>{
  let stopped=false;
  const h=harness([torrent('e2',[2],10),torrent('e4',[4],5)],{checkpoint:()=>{if(stopped)throw new C.Pause();}});
  h.deps.submit=async c=>{h.calls.push(c);stopped=true;return {success:true,data:{download_id:'id'}};};
  await assert.rejects(()=>C.processTask(task(),h.deps),e=>e.name==='Pause');assert.equal(h.calls.length,1);
  stopped=false;await C.processTask(task(),h.deps);assert.equal(h.calls.length,2);assert.equal(h.calls[1].torrent_info.title,'e4');
});
test('页面 API 适配器精确查询媒体和季，默认下载器；返回错误不带认证头',async()=>{
  const calls=[];
  const api={defaults:{baseURL:'/api/v1/'},get:async(p,c)=>{calls.push([p,c]);return p==='download/'?[]:{success:true,data:[torrent()]};},post:async(p,b)=>{calls.push([p,b]);return {success:true,data:{download_id:'x'}};}};
  const adapter=C.createAdapter(api,'https://moviepilot.example');await adapter.search(task());await adapter.active();await adapter.submit(torrent());
  assert.equal(calls[0][0],'search/media/tmdb%3A123');assert.equal(calls[0][1].params.season,'1');
  assert.equal(calls[2][1].save_path,null);assert.equal(calls[2][1].downloader,null);
  assert.throws(()=>C.createAdapter({...api,defaults:{baseURL:'https://other.invalid/api'}},'https://moviepilot.example'));
  api.get=async()=>{throw {response:{status:401},config:{headers:{Authorization:'SECRET'}}};};
  await assert.rejects(()=>adapter.search(task()),e=>/登录/.test(e.message)&&!e.message.includes('SECRET'));
});
test('名称搜索读取所有页；无结果消息与其他失败严格区分',async()=>{
  const pages=[];const api={get:async(p,c)=>{pages.push(c.params.page);return c.params.page===0?{success:true,data:[torrent()]}:{success:false,message:'未搜索到任何资源'};},post(){}};
  const adapter=C.createAdapter(api,'https://moviepilot.example');
  assert.equal((await adapter.search({...task(),tmdb:'',douban:''})).length,1);assert.deepEqual(pages,[0,1]);
  api.get=async()=>({success:false,message:'站点连接失败'});await assert.rejects(()=>adapter.search(task()),/未完成/);
});

test('先查下载管理，覆盖全部缺集时不搜索、不提交，并保存现有任务',async()=>{
 const events=[],h=harness([],{active:async()=>{events.push('active');return [{hash:'already',title:'existing',media:{type:'电视剧',tmdb_id:123},season_episode:'S01E02-E04'}];},search:async()=>{assert.fail('不应搜索');}});
 const r=await C.processTask(task(),h.deps);assert.deepEqual(events,['active']);assert.equal(h.calls.length,0);assert.equal(r.color,'yellow');assert.deepEqual(r.remaining,[]);assert.equal(r.records[0].id,'already');
});
test('已有第2集只补第4集，排除重叠整季和零散包',async()=>{
 const h=harness([torrent('pack'),torrent('overlap',[2,4],100),torrent('E4',[4],5)],{active:async()=>[{hash:'old2',title:'Old2',media:{type:'电视剧',tmdb_id:123},season_episode:'S01E02'}]});
 const r=await C.processTask(task(),h.deps);assert.deepEqual(h.calls.map(c=>c.torrent_info.title),['E4']);assert.deepEqual(r.remaining,[]);assert.equal(r.records.filter(r=>r.state==='existing').length,1);
});
test('搜索期间出现的任务在提交前重新识别，避免重复',async()=>{
 let n=0;const h=harness([torrent('E2',[2]),torrent('E4',[4])],{active:async()=>++n===1?[]:[{hash:'new',title:'New',media:{type:'电视剧',tmdb_id:123},season_episode:'S01E02-E04'}]});
 const r=await C.processTask(task(),h.deps);assert.equal(n,2);assert.equal(h.calls.length,0);assert.equal(r.color,'yellow');
});
test('另一季或明确不同剧ID的下载不阻止本季补缺',async()=>{
 const h=harness([torrent()],{active:async()=>[{hash:'otherseason',media:{type:'电视剧',tmdb_id:123},season_episode:'S02E02'},{hash:'otherid',media:{type:'电视剧',tmdb_id:456},season_episode:'S01E02'}]});await C.processTask(task(),h.deps);assert.equal(h.calls.length,1);
});
test('下载管理不可读取时不搜索、不提交，不能当作空任务列表',async()=>{
 const h=harness([],{active:async()=>({error:'unavailable'}),search:async()=>assert.fail('不应搜索')});await assert.rejects(C.processTask(task(),h.deps),/无法核查/);assert.equal(h.calls.length,0);
});

test('已有任务的资源即使做种不足2，也不会被防重复核查忽略',async()=>{const h=harness([torrent('OldLow',undefined,0)],{active:async()=>[{hash:'low',title:'OldLow',site_name:'测试站'}]});const r=await C.processTask(task(),h.deps);assert.equal(h.calls.length,0);assert.equal(r.color,'yellow');assert.equal(r.records[0].seeds,0);});
