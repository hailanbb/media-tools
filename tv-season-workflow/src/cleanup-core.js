/* 第三阶段的纯判断及删除状态机，不直接操作页面。
 * “完整”指豆瓣本季 1..N 集号均有可识别视频，不代表已校验视频内容或播放质量。
 * 版本按保留技术/发布信息的文件名模板分组，禁止把不同版本拼成一个完整版本。
 * 修改时间仅用于同一版本中少量重复副本的取舍；多个完整版本（含多套完整副本）必须人工处理。
 */
(function createCleanupCore(C) {
  'use strict';
  const MEDIA=/\.(?:mp4|mkv|avi|mov|m4v|ts|m2ts|wmv|flv|webm|mpg|mpeg|strm)$/i;
  const SCRAPE=/\.(?:nfo|jpg|jpeg|png|webp|avif|bmp|gif|tbn)$/i;
  const SHARED=/^(?:tvshow|season(?:\d+)?|movie|poster|fanart|folder|banner|logo|clearlogo|clearart|landscape|disc|season(?:\d+)?[-_.](?:poster|fanart|banner|landscape))$/i;
  const range=n=>Array.from({length:n},(_,i)=>i+1);
  const uniq=a=>[...new Set(a)].sort((a,b)=>a-b);
  const stem=name=>name.replace(/\.[^.]+$/,'');
  const stamp=r=>({path:r.path,name:r.name,directory:!!r.directory,modified:String(r.modified||''),size:String(r.size||'')});
  const snapshot=rows=>rows.map(stamp).sort((a,b)=>a.path.localeCompare(b.path));
  const equal=(a,b)=>JSON.stringify(snapshot(a))===JSON.stringify(snapshot(b));
  function verifySource(task,source) {
    if(source?.error||!Number.isInteger(source?.total)||source.total<1||source.total>10000||!C.validSubjectUrl(source?.url))throw Error('豆瓣未提供可确认的本季集数或详情链接');
    if(!C.candidateMatches({...source,tv:true},C.showInfo(task.name),task.season))throw Error('豆瓣详情标题、年份或季号与本季不匹配');
    return {...source,source:'douban'};
  }
  function completionEvidence(task,db) {
    if(db.broadcast==='已播完')return true;
    if(db.broadcast==='连载中'||db.broadcast==='待播')return false;
    // 只继承初检同数量的“本季已播完”结论；豆瓣数字本身不是播完证据。
    return task.data?.['播出状态']==='已播完'&&Number(task.data?.['来源登记集数'])===db.total;
  }
  function versionOf(row,season) {
    if(/\.strm$/i.test(row.name))return null; // 不把播放指针当作经过验证的视频副本。
    const ext=/\.([^.]+)$/.exec(row.name)?.[1].toLowerCase();if(!ext)return null;
    let name=stem(row.name);
    // 仅清除明确的系统“副本”后缀，不把普通 (1)、数字发布版本或 REPACK 混为一类。
    name=name.replace(/(?:\s+-\s+副本(?:\s*\d+)?|\s*\((?:copy|副本)(?:\s+\d+)?\))$/i,'');
    const episode=C.episodeFromName(name+'.'+ext,season);if(!episode)return null;
    let template=name.replace(/(?:^|[^a-z0-9])S(\d{1,3})[ ._-]*E(\d{1,4})(?!\d)/ig,m=>m.replace(/S\d+[ ._-]*E\d+/i,`S${season}E{集}`));
    if(template===name)template=name.replace(/第\s*\d{1,4}\s*集/g,'第{集}集').replace(/(?:^|[\s._-])(?:EP?|Episode)[ ._-]*\d{1,4}(?!\d)/ig,m=>m.replace(/(?:EP?|Episode)[ ._-]*\d+/i,'E{集}'));
    if(/^\d+$/.test(name))template='{集}';
    const tail=/\s+-\s+(\d{1,4})$/.exec(template);
    if(tail&&Number(tail[1])===episode)template=template.slice(0,tail.index)+' - {集}';
    if(template===name)return null;
    const key=template.normalize('NFKC').replace(/\s+/g,' ').trim().toLowerCase()+'.'+ext;
    return {episode,key,label:template+'.'+ext,time:C.modifiedTime(row.modified),row};
  }
  function sidecarOwners(file,videos) {
    if(!SCRAPE.test(file.name)||SHARED.test(stem(file.name)))return [];
    const base=stem(file.name).toLowerCase();
    return videos.filter(v=>{
      const s=stem(v.name).toLowerCase();
      return base===s||(!/\.nfo$/i.test(file.name)&&['-thumb','-poster','-fanart','-banner','-landscape'].some(t=>base===s+t));
    });
  }
  function plan(task,rows,source,id) {
    const db=verifySource(task,source);
    C.validateListing(rows,task.path,rows.length,{emptyConfirmed:true});
    const videos=rows.filter(r=>!r.directory&&MEDIA.test(r.name)),parsed=videos.map(r=>versionOf(r,task.season));
    const found=uniq(parsed.filter(Boolean).map(v=>v.episode)),missing=range(db.total).filter(n=>!found.includes(n));
    const result={id,taskKey:task.key,path:task.path,season:task.season,showName:task.name,total:db.total,source:db,
      snapshot:snapshot(rows),keep:[],remove:[],groups:[],preserved:[],issues:[],videoCount:videos.length,current:found.length,episodes:found,missing,
      duplicates:found.filter(n=>parsed.filter(p=>p?.episode===n).length>1),unknown:videos.filter((_v,i)=>!parsed[i]).map(v=>v.name),
      confirmedEnded:completionEvidence(task,db),state:'待核实',eligible:false,complete:false};
    if(rows.some(r=>r.directory))result.issues.push('季内存在子目录，不能递归猜测版本或删除目录');
    if(rows.some(r=>/\.(?:part|tmp|download|crdownload|!qb)$/i.test(r.name)))result.issues.push('发现临时下载文件，等待下载完成');
    if(parsed.some(v=>!v)){result.issues.push('存在无法确定集号或版本模板的视频');result.missing=null;}
    if(parsed.some(v=>v&&(v.episode>db.total||!Number.isFinite(v.time))))result.issues.push('视频集号超出豆瓣范围或修改时间无效');
    if(videos.some(v=>!/^\d+(?:\.\d+)?\s*(?:B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$/i.test(v.size||'')||parseFloat(v.size)<=0))result.issues.push('视频大小为零或无法读取，不能认定为有效完整副本');
    if(result.issues.length)return result;
    const groups=new Map();
    for(const p of parsed){if(!groups.has(p.key))groups.set(p.key,{key:p.key,label:p.label,files:[],episodes:[]});groups.get(p.key).files.push(p);}
    for(const g of groups.values()) {
      g.episodes=uniq(g.files.map(v=>v.episode));g.missing=range(db.total).filter(n=>!g.episodes.includes(n));g.complete=!g.missing.length;
      g.latestModified=g.files.reduce((a,v)=>!a||v.time>a.time?v:a,null)?.row.modified||'';
      result.groups.push({key:g.key,label:g.label,episodes:g.episodes,missing:g.missing,complete:g.complete,latestModified:g.latestModified,files:g.files.map(v=>stamp(v.row))});
    }
    const complete=[...groups.values()].filter(g=>g.complete);
    if(complete.length>1){result.state='多个完整版本，人工判断';result.issues.push('不按日期自动淘汰任何完整版本');return result;}
    if(!complete.length){result.state=missing.length?'缺集，保留黄色':'混版拼齐，人工判断';result.issues.push('没有一个独立版本覆盖豆瓣本季全部集号');return result;}
    const best=complete[0];
    if(groups.size>1&&!/\b(?:(?:2160|1080|720|480)p|web[ ._-]?dl|blu[ ._-]?ray|hdtv|x26[45]|h[ ._-]?26[45]|hevc)\b/i.test(best.label)) {
      result.state='完整文件组缺少版本标识，人工判断';result.issues.push('简名文件虽覆盖全部集号，但无法证明来自同一版本，不据此删除其他版本');return result;
    }
    if(range(db.total).every(n=>best.files.filter(v=>v.episode===n).length>=2)) {
      result.state='多套完整副本，人工判断';result.issues.push('同一模板可组成多套完整副本，不按日期自动删除');return result;
    }
    for(const n of range(db.total)) {
      const copies=best.files.filter(v=>v.episode===n).sort((a,b)=>b.time-a.time);
      if(copies.length>1&&copies[0].time===copies[1].time){result.state='重复副本时间相同，人工判断';result.issues.push(`第 ${n} 集最新时间相同，无法确定保留哪份`);result.keep=[];return result;}
      result.keep.push(stamp(copies[0].row));
    }
    const kept=new Set(result.keep.map(r=>r.path));
    result.remove=videos.filter(r=>!kept.has(r.path)).map(r=>({...stamp(r),kind:'video',reason:best.files.some(v=>v.row.path===r.path)?'同版本较旧重复副本':'其他不完整版本；该集已有完整版本保留'}));
    const removed=new Set(result.remove.map(r=>r.path));
    for(const f of rows.filter(r=>!r.directory&&SCRAPE.test(r.name))) {
      const owners=sidecarOwners(f,videos);
      if(owners.length===1&&removed.has(owners[0].path))result.remove.push({...stamp(f),kind:'scrape',owner:owners[0].path,reason:'唯一对应拟删除视频的刮削文件'});
      else result.preserved.push({...stamp(f),reason:SHARED.test(stem(f.name))?'通用刮削文件':owners.length===1?'对应保留视频':owners.length>1?'可能对应多个视频，保留':'归属未明确，保留'});
    }
    // 配套文件在视频之后删除；每一步均需保持整个保留版本未变。
    result.complete=true;result.eligible=result.remove.length>0;result.state=result.eligible?'待确认清理':result.confirmedEnded?'齐全':'集号齐全，播出状态待核实';
    return result;
  }
  function assertFresh(plan,rows,done=[]) {
    const removed=new Set(done),expected=plan.snapshot.filter(r=>!removed.has(r.path));
    if(!equal(expected,rows))throw Error('文件列表、大小或修改时间发生变化，停止清理并重新复核');
    if(plan.keep.some(k=>removed.has(k.path)))throw Error('清理记录错误地包含保留文件，停止');
    if(plan.remove.some(r=>r.directory||C.parent(r.path)!==plan.path||plan.keep.some(k=>k.path===r.path)))throw Error('待删清单越界或包含保留文件');
    return true;
  }
  async function execute(plan,{list,remove,load,save,checkpoint,progress=()=>{}}) {
    if(!plan.complete||!plan.eligible||!plan.keep.length||!plan.remove.length)throw Error('此项没有可执行的唯一完整版本清理计划');
    let journal=load(plan.taskKey)||{planId:plan.id,records:[],pending:null};
    if(journal.pending)throw Error('上次删除结果未确认，请先核实，不能重复提交');
    if(journal.planId!==plan.id)journal={planId:plan.id,records:[],pending:null,history:[...journal.history||[],...journal.records||[]]};
    const done=journal.records.filter(r=>r.state==='verified').map(r=>r.path);
    if(done.some(p=>!plan.remove.some(r=>r.path===p)))throw Error('删除记录与当前清单不一致');
    for(const file of plan.remove.filter(r=>!done.includes(r.path))) {
      checkpoint();const current=await list(plan.path);checkpoint();assertFresh(plan,current,done);
      // 将原因、配套视频及修改时间随请求一起持久化，失败后仍能追溯每次尝试。
      journal.pending={path:file.path,before:stamp(file),kind:file.kind,owner:file.owner||'',reason:file.reason,planId:plan.id,at:new Date().toISOString()};save(plan.taskKey,journal);
      progress(file);
      try {
        await remove(file,current); // 把整个刚读到的快照交给确认窗口再次核对，不只核对待删文件。
        const after=await list(plan.path,{uninterruptible:true});
        assertFresh(plan,after,[...done,file.path]);
      }catch(e){
        if(e.notSubmitted){journal.records.push({...journal.pending,state:'not-submitted',error:e.message,finishedAt:new Date().toISOString()});journal.pending=null;save(plan.taskKey,journal);throw e;}
        journal.pending.error=e.message+'；删除结果未确认；不自动重试';save(plan.taskKey,journal);
        const err=Error('删除结果未确认，已停止后续操作；请刷新云盘核实该文件');err.cause=e;throw err;
      }
      journal.records.push({...journal.pending,state:'verified',finishedAt:new Date().toISOString()});journal.pending=null;
      save(plan.taskKey,journal);done.push(file.path);
    }
    // 全部计划项消失且其他条目保持原样，才允许写“清理成功”。
    assertFresh(plan,await list(plan.path,{uninterruptible:true}),done);
    return {state:'清理已验证',records:journal.records,deleted:done,keep:plan.keep};
  }
  // 每季按最多50个文件一批提交；每批只刷新核实一次，审计仍逐文件记录。
  async function executeBatch(plan,{list,removeBatch,load,save,checkpoint,progress=()=>{},batchSize=50}) {
    if(!plan.complete||!plan.eligible||!plan.keep.length||!plan.remove.length)throw Error('此项没有可执行的唯一完整版本清理计划');
    if(typeof removeBatch!=='function'||!Number.isInteger(batchSize)||batchSize<1||batchSize>50)throw Error('批量删除接口或批次大小无效');
    let journal=load(plan.taskKey)||{planId:plan.id,records:[],pending:null};
    if(journal.pending)throw Error('上次删除结果未确认，请先核实，不能重复提交');
    if(journal.planId!==plan.id)journal={planId:plan.id,records:[],pending:null,history:[...journal.history||[],...journal.records||[]]};
    const done=journal.records.filter(r=>r.state==='verified').map(r=>r.path);
    if(done.some(p=>!plan.remove.some(r=>r.path===p)))throw Error('删除记录与当前清单不一致');
    const remaining=plan.remove.filter(f=>!done.includes(f.path));
    for(let offset=0;offset<remaining.length;offset+=batchSize) {
      checkpoint();const current=await list(plan.path);checkpoint();assertFresh(plan,current,done);
      const batch=remaining.slice(offset,offset+batchSize),at=new Date().toISOString(),batchId=plan.id+':'+journal.records.length+':'+at;
      const events=batch.map(f=>({path:f.path,before:stamp(f),kind:f.kind,owner:f.owner||'',reason:f.reason,planId:plan.id,batchId,at}));
      journal.pending={batchId,planId:plan.id,at,files:events,beforeSnapshot:snapshot(current)};save(plan.taskKey,journal);
      progress({count:batch.length,batchId});let after;
      try {await removeBatch(batch,current);after=await list(plan.path,{uninterruptible:true});}
      catch(e){
        if(e.notSubmitted){journal.records.push(...events.map(f=>({...f,state:'not-submitted',error:e.message,finishedAt:new Date().toISOString()})));journal.pending=null;save(plan.taskKey,journal);throw e;}
        journal.pending.error=e.message+'；本批删除结果未确认，不自动重试';save(plan.taskKey,journal);throw Error('批量删除结果未确认，请核实上次删除结果');
      }
      const gone=batch.filter(f=>!after.some(r=>r.path===f.path)).map(f=>f.path);
      try{assertFresh(plan,after,[...done,...gone]);}catch(e){journal.pending.error=e.message;save(plan.taskKey,journal);throw Error('批量删除后保留清单发生变化，结果未确认');}
      journal.records.push(...events.filter(f=>gone.includes(f.path)).map(f=>({...f,state:'verified',finishedAt:new Date().toISOString()})));done.push(...gone);
      const unresolved=events.filter(f=>!gone.includes(f.path));
      if(unresolved.length){journal.pending.files=unresolved;journal.pending.error='本批部分文件仍存在，请确认没有在途删除后人工核实；不自动重试';save(plan.taskKey,journal);throw Error('本批仅部分删除，已记录成功项并停止；请核实剩余文件');}
      journal.pending=null;save(plan.taskKey,journal);
    }
    return {state:'清理已验证',records:journal.records,deleted:done,keep:plan.keep};
  }
  return {executeBatch,MEDIA,SCRAPE,stamp,snapshot,equal,verifySource,completionEvidence,versionOf,sidecarOwners,plan,assertFresh,execute};
})
