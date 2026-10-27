// ==UserScript==
// @name         剧集工作流 ③ · 豆瓣复核与版本清理
// @namespace    local.tv-season-review
// @version      1.4.1
// @description  导入黄色项，手动开始豆瓣分季复核；列出保留/删除清单，经确认后普通删除多余版本及专属刮削文件。
// @match        https://clouddrive.example/*
// @match        https://movie.douban.com/*
// @match        https://search.douban.com/movie/subject_search*
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_deleteValue
// @grant        GM_openInTab
// @run-at       document-idle
// @noframes
// ==/UserScript==

/* 四阶段工作流 ③：复核黄色项并按具体清单清理。
 * 导入不运行；“开始/继续复核”只读；“执行已列清理清单”才进入确认后的云端删除。
 * 唯一完整版本优先；同版本零散重复副本按云盘显示修改时间取最新。
 * 多个完整版本、时间并列、分组/集号不明均交人工，不删除目录和通用刮削文件。
 * 每批删除前刷新核对，批量删后验证目标消失及其余文件未变；结果不明绝不重试。
 */
(function () {
  'use strict';
  // 修改这里及顶部 @match；SOURCE 必须与第一、第二脚本一致。
  const ORIGIN='https://clouddrive.example';
  const SOURCE='/115/Library_incs/nas/电视剧';
  const simplify=/* build:opencc */ null;
  const C=(/* build:cloud-core */ null)(simplify);
  const K=(/* build:cleanup-core */ null)(C);
  const RepairState=(/* build:repair-state */ null)();
  const createClient=/* build:cloud-client */ null;
  let excel,report,reviewStage=null;
  const createStageReport=/* build:review-stage-report */ null;
  function stageReportEngine(){return createStageReport(excelEngine());}
  function excelEngine(){return excel||=(/* build:xlsx */ null);}
  function reportEngine(){return report||=(/* build:workflow-report */ null)(excelEngine());}
  if(typeof module!=='undefined'&&module.exports){module.exports={...C,...K,createClient,reportEngine,stageReportEngine};return;}
  const NS='tv-review-v1:';
  const get=(k,d)=>GM_getValue(NS+k,d),put=(k,v)=>GM_setValue(NS+k,v),del=k=>GM_deleteValue(NS+k);
  let session=null,index=0,running=false,importing=false,stopping=false,opened=false,ui;
  let activeMode=null,cleanupRun=null;
  // 继续执行只恢复本次已确认的计划ID；重新复核生成的新计划不能混入暂停队列。
  function canResumeCleanup(){return !!cleanupRun?.paused&&!running&&!importing&&!cleanupRun.entries.some(([row])=>load(row.key)?.pending)&&cleanupRun.entries.some(([row,id])=>plans.get(row)?.id===id&&plans.get(row)?.eligible&&approvedPlans.has(id));}
  const plans=new Map(),bindings=new Map(),approvedPlans=new Set();
  function checkpoint(){if(stopping){const e=Error('已暂停，可继续本轮');e.name='Pause';throw e;}}
  const message=text=>{if(ui)ui.message.textContent=text;};
  const client=createClient(C,{source:SOURCE,get,put,del,checkpoint,message,ready:()=>opened&&ui?.ready.checked});
  if(location.hostname!==new URL(ORIGIN).hostname){client.worker().catch(()=>{});return;}
  if(location.origin!==ORIGIN)return;
  const load=key=>get('cleanup:'+key,null);
  const save=(key,value)=>put('cleanup:'+key,structuredClone(value));
  const el=(tag,text,props={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;Object.assign(n,props);return n;};
  const button=(label,action)=>{const b=el('button',label);b.addEventListener('click',()=>Promise.resolve().then(action).catch(e=>message(e.message)));return b;};
  function sourceLink(row){return bindings.get(row.key)||row.data['豆瓣链接']||(row.data['数据来源']==='douban'?row.data['来源链接']:'')||'';}
  function describe(row,p,{state,deleted=[],journal=null,note=''}={}) {
    const repair=RepairState.verify(RepairState.read(row.data),p.episodes,new Date().toISOString());
    const clean=p.complete&&!p.remove.length&&!journal?.pending&&!RepairState.pending(repair);
    const finalState=state||p.state,green=clean&&p.confirmedEnded;
    const source={url:p.source.url,title:p.source.title,total:p.total,checkedAt:p.source.checkedAt||new Date().toISOString()};
    const comments=[...p.issues,note,!p.confirmedEnded&&p.complete?'本地集号齐全不等于已播完，缺少播完依据时保留黄色':'',p.preserved.some(f=>/未明确|多个/.test(f.reason))?'归属不明的刮削文件已保留':''].filter(Boolean).join('；');
    return {repair:repair||undefined,fileCheckedAt:new Date().toISOString(),state:finalState,color:green?'green':null,total:p.total,sourceUrl:source.url,at:new Date().toISOString(),videoCount:p.videoCount,current:p.current,missing:p.missing,
      version:p.groups.filter(g=>g.complete).map(g=>g.label).join('；'),keep:p.keep.map(f=>f.path),remove:p.remove.map(f=>f.path),deleted,preserved:p.preserved.map(f=>f.path),note:comments,
      record:{planId:p.id,source,groups:p.groups,keep:p.keep,remove:p.remove,preserved:p.preserved,issues:p.issues,journal},
      cells:{'视频文件数':p.videoCount,'已识别集数':p.current,'来源登记集数':p.total,'缺失集号':p.missing===null?'未确认':p.missing.length?p.missing.join('、'):'无',
        '已识别集号':p.episodes.join('、'),'重复集号':p.duplicates.join('、'),'无法识别集号的文件':p.unknown.join('、'),
        ...(p.confirmedEnded?{'待播集号':'','播期未知集号':''}:{}),
        '数据来源':'douban','来源链接':source.url,'来源查询时间':source.checkedAt,'豆瓣链接':source.url,'集数核对':clean?'齐全':'待核实','处理结果':finalState,
        '播出状态':p.confirmedEnded?'已播完':p.source.broadcast||'未确认'}};
  }
  function issue(row,error,rows=null) {
    const journal=load(row.key);
    return {state:error.name==='DirectoryMissing'?'目录不存在，已跳过':error.name==='Pause'?'已暂停':'复核/清理待核实',color:null,at:new Date().toISOString(),note:error.message,
      videoCount:rows?rows.filter(r=>!r.directory&&K.MEDIA.test(r.name)).length:null,record:{journal},deleted:(journal?.records||[]).filter(r=>r.state==='verified'||r.state==='manual-verified').map(r=>r.path)};
  }
  async function exclusive(work,mode='review') {
    if(running||importing)return;
    if(!navigator.locks)throw Error('浏览器不支持任务互斥锁，请使用新版 Chrome');
    await navigator.locks.request('tv-season-audit-cloud',{ifAvailable:true},async lock=>{
      if(!lock){message('另一个云盘标签页正在执行，请先暂停那里的任务');return;}
      running=true;activeMode=mode;stopping=false;render();
      try{await work();}catch(e){
        if(mode==='cleanup'&&cleanupRun)cleanupRun.paused=e.name==='Pause'&&!cleanupRun.entries.some(([row])=>load(row.key)?.pending);
        message(mode==='cleanup'&&cleanupRun?.paused?'清理执行已暂停；已提交批次已核实，点击“继续执行”处理剩余清单。':e.message);
      }finally{running=false;activeMode=null;render();}
    });
  }
  async function review() {
    if(!session)return;
    await exclusive(async()=>{
      reviewStage||={fileName:session.fileName,total:session.tasks.length,startedAt:new Date().toISOString(),attempts:[]};
      reviewStage.status='执行中';
      try {
      for(;index<session.tasks.length;) {
        checkpoint();const row=session.tasks[index];let rows=null;
        const attempt={key:row.key,name:row.name,season:row.season,path:row.path,startedAt:new Date().toISOString(),phase:'读取文件列表'};
        try {
          message(`豆瓣复核 ${index+1}/${session.tasks.length}：${row.name} / Season ${row.season}`);
          rows=await client.listing(row.path,{skipMissing:true});checkpoint();
          attempt.phase='查询豆瓣';
          const source=await client.queryDouban(row,sourceLink(row));checkpoint();
          attempt.source=structuredClone(source);attempt.phase='再次核对文件列表';
          const refreshed=await client.listing(row.path,{skipMissing:true});checkpoint();
          if(!K.equal(rows,refreshed))throw Error('复核期间文件列表发生变化，请等待下载/整理结束后重查');
          attempt.phase='分析版本与清理计划';
          const p=K.plan(row,rows,source,crypto.randomUUID()),journal=load(row.key);
          const repair=RepairState.verify(RepairState.read(row.data),p.episodes,new Date().toISOString());
          if(RepairState.pending(repair)){p.eligible=false;p.state='整理修复尚未闭环，暂缓清理';p.issues.push('修复集号仍缺失或操作结果不明，请交第二脚本核实');}
          plans.set(row,p);
          if(journal?.pending)session.updates.set(row,describe(row,p,{state:'上次删除结果未确认',journal,note:'先核实上次操作，不再发起删除'}));
          else session.updates.set(row,describe(row,p,{journal}));
          index++;render();
        }catch(e){session.updates.set(row,issue(row,e,rows));plans.delete(row);if(e.name==='Pause')throw e;index++;render();}
        finally {attempt.endedAt=new Date().toISOString();attempt.result=structuredClone(session.updates.get(row));reviewStage.attempts.push(attempt);}
      }
      message('复核完成。请检查各季的保留/拟删清单；只有点击“执行已列清理清单”并确认后才会删除。也可直接导出复核报告。');
      }finally{reviewStage.updatedAt=new Date().toISOString();reviewStage.status=index>=session.tasks.length?'已完成':'已暂停，可继续';}
    });
  }
  async function cleanup(resume=false) {
    if(resume&&!canResumeCleanup())return;
    const ready=[...plans].filter(([row,p])=>p.eligible&&!load(row.key)?.pending&&(!resume||cleanupRun.entries.some(([r,id])=>r===row&&id===p.id)&&approvedPlans.has(p.id)));
    if(!ready.length||running||importing)return;
    const newPlans=ready.filter(([,p])=>!approvedPlans.has(p.id)),count=newPlans.reduce((n,[,p])=>n+p.remove.length,0);
    // 同一页面内恢复相同清单不重复询问；重新复核生成的新清单必须重新确认。
    if(newPlans.length) {
      if(!confirm(`将按面板列出的清单，对 ${newPlans.length} 季中的 ${count} 个多余视频/专属刮削文件执行云盘普通删除，保留列出的完整版本。是否确认已核对本次清单并开始？`))return;
      newPlans.forEach(([,p])=>approvedPlans.add(p.id));
    }
    await exclusive(async()=>{
      if(!resume)cleanupRun={entries:ready.map(([row,p])=>[row,p.id]),paused:false};
      cleanupRun.paused=false;
      for(const [row,p] of ready) {
        checkpoint();
        try {
          message(`重新核实豆瓣及文件清单：${row.name} / Season ${row.season}`);
          const db=K.verifySource(row,await client.queryDouban(row,sourceLink(row)));checkpoint();
          if(db.total!==p.total||C.validSubjectUrl(db.url)!==C.validSubjectUrl(p.source.url))throw Error('豆瓣本季集数或条目已变化，请重新复核清单');
          const done=await K.executeBatch(p,{list:client.listing,removeBatch:client.deleteFiles,load,save,checkpoint,progress:b=>message(`正在批量清理 ${row.name}：本批 ${b.count} 个文件`)});
          const after=await client.listing(row.path,{uninterruptible:true});
          K.assertFresh(p,after,done.deleted);
          const verified=K.plan(row,after,db,crypto.randomUUID());
          const result=describe(row,verified,{state:verified.confirmedEnded?'齐全，清理已验证':'清理已验证，播出状态待核实',deleted:done.deleted,journal:load(row.key),note:'已验证拟删文件消失，其余文件与确认清单一致'});
          // 报告同时保留清理前的拟删清单和实际结果，不能把删除记录随重扫丢掉。
          result.remove=p.remove.map(f=>f.path);result.record.before={keep:p.keep,remove:p.remove,snapshot:p.snapshot};
          session.updates.set(row,result);plans.set(row,verified);render();
        }catch(e){session.updates.set(row,issue(row,e));render();throw e;}
      }
      cleanupRun=null;
      message('批量清理已逐文件验证。请导出更新报告；结果不明和需要人工判断的项保留黄色。');
    },'cleanup');
  }
  async function reconcile(row) {
    await exclusive(async()=>{
      const journal=load(row.key);if(!journal?.pending)return;
      const pending=journal.pending;
      if(pending.files){
        const files=pending.files;if(!files.length||files.some(f=>C.parent(f.path)!==row.path))throw Error('批次记录不属于本季');
        const rows=await client.listing(row.path),gone=files.filter(f=>!rows.some(r=>r.path===f.path)),present=files.filter(f=>rows.some(r=>r.path===f.path));
        const deleted=[...journal.records.filter(r=>r.batchId===pending.batchId&&r.state==='verified').map(r=>r.path),...gone.map(f=>f.path)];
        if(!Array.isArray(pending.beforeSnapshot)||!K.equal(pending.beforeSnapshot.filter(f=>!deleted.includes(f.path)),rows))throw Error('批次核实发现保留文件变化，请人工检查，不能解除');
        if(!confirm(`本批已消失 ${gone.length} 个，仍存在 ${present.length} 个。请确认没有在途删除；继续将逐文件记录结果，并要求重新复核，不会再次删除。\n`+files.map(f=>f.path).join('\n')))return;
        journal.records.push(...files.map(f=>({...f,state:gone.includes(f)?'manual-verified':'cancelled',finishedAt:new Date().toISOString()})));journal.pending=null;save(row.key,journal);plans.delete(row);index=Math.min(index,session.tasks.indexOf(row));
        session.updates.set(row,{state:'已人工核实批次，需重新复核',color:null,at:new Date().toISOString(),note:'已逐文件记录结果，未再次删除；请继续复核',record:{journal}});render();return;
      }
      if(C.parent(pending.path)!==row.path)throw Error('待核实记录不属于本季，不能解除');
      const rows=await client.listing(row.path),present=rows.some(r=>r.path===pending.path);
      const prompt=present?'该文件仍在目录中。请先确认上次删除已失败/取消且没有在途任务，再解除暂停。':'该文件已不在目录中。是否将其记为人工核实删除，并重新复核剩余文件？';
      if(!confirm(pending.path+'\n\n'+prompt))return;
      journal.records.push({...pending,state:present?'cancelled':'manual-verified',finishedAt:new Date().toISOString()});journal.pending=null;save(row.key,journal);
      plans.delete(row);index=Math.min(index,session.tasks.indexOf(row));
      session.updates.set(row,{state:'已人工核实上次操作，需重新复核',color:null,at:new Date().toISOString(),note:'点击继续复核后重新生成清单；此操作没有再次删除文件',record:{journal}});
      render();message('已保存人工核实结果。请点击继续复核；不会立即删除。');
    });
  }
  function render() {
    if(!ui)return;const busy=running||importing;
    ui.import.disabled=busy;ui.file.disabled=busy;ui.start.disabled=ui.resume.disabled=busy||!session||index>=session.tasks.length;
    ui.stageExport.disabled=busy||!reviewStage?.startedAt;ui.cleanPause.disabled=!running||activeMode!=='cleanup'||stopping;ui.cleanResume.disabled=!canResumeCleanup();ui.pause.disabled=!running||activeMode==='cleanup';ui.export.disabled=busy||!session;ui.prepare.disabled=busy;ui.ready.disabled=busy||!opened;
    ui.clean.disabled=busy||![...plans].some(([row,p])=>p.eligible&&!load(row.key)?.pending);
    ui.status.textContent=session?`${running?'执行中':index>=session.tasks.length?'复核已完成':'待开始 / 可继续'} · ${index}/${session.tasks.length} 个黄色项 · 无效 ${session.errors.length} 项`:'尚未导入；安装默认停止';
    ui.list.replaceChildren();
    for(const [row,result] of session?.updates||[]) {
      const p=plans.get(row),box=el('section',null,{className:'result'});
      box.append(el('strong',`${row.name} / Season ${row.season}`),el('p',result.state),el('small',result.note||''));
      if(p) {
        box.append(el('small',`豆瓣 ${p.total} 集；视频 ${p.videoCount} 个；已识别 ${p.current} 集。`));
        const details=el('details'),summary=el('summary',`查看完整清单：保留 ${p.keep.length} 个视频；拟删 ${p.remove.length} 个文件`);
        details.append(summary);
        for(const g of p.groups)details.append(el('p',`${g.complete?'完整':'缺集'}版本：${g.label}\n集号 ${g.episodes.join('、')}；本版本最新修改 ${g.latestModified}`));
        for(const file of p.keep)details.append(el('p',`保留：${file.path}\n${file.modified} · ${file.size}`));
        for(const file of p.remove)details.append(el('p',`拟删：${file.path}\n${file.modified} · ${file.size}；${file.reason}`));
        for(const file of p.preserved)details.append(el('small',`保留配套文件：${file.name}（${file.reason}）`));
        box.append(details);
      }
      const bind=button('绑定豆瓣详情并重查',()=>{
        if(running)return;
        const text=prompt(`为 ${row.name} / Season ${row.season} 指定豆瓣详情。仍须通过标题、年份及季号校验。`,sourceLink(row));if(text===null)return;
        const url=C.validSubjectUrl(text.trim());if(!url)throw Error('请输入有效的豆瓣 subject 详情链接');
        bindings.set(row.key,url);plans.delete(row);index=Math.min(index,session.tasks.indexOf(row));render();message('链接已绑定到当前页面，请继续复核。');
      });bind.disabled=busy;box.append(bind);
      const redo=button('从此季重新复核',()=>{if(running)return;plans.delete(row);index=Math.min(index,session.tasks.indexOf(row));render();message('已将本季及后续项放回待复核队列，请点击继续复核。');});redo.disabled=busy;box.append(redo);
      if(load(row.key)?.pending){const b=button('核实上次删除结果',()=>reconcile(row));b.disabled=busy;box.append(b);}
      ui.list.append(box);
    }
  }
  function exportStageReport() {
    if(running||importing||!reviewStage?.startedAt)return;
    const bytes=stageReportEngine().write(reviewStage),url=URL.createObjectURL(new Blob([bytes],{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}));
    const a=el('a',null,{href:url,download:session.fileName.replace(/\.xlsx$/i,'')+'_复核阶段报告.xlsx'});a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);
  }
  function exportReport() {
    if(running||!session)return;
    const bytes=reportEngine().write(session),url=URL.createObjectURL(new Blob([bytes],{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}));
    const a=el('a',null,{href:url,download:session.fileName.replace(/\.xlsx$/i,'')+'_复核清理.xlsx'});a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);
  }
  // 面板固定浅色控件配色，兼容站点深色模式；禁用和焦点样式独立设置。
  const setupPanelLayout=/* build:panel-layout */ null;
  function mount() {
    const host=el('div',null,{id:'tv-review-panel'});document.body.append(host);const root=host.attachShadow({mode:'open'});
    root.append(el('style',`:host{position:fixed;right:20px;bottom:20px;z-index:2147483645;font:14px/1.55 system-ui;color:#18343d}*{box-sizing:border-box}.drag-handle{cursor:move;touch-action:none;user-select:none}.box.min{width:240px}.box{width:470px;max-width:94vw;max-height:84vh;overflow:auto;border:1px solid #afc5cb;border-radius:12px;padding:15px;background:#f8fbfc;box-shadow:0 6px 30px #0003}h2{font-size:18px;margin:0 0 8px}button{padding:7px 9px;margin:4px;border:1px solid #afc5cb;border-radius:6px;background:white;cursor:pointer}:host{color-scheme:only light}button,input,select,textarea{font:inherit;color:#18343d;-webkit-text-fill-color:currentColor}button:disabled{opacity:1;color:#50626b;background:#edf1f3;cursor:not-allowed}button:not(:disabled):hover{background:#edf5f8}button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid #176484;outline-offset:2px}input{accent-color:#176484}.danger{color:#9c251c}.message{padding:9px;background:#e7f1f5}.list{max-height:350px;overflow:auto}.result{border-top:1px solid #ccdce0;padding:8px 0}small{display:block;color:#536a73}p,small{white-space:pre-wrap;overflow-wrap:anywhere}p{margin:6px 0}summary{cursor:pointer}`));
    const box=el('section',null,{className:'box'}),body=el('div');root.append(box);let panelLayout;
    const handle=el('header',null,{className:'drag-handle',title:'按住标题栏拖动'}),toggle=button('收起',()=>panelLayout.toggle());handle.append(el('h2','③ 豆瓣复核与版本清理'),toggle);box.append(handle,body);
    ui={status:el('p'),message:el('p','导入后点击开始复核。先列清单，再确认清理；本季多个完整版本只标记，交人工处理。',{className:'message'}),list:el('div',null,{className:'list'})};
    ui.prepare=button('打开豆瓣／登录准备',()=>{GM_openInTab('https://movie.douban.com/',{active:true,insert:true,setParent:true});opened=true;ui.ready.checked=false;render();message('请手动完成豆瓣登录或验证，回来勾选可访问后继续。');});
    ui.ready=el('input',null,{type:'checkbox'});const readyLabel=el('label');readyLabel.append(ui.ready,document.createTextNode(' 豆瓣已可访问'));
    ui.file=el('input',null,{type:'file',accept:'.xlsx'});ui.file.style.display='none';
    ui.import=button('导入 Excel，选择黄色项',()=>ui.file.click());ui.start=button('开始复核',review);ui.resume=button('继续复核',review);
    ui.pause=button('暂停',()=>{stopping=true;message('正在暂停；已发出的删除会先核实结果，未发出的不执行。');});
    ui.cleanPause=button('暂停执行',()=>{if(!running||activeMode!=='cleanup')return;stopping=true;message('正在暂停清理执行；已提交的当前批次会先完成核实，后续批次不再发出。');render();});
    ui.cleanResume=button('继续执行',()=>cleanup(true));
    ui.clean=button('执行已列清理清单',cleanup);ui.clean.className='danger';ui.export=button('导出复核清理报告',exportReport);ui.stageExport=button('复核阶段报告',exportStageReport);
    ui.file.addEventListener('change',async()=>{
      const file=ui.file.files?.[0];ui.file.value='';if(!file||running||importing)return;
      importing=true;render();
      try {
        if(!/\.xlsx$/i.test(file.name)||file.size>30*1024*1024)throw Error('请选择不超过 30 MB 的 .xlsx 报告');
        if(session?.updates.size&&!confirm('请先导出当前结果，再替换报告。是否继续导入？'))return;
        const imported=reportEngine().read(await file.arrayBuffer(),{origin:ORIGIN,source:SOURCE,mode:'review'});
        session={...imported,fileName:file.name};reviewStage=null;cleanupRun=null;index=0;plans.clear();bindings.clear();approvedPlans.clear();
        message(`已导入 ${session.tasks.length} 个黄色项，尚未开始。`+(session.errors.length?' 无效项：'+session.errors.map(e=>`${e.sheetName}!${e.ref} ${e.error}`).join('；'):''));
      }catch(e){message(e.message);}finally{importing=false;render();}
    });
    body.append(ui.status,ui.prepare,readyLabel,ui.import,ui.file,ui.start,ui.pause,ui.resume,ui.clean,ui.cleanPause,ui.cleanResume,ui.stageExport,ui.export,
      el('small','普通删除多余视频和唯一对应的图片/NFO；不删除目录、通用海报、归属不明文件或多个完整版本。文件修改时间取云盘显示值。'),
      el('small','先检查清单再执行。刷新前请导出；上次删除结果不明时不会自动重试。'),ui.message,ui.list);render();
    panelLayout=setupPanelLayout({host,box,body,handle,toggle,load:()=>get('panel-layout-v1',null),save:value=>put('panel-layout-v1',value)});
  }
  mount();
})();
