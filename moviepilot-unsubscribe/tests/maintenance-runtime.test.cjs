const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {parseHTML} = require('linkedom');
const core = require('../maintenance-core.js');
const runtimeSource = fs.readFileSync(require('node:path').join(__dirname,'../maintenance-runtime.js'),'utf8');

function harness(rows, hash='#/subscribe/tv') {
  const {window, document} = parseHTML('<body><div id="details"></div><select id="mode"></select><button id="start"></button><button id="preview"></button><button id="stop"></button></body>');
  const ui = document.querySelector('#details').parentElement;
  const logs=[], exported=[]; let seq=0, stopped=false;
  const data=rows.map((r,i)=>({...r,index:i,original:i}));
  const cards=data.map(row=>{const card=document.createElement('div');card.innerHTML=`<div class="font-medium">${row.year}</div><div class="font-bold">${row.name}</div><div class="flex-shrink-0 text-subtitle-2">${row.progress}</div>`;return card;});
  const targetRows=()=>data.map((r,i)=>({index:i,name:r.name,key:`${r.year}|${r.name}`,card:cards[r.original??i],button:document.createElement('button'),menu:`m-${r.id}`})).sort((a,b)=>b.index-a.index);
  const localMap=new Map(); const storage={};
  const localStorage={getItem:k=>storage[k]??null,setItem:(k,v)=>{storage[k]=String(v);},removeItem:k=>delete storage[k]};
  class BC { constructor(){this.onmessage=null;BC.instances.push(this);} postMessage(m){BC.instances.forEach(x=>{if(x!==this)x.onmessage?.({data:m});});} close(){this.closed=true;} } BC.instances=[];
  const URLMock={createObjectURL:b=>{exported.push(b);return 'blob:mock';},revokeObjectURL:()=>{}};
  const blob=class Blob {constructor(parts,opts){this.parts=parts;this.type=opts?.type;}};
  let lockRelease=null;
  const navigator={locks:{request:(_name,_opts,cb)=>cb({})}};
  window.open=()=>({focus(){throw new Error('禁止抢焦点');},closed:false}); window.focus=()=>{throw new Error('禁止抢焦点');};Object.defineProperty(document,'hidden',{value:true});
  const context=vm.createContext({window,document,location:{hash,href:'https://mp.local/'+hash},crypto:{randomUUID:()=>`id-${++seq}`},BroadcastChannel:BC,localStorage,Blob:blob,URL:URLMock,MPMaintenanceCore:core,navigator,console,setTimeout,clearTimeout,setInterval,clearInterval,Date,JSON,Element:window.Element,Event:window.Event,KeyboardEvent:window.KeyboardEvent,HTMLInputElement:window.HTMLInputElement});
  vm.runInContext(runtimeSource,context);
  const h={host:document.createElement('div'),all:(s,r=document)=>Array.from(r.querySelectorAll(s)),visible:()=>true,clean:s=>String(s??'').replace(/\s+/g,' ').trim(),ui,
    say:s=>logs.push(String(s)),pause:async()=>{await new Promise(r=>setTimeout(r,0));},guard:ctx=>{if(stopped||ctx.controller.signal.aborted)throw new Error(ctx.reason||'已停止');},
    settle:async()=>({g:document.body,s:{},rows:targetRows()}),locateIndex:async(ctx,index)=>{const rows=targetRows();return {g:document.body,s:{},rows,target:rows.find(x=>x.index===index)};},snapshot:()=>targetRows(),
    setMenu:b=>{h.lastMenu=b;},closeMenu:()=>{},setPending:s=>{h.pending=s;},increment:()=>{h.count=(h.count||0)+1;},waitMenu:async(target)=>{const item=document.createElement('div');item.click=()=>{h.cancelled=(h.cancelled||[]).concat(target.index);const at=data.findIndex(r=>r.original===target.index);if(at>=0)data.splice(at,1);};return item;},waitRemoved:async()=>{},waitRemovedMiddle:async()=>{},stop:r=>{stopped=true;h.stopReason=r;}};
  h.host.attachShadow({mode:'open'}); h.host.shadowRoot.innerHTML='<div></div>'; document.body.append(h.host);
  const api=context.MPMaintenanceRuntime.create(h);
  return {api,context,ui,logs,exported,storage,h,data,targetRows, async close(){api.destroy();lockRelease?.();}};
}

