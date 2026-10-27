/* CloudDrive2 / 豆瓣页面适配层。
 * 读取通过当前界面完成，不读取登录秘密值；删除仅使用普通文件菜单及可核对的确认窗口。
 * 永久删除、目录删除、列表加载失败、分页不全、选择范围改变时均停止。
 */
(function createCloudReviewClient(C,options) {
  'use strict';
  const {get,put,del,checkpoint:check,message,ready}=options;
  const SOURCE=options.source||'/115/Library_incs/nas/电视剧';
  const {parent,base,showInfo,mapKey,titleIdentity,candidateMatches,chooseCandidate,matchFailure,validSubjectUrl,parseTotal,parseDoubanBroadcast,doubanAccessProblem,createListingSettler}=C;
  const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms)),now=()=>new Date().toISOString();
  const visible=e=>!!e&&!!e.getClientRects().length;
  let finishing=0;
  const checkpoint=()=>{if(!finishing)check();};
  function pause(text){const e=Error(text);e.name='Pause';return e;}
  async function waitFor(fn,text,timeout=45000) {
    const until=Date.now()+timeout;
    while(Date.now()<until){checkpoint();const value=fn();if(value)return value;await sleep(250);}
    throw Error(text);
  }
  function setInput(input,value) {
    const proto=input.tagName==='SELECT'?HTMLSelectElement.prototype:HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto,'value').set.call(input,value);
    input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));
  }
  function currentPath(){return new URL(location.href).searchParams.get('path')?.replace(/\/$/,'')||'/';}
  function breadcrumbPath(){return '/'+[...document.querySelectorAll('.files-breadcrumb-bar .breadcrumb-item:not(.breadcrumb-root)')].map(e=>e.textContent.trim()).join('/');}
  function allowed(path){return path.startsWith(SOURCE+'/')&&!path.split('/').some(s=>s==='.'||s==='..');}
  // 顶部 health-indicator 不是文件列表是否可用的可靠依据，不能作为读取或操作门槛。
  // 与初检一致，依据刷新后的完整、稳定列表；删除/移动还需原有快照、确认窗口及事后验证。
  function listLoading(){return [...document.querySelectorAll('.file-list-scroll-area .loading-bar')].some(visible);}
  function requireListReady({allowLoading=false,skipMissing=false,path=currentPath()}={}) {
    const errorText=[...document.querySelectorAll('.files-load-error')].filter(visible).map(e=>(e.textContent||'列表加载失败').trim()||'列表加载失败').join('；');
    if(allowLoading&&listLoading())return; // 导航/刷新中可能残留上一目录错误，等当前加载结束再判断。
    if(errorText) {
      // 仅只读复核主动允许跳过明确的不存在；权限、网络、超时及一般404不能据此跳过。
      // 删除/移动和事后核实不传此选项，仍暂停核实，不能把文件消失直接当作操作成功。
      // CloudDrive2 实际文案：get_subfiles of "路径" error: not found "目录名" under "父路径"。
      // 祖先剧目录缺失也会导致 Season 目录不可读；仅接受与本次请求一致的缺失路径。
      const native=/get_subfiles\s+of\s+"([^"]+)"\s+error:\s*not found\s+"([^"/]+)"\s+under\s+"([^"]+)"/i.exec(errorText);
      const missingPath=native?native[3].replace(/\/$/,'')+'/'+native[2]:'';
      const nativeMissing=!!native&&(path===missingPath||path.startsWith(missingPath+'/'))&&(path===native[1]||path.startsWith(native[1]+'/'));
      const missing=nativeMissing||/(?:文件|文件夹|目录|路径)(?:不存在|未找到)|找不到(?:指定的?)?(?:文件|文件夹|目录|路径)|(?:file|folder|directory|path) (?:does not exist|not found)|no such file or directory|code\s*[:=]\s*NotFound\b/i.test(errorText);
      if(skipMissing&&missing&&currentPath()===path&&!listLoading()) {
        const error=Error('目录不存在，已跳过本季：'+path+'；页面提示：'+errorText.slice(0,500));error.name='DirectoryMissing';error.path=path;throw error;
      }
      throw pause('云盘文件列表加载失败，已暂停当前任务：'+errorText.slice(0,500));
    }
    if(!allowLoading&&listLoading())throw pause('云盘文件列表仍在加载，已暂停当前任务；请等待加载完成后点击继续');
  }
  function readRows(){return [...document.querySelectorAll('table.file-list tr.file-row[data-path]')].map(e=>({path:e.getAttribute('data-path'),name:e.querySelector('.file-name-text')?.textContent.trim()||'',directory:e.classList.contains('directory'),modified:e.querySelector('.file-modified')?.textContent.trim()||'',size:e.querySelector('.file-size')?.textContent.trim()||''}));}
  function readCount(){const m=/^\s*([\d,]+)\s*items\b/.exec(document.querySelector('.file-list-status')?.textContent||'');return m?Number(m[1].replace(/,/g,'')):null;}
  async function navigate(path) {
    checkpoint();if(!allowed(path))throw Error('目录不在允许的电视剧源目录内');
    await waitFor(()=>document.querySelector('.files-breadcrumb-bar .breadcrumb-edit-btn'),'请打开 CloudDrive2 文件浏览器');
    if(currentPath()!==path||breadcrumbPath()!==path) {
      let input=document.querySelector('.files-breadcrumb-bar input.breadcrumb-path-input');
      if(!input){document.querySelector('.files-breadcrumb-bar .breadcrumb-edit-btn').click();input=await waitFor(()=>document.querySelector('.files-breadcrumb-bar input.breadcrumb-path-input'),'路径框未出现');}
      setInput(input,path);input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true}));
      await waitFor(()=>currentPath()===path&&breadcrumbPath()===path,'路径导航失败');
    }
    const mode=document.querySelector('button[aria-label="列表视图"]');if(visible(mode))mode.click();
    const filter=document.querySelector('.unified-header .uh-filter-input');if(filter?.value)setInput(filter,'');
  }
  // 请求结束先刷新实际发起操作的目录；即使用户点暂停也先完成刷新，再交核心验证。
  async function refreshAfterOperation(path) {
    finishing++;
    try {
      if(currentPath()!==path||breadcrumbPath()!==path)throw Error('操作后路径改变，结果待核实');
      const refresh=await waitFor(()=>document.querySelector('button.toolbar-refresh-btn'),'操作后刷新控件不存在');refresh.click();
      await sleep(250);
      await waitFor(()=>{if(currentPath()!==path||breadcrumbPath()!==path)throw Error('刷新时工作目录改变');requireListReady({allowLoading:true});return !listLoading();},'操作后刷新未完成，结果待核实');
      requireListReady();
    }finally{finishing--;}
  }
  async function listing(path,{uninterruptible=false,skipMissing=false}={}) {
    if(uninterruptible)finishing++;
    try {
      await navigate(path);
      const refresh=await waitFor(()=>document.querySelector('button.toolbar-refresh-btn'),'刷新控件不存在');refresh.click();
      await sleep(250);checkpoint(); // 等页面提交本次刷新状态，避免沿用上一目录错误。
      const select=await waitFor(()=>{requireListReady({allowLoading:true,skipMissing,path});return document.querySelector('select.page-size-select');},'分页控件不存在');
      if(![...select.options].some(o=>o.value==='all'))throw Error('无法读取全部条目');
      if(select.value!=='all')setInput(select,'all');
      const settle=createListingSettler(path);
      return await waitFor(()=>{
        if(currentPath()!==path||breadcrumbPath()!==path)throw pause('工作目录已切换，停止当前操作');
        requireListReady({allowLoading:true,skipMissing,path});
        const rows=readRows(),count=readCount(),loading=listLoading();
        const emptyConfirmed=[...document.querySelectorAll('table.file-list .empty-state .empty-content p')].some(e=>visible(e)&&/^(此文件夹为空|This folder is empty)[.!。！]?$/i.test(e.textContent.trim()));
        return settle({rows,count,loading,emptyConfirmed,allSelected:document.querySelector('select.page-size-select')?.value==='all'},Date.now());
      },'文件列表未读全，不能确认数量或删除结果');
    }catch(e){if(e.name==='Pause'||e.name==='DirectoryMissing')throw e;throw pause('目录读取未完成，已暂停当前任务：'+e.message);}
    finally{if(uninterruptible)finishing--;}
  }
  // 标准词条读取逻辑在构建时从初检源码复用；请求键独立为 tv_review，不会与初检串话。
  /* build:douban-worker */
  async function queryDouban(task,boundUrl) {
    if(!ready())throw pause('请先打开豆瓣完成登录准备，勾选可访问后继续');
    await sleep(3000);checkpoint();
    const show=showInfo(task.name),id=crypto.randomUUID(),request={show,season:task.season,manual:false,created:Date.now()};
    const url=boundUrl?validSubjectUrl(boundUrl):null;if(boundUrl&&!url)throw Error('豆瓣链接无效');
    put('request:'+id,request);let tab,reply;
    try {
      tab=GM_openInTab((url||`https://search.douban.com/movie/subject_search?search_text=${encodeURIComponent(show.title+(task.season>1?' 第'+task.season+'季':''))}&cat=1002`)+'#tv_review='+id,{active:false,insert:true,setParent:true});
      reply=await waitFor(()=>get('response:'+id,null),'豆瓣未返回结果，请检查登录或验证后继续',90000);
      if(reply.blocked)throw pause(reply.error||'豆瓣需要人工验证');
      if(reply.error)throw Error(reply.error);
      return {...reply,source:'douban'};
    }finally{del('request:'+id);del('response:'+id);if(reply&&!reply.blocked)tab?.close();}
  }
  const cleanText=e=>e.textContent.replace(/[\uE000-\uF8FF]/g,'').trim();
  function rowFor(path){return [...document.querySelectorAll('table.file-list tr.file-row[data-path]')].find(e=>e.getAttribute('data-path')===path);}
  function selected(){return [...document.querySelectorAll('table.file-list tr.file-row.selected')];}
  function cancelDelete(){const d=[...document.querySelectorAll('[aria-labelledby="delete-confirm-title"]')].find(visible);[...d?.querySelectorAll('button')||[]].find(b=>cleanText(b)==='取消')?.click();}
  async function deleteFile(file,expectedRows) {
    let submitted=false;
    function unchanged() {
      if(!Array.isArray(expectedRows)||!expectedRows.length)throw Error('缺少删除前完整快照');
      const key=r=>JSON.stringify([r.path,r.name,!!r.directory,r.modified,r.size]),current=readRows(),expected=new Map(expectedRows.map(r=>[r.path,key(r)]));
      if(current.length!==expectedRows.length||current.some(r=>expected.get(r.path)!==key(r))||readCount()!==expectedRows.length||document.querySelector('select.page-size-select')?.value!=='all')throw Error('确认前文件列表或分页发生变化');
    }
    try {
      checkpoint();requireListReady();
      if(file.directory||!allowed(file.path)||currentPath()!==parent(file.path)||breadcrumbPath()!==parent(file.path))throw Error('拒绝删除目录、越界文件或其他工作目录中的文件');
      if([...document.querySelectorAll('[role="dialog"]')].some(visible))throw Error('已有其他对话框打开，请先关闭后重新执行');
      unchanged();
      for(const e of selected())e.querySelector('.file-icon')?.dispatchEvent(new MouseEvent('click',{bubbles:true,ctrlKey:true}));
      await waitFor(()=>!selected().length,'无法清除旧文件选择');
      const row=rowFor(file.path);if(!row||row.classList.contains('directory'))throw Error('待删文件已经变化');
      const current=readRows().find(r=>r.path===file.path);
      if(!current||current.name!==file.name||current.modified!==file.modified||current.size!==file.size)throw Error('待删文件的名称、大小或时间已经变化');
      row.querySelector('.file-name-ellipsis')?.click();
      const menu=await waitFor(()=>[...document.querySelectorAll('[role="menu"]')].find(visible),'文件菜单未出现');
      const chosen=selected();if(chosen.length!==1||chosen[0].getAttribute('data-path')!==file.path)throw Error('文件选择范围不等于待删单文件');
      const remove=[...menu.querySelectorAll('button')].find(b=>cleanText(b)==='删除');if(!remove)throw Error('找不到普通删除入口');remove.click();
      const dialog=await waitFor(()=>[...document.querySelectorAll('[aria-labelledby="delete-confirm-title"]')].find(visible),'删除确认窗口未出现');
      const names=[...dialog.querySelectorAll('.delete-file-name')];
      if(names.length!==1||names[0].textContent.trim()!==file.name)throw Error('确认窗口中的文件清单与计划不一致');
      if(dialog.querySelector('input:checked')||/永久|不可恢复|permanent/i.test(dialog.textContent))throw Error('检测到永久删除选项，本脚本不执行');
      if(dialog.querySelector('.delete-file-size')?.textContent.trim()!==file.size)throw Error('确认窗口中的文件大小不一致');
      checkpoint();requireListReady();
      unchanged();
      if(currentPath()!==parent(file.path)||selected().length!==1||selected()[0].getAttribute('data-path')!==file.path)throw Error('确认前目录或选择发生变化');
      const confirm=[...dialog.querySelectorAll('button')].find(b=>cleanText(b)==='删除');if(!confirm||confirm.disabled)throw Error('删除确认按钮不可用');
      // 用户已在脚本面板确认本次具体清单，这里只确认已逐字核对的普通单文件删除。
      finishing++;submitted=true;
      try {confirm.click();await waitFor(()=>!visible(dialog),'删除请求未结束');await refreshAfterOperation(parent(file.path));}finally{finishing--;}
    }catch(e){if(!submitted){cancelDelete();e.notSubmitted=true;}throw e;}
  }
  // 不使用全选；只多选计划内普通文件，且通过工具栏入口保持整个选择集合。
  async function deleteFiles(files,expectedRows) {
    let submitted=false;
    const targets=new Set(files.map(f=>f.path)),folder=files.length?parent(files[0].path):'';
    function unchanged(){
      checkpoint();requireListReady();
      if(!files.length||files.length>50||targets.size!==files.length||files.some(f=>f.directory||!allowed(f.path)||parent(f.path)!==folder))throw Error('批量删除范围无效');
      if(currentPath()!==folder||breadcrumbPath()!==folder||!Array.isArray(expectedRows)||readCount()!==expectedRows.length||document.querySelector('select.page-size-select')?.value!=='all')throw Error('批量删除前目录或分页发生变化');
      const key=r=>JSON.stringify([r.path,r.name,!!r.directory,r.modified,r.size]),current=readRows(),expected=new Map(expectedRows.map(f=>[f.path,key(f)]));
      if(current.length!==expectedRows.length||current.some(f=>expected.get(f.path)!==key(f))||files.some(f=>expected.get(f.path)!==key(f)))throw Error('批量删除前文件清单发生变化');
    }
    function exactSelection(){const rows=selected();if(rows.length!==targets.size||rows.some(e=>!targets.has(e.getAttribute('data-path'))))throw Error('网页多选范围与批量清单不一致');}
    try {
      unchanged();if([...document.querySelectorAll('[role="dialog"]')].some(visible))throw Error('请先关闭其他对话框');
      for(const e of selected())e.querySelector('.file-icon')?.dispatchEvent(new MouseEvent('click',{bubbles:true,ctrlKey:true}));
      await waitFor(()=>!selected().length,'无法清除旧文件选择');
      for(const f of files){checkpoint();const row=rowFor(f.path);if(!row||row.classList.contains('directory'))throw Error('批量目标已变化');row.querySelector('.file-icon')?.dispatchEvent(new MouseEvent('click',{bubbles:true,ctrlKey:true}));}
      // 等一次界面状态提交，确保工具栏获得最终多选集合。
      await sleep(100);unchanged();exactSelection();
      const buttons=[...document.querySelectorAll('button[title="删除"]')].filter(visible);if(buttons.length!==1||buttons[0].disabled)throw Error('未找到唯一可用的批量删除工具栏按钮');buttons[0].click();
      const dialog=await waitFor(()=>[...document.querySelectorAll('[aria-labelledby="delete-confirm-title"]')].find(visible),'批量删除确认窗口未出现');
      const items=[...dialog.querySelectorAll('.delete-file-item')],byName=new Map(files.map(f=>[f.name,f]));
      if(items.length!==files.length||byName.size!==files.length)throw Error('批量确认窗口未完整列出本批文件，不提交删除');
      const names=new Set();for(const item of items){const name=item.querySelector('.delete-file-name')?.textContent.trim(),f=byName.get(name);if(!f||names.has(name)||item.querySelector('.delete-file-size')?.textContent.trim()!==f.size)throw Error('批量确认窗口文件名称或大小不一致');names.add(name);}
      if(dialog.querySelector('input:checked')||/永久|不可恢复|permanent/i.test(dialog.textContent))throw Error('检测到永久删除选项，本脚本不执行');
      unchanged();exactSelection();const confirm=[...dialog.querySelectorAll('button')].find(b=>cleanText(b)==='删除');if(!confirm||confirm.disabled)throw Error('批量删除确认按钮不可用');
      finishing++;submitted=true;
      try{confirm.click();await waitFor(()=>!visible(dialog),'批量删除请求未结束');await refreshAfterOperation(folder);}finally{finishing--;}
    }catch(e){if(!submitted){cancelDelete();e.notSubmitted=true;}throw e;}
  }
  return {deleteFiles,listing,queryDouban,deleteFile,worker:doubanWorker,currentPath,readRows,requireListReady};
})
