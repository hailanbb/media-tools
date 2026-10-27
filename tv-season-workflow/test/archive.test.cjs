// 内存目录模拟移动：覆盖整剧范围、防覆盖、状态恢复、文件变化，不操作真实云盘。
const {test}=require('node:test'),assert=require('node:assert/strict');
const A=require('../season-archive.user.js');
test('绿色也不能绕过未闭环整理修复记录，文件复核后才恢复正常计划',async()=>{
  const f=fixture();const record={schema:1,state:'submitted',at:new Date().toISOString(),episodes:[1],items:[]};f.row.data['整理修复记录']=JSON.stringify(record);
  let reads=0;await assert.rejects(A.plan([f.row],async()=>{reads++;return [];},'blocked'),/整理修复/);assert.equal(reads,0);
  f.row.data['整理修复记录']=JSON.stringify({...record,state:'files-verified'});assert.equal((await A.plan([f.row],f.list,'ok')).eligible,true);
});
test('预览只复用目标根和分类，源及目标剧目录始终实时读取，快照不被调用方改写',async()=>{
  const calls=[];const list=async p=>{calls.push(p);return [{path:p+'/子目录',name:'子目录',directory:true}];};
  const preview=A.createPreviewListing(list);
  for(const p of [A.DEST,A.DEST+'/日本剧集']) {
    const first=await preview(p);first[0].name='外部修改';
    const second=await preview(p);assert.equal(second[0].name,'子目录');second.length=0;
    assert.equal((await preview(p)).length,1);assert.equal(calls.filter(x=>x===p).length,1);
  }
  for(const p of [A.SOURCE+'/日本剧集',A.DEST+'/日本剧集/测试剧']){await preview(p);await preview(p);assert.equal(calls.filter(x=>x===p).length,2);}
});
test('预览快照60秒过期，继续检查新实例不复用旧快照，失败清空快照',async()=>{
  let time=0,calls=0,fail=false;
  const list=async()=>{calls++;if(fail)throw Error('列表失败');return [];};
  const preview=A.createPreviewListing(list,{now:()=>time});
  await preview(A.DEST);time=59999;await preview(A.DEST);assert.equal(calls,1);
  time=60000;await preview(A.DEST);assert.equal(calls,2);
  await A.createPreviewListing(list)(A.DEST);assert.equal(calls,3);
  fail=true;await assert.rejects(preview(A.SOURCE+'/日本剧集'),/列表失败/);fail=false;
  await preview(A.DEST);assert.equal(calls,5);
});
test('同分类连续预览共用目标列表，但移动前实时发现新目标并拒绝旧清单',async()=>{
  const f=fixture(),calls=new Map();const list=async p=>{calls.set(p,(calls.get(p)||0)+1);return f.list(p);};
  const preview=A.createPreviewListing(list);
  const p=await A.plan([f.row],preview,'preview');await A.plan([f.row],preview,'again');
  assert.equal(calls.get(A.DEST),1);assert.equal(calls.get(A.DEST+'/'+f.category),1);
  assert.equal(calls.get(f.source),4);assert.equal(calls.get(f.row.path),2);
  f.tree.set(A.DEST+'/'+f.category,[f.file(A.DEST+'/'+f.category,f.name,true)]);f.tree.set(f.target,[]);
  await assert.rejects(A.execute(p,f.api),/发生变化/);assert.equal(f.moves,0);
});
test('同剧目录记录阻挡绿色季，误涂绿色目录记录也不能触发云盘查询',async()=>{
  const f=fixture(),directory={...f.row,path:f.source,season:null,directoryOnly:true,color:'other'};
  let calls=0;const list=async()=>{calls++;throw Error('不应查询');};
  assert.equal(A.groups([f.row,directory]).length,1);
  await assert.rejects(A.plan([f.row,directory],list,'test'),/目录级记录/);
  await assert.rejects(A.plan([{...directory,color:'green'}],list,'test'),/目录级记录/);
  assert.equal(calls,0);assert.equal(A.groups([directory]).length,0);
});
function fixture(name='测试剧 (2025)') {
  const category='日本剧集',source=A.SOURCE+'/'+category+'/'+name,target=A.DEST+'/'+category+'/'+name;
  const row={name,category,showPath:source,path:source+'/Season 1',season:1,color:'green',data:{'来源登记集数':2}};
  const file=(parent,name,directory=false)=>({path:parent+'/'+name,name,directory,size:directory?'':'1 GB',modified:'2026-10-21 10:00'});
  const tree=new Map([[A.DEST,[file(A.DEST,category,true)]],[A.DEST+'/'+category,[]],[A.SOURCE+'/'+category,[file(A.SOURCE+'/'+category,name,true)]],
    [source,[file(source,'Season 1',true),file(source,'tvshow.nfo')]],[row.path,[file(row.path,'测试剧 - S01E01 - 01.mkv'),file(row.path,'测试剧 - S01E02 - 02.mkv')]]]);
  const list=async p=>{if(!tree.has(p))throw Error('not found '+p);return structuredClone(tree.get(p));};
  let moves=0,journal=null;
  const api={list,checkpoint(){},load:async()=>journal,save:async(_p,j)=>{journal=structuredClone(j);},move:async()=>{
    moves++;tree.set(A.SOURCE+'/'+category,[]);tree.set(A.DEST+'/'+category,[file(A.DEST+'/'+category,name,true)]);
    for(const [path,rows] of [...tree])if(path===source||path.startsWith(source+'/')){tree.set(target+path.slice(source.length),rows.map(r=>({...r,path:target+r.path.slice(source.length),modified:r.directory?'2026-10-22 10:00':r.modified})));tree.delete(path);}
  }};
  return {name,category,source,target,row,tree,file,list,api,get moves(){return moves;},get journal(){return journal;}};
}
test('整剧所有实际季绿色，目录及配套文件一起移动，验证后记录成功',async()=>{
  const f=fixture(),p=await A.plan([f.row],f.list,'one');assert.equal(p.manifest.length,4);
  const result=await A.execute(p,f.api);assert.equal(f.moves,1);assert.equal(result.verified,true);assert.equal(f.journal.state,'已归档');
  assert.equal((await A.reconcile([f.row],f.journal,f.list)).verified,true);
  await assert.rejects(A.execute(p,f.api),/已有移动记录/);assert.equal(f.moves,1);
});
test('混色、重复季、缺少实际季记录、报告幽灵季均不能归档',async()=>{
  for(const variant of ['mixed','duplicate','missing','extra']) {
    const f=fixture(),rows=[f.row];
    if(variant==='mixed')rows.push({...f.row,color:'yellow'});
    if(variant==='duplicate')rows.push({...f.row});
    if(variant==='missing'){f.tree.get(f.source).push(f.file(f.source,'Season 2',true));const dir=f.source+'/Season 2';f.tree.set(dir,[f.file(dir,'测试剧.S02E01.mkv')]);}
    if(variant==='extra')rows.push({...f.row,season:2,path:f.source+'/Season 2'});
    await assert.rejects(A.plan(rows,f.list,'p'));assert.equal(f.moves,0);
  }
});
test('缺少目标分类、同名目标、未知子目录、重复视频、临时文件、空季与集数不符均拦截',async()=>{
  for(const variant of ['category','collision','unknown','nested','duplicate','temp','empty','wrongCount','unknownEpisode','zero','rootVideo']) {
    const f=fixture(),files=f.tree.get(f.row.path);
    if(variant==='category')f.tree.set(A.DEST,[]);
    if(variant==='collision')f.tree.get(A.DEST+'/'+f.category).push(f.file(A.DEST+'/'+f.category,f.name,true));
    if(variant==='unknown')f.tree.get(f.source).push(f.file(f.source,'Extras',true));
    if(variant==='nested')files.push(f.file(f.row.path,'1080p',true));
    if(variant==='duplicate')files.push(f.file(f.row.path,'测试剧.S01E01.720p.mkv'));
    if(variant==='temp')files.push(f.file(f.row.path,'x.mkv.!qb'));
    if(variant==='empty')f.tree.set(f.row.path,[]);
    if(variant==='wrongCount')f.row.data['来源登记集数']=3;
    if(variant==='unknownEpisode')files.push(f.file(f.row.path,'unknown.mkv'));
    if(variant==='zero')files[0].size='0 B';
    if(variant==='rootVideo')f.tree.get(f.source).push(f.file(f.source,'测试剧.S01E03.mkv'));
    await assert.rejects(A.plan([f.row],f.list,'p'),undefined,variant);assert.equal(f.moves,0);
  }
});
test('预览后文件变化和新出现目标目录禁止提交',async()=>{
  for(const variant of ['file','target']) {
    const f=fixture(),p=await A.plan([f.row],f.list,'p');
    if(variant==='file')f.tree.get(f.row.path)[0].modified='2026-10-21 11:00';
    else f.tree.get(A.DEST+'/'+f.category).push(f.file(A.DEST+'/'+f.category,f.name,true));
    await assert.rejects(A.execute(p,f.api));assert.equal(f.moves,0);assert.equal(f.journal,null);
  }
});
test('移动后源未消失、目标缺文件、改大小或改时间均不标记成功且不能重试',async()=>{
  for(const variant of ['source','missing','size','modified']) {
    const f=fixture(),p=await A.plan([f.row],f.list,'p'),move=f.api.move;
    f.api.move=async()=>{await move();if(variant==='source')f.tree.get(A.SOURCE+'/'+f.category).push(f.file(A.SOURCE+'/'+f.category,f.name,true));
      else if(variant==='missing')f.tree.get(f.target+'/Season 1').pop();else f.tree.get(f.target+'/Season 1')[0][variant]=variant==='size'?'2 GB':'2026-10-22 10:00';};
    await assert.rejects(A.execute(p,f.api));assert.equal(f.journal.pending,true);assert.equal(f.moves,1);
    await assert.rejects(A.execute(p,f.api),/已有移动记录/);assert.equal(f.moves,1);
  }
});
test('提交前明确失败可重新检查，提交后异常保留完整清单供恢复',async()=>{
  const f=fixture(),p=await A.plan([f.row],f.list,'p'),move=f.api.move;
  f.api.move=async()=>{throw Object.assign(Error('cancelled'),{notSubmitted:true});};await assert.rejects(A.execute(p,f.api));assert.equal(f.journal.pending,false);assert.equal(f.journal.operations[0].state,'not-submitted');
  f.api.move=async()=>{await move();throw Error('response lost');};await assert.rejects(A.execute(p,f.api));assert.equal(f.journal.pending,true);
  const result=await A.reconcile([f.row],f.journal,f.list);assert.equal(result.verified,true);assert.equal(f.moves,1);
});

