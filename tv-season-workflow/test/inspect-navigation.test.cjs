// 初检导航回归：模拟页面控件的延迟、重绘和失败，不连接真实云盘。
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../src/season-inspect.js'),'utf8');
const code=source.slice(source.indexOf('  async function navigate(path)'),source.indexOf('  async function listing(path'));
async function simulate(options={}) {
  const state={clicks:0,entries:0,lookups:0,path:options.already?'/allowed/show':'/allowed',crumb:options.already?'/allowed/show':'/allowed',ready:!!options.opened,paused:false};
  const input={shown:true,value:'',dispatchEvent(){state.entries++;state.path=state.crumb=this.value;}};
  const hidden={shown:false,click(){throw Error('不能点击隐藏按钮');}};
  const button={shown:true,click(){state.clicks++;if(options.pause){state.paused=true;return;}if(!options.never&&state.clicks>=(options.second?2:1))state.ready=true;}};
  class Paused extends Error {constructor(m){super(m);this.name='Paused';}}
  const checkpoint=()=>{if(state.paused)throw new Paused('用户暂停');};
  const context={document:{querySelectorAll(s){if(s==='[role="dialog"]')return options.dialog?[{shown:true}]:[];if(s.includes('input.breadcrumb-path-input'))return state.ready?[input]:[];if(s.includes('.breadcrumb-edit-btn')){state.lookups++;return [hidden,button];}return [];},querySelector(){return null;}},
    visible:e=>e?.shown,checkpoint,Paused,allowed:p=>p.startsWith('/allowed/'),currentPath:()=>state.path,breadcrumbPath:()=>state.crumb,setInput:(e,v)=>{e.value=v;},KeyboardEvent:class{},
    waitFor:async(fn,message)=>{for(let n=0;n<3;n++){checkpoint();const value=fn();if(value)return value;}throw Error(message);}};
  vm.runInNewContext(code+';globalThis.navigate=navigate;',context);let error;
  try{await context.navigate(options.outside?'/outside':'/allowed/show');}catch(e){error=e;}
  return {state,error};
}
test('第一次点击没有生效时重新取得可见按钮重试，只提交一次路径',async()=>{
  const {state,error}=await simulate({second:true});assert.equal(error,undefined);assert.equal(state.clicks,2);assert.equal(state.lookups,2);assert.equal(state.entries,1);assert.equal(state.path,'/allowed/show');
});
test('路径框已经打开时直接复用；已在目标目录时不点击编辑按钮',async()=>{
  const opened=await simulate({opened:true});assert.equal(opened.error,undefined);assert.equal(opened.state.clicks,0);assert.equal(opened.state.entries,1);
  const same=await simulate({already:true});assert.equal(same.error,undefined);assert.equal(same.state.clicks,0);assert.equal(same.state.entries,0);
});
test('三次未出现输入框后暂停，不无限重试且说明目标和恢复操作',async()=>{
  const {state,error}=await simulate({never:true});assert.equal(state.clicks,3);assert.equal(state.entries,0);assert.equal(error.name,'Paused');assert.match(error.message,/已尝试 3 次/);assert.match(error.message,/继续本轮/);assert.match(error.message,/\/allowed\/show/);
});
test('弹窗阻挡及用户暂停不继续点击，越界路径不导航',async()=>{
  const dialog=await simulate({dialog:true});assert.equal(dialog.state.clicks,0);assert.equal(dialog.error.name,'Paused');assert.match(dialog.error.message,/对话框/);
  const paused=await simulate({pause:true});assert.equal(paused.state.clicks,1);assert.equal(paused.error.message,'用户暂停');
  const outside=await simulate({outside:true});assert.equal(outside.state.clicks,0);assert.match(outside.error.message,/拒绝访问/);
});
test('导航暂停向分类扫描上传递，当前剧索引不推进，不记录为零或读取失败',async()=>{
  const run=source.slice(source.indexOf('  async function runJob()'),source.indexOf('  async function supplement('));
  const job={index:0,queue:[{name:'公寓',path:'/allowed/show'}],results:[]};
  class Paused extends Error{}
  const context={job,tmdbDisabledReason:'',tmdbSeriesCache:new Map(),save(){},checkpoint(){},message(){},Paused,scanShow:async()=>{throw new Paused('路径框未出现');},recordResult(){throw Error('不应记录失败行');}};
  vm.runInNewContext(run+';globalThis.run=runJob;',context);await assert.rejects(context.run(),/路径框未出现/);assert.equal(job.index,0);assert.equal(job.results.length,0);
});
