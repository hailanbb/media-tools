// A DOM/GM adapter exercises the actual userscript event handlers without a network or cloud account.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../season-inspect.user.js'),'utf8');
async function launch(initial={},responses=[],adapters={}) {
  const values=new Map(Object.entries(initial)),elements=[],opened=[],requests=[],accesses=[];
  class Element {
    constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.style={};this.listeners={};this.checked=false;this.value='';this.textContent='';this.classes=new Set();
      this.classList={add:c=>this.classes.add(c),contains:c=>this.classes.has(c),toggle:c=>this.classes.has(c)?this.classes.delete(c):this.classes.add(c)};elements.push(this);}
    set className(v){this._className=v;this.classes=new Set(v.split(' '));}
    get className(){return this._className||'';}
    append(...children){this.children.push(...children);for(const c of children)if(c instanceof Element)c.parentElement=this;}
    replaceChildren(...children){this.children=[];this.append(...children);}
    attachShadow(){this.shadow=new Element('shadow');this.shadow.parentElement=this;return this.shadow;}
    addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);}
    async fire(name,props={}){for(const fn of this.listeners[name]||[])await fn({target:this,button:0,preventDefault(){},...props});}
    closest(tag){for(let e=this;e;e=e.parentElement)if(e.tagName===tag.toUpperCase())return e;return null;}
    setPointerCapture(){}
    getBoundingClientRect(){const box=elements.find(e=>e.className==='box');return {left:parseFloat(this.style.left)||500,top:parseFloat(this.style.top)||100,width:box?.classes.has('min')?240:440,height:box?.classes.has('min')?80:600};}
    click(){return this.fire('click');}
  }
  const body=new Element('body');
  const context={console,URL,URLSearchParams,TextDecoder,Blob,Map,Set,Date,JSON,Math,Number,Promise,crypto:require('node:crypto').webcrypto,
    location:adapters.location||{hostname:'clouddrive.example',origin:'https://clouddrive.example',href:'https://clouddrive.example/?page=files'},
    document:{body,createElement:t=>new Element(t),createTextNode:t=>({textContent:t})},
    window:{addEventListener(){}},innerWidth:1200,innerHeight:900,requestAnimationFrame:fn=>fn(),ResizeObserver:class{constructor(fn){this.fn=fn;}observe(){}},
    navigator:{locks:{request:async(_name,options,fn)=>(fn||options)({})}},structuredClone,confirm:adapters.confirm||(()=>true),
    GM_getValue:(k,d)=>{accesses.push(['read',k]);return values.has(k)?values.get(k):d;},GM_setValue:(k,v)=>{accesses.push(['write',k]);values.set(k,structuredClone(v));},GM_deleteValue:k=>{accesses.push(['delete',k]);values.delete(k);},
    GM_xmlhttpRequest:options=>{requests.push(options);queueMicrotask(()=>options.onload(responses.shift()||{status:200,response:{success:true}}));return {abort(){}};},
    GM_openInTab:(url,options)=>{opened.push({url,options});return {close(){}};},setTimeout,clearTimeout};
  context.testReview=adapters.review;context.testScan=adapters.scan;
  context.testWorkflowReport=adapters.workflow;context.testStageReport=adapters.stageReport;
  context.testCloudClient=adapters.cloudClient;
  context.testRepair=adapters.repair;context.unsafeWindow={MoviePilotAPI:{}};
  const testSource=(adapters.source||source).replace('function excelReviewEngine() {','function excelReviewEngine() { if(testReview)return testReview;')
    .replace('const Repair=','const Repair=testRepair||')
    .replace('function stageReportEngine(){','function stageReportEngine(){ if(testStageReport)return testStageReport;')
    .replace('function reportEngine(){', 'function reportEngine(){ if(testWorkflowReport)return testWorkflowReport;')
    .replace('const client=createClient({...C,cleanupFiles:K.cleanupFiles},', 'const client=testCloudClient||createClient({...C,cleanupFiles:K.cleanupFiles},')
    .replace('const client=createClient(C,', 'const client=testCloudClient||createClient(C,')
    .replace("async function scanShow(showRow, destinationNames, onlySeason=null, sourcePolicy='default') {","async function scanShow(showRow, destinationNames, onlySeason=null, sourcePolicy='default') { if(testScan)return testScan(showRow,onlySeason,sourcePolicy);");
  vm.runInNewContext(testSource,context);
  await new Promise(resolve=>setImmediate(resolve));
  return {values,elements,opened,requests,accesses,button:name=>elements.find(e=>e.tagName==='BUTTON'&&e.textContent===name)};
}
test('复核列表失败只暂停当前季，不将错误写入全部剧集；恢复后继续当前季',async()=>{
  const review=fs.readFileSync(require('node:path').join(__dirname,'../season-review.user.js'),'utf8');
  const tasks=['甲','乙'].map(n=>({name:'测试剧'+n+' (2025)',season:1,path:'/115/Library_incs/nas/电视剧/日本剧集/测试剧'+n+' (2025)/Season 1',key:n,data:{}}));
  const updates=new Map(),reads=[],queries=[];let failed=true;
  const page=await launch({},[],{source:review,workflow:{read:()=>({tasks,errors:[],skipped:0,updates})},cloudClient:{worker(){},listing:async p=>{reads.push(p);if(failed)throw Object.assign(Error('文件列表加载失败，已暂停'),{name:'Pause'});return [];},queryDouban:async t=>{queries.push(t.key);return {title:t.name,total:1,releaseYears:['2025'],url:'https://movie.douban.com/subject/1/',broadcast:'已播完'};},deleteFile(){assert.fail('只读复核不得删除');}}});
  const input=page.elements.find(e=>e.type==='file');input.files=[{name:'报告.xlsx',size:10,arrayBuffer:async()=>new ArrayBuffer(10)}];await input.fire('change');
  await page.button('开始复核').click();assert.equal(updates.size,1);assert.equal(updates.get(tasks[0]).state,'已暂停');assert.equal(reads.length,1);assert.equal(queries.length,0);assert.equal(page.button('继续复核').disabled,false);
  failed=false;await page.button('继续复核').click();assert.equal(reads[1],tasks[0].path);assert.deepEqual(queries,['甲','乙']);assert.equal(updates.size,2);assert.notEqual(updates.get(tasks[0]).state,'已暂停');
});
test('归档列表失败暂停当前剧，继续时仍检查同一剧，不污染后续行',async()=>{
  const archive=fs.readFileSync(require('node:path').join(__dirname,'../season-archive.user.js'),'utf8'),A=require('../season-archive.user.js');
  const tasks=['甲','乙'].map(n=>({category:'日本剧集',name:'测试剧'+n,showPath:A.SOURCE+'/日本剧集/测试剧'+n,path:A.SOURCE+'/日本剧集/测试剧'+n+'/Season 1',season:1,color:'green',data:{}}));
  const updates=new Map();let reads=0;
  const page=await launch({},[],{source:archive,workflow:{read:()=>({tasks,errors:[],updates})},cloudClient:{listing:async()=>{reads++;throw Object.assign(Error('列表加载失败'),{name:'Pause'});},moveShow(){assert.fail('检查不得移动');}}});
  const input=page.elements.find(e=>e.type==='file');input.files=[{name:'报告.xlsx',size:10,arrayBuffer:async()=>new ArrayBuffer(10)}];await input.fire('change');
  await page.button('开始检查').click();assert.equal(reads,1);assert.equal(updates.size,1);assert.equal(updates.get(tasks[0]).state,'已暂停');assert.equal(page.button('继续检查').disabled,false);
  await page.button('继续检查').click();assert.equal(reads,2);assert.equal(updates.size,1);assert.equal(updates.has(tasks[1]),false);
});
test('第四阶段安装和导入保持空闲，开始只检查，明确确认后移动并验证成功',async()=>{
  const A=require('../season-archive.user.js'),name='测试剧 (2025)',category='日本剧集',source=A.SOURCE+'/'+category+'/'+name,target=A.DEST+'/'+category+'/'+name;
  const task={name,category,showPath:source,path:source+'/Season 1',season:1,color:'green',data:{'来源登记集数':1}};
  const file=(p,n,directory=false)=>({path:p+'/'+n,name:n,directory,size:directory?'':'1 GB',modified:'2026-10-21 10:00'});
  const tree=new Map([[A.DEST,[file(A.DEST,category,true)]],[A.DEST+'/'+category,[]],[A.SOURCE+'/'+category,[file(A.SOURCE+'/'+category,name,true)]],[source,[file(source,'Season 1',true)]],[task.path,[file(task.path,'测试剧.S01E01.mkv')]]]);
  const session={tasks:[task],errors:[],updates:new Map()};let lists=0,moves=0,confirmed=false;
  const page=await launch({},[],{source:fs.readFileSync(require('node:path').join(__dirname,'../season-archive.user.js'),'utf8'),workflow:{read:()=>session},confirm:()=>confirmed,
    cloudClient:{listing:async p=>{lists++;if(!tree.has(p))throw Error('not found');return structuredClone(tree.get(p));},moveShow:async()=>{moves++;tree.set(A.SOURCE+'/'+category,[]);tree.set(A.DEST+'/'+category,[file(A.DEST+'/'+category,name,true)]);tree.set(target,[file(target,'Season 1',true)]);tree.set(target+'/Season 1',[file(target+'/Season 1','测试剧.S01E01.mkv')]);tree.delete(source);tree.delete(task.path);}}});
  assert.equal(lists,0);assert.equal(moves,0);
  const input=page.elements.find(e=>e.type==='file');input.files=[{name:'归档测试.xlsx',size:10,arrayBuffer:async()=>new ArrayBuffer(10)}];await input.fire('change');
  assert.equal(lists,0);assert.equal(moves,0);await page.button('开始检查').click();assert.ok(lists>0);assert.equal(moves,0);assert.equal(session.updates.get(task).color,null);
  await page.button('执行已列归档清单').click();assert.equal(moves,0);confirmed=true;await page.button('执行已列归档清单').click();assert.equal(moves,1);assert.equal(session.updates.get(task).color,'archived');
  assert.equal(page.opened.length,0);assert.equal(page.requests.length,0);
});