function mergeFixture(extra=false) {
  const f=fixture('天龙八部'),old=f.row.path,second=f.source+'/Season 2';
  f.tree.set(second,f.tree.get(old).map(r=>({...r,path:r.path.replace('/Season 1/','/Season 2/').replace('S01','S02'),name:r.name.replace('S01','S02')})));
  f.tree.delete(old);f.tree.get(f.source)[0]=f.file(f.source,'Season 2',true);f.row.path=second;f.row.season=2;
  f.tree.get(A.DEST+'/'+f.category).push(f.file(A.DEST+'/'+f.category,f.name,true));
  f.tree.set(f.target,[f.file(f.target,'Season 1',true),f.file(f.target,'poster.jpg')]);
  f.tree.set(f.target+'/Season 1',[f.file(f.target+'/Season 1','旧季.S01E01.mkv')]);
  const rows=[f.row];if(extra){f.tree.get(f.source).push(f.file(f.source,'Season 1',true));f.tree.set(old,[f.file(old,'测试剧.S01E01.mkv')]);rows.push({...f.row,path:old,season:1,data:{'来源登记集数':1}});}
  const moves=[];f.api.move=async op=>{
    moves.push(op.source);const entry=f.tree.get(A.parent(op.source)).find(r=>r.path===op.source);
    f.tree.set(A.parent(op.source),f.tree.get(A.parent(op.source)).filter(r=>r.path!==op.source));
    f.tree.get(A.parent(op.target)).push({...entry,path:op.target,modified:entry.directory?'2026-10-22 10:00':entry.modified});
    for(const [path,files] of [...f.tree])if(path===op.source||path.startsWith(op.source+'/')){f.tree.set(op.target+path.slice(op.source.length),files.map(r=>({...r,path:op.target+r.path.slice(op.source.length)})));f.tree.delete(path);}
  };
  f.api.removeEmpty=async()=>{assert.ok(A.equal(f.tree.get(f.source),A.cleanupFiles(f.journal)));f.tree.delete(f.source);f.tree.set(A.parent(f.source),f.tree.get(A.parent(f.source)).filter(r=>r.path!==f.source));};
  return {f,rows,moves};
}
test('目标已有第一季时移入第二季及源根文件，验证后删除源空目录',async()=>{
  const {f,rows,moves}=mergeFixture(),before=structuredClone(f.tree.get(f.target+'/Season 1'));
  const p=await A.plan(rows,f.list,'merge');assert.equal(p.mode,'seasons');assert.equal(p.operations.length,2);
  const result=await A.execute(p,f.api);assert.equal(result.verified,true);assert.deepEqual(moves,[rows[0].path,f.source+'/tvshow.nfo']);
  assert.deepEqual(f.tree.get(f.target+'/Season 1'),before);assert.equal(f.tree.has(f.source),false);assert.ok(f.tree.get(f.target).some(r=>r.name==='tvshow.nfo'));assert.equal(result.cleanup.state,'verified');
  assert.equal(result.operations[0].state,'verified');assert.ok(result.operations[0].at);assert.ok(result.operations[0].finishedAt);
  assert.equal((await A.reconcile(rows,f.journal,f.list)).verified,true);
});
test('同季冲突含别名整剧暂缓，不移动其他季或根文件',async()=>{
  for(const alias of ['Season 1','Season 01']) {
    const {f,rows,moves}=mergeFixture(true);
    if(alias!=='Season 1'){const old=f.target+'/Season 1',next=f.target+'/'+alias;f.tree.get(f.target)[0]={...f.tree.get(f.target)[0],name:alias,path:next};f.tree.set(next,f.tree.get(old).map(r=>({...r,path:next+'/'+r.name})));f.tree.delete(old);}
    const p=await A.plan(rows,f.list,'mixed');assert.equal(p.skipped.length,1);assert.equal(p.operations.length,0);assert.equal(p.eligible,false);
    await assert.rejects(A.execute(p,f.api));assert.equal(moves.length,0);assert.ok(f.tree.has(f.source+'/Season 1'));
  }
});
test('所有季均冲突只生成保留明细，不允许移动',async()=>{
  const {f,rows,moves}=mergeFixture();f.tree.get(f.target).push(f.file(f.target,'Season 2',true));f.tree.set(f.target+'/Season 2',[]);
  const p=await A.plan(rows,f.list,'skip');assert.equal(p.eligible,false);assert.equal(p.skipped.length,1);assert.equal(p.operations.length,0);
  await assert.rejects(A.execute(p,f.api));assert.equal(moves.length,0);
});
test('合并季前目标改动不移动；移动后旧季被修改则不能标记整剧验证成功',async()=>{
  for(const when of ['before','after']) {
    const {f,rows,moves}=mergeFixture(),p=await A.plan(rows,f.list,'change');
    const change=()=>{f.tree.get(f.target+'/Season 1')[0].size='9 GB';};
    if(when==='before')change();else {const move=f.api.move;f.api.move=async op=>{await move(op);change();};}
    await assert.rejects(A.execute(p,f.api));assert.equal(moves.length,when==='before'?0:1);
    if(when==='after'){assert.equal(f.journal.pending,true);assert.equal(f.journal.operations[0].state,'verified');}
  }
});
test('季移动请求成功但响应丢失，可只读恢复，不重发移动',async()=>{
  const {f,rows,moves}=mergeFixture(),p=await A.plan(rows,f.list,'lost'),move=f.api.move;
  f.api.move=async op=>{await move(op);if(op.kind==='file')throw Error('response lost');};await assert.rejects(A.execute(p,f.api));
  assert.equal(f.journal.pending,true);assert.equal(f.journal.operations[1].state,'pending');
  await assert.rejects(A.reconcile(rows,f.journal,f.list),/源剧集目录仍存在/);
  const result=await A.finishEmpty(f.journal,f.api);assert.equal(result.verified,true);assert.equal(moves.length,2);
});
test('上次归档成功后源目录新增下一季，允许新计划归档且保存旧批次明细',async()=>{
  const {f,rows,moves}=mergeFixture(),first=await A.plan(rows,f.list,'first');await A.execute(first,f.api);
  const path=f.source+'/Season 3';f.tree.set(f.source,[]);f.tree.get(A.parent(f.source)).push(f.file(A.parent(f.source),f.name,true));f.tree.get(f.source).push(f.file(f.source,'Season 3',true));f.tree.set(path,[f.file(path,'新季.S03E01.mkv')]);
  const row={...f.row,path,season:3,data:{'来源登记集数':1}},next=await A.plan([row],f.list,'next');
  const result=await A.execute(next,f.api);assert.equal(result.verified,true);assert.equal(moves.length,3);assert.equal(result.history[0].planId,'first');assert.equal(result.operations[0].planId,'next');
});
test('绿色人工确认且总数空白可以归档，但仍要求集号连续及非空视频',async()=>{
  const f=fixture();f.row.data['来源登记集数']='';assert.equal((await A.plan([f.row],f.list,'p')).eligible,true);
  f.tree.get(f.row.path).shift();await assert.rejects(A.plan([f.row],f.list,'p'),/不连续/);
});
test('不让非绿色剧集单独进入候选队列，但保留同剧所有颜色以阻止半剧移动',()=>{
  const f=fixture(),yellow={...f.row,color:'yellow'},other={...yellow,showPath:f.source+'2'};
  assert.deepEqual(A.groups([f.row,yellow,other]),[[f.row,yellow]]);
});
test('归档按真实路径处理混排片名，不把译名/简繁近似目录当作移动对象',async()=>{
  for(const name of ['宇宙Marry Me？ (2025)','睁眼一看是\u00a0SUPER TV (2025)']) {
    const f=fixture(name),p=await A.plan([f.row],f.list,'p');assert.equal(p.source,f.source);assert.equal(p.target,f.target);
    f.row.name='另一译名 (2025)';await assert.rejects(A.plan([f.row],f.list,'wrong'),/路径/);assert.equal(f.moves,0);
  }
});

