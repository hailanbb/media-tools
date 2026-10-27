const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {parseHTML}=require('linkedom');
const core=require('../maintenance-core');
const source=fs.readFileSync(require('node:path').join(__dirname,'../maintenance-runtime.js'),'utf8');
async function scenario(success=true,more=false,stopBeforeDownload=false,filterFailure=false){
  const {window,document}=parseHTML('<html><body><input id="global-media-search"><main></main><aside><div id="details"><select id="mode"></select><button id="start"></button><button id="preview"></button><button id="stop"></button></div></aside></body></html>');
  const main=document.querySelector('main'),ui=document.querySelector('aside');
  const location={origin:'https://moviepilot.example',href:'https://moviepilot.example/#/resource',hash:'#/resource'};
  const storage=new Map(),messages=[],downloads=[];let seq=0,selected=[];
  const localStorage={getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,String(v))};
  localStorage.setItem('mp-tv-maintenance-v2:control',JSON.stringify({run:'test',owner:'owner',active:true,time:Date.now()}));
  class BC{constructor(){BC.instance=this;}postMessage(m){messages.push(m);if(stopBeforeDownload&&m.type==='progress'&&m.state?.status==='download-submitting')localStorage.setItem('mp-tv-maintenance-v2:control',JSON.stringify({run:'test',owner:'owner',active:false,time:Date.now()}));}close(){}}
  class XHR{constructor(){this.listeners={};this.status=200;this.responseType='';}open(method,url){assert.equal(method,'POST');assert.equal(url,'/api/v1/download/');}addEventListener(k,fn){this.listeners[k]=fn;}removeEventListener(k,fn){if(this.listeners[k]===fn)delete this.listeners[k];}send(){this.responseText=JSON.stringify({success,message:success?'':'模拟拒绝下载'});this.listeners.loadend?.();}}
  class KeyEvent extends window.Event{constructor(type,opts){super(type,opts);this.key=opts.key;this.code=opts.code;}}
  window.Element.prototype.getClientRects=function(){return this.isConnected?[{}]:[];};
  const scroller={scrollHeight:700,clientHeight:500,scrollTop:0,scrollTo({top}){this.scrollTop=Math.max(0,Math.min(200,top));}};
  Object.defineProperty(document,'scrollingElement',{value:scroller});
  Object.defineProperty(document,'hidden',{value:true});
  window.name='mp-tv-maintenance-resource-v2';window.focus=()=>{throw new Error('禁止抢焦点');};window.opener={focus(){throw new Error('禁止抢焦点');}};window.XMLHttpRequest=XHR;window.fetch=undefined;
  function dialog(title,contents){const d=document.createElement('div');d.className='v-dialog v-overlay--active';d.innerHTML=`<div class="v-card-title">${title}</div><button aria-label="关闭">×</button>${contents}`;d.querySelector('button').onclick=()=>d.remove();document.body.append(d);return d;}
  let seasonVerified=false;
  const candidates=[{title:'目标剧',season:1,site:'天空',n:999},{title:'目标剧',season:2,site:'天空',n:9},{title:'目标剧',season:2,site:'观众',n:5,size:'13.69 GB'},{title:'另一部',season:2,site:'天空',n:999},{title:'目标剧',season:2,site:'馒头',n:39,size:'60.96 GB'},{title:'目标剧',season:2,site:'馒头',n:39,size:'16.91 GB'},{title:'目标剧',season:2,site:'馒头',n:999,episode:true}];
  function confirmResource(r){const d=dialog('确认下载',`<div class="v-card-subtitle">${r.site} - ${r.title} (2025) S0${r.season}</div><div class="v-list-item-title"><span>release-${r.n}</span></div><button>开始下载</button>`);d.lastElementChild.onclick=()=>{downloads.push(r);const x=new window.XMLHttpRequest();x.open('POST','/api/v1/download/');x.send();d.remove();};}
  function renderResources(){
    main.innerHTML='<h2>资源搜索结果</h2><button aria-haspopup="menu" aria-expanded="false" aria-owns="season-menu">季 1</button>';
    const seasonButton=main.querySelector('button');
    seasonButton.onclick=()=>{
      if(seasonButton.getAttribute('aria-expanded')==='true'){const m=document.getElementById('season-menu');seasonVerified=[...m.querySelectorAll('.v-chip--selected')].map(e=>e.textContent).join(',')==='S02';m.remove();seasonButton.setAttribute('aria-expanded','false');return;}
      seasonButton.setAttribute('aria-expanded','true');const m=document.createElement('div');m.id='season-menu';m.className='v-menu v-overlay--active';
      m.innerHTML='<span class="filter-chip v-chip--selected">S01</span><span class="filter-chip">S02</span><span class="filter-chip">S02 E01</span>';
      m.querySelectorAll('.filter-chip').forEach(e=>e.onclick=()=>{if(!filterFailure)e.classList.toggle('v-chip--selected');});document.body.append(m);
    };
    candidates.forEach(r=>{
      const c=document.createElement('div');c.className='torrent-card';
      c.innerHTML=`<span class="text-h6 font-weight-bold">${r.title}</span><span class="chip-season">S0${r.season}${r.episode?' E01':''}</span><span class="font-weight-bold text-body-2">${r.site}</span><span><svg class="text-success"></svg>${r.n}</span><div class="text-subtitle-2" title="release-${r.n}">release-${r.n}</div><span class="bg-primary"><span class="v-chip__content">${r.size||''}</span></span>`;
      c.onclick=()=>{assert.ok(seasonVerified);confirmResource(r);};main.append(c);
      if(more&&r.n===9){const b=document.createElement('button');b.textContent='更多来源 (1)';b.onclick=e=>{e.stopPropagation();const d=dialog('其他来源','<div class="more-sources-content"><div class="v-list-item"><span class="text-body-2 font-weight-bold">观众</span><span class="chip-season">S02</span><span><svg class="text-success"></svg>45</span><span class="text-caption font-weight-bold text-primary">10 GB</span></div></div>');d.querySelector('.v-list-item').onclick=()=>confirmResource({...r,site:'观众',n:45});};c.append(b);}
    });
  }
  document.querySelector('input').addEventListener('keydown',()=>{
    location.hash='#/browse/media/search?title=目标剧';
    main.innerHTML='<div class="media-card"><h1 class="media-card-title">目标剧</h1><span class="font-semibold text-sm">2025</span><span class="v-chip">电视剧</span><button><svg><path d="M9.5 3A6.5"></path></svg></button></div>';
    main.querySelector('button').onclick=()=>{
      const d=dialog('选择站点','<div class="site-checkbox-wrapper site-selected"><span class="site-name">其他</span></div><div class="site-checkbox-wrapper"><span class="site-name">天空</span></div><div class="site-checkbox-wrapper"><span class="site-name">观众</span></div><div class="site-checkbox-wrapper site-selected"><span class="site-name">馒头</span></div><button>搜索</button>');
      d.querySelectorAll('.site-checkbox-wrapper').forEach(e=>e.onclick=()=>e.classList.toggle('site-selected'));
      d.lastElementChild.onclick=()=>{selected=[...d.querySelectorAll('.site-selected .site-name')].map(e=>e.textContent);d.remove();location.hash='#/resource';renderResources();};
    };
  });
  const nativeSetTimeout=setTimeout;
  const context=vm.createContext({window,document,location,localStorage,BroadcastChannel:BC,XMLHttpRequest:XHR,URL,Blob,crypto:{randomUUID:()=>`id${++seq}`},navigator:{locks:{}},MPMaintenanceCore:core,HTMLInputElement:window.HTMLInputElement,Event:window.Event,KeyboardEvent:KeyEvent,getComputedStyle:()=>({visibility:'visible',display:'block',overflowY:'visible'}),setTimeout:(f,ms)=>nativeSetTimeout(f,Math.max(1,ms/50)),clearTimeout,setInterval,clearInterval,console});
  vm.runInContext(source,context);
  const api=context.MPMaintenanceRuntime.create({ui,host:ui,all:(q,e=document)=>[...e.querySelectorAll(q)],visible:e=>!!e?.isConnected,clean:s=>String(s||'').replace(/\s+/g,' ').trim(),say:()=>{},stop:()=>{}});
  try{
    const workerId=messages.find(m=>m.type==='ready').from;
    const job=core.parseSubscription({index:100,name:'目标剧 S02',year:'2025',progress:'3/12'});
    await BC.instance.onmessage({data:{type:'job',from:'owner',to:workerId,run:'test',jobId:'job1',job}});
    const result=messages.find(m=>m.type==='result');
    assert.ok(result,JSON.stringify(messages));
    assert.deepEqual(selected.sort(),['其他','天空','观众','馒头'].sort());
    if(filterFailure){assert.equal(downloads.length,0);assert.equal(result.ok,false);assert.match(result.error,/季筛选核对失败/);console.log('PASS unconfirmed season selection prevents download');return;}
    if(stopBeforeDownload){assert.equal(downloads.length,0);assert.equal(result.ok,false);console.log('PASS worker stop immediately before download prevents click');return;}
    assert.equal(downloads.length,1);
    assert.equal(downloads[0].n,more?45:5);
    if(!more)assert.equal(downloads[0].size,'13.69 GB');
    assert.equal(downloads[0].season,2);
    assert.equal(result.ok,success,JSON.stringify(result));
    assert.equal(JSON.parse(storage.get('mp-tv-maintenance-v2:job:job1')).status,success?'download-confirmed':'failed');
    if(more)assert.equal(document.querySelector('.more-sources-content'),null);
    console.log(`PASS worker UI flow: preserved sites plus two + verified season menu + seeders/size ranking + ${success?'download acknowledged':'rejection retained'}`);
  }finally{api.destroy();}
}
(async()=>{await scenario(true);await scenario(false);await scenario(true,true);await scenario(true,false,true);await scenario(true,false,false,true);})().catch(e=>{console.error(e);process.exitCode=1;});
assert.equal(core.chooseResource([{title:'零日攻击',season:1,site:'天空',seeders:20,total:6},{title:'零日攻击',season:1,site:'观众',seeders:7,total:10},{title:'零日攻击',season:1,site:'天空',seeders:30,total:null}],{name:'零日攻击',season:1,total:10}).seeders,7);
