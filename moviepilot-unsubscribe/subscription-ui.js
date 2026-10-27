// ==UserScript==
// @name         MoviePilot 从底部逐个取消订阅
// @namespace    local.moviepilot.helpers
// @version      2.4.0
// @description  Alt+Shift+S 启动/停止，Esc 停止；安装后默认不运行。
// @match        https://moviepilot.example/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

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
