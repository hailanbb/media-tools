(function(root){
  'use strict';
  root.MPMaintenanceRuntime={create(h){
    const C=root.MPMaintenanceCore;
    const NS='mp-tv-maintenance-v2', id=crypto.randomUUID();
    const channel=new BroadcastChannel(NS), peers=new Map();
    const {all,visible,clean,ui}=h;
    const RESOURCE_WINDOW='mp-tv-maintenance-resource-v2';
    const LEASE_MS=300000;
    let ready=false, worker=null, active=null, waiting=null, heartbeat=null, releaseLock=null, companion=null, disposed=false;
    const isWorkerPage=()=>!/^#\/subscribe\//.test(location.hash);
    const read=(key)=>JSON.parse(localStorage.getItem(NS+':'+key)||'null');
    const write=(key,value)=>localStorage.setItem(NS+':'+key,JSON.stringify(value));
    const send=(type,body={})=>{if(!disposed)channel.postMessage({type,from:id,...body});};
    const stamp=()=>new Date().toISOString();
    const button=(text,fn)=>{const b=document.createElement('button');b.textContent=text;b.onclick=fn;ui.querySelector('#details').append(b);return b;};
    const connect=button('连接此资源页',()=>{
      if(!isWorkerPage()){h.say('请在另一个资源搜索标签页点击“连接此资源页”');return;}
      ready=!ready;connect.textContent=ready?'断开资源页':'连接此资源页';
      if(ready)window.name=RESOURCE_WINDOW;
      send('ready',{ready});h.say(ready?'资源页已连接。回到电视剧页，选择“2026年前未完成维护”启动。':'资源页已断开');
    });
    const openWorker=button('打开／绑定资源页',async()=>{
      if(active){h.say('任务正在运行，请先停止。');return;}
      companion=window.open(location.origin+'/#/resource',RESOURCE_WINDOW);
      if(!companion)h.say('浏览器阻止打开资源页，请允许本站弹出窗口后再点此按钮。');
      else {
        peers.clear();h.say('正在检查资源页连接……');
        for(let n=0;n<20&&!disposed;n++){send('ping');await new Promise(r=>setTimeout(r,500));if(peers.size)break;}
        h.say(peers.size===1?'资源页已连接。回到电视剧页，自由选择单项试跑或批量运行，预览可选。':peers.size>1?'连接了多个资源页，请只保留一个连接。':'资源页尚未连接：请确认该页也显示相同版本脚本，再点“连接此资源页”。');
      }
    });
    button('查看最近记录',()=>viewReport());
    button('CSV 另存为（选择位置）',()=>saveReportAs());
    const exportButton=button('导出最近记录 CSV',()=>exportReport('csv'));
    button('导出最近记录 Markdown',()=>exportReport('md'));
    const option=document.createElement('option');option.value='maintenance';
    option.textContent='电视剧：2026年前未完成维护';ui.querySelector('#mode').append(option);
    const skipBox=document.createElement('div');skipBox.id='skip-settings';
    const skipLabel=document.createElement('label');skipLabel.htmlFor='skip-names';skipLabel.textContent='跳过的电视剧名（维护模式）';
    const skipInput=document.createElement('textarea');skipInput.id='skip-names';skipInput.rows=4;skipInput.placeholder='每行一个名称，例如：\n仙逆\n克金玩家';
    skipInput.style.cssText='box-sizing:border-box;width:100%;resize:vertical;font:inherit;margin-top:4px';
    skipInput.value=read('skip-names')||'';
    const skipHelp=document.createElement('small');skipHelp.textContent='每行一个，按完整剧名匹配，跳过全部季。自动保存；运行中需先停止再修改。';
    const skipName=value=>C.normalizeTitle(value).replace(/\s+S\d+$/i,'').trim();
    const skipNames=()=>[...new Set(skipInput.value.split(/\r?\n/).map(skipName).filter(Boolean))];
    skipInput.addEventListener('input',()=>{try{write('skip-names',skipInput.value);skipHelp.textContent=`已保存 ${skipNames().length} 个剧名；完整名称匹配，跳过全部季。`;}catch(e){skipHelp.textContent='保存失败：'+e.message+'。当前输入仍用于本页下一次运行。';}});
    skipBox.append(skipLabel,skipInput,skipHelp);ui.querySelector('#mode').after(skipBox);
    if(/^#\/subscribe\/tv(?:\/|\?|$)/.test(location.hash))option.selected=true;
    function modeLabel(){const selected=ui.querySelector('#mode').value==='maintenance';ui.querySelector('#start').textContent=selected?'批量运行（含下载）':'开始取消';ui.querySelector('#preview').textContent=selected?'预览清单（可选）':'检查下一目标（不取消）';}
    ui.querySelector('#mode').addEventListener('change',modeLabel);modeLabel();
    function saveReport(){if(active){write('report:'+active.report.id,active.report);write('latest',active.report.id);}}
    function reconcile(report){
      if(!report)return;
      const ledger=read('ledger')||{};
      for(const row of report.records||[]){
        const state=row.jobId?read('job:'+row.jobId):null;
        if(row.status!=='完成'&&['download-submitting','download-confirmed'].includes(state?.status)){
          ledger[row.key]={run:report.id,jobId:row.jobId,status:state.status,time:state.time};
          Object.assign(row,{status:'待核对',detail:'上轮中断，下载已发起或提交成功；请核对下载器',resourceTitle:state.resourceTitle,site:state.site,seeders:state.seeders});
        }
      }
      write('ledger',ledger);
    }
    function reportNow(){
      const report=active?.report||read('report:'+read('latest'));if(!report)throw new Error('没有可导出的维护记录');
      if(!active){const control=read('control');if(!control?.active||Date.now()-control.time>LEASE_MS){reconcile(report);if(!report.endedAt){report.status='任务中断（恢复记录）';report.endedAt=stamp();}write('report:'+report.id,report);}}
      return report;
    }
    let reportUrl=null;
    function reportView(kind,r,text){
      ui.querySelector('#report-view')?.remove();
      if(reportUrl)URL.revokeObjectURL(reportUrl);
      const box=document.createElement('div');box.id='report-view';
      const info=document.createElement('p');info.textContent=`记录：${r.id}\n状态：${r.status}\n条目：${r.records?.length||0} 项\n以下内容可直接复制保存。`;
      const area=document.createElement('textarea');area.readOnly=true;area.value=text;area.setAttribute('aria-label','最近处理记录');area.style.cssText='box-sizing:border-box;width:100%;height:160px;font:12px monospace;';
      const copy=document.createElement('button');copy.textContent='复制记录';copy.onclick=async()=>{try{await navigator.clipboard.writeText(area.value);info.textContent='记录已复制，可粘贴到记事本或 Obsidian。';}catch{area.focus();area.select();info.textContent='自动复制不可用，内容已选中，请按 Ctrl+C。';}};
      const a=document.createElement('a');
      reportUrl=URL.createObjectURL(new Blob([text],{type:kind==='csv'?'text/csv;charset=utf-8':'text/markdown;charset=utf-8'}));a.href=reportUrl;a.download=`MoviePilot-电视剧处理记录-${r.id}.${kind}`;a.textContent='点击下载 '+a.download;a.style.cssText='display:block;color:#93c5fd;overflow-wrap:anywhere';
      const close=document.createElement('button');close.textContent='收起记录';close.onclick=()=>{box.remove();if(reportUrl)URL.revokeObjectURL(reportUrl);reportUrl=null;};
      box.append(info,area,copy,a,close);(ui.querySelector('#report-actions')||ui.querySelector('#details')).append(box);
      return a;
    }
    function viewReport(){try{const r=reportNow();reportView('md',r,C.reportMarkdown(r));h.say('最近记录已在“记录与导出”中显示，可复制或下载。');}catch(e){h.say('无法读取记录：'+e.message+'。只有维护模式预览或运行才生成记录；人工操作不自动记入脚本。');}}
    async function saveReportAs(){
      let r,text;
      try{r=reportNow();text=C.reportCSV(r);}
      catch(e){h.say('无法读取记录：'+e.message);return;}
      if(typeof window.showSaveFilePicker!=='function'){exportReport('csv',r);h.say('当前浏览器不支持选择保存位置；已显示记录和下载链接。下载后按 Ctrl+J 查看。');return;}
      try{
        const file=await window.showSaveFilePicker({suggestedName:`MoviePilot-电视剧处理记录-${r.id}.csv`,types:[{description:'CSV 处理记录',accept:{'text/csv':['.csv']}}]});
        const stream=await file.createWritable();await stream.write(new Blob([text],{type:'text/csv;charset=utf-8'}));await stream.close();
        h.say(`已保存：${file.name}。位置为你刚才选择的文件夹。`);
      }catch(e){if(e.name==='AbortError'){h.say('已取消另存为，记录仍保存在浏览器中。');return;}reportView('csv',r,text);h.say('另存为失败：'+e.message+'；可复制面板下方内容或点击下载链接。');}
    }
    function exportReport(kind,provided){
      try {
        const r=provided||reportNow();
        const text=kind==='csv'?C.reportCSV(r):C.reportMarkdown(r);
        const a=reportView(kind,r,text);a.click();
        h.say(`本轮状态：${r.status}\n${r.records?.find(x=>['保留订阅','待核对','已停止'].includes(x.status))?.detail||''}\n已发起记录下载：${a.download}\n按 Ctrl+J 查看 Chrome 下载记录，再点“在文件夹中显示”。保存位置由 Chrome 设置决定，脚本无法确认下载完成。若没有下载项，可用“CSV 另存为”或复制下方内容。`);
      }catch(e){h.say('记录导出失败：'+e.message+'；可再次点击导出按钮。');}
    }
    function phase(row,status,detail){
      Object.assign(row,{status,detail,time:stamp()});
      if(['下载已提交','完成','保留订阅','待核对'].includes(status)){
        const event={time:row.time,name:row.displayName||row.name,status,detail:status==='下载已提交'?'下载任务提交成功':status==='完成'?'取消订阅成功':detail};
        active.report.events=(active.report.events||[]).concat(event).slice(-100);renderResults(active.report.events);
      }
      saveReport();h.say(`${row.name||row.displayName||''}：${detail}`);
    }
    function renderResults(events){const list=ui.querySelector('#live-results');if(!list)return;list.replaceChildren();for(const e of [...events].reverse()){const item=document.createElement('li');item.textContent=`${new Date(e.time).toLocaleTimeString()} ${e.name}：${e.detail}`;list.append(item);}}
    function signalStop(reason){
      if(active){active.report.status=reason;try{write('control',{run:active.report.id,owner:id,active:false,time:Date.now()});saveReport();}catch{}send('stop',{run:active.report.id,reason});}
      if(worker){worker.stopped=true;send('stop',{run:worker.run,reason});}
    }
    function workerGuard(ctx){
      const control=read('control');
      if(ctx.stopped||!control?.active||control.run!==ctx.run||control.owner!==ctx.owner||Date.now()-control.time>LEASE_MS)
        throw new Error('任务已停止或订阅页失去连接');
      if(!isWorkerPage())throw new Error('资源页已离开搜索流程');
    }
    async function takeLock(){
      if(!navigator.locks)throw new Error('浏览器不支持安全的跨标签页运行锁');
      await new Promise((resolve,reject)=>{
        navigator.locks.request(NS,{mode:'exclusive',ifAvailable:true},async lock=>{
          if(!lock){reject(new Error('另一个订阅页正在运行维护任务，请先停止它'));return;}
          await new Promise(release=>{releaseLock=release;resolve();});
        }).catch(reject);
      });
    }
    const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
    async function wait(ctx,fn,ms=30000,label='页面状态'){
      const end=Date.now()+ms;
      while(Date.now()<end){workerGuard(ctx);const value=fn();if(value)return value;await sleep(250);}
      throw new Error(`${label}超时（${ms}ms）`);
    }
    async function tick(ctx,ms=350){await sleep(ms);workerGuard(ctx);}
    function dialog(title){return all('.v-dialog.v-overlay--active').filter(visible).find(d=>all('.v-card-title',d).some(e=>clean(e.textContent)===title));}
    function actionButton(scope,text){const matches=all('button',scope).filter(e=>visible(e)&&!e.disabled&&clean(e.textContent)===text);if(matches.length!==1)throw new Error(`找不到唯一的“${text}”按钮`);return matches[0];}
    function closeDialog(d){const close=d.querySelector('button[aria-label="关闭"]')||all('button',d).find(b=>b.querySelector('svg path[d^="M19 6.41"]'));if(!close)throw new Error('无法关闭检查窗口');close.click();}
    function workerState(ctx,extra){ctx.state={...ctx.state,...extra,time:stamp()};write('job:'+ctx.jobId,ctx.state);send('progress',{to:ctx.owner,jobId:ctx.jobId,state:ctx.state});}
    // 只读取本次 UI 操作的响应状态，不读取请求体、Cookie、资源链接或凭据。
    function observeResponse(ctx,method,pattern){
      const prototype=XMLHttpRequest.prototype,open=prototype.open,sendXHR=prototype.send,fetchFn=window.fetch;
      const targets=new WeakMap(),listeners=[];let response=null,claimed=false;
      const match=(m,url)=>{try{const u=new URL(url,location.href);return m?.toUpperCase()===method&&u.origin===location.origin&&pattern.test(u.pathname);}catch{return false;}};
      const result=(status,body)=>{response={ok:status>=200&&status<300&&body?.success===true,status,success:body?.success,message:clean(body?.message||body?.detail||'未收到明确成功响应').slice(0,180)};};
      function wrappedOpen(m,u,...rest){targets.set(this,match(m,u));return open.call(this,m,u,...rest);}
      function wrappedSend(...args){
        if(targets.get(this)&&!claimed){claimed=true;const xhr=this;const done=()=>{xhr.removeEventListener('loadend',done);let body;try{body=xhr.responseType==='json'?xhr.response:JSON.parse(xhr.responseText);}catch{}result(xhr.status,body);};xhr.addEventListener('loadend',done);listeners.push(()=>xhr.removeEventListener('loadend',done));}
        return sendXHR.apply(this,args);
      }
      async function wrappedFetch(input,options){
        const watched=!claimed&&match(options?.method||input?.method||'GET',typeof input==='string'||input instanceof URL?String(input):input?.url);
        if(watched)claimed=true;
        try{const r=await fetchFn.call(this,input,options);if(watched)r.clone().json().then(body=>result(r.status,body),()=>result(r.status));return r;}
        catch(e){if(watched)result(0,{message:e.message});throw e;}
      }
      prototype.open=wrappedOpen;prototype.send=wrappedSend;if(fetchFn)window.fetch=wrappedFetch;
      return {get:()=>response,seen:()=>claimed,close(){listeners.forEach(f=>f());if(prototype.open===wrappedOpen)prototype.open=open;if(prototype.send===wrappedSend)prototype.send=sendXHR;if(window.fetch===wrappedFetch)window.fetch=fetchFn;}};
    }
    function resourceData(card,parent){
      const seasonText=clean(card.querySelector('.chip-season')?.textContent);
      const seasonMatch=/^S(\d+)$/i.exec(seasonText);
      if(!seasonMatch)return null;
      const arrow=card.querySelector('svg.text-success');
      const seedText=arrow?clean(arrow.parentElement.textContent).replace(/,/g,''):'0';
      if(!/^\d+$/.test(seedText))return null;
      const title=parent?.title||clean(card.querySelector('.text-h6.font-weight-bold')?.textContent);
      const site=clean(card.querySelector('.font-weight-bold.text-body-2,.text-body-2.font-weight-bold')?.textContent);
      const resourceTitle=clean(card.querySelector('.text-subtitle-2[title]')?.getAttribute('title'))||parent?.resourceTitle||'';
      const description=clean(`${card.textContent||''} ${card.querySelector('.text-body-2[title]')?.getAttribute('title')||''} ${resourceTitle}`);
      const totals=[...description.matchAll(/全\s*(\d+)\s*集/gu)].map(m=>Number(m[1]));
      const distinct=[...new Set(totals)];
      const size=clean(card.querySelector('.bg-primary .v-chip__content,.text-caption.font-weight-bold.text-primary')?.textContent);
      const ownTotal=distinct.length===1?distinct[0]:distinct.length>1?-1:null;
      const total=ownTotal!=null?ownTotal:(parent?.total??null);
      return {title,season:Number(seasonMatch[1]),site,seeders:Number(seedText),resourceTitle,size,total,
        key:JSON.stringify([title,seasonText,site,resourceTitle,size,Number(seedText)]),element:card};
    }
    function scrollRoot(element){for(let e=element?.parentElement;e&&e!==document.body;e=e.parentElement)if(/auto|scroll/.test(getComputedStyle(e).overflowY)&&e.scrollHeight>e.clientHeight+4)return e;return document.scrollingElement;}
    function bottomComplete(s){const grids=all('.progressive-card-grid').filter(visible);return s.scrollHeight-s.clientHeight-s.scrollTop<=5&&grids.every(g=>{const last=g.querySelector('.progressive-card-grid__track')?.lastElementChild;return !last?.classList.contains('progressive-card-grid__spacer')||last.getBoundingClientRect().height<1;});}
    async function scanResources(ctx,job){
      const candidates=new Map(),groups=new Set();let stable=0,lastSignature='';
      const s=scrollRoot(document.querySelector('.torrent-card'));s.scrollTo({top:0,behavior:'instant'});await tick(ctx);
      for(let step=0;step<2000;step++){
        workerGuard(ctx);
        const cards=all('.torrent-card').filter(visible);
        for(const card of cards){
          const row=resourceData(card);if(!row||C.normalizeTitle(row.title)!==C.normalizeTitle(job.name)||row.season!==job.season)continue;
          candidates.set(row.key,{...row,element:undefined,parentKey:row.key,more:false});
          const more=all('button',card).find(b=>/更多来源/.test(b.textContent));
          if(more&&!groups.has(row.key)){
            groups.add(row.key);workerGuard(ctx);more.click();
            const d=await wait(ctx,()=>all('.v-dialog.v-overlay--active').filter(visible).find(e=>e.querySelector('.more-sources-content')),30000,'其他来源窗口');
            const entries=all('.more-sources-content .v-list-item',d);
            if(!entries.length)throw new Error('其他来源列表无法读取');
            for(const entry of entries){const child=resourceData(entry,row);if(child)candidates.set(child.key,{...child,element:undefined,parentKey:row.key,more:true});}
            closeDialog(d);await tick(ctx);
          }
        }
        const signature=JSON.stringify([candidates.size,s.scrollHeight,s.scrollTop]);
        stable=bottomComplete(s)&&signature===lastSignature?stable+1:0;lastSignature=signature;
        if(stable>=8)break;
        s.scrollTo({top:s.scrollTop+Math.max(200,s.clientHeight*.75),behavior:'instant'});await tick(ctx,250);
      }
      if(stable<8)throw new Error('资源列表扫描未完成，无法确认最大做种数');
      const allCandidates=[...candidates.values()];
      const hasConflict=allCandidates.some(x=>x.total!=null&&x.total!==job.total);
      const usable=allCandidates.filter(x=>hasConflict?x.total===job.total:x.total==null||x.total===job.total);
      if(!usable.length)throw new Error('同名同季资源全集数冲突，无法安全选择');
      return usable;
    }
    async function findResource(ctx,best){
      const s=scrollRoot(document.querySelector('.torrent-card'));s.scrollTo({top:0,behavior:'instant'});await tick(ctx);
      let bottomWait=0;
      for(let n=0;n<2000;n++){
        workerGuard(ctx);
        const matches=all('.torrent-card').filter(visible).filter(card=>resourceData(card)?.key===best.parentKey);
        if(matches.length>1)throw new Error('资源标识重复，停止避免选错');
        if(matches.length===1){
          if(!best.more)return matches[0];
          const parent=resourceData(matches[0]),more=all('button',matches[0]).find(b=>/更多来源/.test(b.textContent));
          if(!more)throw new Error('资源的其他来源入口发生变化');more.click();
          const d=await wait(ctx,()=>all('.v-dialog.v-overlay--active').filter(visible).find(e=>e.querySelector('.more-sources-content')),30000,'其他来源窗口');
          const rows=all('.more-sources-content .v-list-item',d).filter(e=>resourceData(e,parent)?.key===best.key);
          if(rows.length!==1)throw new Error('其他来源无法唯一定位');return rows[0];
        }
        if(bottomComplete(s)&&++bottomWait>8)break;
        s.scrollTo({top:s.scrollTop+Math.max(200,s.clientHeight*.75),behavior:'instant'});await tick(ctx,250);
      }
      throw new Error('资源列表已变化，找不到已选资源');
    }
    async function processResource(ctx,job){
      const validated=C.parseSubscription({index:job.index,name:job.displayName,year:String(job.year),progress:`${job.downloaded}/${job.total}`});
      if(C.classify(validated)!=='download-then-cancel'||validated.key!==job.key)throw new Error('资源任务不符合前100保护、年份或进度规则');
      workerState(ctx,{status:'searching',detail:'搜索媒体名称'});h.say(`正在搜索：${job.name} S${job.season}`);
      if(all('.v-dialog.v-overlay--active').some(visible))throw new Error('资源页有未关闭弹窗');
      const input=await wait(ctx,()=>document.querySelector('#global-media-search'),30000,'资源搜索输入框');
      workerGuard(ctx);input.focus();Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,job.name);
      input.dispatchEvent(new Event('input',{bubbles:true}));await tick(ctx);
      input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true}));
      await wait(ctx,()=>location.hash.startsWith('#/browse/media/search'),15000,'媒体搜索页面');
      const media=await wait(ctx,()=>{
        const matches=all('.media-card').filter(card=>card.isConnected&&C.normalizeTitle(card.querySelector('.media-card-title')?.textContent)===C.normalizeTitle(job.name)&&
          clean(card.querySelector('.font-semibold.text-sm')?.textContent)===String(job.year)&&all('.v-chip',card).some(e=>clean(e.textContent)==='电视剧'));
        if(matches.length>1)throw new Error('同名同年份的媒体结果不唯一');return matches[0];
      },120000,`媒体结果“${job.name}”（${job.year}）加载或匹配`);
      const search=all('button',media).filter(b=>b.querySelector('svg path[d^="M9.5 3A6.5"]'));
      if(search.length!==1)throw new Error('找不到海报左下角的搜索按钮');workerGuard(ctx);search[0].click();
      const sites=await wait(ctx,()=>{
        if(location.hash.startsWith('#/resource'))throw new Error('站点选择未打开：页面已进入资源页，无法核对搜索站点');
        return dialog('选择站点');
      },30000,'站点选择窗口');
      const siteRows=all('.site-checkbox-wrapper',sites);
      const originalSites=siteRows.filter(e=>e.classList.contains('site-selected')).map(e=>clean(e.querySelector('.site-name')?.textContent));
      const wantedSites=[...new Set([...originalSites,'天空','观众'])].sort();
      for(const name of wantedSites)if(siteRows.filter(e=>clean(e.querySelector('.site-name')?.textContent)===name).length!==1)throw new Error(`站点“${name}”不可用`);
      for(const item of siteRows){workerGuard(ctx);const wanted=wantedSites.includes(clean(item.querySelector('.site-name')?.textContent));if(wanted&&!item.classList.contains('site-selected')){item.click();await tick(ctx,80);}}
      const selected=all('.site-checkbox-wrapper.site-selected',sites).map(e=>clean(e.querySelector('.site-name')?.textContent)).sort();
      if(JSON.stringify(selected)!==JSON.stringify(wantedSites))throw new Error('站点选择核对失败，必须保留原选站点并追加天空、观众');
      workerState(ctx,{status:'searching',detail:`等待资源搜索完成：${selected.join('、')}`});
      workerGuard(ctx);actionButton(sites,'搜索').click();
      await wait(ctx,()=>location.hash.startsWith('#/resource'),15000,'资源搜索结果页面');
      await tick(ctx,700);
      await wait(ctx,()=>all('h1,h2,h3').some(e=>visible(e)&&clean(e.textContent)==='资源搜索结果')&&!document.body.innerText.includes('正在搜索，请稍候'),240000,'资源结果加载');
      if(document.body.innerText.includes('未搜索到任何资源'))throw new Error('已选站点未找到资源');
      if(all('.filter-tag').some(visible)){
        const clear=all('button').filter(visible).filter(b=>/^(清除筛选|清空筛选|重置筛选|清除所有筛选)$/.test(clean(b.textContent)));
        if(clear.length!==1)throw new Error('资源页保留了其他筛选，无法核对全量最大做种数');
        workerGuard(ctx);clear[0].click();await tick(ctx);
        if(all('.filter-tag').some(visible))throw new Error('旧资源筛选未能清除');
      }
      const seasonLabel=`S${String(job.season).padStart(2,'0')}`;
      const seasonButtons=all('button[aria-haspopup="menu"]').filter(visible).filter(b=>/^季(?:\s*\d+)?$/.test(clean(b.textContent)));
      if(seasonButtons.length!==1)throw new Error('找不到唯一的“季”筛选按钮，保留订阅');
      const seasonButton=seasonButtons[0];workerGuard(ctx);seasonButton.click();
      const menu=await wait(ctx,()=>{const e=document.getElementById(seasonButton.getAttribute('aria-owns'))||all('.v-menu.v-overlay--active').filter(visible)[0];return visible(e)?e:null;},30000,'季数筛选菜单');
      const chips=()=>all('.filter-chip',menu);
      if(chips().filter(e=>clean(e.textContent)===seasonLabel).length!==1)throw new Error(`季菜单没有唯一的整季选项 ${seasonLabel}`);
      for(const chip of chips()){
        const wanted=clean(chip.textContent)===seasonLabel;
        if(chip.classList.contains('v-chip--selected')!==wanted){workerGuard(ctx);chip.click();await tick(ctx,100);}
      }
      const chosen=chips().filter(e=>e.classList.contains('v-chip--selected')).map(e=>clean(e.textContent));
      if(chosen.length!==1||chosen[0]!==seasonLabel)throw new Error(`季筛选核对失败，必须仅选中 ${seasonLabel}`);
      if(seasonButton.getAttribute('aria-expanded')==='true')seasonButton.click();await tick(ctx);
      workerState(ctx,{status:'searching',detail:`核对同名第 ${job.season} 季的所有资源及其他来源`});
      const resources=await scanResources(ctx,job),best=C.chooseResource(resources,{...job,sites:selected});
      const item=await findResource(ctx,best);workerGuard(ctx);item.click();
      const confirm=await wait(ctx,()=>dialog('确认下载'),30000,'下载确认窗口');
      const subtitle=clean(confirm.querySelector('.v-card-subtitle')?.textContent);
      const season=subtitle.match(/\bS(\d+)(?:\b|\s)/i);
      if(!C.normalizeTitle(subtitle).startsWith(C.normalizeTitle(`${best.site} - ${job.name} (${job.year}) `))||Number(season?.[1])!==job.season)throw new Error('下载确认窗口与目标剧集、年份、季数或站点不符');
      const resourceTitle=clean(confirm.querySelector('.v-list-item-title span')?.textContent)||best.resourceTitle;
      const start=actionButton(confirm,'开始下载');
      const response=observeResponse(ctx,'POST',/\/download(?:\/add)?\/?$/);
      try{
        workerState(ctx,{status:'download-submitting',detail:'即将提交下载；若中止，不会自动重复下载',resourceTitle,site:best.site,seeders:best.seeders});
        workerGuard(ctx);start.click();
      const result=await wait(ctx,()=>response.get(),90000,'下载请求响应');
        if(!result.ok){if(result.success===false)workerState(ctx,{status:'failed',detail:'站点明确返回下载失败'});throw new Error(`下载未成功：${result.message}（${result.status}）`);}
        workerState(ctx,{status:'download-confirmed',detail:'站点已确认下载任务提交成功',resourceTitle,site:best.site,seeders:best.seeders});
        for(const d of all('.v-dialog.v-overlay--active').filter(visible).filter(e=>e.querySelector('.more-sources-content')))closeDialog(d);
        // 不把下载任务已提交描述为文件已下载完成。
        return ctx.state;
      }finally{response.close();}
    }
    channel.onmessage=async({data:m})=>{
      if(!m||m.from===id)return;
      if(m.type==='ping'&&ready&&!worker&&isWorkerPage())send('ready',{ready:true,to:m.from});
      if(m.type==='ready'&&(!m.to||m.to===id)){if(m.ready)peers.set(m.from,Date.now());else peers.delete(m.from);}
      if(m.type==='stop'){
        if(worker?.run===m.run)worker.stopped=true;
        if(active?.report.id===m.run){h.stop(m.reason||'资源页手动停止');waiting?.reject(new Error(m.reason||'已停止'));}
      }
      if(m.type==='result'&&m.to===id&&waiting?.jobId===m.jobId){const pending=waiting;waiting=null;m.ok?pending.resolve(m.state):pending.reject(new Error(m.error));}
      if(m.type==='alive'&&m.to===id&&waiting?.jobId===m.jobId)waiting.lastAlive=Date.now();
      if(m.type==='progress'&&m.to===id&&active?.current&&active.jobId===m.jobId){Object.assign(active.current,{detail:m.state.detail,...Object.fromEntries(['resourceTitle','site','seeders'].filter(k=>m.state[k]!==undefined).map(k=>[k,m.state[k]]))});saveReport();h.say(`${active.current.name}：${m.state.detail}`);}
      if(m.type==='job'&&m.to===id&&ready&&!worker){
        const ctx={run:m.run,owner:m.from,jobId:m.jobId,state:{},stopped:false};worker=ctx;
        const workerHeartbeat=setInterval(()=>send('alive',{to:m.from,jobId:m.jobId}),2000);
        try{workerGuard(ctx);const state=await processResource(ctx,m.job);send('result',{to:m.from,jobId:m.jobId,ok:true,state});}
        catch(e){const uncertain=['download-submitting','download-confirmed'].includes(ctx.state.status);workerState(ctx,{status:uncertain?ctx.state.status:'failed',detail:e.message});
          if(!uncertain)for(const d of all('.v-dialog.v-overlay--active').filter(visible)){try{closeDialog(d);}catch{}}
          send('result',{to:m.from,jobId:m.jobId,ok:false,error:e.message,state:ctx.state});h.say(e.message);}
        finally{clearInterval(workerHeartbeat);worker=null;}
      }
    };
    async function workerFor(ctx){
      if(!companion||companion.closed)throw new Error('请先点击本面板“打开／绑定资源页”，然后回到电视剧页启动');
      peers.clear();send('ping');await h.pause(1200,ctx);
      if(peers.size!==1)throw new Error(peers.size?'连接了多个资源页，请只保留一个':'请先在另一个资源页展开面板，点击“连接此资源页”');
      return [...peers.keys()][0];
    }
    async function requestDownload(ctx,row,workerId){
      const jobId=crypto.randomUUID();active.jobId=jobId;row.jobId=jobId;
      write('job:'+jobId,{status:'queued',time:stamp()});saveReport();
      const promise=new Promise((resolve,reject)=>{waiting={jobId,resolve,reject,lastAlive:Date.now()};});
      send('job',{to:workerId,run:active.report.id,jobId,job:row});
      const abort=()=>{if(waiting?.jobId===jobId){waiting.reject(new Error('已停止；请核对资源页是否已提交下载'));waiting=null;}};
      ctx.controller.signal.addEventListener('abort',abort,{once:true});
      const timeout=setTimeout(()=>{if(waiting?.jobId===jobId){signalStop('资源页响应超时');waiting.reject(new Error('资源页响应超时，保留订阅'));waiting=null;}},12*60*1000);
      const watchdog=setInterval(()=>{
        if(waiting?.jobId!==jobId)return;
        const state=read('job:'+jobId);
        // 结果已写入共享记录时，即使跨页消息迟到也能取得准确结果。
        if(['failed','download-confirmed'].includes(state?.status)){
          const pending=waiting;waiting=null;
          state.status==='download-confirmed'?pending.resolve(state):pending.reject(new Error(state.detail||'资源步骤失败'));
          return;
        }
        if(companion.closed||Date.now()-waiting.lastAlive>LEASE_MS){const pending=waiting;waiting=null;const reason=companion.closed?'资源页已关闭，已停止':'资源页超过五分钟未响应，可能被浏览器暂停；已停止并保留订阅';signalStop(reason);pending.reject(new Error(reason));}
      },3000);
      try{return await promise;}finally{clearTimeout(timeout);clearInterval(watchdog);ctx.controller.signal.removeEventListener('abort',abort);}
    }
    function parseTarget(target){
      const values=all('.flex-shrink-0.text-subtitle-2',target.card).filter(visible).map(e=>clean(e.textContent)).filter(t=>/^\d+\s*\/\s*\d+$/.test(t));
      if(values.length!==1)throw new Error('没有唯一的已下载/总集数数字栏');
      return C.parseSubscription({index:target.index,name:target.name,year:clean(target.card.querySelector('.font-medium')?.textContent),progress:values[0]});
    }
    async function collect(ctx){
      const first=await h.settle(ctx),items=new Map(),max=first.rows[0].index;
      for(let index=max;index>=0;index--){
        h.guard(ctx);if(items.has(index))continue;
        const view=await h.locateIndex(ctx,index);if(!view)throw new Error(`预览未能读取第 ${index+1} 张卡片`);
        for(const target of view.rows){
          if(items.has(target.index))continue;
          let row={index:target.index,name:target.name,cardKey:target.key,time:stamp()};
          if(target.index<100){row.action='protected';row.status='受保护';row.detail='启动时页面前100项，不处理';}
          else if(active.report.skipNames.includes(skipName(target.name))){row.action='skip-name';row.status='跳过';row.detail='命中自定义跳过名单：不下载、不取消';}
          else try{row={...row,...parseTarget(target)};row.action=C.classify(row);row.status='待处理';row.detail='';}
          catch(e){row.action='invalid';row.status='跳过';row.detail=e.message;}
          items.set(target.index,row);
        }
        if(active){active.report.records=[...items.values()].sort((a,b)=>b.index-a.index);saveReport();}
        h.say(`正在建立启动清单：已读取 ${items.size}/${max+1} 项，保护前100项`);
      }
      if(items.size!==max+1)throw new Error('启动清单不完整');
      const top=await h.locateIndex(ctx,0);if(!top||top.target.key!==items.get(0).cardKey)throw new Error('扫描期间列表顺序发生变化');
      return [...items.values()].sort((a,b)=>b.index-a.index);
    }
    async function run(ctx,preview,oneOnly=false){
      await takeLock();h.guard(ctx);
      reconcile(read('report:'+read('latest')));
      const report={id:new Date().toISOString().replace(/[:.]/g,'-'),startedAt:stamp(),endedAt:'',status:preview?'预览中':'运行中',skipNames:skipNames(),records:[]};
      skipInput.disabled=true;
      active={ctx,report,current:null,jobId:null};saveReport();
      renderResults([]);
      const previousLedger=read('ledger')||{};
      write('control',{run:report.id,owner:id,active:true,time:Date.now()});
      heartbeat=setInterval(()=>{if(active&&!ctx.controller.signal.aborted)write('control',{run:report.id,owner:id,active:true,time:Date.now()});},2000);
      report.records=await collect(ctx);saveReport();
      const protectedKeys=new Set(report.records.filter(r=>r.index<100).map(r=>r.cardKey));
      if(preview){report.status='预览完成（未执行下载或取消）';return;}
      const actionable=report.records.filter(r=>['cancel-zero','download-then-cancel'].includes(r.action)&&(!previousLedger[r.key]||previousLedger[r.key].resolution==='cancel-only'));
      const needsWorker=(oneOnly?actionable.slice(0,1):actionable).some(r=>r.action==='download-then-cancel'&&!previousLedger[r.key]);
      const workerId=needsWorker?await workerFor(ctx):null;
      let lastIndex=Math.max(...report.records.map(r=>r.index)),attempted=false;
      for(const row of report.records){
        h.guard(ctx);active.current=row;active.jobId=null;
        if(!['cancel-zero','download-then-cancel'].includes(row.action)){
          if(row.status==='待处理')phase(row,'跳过',row.action==='complete'?'已下载集数等于总集数':'年份为2026或之后');continue;
        }
        if(oneOnly&&attempted)break;
        if(previousLedger[row.key]&&previousLedger[row.key].resolution!=='cancel-only'){phase(row,'待核对','此前已发起过该剧下载，请核对下载器后使用“处理待核对项”，避免重复下载');continue;}
        attempted=true;
        let view=await h.locateIndex(ctx,row.index);if(!view||view.target.key!==row.cardKey||protectedKeys.has(view.target.key)){phase(row,'保留订阅','目标位置变化或属于前100保护范围');continue;}
        const current=parseTarget(view.target);
        if(current.key!==row.key||C.classify(current)!==row.action||current.downloaded!==row.downloaded||current.total!==row.total){phase(row,'保留订阅','年份、季数或进度已变化，请重新预览');continue;}
        if(row.action==='download-then-cancel'&&previousLedger[row.key]?.resolution!=='cancel-only'){
          phase(row,'搜索中','在资源页搜索同名同季资源');
          try{
            const result=await requestDownload(ctx,row,workerId);
            Object.assign(row,{resourceTitle:result.resourceTitle,site:result.site,seeders:result.seeders});
            previousLedger[row.key]={run:report.id,jobId:row.jobId,status:'下载已提交，待取消订阅',time:stamp()};write('ledger',previousLedger);
            phase(row,'下载已提交','站点确认下载任务提交成功，准备核对订阅');
          }catch(e){
            const state=read('job:'+row.jobId);
            if(['download-submitting','download-confirmed'].includes(state?.status)){
              previousLedger[row.key]={run:report.id,jobId:row.jobId,status:state.status,time:stamp()};write('ledger',previousLedger);
              phase(row,'待核对',e.message+'；下载可能已提交，保留订阅');throw e;
            }
            if(ctx.controller.signal.aborted)throw e;
            phase(row,'保留订阅',e.message);
            if(state?.status==='failed')continue;
            throw new Error('资源步骤未完成，已停止，本项订阅保留。');
          }
          view=await h.locateIndex(ctx,row.index);
          if(!view||view.target.key!==row.cardKey){phase(row,'待核对','下载已提交，但原订阅位置变化；未取消');throw new Error(row.detail);}
        }
        h.guard(ctx);
        const checked=parseTarget(view.target);
        if(checked.index<100||protectedKeys.has(view.target.key)||checked.key!==row.key||C.classify(checked)!==row.action||checked.downloaded!==row.downloaded||checked.total!==row.total){phase(row,'保留订阅','取消前进度或目标变化');continue;}
        const target=view.target,before=view.rows.find(r=>r.index===target.index-1),after=view.rows.find(r=>r.index===target.index+1);
        if(!before||(target.index<lastIndex&&!after)||before.key===target.key||after?.key===target.key)throw new Error('无法明确核对目标相邻卡片');
        phase(row,'准备取消','核对完成，准备取消该订阅');h.setMenu(target.button);target.button.click();
        const item=await h.waitMenu(target,ctx),fresh=h.snapshot(view.g).find(r=>r.index===target.index);
        if(!fresh||fresh.key!==row.cardKey)throw new Error('菜单打开后目标发生变化');
        const final=parseTarget(fresh);
        if(C.classify(final)!==row.action||final.downloaded!==row.downloaded||final.total!==row.total){h.closeMenu();phase(row,'保留订阅','点击前进度变化');continue;}
        h.guard(ctx);h.setPending(target.name);ctx.confirmed=false;ctx.network=null;ctx.awaitingRequest=true;
        phase(row,'取消已提交','等待订阅列表确认');item.click();
        if(after)await h.waitRemovedMiddle(target,before,after,ctx);else await h.waitRemoved(target,before,view.g,view.s,ctx);
        h.setPending('');h.setMenu(null);h.increment();lastIndex--;
        phase(row,'完成',row.action==='cancel-zero'?'零下载订阅已取消':'下载任务已提交，订阅已取消');
        if(previousLedger[row.key]){previousLedger[row.key].status='下载已提交且订阅已取消';write('ledger',previousLedger);}
        await h.pause(700,ctx);
        if(oneOnly)break;
      }
      report.status=oneOnly?'单项试跑结束':'处理完成';
    }
    function finish(ctx){
      if(!ctx.maintenance)return;
      skipInput.disabled=false;
      if(!active){releaseLock?.();releaseLock=null;return;}
      const finishing=active;
      try{
      clearInterval(heartbeat);heartbeat=null;
      write('control',{run:active.report.id,owner:id,active:false,time:Date.now()});send('stop',{run:active.report.id,reason:'本轮已结束'});
      if(active.current&&ctx.controller.signal.aborted&&active.current.status!=='完成'){
        const row=active.current,state=row.jobId?read('job:'+row.jobId):null;
        if(['download-submitting','download-confirmed'].includes(state?.status)){
          const ledger=read('ledger')||{};ledger[row.key]={run:active.report.id,jobId:row.jobId,status:state.status,time:stamp()};write('ledger',ledger);
          Object.assign(row,{status:'待核对',detail:`${ctx.reason||'已停止'}；下载可能已提交，订阅未必已取消`,resourceTitle:state.resourceTitle,site:state.site,seeders:state.seeders});
        }else if(['取消已提交','准备取消'].includes(row.status))row.detail+='；停止时请核对结果';
      }
      for(const row of active.report.records){
        if(row.status==='待处理')row.status=active.report.status.startsWith('预览')?'仅预览':'未处理';
        if(row.status==='搜索中')row.status='已停止';
      }
      active.report.endedAt=stamp();saveReport();const report=active.report;active=null;
      exportReport('csv',report);
      }catch(e){
        const report=finishing.report;report.endedAt=stamp();report.status+='（浏览器存储失败）';active=null;
        exportReport('csv',report);h.say('已停止，浏览器存储失败；已尝试直接导出本轮 CSV：'+e.message);
      }finally{clearInterval(heartbeat);heartbeat=null;releaseLock?.();releaseLock=null;}
    }
    ui.querySelector('#stop').addEventListener('click',()=>signalStop('手动停止'));
    button('处理待核对项',()=>{
      if(active||worker){h.say('请先停止任务，再核对记录');return;}
      const ledger=read('ledger')||{},entries=Object.entries(ledger).filter(([,v])=>v.status!=='下载已提交且订阅已取消');
      if(!entries.length){h.say('没有待核对下载记录');return;}
      const old=ui.querySelector('#ledger-review');old?.remove();const box=document.createElement('div');box.id='ledger-review';
      for(const [key,value] of entries){const label=document.createElement('p');label.textContent=key+'：'+value.status;box.append(label);
        for(const [text,resolution] of [['已确认未下载：允许重试','retry'],['已确认下载成功：仅取消订阅','cancel-only']]){
          const b=document.createElement('button');b.textContent=text;b.onclick=()=>{
            if(!window.confirm(key+'\n'+text+'？请先在下载器中确认实际结果。'))return;
            const latest=read('ledger')||{};if(resolution==='retry')delete latest[key];else latest[key]={...value,resolution:'cancel-only'};
            write('ledger',latest);label.textContent=key+'：已记录你的核对结果';b.disabled=true;
          };box.append(b);
        }
      }ui.querySelector('#details').append(box);
    });
    const keydown=e=>{if(isWorkerPage()&&!e.repeat&&(e.key==='Escape'||(e.altKey&&e.shiftKey&&e.code==='KeyS'))){signalStop('资源页手动停止');h.say('已发送停止指令；处理记录将在订阅页导出。');}};
    const unload=()=>signalStop('页面关闭或刷新，任务中断');
    window.addEventListener('keydown',keydown,true);window.addEventListener('beforeunload',unload);
    const manual=e=>{if(worker&&e.isTrusted&&!e.composedPath().includes(h.host)){signalStop('资源页检测到手动操作，已停止');}};
    document.addEventListener('pointerdown',manual,true);
    if(isWorkerPage()){
      skipBox.hidden=true;
      openWorker.hidden=true;
      for(const selector of ['#mode','#start','#preview','#single','label[for="mode"]']){const e=ui.querySelector(selector);if(e)e.hidden=true;}
      h.say('资源页：展开面板可检查连接，从电视剧页启动维护。');
      if(window.name===RESOURCE_WINDOW){ready=true;connect.textContent='断开资源页';send('ready',{ready:true});h.say('资源页已连接。返回电视剧页启动；运行期间可浏览其他标签页。');}
    }else{connect.hidden=true;if(/^#\/subscribe\/tv(?:\/|\?|$)/.test(location.hash))h.say('先设置跳过名单并绑定资源页，再自由选择单项或批量运行。预览可选，前100项受保护。');}
    const wrap=(id,title,nodes,open=false)=>{const group=document.createElement('details');group.id=id;group.open=open;const summary=document.createElement('summary');summary.textContent=title;group.append(summary);for(const node of nodes)if(node)group.append(node);ui.querySelector('#details').append(group);return group;};
    const findButton=text=>all('button',ui).find(b=>b.textContent===text);
    wrap('setup-actions','1 · 设置范围与跳过名单',[ui.querySelector('label[for="mode"]'),ui.querySelector('#mode'),skipBox]);
    wrap('connection-actions','2 · 连接资源页',[openWorker,connect],true);
    wrap('run-actions','3 · 选择运行方式',[ui.querySelector('#preview'),ui.querySelector('#single'),ui.querySelector('#start'),ui.querySelector('#stop')],true);
    const list=document.createElement('ol');list.id='live-results';list.setAttribute('aria-live','polite');
    wrap('results-actions','执行结果（最近100条）',[list],true);
    wrap('report-actions','4 · 记录与导出',['查看最近记录','CSV 另存为（选择位置）','导出最近记录 CSV','导出最近记录 Markdown','处理待核对项'].map(findButton));
    wrap('advanced-actions','其他',[ui.querySelector('#remove')]);
    if(isWorkerPage()){ui.querySelector('#connection-actions').append(ui.querySelector('#stop'));for(const key of ['setup-actions','run-actions','results-actions'])ui.querySelector('#'+key).hidden=true;}
    try{renderResults(read('report:'+read('latest'))?.events||[]);}catch{}
    h.syncFold?.();
    return {run,finish,isWorkerPage,onStop:signalStop,destroy(){signalStop('脚本已移除');clearInterval(heartbeat);disposed=true;channel.close();window.removeEventListener('keydown',keydown,true);window.removeEventListener('beforeunload',unload);document.removeEventListener('pointerdown',manual,true);}};
  }};
})(globalThis);