test('归档面板遇到同季冲突整剧暂缓，所有季保持原色且无移动',async()=>{
  const A=require('../season-archive.user.js'),name='天龙八部',category='日本剧集',source=A.SOURCE+'/'+category+'/'+name,target=A.DEST+'/'+category+'/'+name;
  const file=(p,n,directory=false)=>({path:p+'/'+n,name:n,directory,size:directory?'':'1 GB',modified:'2026-10-21 10:00'});
  const tasks=[1,2].map(season=>({name,category,showPath:source,path:source+'/Season '+season,season,color:'green',data:{'来源登记集数':1}}));
  const tree=new Map([[A.DEST,[file(A.DEST,category,true)]],[A.DEST+'/'+category,[file(A.DEST+'/'+category,name,true)]],[A.SOURCE+'/'+category,[file(A.SOURCE+'/'+category,name,true)]],
    [source,[file(source,'Season 1',true),file(source,'Season 2',true)]],[target,[file(target,'Season 1',true)]],[target+'/Season 1',[file(target+'/Season 1','旧版.S01E01.mkv')]],
    ...tasks.map(t=>[t.path,[file(t.path,`视频.S0${t.season}E01.mkv`)]])]);
  const session={tasks,errors:[],updates:new Map()};let moves=0;
  const page=await launch({},[],{source:fs.readFileSync(require('node:path').join(__dirname,'../season-archive.user.js'),'utf8'),workflow:{read:()=>session},cloudClient:{listing:async p=>structuredClone(tree.get(p)),moveShow:async op=>{
    moves++;assert.equal(op.source,tasks[1].path);tree.set(source,tree.get(source).filter(f=>f.path!==op.source));tree.get(target).push(file(target,'Season 2',true));tree.set(op.target,tree.get(op.source).map(f=>({...f,path:op.target+'/'+f.name})));tree.delete(op.source);
  }}});
  const input=page.elements.find(e=>e.type==='file');input.files=[{name:'归档.xlsx',size:10,arrayBuffer:async()=>new ArrayBuffer(10)}];await input.fire('change');await page.button('开始检查').click();
  assert.equal(session.updates.get(tasks[0]).state,'整剧暂缓归档');await page.button('执行已列归档清单').click();
  assert.equal(moves,0);assert.equal(session.updates.get(tasks[0]).color,null);assert.equal(session.updates.get(tasks[1]).color,null);
  const record=session.updates.get(tasks[1]).record;assert.equal(record.operations.length,0);assert.ok(record.preserved.some(f=>f.path===tasks[0].path));
});
test('初检持久进度必须手动恢复，恢复不查询云盘或豆瓣，也不自动续跑',async()=>{
  const C=require('../season-inspect.user.js'),name='测试剧 (2025)',showPath=C.SOURCE+'/日本剧集/'+name;
  const saved={schema:2,job:{id:'saved',category:'日本剧集',startedAt:'2026-10-21T00:00:00Z',mode:'read-only',status:'已完成',index:1,
    queue:[{name,path:showPath,directory:true}],results:[{showName:name,showPath,path:showPath+'/Season 1',show:C.showInfo(name),season:1,pendingDouban:true,status:'豆瓣待补查',total:2}]}};
  const page=await launch({'tv-inspect-v1:progress-available':true,'tv-inspect-v1:progress-v2':saved},[],{source:fs.readFileSync(require('node:path').join(__dirname,'../season-inspect.user.js'),'utf8')});
  assert.equal(page.button('仅补查豆瓣待处理项').disabled,true);await page.button('恢复上次进度').click();
  assert.equal(page.button('仅补查豆瓣待处理项').disabled,false);assert.equal(page.values.get('tv-inspect-v1:progress-v2').job.status,'已暂停');
  assert.equal(page.requests.length,0);assert.equal(page.opened.length,0);
});
test('初检已有标签页可人工确认恢复，但冷却未结束不能解除且不会发请求',async()=>{
  const gate={pages:[],batch:0,restUntil:0,hold:true,holdUntil:Date.now()+1800000,reason:'验证码'};
  const page=await launch({'tv-inspect-v1:douban-budget-v1':gate},[],{source:fs.readFileSync(require('node:path').join(__dirname,'../season-inspect.user.js'),'utf8')});
  const ready=page.elements.find(e=>e.type==='checkbox');assert.equal(ready.disabled,false);ready.checked=true;await ready.fire('change');
  await page.button('人工确认豆瓣已恢复').click();assert.equal(page.values.get('tv-inspect-v1:douban-budget-v1').hold,true);
  page.values.set('tv-inspect-v1:douban-budget-v1',{...gate,holdUntil:Date.now()-1});await page.button('人工确认豆瓣已恢复').click();
  assert.equal(page.values.get('tv-inspect-v1:douban-budget-v1').hold,false);assert.equal(page.requests.length,0);assert.equal(page.opened.length,0);
});
test('独立初检脚本安装默认停止，没有导入、复核、删除、移动入口，不读旧版设置',async()=>{
  const inspect=fs.readFileSync(require('node:path').join(__dirname,'../season-inspect.user.js'),'utf8');
  const page=await launch({'tv-audit-v1:job':{status:'运行中'}},[],{source:inspect});
  assert.equal(page.opened.length,0);assert.equal(page.requests.length,0);
  assert.equal(page.button('开始检查').disabled,false);assert.equal(page.button('继续本轮').disabled,true);
  assert.equal(page.button('导入 Excel，复核黄色项'),undefined);assert.equal(page.elements.some(e=>e.type==='file'),false);
  assert.ok(page.button('导出 Excel（绿 / 蓝标记）'));
  assert.ok(page.accesses.every(([,key])=>key.startsWith('tv-inspect-v1:')));
});

