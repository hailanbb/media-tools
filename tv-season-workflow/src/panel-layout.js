/* 四个阶段共用的面板位置控制，不访问网络或执行业务任务。
 * 标题栏可拖动；点击收起一律停靠右下角；展开恢复原展开位置。
 * 收起后仍可拖动，下一次收起重新停靠。位置按各脚本自己的存储命名空间保存。
 */
(function setupPanelLayout({host,box,body,handle,toggle,load,save}) {
  const margin=20,edge=8,valid=p=>p&&Number.isFinite(p.x)&&Number.isFinite(p.y);
  const saved=load()||{},initial=valid(saved.expanded)?saved.expanded:valid(saved)?saved:null;
  let expanded=initial?{x:initial.x,y:initial.y}:null,collapsed=!!saved.collapsed,docked=collapsed,drag=null,scheduled=false;
  function point(){const r=host.getBoundingClientRect();return {x:r.left,y:r.top};}
  function place(p){
    const r=host.getBoundingClientRect(),x=Math.max(edge,Math.min(p.x,Math.max(edge,innerWidth-r.width-edge))),y=Math.max(edge,Math.min(p.y,Math.max(edge,innerHeight-r.height-edge)));
    Object.assign(host.style,{left:x+'px',top:y+'px',right:'auto',bottom:'auto'});return {x,y};
  }
  function dock(){Object.assign(host.style,{left:'auto',top:'auto',right:margin+'px',bottom:margin+'px'});}
  function paint(){
    if(box.classList.contains('min')!==collapsed)box.classList.toggle('min');
    body.hidden=collapsed;toggle.textContent=collapsed?'展开':'收起';
    toggle.setAttribute?.('aria-expanded',String(!collapsed));
  }
  function persist(){save({version:1,collapsed,docked,expanded,position:point()});}
  function fit(){
    if(drag)return;
    if(collapsed&&docked)dock();
    else {const p=place(point());if(!collapsed)expanded=p;}
  }
  function schedule(){if(scheduled)return;scheduled=true;requestAnimationFrame(()=>{scheduled=false;fit();});}
  function togglePanel(){
    drag=null;
    if(!collapsed){expanded=point();collapsed=true;docked=true;paint();dock();}
    else {collapsed=false;docked=false;paint();if(expanded)expanded=place(expanded);else dock();}
    persist();schedule();
  }
  paint();
  if(collapsed){
    // 刷新时允许恢复收起后手动拖过的位置；旧版收起位置默认迁移到右下角。
    if(saved.version===1&&saved.docked===false&&valid(saved.position)){docked=false;place(saved.position);}else dock();
  }else if(expanded)expanded=place(expanded);
  handle.addEventListener('pointerdown',e=>{
    if(e.button!==0||e.target.closest('button, input, select, textarea, a'))return;
    const p=point();drag={id:e.pointerId,x:e.clientX,y:e.clientY,left:p.x,top:p.y};docked=false;
    handle.setPointerCapture(e.pointerId);e.preventDefault();
  });
  handle.addEventListener('pointermove',e=>{
    if(drag?.id!==e.pointerId)return;
    place({x:drag.left+e.clientX-drag.x,y:drag.top+e.clientY-drag.y});
  });
  const end=e=>{
    if(drag?.id!==e.pointerId)return;drag=null;
    const p=place(point());if(!collapsed)expanded=p;persist();
  };
  handle.addEventListener('pointerup',end);handle.addEventListener('pointercancel',end);handle.addEventListener('lostpointercapture',end);
  window.addEventListener('resize',()=>{fit();persist();});
  new ResizeObserver(schedule).observe(box);schedule();
  return {toggle:togglePanel};
})
