/* 第四阶段：按整剧生成归档清单；目标同剧已存在时逐季合入，并验证原目标未变。
 * 报告中某一季绿色不能代表整剧；源目录中的每个实际季都必须唯一对应绿色行。
 * 比较依据是界面可见的相对路径、类型、大小及文件修改时间，不声称做过内容哈希校验。
 * 同季或根文件冲突则整剧暂缓；无冲突移走全部内容并清理源目录；请求前保存逐项记录，结果不明只能复查，不能重复移动。
 */
(function createArchiveCore(C,settings) {
  'use strict';
  settings=settings||{};
  const SOURCE=settings.source||'/115/Library_incs/nas/电视剧',DEST=settings.destination||'/115/Library_incs/nas.finish/电视剧';
  const RepairState=(/* build:repair-state */ null)();
  const MEDIA=/\.(?:mp4|mkv|avi|mov|m4v|ts|m2ts|wmv|flv|webm|mpg|mpeg|strm)$/i;
  // 仅剧集根部的标准共用资料可保留目标、丢弃源；视频、字幕、任意 NFO 不在范围内。
  const shared=f=>!f.directory&&/^(?:tvshow\.nfo|(?:poster|fanart|banner|clearlogo|clearart|landscape|thumb|folder|logo|background)\.(?:jpg|jpeg|png|webp))$/i.test(f.name);
  function cleanupFiles(p) {
    const files=p.cleanup?.discardFiles||[];
    for(const f of files){
      const target=p.destinationBefore?.find(t=>C.parent(t.path)===p.target&&t.name.normalize('NFC').toLowerCase()===f.name.normalize('NFC').toLowerCase());
      if(C.parent(f.path)!==p.source||!shared(f)||!target||!shared(target)||!/^\d+(?:\.\d+)?\s*(?:B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$/i.test(target.size)||parseFloat(target.size)<=0)throw Error('源清理清单不是已确认的共用资料，停止删除');
    }
    return files;
  }
  const pathOK=p=>typeof p==='string'&&!p.includes('//')&&!p.split('/').some(s=>s==='.'||s==='..')&&!/[\\\x00-\x1f]/.test(p);
  // 配置错误时先停止，避免把源目录移入自己或扩大到云盘根目录。
  if([SOURCE,DEST].some(p=>!pathOK(p)||!p.startsWith('/')||p==='/'||p.endsWith('/'))||SOURCE===DEST||SOURCE.startsWith(DEST+'/')||DEST.startsWith(SOURCE+'/'))throw Error('源目录与归档目录必须是独立的绝对目录，不能相同或相互包含');
  const snapshot=rows=>rows.map(r=>({path:r.path,name:r.name,directory:!!r.directory,size:String(r.size||''),modified:String(r.modified||'')})).sort((a,b)=>a.path.localeCompare(b.path));
  const equal=(a,b)=>JSON.stringify(snapshot(a))===JSON.stringify(snapshot(b));
  function groups(rows) {
    const map=new Map();
    for(const row of rows){if(!map.has(row.showPath))map.set(row.showPath,[]);map.get(row.showPath).push(row);}
    return [...map.values()].filter(g=>g.some(r=>r.color==='green'));
  }
  function identity(rows) {
    if(rows.some(r=>r.directoryOnly))throw Error('本剧含季号未确认的目录级记录，暂缓归档；请先重新初检或人工核实');
    if(rows.some(r=>RepairState.pending(RepairState.read(r.data))))throw Error('本剧有未完成的整理修复记录，请先复核文件');
    const first=rows[0];if(!first)throw Error('没有可归档的报告行');
    const source=SOURCE+'/'+first.category+'/'+first.name,target=DEST+'/'+first.category+'/'+first.name;
    if(!pathOK(source)||!pathOK(target)||/[\\/]/.test(first.category+first.name)||rows.some(r=>r.showPath!==source||r.category!==first.category||r.name!==first.name||C.parent(r.path)!==source||Number(/^Season\s+(\d+)$/i.exec(C.base(r.path))?.[1])!==r.season))throw Error('报告整剧路径或季目录不一致');
    return {source,target,category:first.category,name:first.name};
  }
  // 仅供一次“开始/继续检查”的预览使用，最多复用 60 秒。
  // 只缓存目标根目录和它的直接分类；源、目标剧目录与季内文件始终实时读取。
  // 执行、恢复核实和事后验证不得使用此包装器；读取失败清空已存快照。
  function createPreviewListing(list,{now=()=>Date.now()}={}) {
    const cache=new Map();
    return async path=>{
      const reusable=path===DEST||C.parent(path)===DEST;
      const cached=reusable?cache.get(path):null,at=now();
      if(cached&&at>=cached.at&&at-cached.at<60000)return structuredClone(cached.rows);
      try {
        const rows=await listChecked(list,path);
        if(reusable)cache.set(path,{at:now(),rows:structuredClone(rows)});
        return rows;
      }catch(e){cache.clear();throw e;}
    };
  }
  async function listChecked(list,path) {
    const rows=await list(path);C.validateListing(rows,path,rows.length,{emptyConfirmed:true});return rows;
  }
  // 只接受扁平的 Season N。未知子目录、根目录视频和下载中间文件必须先人工处理。
  async function scan(list,path) {
    const root=await listChecked(list,path),seasons=root.filter(r=>r.directory),all=[...root];
    if(root.some(r=>!r.directory&&MEDIA.test(r.name)))throw Error('剧集根目录有未归入季的视频');
    if(!seasons.length)throw Error('没有实际季目录');
    const seen=new Set();
    for(const dir of seasons) {
      const season=Number(/^Season\s+(\d+)$/i.exec(dir.name)?.[1]);
      if(!Number.isInteger(season)||season<1||season>999||seen.has(season))throw Error('存在未知、特别篇或重复季目录，须人工确认');seen.add(season);
      const files=await listChecked(list,dir.path);if(files.some(r=>r.directory))throw Error('季目录中存在子目录，不能确认整剧范围');
      const videos=files.filter(r=>MEDIA.test(r.name)),episodes=videos.map(r=>C.episodeFromName(r.name,season));
      if(!videos.length||episodes.some(n=>!n)||new Set(episodes).size!==episodes.length)throw Error('季目录为空、集号无法识别或仍有重复版本');
      if(videos.some(r=>/\.strm$/i.test(r.name)||!/^\d+(?:\.\d+)?\s*(?:B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$/i.test(r.size)||parseFloat(r.size)<=0))throw Error('发现播放指针、零大小或大小未知的视频');
      all.push(...files);
    }
    if(all.some(r=>/\.(?:part|tmp|download|crdownload|!qb)$/i.test(r.name)))throw Error('存在下载中的临时文件');
    if(!equal(root,await listChecked(list,path)))throw Error('扫描期间剧集根目录发生变化');
    return snapshot(all);
  }
  async function plan(rows,list,id) {
    const p={...identity(rows),id,rows,eligible:false,manifest:[]};
    if(rows.some(r=>r.color!=='green'||/已归档/.test(String(r.data?.['归档结果'])+String(r.data?.['工作阶段']))))throw Error('本剧还有非绿色或已经归档的报告行');
    if(new Set(rows.map(r=>r.season)).size!==rows.length)throw Error('同一季在报告中重复，请先统一记录');
    const targetRoot=await listChecked(list,DEST),category=targetRoot.find(r=>r.name===p.category);
    if(!category?.directory)throw Error('归档目标缺少同名分类目录，请先在目标目录中建立该分类');
    const existing=(await listChecked(list,C.parent(p.target))).find(r=>r.name.normalize('NFC').toLowerCase()===p.name.normalize('NFC').toLowerCase());
    if(existing&&(!existing.directory||existing.name!==p.name))throw Error('目标名称冲突或大小写不同，须人工确认');
    const manifest=await scan(list,p.source),actual=manifest.filter(r=>r.directory);
    if(actual.length!==rows.length||actual.some(d=>!rows.some(r=>r.path===d.path)))throw Error('实际季目录与报告绿色季不完全对应');
    for(const row of rows) {
      const eps=manifest.filter(f=>!f.directory&&C.parent(f.path)===row.path&&MEDIA.test(f.name)).map(f=>C.episodeFromName(f.name,row.season)).sort((a,b)=>a-b);
      const raw=row.data?.['来源登记集数'],total=Number(raw);
      if(raw!==''&&raw!=null&&(!Number.isInteger(total)||total<1||eps.length!==total||eps.some((n,i)=>n!==i+1)))throw Error('当前视频集号与报告登记集数不符');
      if(eps.some((n,i)=>n!==i+1))throw Error('当前集号不连续，不能按绿色行归档');
    }
    p.manifest=manifest;p.mode=existing?'seasons':'show';p.destinationBefore=[];p.preserved=[];
    if(existing) {
      // 同名目标只接收完整的一部剧：同季/未知根文件冲突不部分移动；标准共用资料允许保留目标并清理源。
      p.destinationBefore=await inventory(list,p.target);
      const root=p.destinationBefore.filter(f=>C.parent(f.path)===p.target),entries=manifest.filter(f=>C.parent(f.path)===p.source);
      const match=(a,b)=>a.name.normalize('NFC').toLowerCase()===b.name.normalize('NFC').toLowerCase()||a.directory&&b.directory&&Number(/^Season\s+(\d+)$/i.exec(a.name)?.[1])===Number(/^Season\s+(\d+)$/i.exec(b.name)?.[1]);
      const discard=entries.filter(d=>shared(d)&&root.some(f=>match(d,f)&&shared(f)&&/^\d+(?:\.\d+)?\s*(?:B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$/i.test(f.size)&&parseFloat(f.size)>0));
      p.skipped=entries.filter(d=>!discard.includes(d)&&root.some(f=>match(d,f))).map(d=>({source:d.path,target:root.find(f=>match(d,f)).path,reason:'目标同季或同名文件冲突，整剧暂缓归档'}));
      p.operations=p.skipped.length?[]:entries.filter(d=>!discard.includes(d)).map(d=>({kind:d.directory?'season':'file',source:d.path,target:p.target+'/'+d.name,name:d.name,category:p.category,showSource:p.source,showTarget:p.target,
        manifest:manifest.filter(f=>f.path===d.path||f.path.startsWith(d.path+'/')),planId:id,state:'planned'}));
      p.preserved=p.skipped.length?manifest:[];
      if(!p.skipped.length)p.cleanup={kind:'source-directory',discardFiles:discard,source:p.source,target:'',planId:id,state:'planned',manifest:[{name:p.name,path:p.source,directory:true},...discard],reason:'季与文件迁移验证完成，保留目标共用资料；删除源端已列多余资料及剧集目录'};
    }else p.operations=[{kind:'show',source:p.source,target:p.target,name:p.name,category:p.category,manifest,planId:id,state:'planned'}];
    p.eligible=p.operations.length>0;return p;
  }
  // 只读快照用于确保目标已有内容没有被改动；不要求目标旧季也是齐全的。
  async function inventory(list,root,depth=0) {
    if(depth>10)throw Error('目标目录层级过深，须人工确认');
    const rows=await listChecked(list,root),all=[...rows];
    for(const d of rows.filter(r=>r.directory)){all.push(...await inventory(list,d.path,depth+1));if(all.length>50000)throw Error('目标文件过多，请拆分归档');}
    if(!equal(rows,await listChecked(list,root)))throw Error('目标目录扫描期间发生变化');return snapshot(all);
  }
  function relative(manifest,root) {
    if(manifest.some(r=>r.path!==root&&!r.path.startsWith(root+'/')||!pathOK(r.path)))throw Error('文件清单超出整剧范围');
    // 移动后目录时间可能变化；文件的修改时间仍要求保持一致。
    return manifest.map(r=>({path:r.path.slice(root.length),name:r.name,directory:!!r.directory,size:r.directory?'':r.size,modified:r.directory?'':r.modified})).sort((a,b)=>a.path.localeCompare(b.path));
  }
  async function verify(p,list) {
    if(p.mode==='seasons') {
      await verifyMergedTarget(p,list);
      if((await listChecked(list,C.parent(p.source))).some(f=>f.path===p.source))throw Error('源剧集目录仍存在；全部内容验证后须完成源空目录清理');
      return {...p,rows:undefined,eligible:undefined,state:'已归档',pending:false,verified:true,at:new Date().toISOString(),cleanup:{...p.cleanup,state:'verified',finishedAt:p.cleanup?.finishedAt||new Date().toISOString()},operations:p.operations.map(op=>({...op,state:'verified',finishedAt:op.finishedAt||new Date().toISOString()}))};
    }
    const from=await listChecked(list,C.parent(p.source)),to=await listChecked(list,C.parent(p.target));
    if(from.some(r=>r.path===p.source))throw Error('源剧集目录仍存在，移动结果待核实');
    if(!to.some(r=>r.path===p.target&&r.directory))throw Error('目标剧集目录尚未出现，移动结果待核实');
    const after=await scan(list,p.target);
    if(JSON.stringify(relative(p.manifest,p.source))!==JSON.stringify(relative(after,p.target)))throw Error('目标文件清单、大小或文件修改时间与移动前不同');
    return {...p,rows:undefined,eligible:undefined,state:'已归档',pending:false,at:new Date().toISOString(),source:p.source,target:p.target,manifest:p.manifest,verified:true,
      operations:(p.operations||[]).map(op=>({...op,state:'verified',finishedAt:op.finishedAt||new Date().toISOString()}))};
  }
  async function verifyMergedTarget(p,list) {
    // 拒绝将旧版“部分合入且保留源文件”的日志按整剧归档处理。
    if(!p.cleanup||p.preserved?.length||!equal(p.manifest,[...p.operations.flatMap(o=>o.manifest),...cleanupFiles(p)]))throw Error('旧版或不完整合入清单，不能确认整剧归档');
    const expected=[...p.destinationBefore,...p.operations.flatMap(o=>o.manifest.map(f=>({...f,path:o.target+f.path.slice(o.source.length)})))];
    if(JSON.stringify(relative(expected,p.target))!==JSON.stringify(relative(await inventory(list,p.target),p.target)))throw Error('目标原有文件或移入文件发生变化，待核实');
  }
  // 只允许清理本剧目录；源必须为空或只剩已确认可丢弃的根共用资料。
  // 删除结果不明时不自动重发，改由只读核实恢复。
  async function finishEmpty(p,api) {
    if(p.mode!=='seasons'||!p.cleanup)throw Error('没有源空目录清理计划');
    await verifyMergedTarget(p,api.list);
    let parents=await listChecked(api.list,C.parent(p.source));
    if(!parents.some(f=>f.path===p.source))return verify(p,api.verifyList||api.list);
    if(p.cleanup.state==='pending'||p.cleanup.state==='verified')throw Error('源空目录删除已有提交记录，须人工核实，不重复删除');
    if(!equal(await listChecked(api.list,p.source),cleanupFiles(p)))throw Error('源目录出现清单外内容或共用资料变化，停止清理');
    parents=await listChecked(api.list,C.parent(p.source));
    const entry=parents.find(f=>f.path===p.source&&f.directory);if(!entry)throw Error('源目录已变化');
    api.checkpoint();p.pending=true;p.cleanup.at=new Date().toISOString();p.cleanup.state='pending';await api.save(p.source,p);
    try {
      await api.removeEmpty(p,parents,entry);
      const result=await verify(p,api.verifyList||api.list);await api.save(p.source,result);return result;
    }catch(e){p.cleanup.state=e.notSubmitted?'not-submitted':'pending';p.cleanup.error=e.message;p.cleanup.finishedAt=new Date().toISOString();await api.save(p.source,p);throw e;}
  }
  async function verifyOperation(op,list) {
    const from=await listChecked(list,C.parent(op.source)),to=await listChecked(list,C.parent(op.target));
    if(from.some(f=>f.path===op.source)||!to.some(f=>f.path===op.target&&!!f.directory===(op.kind!=='file')))throw Error('季目录移动结果待核实');
    const after=[to.find(f=>f.path===op.target),...(op.kind==='file'?[]:await inventory(list,op.target))];
    if(JSON.stringify(relative(op.manifest,op.source))!==JSON.stringify(relative(after,op.target)))throw Error('移入季的文件清单、大小或修改时间不符');
  }
  async function execute(p,api) {
    if(!p.eligible)throw Error('归档计划未经检查');
    const old=await api.load(p.source);
    if(old?.pending||old?.verified&&equal(old.manifest||[],p.manifest))throw Error('本剧已有移动记录，请先核实上次结果，不能重复移动');
    api.checkpoint();
    const fresh=await plan(p.rows,api.list,p.id);if(!equal(p.manifest,fresh.manifest)||p.mode!==fresh.mode||!equal(p.destinationBefore,fresh.destinationBefore))throw Error('预览后源或目标文件清单发生变化，请重新检查');
    const pending={...p,rows:undefined,eligible:undefined,pending:true,at:new Date().toISOString(),planId:p.id,operations:structuredClone(p.operations),history:[...old?.history||[],...old?.operations||[],...(old?.cleanup?[old.cleanup]:[])]};
    for(const op of pending.operations) {
      api.checkpoint();
      // 每一步都再次检查已移动/尚未移动内容及目标原有内容，防止两次移动之间出现新冲突。
      if(p.mode==='seasons') {
        const done=pending.operations.filter(o=>o.state==='verified'),sourceExpected=p.manifest.filter(f=>!done.some(o=>f.path===o.source||f.path.startsWith(o.source+'/')));
        if(!equal(sourceExpected,await inventory(api.list,p.source)))throw Error('移动期间源文件发生变化，停止后续移动');
        const targetExpected=[...p.destinationBefore,...done.flatMap(o=>o.manifest.map(f=>({...f,path:o.target+f.path.slice(o.source.length)})))];
        if(JSON.stringify(relative(targetExpected,p.target))!==JSON.stringify(relative(await inventory(api.list,p.target),p.target)))throw Error('移动期间目标文件发生变化，停止后续移动');
      }
      const parentRows=await listChecked(api.list,C.parent(op.source)),entry=parentRows.find(r=>r.path===op.source&&!!r.directory===(op.kind!=='file'));if(!entry)throw Error('源目录不存在');
      api.checkpoint();op.at=new Date().toISOString();op.state='pending';await api.save(p.source,pending);
      try {
        await api.move(op,parentRows,entry);
        // adapter 先刷新当前源操作路径；随后按用户要求刷新目标季及其剧集父目录。
        if(op.kind!=='file')await listChecked(api.verifyList||api.list,op.target);
        await listChecked(api.verifyList||api.list,C.parent(op.target));
        if(p.mode==='seasons')await verifyOperation(op,api.verifyList||api.list);
        else await verify({...p,operations:pending.operations},api.verifyList||api.list);
        op.state='verified';op.finishedAt=new Date().toISOString();await api.save(p.source,pending);
      }catch(e){op.state=e.notSubmitted?'not-submitted':'pending';op.error=e.message;op.finishedAt=new Date().toISOString();pending.error=e.message;pending.pending=pending.operations.some(o=>o.state==='pending'||o.state==='verified');await api.save(p.source,pending);throw e;}
    }
    if(p.mode==='seasons')return finishEmpty(pending,api);
    const result=await verify(pending,api.verifyList||api.list);await api.save(p.source,result);return result;
  }
  async function reconcile(rows,journal,list) {
    const p=identity(rows);if(!journal||journal.source!==p.source||journal.target!==p.target||!Array.isArray(journal.manifest)||!journal.manifest.length)throw Error('上次移动记录与本剧不一致');
    return verify({...journal,...p},list);
  }
  return {SOURCE,DEST,MEDIA,groups,identity,cleanupFiles,createPreviewListing,plan,scan,verify,execute,finishEmpty,reconcile,equal,snapshot};
})