test('下载面板默认停止；导入 Excel 不查询接口、不提交下载，只启用开始/继续',async()=>{
  const download=fs.readFileSync(require('node:path').join(__dirname,'../season-download.user.js'),'utf8');
  const page=await launch({},[],{source:download,location:{hostname:'moviepilot.example',origin:'https://moviepilot.example'},workflow:{read:()=>({tasks:[{name:'测试剧',season:1}],errors:[],skipped:3,updates:new Map()})}});
  assert.equal(page.button('开始补缺下载').disabled,true);assert.equal(page.requests.length,0);assert.equal(page.opened.length,0);
  const file=page.elements.find(e=>e.type==='file');file.files=[{name:'检查.xlsx',size:10,arrayBuffer:async()=>new ArrayBuffer(10)}];await file.fire('change');
  assert.equal(page.button('开始补缺下载').disabled,false);assert.equal(page.button('继续本轮').disabled,false);
  assert.equal(page.requests.length,0);assert.equal(page.opened.length,0);assert.ok(page.elements.some(e=>e.textContent.includes('尚未执行')));
});

test('整理修复面板导入不执行，检查不删除，取消确认不提交；确认后保存结果并禁用重复执行',async()=>{
  const row={key:'repair-ui',name:'测试剧 (2025) [tmdbid=123]',season:1,category:'韩国剧集',data:{来源登记集数:2,缺失集号:'2'}},session={tasks:[row],errors:[],skipped:0,updates:new Map()};let checks=0,writes=0,confirm=false;
  const repair={adapter:()=>({}),check:async()=>{checks++;return {schema:1,taskKey:row.key,state:'ready',at:new Date().toISOString(),episodes:[2],items:[{id:9,episode:2,source:'/download/剧.mkv',target:'/media/剧.mkv',state:'ready',steps:[]}]};},execute:async(_t,p,_a,_c,{save})=>{writes++;save({...p,state:'submitted'});}};
  const page=await launch({},[],{source:fs.readFileSync(require('node:path').join(__dirname,'../season-download.user.js'),'utf8'),location:{hostname:'moviepilot.example',origin:'https://moviepilot.example'},workflow:{read:()=>session},repair,confirm:()=>confirm});
  const input=page.elements.find(e=>e.type==='file');input.files=[{name:'检查.xlsx',size:10,arrayBuffer:async()=>new ArrayBuffer(10)}];await input.fire('change');assert.equal(checks,0);assert.equal(writes,0);assert.equal(page.button('修复整理').disabled,true);
  await page.button('检查整理问题').click();assert.equal(checks,1);assert.equal(writes,0);assert.equal(page.button('修复整理').disabled,false);
  await page.button('修复整理').click();assert.equal(writes,0);confirm=true;await page.button('修复整理').click();assert.equal(writes,1);assert.equal(session.updates.get(row).repair.state,'submitted');assert.equal(page.button('修复整理').disabled,true);
});

