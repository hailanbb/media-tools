// ==UserScript==
// @name         剧集工作流 ④ · 绿色剧集归档
// @namespace    local.tv-season-archive
// @version      1.4.0
// @description  确认清单后归档绿色季；支持整剧无冲突合入并清理源目录，逐文件输出操作明细，验证成功标紫色。
// @match        https://clouddrive.example/*
// @grant        GM_getValue
// @grant        GM_setValue
// @run-at       document-idle
// @noframes
// ==/UserScript==

/* 工作流 ④：默认不运行，导入只排队，检查只读，确认移动清单才操作云盘。
 * 归档到用户配置的 DEST，并保留分类/剧名/季目录。
 * 同名电视剧支持移入目标缺少的季；同季或未知文件冲突则整剧暂缓；共用资料保留目标，迁移验证后清理源目录。
 * 本地持久记录用于防重复移动；刷新后重新导入原报告，可只读核实上次移动并恢复结果。
 */
(function () {
  'use strict';
  // 四阶段共用同一 SOURCE；DEST 是归档根目录，二者不能相同或相互包含。
  const ORIGIN='https://clouddrive.example';
  const SOURCE='/115/Library_incs/nas/电视剧';
  const DEST='/115/Library_incs/nas.finish/电视剧';
  const simplify=/* build:opencc */ null;
  const C=(/* build:cloud-core */ null)(simplify);
  const K=(/* build:archive-core */ null)(C,{source:SOURCE,destination:DEST});
  const createClient=/* build:archive-client */ null;
  let excel,report;
  function excelEngine(){return excel||=(/* build:xlsx */ null);}
  function reportEngine(){return report||=(/* build:workflow-report */ null)(excelEngine());}
  if(typeof module!=='undefined'&&module.exports){module.exports={...C,...K,createClient,reportEngine};return;}
  if(location.origin!==ORIGIN)return;
  const NS='tv-archive-v1:',load=p=>GM_getValue(NS+p,null),save=(p,v)=>GM_setValue(NS+p,structuredClone(v));
  let session=null,queue=[],index=0,running=false,importing=false,stopping=false,ui;
  const plans=new Map(),approved=new Set();
  function checkpoint(){if(stopping){const e=Error('已暂停，可继续检查');e.name='Pause';throw e;}}
  const client=createClient({...C,cleanupFiles:K.cleanupFiles},{checkpoint,source:K.SOURCE,destination:K.DEST});
  const message=text=>{ui.message.textContent=text;};
  const el=(tag,text,props={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;Object.assign(n,props);return n;};
  const button=(text,fn)=>{const b=el('button',text);b.addEventListener('click',()=>Promise.resolve().then(fn).catch(e=>message(e.message)));return b;};
  function update(rows,state,{p=null,result=null,note=''}={}) {
    for(const row of rows) {
      const detail=result||p,skipped=detail?.skipped?.find(s=>s.source===row.path);
      const target=result?.target||p?.target||'',success=result?.verified===true&&!result?.skipped?.length&&!skipped;
      const targetUrl=success?ORIGIN+'/?page=files&path='+encodeURIComponent(target+row.path.slice(row.showPath.length)):'';
      session.updates.set(row,{state:skipped?'整剧暂缓归档':state,color:success?'archived':null,source:row.showPath,target,targetUrl,at:new Date().toISOString(),note:skipped?skipped.reason+'：'+skipped.target:note,
        record:result||{...p,rows:undefined,source:row.showPath,target,manifest:p?.manifest||[],journal:load(row.showPath)}});
    }
  }
  async function exclusive(fn) {
    if(running||importing)return;
    if(!navigator.locks)throw Error('浏览器不支持互斥锁，请使用新版 Chrome');
    await navigator.locks.request('tv-season-audit-cloud',{ifAvailable:true},async lock=>{
      if(!lock){message('其他云盘任务正在执行，请先暂停');return;}running=true;stopping=false;render();
      try{await fn();}catch(e){message(e.message);}finally{running=false;render();}
    });
  }
  async function check() {
    if(!session)return;
    await exclusive(async()=>{
      // 每次开始或继续都新建快照；暂停退出后不沿用，移动阶段仍使用 client.listing。
      const previewList=K.createPreviewListing(client.listing);
      for(;index<queue.length;) {
        checkpoint();const rows=queue[index],source=rows[0].showPath;
        try {
          message(`检查 ${index+1}/${queue.length}：${rows[0].name}`);
          const previous=load(source),restore=previous?.verified&&!(await client.listing(C.parent(source))).some(f=>f.path===source&&f.directory);
          if(previous?.pending||restore){update(rows,'上次移动待核实',{note:'发现本地移动记录，请点击“核实上次移动结果”，不会重复移动'});plans.delete(source);}
          else {const p=await K.plan(rows,previewList,crypto.randomUUID());plans.set(source,p);update(rows,p.eligible?'待确认移动':'整剧暂缓归档',{p,note:!p.eligible?'同季或同名文件冲突，整剧不移动：'+p.skipped.map(x=>x.source+' → '+x.target).join('；'):p.mode==='seasons'?'移动全部季及无冲突根文件；保留目标共用资料，验证后删除源重复资料及源目录。尚未移动':'整剧各季均绿色，文件清单已读取；尚未移动'});}
          index++;render();
        }catch(e){plans.delete(source);update(rows,e.name==='Pause'?'已暂停':'暂缓归档',{note:e.message});render();if(e.name==='Pause')throw e;index++;}
      }
      message('检查完成。展开核对清单，再点击“执行已列归档清单”；也可先导出检查结果。');
    });
  }
  async function move() {
    const ready=[...plans.values()].filter(p=>p.eligible&&!load(p.source)?.pending);if(!ready.length||running||importing)return;
    const fresh=ready.filter(p=>!approved.has(p.id));
    if(fresh.length&&!confirm(`确认按已列清单，将 ${fresh.length} 部剧集归档到 ${K.DEST} 下的同名分类？目标已有同名剧集时移入全部季及根目录文件，验证后删除源目录；目标常规共用资料保留，源重复资料随源目录删除；同季或未知文件冲突均整剧暂缓，不覆盖。请确认已展开检查清单。`))return;
    fresh.forEach(p=>approved.add(p.id));
    await exclusive(async()=>{
      for(const p of ready) {
        checkpoint();message('正在重新检查并归档：'+p.name);
        try {
          const result=await K.execute(p,{list:client.listing,verifyList:path=>client.listing(path,{uninterruptible:true}),move:client.moveShow,removeEmpty:client.removeEmpty,load,save,checkpoint});
          update(p.rows,'已归档',{p,result,note:p.mode==='seasons'?'全部内容已移入目标同名剧集，目标原有内容未变，源目录已清除；路径、大小和修改时间已验证':'已核实源目录消失，目标相对路径、文件大小和文件修改时间一致'});plans.delete(p.source);render();
        }catch(e){plans.delete(p.source);update(p.rows,'归档待核实',{p,note:e.message});render();throw e;}
      }
      message('归档结果已核对。请导出报告；验证成功的剧集目录格为浅紫底、深紫字。');
    });
  }
  async function reconcile(rows) {
    await exclusive(async()=>{
      try {
        const result=await K.reconcile(rows,load(rows[0].showPath),client.listing);save(rows[0].showPath,result);
        update(rows,'已归档',{result,note:'只读复查确认上次移动成功，已恢复紫色标记'});plans.delete(rows[0].showPath);message('上次移动结果已确认，可导出报告。');
      }catch(e){update(rows,'归档待核实',{note:e.message+'；保留移动记录，不自动重试'});throw e;}
    });
  }
  async function finishCleanup(rows) {
    await exclusive(async()=>{
      const journal=load(rows[0].showPath);if(!journal)throw Error('没有待完成的归档记录');
      if(!confirm('确认验证目标完整，且源为空或仅剩已列多余共用资料后，删除本剧源目录？不会重新移动文件：'+rows[0].showPath))return;
      try {
        const result=await K.finishEmpty(journal,{list:client.listing,verifyList:path=>client.listing(path,{uninterruptible:true}),removeEmpty:client.removeEmpty,save,checkpoint});
        save(rows[0].showPath,result);update(rows,'已归档',{result,note:'全部内容及源目录消失已验证'});
      }catch(e){update(rows,'归档待核实',{result:load(rows[0].showPath),note:e.message});throw e;}
      finally{render();}
    });
  }
  function render() {
    if(!ui)return;const busy=running||importing;
    ui.import.disabled=busy;ui.start.disabled=busy||!session||index>=queue.length;ui.resume.disabled=ui.start.disabled;ui.pause.disabled=!running;
    ui.move.disabled=busy||![...plans.values()].some(p=>p.eligible&&!load(p.source)?.pending);ui.export.disabled=busy||!session;
    ui.status.textContent=session?`含绿色的电视剧 ${queue.length} 部；已检查 ${index} 部`:'尚未导入报告';ui.list.replaceChildren();
    for(const rows of queue) {
      const source=rows[0].showPath,p=plans.get(source),result=session.updates.get(rows[0]),box=el('div',null,{className:'result'});
      box.append(el('strong',rows[0].name),el('p',result?.state||'待检查'));if(result?.note)box.append(el('p',result.note));
      if(p) {
        const details=el('details');details.append(el('summary',`归档清单：${rows.length} 季，${p.manifest.filter(f=>!f.directory).length} 个文件`),el('p',`从：${p.source}\n到：${p.target}`));
        if(p.eligible&&p.cleanup){details.append(el('p','移动验证后删除源目录：'+p.source));for(const f of p.cleanup.discardFiles||[])details.append(el('p','删除源端多余共用资料（目标同名文件保留）：'+f.path));}
        for(const op of p.operations||[])details.append(el('p',`移动：${op.source}\n到：${op.target}`));
        for(const s of p.skipped||[])details.append(el('p',`整剧暂缓，冲突条目：${s.source}\n目标：${s.target}`));
        for(const f of p.manifest)details.append(el('p',`${p.preserved?.some(x=>x.path===f.path)?'保留原位':f.directory?'目录':'文件'}：${f.path}\n${f.modified} · ${f.size}`));box.append(details);
      }
      const redo=button('从此剧重新检查',()=>{plans.delete(source);index=Math.min(index,queue.indexOf(rows));render();message('已排队，请点击继续检查。');});redo.disabled=busy;box.append(redo);
      if(load(source)?.cleanup){const cleanup=button('完成源目录清理',()=>finishCleanup(rows));cleanup.disabled=busy;box.append(cleanup);}
      if(load(source)){const b=button('核实上次移动结果',()=>reconcile(rows));b.disabled=busy;box.append(b);}ui.list.append(box);
    }
  }
  function exportReport() {
    if(running||!session)return;
    const bytes=reportEngine().write(session),url=URL.createObjectURL(new Blob([bytes],{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}));
    el('a',null,{href:url,download:session.fileName.replace(/\.xlsx$/i,'')+'_归档.xlsx'}).click();setTimeout(()=>URL.revokeObjectURL(url),10000);
  }
  // 面板固定浅色控件配色，兼容站点深色模式；禁用和焦点样式独立设置。
  const setupPanelLayout=/* build:panel-layout */ null;
  function mount() {
    const host=el('div',null,{id:'tv-archive-panel'});document.body.append(host);const root=host.attachShadow({mode:'open'});
    root.append(el('style',`:host{position:fixed;right:20px;bottom:20px;z-index:2147483645;font:14px/1.55 system-ui;color:#332541}*{box-sizing:border-box}.drag-handle{cursor:move;touch-action:none;user-select:none}.box.min{width:240px}.box{width:470px;max-width:94vw;max-height:84vh;overflow:auto;padding:15px;background:#faf8fc;border:1px solid #bda6d2;border-radius:12px;box-shadow:0 6px 30px #0003}h2{font-size:18px;margin:0}button{margin:4px;padding:7px;border:1px solid #bda6d2;border-radius:6px;background:white;cursor:pointer}:host{color-scheme:only light}button,input,select,textarea{font:inherit;color:#332541;-webkit-text-fill-color:currentColor}button:disabled{opacity:1;color:#65576f;background:#eee8f2;cursor:not-allowed}button:not(:disabled):hover{background:#f0e9f6}button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid #745297;outline-offset:2px}input{accent-color:#745297}.message{padding:9px;background:#eee7f4}.list{max-height:350px;overflow:auto}.result{border-top:1px solid #ccc;padding:8px 0}p,small{white-space:pre-wrap;overflow-wrap:anywhere}summary{cursor:pointer}`));
    const box=el('section',null,{className:'box'}),body=el('div');root.append(box);let panelLayout;
    const handle=el('header',null,{className:'drag-handle',title:'按住标题栏拖动'}),toggle=button('收起',()=>panelLayout.toggle());handle.append(el('h2','④ 绿色剧集归档'),toggle);box.append(handle,body);
    ui={status:el('p'),message:el('p','导入只排队；开始检查只读；确认具体清单后才移动。',{className:'message'}),list:el('div',null,{className:'list'})};
    ui.file=el('input',null,{type:'file',accept:'.xlsx'});ui.file.style.display='none';
    ui.import=button('导入 Excel，检查绿色项',()=>ui.file.click());ui.start=button('开始检查',check);ui.resume=button('继续检查',check);
    ui.pause=button('暂停',()=>{stopping=true;message('正在暂停；已发出的移动会先核实结果。');});ui.move=button('执行已列归档清单',move);ui.export=button('导出归档报告',exportReport);
    ui.file.addEventListener('change',async()=>{
      const file=ui.file.files?.[0];ui.file.value='';if(!file||running||importing)return;importing=true;render();
      try {
        if(!/\.xlsx$/i.test(file.name)||file.size>30*1024*1024)throw Error('请选择不超过 30 MB 的 .xlsx 报告');
        if(session?.updates.size&&!confirm('请先导出当前结果。是否替换当前报告？'))return;
        const imported=reportEngine().read(await file.arrayBuffer(),{origin:ORIGIN,source:K.SOURCE,destination:K.DEST,mode:'archive'});
        // 无效行可能正是绿色整剧缺失的一季，不能静默丢弃后执行整剧移动。
        if(imported.errors.length)throw Error('报告含无法确认的行，请先修正再导入：'+imported.errors.map(e=>`${e.sheetName}!${e.ref} ${e.error}`).join('；'));
        session={...imported,fileName:file.name};queue=K.groups(session.tasks);index=0;plans.clear();approved.clear();
        const directoryRows=session.tasks.filter(r=>r.directoryOnly);
        // 目录级异常原色保留，导出可追溯；同剧绿色季仍会被整剧校验阻挡。
        for(const row of directoryRows)update([row],'暂缓归档',{note:'目录级记录，季号未确认；原检查结果：'+String(row.data['处理结果']||'未确认')+'；'+String(row.data['核对说明']||'')});
        message(`已导入 ${queue.length} 部含绿色行的电视剧，尚未运行。`+(directoryRows.length?`另有 ${directoryRows.length} 条目录级记录暂缓归档，已记录到导出报告。`:''));
      }catch(e){message(e.message);}finally{importing=false;render();}
    });
    body.append(ui.status,el('p','归档目标：'+K.DEST),ui.import,ui.file,ui.start,ui.pause,ui.resume,ui.move,ui.export,
      el('small','所有实际季须在报告中且为绿色。同季或未知文件冲突则整剧暂缓；目标共用资料保留；重复视频、未知目录、文件变化暂缓。只使用已存在的目标分类。'),ui.message,ui.list);render();
    panelLayout=setupPanelLayout({host,box,body,handle,toggle,load:()=>GM_getValue(NS+'panel-layout-v1',null),save:value=>GM_setValue(NS+'panel-layout-v1',value)});
  }
  mount();
})();
