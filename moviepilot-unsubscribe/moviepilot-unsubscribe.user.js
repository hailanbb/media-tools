// ==UserScript==
// @name         MoviePilot 从底部逐个取消订阅
// @namespace    local.moviepilot.helpers
// @version      2.4.0
// @description  Alt+Shift+S 启动/停止，Esc 停止；安装后默认不运行。
// @match        https://moviepilot.example/*
// @noframes
// @grant        none
// @run-at       document-idle
// ==/UserScript==

(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.MPMaintenanceCore = factory();
})(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';

  const SEARCH_SITES = Object.freeze(['天空', '观众', '馒头']);
  const SITES = new Set(SEARCH_SITES);
  const clean = value => String(value == null ? '' : value).normalize('NFKC').replace(/\s+/gu, ' ').trim();

  function normalizeTitle(value) { return clean(value); }

  function sizeBytes(value) {
    const text = clean(value);
    const m = /^(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$/iu.exec(text);
    if (!m) return Infinity;
    const unit = m[2].toLowerCase();
    const base = /^k|^m|^g|^t/.test(unit) && /ib$/i.test(m[2]) ? 1024 : 1000;
    const power = {b:0,kb:1,mb:2,gb:3,tb:4,kib:1,mib:2,gib:3,tib:4}[unit];
    return Number(m[1]) * Math.pow(base, power);
  }

  function parseSubscription(input) {
    if (!input || !Number.isInteger(input.index) || input.index < 0) throw new Error('订阅 index 无效');
    const displayName = clean(input.name);
    const m = /^(.*?)\s*S(\d{1,3})$/iu.exec(displayName);
    if (!m || !clean(m[1])) throw new Error('订阅名称缺少有效季号 Sxx');
    const name = clean(m[1]);
    const season = Number(m[2]);
    const yearText = clean(input.year);
    if (!/^\d{4}$/u.test(yearText)) throw new Error('订阅年份无效');
    const year = Number(yearText);
    const progress = clean(input.progress);
    const p = /^(\d+)\s*\/\s*(\d+)$/u.exec(progress);
    if (!p) throw new Error('订阅进度无效');
    const downloaded = Number(p[1]), total = Number(p[2]);
    if (!Number.isSafeInteger(downloaded) || !Number.isSafeInteger(total) || total <= 0) {
      throw new Error('订阅进度数值无效');
    }
    return {index: input.index, displayName, name, season, year, downloaded, total,
      key: `${name}|S${String(season).padStart(2, '0')}|${year}`};
  }

  function classify(row) {
    if (row.index < 100) return 'protected';
    if (row.year >= 2026) return 'skip-year';
    if (row.downloaded === row.total) return 'complete';
    if (row.downloaded === 0) return 'cancel-zero';
    return 'download-then-cancel';
  }

  function chooseResource(list, job) {
    const allowedSites=Array.isArray(job?.sites)?new Set(job.sites):SITES;
    const title = normalizeTitle(job && (job.name || job.displayName));
    const season = Number(job && job.season);
    const matching = (Array.isArray(list) ? list : []).filter(item => {
      const rawSeeders = item && item.seeders;
      const seeders = (typeof rawSeeders === 'number' || (typeof rawSeeders === 'string' && rawSeeders.trim() !== '')) ? Number(rawSeeders) : NaN;
      return item && allowedSites.has(item.site) && normalizeTitle(item.title) === title &&
        Number.isInteger(item.season) && item.season === season && Number.isFinite(seeders) &&
        Number.isInteger(seeders) && seeders >= 0;
    });
    const hasConflict = matching.some(item => item.total != null && item.total !== Number(job.total));
    const candidates = matching.filter(item => hasConflict ? item.total === Number(job.total) :
      (job.total == null || item.total == null || item.total === Number(job.total)));
    if (!candidates.length) throw new Error('已选站点没有匹配的整季资源');
    const wellSeeded=candidates.filter(item=>Number(item.seeders)>=5);
    const pool=wellSeeded.length?wellSeeded:candidates;
    return pool.map((item, index) => ({item, index})).sort((a, b) => {
      const seedDiff = Number(b.item.seeders) - Number(a.item.seeders);
      const sizeDiff = sizeBytes(a.item.size) - sizeBytes(b.item.size);
      return (wellSeeded.length?(sizeDiff||seedDiff):(seedDiff||sizeDiff)) || a.index - b.index;
    })[0].item;
  }

  const md = value => String(value == null ? '' : value).replace(/([\\`*_{}\[\]()#+.!|>])/g, '\\$1').replace(/\r?\n/gu, ' ');
  const actionLabel=value=>({'skip-name':'自定义名单跳过',protected:'保护前100项',invalid:'信息无法识别','skip-year':'跳过2026年及之后',complete:'集数相等','cancel-zero':'零下载取消订阅','download-then-cancel':'搜索下载后取消订阅'}[value]||value);
  function reportMarkdown(report) {
    const r = report || {}, records = Array.isArray(r.records) ? r.records : [];
    const lines = ['# MoviePilot 订阅维护记录', '', `- 记录 ID：${md(r.id)}`, `- 开始：${md(r.startedAt)}`, `- 结束：${md(r.endedAt)}`, `- 状态：${md(r.status)}`, '', '启动时前 100 项（第 1–100 张）受保护，未执行取消或下载操作。', '', '| 原始序号 | 名称 | 季 | 年 | 已下载/总数 | 动作 | 状态 | 详情 | 资源 | 站点 | 做种数 | 时间 |', '|---:|---|---:|---:|---:|---|---|---|---|---|---:|---|'];
    for (const x of records) lines.push(`| ${md(x.index+1)} | ${md(x.name)} | ${md(x.season)} | ${md(x.year)} | ${md(x.downloaded==null?'':`${x.downloaded}/${x.total}`)} | ${md(actionLabel(x.action))} | ${md(x.status)} | ${md(x.detail)} | ${md(x.resourceTitle)} | ${md(x.site)} | ${md(x.seeders)} | ${md(x.time)} |`);
    return lines.join('\n');
  }

  function csvCell(value) {
    let s = String(value == null ? '' : value);
    if (/^[\s\t]*[=+\-@]/u.test(s)) s = `'${s}`;
    return /[",\r\n]/u.test(s) ? `"${s.replace(/"/gu, '""')}"` : s;
  }
  function reportCSV(report) {
    const r = report || {}, rows = [['记录ID','开始时间','结束时间','任务状态','原始序号','名称','季','年','已下载','总数','动作','条目状态','详情','资源标题','站点','做种数','时间']];
    for (const x of Array.isArray(r.records) ? r.records : []) rows.push([r.id,r.startedAt,r.endedAt,r.status,x.index+1,x.name,x.season,x.year,x.downloaded,x.total,actionLabel(x.action),x.status,x.detail,x.resourceTitle,x.site,x.seeders,x.time]);
    if(rows.length===1)rows.push([r.id,r.startedAt,r.endedAt,r.status,'','','','','','','','未处理','尚未完成清单读取','','','','']);
    return '\uFEFF' + rows.map(row => row.map(csvCell).join(',')).join('\r\n') + '\r\n';
  }

  return {SEARCH_SITES, normalizeTitle, parseSubscription, classify, chooseResource, reportMarkdown, reportCSV};
});


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


(() => {
  'use strict';
  const HOST = 'moviepilot.example';
  const SLOT = '__moviepilotBottomUnsubscribe';
  if (location.host !== HOST) return;
  if (window[SLOT]) { window[SLOT].show(); return; }
  const routeOK = () => /^#\/subscribe\/(movie|tv)\/?(?:\?.*)?$/.test(location.hash);
  const clean = s => String(s || '').replace(/\s+/g, ' ').trim();
  const visible = e => !!e?.isConnected && e.getClientRects().length > 0 &&
    getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none';
  const all = (s, root = document) => Array.from(root.querySelectorAll(s));
  let run = null, busy = false, count = 0, pending = '', lastMenu = null, lastStop = '', maintenance = null;
  const listeners = [];
  const host = document.createElement('div');
  host.id = 'moviepilot-bottom-unsubscribe';
  host.style.cssText = 'position:fixed;bottom:12px;left:8px;width:224px;z-index:2147483647';
  const ui = host.attachShadow({mode:'open'});
  ui.innerHTML = `<style>
    :host{font:14px/1.6 system-ui,sans-serif;color:#eef2ff}
    section{background:#172033;padding:10px;border:1px solid #64748b;border-radius:12px;box-shadow:0 6px 24px #0005}
    [hidden]{display:none!important}#details{max-height:55vh;overflow:auto}#fold{display:block;width:100%;text-align:left;margin:0;background:#172033;color:#eef2ff}#status{max-height:100px;overflow:auto}
    details{border-top:1px solid #475569;margin-top:8px;padding-top:5px}summary{cursor:pointer;font-weight:600}#live-results{max-height:160px;overflow:auto;padding-left:20px;margin:6px 0}#live-results li{margin-bottom:6px;overflow-wrap:anywhere;font-size:12px}#stop{width:100%}
    strong{font-size:15px}p{margin:8px 0;overflow-wrap:anywhere;white-space:pre-line}
    small{color:#cbd5e1}button{font:inherit;border:0;border-radius:6px;padding:6px 10px;margin:4px 4px 0 0;cursor:pointer;background:#dbeafe;color:#172033}
    button:disabled{opacity:.5;cursor:default}#stop{background:#fecaca}
    select{font:inherit;width:100%;margin:8px 0;padding:5px;border-radius:5px;background:#eef2ff;color:#172033}
  </style><section><button id="fold">取消订阅 v2.4.0 · 未运行 ▴</button><div id="details" hidden><strong>从底部逐个取消订阅</strong>
  <p id="status"></p><small>Alt+Shift+S 启动／停止 · Esc 停止<br>只处理当前分类和筛选范围</small><br>
  <label for="mode">取消范围</label><select id="mode"><option value="all">全部订阅（当前分类）</option><option value="zero">电视剧：仅进度 0 / 总数</option></select>
  <button id="preview">检查下一目标（不取消）</button><button id="start">开始取消</button><button id="single">单项试跑</button><button id="stop">停止</button>
  <button id="remove">移除脚本</button></div></section>`;
  document.body.append(host);
  const status = ui.querySelector('#status');
  function syncFold(){const expanded=!ui.querySelector('#details').hidden;const fold=ui.querySelector('#fold');fold.setAttribute('aria-expanded',String(expanded));fold.textContent=`${expanded?'收起':'展开'} · 订阅助手 v2.4.0 · ${busy?'运行中':'已停止'} · ${count}项`;}
  function say(s) {
    status.textContent = `已核对完成：${count} 项\n${s}`;
    syncFold();
  }
  function controls() {
    ui.querySelector('#start').disabled = busy;
    ui.querySelector('#preview').disabled = busy;
    ui.querySelector('#single').disabled = busy;
    ui.querySelector('#mode').disabled = busy;
  }
  function listen(target, name, fn, options) {
    target.addEventListener(name, fn, options);
    listeners.push(() => target.removeEventListener(name, fn, options));
  }
  function closeMenu() {
    const b = lastMenu;
    lastMenu = null;
    if (b?.isConnected && b.getAttribute('aria-expanded') === 'true') b.click();
  }
  function stop(reason = '已停止') {
    if (!run && lastStop) return;
    if (run?.controller.signal.aborted) return;
    lastStop = reason;
    if (run) { run.reason = reason; run.controller.abort(); }
    if(run?.maintenance) maintenance?.onStop(reason);
    closeMenu();
    say(reason + (pending ? `\n“${pending}”可能已提交，请先核对；停止不能撤回已发出的请求。` : ''));
  }
  // 只观察正常菜单点击产生的取消请求，不读取凭据、不主动调用接口。
  function observeCancel(ctx) {
    const xhr = window.XMLHttpRequest?.prototype;
    const originalOpen = xhr?.open, originalSend = xhr?.send, originalFetch = window.fetch;
    const requests = new WeakMap(), removers = [];
    function matches(method,url) {
      try { const u = new URL(url,ctx.url); return String(method).toUpperCase() === 'DELETE' &&
        u.origin === new URL(ctx.url).origin && /\/subscribe\/\d+\/?$/.test(u.pathname); } catch { return false; }
    }
    function claim() {
      if (run !== ctx || ctx.controller.signal.aborted || !ctx.awaitingRequest) return false;
      ctx.network = {state:'waiting'}; ctx.awaitingRequest = false; return true;
    }
    function result(status,body) {
      if (run !== ctx || ctx.controller.signal.aborted) return;
      if (status < 200 || status >= 300 || body?.success === false) {
        ctx.network = {state:'error',message:`站点返回 ${status}：${clean(body?.message || body?.detail || '取消请求未成功').slice(0,160)}`};
      } else if (body?.success === true) ctx.network = {state:'success'};
      else ctx.network = {state:'unknown'};
    }
    function open(method,url,...rest) {
      requests.set(this,matches(method,url)); return originalOpen.call(this,method,url,...rest);
    }
    function send(...args) {
      if (requests.get(this) && claim()) {
        const done = () => {
          this.removeEventListener('loadend',done);
          let body;
          try { body = this.responseType === 'json' ? this.response : JSON.parse(this.responseText); } catch {}
          result(this.status,body);
        };
        this.addEventListener('loadend',done);
        removers.push(() => this.removeEventListener('loadend',done));
      }
      return originalSend.apply(this,args);
    }
    async function fetchWrapper(input,options) {
      const watched = matches(options?.method || input?.method || 'GET', typeof input === 'string' || input instanceof URL ? String(input) : input?.url) && claim();
      try {
        const response = await originalFetch.call(this,input,options);
        if (watched) response.clone().json().then(body => result(response.status,body),() => result(response.status));
        return response;
      } catch (e) { if (watched) result(0,{message:e.message}); throw e; }
    }
    if (xhr) { xhr.open = open; xhr.send = send; }
    if (originalFetch) window.fetch = fetchWrapper;
    return () => {
      removers.forEach(off => off());
      if (xhr?.open === open) xhr.open = originalOpen;
      if (xhr?.send === send) xhr.send = originalSend;
      if (window.fetch === fetchWrapper) window.fetch = originalFetch;
    };
  }
  function guard(ctx) {
    if (run !== ctx || ctx.controller.signal.aborted) throw new Error(ctx.reason || '已停止');
    if (location.href !== ctx.url || !routeOK()) throw new Error('页面已切换');
    if (document.hidden && !ctx.maintenance) throw new Error('页面已进入后台');
  }
  async function pause(ms, ctx) {
    guard(ctx);
    await new Promise((resolve, reject) => {
      const signal = ctx.controller.signal;
      const onAbort = () => { clearTimeout(timer); reject(new Error(ctx.reason || '已停止')); };
      const timer = setTimeout(() => { signal.removeEventListener('abort', onAbort); resolve(); }, ms);
      signal.addEventListener('abort', onAbort, {once:true});
    });
    guard(ctx);
  }
  function grid() {
    const grids = all('.v-window-item--active .progressive-card-grid').filter(visible);
    if (grids.length !== 1) throw new Error('未找到唯一订阅列表，请打开“我的订阅”');
    return grids[0];
  }
  function snapshot(g) {
    return all('.progressive-card-grid__item[data-progressive-grid-index]', g).map(item => {
      const card = item.querySelector('.subscribe-card');
      const button = card?.querySelector('button[aria-haspopup="menu"]');
      const name = clean(card?.querySelector('.font-bold')?.textContent);
      const year = clean(card?.querySelector('.font-medium')?.textContent);
      return {item, card, button, name, key:year + '|' + name,
        index:Number(item.getAttribute('data-progressive-grid-index')),
        menu:button?.getAttribute('aria-owns') || button?.getAttribute('aria-controls')};
    }).filter(x => visible(x.card) && x.button && x.name && x.menu && Number.isInteger(x.index))
      .sort((a,b) => b.index - a.index);
  }
  function zeroProgress(card) {
    const values = all('.flex-shrink-0.text-subtitle-2',card).filter(visible)
      .map(e => clean(e.textContent).match(/^(\d+)\s*\/\s*(\d+)$/)).filter(Boolean);
    return values.length === 1 && Number(values[0][1]) === 0 && Number(values[0][2]) > 0;
  }
  // 使用当前可见窗口的实际行高定位序号，不要求把所有卡片同时留在 DOM 中。
  async function locateIndex(ctx,index,attempts=48) {
    for (let n=0;n<attempts;n++) {
      guard(ctx); noDialog();
      const grids=all('.v-window-item--active .progressive-card-grid').filter(visible);
      if (grids.length!==1) { await pause(250,ctx); continue; }
      const g=grids[0],s=scroller(g),rows=snapshot(g),target=rows.find(x=>x.index===index);
      if (target) {
        const r=target.item.getBoundingClientRect();
        const top=s===document.scrollingElement?0:s.getBoundingClientRect().top;
        if (r.top<top+100 || r.bottom>top+s.clientHeight-40) {
          target.item.scrollIntoView({block:'center',behavior:'instant'});
          await pause(250,ctx);
          if(g.isConnected) {
            const fresh=snapshot(g),resolved=fresh.find(x=>x.index===index);
            if(resolved)return {g,s,rows:fresh,target:resolved};
          }
          continue;
        }
        return {g,s,rows,target};
      }
      const items=all('.progressive-card-grid__item[data-progressive-grid-index]',g);
      const anchor=items.sort((a,b)=>Math.abs(Number(a.dataset.progressiveGridIndex)-index)-Math.abs(Number(b.dataset.progressiveGridIndex)-index))[0];
      if (anchor) {
        const style=getComputedStyle(g.querySelector('.progressive-card-grid__grid')||g);
        const columns=Math.max(1,(style.gridTemplateColumns||'').split(/\s+/).filter(Boolean).length);
        const rect=anchor.getBoundingClientRect(),anchorIndex=Number(anchor.dataset.progressiveGridIndex);
        const delta=(Math.floor(index/columns)-Math.floor(anchorIndex/columns))*(rect.height+(parseFloat(style.rowGap)||16));
        const top=s===document.scrollingElement?0:s.getBoundingClientRect().top;
        s.scrollTo({top:Math.max(0,s.scrollTop+rect.top-top+delta-s.clientHeight/2),behavior:'instant'});
      }
      await pause(250,ctx);
    }
    return null;
  }
  async function waitRemovedMiddle(target,before,after,ctx) {
    let stable=0;
    for(let n=0;n<40;n++) {
      guard(ctx);
      if(ctx.network?.state==='error')throw new Error(ctx.network.message);
      confirmIfKnown(target,ctx);
      if(dialogs().length) { await pause(250,ctx);continue; }
      const view=await locateIndex(ctx,target.index,2);
      const current=view?.rows.find(x=>x.index===target.index);
      const upper=view?.rows.find(x=>x.index===target.index-1);
      const confirmed=current?.key===after.key && (!before || upper?.key===before.key) &&
        current.key!==target.key && (!ctx.network || ctx.network.state==='success');
      stable=confirmed?stable+1:0;
      if(stable>=5)return;
      await pause(250,ctx);
    }
    throw new Error('未能核对筛选目标取消后的相邻卡片，已停止，不会重试');
  }
  async function runZero(ctx,preview) {
    const initial=await settle(ctx);
    let lastIndex=initial.rows[0].index,skipped=0,removed=0;
    for(let index=lastIndex;index>=0;index--) {
      say(`正在自下往上检查第 ${index+1} 项；已跳过 ${skipped} 项`);
      const view=await locateIndex(ctx,index);
      if(!view)throw new Error(`无法定位第 ${index+1} 项，已停止`);
      const {g,s,target}=view;
      if(!zeroProgress(target.card)) { skipped++;continue; }
      const before=view.rows.find(x=>x.index===index-1),after=view.rows.find(x=>x.index===index+1);
      if((index>0&&!before)||(index<lastIndex&&!after)||before?.key===target.key||after?.key===target.key) {
        throw new Error('筛选目标的相邻卡片无法明确区分，已停止');
      }
      say(`${preview?'检查':'即将取消'}：${target.name}（进度首数字为 0）`);
      await pause(800,ctx);
      const current=snapshot(g).find(x=>x.index===index);
      if(!current||current.key!==target.key||current.menu!==target.menu)throw new Error('筛选目标已变化，已停止');
      if(!zeroProgress(current.card)){skipped++;continue;}
      guard(ctx);noDialog();lastMenu=current.button;lastMenu.click();
      const item=await waitMenu(current,ctx);
      const fresh=snapshot(g),recheck=fresh.find(x=>x.index===index);
      if(!recheck||recheck.key!==current.key||recheck.menu!==current.menu||
        (before&&fresh.find(x=>x.index===index-1)?.key!==before.key)||
        (after&&fresh.find(x=>x.index===index+1)?.key!==after.key))throw new Error('筛选目标或相邻卡片已变化，已停止');
      if(!zeroProgress(recheck.card)){closeMenu();skipped++;continue;}
      if(preview) {
        closeMenu();say(`检查通过：${target.name}，进度首数字为 0；已找到取消菜单。\n未取消任何订阅。`);return;
      }
      guard(ctx);pending=target.name;ctx.confirmed=false;ctx.network=null;ctx.awaitingRequest=true;
      item.click();
      if(after)await waitRemovedMiddle(current,before,after,ctx);
      else await waitRemoved(current,before,g,s,ctx);
      pending='';lastMenu=null;count++;removed++;lastIndex--;
      say(`已核对取消：${target.name}；已跳过 ${skipped} 项`);
      await pause(900,ctx);
    }
    say(`零进度筛选完成：取消 ${removed} 项，跳过 ${skipped} 项。`);
  }
  function scroller(g) {
    for (let e = g.parentElement; e && e !== document.body; e = e.parentElement) {
      if (/(auto|scroll)/.test(getComputedStyle(e).overflowY) && e.scrollHeight > e.clientHeight + 4) return e;
    }
    return document.scrollingElement;
  }
  function bottom(s) { return s.scrollHeight - s.clientHeight - s.scrollTop <= 4; }
  function completeTail(g) {
    const track = g.querySelector('.progressive-card-grid__track');
    const tail = track?.lastElementChild;
    return !tail?.classList.contains('progressive-card-grid__spacer') || tail.getBoundingClientRect().height < 1;
  }
  const dialogs = () => all('.v-dialog.v-overlay--active,[role="dialog"],[role="alertdialog"]').filter(visible)
    .filter((e, i, a) => !a.some(other => other !== e && other.contains(e)));
  function noDialog() { if (dialogs().length) throw new Error('有弹窗未关闭，请处理后再启动'); }
  async function settle(ctx) {
    let previous = '', stable = 0;
    for (let i = 0; i < 50; i++) {
      guard(ctx); noDialog();
      const g = grid(), s = scroller(g);
      s.scrollTo({top:s.scrollHeight, behavior:'instant'});
      await pause(250, ctx);
      const rows = snapshot(g);
      const sig = JSON.stringify(rows.slice(0,2).map(x => [x.index,x.key,x.menu])) + ':' + s.scrollHeight;
      stable = bottom(s) && completeTail(g) && rows.length && sig === previous ? stable + 1 : 0;
      previous = sig;
      if (stable >= 8) return {g, s, rows};
    }
    throw new Error('列表未能稳定到达底部，或当前没有可处理的订阅');
  }
  async function waitMenu(target, ctx) {
    for (let i = 0; i < 24; i++) {
      guard(ctx); noDialog();
      const menu = document.getElementById(target.menu);
      if (visible(menu)) {
        const entries = all('.v-list-item', menu).filter(e => visible(e) &&
          clean(e.querySelector('.v-list-item-title')?.textContent) === '取消订阅');
        if (entries.length === 1) return entries[0];
      }
      await pause(125, ctx);
    }
    throw new Error('未找到该卡片唯一的“取消订阅”菜单项');
  }
  function confirmIfKnown(target, ctx) {
    const ds = dialogs();
    if (!ds.length) return;
    if (ctx.confirmed || ds.length !== 1) return;
    const d = ds[0], text = clean(d.textContent);
    const headings = all('.v-card-title,.v-toolbar-title__placeholder,h1,h2,h3,[role="heading"]',d)
      .filter(visible).map(e => clean(e.textContent));
    // 未实际验证过的弹窗保守匹配：必须同时出现操作和目标片名，不选择删除文件等附加项。
    if (!headings.some(t => t.includes('取消订阅')) || !text.includes(target.name) ||
        /删除.{0,10}(文件|下载|媒体库)|清空|批量|全部/.test(text) ||
        d.querySelector('input,select,[role="checkbox"]')) {
      throw new Error('出现未识别的确认框，已停止；请手动核对弹窗');
    }
    const buttons = all('button', d).filter(b => visible(b) && !b.disabled &&
      /^(确定|确认|取消订阅|确认取消|确认取消订阅)$/.test(clean(b.textContent)));
    if (buttons.length !== 1) throw new Error('确认按钮不唯一，已停止');
    guard(ctx);
    ctx.confirmed = true;
    buttons[0].click();
  }
  async function waitRemoved(target, predecessor, g, s, ctx) {
    let stable = 0;
    for (let i = 0; i < 240; i++) {
      guard(ctx);
      if (ctx.network?.state === 'error') throw new Error(ctx.network.message);
      confirmIfKnown(target, ctx);
      if (!g.isConnected && target.index === 0 && ctx.network?.state === 'success' &&
          !document.querySelector('.v-window-item--active .subscribe-card')) {
        if (++stable >= 8) return;
        await pause(125,ctx); continue;
      }
      if (!g.isConnected) {
        const replacement = all('.v-window-item--active .progressive-card-grid').filter(visible);
        if (replacement.length !== 1) { stable=0; await pause(125,ctx); continue; }
        g = replacement[0]; s = scroller(g);
      }
      // 列表刷新可能重建节点或重置滚动位置；重新触底后再核对。
      if (!dialogs().length) s.scrollTo({top:s.scrollHeight,behavior:'instant'});
      // 虚拟列表会卸载卡片，不能仅用节点消失判断成功。
      const rows = snapshot(g), last = rows[0];
      const dialogOpen = dialogs().length > 0;
      const stillOld = rows.some(x => x.index === target.index);
      let removed = false;
      if (target.index === 0) {
        removed = !rows.length && !g.querySelector('.subscribe-card') && !target.card.isConnected;
      } else if (last && last.index === target.index - 1 && last.key === predecessor.key && !stillOld) {
        removed = true;
      } else if (last && completeTail(g) && bottom(s) && (last.index > target.index || last.index < target.index - 1)) {
        throw new Error('列表顺序或数量发生额外变化，已停止');
      }
      const requestSettled = !ctx.network || ctx.network.state === 'success';
      stable = removed && requestSettled && !dialogOpen && completeTail(g) && bottom(s) ? stable + 1 : 0;
      if (stable >= 5) return;
      await pause(125, ctx);
    }
    const detail = ctx.network?.state === 'success' ? '站点已返回成功，但列表变化未能核对' :
      ctx.network?.state === 'waiting' ? '站点请求仍未完成' : ctx.network?.state === 'unknown' ? '站点响应格式无法识别' : '未捕获到取消请求，菜单点击可能未生效';
    throw new Error(`30 秒内未能核对取消结果：${detail}。已停止，不会自动重试`);
  }
  async function start(preview = false, singleOnly = false) {
    if (busy) { say('任务正在运行，请查看当前进度；需要结束时点击“停止”。');return; }
    if (!routeOK()) { say('请先打开电影或电视剧的“我的订阅”页面'); return; }
    if (document.hidden) { say('请切回本电视剧订阅页，再点击启动按钮。');return; }
    const maintenanceOnly=ui.querySelector('#mode').value==='maintenance';
    const zeroOnly=ui.querySelector('#mode').value==='zero';
    if((zeroOnly||maintenanceOnly)&&!/^#\/subscribe\/tv\/?(?:\?.*)?$/.test(location.hash)) {
      say('“仅进度 0 / 总数”选项只用于电视剧，请切换到电视剧页面');return;
    }
    busy = true; controls();
    lastStop = '';
    const ctx = {url:location.href,controller:new AbortController(),reason:'',confirmed:false,maintenance:maintenanceOnly,singleOnly};
    run = ctx;
    let unobserve = () => {};
    try {
      if (pending && !preview) throw new Error('上次结果尚待核对。请刷新页面后重新装载脚本，再继续');
      noDialog();
      if (all('.v-menu.v-overlay--active').some(visible)) throw new Error('请先关闭当前菜单');
      say('正在定位真正的列表底部……');
      if (!preview) unobserve = observeCancel(ctx);
      if (maintenanceOnly) { await maintenance.run(ctx,preview,ctx.singleOnly);return; }
      if (zeroOnly) { await runZero(ctx,preview);return; }
      while (true) {
        const {g,s,rows} = await settle(ctx);
        const target = rows[0], predecessor = rows[1];
        if (preview) {
          guard(ctx); lastMenu = target.button; lastMenu.click();
          await waitMenu(target,ctx); await pause(300,ctx); closeMenu();
          say(`检查通过：第 ${target.index + 1} 项“${target.name}”，已找到取消菜单。\n未取消任何订阅。`); break;
        }
        if (target.index > 0 && (!predecessor || predecessor.index !== target.index - 1 || predecessor.key === target.key)) {
          throw new Error('末尾相邻卡片无法明确区分，已停止');
        }
        say(`即将取消第 ${target.index + 1} 项：${target.name}\n按 Esc 可停止`);
        await pause(800,ctx);
        const current = snapshot(g)[0];
        if (!bottom(s) || !current || current.index !== target.index || current.key !== target.key || current.menu !== target.menu) {
          throw new Error('目标卡片已变化，已停止');
        }
        guard(ctx); noDialog();
        lastMenu = current.button;
        lastMenu.click();
        const item = await waitMenu(current,ctx);
        const recheck = snapshot(g)[0];
        if (!recheck || recheck.key !== current.key || recheck.index !== current.index || recheck.menu !== current.menu) {
          throw new Error('打开菜单后目标发生变化，已停止');
        }
        guard(ctx);
        pending = target.name; ctx.confirmed = false; ctx.network = null; ctx.awaitingRequest = true;
        say(`正在等待取消结果：${target.name}`);
        item.click();
        await waitRemoved(current,predecessor,g,s,ctx);
        pending = ''; lastMenu = null; count++;
        say(`已核对取消：${target.name}`);
        if (target.index === 0) { say('当前列表已处理完成'); break; }
        await pause(900,ctx);
      }
    } catch (e) {
      stop(e.message || String(e));
    } finally {
      unobserve();
      maintenance?.finish(ctx);
      if (run === ctx) run = null;
      busy = false; closeMenu(); controls();
      syncFold();
    }
  }
  ui.querySelector('#fold').onclick = () => {
    const details = ui.querySelector('#details'); details.hidden = !details.hidden;
    syncFold();
  };
  function remove() {
    stop('脚本已移除');
    maintenance?.destroy();
    listeners.forEach(off => off());
    host.remove(); delete window[SLOT];
  }
  ui.querySelector('#start').onclick = () => start();
  ui.querySelector('#preview').onclick = () => start(true);
  ui.querySelector('#single').onclick = () => {
    if (ui.querySelector('#mode').value !== 'maintenance') { say('“单项试跑”只用于电视剧维护模式。'); return; }
    start(false, true);
  };
  ui.querySelector('#stop').onclick = () => stop();
  ui.querySelector('#remove').onclick = remove;
  listen(window,'keydown',e => {
    if(maintenance?.isWorkerPage())return;
    if (e.repeat) return;
    if (e.key === 'Escape' && busy) { stop(); return; }
    if (e.altKey && e.shiftKey && e.code === 'KeyS') {
      const editing = e.composedPath().some(n => n instanceof Element &&
        (n.matches('input,textarea,select') || n.isContentEditable));
      if (editing && !busy) return;
      e.preventDefault(); busy ? stop() : start();
    } else if (busy && ['PageUp','PageDown','Home','End','ArrowUp','ArrowDown',' '].includes(e.key)) {
      stop('检测到手动滚动，已停止');
    }
  },true);
  listen(window,'hashchange',() => {if(!maintenance?.isWorkerPage()||run)stop('页面已切换，已停止');});
  listen(document,'visibilitychange',() => { if (document.hidden && busy && !run?.maintenance) stop('页面进入后台，已停止'); });
  listen(window,'blur',() => { if (busy && !run?.maintenance) stop('页面失去焦点，已停止'); });
  for (const event of ['wheel','touchstart','pointerdown']) listen(document,event,e => {
    if (busy && e.isTrusted && !e.composedPath().includes(host)) stop('检测到手动操作，已停止');
  },{capture:true,passive:true});
  window[SLOT] = {show:() => { host.style.display = ''; },stop,remove};
  say('v2.4.0 已装载，尚未开始。维护模式支持单项试跑。');
  maintenance=window.MPMaintenanceRuntime?.create({host,ui,all,visible,clean,snapshot,settle,locateIndex,waitMenu,waitRemoved,waitRemovedMiddle,guard,pause,closeMenu,stop,say,syncFold,setMenu:v=>{lastMenu=v;},setPending:v=>{pending=v;},increment:()=>{count++;}});
})();