test('第三脚本实际见到修复集号后解除阻挡；结果不明即使齐全仍不标绿或清理',async()=>{
  for(const uncertain of [false,true]){
    const name='测试剧 (2025)',path='/115/Library_incs/nas/电视剧/韩国剧集/'+name+'/Season 1',repair={schema:1,state:uncertain?'uncertain':'submitted',at:new Date().toISOString(),episodes:[1],items:[]};
    const task={name,season:1,path,key:'repair',data:{整理修复记录:JSON.stringify(repair)}},session={tasks:[task],errors:[],updates:new Map()};
    const files=[{name:'测试剧.S01E01.1080p.WEB-DL-Group.mkv',path:path+'/测试剧.S01E01.1080p.WEB-DL-Group.mkv',directory:false,size:'1 GB',modified:'2026-10-22 08:00'}];
    const page=await launch({},[],{source:fs.readFileSync(require('node:path').join(__dirname,'../season-review.user.js'),'utf8'),workflow:{read:()=>session},cloudClient:{worker(){},listing:async()=>structuredClone(files),queryDouban:async()=>({title:name,total:1,releaseYears:['2025'],url:'https://movie.douban.com/subject/1/',broadcast:'已播完'}),deleteFiles(){assert.fail('不应删除');}}});
    const input=page.elements.find(e=>e.type==='file');input.files=[{name:'复核.xlsx',size:10,arrayBuffer:async()=>new ArrayBuffer(10)}];await input.fire('change');await page.button('开始复核').click();
    assert.equal(session.updates.get(task).repair.state,uncertain?'uncertain':'files-verified');assert.equal(session.updates.get(task).color,uncertain?null:'green');
  }
});

