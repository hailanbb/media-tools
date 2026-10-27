const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {parseHTML} = require('linkedom');
const source = fs.readFileSync(require('node:path').join(__dirname,'../moviepilot-unsubscribe.user.js'),'utf8');
const wait = ms => new Promise(r => setTimeout(r,ms));

function fixture(mode='success', total=6) {
  const {window,document} = parseHTML('<html><body><div class="v-window-item--active"><div class="progressive-card-grid"></div></div></body></html>');
  let g = document.querySelector('.progressive-card-grid');
  const data = Array.from({length:total},(_,i)=>'电影'+i);
  const calls=[];
  window.Element.prototype.getClientRects = function() {return this.isConnected ? [{}] : [];};
  let needsRender = false;
  const scroll = {scrollHeight:1000,clientHeight:500,scrollTop:0,scrollTo({top}) {this.scrollTop = top-this.clientHeight;if(needsRender){needsRender=false;render();}}};
  Object.defineProperty(document,'scrollingElement',{value:scroll});
  Object.defineProperty(document,'hidden',{value:false,writable:true});
  const sandbox = {
    window,document,location:{host:'moviepilot.example',hash:'#/subscribe/movie',href:'https://moviepilot.example/#/subscribe/movie'},
    Element:window.Element,AbortController,console,URL,
    getComputedStyle:()=>({visibility:'visible',display:'block',overflowY:'visible'}),
    setTimeout:(f,ms)=>setTimeout(f,Math.max(1,ms/40)),clearTimeout,
  };
  window.XMLHttpRequest = class {
    constructor(){this.events=new Map();this.status=200;this.responseType='';}
    open(method,url){this.method=method;this.url=url;}
    addEventListener(n,f){this.events.set(n,f);}
    removeEventListener(n,f){if(this.events.get(n)===f)this.events.delete(n);}
    send(){const finish=()=>{const failed=['networkError','lateError'].includes(mode);this.responseText=JSON.stringify({success:!failed,message:failed?'没有删除权限':''});this.events.get('loadend')?.();};if(mode==='lateError')setTimeout(finish,90);else finish();}
  };
  const context=vm.createContext(sandbox);
  function render() {
    // 只保留最后三张卡；删除后整批重建，模拟虚拟列表换节点。
    g.innerHTML='';
    data.forEach((name,i) => {
      if (i<data.length-3) return;
      const item=document.createElement('div');
      item.className='progressive-card-grid__item';
      item.setAttribute('data-progressive-grid-index',String(i));
      item.innerHTML=`<div class="subscribe-card"><div class="font-medium">2026</div><div class="font-bold">${name}</div><button aria-haspopup="menu" aria-expanded="false" aria-owns="menu-${i}"></button></div>`;
      const b=item.querySelector('button');
      b.onclick=()=>{
        const old=document.getElementById('menu-'+i);
        if(old) {old.remove();b.setAttribute('aria-expanded','false');return;}
        b.setAttribute('aria-expanded','true');
        const m=document.createElement('div');m.id='menu-'+i;m.className='v-menu v-overlay--active';
        m.innerHTML='<div class="v-list-item"><div class="v-list-item-title">取消订阅</div></div>';
        m.querySelector('.v-list-item').onclick=()=>{
          calls.push(i);m.remove();b.setAttribute('aria-expanded','false');
          if(mode==='networkError'||mode==='refresh'||mode==='lateError') {
            const req = new window.XMLHttpRequest();req.open('DELETE','/api/v1/subscribe/'+i);req.send();
            if(mode==='networkError')return;
            if(mode==='lateError'){data.pop();render();return;}
            data.pop();
            const next = document.createElement('div');next.className='progressive-card-grid';g.replaceWith(next);g=next;
            scroll.scrollTop=0;needsRender=true;return;
          }
          if(mode==='stop') {window.__moviepilotBottomUnsubscribe.stop();return;}
          if(mode==='unknown'||mode==='confirm') {
            const d=document.createElement('div');d.className='v-dialog v-overlay--active';
            d.innerHTML=`<div class="v-card-title">${mode==='confirm'?'取消订阅':'其他操作'}</div><p>取消订阅 ${name}</p><button>确认</button>`;
            d.querySelector('button').onclick=()=>{d.remove();data.pop();render();};
            document.body.append(d);return;
          }
          if(mode==='timeout') return;
          if(mode==='reorder') {data.pop();data.pop();render();return;}
          data.pop();render();
        };
        document.body.append(m);
      };
      g.append(item);
    });
  }
  render();vm.runInContext(source,context);
  const ui=document.getElementById('moviepilot-bottom-unsubscribe').shadowRoot;
  return {window,document,context,ui,calls,data,run:()=>ui.querySelector('#start').click(),status:()=>ui.querySelector('#status').textContent,
    async done() {for(let i=0;i<1000;i++){if(!ui.querySelector('#start').disabled)return;await wait(10);}throw Error('test timed out: '+ui.querySelector('#status').textContent);},
    cleanup:()=>window.__moviepilotBottomUnsubscribe?.remove()};
}

