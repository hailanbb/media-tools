// 执行真实空目录适配函数，用假页面验证选择范围；不连接云盘。
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('fs'),vm=require('vm'),path=require('path');
const src=fs.readFileSync(path.join(__dirname,'../src/cloud-archive-client.js'),'utf8');
const code=src.slice(src.indexOf('  async function removeEmpty('),src.indexOf('  return {listing,moveShow'));
async function run(options={}) {
  const SOURCE='/source',DEST='/dest',source='/source/分类/剧',entry={path:source,name:'剧',directory:true};
  const asset={path:source+'/poster.jpg',name:'poster.jpg',directory:false,size:'1 MB',modified:'2026-10-23 08:00'},targetAsset={...asset,path:'/dest/分类/剧/poster.jpg'};
  const state={selected:false,dialog:false,submitted:0,reads:[]};let current='/source/分类';
  const cancel={textContent:'取消',click(){state.dialog=false;}},confirm={textContent:'删除',click(){state.submitted++;if(options.lost)throw Error('响应丢失');state.dialog=false;}};
  const row={getAttribute:()=>source,classList:{contains:()=>true},querySelector:s=>s==='.file-name-ellipsis'?{click(){state.selected=true;}}:{dispatchEvent(){state.selected=false;}}};
  const dialog={textContent:options.permanent?'永久删除':'删除',querySelector:()=>null,querySelectorAll:s=>s==='.delete-file-name'?[{textContent:options.wrong?'另一目录':'剧'}]:[cancel,confirm]};
  const menu={querySelectorAll:()=>[{textContent:'删除',click(){state.dialog=true;}}]};
  const context={SOURCE,DEST,C:{cleanupFiles:()=>options.shared?[asset]:[]},refreshAfterOperation:async p=>{state.reads.push(p);},parent:p=>p.slice(0,p.lastIndexOf('/')),checkpoint(){},requireListReady(){},dialogs:()=>[],visible:()=>true,cleanText:e=>e?.textContent?.trim()||'',
    listing:async p=>{state.reads.push(p);current=p;if(p==='/dest/分类/剧')return [{...targetAsset,size:options.targetChanged?'2 MB':'1 MB'}];return p===source?(options.nonempty?[{path:source+'/new.mkv'}]:options.shared?[asset]:[]):[entry];},currentPath:()=>current,breadcrumbPath:()=>current,
    readRows:()=>options.changed?[]:[entry],readCount:()=>1,selected:()=>state.selected?(options.multi?[row,row]:[row]):[],
    document:{querySelector:()=>({value:'all'}),querySelectorAll:s=>s.includes('file-row[data-path]')?[row]:s==='[role="menu"]'?[menu]:[dialog]},
    waitFor:async(fn,message)=>{const r=fn();if(r)return r;throw Error(message);},MouseEvent:class{},finishing:0};
  // 删除后窗口不可见；其他控件始终可见。
  context.visible=e=>e===dialog?state.dialog:true;
  vm.runInNewContext(code+';globalThis.run=removeEmpty',context);
  let error;try{await context.run({mode:'seasons',source,target:'/dest/分类/剧',destinationBefore:[targetAsset],category:'分类',cleanup:{source:options.outside?'/other':source}},[entry],entry);}catch(e){error=e;}
  return {state,error};
}
test('删除源空目录前重新读取空内容及父列表，只提交精确单目录普通删除',async()=>{
  const {state,error}=await run();assert.equal(error,undefined);assert.equal(state.submitted,1);assert.deepEqual(state.reads,['/dest/分类/剧','/source/分类/剧','/source/分类','/source/分类']);
});
test('源只剩已列图片且目标保留资料未变才可删除；目标变化不提交',async()=>{
  const ok=await run({shared:true});assert.equal(ok.error,undefined);assert.equal(ok.state.submitted,1);assert.equal(ok.state.reads.at(-1),'/source/分类');
  const changed=await run({shared:true,targetChanged:true});assert.ok(changed.error);assert.equal(changed.state.submitted,0);
  const extra=await run({shared:true,nonempty:true});assert.ok(extra.error);assert.equal(extra.state.submitted,0);
});
test('非空、范围错误、同名确认不符、多选、永久删除及列表变化均不提交',async()=>{
  for(const key of ['nonempty','outside','wrong','multi','permanent','changed']){const r=await run({[key]:true});assert.ok(r.error,key);assert.equal(r.error.notSubmitted,true,key);assert.equal(r.state.submitted,0,key);}
});
test('空目录删除响应丢失不能误标为未提交',async()=>{
  const r=await run({lost:true});assert.ok(r.error);assert.equal(r.state.submitted,1);assert.equal(r.error.notSubmitted,undefined);
});
