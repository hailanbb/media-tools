/**
 * 整理修复状态机与跨阶段校验
 * 移植自原仓库 repair-state.js
 *
 * 管理 MoviePilot 整理历史修复状态，并在复核阶段（三）与归档阶段（四）提供闭环验证
 */

export const REPAIR_STATES = [
  'ready',                   // 待确认修复
  'blocked',                 // 修复阻挡待人工核实
  'checked',                 // 已检查
  'history-delete-pending',  // 历史删除待核实
  'history-deleted',         // 历史已解除
  'transfer-pending',        // 转移待核实
  'submitted',               // 已提交重转
  'uncertain',               // 修复结果待核实
  'files-verified'           // 文件已复核
];

/**
 * 从报告数据行读取修复记录
 * @param {object} data - 任务行单元格数据
 * @returns {object|null} 解析出的修复记录对象
 */
export function readRepairState(data) {
  if (!data) return null;
  const continuations = Object.keys(data)
    .filter(h => /^修复记录续\d+$/.test(h))
    .sort((a, b) => Number(a.match(/\d+$/)[0]) - Number(b.match(/\d+$/)[0]))
    .map(h => String(data[h] || ''))
    .join('');

  const text = String(data['修复记录'] || '') + continuations;
  if (!text) return null;
  if (text.length > 2000000) throw new Error('修复记录超出大小限制');

  try {
    const r = JSON.parse(text);
    if (r?.schema !== 1 || !REPAIR_STATES.includes(r.state) || !Array.isArray(r.items) || !Array.isArray(r.episodes)) {
      return null;
    }
    return r;
  } catch {
    return null;
  }
}

/**
 * 判断修复记录是否处于未闭环状态
 * 只要存在且状态不是 'files-verified'，就属于未闭环
 * @param {object|null} r
 * @returns {boolean}
 */
export function isRepairPending(r) {
  return !!r && r.state !== 'files-verified';
}

/**
 * 判断修复记录是否存在待核实的不确定状态
 * @param {object|null} r
 * @returns {boolean}
 */
export function isRepairUncertain(r) {
  if (!r) return false;
  const uncertainStates = ['history-delete-pending', 'history-deleted', 'transfer-pending', 'uncertain'];
  if (uncertainStates.includes(r.state)) return true;
  return Array.isArray(r.items) && r.items.some(i => uncertainStates.includes(i.state));
}

/**
 * 获取友好的中文状态标签
 * @param {object|null} r
 * @returns {string}
 */
export function getRepairLabel(r) {
  const map = {
    ready: '待确认修复',
    blocked: '修复阻挡待人工核实',
    checked: '已检查/待重转',
    submitted: '已提交重转',
    uncertain: '修复结果待核实',
    'history-delete-pending': '历史删除待核实',
    'history-deleted': '历史已解除',
    'transfer-pending': '转移待核实',
    'files-verified': '文件已复核'
  };
  return map[r?.state] || '未记录';
}

/**
 * 闭环验证：核实网盘中实际存在的集号是否已包含修复涉及的集号
 * 供阶段三（复核清理）使用
 * @param {object|null} r - 当前修复记录
 * @param {number[]} episodes - 网盘当前已识别出的集号数组
 * @param {string} at - 校验时间 ISO
 * @returns {object|null} 更新后的修复记录对象
 */
export function verifyRepairState(r, episodes, at = new Date().toISOString()) {
  if (!r || isRepairUncertain(r) || !r.episodes?.length) return r;
  // 检查是否所有修复集号都已实际出现在网盘中
  const allPresent = r.episodes.every(n => episodes.includes(n));
  if (!allPresent) return r;

  return {
    ...r,
    state: 'files-verified',
    at,
    verifiedAt: at,
    note: '第三脚本确认本地修复集号已实际出现，允许与同版本及其他集一同复核'
  };
}
