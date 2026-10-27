/* 资源覆盖是检索快照，不是完整播出表。按同剧同季的来源日期确认连载，不以资源站最高集号猜已播范围。 */
(function createBroadcastAudit() {
  'use strict';
  const validDate=s=>/^\d{4}-\d{2}-\d{2}$/.test(s||'')&&Number.isFinite(Date.parse(s+'T00:00:00Z'))&&new Date(s+'T00:00:00Z').toISOString().slice(0,10)===s;
  function assess(task,coverage,evidence,today=new Date(Date.now()+8*3600000).toISOString().slice(0,10)) {
    const max=coverage.length?Math.max(...coverage):null,at=new Date().toISOString();
    const audit={maximum:max,coverage,at,status:'未复核',basis:'',sourceUrl:task.tmdb?'https://www.themoviedb.org/tv/'+task.tmdb+'/season/'+task.season:'',confirmed:false,exclude:[]};
    if(max==null||max>=task.total){audit.basis=max==null?'本轮未取得可确认的本季资源集号':'未出现资源站最高集号低于登记总数的差异';return audit;}
    audit.status='疑似连载，待核实';audit.basis='资源站最高集号 '+max+'，来源登记 '+task.total+' 集；资源不全不能证明仍在播出';
    if(evidence?.error||!evidence?.media||!Array.isArray(evidence?.episodes)) {
      if(task.data?.['播出状态']==='连载中'){audit.status='连载中';audit.confirmed=true;audit.basis+='；沿用原报告已确认的连载结论（本轮未刷新来源）';}
      else if(task.data?.['播出状态']==='已播完'){audit.status='已播完，资源不全';audit.basis+='；原报告明确已播完，不能据此改判连载';}
      audit.basis+='；'+(evidence?.error||'本轮来源信息不可用');return audit;
    }
    const list=evidence.episodes,m=evidence.media;
    if(list.some(e=>!Number.isInteger(e.episode_number)||e.episode_number<1||e.episode_number>task.total||e.season_number!==task.season)||new Set(list.map(e=>e.episode_number)).size!==list.length){audit.status='来源集目待核实';audit.basis+='；来源季号、集数范围或重复集目与报告不一致';return audit;}
    const aired=list.filter(e=>validDate(e.air_date)&&e.air_date<=today),future=list.filter(e=>validDate(e.air_date)&&e.air_date>today),unknown=list.filter(e=>!validDate(e.air_date));
    audit.exclude=[...future,...unknown].map(e=>e.episode_number);
    const next=m.next_episode_to_air,upcoming=next&&next.season_number===task.season&&Number.isInteger(next.episode_number)&&next.episode_number>=1&&next.episode_number<=task.total&&validDate(next.air_date)&&next.air_date>today;
    if(upcoming&&!audit.exclude.includes(next.episode_number))audit.exclude.push(next.episode_number);
    const ended=m.status==='Ended'||m.status==='Canceled';
    if(ended&&(future.length||upcoming)){audit.status='来源状态冲突，待核实';audit.basis+='；结束状态与未来播出日期冲突';return audit;}
    if(aired.length&&(future.length||upcoming)) {audit.status='连载中';audit.confirmed=true;audit.basis+='；TMDB本季已有已播集，且'+(future.length?'存在 '+future.length+' 集未来播出日期':'下一集 E'+next.episode_number+' 定于 '+next.air_date+' 播出');return audit;}
    const finale=aired.some(e=>e.episode_number===task.total&&e.episode_type==='finale');
    if(list.length===task.total&&aired.length===task.total&&(ended||finale)){audit.status='已播完，资源不全';audit.confirmed=true;audit.basis+='；本季完整集目均已到播出日，并有结束状态或季终标记';return audit;}
    if(ended){audit.status='已结束，资源或登记集数待核实';audit.basis+='；来源标记已结束/取消，但本季集目不足以核实报告总数，不改判连载';return audit;}
    if(!aired.length&&future.length){audit.status='待播';audit.basis+='；本季尚无已到播出日的集目';return audit;}
    if(task.data?.['播出状态']==='已播完'){audit.status='已播完，资源不全';audit.basis+='；保留原报告已播完结论，本轮没有连载证据';}
    else audit.basis+='；未取得本季明确连载证据，不改蓝';
    return audit;
  }
  return {assess};
})
