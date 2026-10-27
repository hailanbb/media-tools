/* 四阶段共用的整理修复状态：报告是证据，不是可直接执行的请求。
 * 任何删除历史/整理动作都必须由第二脚本重新查询并构建清单。
 */
(function createRepairState() {
  'use strict';
  const states=['ready','blocked','checked','history-delete-pending','history-deleted','transfer-pending','submitted','uncertain','files-verified'];
  function read(data) {
    const text=String(data?.['整理修复记录']||'')+Object.keys(data||{}).filter(h=>/^整理修复记录续\d+$/.test(h)).sort((a,b)=>Number(a.match(/\d+$/)[0])-Number(b.match(/\d+$/)[0])).map(h=>String(data[h]||'')).join('');
    if(!text)return null;
    if(text.length>2000000)throw Error('整理修复记录过大');
    const r=JSON.parse(text);
    if(r?.schema!==1||!states.includes(r.state)||!Array.isArray(r.items)||!Array.isArray(r.episodes)||r.episodes.some(n=>!Number.isInteger(n)||n<1||n>10000)||!Number.isFinite(Date.parse(r.at)))throw Error('整理修复记录格式无效');
    return r;
  }
  function pending(r){return !!r&&r.state!=='files-verified';}
  function uncertain(r){return !!r&&(['history-delete-pending','history-deleted','transfer-pending','uncertain'].includes(r.state)||r.items?.some(i=>['history-delete-pending','history-deleted','transfer-pending','uncertain'].includes(i.state)));}
  function label(r){return ({ready:'待确认修复',blocked:'整理修复待人工核实',checked:'整理问题已检查',submitted:'已提交整理，待云盘复核',uncertain:'整理修复结果待核实','history-delete-pending':'删除历史结果待核实','history-deleted':'历史已解除，整理待完成','transfer-pending':'整理请求结果待核实','files-verified':'云盘文件已复核'})[r?.state]||'未检查';}
  function verify(r,episodes,at) {
    if(!r||uncertain(r)||!r.episodes.length||!r.episodes.every(n=>episodes.includes(n)))return r;
    return {...r,state:'files-verified',at,verifiedAt:at,note:'第三脚本确认本轮修复集号已实际出现；整季完整性和版本清理由复核结果单独判定'};
  }
  return {read,pending,uncertain,label,verify};
})