test('清理面板默认停止；导入只生成黄色队列，不查询豆瓣或删除；无清单不能执行清理',async()=>{
  const review=fs.readFileSync(require('node:path').join(__dirname,'../season-review.user.js'),'utf8');
  let mode;
  const page=await launch({},[],{source:review,workflow:{read:(_bytes,options)=>{mode=options.mode;return {tasks:[{name:'测试剧',season:1}],errors:[],skipped:2,updates:new Map()};}}});
  assert.equal(page.button('开始复核').disabled,true);assert.equal(page.button('执行已列清理清单').disabled,true);
  const file=page.elements.find(e=>e.type==='file');file.files=[{name:'下载报告.xlsx',size:10,arrayBuffer:async()=>new ArrayBuffer(10)}];await file.fire('change');
  assert.equal(mode,'review');assert.equal(page.button('开始复核').disabled,false);assert.equal(page.button('继续复核').disabled,false);
  assert.equal(page.button('执行已列清理清单').disabled,true);assert.equal(page.opened.length,0);assert.equal(page.requests.length,0);
  assert.ok(page.accesses.every(([,key])=>key.startsWith('tv-review-v1:')));
});

test('真实清理面板流程：开始只生成清单；拒绝确认不删；确认后验证并转绿',async()=>{
  const review=fs.readFileSync(require('node:path').join(__dirname,'../season-review.user.js'),'utf8');
  const dir='/115/Library_incs/nas/电视剧/日本剧集/测试剧 (2025)/Season 1';
  const task={name:'测试剧 (2025)',season:1,path:dir,key:'cleanup-panel',data:{'来源登记集数':2,'播出状态':'已播完'}};
  let files=['Test.S01E01.1080p-A.mkv','Test.S01E02.1080p-A.mkv','Test.S01E01.720p-B.mkv'].map(name=>({name,path:dir+'/'+name,size:'1 GB',modified:'2026-10-20 10:00',directory:false}));
  const updates=new Map(),removed=[];let permit=false,queries=0;
  const page=await launch({},[],{source:review,confirm:()=>permit,workflow:{read:()=>({tasks:[task],errors:[],skipped:0,updates})},cloudClient:{
    worker(){},listing:async path=>{assert.equal(path,dir);return structuredClone(files);},queryDouban:async()=>{queries++;return {total:2,title:'测试剧 (2025)',releaseYears:['2025'],url:'https://movie.douban.com/subject/1/',broadcast:'已播完'};},
    deleteFiles:async batch=>{for(const f of batch){removed.push(f.path);files=files.filter(r=>r.path!==f.path);}}
  }});
  const input=page.elements.find(e=>e.type==='file');input.files=[{name:'下载报告.xlsx',size:10,arrayBuffer:async()=>new ArrayBuffer(10)}];await input.fire('change');
  await page.button('开始复核').click();assert.equal(queries,1);assert.equal(removed.length,0);assert.equal(updates.get(task).state,'待确认清理');assert.equal(page.button('执行已列清理清单').disabled,false);
  await page.button('执行已列清理清单').click();assert.equal(removed.length,0);
  permit=true;await page.button('执行已列清理清单').click();assert.equal(removed.length,1);assert.equal(files.length,2);
  assert.equal(updates.get(task).color,'green');assert.equal(updates.get(task).videoCount,2);assert.equal(updates.get(task).deleted.length,1);assert.equal(page.button('执行已列清理清单').disabled,true);
});

