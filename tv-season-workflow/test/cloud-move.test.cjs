// 使用真实归档适配器和模拟 DOM，验证菜单/目标窗口的边界；没有网络和实际文件移动。
const {test}=require('node:test'),assert=require('node:assert/strict'),A=require('../season-archive.user.js');
async function simulate(options={}) {
  const category='日本剧集',showName='测试剧 (2025)',name=options.file?'tvshow.nfo':options.season?'Season 2':showName,showSource=A.SOURCE+'/'+category+'/'+showName,showTarget=A.DEST+'/'+category+'/'+showName,
    source=(options.season||options.file)?showSource+'/'+name:showSource,target=(options.season||options.file)?showTarget+'/'+name:showTarget;
  const entry={name,path:source,directory:!options.file,size:'',modified:'2026-10-21 10:00'};
  const state={selected:false,menu:false,dialog:false,input:false,confirmed:0,refreshed:0,cancelled:0,destination:''};
  const text=textContent=>({textContent});
  class Input {constructor(){this.tagName='INPUT';}set value(v){this._value=v;}get value(){return this._value;}dispatchEvent(e){if(e.type==='keydown')state.destination=this._value;}}
  const input=new Input();
  const cancel={textContent:'取消',click(){state.dialog=false;state.cancelled++;}};
  const confirm={disabled:options.disabled,click(){state.confirmed++;if(options.throwAfter)throw Error('server error');state.dialog=false;}};
  const dialog={getClientRects:()=>state.dialog?[{}]:[],querySelector:s=>({
    '.modal-title':text('移动/复制到...'),'.folder-breadcrumb-bar .breadcrumb-edit-btn':{click(){state.input=true;}},
    'input.breadcrumb-path-input':state.input?input:null,'.selected-path-display span':text(state.destination),
    '.folder-list':text('其他剧集'),'button[aria-label="移动"]':confirm
  }[s]||null),querySelectorAll:s=>s==='button'?[cancel,confirm]:s==='.folder-select-warning'&&options.warning?[{getClientRects:()=>[{}]}]:s==='.folder-list-item:not(.up-folder)'&&(options.collision||options.alias)?[{querySelectorAll:()=>[text(options.alias?'Season 02':name)]}]:[]};
  const menu={getClientRects:()=>state.menu?[{}]:[],querySelectorAll:()=>[{textContent:'移动/复制到...',click(){state.dialog=true;state.menu=false;}}]};
  const row={getAttribute:()=>source,classList:{contains:()=>!options.file},querySelector:s=>({'.file-name-text':text(name),'.file-modified':text(options.changed&&state.dialog?'2026-10-22 10:00':entry.modified),'.file-size':text(''),
    '.file-icon':{dispatchEvent(){state.selected=!state.selected;}},'.file-name-ellipsis':{click(){state.selected=true;state.menu=true;}}}[s])};
  const document={querySelector:s=>s==='button.toolbar-refresh-btn'?{click(){state.refreshed++;}}:s==='.health-indicator.connected'?(options.disconnected?null:{}):s==='select.page-size-select'?{value:options.pagination?'50':'all'}:s==='.file-list-status'?text('1 items'):null,
    querySelectorAll:s=>{
      if(s==='.files-load-error')return options.loadError?[{getClientRects:()=>[{}]}]:[];
      if(s==='.file-list-scroll-area .loading-bar')return options.loading?[{getClientRects:()=>[{}]}]:[];
      if(s==='table.file-list tr.file-row[data-path]')return [row];
      if(s==='table.file-list tr.file-row.selected')return state.selected?(options.selection?[row,row]:[row]):[];
      if(s==='.files-breadcrumb-bar .breadcrumb-item:not(.breadcrumb-root)')return A.parent(source).split('/').slice(1).map(text);
      if(s==='[role="menu"]')return [menu];if(s==='[role="dialog"]')return [dialog];return [];
    }};
  const previous=Object.fromEntries(['document','location','MouseEvent','KeyboardEvent','Event','HTMLInputElement'].map(k=>[k,global[k]]));
  Object.assign(global,{document,location:{href:'https://clouddrive.example/?page=files&path='+encodeURIComponent(options.path?A.parent(target):A.parent(source))},HTMLInputElement:Input,MouseEvent:class{},KeyboardEvent:class{constructor(type){this.type=type;}},Event:class{constructor(type){this.type=type;}}});
  try {
    const client=A.createClient(A,{checkpoint(){if(options.pause&&state.dialog)throw Error('paused');}});let error;
    try{await client.moveShow({source,target:options.outside?target+'/other':target,name,category,...(options.file?{kind:'file',showSource,showTarget}:options.season?{kind:'season',showSource,showTarget}:{})},[entry],entry);}catch(e){error=e;}
    return {state,error};
  }finally{Object.assign(global,previous);}
}
test('精确选择单个剧集和目标分类后，仅提交一次移动',async()=>{
  const {state,error}=await simulate();assert.equal(error,undefined);assert.equal(state.confirmed,1);assert.equal(state.refreshed,1);assert.equal(state.destination,A.DEST+'/日本剧集');
});
test('精确移动单季到已存在的同剧目录；同季别名或越界目标不提交',async()=>{
  const ok=await simulate({season:true});assert.equal(ok.error,undefined);assert.equal(ok.state.confirmed,1);assert.ok(ok.state.destination.endsWith('/测试剧 (2025)'));
  for(const extra of [{alias:true},{outside:true}]){const r=await simulate({season:true,...extra});assert.ok(r.error);assert.equal(r.state.confirmed,0);assert.equal(r.error.notSubmitted,true);}
});
test('错路径、列表失败或加载中、分页变化、选择变化、清单变化和越界目标不提交',async()=>{
  for(const key of ['path','loadError','loading','pagination','selection','changed','outside']) {
    const {state,error}=await simulate({[key]:true});assert.ok(error,key);assert.equal(state.confirmed,0,key);assert.equal(error.notSubmitted,true,key);
  }
});
test('目标出现同名目录、警告、禁用按钮或提交前暂停均取消移动窗口',async()=>{
  for(const key of ['collision','warning','disabled','pause']) {
    const {state,error}=await simulate({[key]:true});assert.ok(error,key);assert.equal(state.confirmed,0,key);assert.equal(error.notSubmitted,true,key);assert.equal(state.dialog,false,key);
  }
});
test('发出移动后响应异常必须保留不确定状态，不能标作未提交',async()=>{
  const {state,error}=await simulate({throwAfter:true});assert.ok(error);assert.equal(state.confirmed,1);assert.equal(error.notSubmitted,undefined);
});

test('指示器缺少 connected 时，匹配目录及移动确认仍可执行；真实列表错误照常拦截',async()=>{
  const ok=await simulate({disconnected:true});assert.equal(ok.error,undefined);assert.equal(ok.state.confirmed,1);
  const bad=await simulate({disconnected:true,loadError:true});assert.equal(bad.error.name,'Pause');assert.equal(bad.state.confirmed,0);assert.equal(bad.error.notSubmitted,true);
});

test('根目录普通文件可移入同名剧，文件同名冲突不提交',async()=>{const ok=await simulate({file:true});assert.equal(ok.error,undefined);assert.equal(ok.state.confirmed,1);const bad=await simulate({file:true,collision:true});assert.ok(bad.error);assert.equal(bad.state.confirmed,0);});
