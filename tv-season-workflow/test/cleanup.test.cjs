// 清理只使用内存文件列表和模拟 remove；测试不会访问或删除云盘文件。
const {test}=require('node:test');
const assert=require('node:assert/strict');
const C=require('../season-review.user.js');
const path='/115/Library_incs/nas/电视剧/日本剧集/测试剧 (2025) [tmdbid=1]/Season 1';
const task={name:'测试剧 (2025) [tmdbid=1]',season:1,path,key:'test',data:{'来源登记集数':2,'播出状态':'已播完'}};
const source={total:2,title:'测试剧 (2025)',url:'https://movie.douban.com/subject/123/',releaseYears:['2025'],broadcast:'已播完',checkedAt:'2026-10-21T00:00:00Z'};
const file=(name,modified='2026-10-20 10:00',size='1.0 GB')=>({name,path:path+'/'+name,directory:false,modified,size});
const full=()=>[1,2].map(n=>file(`Test.S01E0${n}.1080p-WEB-A.mkv`));
const partial=()=>file('Test.S01E01.720p-WEB-B.mkv','2026-10-21 10:00');
const create=(rows,db=source,t=task)=>C.plan(t,rows,db,'plan-1');
test('完整版本优先于更新但不完整的版本，生成精确的普通文件删除清单',()=>{
  const rows=[...full(),partial()],p=create(rows);
  assert.equal(p.eligible,true);assert.equal(p.keep.length,2);assert.deepEqual(p.remove.map(f=>f.name),[partial().name]);
  assert.ok(p.keep.every(f=>f.modified==='2026-10-20 10:00'));
});
test('两个版本都齐全时，不因新旧、分辨率差异自动删除',()=>{
  const p=create([...full(),partial(),file('Test.S01E02.720p-WEB-B.mkv','2026-10-21 10:00')]);
  assert.equal(p.state,'多个完整版本，人工判断');assert.equal(p.eligible,false);assert.deepEqual(p.remove,[]);
});
test('同一版本的少量重复副本按云盘显示修改时间保留最新',()=>{
  const copy=file('Test.S01E01.1080p-WEB-A (copy 2).mkv','2026-10-21 10:00');
  const p=create([...full(),copy]);assert.equal(p.eligible,true);assert.ok(p.keep.some(f=>f.path===copy.path));assert.equal(p.remove[0].name,full()[0].name);
});
test('同模板每集都有两份构成多套完整副本，依然交人工判断',()=>{
  const p=create([...full(),...full().map(f=>file(f.name.replace('.mkv',' (copy 2).mkv'),'2026-10-21 10:00'))]);
  assert.equal(p.state,'多套完整副本，人工判断');assert.equal(p.eligible,false);
});
test('最新时间相同不任意挑文件；未知时间、大小、空文件和超范围集号不清理',()=>{
  const p=create([...full(),file('Test.S01E01.1080p-WEB-A (copy 2).mkv')]);assert.match(p.state,/时间相同/);assert.equal(p.remove.length,0);
  for(const bad of [file(partial().name,''),file(partial().name,'2026-10-21 10:00','0 B'),file(partial().name,'2026-10-21 10:00','--'),file('Test.S01E03.720p-WEB-B.mkv')])assert.equal(create([...full(),bad]).eligible,false);
});
test('混版合计齐全但无独立完整版本时不清理',()=>{
  const p=create([full()[1],partial()]);assert.deepEqual(p.missing,[]);assert.equal(p.state,'混版拼齐，人工判断');assert.equal(p.eligible,false);
});
test('无技术版本标记的完整简名组不能用于淘汰其他版本',()=>{
  const p=create([file('测试剧 - S01E01 - 01.mkv'),file('测试剧 - S01E02 - 02.mkv'),partial()]);
  assert.equal(p.eligible,false);assert.match(p.state,/缺少版本标识/);
  const clean=create([file('测试剧 - S01E01 - 01.mkv'),file('测试剧 - S01E02 - 02.mkv')]);assert.equal(clean.complete,true);assert.equal(clean.state,'齐全');
});
test('同一文件名重复 S01E01、数字尾标题可归组；REPACK 与容器差异不合并',()=>{
  const rows=[1,2].map(n=>file(`测试剧 - S01E0${n} - [测试剧].Test.2025.S01E0${n}.1080p.WEB-DL.H264.AAC-UBWEB.mkv`));
  assert.equal(create(rows).complete,true);
  const variants=[...full(),...full().map(r=>file(r.name.replace('.mkv','.mp4')))];assert.equal(create(variants).state,'多个完整版本，人工判断');
});
test('仅删除待删视频唯一关联的图片和 NFO，通用及孤立刮削文件保留',()=>{
  const bad=partial(),base=bad.name.replace('.mkv',''),rows=[...full(),bad,file(base+'.nfo',undefined,'2 KB'),file(base+'-thumb.jpg',undefined,'50 KB'),file('season.nfo',undefined,'1 KB'),file('poster.jpg',undefined,'80 KB'),file('孤立图片.jpg',undefined,'20 KB')];
  const p=create(rows);assert.deepEqual(p.remove.map(f=>f.name),[bad.name,base+'.nfo',base+'-thumb.jpg']);
  assert.deepEqual(p.preserved.map(f=>f.name),['season.nfo','poster.jpg','孤立图片.jpg']);
});
test('同名不同容器共用 NFO 时，不能连带删除保留版本的配套文件',()=>{
  const sameBase=full()[0].name.replace('.mkv',''),rows=[...full(),file(sameBase+'.mp4'),file(sameBase+'.nfo',undefined,'2 KB')];
  const p=create(rows);assert.equal(p.remove.length,1);assert.equal(p.remove[0].name,sameBase+'.mp4');assert.equal(p.preserved[0].reason,'可能对应多个视频，保留');
});
test('季内子目录、临时下载、无法识别视频或 STRM 指针不执行清理',()=>{
  for(const extra of [{...file('嵌套目录'),directory:true},file('pending.part'),file('movie.mkv'),file('Test.S01E01.720p-B.strm')]){
    const p=create([...full(),partial(),extra]);assert.equal(p.eligible,false);assert.equal(p.remove.length,0);
  }
  assert.equal(create([...full(),file('movie.mkv')]).missing,null);
});
test('空季保留 0/0 和明确缺集；来源无效、错剧错季不猜集数',()=>{
  const p=create([]);assert.equal(p.videoCount,0);assert.equal(p.current,0);assert.deepEqual(p.missing,[1,2]);assert.equal(p.eligible,false);
  for(const db of [{...source,total:0},{...source,url:'https://else.invalid/'},{...source,title:'其他剧 (2025)'},{...source,title:'测试剧 第二季 (2025)'},{...source,error:'未匹配'}])assert.throws(()=>create(full(),db));
});
test('仅豆瓣数字不代表播完；同数量初检结束结论可保留，冲突或数量变化不可沿用',()=>{
  const noEnd={...source,broadcast:'未确认'};
  assert.equal(create(full(),noEnd).confirmedEnded,true);
  assert.equal(create(full(),noEnd,{...task,data:{}}).confirmedEnded,false);
  assert.equal(create(full(),{...source,broadcast:'连载中'}).confirmedEnded,false);
  assert.equal(create(full(),noEnd,{...task,data:{'播出状态':'已播完','来源登记集数':3}}).confirmedEnded,false);
});
function harness(rows) {
  let current=structuredClone(rows),journal=null;const removed=[],saved=[];
  const deps={list:async()=>structuredClone(current),remove:async f=>{removed.push(f.path);current=current.filter(r=>r.path!==f.path);},load:()=>journal,save:(_k,v)=>{journal=structuredClone(v);saved.push(structuredClone(v));},checkpoint(){}};
  return {deps,removed,saved,get rows(){return current;},set rows(v){current=v;},get journal(){return journal;}};
}
test('每次删除前落盘，删后确认仅目标消失，保留版全部还在才返回成功',async()=>{
  const rows=[...full(),partial(),file(partial().name.replace('.mkv','.nfo'),undefined,'1 KB')],p=create(rows),h=harness(rows);
  const result=await C.execute(p,h.deps);
  assert.equal(result.deleted.length,2);assert.equal(h.saved[0].pending.path,partial().path);assert.equal(h.journal.pending,null);
  assert.deepEqual(h.rows.map(r=>r.path),full().map(r=>r.path));assert.ok(h.journal.records.every(r=>r.state==='verified'));
});
test('确认后文件大小、修改时间或新增文件发生变化时，第一项也不删',async()=>{
  for(const change of [r=>{r[0].size='2 GB';},r=>{r[0].modified='2026-10-21 11:00';},r=>r.push(file('new.txt',undefined,'1 KB'))]){
    const rows=[...full(),partial()],p=create(rows),h=harness(rows);change(h.rows);
    await assert.rejects(()=>C.execute(p,h.deps),/发生变化/);assert.equal(h.removed.length,0);
  }
});
test('删除响应失败/结果未知不重试；目标仍在或保留文件消失都停止后续删除',async()=>{
  for(const scenario of ['throw','still-there','lost-keep']) {
    const rows=[...full(),partial()],p=create(rows),h=harness(rows);let attempts=0;
    h.deps.remove=async f=>{attempts++;if(scenario==='throw')throw Error('timeout');if(scenario==='lost-keep')h.rows=h.rows.filter(r=>r.path!==f.path&&r.path!==full()[0].path);};
    await assert.rejects(()=>C.execute(p,h.deps),/结果未确认/);assert.ok(h.journal.pending);
    await assert.rejects(()=>C.execute(p,h.deps),/上次删除结果未确认/);assert.equal(attempts,1);
  }
});
test('暂停后继续不会重删已验证项；未点击原生确认前中止可解除待提交状态',async()=>{
  const rows=[...full(),partial(),file(partial().name.replace('.mkv','.nfo'),undefined,'1 KB')],p=create(rows),h=harness(rows);
  h.deps.checkpoint=()=>{if(h.removed.length===1)throw Object.assign(Error('暂停'),{name:'Pause'});};
  await assert.rejects(()=>C.execute(p,h.deps),/暂停/);assert.equal(h.journal.pending,null);
  h.deps.checkpoint=()=>{};await C.execute(p,h.deps);assert.equal(h.removed.length,2);assert.equal(new Set(h.removed).size,2);
  const h2=harness(rows);h2.deps.remove=async()=>{throw Object.assign(Error('用户暂停'),{notSubmitted:true,name:'Pause'});};
  await assert.rejects(()=>C.execute(p,h2.deps),/暂停/);assert.equal(h2.journal.pending,null);assert.equal(h2.removed.length,0);
});
test('持久化失败、伪造删除路径或多完整版本不会调用删除适配器',async()=>{
  const rows=[...full(),partial()],p=create(rows),h=harness(rows);h.deps.save=()=>{throw Error('存储失败');};
  await assert.rejects(()=>C.execute(p,h.deps),/存储失败/);assert.equal(h.removed.length,0);
  const bad={...p,remove:[{...p.remove[0],path:full()[0].path}]};await assert.rejects(()=>C.execute(bad,harness(rows).deps));
  await assert.rejects(()=>C.execute(create(full()),harness(full()).deps),/没有可执行/);
});