test('目录不存在跳过本季并继续下一剧，保持黄色且不查询缺失目录的豆瓣或执行删除',async()=>{
 const source=fs.readFileSync(require('node:path').join(__dirname,'../season-review.user.js'),'utf8');
 const tasks=['甲','乙'].map(n=>({name:'测试剧'+n+' (2025)',season:1,path:'/115/Library_incs/nas/电视剧/日本剧集/测试剧'+n+' (2025)/Season 1',key:n,data:{}}));
 const updates=new Map(),queries=[],reads=[];
 const page=await launch({},[],{source,workflow:{read:()=>({tasks,errors:[],skipped:0,updates})},cloudClient:{worker(){},listing:async(p,options)=>{assert.equal(options.skipMissing,true);reads.push(p);if(p===tasks[0].path)throw Object.assign(Error('目录不存在：'+p),{name:'DirectoryMissing'});return [];},queryDouban:async t=>{queries.push(t.key);return {title:t.name,total:1,releaseYears:['2025'],url:'https://movie.douban.com/subject/1/',broadcast:'已播完'};},deleteFile(){assert.fail('复核不得删除');}}});
 const input=page.elements.find(e=>e.type==='file');input.files=[{name:'报告.xlsx',size:10,arrayBuffer:async()=>new ArrayBuffer(10)}];await input.fire('change');await page.button('开始复核').click();
 assert.equal(updates.size,2);assert.equal(updates.get(tasks[0]).state,'目录不存在，已跳过');assert.equal(updates.get(tasks[0]).color,null);assert.equal(updates.get(tasks[0]).videoCount,null);assert.deepEqual(queries,['乙']);assert.equal(reads.length,3);assert.equal(page.button('继续复核').disabled,true);
});

