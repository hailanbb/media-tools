// 模拟原生文件菜单/确认窗口，仅测试真实适配器的 DOM 检查；不连接浏览器或云盘。
const {test}=require('node:test');
const assert=require('node:assert/strict');
const C=require('../season-review.user.js');
const dir='/115/Library_incs/nas/电视剧/日本剧集/测试剧 (2025)/Season 1';
const file={path:dir+'/Test.S01E01.720p-B.mkv',name:'Test.S01E01.720p-B.mkv',modified:'2026-10-21 10:00',size:'1 GB',directory:false};
async function simulate(options={}) {
  const state={selected:false,menu:false,dialog:false,confirmed:0,refreshed:0,cancelled:0};
  const text=value=>({textContent:value});
  const confirm={textContent:'删除',disabled:!!options.disabled,click(){state.confirmed++;if(options.serverThrow)throw Error('server failure');state.dialog=false;}};
  const cancel={textContent:'取消',click(){state.dialog=false;state.cancelled++;}};
  const dialog={getClientRects:()=>state.dialog?[{}]:[],textContent:options.permanent?'永久删除，不可恢复':'删除文件',
    querySelectorAll:s=>s==='button'?[cancel,confirm]:s==='.delete-file-name'?[text(options.wrongName?'wrong.mkv':file.name)]:[],
    querySelector:s=>s==='input:checked'?options.checked?{}:null:s==='.delete-file-size'?text(options.wrongSize?'2 GB':file.size):null};
  const menu={getClientRects:()=>state.menu?[{}]:[],querySelectorAll:()=>[{textContent:'\uf1f8 删除',click(){state.dialog=true;state.menu=false;}}]};
  const row={getAttribute:()=>file.path,classList:{contains:()=>false},querySelector:s=>({
    '.file-name-text':text(file.name),'.file-modified':text(options.changed||(options.changedAfterDialog&&state.dialog)?'2026-10-21 11:00':file.modified),'.file-size':text(file.size),
    '.file-icon':{dispatchEvent(){state.selected=!state.selected;}},'.file-name-ellipsis':{click(){state.selected=true;state.menu=true;}}
  }[s])};
  const document={querySelector:s=>s==='button.toolbar-refresh-btn'?{click(){state.refreshed++;}}:s==='.health-indicator.connected'?(options.disconnected?null:{}):s==='select.page-size-select'?{value:options.changedPage?'50':'all'}:s==='.file-list-status'?text('1 items'):null,
    querySelectorAll:s=>{
      if(s==='.files-load-error')return options.loadError?[{getClientRects:()=>[{}]}]:[];
      if(s==='.file-list-scroll-area .loading-bar')return options.loading?[{getClientRects:()=>[{}]}]:[];
      if(s==='table.file-list tr.file-row[data-path]')return [row];
      if(s==='table.file-list tr.file-row.selected')return state.selected?(options.wrongSelection?[row,row]:[row]):[];
      if(s==='.files-breadcrumb-bar .breadcrumb-item:not(.breadcrumb-root)')return dir.split('/').slice(1).map(text);
      if(s==='[role="menu"]')return [menu];
      if(s==='[role="dialog"]'||s==='[aria-labelledby="delete-confirm-title"]')return [dialog];
      return [];
    }};
  const previous={document:global.document,location:global.location,MouseEvent:global.MouseEvent};
  global.document=document;global.location={href:'https://clouddrive.example/?page=files&path='+encodeURIComponent(options.wrongPath?dir+'/other':dir)};
  global.MouseEvent=class {};
  try {
    const client=C.createClient(C,{get(){},put(){},del(){},message(){},ready:()=>true,checkpoint(){if(options.pause&&state.dialog)throw Object.assign(Error('pause'),{name:'Pause'});}});
    let error;try{await client.deleteFile(file,options.missingSnapshot?undefined:[file]);}catch(e){error=e;}
    return {state,error};
  }finally{Object.assign(global,previous);}
}
test('匹配单文件、名称/大小及普通删除窗口后，适配器只点一次确认',async()=>{
  const {state,error}=await simulate();assert.equal(error,undefined);assert.equal(state.confirmed,1);assert.equal(state.refreshed,1);assert.equal(state.dialog,false);
});
test('列表加载失败或未结束、目录不符、文件变化、选择范围不符时不确认删除',async()=>{
  for(const name of ['loadError','loading','wrongPath','changed','wrongSelection','changedAfterDialog','changedPage','missingSnapshot']) {
    const {state,error}=await simulate({[name]:true});assert.ok(error,name);assert.equal(error.notSubmitted,true);assert.equal(state.confirmed,0);
  }
});
test('确认窗口名称/大小错误、永久删除、已勾选选项或按钮禁用时取消退出',async()=>{
  for(const name of ['wrongName','wrongSize','permanent','checked','disabled']) {
    const {state,error}=await simulate({[name]:true});assert.ok(error,name);assert.equal(error.notSubmitted,true);assert.equal(state.confirmed,0);assert.equal(state.dialog,false);
  }
});
test('点击确认前暂停可证明未提交；确认后异常则不能声称未提交',async()=>{
  const before=await simulate({pause:true});assert.equal(before.error.notSubmitted,true);assert.equal(before.state.confirmed,0);
  const after=await simulate({serverThrow:true});assert.ok(after.error);assert.equal(after.error.notSubmitted,undefined);assert.equal(after.state.confirmed,1);
});

test('指示器缺少 connected 时，匹配文件及普通删除确认仍可执行；真实列表错误照常拦截',async()=>{
  const ok=await simulate({disconnected:true});assert.equal(ok.error,undefined);assert.equal(ok.state.confirmed,1);
  const bad=await simulate({disconnected:true,loadError:true});assert.equal(bad.error.name,'Pause');assert.equal(bad.state.confirmed,0);assert.equal(bad.error.notSubmitted,true);
});
