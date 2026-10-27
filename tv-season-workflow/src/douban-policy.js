/* 初检专用的持久缓存与访问预算。不存登录信息，不与第三脚本共用存储。
 * 所有页面额度由云盘主队列在同源互斥锁内预留；豆瓣子页不能自行跳转消耗第二页。
 * 时间和额度是保守设计值，不是豆瓣承诺的安全阈值。缓存从不代替云盘文件重扫。
 */
(function createDoubanPolicy(C,options) {
  'use strict';
  const {get,put,clock=()=>Date.now()}=options;
  const MINUTE=60000,HOUR=60*MINUTE,DAY=24*HOUR;
  const limits={gap:45000,batch:20,rest:10*MINUTE,hour:40,day:200,cooldown:30*MINUTE};
  const key=(show,season)=>JSON.stringify([show.tmdb||C.norm(show.title),show.year||'',season]);
  function gate() {
    const s=get('douban-budget-v1',{pages:[],batch:0,restUntil:0,hold:false,holdUntil:0,reason:''});
    if(!Array.isArray(s.pages)||s.pages.some(t=>!Number.isFinite(t)||t<0)||!Number.isInteger(s.batch)||s.batch<0||s.batch>20||['restUntil','holdUntil','ackAt'].some(k=>s[k]!=null&&(!Number.isFinite(s[k])||s[k]<0)))throw Error('豆瓣访问记录异常，请保留记录并检查，不能重置额度后重试');
    return {...s,pages:s.pages.filter(t=>t>clock()-DAY).sort((a,b)=>a-b)};
  }
  function status() {
    const s=gate(),time=clock(),hour=s.pages.filter(t=>t>time-HOUR);
    const signal=get('douban-risk-signal-v1',null),unacked=signal&&signal.at>(s.ackAt||0);
    let next=Math.max(time,s.pages.length?s.pages.at(-1)+limits.gap:0,s.restUntil||0);
    if(hour.length>=limits.hour)next=Math.max(next,hour[hour.length-limits.hour]+HOUR);
    if(s.pages.length>=limits.day)next=Math.max(next,s.pages[s.pages.length-limits.day]+DAY);
    const holdUntil=Math.max(s.holdUntil||0,unacked?signal.until||0:0);
    return {hold:!!s.hold||!!unacked,reason:unacked?signal.reason:s.reason||'',holdUntil,next:Math.max(next,holdUntil),hour:hour.length,day:s.pages.length};
  }
  function reserve() {
    const st=status();if(st.hold)return {...st,allowed:false};if(st.next>clock())return {...st,allowed:false,reason:'豆瓣逐页限速或访问额度等待'};
    const s=gate(),time=clock();if(s.restUntil&&s.restUntil<=time){s.batch=0;s.restUntil=0;}
    s.pages.push(time);s.batch++;if(s.batch===limits.batch)s.restUntil=time+limits.rest;
    put('douban-budget-v1',s);return {allowed:true,at:time};
  }
  function block(reason,{login=false,until=0}={}) {
    const s=gate();s.hold=true;s.reason=reason;s.holdUntil=Math.max(s.holdUntil||0,until,clock()+(login?0:limits.cooldown));put('douban-budget-v1',s);return status();
  }
  function resume() {
    const s=gate();if(clock()<status().holdUntil)throw Error('豆瓣冷却尚未结束，不能提前恢复');
    s.hold=false;s.reason='';s.holdUntil=0;s.ackAt=get('douban-risk-signal-v1',null)?.at||s.ackAt||0;put('douban-budget-v1',s);
  }
  const entry=(show,season)=>get('douban-entry-v1:'+key(show,season),null);
  const write=(show,season,value)=>put('douban-entry-v1:'+key(show,season),value);
  function valid(reply,show,season) {
    return !!reply&&!reply.error&&Number.isInteger(reply.total)&&reply.total>0&&reply.total<=10000&&!!C.validSubjectUrl(reply.url)&&C.candidateMatches({...reply,tv:true},show,season);
  }
  function cached(show,season,reference) {
    const e=entry(show,season);if(e?.rules!==1||!Number.isFinite(e.expires)||e.expires<=clock()||!valid(e.reply,show,season))return null;
    if(reference?.total&&reference.total!==e.reply.total&&reference.total!==e.referenceTotal)return null;
    if(reference?.broadcast&&reference.broadcast!=='未确认'&&e.referenceBroadcast&&e.referenceBroadcast!==reference.broadcast)return null;
    if(reference?.broadcast&&reference.broadcast!=='未确认'&&e.reply.broadcast&&e.reply.broadcast!=='未确认'&&reference.broadcast!==e.reply.broadcast)return null;
    return {...e.reply,fromCache:true,cacheNote:'使用本地豆瓣缓存；来源查询时间为原始查询时间'};
  }
  function remember(show,season,reply,reference) {
    if(!valid(reply,show,season))throw Error('豆瓣结果未通过剧名、年份、季号及数量校验，不能缓存');
    const ended=reply.broadcast==='已播完'||(reference?.completionConfirmed&&reference.total===reply.total);
    const ongoing=['连载中','待播'].includes(reply.broadcast)||['连载中','待播'].includes(reference?.broadcast);
    const ttl=ongoing?6*HOUR:ended?30*DAY:DAY;
    // 白名单存储公开元数据；不缓存整页、账号信息或错误对象。
    const clean={};for(const k of ['total','title','url','checkedAt','aliases','releaseYears','japanese','broadcast','broadcastEvidence','matchNote'])if(reply[k]!==undefined)clean[k]=reply[k];
    if(!Number.isFinite(Date.parse(clean.checkedAt)))clean.checkedAt=new Date(clock()).toISOString();
    clean.source='douban';write(show,season,{rules:1,reply:clean,expires:clock()+ttl,url:reply.url,referenceTotal:reference?.total||null,referenceBroadcast:reference?.broadcast||null});
    return {...clean,fromCache:false};
  }
  function route(show,season) {
    const e=entry(show,season);return e?.rules===1&&C.validSubjectUrl(e.url)?e.url:null;
  }
  function candidate(show,season,url) {
    if(!C.validSubjectUrl(url))throw Error('候选详情链接无效');
    write(show,season,{rules:1,url}); // 新候选清除旧失败状态，但不等于通过详情验证的缓存。
  }
  function failure(show,season,error) {
    write(show,season,{rules:1,error:String(error),retryAt:clock()+DAY});
  }
  function failed(show,season) {const e=entry(show,season);return e?.error&&e.retryAt>clock()?e:null;}
  function bind(show,season,url) {if(!C.validSubjectUrl(url))throw Error('豆瓣详情链接无效');write(show,season,{rules:1,url});}
  return {limits,key,status,reserve,block,resume,cached,remember,route,candidate,failure,failed,bind,valid};
})