(async()=>{
  let f=harness(Array.from({length:103},(_,i)=>({name:`剧${i} S01`,season:1,year:i===101?2025:2026,progress:i===102?'0 / 12':i===101?'1 / 12':'12 / 12'})));
  const ctx={maintenance:true,controller:new AbortController(),reason:''};
  await f.api.run(ctx,true); assert.equal(f.h.cancelled,undefined); assert.ok(f.logs.some(x=>/保护前100项/.test(x))); f.api.finish(ctx); await f.close(); console.log('PASS parent preview protects and does not cancel');

  f=harness([...Array.from({length:100},()=>({name:'保护 S01',season:1,year:2025,progress:'0 / 2'})),{name:'旧零 S01',season:1,year:2025,progress:'0 / 2'},{name:'新零 S01',season:1,year:2025,progress:'0 / 2'},{name:'新剧 S01',season:1,year:2026,progress:'0 / 2'},{name:'完成 S01',season:1,year:2025,progress:'2 / 2'}]);
  const ctx2={maintenance:true,controller:new AbortController(),reason:''}; await f.api.run(ctx2,false); assert.deepEqual(f.h.cancelled,[101,100]); f.api.finish(ctx2); await f.close(); console.log('PASS zero-only run skips year and complete records');

  f=harness([...Array.from({length:100},()=>({name:'保护 S01',season:1,year:2025,progress:'0 / 2'})),{name:'部分 S01',season:1,year:2025,progress:'1 / 2'}]); const ctx3={maintenance:true,controller:new AbortController(),reason:''}; await assert.rejects(f.api.run(ctx3,false)); assert.equal(f.h.cancelled,undefined); await f.close(); console.log('PASS missing resource worker cancels nothing');

  f=harness([...Array.from({length:100},()=>({name:'保护 S01',season:1,year:2025,progress:'0 / 2'})),{name:'零 S01',season:1,year:2025,progress:'0 / 2'}]); const ctx4={maintenance:true,controller:new AbortController(),reason:'手动停止'}; const p=f.api.run(ctx4,false); setTimeout(()=>ctx4.controller.abort(),0); await assert.rejects(p); f.api.finish(ctx4); assert.ok(f.storage['mp-tv-maintenance-v2:latest']); assert.ok(f.exported.length>=1); await f.close(); console.log('PASS stop finish persists and exports report');

  f=harness([]);f.context.navigator.locks.request=async(name,options,callback)=>callback(null);
  const locked={maintenance:true,controller:new AbortController()};await assert.rejects(f.api.run(locked,false),/另一个订阅页/);f.api.finish(locked);assert.equal(f.h.cancelled,undefined);await f.close();console.log('PASS concurrent run lock refuses second runner');

  for(const success of [true,false]){
    f=harness([...Array.from({length:100},()=>({name:'保护 S01',season:1,year:2025,progress:'0 / 2'})),{name:'部分 S01',season:1,year:2025,progress:'1 / 2'}]);
    const events=[],peer=new f.context.BroadcastChannel('test');
    peer.onmessage=({data:m})=>{
      if(m.type==='ping')peer.postMessage({type:'ready',from:'resource',to:m.from,ready:true});
      if(m.type==='job'){
        events.push('download-response');
        const state={status:success?'download-confirmed':'failed',resourceTitle:'资源',site:'天空',seeders:12,detail:success?'下载已提交':'没有资源'};
        f.context.localStorage.setItem('mp-tv-maintenance-v2:job:'+m.jobId,JSON.stringify(state));
        peer.postMessage({type:'result',from:'resource',to:m.from,jobId:m.jobId,ok:success,state,error:success?null:'没有资源'});
      }
    };
    [...f.ui.querySelectorAll('button')].find(b=>b.textContent==='打开／绑定资源页').click();
    const oldWaitMenu=f.h.waitMenu;f.h.waitMenu=async(...args)=>{const item=await oldWaitMenu(...args);const click=item.click;item.click=()=>{events.push('cancel');click();};return item;};
    f.ui.querySelector('#details').hidden=true;
    const ctx5={maintenance:true,controller:new AbortController(),reason:''};await f.api.run(ctx5,false);f.api.finish(ctx5);
    assert.equal(f.ui.querySelector('#details').hidden,true,'完成和导出不能强制展开面板');
    assert.deepEqual(events,success?['download-response','cancel']:['download-response']);
    assert.equal(f.h.cancelled?.length||0,success?1:0);
    const report=JSON.parse(f.storage['mp-tv-maintenance-v2:report:'+JSON.parse(f.storage['mp-tv-maintenance-v2:latest'])]);
    assert.equal(report.records.find(r=>r.index===100).status,success?'完成':'保留订阅');
    if(success){assert.equal(report.events.length,2);assert.ok(f.ui.querySelector('#live-results').textContent.includes('下载任务提交成功'));assert.ok(f.ui.querySelector('#live-results').textContent.includes('取消订阅成功'));}
    assert.deepEqual([...f.ui.querySelectorAll('details')].map(e=>e.id),['setup-actions','connection-actions','run-actions','results-actions','report-actions','advanced-actions']);
    await f.close();console.log('PASS parent download '+(success?'confirmed before cancel':'failure retains subscription'));
  }
  const protectedRows=()=>Array.from({length:100},()=>({name:'保护 S01',year:2025,progress:'0 / 2'}));
  for(const fail of [false,true]){
    f=harness([...protectedRows(),{name:'下一项 S01',year:2025,progress:'1 / 2'},{name:'测试项 S01',year:2025,progress:'0 / 2'}]);
    if(fail){const locate=f.h.locateIndex;let visits=0;f.h.locateIndex=async(ctx,index)=>{const v=await locate(ctx,index);if(index===101&&++visits>1)v.target={...v.target,key:'changed'};return v;};}
    const single={maintenance:true,controller:new AbortController()};await f.api.run(single,false,true);f.api.finish(single);
    assert.deepEqual(f.h.cancelled||[],fail?[]:[101]);
    await f.close();console.log('PASS single zero '+(fail?'failure stops before next':'success without resource worker'));
  }
  for(const singleMode of [true,false]){
    f=harness([...protectedRows(),{name:'下一项 S01',year:2025,progress:'1 / 2'},{name:'首项 S01',year:2025,progress:singleMode?'1 / 2':'0 / 2'}]);
    let jobs=0;const peer=new f.context.BroadcastChannel('test');
    peer.onmessage=({data:m})=>{if(m.type==='ping')peer.postMessage({type:'ready',from:'resource',to:m.from,ready:true});if(m.type==='job'){jobs++;f.context.localStorage.setItem('mp-tv-maintenance-v2:job:'+m.jobId,JSON.stringify({status:'failed'}));peer.postMessage({type:'result',from:'resource',to:m.from,jobId:m.jobId,ok:false,error:'模拟搜索失败'});}};
    [...f.ui.querySelectorAll('button')].find(b=>b.textContent==='打开／绑定资源页').click();
    const c={maintenance:true,controller:new AbortController()};await f.api.run(c,false,singleMode);f.api.finish(c);
    assert.equal(jobs,1);assert.deepEqual(f.h.cancelled||[],singleMode?[]:[101]);await f.close();
    console.log('PASS '+(singleMode?'single download failure does not try next':'batch zero first still connects resource worker'));
  }
  for(const previewOnly of [true,false]){
    f=harness([...protectedRows(),{name:'仙逆 S01',year:2023,progress:'0 / 10'},{name:'仙逆 S02',year:2023,progress:'1 / 10'},{name:'仙逆合集篇 S01',year:2025,progress:'0 / 10'}]);
    const input=f.ui.querySelector('#skip-names');input.value=' 仙逆 \n克金玩家\n仙逆 S01\n';input.dispatchEvent(new f.context.window.Event('input'));
    assert.equal(JSON.parse(f.storage['mp-tv-maintenance-v2:skip-names']),input.value);
    const c={maintenance:true,controller:new AbortController()};await f.api.run(c,previewOnly);assert.equal(input.disabled,true);f.api.finish(c);assert.equal(input.disabled,false);
    assert.deepEqual(f.h.cancelled||[],previewOnly?[]:[102]);
    const report=JSON.parse(f.storage['mp-tv-maintenance-v2:report:'+JSON.parse(f.storage['mp-tv-maintenance-v2:latest'])]);
    assert.deepEqual(report.skipNames,['仙逆','克金玩家']);assert.equal(report.records.filter(r=>r.action==='skip-name').length,2);
    assert.ok(f.ui.querySelector('textarea[aria-label="最近处理记录"]').value.includes('自定义名单跳过'));
    await f.close();console.log('PASS skip names '+(previewOnly?'preview':'run')+' protects all seasons, exact names, zero and download actions');
  }
  for(const confirmed of [false,true]){
    f=harness([...protectedRows(),{name:'消息迟到 S01',year:2025,progress:'1 / 2'}]);
    const peer=new f.context.BroadcastChannel('test');
    peer.onmessage=({data:m})=>{if(m.type==='ping')peer.postMessage({type:'ready',from:'resource',to:m.from,ready:true});if(m.type==='job')f.context.localStorage.setItem('mp-tv-maintenance-v2:job:'+m.jobId,JSON.stringify({status:confirmed?'download-confirmed':'failed',detail:'媒体结果加载超时'}));};
    await [...f.ui.querySelectorAll('button')].find(b=>b.textContent==='打开／绑定资源页').onclick();
    const c={maintenance:true,controller:new AbortController()};await f.api.run(c,false,true);f.api.finish(c);
    assert.equal(f.h.cancelled?.length||0,confirmed?1:0);
    if(!confirmed)assert.ok(f.logs.at(-1).includes('媒体结果加载超时'));
    await f.close();console.log('PASS durable '+(confirmed?'confirmed result':'failure reason')+' survives missing channel response');
  }
  f=harness([]);
  const clickNamed=label=>[...f.ui.querySelectorAll('button')].find(b=>b.textContent===label).onclick();
  clickNamed('查看最近记录');assert.ok(f.logs.at(-1).includes('没有可导出的维护记录'));
  f.storage['mp-tv-maintenance-v2:latest']=JSON.stringify('export-test');
  f.storage['mp-tv-maintenance-v2:report:export-test']=JSON.stringify({id:'export-test',status:'单项试跑结束',endedAt:'2026-09-18',records:[{index:100,name:'中文记录',status:'完成'}]});
  clickNamed('查看最近记录');assert.ok(f.ui.querySelector('#report-view textarea').value.includes('中文记录'));
  clickNamed('导出最近记录 CSV');assert.equal(decodeURIComponent(f.ui.querySelector('#report-view a').download),'MoviePilot-电视剧处理记录-export-test.csv');assert.ok(f.logs.at(-1).includes('无法确认下载完成'));
  let saved='',closed=false;f.context.window.showSaveFilePicker=async()=>({name:'用户选择.csv',createWritable:async()=>({write:async b=>{saved=b.parts.join('');},close:async()=>{closed=true;}})});
  await clickNamed('CSV 另存为（选择位置）');assert.ok(saved.startsWith('\uFEFF'));assert.ok(saved.includes('中文记录'));assert.ok(closed);assert.ok(f.logs.at(-1).includes('已保存：用户选择.csv'));
  f.context.window.showSaveFilePicker=async()=>{throw Object.assign(new Error('cancel'),{name:'AbortError'});};await clickNamed('CSV 另存为（选择位置）');assert.ok(f.logs.at(-1).includes('已取消另存为'));
  await f.close();console.log('PASS report view, filename, save-as success and cancellation');
})().catch(e=>{console.error(e);process.exitCode=1;});
