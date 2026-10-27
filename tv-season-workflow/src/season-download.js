// ==UserScript==
// @name         剧集工作流 ② · MoviePilot 补缺下载
// @namespace    local.tv-season-download
// @version      1.5.0
// @description  手动补缺；整季优先、中文标记优先，做种≥5时优先小容量；逐资源导出选择依据。
// @match        https://moviepilot.example/*
// @grant        unsafeWindow
// @grant        GM_getValue
// @grant        GM_setValue
// @run-at       document-idle
// @noframes
// ==/UserScript==

/* 使用流程：导入原报告 → 查看待处理数量 → 点击开始 → 导出更新后的报告。
 * 导入、安装和刷新均不发起下载。开始后按既定规则提交到 MoviePilot 默认下载器及自动目录。
 * 补缺下载之外，可手动检查整理阻挡；确认后仅删除指定历史记录并复制整理单集视频。
 * 不删除视频、下载任务或全局历史；第三脚本负责实际云盘复核。
 * 下载记录保留任务、站点、容量、中文标记、选择依据及时间；不保存登录凭据或种子私密链接。
 */
(function () {
  'use strict';
  // 首次安装只改这三项及顶部 @match；CLOUD/SOURCE 必须与第一脚本相同。
  const ORIGIN='https://moviepilot.example';
  const CLOUD='https://clouddrive.example';
  const SOURCE='/115/Library_incs/nas/电视剧';
  const simplify=/* build:opencc */ null;
  const Titles=(/* build:title-text */ null)(simplify);
  const VersionCloud=(/* build:cloud-core */ null)(simplify);
  const VersionHelpers=(/* build:version-helpers */ null)(VersionCloud);
  const Versions=(/* build:version-download */ null)(VersionHelpers);
  const Broadcast=(/* build:broadcast-audit */ null)();
  const Core=(/* build:download-core */ null)(Titles,Versions,Broadcast);
  const RepairState=(/* build:repair-state */ null)();
  const Repair=(/* build:transfer-repair */ null)(Core,VersionCloud,RepairState,Versions);
  // MoviePilot 本地文件管理的目录范围，不是 CloudDrive2 路径；按自己的部署修改。
  const REPAIR_CONFIG={source:'/video/downloads/电视剧',target:'/video/link/电视剧'};
  const repairPlans=new Map();
  let excel,report;
  const excelEngine=()=>excel||=(/* build:xlsx */ null);
  function reportEngine(){return report||=(/* build:workflow-report */ null)(excelEngine());}
  if(typeof module!=='undefined'&&module.exports){module.exports={...Core,Versions,Broadcast,Repair,RepairState,reportEngine};return;}
  if(location.origin!==ORIGIN)return;
  const NS='tv-download-v1:';
  let session=null,index=0,running=false,importing=false,stopping=false,ui;
  const get=(key,fallback)=>GM_getValue(NS+key,fallback);
  const put=(key,value)=>GM_setValue(NS+key,value);
  function checkpoint(){if(stopping)throw new Core.Pause();}
  const el=(tag,text,props={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;Object.assign(n,props);return n;};
  const button=(label,fn)=>{const b=el('button',label);b.addEventListener('click',()=>Promise.resolve().then(fn).catch(e=>message(e.message)));return b;};
  const message=text=>{ui.message.textContent=text;};
  function repairRecord(row){
    const imported=RepairState.read(row.data),local=get('repair:'+row.key,null),current=session?.updates.get(row)?.repair;
    if(RepairState.uncertain(local)&&!current)return local;
    return [local,current,imported].filter(Boolean).sort((a,b)=>Date.parse(b.at)-Date.parse(a.at))[0]||null;
  }
  function setUpdate(row,result){session.updates.set(row,{...result,repair:result.repair||repairRecord(row)||undefined});}
  function repairUpdate(row,r){
    put('repair:'+row.key,r);
    const previous=session.updates.get(row),task=Core.taskFromRow(row),records=Core.readRecords(load(task.key).length?load(task.key):task.records);
    setUpdate(row,{...previous,state:RepairState.label(r),color:'yellow',records,covered:previous?.covered||[],remaining:task.needed,at:r.at,note:r.note,repair:r});
  }
  async function repairWork(action){
    if(running||importing||!session)return;
    if(!navigator.locks)throw Error('浏览器不支持互斥锁');
    await navigator.locks.request('tv-workflow-moviepilot-download',{ifAvailable:true},async lock=>{
      if(!lock){message('其他补缺或修复任务正在运行');return;}
      const targets=action==='execute'?[...repairPlans.keys()]:session.tasks;
      if(action==='execute'&&(!targets.length||!confirm('请确认以下下载文件已经下载完成，并允许只删除列出的旧整理历史，再复制整理单集视频。不会删除源文件、目标文件或下载任务。\n'+targets.map(row=>repairPlans.get(row).items.map(i=>'['+row.name+' S'+row.season+'E'+i.episode+'] 历史ID '+i.id+'\n'+i.source+' → '+i.target).join('\n')).join('\n'))))return;
      running=true;stopping=false;render();
      try{
        const api=Repair.adapter(unsafeWindow.MoviePilotAPI,ORIGIN);
        for(const row of targets){checkpoint();message(row.name+' / Season '+row.season+'：'+(action==='check'?'检查整理问题':action==='execute'?'执行整理修复':'只读核实修复结果'));
          let task;
          try{
            task=Core.taskFromRow(row);const ledger=load(task.key);if(ledger.length)task.records=Core.readRecords(ledger);
            const previous=repairRecord(row);
            if(action==='check'){
              repairPlans.delete(row);
              if(RepairState.uncertain(previous)||previous?.state==='submitted'){repairUpdate(row,previous);continue;}
              const r=await Repair.check(task,api,REPAIR_CONFIG,checkpoint);if(previous)r.previousItems=[...(previous.previousItems||[]),...previous.items.filter(i=>i.steps?.length)];repairUpdate(row,r);if(r.state==='ready')repairPlans.set(row,r);
            }else if(action==='execute'){
              const plan=repairPlans.get(row);repairPlans.delete(row);
              await Repair.execute(task,plan,api,REPAIR_CONFIG,{checkpoint,save:r=>repairUpdate(row,r)});
            }else if(previous?.items?.length){repairUpdate(row,await Repair.reconcile(task,previous,api,REPAIR_CONFIG,checkpoint));repairPlans.delete(row);}
          }catch(e){
            const previous=repairRecord(row);
            if(previous){setUpdate(row,{...session.updates.get(row),state:RepairState.label(previous),note:e.message,repair:previous});}
            else setUpdate(row,{state:'整理修复待核实',color:'yellow',records:task?.records||[],note:e.message,at:new Date().toISOString()});
            if(action!=='check'||e.name==='Pause')throw e;
          }
          render();
        }
        message(action==='check'?'检查完成。展开逐文件清单，确认下载完成后点击“修复整理”。':action==='execute'?'本轮修复已提交，请导出报告交第三脚本复核。':'只读核实完成，请导出报告。');
      }catch(e){message(e.message);}finally{running=false;render();}
    });
  }
  function render(){
    const busy=running||importing;
    ui.import.disabled=busy;ui.file.disabled=busy;ui.start.disabled=busy||!session||index>=session.tasks.length;
    ui.resume.disabled=busy||!session||index>=session.tasks.length;ui.pause.disabled=!running;ui.export.disabled=busy||!session;
    ui.status.textContent=session?`${running?'执行中':index>=session.tasks.length?'本轮已处理':'待开始 / 可继续'} · ${index}/${session.tasks.length} 项 · 跳过绿色/已归档 ${session.skipped} 项 · 无效 ${session.errors.length} 项`:'尚未导入报告，默认停止';
    ui.checkRepair.disabled=busy||!session;ui.executeRepair.disabled=busy||!repairPlans.size;ui.reconcileRepair.disabled=busy||!session;
    ui.list.replaceChildren();
    for(const [t,r] of [...session?.updates||[]].slice(-60)) {
      const line=el('p',`${t.name} / Season ${t.season}：${r.state}。${r.note||''}`);
      if(r.repair){const details=el('details');details.append(el('summary','整理修复清单与记录'));for(const i of r.repair.items)details.append(el('p',`第 ${i.episode} 集 · 历史ID ${i.id} · ${i.state}\n源：${i.source}\n目标：${i.target}\n${i.note||''}`));line.append(details);}
      if(r.resourceAudit)line.append(el('small',`播出复核：${r.resourceAudit.status}；资源站最高集号：${r.resourceAudit.maximum??'未确认'}。${r.resourceAudit.basis}`));
      if(r.records?.length) {
        const reset=button('核实任务已取消，解除本季防重复',()=>{
          if(running)return;
          if(RepairState.pending(repairRecord(t)))throw Error('整理修复尚未闭环，不能通过取消下载记录解除');
          if(!confirm(`仅在 MoviePilot 中确认“${t.name} / Season ${t.season}”相关任务均已取消或失败后使用。解除后再次开始可能重新下载，是否确认？`))return;
          // 用户核实后只清除本季本地防重复记录，不取消或删除任何真实下载。
          save(t.key,[]);t.data['下载记录']='';t.data['下载处理']='';
          for(const h of Object.keys(t.data))if(/^下载记录续\d+$/.test(h))t.data[h]='';
          setUpdate(t,{state:'人工确认原任务已取消',color:null,records:[],covered:[],remaining:null,at:new Date().toISOString(),note:'仅解除本地防重复记录；再次点击开始/继续才会处理'});
          index=Math.min(index,session.tasks.indexOf(t));render();message('已解除这一季的本地记录，尚未重新下载。请继续本轮或导出记录。');
        });
        reset.disabled=running;line.append(reset);
      }
      ui.list.append(line);
    }
  }
  // 原 Excel 留在内存；本地日志跨刷新保留，用于防止重导旧报告后重复下载。
  function load(key){return get('ledger:'+key,[]);}
  function save(key,records){put('ledger:'+key,records.map(Core.safeRecord));}
  async function run(){
    if(running||importing||!session||index>=session.tasks.length)return;
    if(!navigator.locks)throw Error('当前浏览器不支持任务互斥锁，请使用新版 Chrome');
    await navigator.locks.request('tv-workflow-moviepilot-download',{ifAvailable:true},async lock=>{
      if(!lock){message('其他 MoviePilot 标签页正在执行补缺，请先暂停那里的任务');return;}
      running=true;stopping=false;render();
      try {
        const api=Core.createAdapter(unsafeWindow.MoviePilotAPI,ORIGIN);
        for(;index<session.tasks.length;) {
          checkpoint();const row=session.tasks[index];let task;
          try {
            task=Core.taskFromRow(row);
            const repair=repairRecord(row);if(RepairState.pending(repair)&&repair.items.length){setUpdate(row,{state:RepairState.label(repair),color:'yellow',records:Core.readRecords(load(task.key).length?load(task.key):task.records),note:'已有整理修复记录，请使用检查/核实整理功能，避免重复下载',repair});index++;render();continue;}
            const result=await Core.processTask(task,{...api,load,save,checkpoint,progress:text=>message(`${row.name} / Season ${row.season}：${text}`)});
            setUpdate(row,result);index++;render();
          } catch(e) {
            if(e.name==='Pause') {
              // 暂停可能发生在一部分零散集已提交后；原表导出也应保留这些成功记录。
              if(task){const records=Core.readRecords(load(task.key));setUpdate(row,Core.outcome(task,records,{state:'已暂停，待继续',color:records.some(r=>['accepted','existing'].includes(r.state))?'yellow':null,note:'已保存本季提交记录；继续时不会重下已覆盖集号'}));}
              throw e;
            }
            const note=e.message||'处理失败';
            const records=task?Core.readRecords(load(task.key)):[];
            setUpdate(row,{state:'待核实',color:records.some(r=>['accepted','existing'].includes(r.state))?'yellow':null,note,records,covered:[],remaining:task?.needed??null,at:new Date().toISOString()});
            // 来源格式/数据错误只影响本行；网络/权限错误暂停队列，不把整表标红。
            if(task)throw Error(note);
            index++;
          }
        }
        message('本轮处理完成。请导出报告；黄色项待下载完成后交给第三个脚本复核。');
      }catch(e){message(e.message);}finally{running=false;render();}
    });
  }
  function exportReport(){
    if(!session||running)return;
    const bytes=reportEngine().write(session),url=URL.createObjectURL(new Blob([bytes],{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}));
    const a=el('a',null,{href:url,download:session.fileName.replace(/\.xlsx$/i,'')+'_下载补充.xlsx'});a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);
  }
  // 面板固定浅色控件配色，兼容站点深色模式；禁用和焦点样式独立设置。
  const setupPanelLayout=/* build:panel-layout */ null;
  function mount(){
    const host=el('div',null,{id:'tv-download-panel'});document.body.append(host);const root=host.attachShadow({mode:'open'});
    const style=el('style',`:host{position:fixed;right:20px;bottom:20px;z-index:2147483645;font:14px/1.55 system-ui;color:#18343d}*{box-sizing:border-box}.drag-handle{cursor:move;touch-action:none;user-select:none}.box.min{width:240px}.box{width:430px;max-width:92vw;max-height:82vh;overflow:auto;padding:16px;border:1px solid #afc5cb;border-radius:12px;background:#f8fbfc;box-shadow:0 6px 30px #0003}h2{font-size:18px;margin:0 0 8px}button{padding:7px 10px;margin:4px;border:1px solid #afc5cb;border-radius:6px;background:white;cursor:pointer}:host{color-scheme:only light}button,input,select,textarea{font:inherit;color:#18343d;-webkit-text-fill-color:currentColor}button:disabled{opacity:1;color:#50626b;background:#edf1f3;cursor:not-allowed}button:not(:disabled):hover{background:#edf5f8}button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid #176484;outline-offset:2px}input{accent-color:#176484}.message{background:#e7f1f5;padding:9px;white-space:pre-wrap;overflow-wrap:anywhere}.list{max-height:240px;overflow:auto}small{display:block;color:#536a73}p{margin:7px 0}`);
    const box=el('section',null,{className:'box'}),body=el('div');root.append(style,box);
    let panelLayout;const head=el('h2','② MoviePilot 补缺下载'),toggle=button('收起',()=>panelLayout.toggle()),handle=el('header',null,{className:'drag-handle',title:'按住标题栏拖动'});handle.append(head,toggle);box.append(handle,body);
    ui={status:el('p'),message:el('p','导入后点击开始才会下载。沿用 MoviePilot 默认下载器与自动保存目录。',{className:'message'}),list:el('div',null,{className:'list'})};
    ui.file=el('input',null,{type:'file',accept:'.xlsx'});ui.file.style.display='none';
    ui.import=button('导入 Excel',()=>ui.file.click());ui.start=button('开始补缺下载',run);ui.resume=button('继续本轮',run);
    ui.checkRepair=button('检查整理问题',()=>repairWork('check'));ui.executeRepair=button('修复整理',()=>repairWork('execute'));ui.reconcileRepair=button('核实修复结果',()=>repairWork('reconcile'));
    ui.pause=button('暂停',()=>{stopping=true;message('正在暂停。已发出的下载请求会先记录结果，避免重复提交。');});ui.export=button('导出更新报告',exportReport);
    ui.file.addEventListener('change',async()=>{
      const file=ui.file.files?.[0];ui.file.value='';if(!file||running||importing)return;
      importing=true;render();
      try {
        if(!/\.xlsx$/i.test(file.name)||file.size>30*1024*1024)throw Error('请选择不超过 30 MB 的 .xlsx 报告');
        if(session?.updates.size&&!confirm('替换当前报告前请先导出结果。是否继续导入？'))return;
        const imported=reportEngine().read(await file.arrayBuffer(),{origin:CLOUD,source:SOURCE});
        session={...imported,fileName:file.name};index=0;repairPlans.clear();
        message(`已导入 ${session.tasks.length} 个非绿色项，尚未执行。`+(session.errors.length?' 无法处理：'+session.errors.map(e=>`${e.sheetName}!${e.ref} ${e.error}`).join('；'):''));render();
      }catch(e){message(e.message);}finally{importing=false;render();}
    });
    body.append(ui.status,ui.import,ui.file,ui.start,ui.pause,ui.resume,ui.checkRepair,ui.executeRepair,ui.reconcileRepair,ui.export,
      el('small','规则：整季包优先，无合格包补零散集；同类优先中文标记，做种≥5且容量已知时优先小容量，否则优先做种数；最低2个做种。零散多集按每个待补集估算容量。绿/紫跳过；黄＝已提交待复核，红＝无合格资源。'),
      el('small','结果不明的任务不自动重试。刷新前先导出；重新导入旧报告也会读取本地防重复记录。'),ui.message,ui.list);
    render();
    panelLayout=setupPanelLayout({host,box,body,handle,toggle,load:()=>get('panel-layout-v1',null),save:value=>put('panel-layout-v1',value)});
  }
  mount();
})();