test('复核阶段报告按钮仅开始后启用，保留暂停与继续尝试，重新导入后重置',async()=>{
 const source=fs.readFileSync(require('node:path').join(__dirname,'../season-review.user.js'),'utf8');let failed=true,captured;
 const task={name:'测试剧 (2025)',season:1,path:'/115/Library_incs/nas/电视剧/日本剧集/测试剧 (2025)/Season 1',key:'stage',data:{}};
 const page=await launch({},[],{source,stageReport:{write:stage=>{captured=structuredClone(stage);return new Uint8Array([1]);}},workflow:{read:()=>({tasks:[task],errors:[],updates:new Map()})},cloudClient:{worker(){},listing:async()=>{if(failed)throw Object.assign(Error('文件列表加载失败'),{name:'Pause'});return [];},queryDouban:async()=>({total:1,title:task.name,releaseYears:['2025'],url:'https://movie.douban.com/subject/1/',broadcast:'已播完'})}});
 const button=page.button('复核阶段报告');assert.ok(button);assert.equal(button.disabled,true);
 const input=page.elements.find(e=>e.type==='file');const importFile=async()=>{input.files=[{name:'原报告.xlsx',size:10,arrayBuffer:async()=>new ArrayBuffer(10)}];await input.fire('change');};
 await importFile();assert.equal(button.disabled,true);await page.button('开始复核').click();assert.equal(button.disabled,false);await button.click();assert.equal(captured.attempts.length,1);assert.equal(captured.status,'已暂停，可继续');
 failed=false;await page.button('继续复核').click();await button.click();assert.equal(captured.attempts.length,2);assert.equal(captured.attempts[0].result.state,'已暂停');assert.equal(captured.status,'已完成');assert.ok(page.elements.some(e=>e.download==='原报告_复核阶段报告.xlsx'));
 await importFile();assert.equal(button.disabled,true);
});

test('批次结果核实只读检查保留清单，分别记录已消失和仍存在文件',async()=>{
 const source=fs.readFileSync(require('node:path').join(__dirname,'../season-review.user.js'),'utf8'),dir='/115/Library_incs/nas/电视剧/日本剧集/测试剧 (2025)/Season 1';
 const file=name=>({name,path:dir+'/'+name,directory:false,size:'1 GB',modified:'2026-10-22 10:00'}),keep=file('Test.S01E01.1080p-A.mkv'),a=file('Test.S01E01.720p-B.mkv'),b=file('Test.S01E01.480p-C.mkv');
 const task={name:'测试剧 (2025)',season:1,path:dir,key:'batch-test',data:{}},key='tv-review-v1:cleanup:'+task.key;
 const pending={batchId:'batch',beforeSnapshot:[keep,a,b],files:[a,b].map(f=>({path:f.path,before:f,batchId:'batch'}))};
 const page=await launch({[key]:{records:[],pending}},[],{source,workflow:{read:()=>({tasks:[task],errors:[],updates:new Map()})},cloudClient:{worker(){},listing:async()=>structuredClone([keep,b]),queryDouban:async()=>({total:1,title:task.name,releaseYears:['2025'],url:'https://movie.douban.com/subject/1/',broadcast:'已播完'}),deleteFiles(){assert.fail('核实不能再删除');}}});
 const input=page.elements.find(e=>e.type==='file');input.files=[{name:'报告.xlsx',size:10,arrayBuffer:async()=>new ArrayBuffer(10)}];await input.fire('change');await page.button('开始复核').click();await page.button('核实上次删除结果').click();
 const record=page.values.get(key);assert.equal(record.pending,null);assert.equal(record.records[0].state,'manual-verified');assert.equal(record.records[1].state,'cancelled');assert.equal(page.button('继续复核').disabled,false);
});

