/* 初检查询协调器：搜索/详情分别预留额度，排队时间不计入页面加载超时。
 * 初次云盘扫描遇到限速直接排入待补查，不阻塞其他剧集；补查可等待普通额度。
 * 风控/登录暂停必须人工恢复；强制查询仅跳过缓存，不绕过限额或冷却。
 */
(function createDoubanQuery(C,policy,options) {
  'use strict';
  const {page,ready,checkpoint,sleep,message,clock=()=>Date.now()}=options;
  function pending(reason,next=0) {return {source:'douban',deferred:true,total:null,error:'豆瓣待补查：'+reason,retryAt:next||null};}
  async function slot(wait) {
    while(true) {
      checkpoint();const s=policy.reserve();if(s.allowed)return null;
      if(s.hold||!wait)return pending(s.reason||'等待人工恢复',s.next);
      message('豆瓣限速等待至 '+new Date(s.next).toLocaleString('zh-CN')+'；可随时暂停');
      await sleep(Math.min(1000,Math.max(1,s.next-clock())));
    }
  }
  async function query(show,season,{wait=false,force=false,reference=null,boundUrl=null}={}) {
    checkpoint();
    if(boundUrl&&!C.validSubjectUrl(boundUrl))return pending('指定的豆瓣详情链接无效');
    if(!force&&!boundUrl) {const hit=policy.cached(show,season,reference);if(hit)return hit;const old=policy.failed(show,season);if(old)return pending(old.error+'；本条目 24 小时内不重复自动查询',old.retryAt);}
    const state=policy.status();if(state.hold)return pending(state.reason,state.next);
    if(!ready())return pending('请先完成豆瓣登录准备并勾选可访问');
    let url=boundUrl||policy.route(show,season)||`https://search.douban.com/movie/subject_search?search_text=${encodeURIComponent(show.title+(season>1?' 第'+season+'季':''))}&cat=1002`;
    let retries=0;
    for(let stage=0;stage<4;stage++) {
      const deferred=await slot(wait);if(deferred)return deferred;
      checkpoint();let reply;
      try{reply=await page(url,{show,season,manual:false,paced:true});}catch(e){if(e.name==='Paused')throw e;reply={blocked:true,error:'查询页没有确定响应，请检查登录、验证或脚本权限后恢复'};}
      if(!reply||typeof reply!=='object')reply={blocked:true,error:'豆瓣查询页返回异常，请检查后人工恢复'};
      if(reply.blocked) {
        const st=policy.block(reply.error||'豆瓣访问受限',{login:reply.blockedKind==='login',until:reply.retryAt||0});
        return pending(st.reason,st.next);
      }
      if(reply.errorKind==='network'&&retries++===0) {message('豆瓣页面网络异常，仅延迟重试一次');for(let n=0;n<60;n++){checkpoint();await sleep(1000);}continue;}
      if(reply.nextUrl) {
        if(!C.validSubjectUrl(reply.nextUrl)||!C.chooseCandidate(reply.candidates||[],show,season,{inspectDetails:true})||C.chooseCandidate(reply.candidates,show,season,{inspectDetails:true}).url!==reply.nextUrl) {
          policy.failure(show,season,'搜索候选身份不明');return pending('搜索候选身份不明');
        }
        policy.candidate(show,season,reply.nextUrl);url=reply.nextUrl;continue;
      }
      if(!policy.valid(reply,show,season)) {const reason=reply.error||'详情身份或集数未通过校验';policy.failure(show,season,reason);return pending(reason,clock()+86400000);}
      return policy.remember(show,season,reply,reference);
    }
    policy.failure(show,season,'查询阶段未能完成');return pending('查询阶段未能完成');
  }
  return {query};
})
