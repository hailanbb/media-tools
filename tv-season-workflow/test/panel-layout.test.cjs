// 共用面板状态机：只测试位置/收起，不包含网络、下载、删除或移动动作。
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
function harness(saved=null){
 const handlers={},windowHandlers={},classes=new Set(),style={},body={},toggle={setAttribute(k,v){this[k]=v;}};
 let stored=saved;
 const env={innerWidth:1200,innerHeight:900,requestAnimationFrame:fn=>fn(),window:{addEventListener:(t,fn)=>{windowHandlers[t]=fn;}},ResizeObserver:class{constructor(fn){this.fn=fn;}observe(){}}};
 const host={style,getBoundingClientRect(){const width=classes.has('min')?240:440,height=classes.has('min')?100:500;return {width,height,left:style.left&&style.left!=='auto'?parseFloat(style.left):env.innerWidth-width-parseFloat(style.right||20),top:style.top&&style.top!=='auto'?parseFloat(style.top):env.innerHeight-height-parseFloat(style.bottom||20)};}};
 const box={classList:{contains:k=>classes.has(k),toggle:k=>classes.has(k)?classes.delete(k):classes.add(k)}},handle={addEventListener:(t,fn)=>{handlers[t]=fn;},setPointerCapture(){}};
 const setup=vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../src/panel-layout.js'),'utf8'),env);
 const api=setup({host,box,body,handle,toggle,load:()=>stored,save:v=>{stored=structuredClone(v);}});
 function move(x,y){const r=host.getBoundingClientRect(),e={button:0,pointerId:1,clientX:r.left,clientY:r.top,target:{closest:()=>null},preventDefault(){}};handlers.pointerdown(e);handlers.pointermove({...e,clientX:x,clientY:y});handlers.pointerup(e);}
 return {api,host,body,toggle,move,handlers,env,windowHandlers,get stored(){return stored;}};
}
test('展开拖动后收起固定右下角，展开恢复原位置，点击只更新布局状态',()=>{
 const h=harness();h.move(100,70);h.api.toggle();assert.equal(h.body.hidden,true);assert.equal(h.toggle.textContent,'展开');assert.equal(h.host.style.right,'20px');assert.equal(h.host.style.bottom,'20px');
 h.api.toggle();assert.equal(h.toggle.textContent,'收起');assert.equal(h.host.style.left,'100px');assert.equal(h.host.style.top,'70px');assert.equal(h.body.hidden,false);
});
test('收起后可拖动并跨刷新恢复，展开位置单独保存，下次收起仍重新停靠',()=>{
 const h=harness();h.move(100,70);h.api.toggle();h.move(300,200);assert.equal(h.stored.docked,false);
 const reloaded=harness(h.stored);assert.equal(reloaded.body.hidden,true);assert.equal(reloaded.host.style.left,'300px');reloaded.api.toggle();assert.equal(reloaded.host.style.left,'100px');assert.equal(reloaded.host.style.top,'70px');reloaded.api.toggle();assert.equal(reloaded.host.style.right,'20px');
});
test('拖动越界会夹回视口，窗口缩小时面板可见，停靠时保持右下角',()=>{
 const h=harness();h.move(-100,-200);assert.equal(h.host.style.left,'8px');assert.equal(h.host.style.top,'8px');h.move(9999,9999);assert.equal(h.host.style.left,'752px');assert.equal(h.host.style.top,'392px');
 h.env.innerWidth=800;h.env.innerHeight=650;h.windowHandlers.resize();assert.equal(h.host.style.left,'352px');assert.equal(h.host.style.top,'142px');h.api.toggle();h.env.innerWidth=1000;h.windowHandlers.resize();assert.equal(h.host.getBoundingClientRect().left,740);assert.equal(h.host.style.bottom,'20px');
});
test('标题栏内按钮和右键不触发拖动',()=>{
 for(const [button,control] of [[0,true],[2,false]]){const h=harness(),before=h.host.getBoundingClientRect();h.handlers.pointerdown({button,pointerId:1,clientX:100,clientY:100,target:{closest:()=>control?{}:null},preventDefault(){throw Error('不应拦截控件');}});h.handlers.pointermove({pointerId:1,clientX:0,clientY:0});assert.deepEqual(h.host.getBoundingClientRect(),before);}
});
test('第一脚本旧位置记录可迁移，旧收起状态默认停靠，异常坐标不导致越界',()=>{
 const old=harness({x:120,y:90,collapsed:true});assert.equal(old.host.style.right,'20px');old.api.toggle();assert.equal(old.host.style.left,'120px');assert.equal(old.host.style.top,'90px');
 const invalid=harness({expanded:{x:NaN,y:Infinity},collapsed:false});invalid.api.toggle();assert.equal(invalid.host.style.right,'20px');
});