test('独立暂停执行等待当前批次核实，继续仅删除剩余批次且不重复确认',async()=>{
 const source=fs.readFileSync(require('node:path').join(__dirname,'../season-review.user.js'),'utf8'),dir='/115/Library_incs/nas/电视剧/日本剧集/测试剧 (2025)/Season 1';
 const task={name:'测试剧 (2025)',season:1,path:dir,key:'pause-batch',data:{'来源登记集数':2,'播出状态':'已播完'}},updates=new Map();
 const file=name=>({name,path:dir+'/'+name,directory:false,size:'1 GB',modified:'2026-10-20 10:00'});
 let files=['Test.S01E01.1080p-A.mkv','Test.S01E02.1080p-A.mkv',...Array.from({length:51},(_,i)=>'Test.S01E01.720p-B (copy '+(i+1)+').mkv')].map(file),confirmations=0,release,started;
 const entered=new Promise(r=>started=r),gate=new Promise(r=>release=r),batches=[];
 const page=await launch({},[],{source,confirm:()=>{confirmations++;return true;},workflow:{read:()=>({tasks:[task],errors:[],updates})},cloudClient:{worker(){},listing:async()=>structuredClone(files),queryDouban:async()=>({total:2,title:task.name,releaseYears:['2025'],url:'https://movie.douban.com/subject/1/',broadcast:'已播完'}),deleteFiles:async batch=>{batches.push(batch.map(f=>f.path));if(batches.length===1){started();await gate;}files=files.filter(f=>!batch.some(b=>b.path===f.path));}}});
 assert.equal(page.button('暂停执行').disabled,true);assert.equal(page.button('继续执行').disabled,true);
 const input=page.elements.find(e=>e.type==='file');input.files=[{name:'报告.xlsx',size:10,arrayBuffer:async()=>new ArrayBuffer(10)}];await input.fire('change');await page.button('开始复核').click();
 assert.equal(page.button('继续执行').disabled,true);const executing=page.button('执行已列清理清单').click();await entered;
 assert.equal(page.button('暂停执行').disabled,false);assert.equal(page.button('暂停').disabled,true);await page.button('暂停执行').click();assert.equal(page.button('继续执行').disabled,true);
 release();await executing;assert.equal(batches.length,1);assert.equal(batches[0].length,50);assert.equal(files.length,3);assert.equal(page.button('继续执行').disabled,false);
 assert.equal(page.values.get('tv-review-v1:cleanup:'+task.key).pending,null);await page.button('继续执行').click();
 assert.deepEqual(batches.map(b=>b.length),[50,1]);assert.equal(new Set(batches.flat()).size,51);assert.equal(confirmations,1);assert.equal(updates.get(task).color,'green');assert.equal(page.button('继续执行').disabled,true);assert.equal(page.button('暂停执行').disabled,true);
});
test('清理结果未知时继续执行不可用，必须先核实待处理批次',async()=>{
 const source=fs.readFileSync(require('node:path').join(__dirname,'../season-review.user.js'),'utf8'),dir='/115/Library_incs/nas/电视剧/日本剧集/测试剧 (2025)/Season 1',task={name:'测试剧 (2025)',season:1,path:dir,key:'unknown-batch',data:{}};
 const files=['Test.S01E01.1080p-A.mkv','Test.S01E02.1080p-A.mkv','Test.S01E01.720p-B.mkv'].map(name=>({name,path:dir+'/'+name,size:'1 GB',modified:'2026-10-20 10:00',directory:false}));let calls=0;
 const page=await launch({},[],{source,workflow:{read:()=>({tasks:[task],errors:[],updates:new Map()})},cloudClient:{worker(){},listing:async()=>structuredClone(files),queryDouban:async()=>({total:2,title:task.name,releaseYears:['2025'],url:'https://movie.douban.com/subject/1/',broadcast:'已播完'}),deleteFiles:async()=>{calls++;throw Error('响应未知');}}});
 const input=page.elements.find(e=>e.type==='file');input.files=[{name:'报告.xlsx',size:10,arrayBuffer:async()=>new ArrayBuffer(10)}];await input.fire('change');await page.button('开始复核').click();await page.button('执行已列清理清单').click();assert.equal(calls,1);assert.equal(page.button('继续执行').disabled,true);await page.button('继续执行').click();assert.equal(calls,1);assert.ok(page.button('核实上次删除结果'));
});
