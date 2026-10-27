/* CloudDrive2 归档页面适配器：只开放已配置源/目标树内的读取、整剧/季/根文件移动及已验证源目录的普通删除。
 * 复用只读列表代码；空目录清理有独立的范围、空内容、选择与确认检查。
 * 目标必须是现有分类或同剧目录；遇到冲突或额外确认窗口停止，不选择覆盖、合并或复制。
 */
(function createCloudArchiveClient(C,options) {
  'use strict';
  const {checkpoint:check}=options;
  const SOURCE=options.source||'/115/Library_incs/nas/电视剧',DEST=options.destination||'/115/Library_incs/nas.finish/电视剧';
  const {parent,createListingSettler}=C;
  /* build:cloud-listing */
  const cleanText=e=>(e?.textContent||'').replace(/[\uE000-\uF8FF]/g,'').trim();
  const dialogs=()=>[...document.querySelectorAll('[role="dialog"]')].filter(visible);
  const selected=()=>[...document.querySelectorAll('table.file-list tr.file-row.selected')];
  function cancel(){for(const d of dialogs())if(cleanText(d.querySelector('.modal-title'))==='移动/复制到...')[...d.querySelectorAll('button')].find(b=>cleanText(b)==='取消')?.click();}
  async function moveShow(plan,expectedRows,entry) {
    let submitted=false;
    function unchanged() {
      const key=r=>JSON.stringify([r.path,r.name,!!r.directory,r.modified,r.size]),expected=new Map(expectedRows.map(r=>[r.path,key(r)])),current=readRows();
      if(currentPath()!==parent(plan.source)||breadcrumbPath()!==parent(plan.source)||current.length!==expectedRows.length||current.some(r=>expected.get(r.path)!==key(r))||readCount()!==current.length||document.querySelector('select.page-size-select')?.value!=='all')throw Error('移动确认前源列表或分页发生变化');
      requireListReady();checkpoint();
    }
    try {
      const seasonMove=plan.kind==='season'&&entry?.directory&&parent(plan.source)===plan.showSource&&parent(plan.showSource)===SOURCE+'/'+plan.category&&plan.showTarget===DEST+'/'+plan.category+'/'+C.base(plan.showSource)&&/^Season\s+[0-9]+$/i.test(entry?.name||'')&&plan.target===plan.showTarget+'/'+entry?.name;
      const fileMove=plan.kind==='file'&&!entry?.directory&&parent(plan.source)===plan.showSource&&parent(plan.showSource)===SOURCE+'/'+plan.category&&plan.showTarget===DEST+'/'+plan.category+'/'+C.base(plan.showSource)&&plan.target===plan.showTarget+'/'+entry?.name;
      const showMove=(!plan.kind||plan.kind==='show')&&entry?.directory&&parent(plan.source)===SOURCE+'/'+plan.category&&plan.target===DEST+'/'+plan.category+'/'+entry?.name;
      if(!entry||entry.path!==plan.source||(!seasonMove&&!showMove&&!fileMove)||!allowed(plan.source)||!allowed(plan.target))throw Error('拒绝移动计划外目录');
      if(dialogs().length)throw Error('请先关闭页面已有的对话框');unchanged();
      for(const row of selected())row.querySelector('.file-icon')?.dispatchEvent(new MouseEvent('click',{bubbles:true,ctrlKey:true}));
      await waitFor(()=>!selected().length,'旧文件选择未清除');
      const row=[...document.querySelectorAll('table.file-list tr.file-row[data-path]')].find(r=>r.getAttribute('data-path')===plan.source);
      if(!row||row.classList.contains('directory')!==!!entry.directory)throw Error('源目录已变化');row.querySelector('.file-name-ellipsis')?.click();
      const menu=await waitFor(()=>[...document.querySelectorAll('[role="menu"]')].find(visible),'目录菜单未出现');
      const action=[...menu.querySelectorAll('[role="menuitem"],button')].find(e=>cleanText(e)==='移动/复制到...');if(!action)throw Error('找不到移动入口');action.click();
      const dialog=await waitFor(()=>dialogs().find(d=>cleanText(d.querySelector('.modal-title'))==='移动/复制到...'),'移动窗口未出现');
      dialog.querySelector('.folder-breadcrumb-bar .breadcrumb-edit-btn')?.click();
      const input=await waitFor(()=>dialog.querySelector('input.breadcrumb-path-input'),'移动路径输入框未出现');
      setInput(input,parent(plan.target));input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true}));
      let last='',stableSince=0;
      await waitFor(()=>{
        if(cleanText(dialog.querySelector('.selected-path-display span'))!==parent(plan.target)){stableSince=0;return false;}
        if(dialog.querySelector('.fa-spinner')||/加载中|Loading/i.test(cleanText(dialog.querySelector('.folder-list')))){stableSince=0;return false;}
        const list=dialog.querySelector('.folder-list');if(!list||/失败|错误|error|failed/i.test(cleanText(list)))throw Error('目标分类列表加载失败');
        if([...dialog.querySelectorAll('.folder-list-item:not(.up-folder)')].some(e=>[...e.querySelectorAll('span')].some(s=>cleanText(s).normalize('NFC').toLowerCase()===entry.name.normalize('NFC').toLowerCase()||seasonMove&&Number(/^Season\s+(\d+)$/i.exec(cleanText(s))?.[1])===Number(/^Season\s+(\d+)$/i.exec(entry.name)?.[1]))))throw Error('目标已出现同名目录，取消移动');
        // 路径文字可能先于异步列表更新，必须等目标清单稳定后才允许提交。
        const fingerprint=cleanText(list);if(!stableSince||last!==fingerprint){last=fingerprint;stableSince=Date.now();return false;}
        return Date.now()-stableSince>=1800;
      },'目标分类尚未确认加载，未发出移动');
      unchanged();
      if(selected().length!==1||selected()[0].getAttribute('data-path')!==plan.source||dialogs().length!==1)throw Error('移动选择范围或对话框发生变化');
      if([...dialog.querySelectorAll('.folder-select-warning')].some(visible))throw Error('目标目录有警告，停止移动');
      const button=dialog.querySelector('button[aria-label="移动"]');if(!button||button.disabled)throw Error('移动按钮不可用');
      // 面板已经列出并确认整剧清单。这里只触发一次移动；不点击任何后续冲突处理选项。
      finishing++;submitted=true;
      try {
        button.click();await waitFor(()=>{
          if(dialogs().some(d=>d!==dialog))throw Error('出现额外确认/冲突窗口，请人工核实；脚本不会覆盖或合并');
          return !visible(dialog);
        },'移动请求尚未结束，请核实结果');
        await refreshAfterOperation(parent(plan.source));
      }finally{finishing--;}
    }catch(e){if(!submitted){cancel();e.notSubmitted=true;}throw e;}
  }
  async function removeEmpty(plan,_expectedRows,entry) {
    let submitted=false,dialog;
    try {
      if(plan.mode!=='seasons'||plan.cleanup?.source!==plan.source||entry?.path!==plan.source||!entry.directory||parent(plan.source)!==SOURCE+'/'+plan.category||plan.target!==DEST+'/'+plan.category+'/'+entry.name)throw Error('源目录清理范围不符');
      checkpoint();if(dialogs().length)throw Error('请先关闭已有对话框');
      // 再读取源清单及目标保留资料；只接受空目录或列明的根共用资料，禁止清单外内容。
      const expected=C.cleanupFiles(plan);
      const fingerprint=rows=>JSON.stringify(rows.map(f=>[f.path,f.name,!!f.directory,f.size||'',f.modified||'']).sort((a,b)=>a[0].localeCompare(b[0])));
      const kept=await listing(plan.target);
      for(const f of expected){const before=plan.destinationBefore.find(t=>!t.directory&&t.name.normalize('NFC').toLowerCase()===f.name.normalize('NFC').toLowerCase());if(!before||!kept.some(t=>fingerprint([t])===fingerprint([before])))throw Error('目标共用资料已变化，禁止清理源');}
      if(fingerprint(await listing(plan.source))!==fingerprint(expected))throw Error('源目录出现新内容或清理资料变化，禁止删除');
      const parents=await listing(parent(plan.source)),current=parents.find(f=>f.path===plan.source&&f.directory);
      if(!current||current.name!==entry.name)throw Error('源目录已变化');
      const unchanged=()=>{
        checkpoint();requireListReady();
        if(currentPath()!==parent(plan.source)||breadcrumbPath()!==parent(plan.source)||JSON.stringify(parents)!==JSON.stringify(readRows())||readCount()!==parents.length||document.querySelector('select.page-size-select')?.value!=='all')throw Error('空目录删除前列表发生变化');
      };
      unchanged();
      for(const row of selected())row.querySelector('.file-icon')?.dispatchEvent(new MouseEvent('click',{bubbles:true,ctrlKey:true}));
      await waitFor(()=>!selected().length,'旧选择未清除');
      const row=[...document.querySelectorAll('table.file-list tr.file-row[data-path]')].find(r=>r.getAttribute('data-path')===plan.source);
      if(!row?.classList.contains('directory'))throw Error('源目录不存在');row.querySelector('.file-name-ellipsis')?.click();
      const menu=await waitFor(()=>[...document.querySelectorAll('[role="menu"]')].find(visible),'目录菜单未出现');
      const remove=[...menu.querySelectorAll('button,[role="menuitem"]')].find(b=>cleanText(b)==='删除');if(!remove)throw Error('找不到普通删除入口');remove.click();
      dialog=await waitFor(()=>[...document.querySelectorAll('[aria-labelledby="delete-confirm-title"]')].find(visible),'删除确认窗口未出现');
      const names=[...dialog.querySelectorAll('.delete-file-name')];
      if(names.length!==1||cleanText(names[0])!==entry.name||selected().length!==1||selected()[0].getAttribute('data-path')!==plan.source)throw Error('删除确认范围不是计划中的单个源目录');
      if(dialog.querySelector('input:checked')||/永久|不可恢复|permanent/i.test(cleanText(dialog)))throw Error('拒绝永久删除');
      unchanged();
      const confirm=[...dialog.querySelectorAll('button')].find(b=>cleanText(b)==='删除');if(!confirm||confirm.disabled)throw Error('删除按钮不可用');
      finishing++;submitted=true;
      try{confirm.click();await waitFor(()=>!visible(dialog),'源目录删除结果待核实');await listing(parent(plan.source),{uninterruptible:true});}finally{finishing--;}
    }catch(e){if(!submitted){if(dialog)[...dialog.querySelectorAll('button')].find(b=>cleanText(b)==='取消')?.click();e.notSubmitted=true;}throw e;}
  }
  return {listing,moveShow,removeEmpty,currentPath,readRows,requireListReady};
})