test('根文件同名冲突整剧暂缓并列出路径，不覆盖文件',async()=>{
  const {f,rows,moves}=mergeFixture();f.tree.get(f.source).push(f.file(f.source,'custom.txt'));f.tree.get(f.target).push(f.file(f.target,'custom.txt'));
  const p=await A.plan(rows,f.list,'conflict');assert.equal(p.eligible,false);assert.equal(p.operations.length,0);assert.equal(p.skipped[0].source,f.source+'/custom.txt');
  await assert.rejects(A.execute(p,f.api));assert.equal(moves.length,0);
});
test('共用图片及tvshow.nfo保留目标，移动季后删除已列源资料，目标季和父目录均刷新读取',async()=>{
  const {f,rows,moves}=mergeFixture();
  for(const name of ['clearlogo.png','poster.jpg'])if(!f.tree.get(f.source).some(x=>x.name===name))f.tree.get(f.source).push(f.file(f.source,name));
  f.tree.get(f.target).push(f.file(f.target,'clearlogo.png'),f.file(f.target,'tvshow.nfo'));
  const before=structuredClone(f.tree.get(f.target)),p=await A.plan(rows,f.list,'shared');
  assert.equal(p.eligible,true);assert.equal(p.skipped.length,0);assert.equal(p.cleanup.discardFiles.length,3);assert.equal(p.operations.length,1);
  const reads=[],list=f.api.list;f.api.list=async path=>{reads.push(path);return list(path);};
  const r=await A.execute(p,f.api);assert.equal(r.verified,true);assert.equal(f.tree.has(f.source),false);assert.equal(moves.length,1);
  for(const old of before)assert.deepEqual(f.tree.get(f.target).find(x=>x.path===old.path),old);
  const index=reads.indexOf(p.operations[0].target);assert.ok(index>=0);assert.equal(reads[index+1],f.target);
  assert.equal(r.cleanup.manifest.filter(x=>!x.directory).length,3);
});
test('目标共用文件为零、目录或移动后被修改，均不删除源剧集',async()=>{
  for(const variant of ['zero','directory','changed']){
    const {f,rows}=mergeFixture();const target=f.file(f.target,'tvshow.nfo');f.tree.get(f.target).push(target);
    if(variant==='zero')target.size='0 B';if(variant==='directory'){target.directory=true;f.tree.set(target.path,[]);}
    const p=await A.plan(rows,f.list,'guard');
    if(variant!=='changed'){assert.equal(p.eligible,false);continue;}
    let removed=0;f.api.removeEmpty=async()=>{removed++;};const move=f.api.move;f.api.move=async(...args)=>{await move(...args);target.size='99 GB';};
    await assert.rejects(A.execute(p,f.api));assert.equal(removed,0);assert.equal(f.tree.has(f.source),true);
  }
});
test('空目录清理前新增文件或目标变更，不删除且不标成功',async()=>{
  for(const variant of ['source','target']) {
    const {f,rows}=mergeFixture(),p=await A.plan(rows,f.list,'race');let deletes=0;f.api.removeEmpty=async()=>{deletes++;};
    const move=f.api.move;f.api.move=async op=>{await move(op);if(op.kind==='file'){
      if(variant==='source')f.tree.get(f.source).push(f.file(f.source,'新到文件.nfo'));
      else f.tree.get(f.target+'/Season 1')[0].size='9 GB';
    }};
    await assert.rejects(A.execute(p,f.api));assert.equal(deletes,0);assert.notEqual(f.journal.verified,true);
  }
});
test('源空目录删除响应丢失只读核实恢复，不重发删除；目录仍在则保持待核实',async()=>{
  for(const actuallyDeleted of [true,false]) {
    const {f,rows}=mergeFixture(),p=await A.plan(rows,f.list,'delete');let calls=0;const remove=f.api.removeEmpty;
    f.api.removeEmpty=async(...args)=>{calls++;if(actuallyDeleted)await remove(...args);throw Error('response lost');};
    await assert.rejects(A.execute(p,f.api));assert.equal(f.journal.cleanup.state,'pending');
    if(actuallyDeleted)assert.equal((await A.reconcile(rows,f.journal,f.list)).verified,true);
    else {await assert.rejects(A.reconcile(rows,f.journal,f.list));await assert.rejects(A.finishEmpty(f.journal,f.api),/不重复删除/);}
    assert.equal(calls,1);
  }
});