(async()=>{
  let f=fixture();
  assert.equal(f.calls.length,0,'默认不运行');
  vm.runInContext(source,f.context);
  assert.equal(f.document.querySelectorAll('#moviepilot-bottom-unsubscribe').length,1,'单例');
  f.ui.querySelector('#preview').click();await f.done();assert.deepEqual(f.calls,[]);assert.match(f.status(),/第 6 项/);
  f.run();await f.done();assert.deepEqual(f.calls,[5,4,3,2,1,0]);assert.match(f.status(),/处理完成/);f.cleanup();
  console.log('PASS 默认闲置、单例、无操作预览、虚拟列表从尾到头、最后一项');
  for(const mode of ['stop','timeout','unknown','reorder']){
    f=fixture(mode);f.run();await f.done();assert.deepEqual(f.calls,[5],mode+' 不得继续操作下一项');
    assert.match(f.status(),/可能已提交/);f.run();await f.done();assert.deepEqual(f.calls,[5],'不确定请求不得重试');
    if(mode==='unknown')assert.equal(f.data.length,6,'未知弹窗不得自动确认');
    f.cleanup();console.log('PASS '+mode+' 停止且不重复发送');
  }
  f=fixture('confirm',2);f.run();await f.done();assert.deepEqual(f.calls,[1,0]);f.cleanup();console.log('PASS 严格匹配确认弹窗');
  f=fixture();f.run();f.window.__moviepilotBottomUnsubscribe.stop();await f.done();assert.deepEqual(f.calls,[]);
  f.run();await f.done();assert.deepEqual(f.calls,[5,4,3,2,1,0]);f.cleanup();console.log('PASS 提交前停止后可重新启动');
  f=fixture();f.run();f.document.hidden=true;await f.done();assert.deepEqual(f.calls,[]);f.cleanup();console.log('PASS 后台停止');
  f=fixture();f.run();f.context.location.href+='?changed';await f.done();assert.deepEqual(f.calls,[]);f.cleanup();console.log('PASS 路由变化停止');
  f=fixture('networkError');f.run();await f.done();assert.deepEqual(f.calls,[5]);assert.match(f.status(),/没有删除权限/);
  const error = f.status();f.window.__moviepilotBottomUnsubscribe.stop();assert.equal(f.status(),error,'停止不得覆盖原始错误');f.cleanup();console.log('PASS 站点返回失败原因、保留原始错误');
  f=fixture('refresh');f.run();await f.done();assert.deepEqual(f.calls,[5,4,3,2,1,0]);assert.match(f.status(),/处理完成/);f.cleanup();console.log('PASS 列表整体重建、滚动位置重置、成功响应');
  f=fixture('lateError');f.run();await f.done();assert.deepEqual(f.calls,[5]);assert.match(f.status(),/已核对完成：0 项/);assert.match(f.status(),/没有删除权限/);f.cleanup();console.log('PASS 列表先变化但请求随后失败时不误计成功');
})().catch(e=>{console.error(e);process.exitCode=1;});
