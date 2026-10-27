const {test}=require('node:test'),assert=require('node:assert/strict');
const D=require('../season-download.user.js'),R=D.Repair,S=D.RepairState;
function fixture(){
 const config={source:'/video/downloads/电视剧',target:'/video/link/电视剧'};
 const task={key:'one',tmdb:'123',category:'韩国剧集',name:'测试剧 (2025) [tmdbid=123]',title:'测试剧',season:1,needed:[2],records:[{id:'hash',state:'accepted'}],data:{扫描时间:new Date().toISOString()}};
 const src=config.source+'/测试剧.S01E02.mkv',target=config.target+'/'+task.category+'/'+task.name+'/Season 1/测试剧 - S01E02.mkv';
 const sourceFile={storage:'local',type:'file',path:src,name:'测试剧.S01E02.mkv',size:100,modify_time:1000};
 const h={id:9,src,dest:target,src_storage:'local',dest_storage:'local',src_fileitem:sourceFile,dest_fileitem:{...sourceFile,path:target},type:'电视剧',title:task.title,year:'2025',tmdbid:123,seasons:'S01',episodes:'E02',status:true,download_hash:'hash',mode:'copy'};
 const tree=new Map();const parent=p=>p.slice(0,p.lastIndexOf('/'))||'/';
 function mkdir(p){if(tree.has(p))return;tree.set(p,[]);if(p!=='/'){mkdir(parent(p));tree.get(parent(p)).push({storage:'local',type:'dir',path:p,name:p.split('/').pop()});}}
 mkdir(config.source);mkdir(parent(target));tree.get(config.source).push(sourceFile);
 let histories=[h],queue=[],active=[],saved=null;const calls=[];
 const api={version:async()=>{},histories:async()=>structuredClone(histories),list:async p=>structuredClone(tree.get(p)||[]),active:async()=>active,queue:async()=>queue,
 preview:async req=>{calls.push(['preview',req]);return {success:true,data:{summary:{total:1,success:1,failed:0},items:[{success:true,source:src,target,type:'电视剧',season:1,episode:2}]}};},
 removeHistory:async id=>{calls.push(['delete',id]);histories=[];return {success:true};},submit:async req=>{calls.push(['submit',req]);return {success:true};}};
 return {config,task,h,src,target,sourceFile,tree,api,calls,setHistories:v=>histories=v,setQueue:v=>queue=v,setActive:v=>active=v,save:r=>saved=structuredClone(r),get saved(){return saved;}};
}
test('检查只查询和预览；执行重新核对后只解除指定历史，再提交当前单集复制整理',async()=>{
 const f=fixture(),p=await R.check(f.task,f.api,f.config);assert.equal(p.state,'ready');assert.equal(p.items[0].id,9);assert.ok(f.calls.every(c=>c[0]==='preview'));
 const r=await R.execute(f.task,p,f.api,f.config,{save:f.save});assert.equal(r.state,'submitted');assert.deepEqual(f.calls.filter(c=>c[0]!=='preview').map(c=>c[0]),['delete','submit']);
 const req=f.calls.find(c=>c[0]==='submit')[1];assert.equal(req.logid,undefined);assert.equal(req.reorganize,false);assert.equal(req.transfer_type,'copy');assert.equal(req.fileitems.length,1);assert.equal(req.scrape,false);assert.equal(r.items[0].history.id,9);
});
test('报告混版身份未确认时拒绝用旧历史文件补入新版本',async()=>{
 const f=fixture();f.task.data['重复集号']='1';await assert.rejects(R.check(f.task,f.api,f.config),/混版/);assert.equal(f.calls.length,0);
});
test('旧报告、错误媒体身份、同集目标、非唯一历史、在途任务和预览误识别均不进入修复清单',async()=>{
 const stale=fixture();stale.task.data.扫描时间='2020-01-01';await assert.rejects(R.check(stale.task,stale.api,stale.config),/24小时/);
 for(const variant of ['tmdb','source','target','duplicate','queue','active','preview','changed','identity']){
  const f=fixture();
  if(variant==='tmdb')f.setHistories([{...f.h,tmdbid:456}]);
  if(variant==='source')f.tree.get(f.config.source).length=0;
  if(variant==='target')f.tree.get(f.target.slice(0,f.target.lastIndexOf('/'))).push({...f.sourceFile,path:f.target,name:'测试剧.S01E02.mkv'});
  if(variant==='duplicate')f.setHistories([f.h,{...f.h,id:10}]);
  if(variant==='queue')f.setQueue([{tasks:[{fileitem:{path:f.src}}]}]);
  if(variant==='active')f.setActive([{hash:'hash'}]);
  if(variant==='preview')f.api.preview=async()=>({success:true,data:{summary:{total:1,success:1,failed:0},items:[{success:true,source:f.src,target:f.target,type:'电视剧',season:2,episode:2}]}});
  if(variant==='identity')f.h.src_fileitem={...f.h.src_fileitem,storage:'u115'};
  if(variant==='changed'){const preview=f.api.preview;f.api.preview=async r=>{const out=await preview(r);f.tree.get(f.config.source)[0].size++;return out;};}
  const p=await R.check(f.task,f.api,f.config);assert.equal(p.state,'blocked',variant);assert.equal(p.items.length,0,variant);assert.ok(f.calls.every(c=>c[0]==='preview'));
 }
});
test('执行前源变化不删除历史；删除响应丢失保留未知记录，不提交整理',async()=>{
 const f=fixture(),p=await R.check(f.task,f.api,f.config);f.tree.get(f.config.source)[0].size++;
 await assert.rejects(R.execute(f.task,p,f.api,f.config,{save:f.save}),/变化/);assert.equal(f.calls.filter(c=>c[0]==='delete').length,0);
 const g=fixture(),q=await R.check(g.task,g.api,g.config);g.api.removeHistory=async()=>{assert.equal(g.saved.state,'history-delete-pending');throw Error('响应丢失');};
 await assert.rejects(R.execute(g.task,q,g.api,g.config,{save:g.save}));assert.equal(g.saved.state,'uncertain');assert.equal(g.calls.filter(c=>c[0]==='submit').length,0);assert.ok(S.uncertain(g.saved));
});
test('删除后自动整理抢先产生历史或目标时，停止后续提交',async()=>{
 for(const variant of ['history','target']){const f=fixture(),p=await R.check(f.task,f.api,f.config),remove=f.api.removeHistory;
 f.api.removeHistory=async id=>{await remove(id);if(variant==='history')f.setHistories([{...f.h,id:10}]);else f.tree.get(f.target.slice(0,f.target.lastIndexOf('/'))).push({...f.sourceFile,path:f.target});return {success:true};};
 await assert.rejects(R.execute(f.task,p,f.api,f.config,{save:f.save}));assert.equal(f.calls.filter(c=>c[0]==='submit').length,0);assert.equal(f.saved.state,'uncertain');}
});
test('只读核实需新的成功历史及匹配目标；第三脚本只能在实际集号到位后解除归档阻挡',async()=>{
 const f=fixture(),p=await R.check(f.task,f.api,f.config);await R.execute(f.task,p,f.api,f.config,{save:f.save});
 await assert.rejects(R.reconcile(f.task,f.saved,f.api,f.config));f.setHistories([{...f.h,id:10}]);f.tree.get(f.target.slice(0,f.target.lastIndexOf('/'))).push({...f.sourceFile,path:f.target});
 const r=await R.reconcile(f.task,f.saved,f.api,f.config);assert.equal(r.state,'submitted');assert.ok(S.pending(S.verify(r,[1],new Date().toISOString())));assert.equal(S.verify(r,[1,2],new Date().toISOString()).state,'files-verified');
 assert.equal(S.verify({...r,state:'uncertain'},[1,2],new Date().toISOString()).state,'uncertain');
});
test('适配器只删历史，明确传两个false；预览/执行不触发全局整理或文件删除',async()=>{
 const calls=[];const raw={defaults:{baseURL:'/api/v1/'},get:async(p,c)=>{calls.push(['get',p,c]);return {success:true,data:{BACKEND_VERSION:'v2.15.6'}};},post:async(p,b)=>{calls.push(['post',p,b]);return {success:true};},delete:async(p,c)=>{calls.push(['delete',p,c]);return {success:true};}};
 const api=R.adapter(raw,'https://mp.example');await api.version();await api.removeHistory(9);
 assert.deepEqual(calls[1],['delete','history/transfer',{params:{deletesrc:false,deletedest:false},data:{id:9},timeout:30000}]);
 raw.get=async()=>({success:true,data:{BACKEND_VERSION:'v3.0.0'}});await assert.rejects(api.version(),/v2.15.6/);
});
test('历史分页重复、总量变化或接口失败均拒绝按不完整历史修复',async()=>{
 for(const variant of ['duplicate','total','failure']){
 const raw={get:async(_p,c)=>variant==='failure'?{success:false}:({success:true,data:{total:variant==='total'&&c.params.page===2?3:2,list:[{id:1}]}}),post(){},delete(){}};
 await assert.rejects(R.adapter(raw,'https://mp.example').histories('测试剧'));
 }
});
