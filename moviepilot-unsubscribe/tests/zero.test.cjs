const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {parseHTML} = require('linkedom');
const source = fs.readFileSync(require('node:path').join(__dirname,'../moviepilot-unsubscribe.user.js'),'utf8');
const wait = ms => new Promise(r => setTimeout(r,ms));

function fixture({hash='#/subscribe/tv', rows, windowSize=3, mutateBeforeClick=false}={}) {
  const {window,document} = parseHTML('<html><body><div class="v-window-item--active"><div class="progressive-card-grid"></div></div></body></html>');
  let g = document.querySelector('.progressive-card-grid');
  const data = rows.map((progress,i) => ({id:i, name:'剧'+i, progress}));
  const calls=[];
  let scrollTop=0;
  const scroll = {
    clientHeight:300, scrollHeight:data.length*100, get scrollTop(){return scrollTop;}, set scrollTop(v){scrollTop=v;},
    getBoundingClientRect(){return {top:0,bottom:300,height:300};},
    scrollTo({top}) { scrollTop=Math.max(0,Math.min(top,Math.max(0,this.scrollHeight-this.clientHeight))); render(); }
  };
  Object.defineProperty(document,'scrollingElement',{value:scroll});
  Object.defineProperty(document,'hidden',{value:false,writable:true});
  window.Element.prototype.getClientRects=function(){return this.isConnected?[{}]:[];};
  window.Element.prototype.getBoundingClientRect=function(){
    const item=this.closest?.('.progressive-card-grid__item');
    const i=item ? Number(item.dataset.progressiveGridIndex) : 0;
    const top=i*100-scrollTop;
    return {top,bottom:top+100,height:100,left:0,right:800,width:800};
  };
  window.Element.prototype.scrollIntoView=function(){
    const i=Number(this.dataset.progressiveGridIndex); scroll.scrollTo({top:i*100-100,behavior:'instant'});
  };
  window.XMLHttpRequest=class {
    constructor(){this.events={};this.status=200;this.responseType='';}
    open(method,url){this.method=method;this.url=url;}
    addEventListener(n,f){this.events[n]=f;}
    removeEventListener(n,f){if(this.events[n]===f)delete this.events[n];}
    send(){this.responseText=JSON.stringify({success:true});this.events.loadend?.();}
  };
  const context=vm.createContext({window,document,location:{host:'moviepilot.example',hash,href:'https://moviepilot.example/'+hash},Element:window.Element,AbortController,console,URL,
    getComputedStyle:e=>({visibility:'visible',display:'block',overflowY:e===scroll?'auto':'visible',gridTemplateColumns:'1fr',rowGap:'0'}),
    setTimeout:(f,ms)=>setTimeout(f,Math.max(1,ms/40)),clearTimeout});
  function render(){
    scroll.scrollHeight=data.length*100; g.innerHTML='';
    const first=Math.max(0,Math.min(data.length-windowSize,Math.floor(scrollTop/100)));
    data.slice(first,first+windowSize).forEach((row,i)=>{
      const index=first+i, item=document.createElement('div'); item.className='progressive-card-grid__item'; item.dataset.progressiveGridIndex=String(index);
      const progress=row.progress==null?'':String(row.progress);
      item.innerHTML=`<div class="subscribe-card"><div class="font-medium">2026</div><div class="font-bold">${row.name}</div><div class="flex-shrink-0 text-subtitle-2">${progress}</div><button aria-haspopup="menu" aria-expanded="false" aria-owns="menu-${row.id}"></button></div>`;
      const b=item.querySelector('button'); b.onclick=()=>{
        const old=document.getElementById('menu-'+row.id); if(old){old.remove();b.setAttribute('aria-expanded','false');return;}
        b.setAttribute('aria-expanded','true'); const m=document.createElement('div');m.id='menu-'+row.id;m.className='v-menu v-overlay--active';m.innerHTML='<div class="v-list-item"><div class="v-list-item-title">取消订阅</div></div>';
        m.querySelector('.v-list-item').onclick=()=>{calls.push(row.id);m.remove();b.setAttribute('aria-expanded','false');const req=new window.XMLHttpRequest();req.open('DELETE','/subscribe/'+row.id);req.send();data.splice(index,1);render();}; document.body.append(m);
        if(mutateBeforeClick && row.progress==='0 / 2') item.querySelector('.flex-shrink-0.text-subtitle-2').textContent='1 / 12';
      }; g.append(item);
    });
  }
  render(); vm.runInContext(source,context);
  const ui=document.getElementById('moviepilot-bottom-unsubscribe').shadowRoot;
  const mode=ui.querySelector('#mode'); mode.querySelector('option[value="all"]').selected=false; mode.querySelector('option[value="zero"]').selected=true; mode.value='zero';
  return {ui,data,calls,status:()=>ui.querySelector('#status').textContent,run:preview=>{if(preview)ui.querySelector('#preview').click();else ui.querySelector('#start').click();},async done(){for(let i=0;i<1000;i++){if(!ui.querySelector('#start').disabled)return;await wait(10);}throw Error('timeout: '+this.status());},cleanup:()=>window.__moviepilotBottomUnsubscribe?.remove()};
}

(async()=>{
  let f=fixture({rows:['0 / 12','5 / 12',null,'x / 4','0 / 8','2 / 2','0 / 1','0 / 3']}); f.run(); await f.done(); assert.deepEqual(f.calls,[7,6,4,0]); f.cleanup(); console.log('PASS mixed zero/nonzero/missing/malformed');
  f=fixture({rows:Array.from({length:18},(_,i)=>i%5===0?'0 / 10':'2 / 10'),windowSize:3}); f.run(); await f.done(); assert.deepEqual(f.calls,[15,10,5,0]); f.cleanup(); console.log('PASS virtual windows bottom-up');
  f=fixture({rows:['0 / 2','2 / 2','0 / 2']}); f.run(true); await f.done(); assert.deepEqual(f.calls,[]); assert.match(f.status(),/未取消任何订阅/); f.cleanup(); console.log('PASS preview');
  f=fixture({rows:['0 / 0','10 / 12','0 / 1']}); f.run(); await f.done(); assert.deepEqual(f.calls,[2]); f.cleanup(); console.log('PASS rejects 0/0 and nonzero first progress');
  f=fixture({rows:['0 / 1','4 / 4','0 / 1']}); f.run(); await f.done(); assert.deepEqual(f.calls,[2,0]); f.cleanup(); console.log('PASS first and last zero cards');
  f=fixture({hash:'#/subscribe/movie',rows:['0 / 2','0 / 2']}); f.run(); await f.done(); assert.deepEqual(f.calls,[]); assert.match(f.status(),/只用于电视剧/); f.cleanup(); console.log('PASS movie rejects zero mode');
  f=fixture({rows:['0 / 2','2 / 2'],mutateBeforeClick:true}); f.run(); await f.done(); assert.deepEqual(f.calls,[]); f.cleanup(); console.log('PASS zero changes before click is skipped');
})().catch(e=>{console.error(e);process.exitCode=1;});
