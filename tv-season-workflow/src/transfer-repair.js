/* MoviePilot v2.15.6 的定向整理修复。
 * 检查只调用查询/预览。执行前重新查询并逐项比对，不使用 Excel 内的请求参数。
 * 只删除精确历史 ID，deletesrc/deletedest 固定 false；禁止 logid、reorganize、覆盖整理。
 * 当前实现仅支持配置范围内的 local 普通视频；不读取认证秘密，不处理上传链路。
 */
(function createTransferRepair(Core,Cloud,State,Versions) {
  'use strict';
  const media=/\.(mkv|mp4|avi|mov|m4v|ts|m2ts|wmv)$/i;
  const parent=p=>p.slice(0,p.lastIndexOf('/'))||'/';
  const valid=p=>typeof p==='string'&&p.startsWith('/')&&!/[\\\x00-\x1f]/.test(p)&&!p.includes('//')&&!p.split('/').some(s=>s==='.'||s==='..');
  const within=(p,root)=>valid(p)&&valid(root)&&p.startsWith(root.replace(/\/$/,'')+'/');
  const fileKey=f=>JSON.stringify([f.storage||'local',f.path,f.type,f.size,f.modify_time]);
  const local=f=>f&&(!f.storage||f.storage==='local')&&valid(f.path);
  const file=f=>({storage:f.storage||'local',path:f.path,type:f.type,name:f.name||f.path.split('/').pop(),size:f.size,modify_time:f.modify_time});
  const history=h=>Object.fromEntries(['id','src','dest','src_storage','dest_storage','mode','type','title','year','tmdbid','seasons','episodes','download_hash','downloader','status','date','episode_group','errmsg'].map(k=>[k,h[k]??null]).concat([['src_fileitem',h.src_fileitem?file(h.src_fileitem):null],['dest_fileitem',h.dest_fileitem?file(h.dest_fileitem):null]]));
  const sameHistory=(a,b)=>JSON.stringify(history(a))===JSON.stringify(history(b));
  function fresh(task,now=Date.now()) {
    const d=task.data,time=d['文件核查时间']||(d['复核视频文件数']!==''&&d['复核视频文件数']!=null?d['复核时间']:null)||d['扫描时间'],at=Date.parse(time);
    if(!Number.isFinite(at)||now-at>24*3600000||at-now>300000)throw Error('文件核查结果超过24小时或时间未知，请先用第一/第三脚本刷新缺集报告');
    if(!task.tmdb||!task.needed?.length)throw Error('需要准确的TMDB ID和明确缺失集号，才能检查整理阻挡');
  }
  function identity(task,h){return String(h.tmdbid)===task.tmdb&&h.type==='电视剧'&&/^S\d+$/i.test(h.seasons||'')&&Number(h.seasons.slice(1))===task.season&&/^E\d+$/i.test(h.episodes||'')&&task.needed.includes(Number(h.episodes.slice(1)));}
  function queueContains(jobs,task,path){
    if(!Array.isArray(jobs))throw Error('无法完整查询整理队列');
    return jobs.some(j=>{
      if(!Array.isArray(j.tasks))throw Error('整理队列格式不支持');
      return j.tasks.some(t=>{const p=t.fileitem?.path;if(!p)throw Error('整理队列缺少文件路径');return p===path||path.startsWith(p.replace(/\/$/,'')+'/');})||String(j.media?.tmdb_id||j.media?.tmdbid||'')===task.tmdb&&Number(j.season)===task.season;
    });
  }
  function activeContains(items,task,h){
    if(!Array.isArray(items))throw Error('下载任务查询不完整');
    return items.some(t=>t.hash===h.download_hash||t.content_path&&within(h.src,t.content_path)||t.content_path===h.src||Core.activeMatch(task,t));
  }
  // 逐层确认目录存在，空返回值不能作为“路径不存在”的证据。
  async function inspectPath(api,path,root) {
    if(!within(path,root))throw Error('文件路径超出配置范围');
    let dir=root;
    const rootRows=await api.list(parent(root));
    if(!rootRows.some(f=>local(f)&&f.path===root&&f.type==='dir'))throw Error('配置根目录不存在或无法读取');
    const parts=path.slice(root.length+1).split('/');
    for(let n=0;n<parts.length;n++) {
      const rows=await api.list(dir),expected=dir+'/'+parts[n];
      const found=rows.find(f=>f.path===expected);
      if(!found)return {exists:false,rows,parent:dir};
      if(!local(found))throw Error('仅支持本地存储路径');
      if(n===parts.length-1)return {exists:true,item:file(found),rows,parent:dir};
      if(found.type!=='dir')throw Error('路径中间存在同名文件');dir=expected;
    }
  }
  function request(task,h,source,targetRoot){
    return {fileitems:[source],target_storage:'local',target_path:targetRoot+'/'+task.category,tmdbid:Number(task.tmdb),type_name:'电视剧',season:task.season,
      transfer_type:'copy',scrape:false,library_type_folder:false,library_category_folder:false,from_history:false,reorganize:false,preview:true};
  }
  async function candidate(task,h,api,config) {
    const ep=Number(h.episodes.slice(1));
    if(!Number.isSafeInteger(h.id)||h.id<1||!local(h.src_fileitem)||h.src!==h.src_fileitem.path||!within(h.src,config.source)||!media.test(h.src))throw Error('历史源路径或文件类型不支持');
    if(h.episode_group)throw Error('自定义剧集组需人工确认，不自动修复');
    if(/E\d+[-~]E?\d+/i.test(h.src)||Cloud.episodeFromName(h.src.split('/').pop(),task.season)!==ep)throw Error('历史集号与当前文件名不一致');
    const src=await inspectPath(api,h.src,config.source);
    if(!src.exists||src.item.type!=='file'||!Number.isSafeInteger(src.item.size)||src.item.size<1||!Number.isFinite(src.item.modify_time))throw Error('下载源文件不存在、大小或修改时间未确认');
    if(activeContains(await api.active(),task,h))throw Error('本季或对应资源仍在下载，暂缓整理修复');
    if(queueContains(await api.queue(),task,h.src))throw Error('本季或对应文件正在整理，不重复提交');
    const all=await api.histories(h.src);
    const exact=all.filter(x=>x.src===h.src&&(x.src_storage||x.src_fileitem?.storage||'local')==='local');
    if(exact.length!==1||!sameHistory(h,exact[0]))throw Error('同源历史重复或已变化，需人工核实');
    const req=request(task,h,src.item,config.target),preview=await api.preview(req),p=preview.data;
    if(preview.success!==true||p?.summary?.failed!==0||p.summary.total!==1||p.summary.success!==1||p.items?.length!==1)throw Error('整理预览未确认单个视频成功');
    const item=p.items[0];
    const seasonDir=parent(item.target||''),showDir=parent(seasonDir),expectedShow=config.target+'/'+task.category+'/'+task.name;
    if(item.success!==true||item.source!==h.src||Number(item.season)!==task.season||Number(item.episode)!==ep||Number(item.episode_end||ep)!==ep||item.type!=='电视剧'||showDir!==expectedShow||!/^Season\s+\d+$/i.test(seasonDir.split('/').pop())||Number(seasonDir.split('/').pop().match(/\d+$/)[0])!==task.season||!within(item.target,config.target))throw Error('整理预览的剧集、季集或目标路径不一致');
    const dest=await inspectPath(api,item.target,config.target);
    if(dest.exists||dest.rows.some(f=>f.type==='file'&&media.test(f.name||'')&&(!Cloud.episodeFromName(f.name,task.season)||/E\d+[-~]E?\d+/i.test(f.name)||Cloud.episodeFromName(f.name,task.season)===ep)))throw Error('整理目标已有同集或同名文件，先由第三脚本复核');
    const after=await inspectPath(api,h.src,config.source);
    if(!after.exists||fileKey(after.item)!==fileKey(src.item))throw Error('预览期间下载源文件发生变化');
    return {id:h.id,episode:ep,source:h.src,target:item.target,history:history(h),sourceFile:src.item,request:req,state:'ready',steps:[],note:'请确认该资源下载已完成；仅解除一条历史并复制单集视频'};
  }
  async function check(task,api,config,checkpoint=()=>{}) {
    fresh(task);const version=Versions?.inspect(task);if(version?.blocked||version?.groups||version?.complete)throw Error(version.blocked||'报告有混版或已有完整版本，请先由第三脚本明确版本，不自动重整历史文件');
    await api.version();checkpoint();
    const records=await api.histories(task.title),matches=records.filter(h=>identity(task,h)),items=[],notes=[];
    const ids=new Set((task.records||[]).filter(r=>['accepted','existing'].includes(r.state)).map(r=>r.id).filter(Boolean));
    for(const ep of task.needed){checkpoint();
      const hs=matches.filter(h=>Number(h.episodes.slice(1))===ep&&(!ids.size||ids.has(h.download_hash)));
      if(hs.length!==1){notes.push('E'+ep+'：'+(hs.length?'有多条匹配记录，需人工选择':'未找到唯一匹配的整理历史'));continue;}
      try{items.push(await candidate(task,hs[0],api,config));}catch(e){notes.push('E'+ep+'：'+e.message);}
    }
    return {schema:1,taskKey:task.key,state:items.length?'ready':'blocked',at:new Date().toISOString(),episodes:items.length?items.map(i=>i.episode):task.needed,items,note:notes.join('；')||'已列清单，尚未执行；不在下载列表中不代表下载完成',checkedAt:new Date().toISOString()};
  }
  const fingerprint=p=>JSON.stringify(p.items.map(i=>[i.id,i.episode,i.source,i.target,i.history,i.sourceFile,i.request]));
  async function reconcile(task,record,api,config,checkpoint=()=>{}) {
    if(record?.taskKey!==task.key||!record.items?.length)throw Error('没有可核实的修复记录');
    await api.version();const r=structuredClone(record);
    for(const i of r.items){checkpoint();
      if(queueContains(await api.queue(),task,i.source))throw Error('仍有整理任务，请等待结束');
      const hs=await api.histories(i.source),dest=await inspectPath(api,i.target,config.target);
      if(!dest.exists||dest.item.type!=='file'||dest.item.size!==i.sourceFile.size||!hs.some(h=>h.id!==i.id&&h.src===i.source&&h.dest===i.target&&h.status===true&&identity(task,h)))throw Error('尚无新的成功整理记录和匹配目标文件，保留待核实；不会重发请求');
      i.state='submitted';i.note='只读核实到新的整理成功记录及目标文件，待云盘复核';
    }
    r.state='submitted';r.at=new Date().toISOString();r.note='只读核实完成，未重发删除或整理请求；交第三脚本复核';return r;
  }
  async function execute(task,plan,api,config,{save,checkpoint=()=>{}}) {
    if(plan.taskKey!==task.key||plan.state!=='ready'||!plan.items.length||State.uncertain(plan))throw Error('请重新检查整理问题后再执行');
    const freshPlan=await check(task,api,config,checkpoint);
    if(fingerprint(freshPlan)!==fingerprint(plan))throw Error('修复清单已变化，请重新检查确认');
    const record=structuredClone(plan);record.at=new Date().toISOString();
    const persist=()=>{record.at=new Date().toISOString();save(record);};
    for(const i of record.items) {
      try{checkpoint();await candidate(task,i.history,api,config);checkpoint();}
      catch(e){record.state='checked';record.note='本轮已停止；已提交项保留记录，重新检查只列尚未修复且目标缺失的文件';persist();throw e;}
      // 删除历史前先落盘完整白名单快照。所有写请求只发送一次，超时保留未知状态。
      i.state=record.state='history-delete-pending';i.steps.push({action:'仅删除整理历史',state:'pending',at:new Date().toISOString(),historyId:i.id});persist();
      try {
        const reply=await api.removeHistory(i.id);
        if(reply?.success!==true)throw Error('历史删除未确认成功');
        i.steps.at(-1).state='verified';i.steps.at(-1).finishedAt=new Date().toISOString();i.state=record.state='history-deleted';persist();
        // 期间自动整理可能抢先运行：再次查历史、在途任务、源文件和目标，不盲目接着提交。
        const histories=await api.histories(i.source);
        if(histories.some(h=>h.src===i.source&&(h.src_storage||'local')==='local'))throw Error('删除后出现新的同源整理记录，停止提交并核实');
        if(activeContains(await api.active(),task,i.history)||queueContains(await api.queue(),task,i.source))throw Error('下载或整理任务状态已变化');
        const src=await inspectPath(api,i.source,config.source),dst=await inspectPath(api,i.target,config.target);
        if(!src.exists||fileKey(src.item)!==fileKey(i.sourceFile)||dst.exists||dst.rows.some(f=>f.type==='file'&&media.test(f.name||'')&&Cloud.episodeFromName(f.name,task.season)===i.episode))throw Error('源或目标已变化，停止整理提交');
        checkpoint();
        i.state=record.state='transfer-pending';i.steps.push({action:'复制单集整理',state:'pending',at:new Date().toISOString()});persist();
        const result=await api.submit({...i.request,preview:false});
        if(result?.success!==true)throw Error('整理请求未确认成功');
        i.steps.at(-1).state='submitted';i.steps.at(-1).finishedAt=new Date().toISOString();i.state='submitted';persist();
      }catch(e){i.note=e.message;i.state=record.state='uncertain';i.steps.at(-1).error=e.message;persist();throw e;}
    }
    record.state='submitted';record.note='整理请求已返回，需第三脚本检查实际文件；未确认的其他缺集仍保留';persist();return record;
  }
  function adapter(api,origin) {
    if(!api||!['get','post','delete'].every(k=>typeof api[k]==='function'))throw Error('MoviePilot插件接口不支持整理修复');
    if(new URL(api.defaults?.baseURL||'/api/v1/',origin).origin!==origin)throw Error('拒绝跨域接口');
    const call=async(method,url,...args)=>{try{return await api[method](url,...args);}catch{throw Error('MoviePilot请求失败或结果不明，请核实；不自动重试');}};
    const get=(url,params={})=>call('get',url,{params:{...params,_ts:Date.now()},timeout:30000});
    const post=(url,data)=>call('post',url,data,{timeout:120000});
    return {
      version:async()=>{const r=await get('system/global',{token:'moviepilot'});if(r?.success!==true||r.data?.BACKEND_VERSION!=='v2.15.6')throw Error('整理修复仅适配MoviePilot v2.15.6，其他版本先核实');},
      active:()=>get('download/'),queue:()=>get('transfer/queue'),
      histories:async title=>{const rows=[],seen=new Set();let total=null;
        for(let page=1;page<=20;page++){const r=await get('history/transfer',{title,page,count:100});
          if(r?.success!==true||!Array.isArray(r.data?.list)||!Number.isInteger(r.data.total)||r.data.total<0||r.data.total>2000||total!==null&&total!==r.data.total)throw Error('整理历史分页不完整或查询期间发生变化');total=r.data.total;
          for(const h of r.data.list){if(seen.has(h.id))throw Error('整理历史分页重复');seen.add(h.id);rows.push(h);}
          if(rows.length===total)return rows;if(!r.data.list.length||rows.length>total)throw Error('整理历史数量不一致');
        }throw Error('匹配历史过多，请缩小范围');},
      list:async path=>{const rows=await post('storage/list',{storage:'local',path,type:'dir'});if(!Array.isArray(rows)||rows.some(f=>!local(f)||parent(f.path)!==path)||new Set(rows.map(f=>f.path)).size!==rows.length)throw Error('文件列表不完整或路径不一致');return rows;},
      preview:req=>post('transfer/manual',{...req,preview:true}),
      removeHistory:id=>call('delete','history/transfer',{params:{deletesrc:false,deletedest:false},data:{id},timeout:30000}),
      submit:req=>post('transfer/manual',req)
    };
  }
  return {check,execute,reconcile,adapter,fingerprint,fresh,inspectPath,history};
})
