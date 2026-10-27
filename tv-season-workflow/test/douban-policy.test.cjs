// 使用虚拟时钟验证预算、缓存、冷却及搜索/详情调度，无网络或真实等待。
const {test}=require('node:test'),assert=require('node:assert/strict');
const C=require('../season-inspect.user.js');
const show={title:'测试剧',year:'2025',tmdb:'123'},url='https://movie.douban.com/subject/123/';
const reply={title:'测试剧 (2025)',url,total:2,aliases:[],releaseYears:['2025'],broadcast:'未确认',checkedAt:'2026-10-21T00:00:00Z'};
function fixture() {
  let time=Date.UTC(2026,9,21),pages=[],stopped=false;
  const values=new Map(),get=(k,d)=>values.has(k)?structuredClone(values.get(k)):d,put=(k,v)=>values.set(k,structuredClone(v));
  const policy=C.createDoubanPolicy(C,{get,put,clock:()=>time});
  const options={clock:()=>time,ready:()=>true,checkpoint(){if(stopped)throw Object.assign(Error('paused'),{name:'Paused'});},sleep:async ms=>{time+=ms;},message(){},page:async(u)=>{pages.push(u);return u.includes('/subject/')?reply:{nextUrl:url,candidates:[{title:reply.title,url,tv:true}]};}};
  return {policy,options,values,pages,get,put,advance:ms=>time+=ms,stop:()=>{stopped=true;},query:C.createDoubanQuery(C,policy,options),time:()=>time,reload:()=>C.createDoubanPolicy(C,{get,put,clock:()=>time})};
}
test('逐页 45 秒、每 20 页休息、40/小时与 200/天额度重建实例后仍有效',()=>{
  const f=fixture(),first=f.time();
  assert.equal(f.policy.reserve().allowed,true);assert.equal(f.reload().reserve().allowed,false);assert.equal(f.policy.status().next-first,45000);
  for(let n=1;n<200;n++){f.advance(f.policy.status().next-f.time());assert.equal(f.policy.reserve().allowed,true,n);if(n===19)assert.equal(f.policy.status().next-f.time(),600000);if(n===39)assert.ok(f.policy.status().next>=first+3600000);}
  assert.equal(f.reload().status().day,200);assert.ok(f.policy.status().next>=first+86400000);assert.equal(f.reload().reserve().allowed,false);
});
test('风险冷却至少 30 分钟且到期不自动解锁，恢复不清空访问记录',()=>{
  const f=fixture();f.policy.reserve();f.policy.block('验证码');assert.throws(()=>f.policy.resume(),/冷却/);
  f.advance(1800001);assert.equal(f.reload().reserve().allowed,false);f.reload().resume();assert.equal(f.policy.status().day,1);assert.equal(f.policy.reserve().allowed,true);
});
test('子页风控信号在主页面中断后仍生效，人工恢复只确认当前信号',()=>{
  const f=fixture();f.put('douban-risk-signal-v1',{at:f.time(),until:f.time()+1800000,reason:'异常请求'});
  assert.equal(f.reload().status().hold,true);f.advance(1800000);assert.equal(f.policy.reserve().allowed,false);f.policy.resume();assert.equal(f.policy.status().hold,false);
  f.advance(1);f.put('douban-risk-signal-v1',{at:f.time(),until:f.time()+1800000,reason:'再次受限'});assert.equal(f.policy.status().hold,true);
});
test('缓存 TTL 分别为已播完 30 天、连载 6 小时、未知 24 小时；错季/错年拒绝',()=>{
  for(const [broadcast,hours] of [['已播完',720],['连载中',6],['未确认',24]]) {
    const f=fixture();f.policy.remember(show,1,{...reply,broadcast});f.advance(hours*3600000-1);assert.ok(f.policy.cached(show,1));f.advance(2);assert.equal(f.policy.cached(show,1),null);assert.equal(f.policy.route(show,1),url);
  }
  const f=fixture();assert.throws(()=>f.policy.remember(show,2,reply),/校验/);assert.throws(()=>f.policy.remember({...show,year:'2024'},1,reply),/校验/);
  f.policy.remember(show,1,reply);assert.equal(f.policy.cached(show,2),null);assert.equal(f.policy.cached(show,1,{total:3}),null);
  const k='douban-entry-v1:'+f.policy.key(show,1),stored=f.get(k);delete stored.expires;f.put(k,stored);assert.equal(f.policy.cached(show,1),null);
});
test('初扫仅打开搜索页，详情受同一额度约束；补查直达详情，之后命中缓存零访问',async()=>{
  const f=fixture();const first=await f.query.query(show,1);assert.equal(first.deferred,true);assert.equal(f.pages.length,1);assert.equal(f.policy.route(show,1),url);
  const second=await f.query.query(show,1,{wait:true});assert.equal(second.total,2);assert.equal(f.pages.length,2);assert.equal(f.pages[1],url);assert.ok(f.time()>=Date.UTC(2026,9,21)+45000);
  const hit=await f.query.query(show,1);assert.equal(hit.fromCache,true);assert.equal(hit.checkedAt,reply.checkedAt);assert.equal(f.pages.length,2);
  f.policy.block('受限');assert.equal((await f.query.query(show,1)).fromCache,true);assert.equal((await f.query.query(show,1,{force:true,wait:true})).deferred,true);assert.equal(f.pages.length,2);
});
test('排队等待不调用页面读取，暂停立即退出；强制刷新仍按逐页间隔',async()=>{
  const f=fixture();f.policy.remember(show,1,reply);f.policy.reserve();f.stop();await assert.rejects(f.query.query(show,1,{wait:true,force:true}),{name:'Paused'});assert.equal(f.pages.length,0);
  const g=fixture();g.policy.remember(show,1,reply);g.policy.reserve();const r=await g.query.query(show,1,{wait:true,force:true});assert.equal(r.fromCache,false);assert.equal(g.pages.length,1);assert.equal(g.policy.status().day,2);
});
test('普通网络错误只重试一次，无响应则立即冷却；错误不写入成功缓存',async()=>{
  const f=fixture();let calls=0;f.options.page=async()=>{calls++;return {errorKind:'network',error:'网络失败'};};const q=C.createDoubanQuery(C,f.policy,f.options);
  assert.equal((await q.query(show,1,{wait:true})).deferred,true);assert.equal(calls,2);assert.equal(f.policy.cached(show,1),null);
  await q.query(show,1,{wait:true});assert.equal(calls,2);
  const g=fixture();g.options.page=async()=>{throw Error('timeout');};const other=C.createDoubanQuery(C,g.policy,g.options);
  await other.query(show,1);assert.equal(g.policy.status().hold,true);
});
test('无匹配 24 小时内不自动重查，手工绑定新链接仍需经过详情验证',async()=>{
  const f=fixture();f.options.page=async()=>({error:'无匹配'});const q=C.createDoubanQuery(C,f.policy,f.options);
  await q.query(show,1);await q.query(show,1);assert.equal(f.policy.status().day,1);
  f.policy.bind(show,1,url);f.options.page=async()=>({...reply,title:'错误电视剧 (2025)'});const q2=C.createDoubanQuery(C,f.policy,f.options);
  const result=await q2.query(show,1,{wait:true});assert.equal(result.deferred,true);assert.equal(f.policy.cached(show,1),null);
});
test('缓存保留已核实别名和季身份，结果缺失不会伪造成 0 集',async()=>{
  const f=fixture(),s={title:'宇宙Marry Me？',year:'2025'};
  f.policy.remember(s,1,{...reply,title:'宇宙嫁给我 우주메리미 (2025)',total:12,aliases:['宇宙Marry Me?(台)']});assert.equal(f.policy.cached(s,1).total,12);
  f.options.page=async()=>undefined;const q=C.createDoubanQuery(C,f.policy,f.options);const r=await q.query(show,1);assert.equal(r.total,null);assert.equal(r.deferred,true);assert.equal(f.policy.status().hold,true);
});
