/**
 * @fileoverview Excel 报表适配器（无头模式）
 *
 * 封装 xlsx-js-style，提供剧集整理工作流所需的 .xlsx 报表读取/写入能力。
 * 支持：
 * - 初检全新报表创建
 * - 补缺/复核/归档三阶段结果回写
 * - 单元格颜色识别与着色
 * - MoviePilot 整理修复列（修复状态、修复集号、修复说明、修复记录等）分片写入与读取
 */

import XLSX from 'xlsx-js-style';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { getRepairLabel } from './repair-state.js';

// ── 颜色常量与映射 ───────────────────────────────────────────────────

export const COLORS = {
  blue: { bg: 'BDD7EE', fg: '1F4E78' },     // 连载中
  yellow: { bg: 'FFFF00', fg: '000000' },   // 待复核/待下载/待清理
  red: { bg: 'FFC7CE', fg: '9C0006' },      // 无合格资源/空目录
  green: { bg: 'C6EFCE', fg: '006100' },    // 已播完且齐全
  archived: { bg: 'E4DFEC', fg: '7030A0' }, // 已归档（紫色）
};

/**
 * 将十六进制 RGB 转换为 HSV
 */
function hexToHsv(hex) {
  const r = parseInt(hex.slice(0, 2), 16) / 255;
  const g = parseInt(hex.slice(2, 4), 16) / 255;
  const b = parseInt(hex.slice(4, 6), 16) / 255;

  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;

  let h = 0;
  if (d !== 0) {
    if (max === r) h = 60 * (((g - b) / d) % 6);
    else if (max === g) h = 60 * ((b - r) / d + 2);
    else h = 60 * ((r - g) / d + 4);
  }
  if (h < 0) h += 360;

  const s = max === 0 ? 0 : d / max;
  const v = max;
  return { h, s, v };
}

/**
 * 从样式中识别颜色类别
 */
export function detectColor(style) {
  const fg = style?.fgColor || style?.fill?.fgColor;
  let hex = fg?.rgb || fg?.color;
  if (!hex || typeof hex !== 'string') return 'other';
  if (hex.length === 8 && /^FF/i.test(hex)) hex = hex.slice(2);
  hex = hex.toUpperCase();
  if (hex.length !== 6) return 'other';

  if (hex === 'E4DFEC') return 'archived';

  const { h, s, v } = hexToHsv(hex);
  if (h >= 75 && h <= 170 && s >= 0.08 && v >= 0.4) return 'green';
  if (h >= 40 && h <= 70 && s >= 0.15 && v >= 0.7) return 'yellow';
  if (h >= 190 && h <= 250 && s >= 0.1) return 'blue';
  if ((h >= 340 || h <= 20) && s >= 0.15) return 'red';

  return 'other';
}

function buildFill(colorName) {
  const c = COLORS[colorName];
  if (!c) return undefined;
  return {
    patternType: 'solid',
    fgColor: { rgb: 'FF' + c.bg },
  };
}

function buildFont(colorName) {
  const c = COLORS[colorName];
  if (!c) return { name: 'Microsoft YaHei', sz: 10 };
  return {
    name: 'Microsoft YaHei',
    sz: 10,
    color: { rgb: 'FF' + c.fg },
    bold: colorName === 'yellow' || colorName === 'archived',
  };
}

// ── 读取报表 ─────────────────────────────────────────────────────────

export function readReport(filePath, { origin, source, mode = 'download' } = {}) {
  const absPath = resolve(filePath);
  const original = readFileSync(absPath);

  const workbook = XLSX.read(original, {
    type: 'buffer',
    cellStyles: true,
    cellDates: true,
  });

  const tasks = [];
  const errors = [];
  let skipped = 0;

  for (const sheetName of workbook.SheetNames) {
    if (/明细|日志|Audit/i.test(sheetName)) continue;
    const sheet = workbook.Sheets[sheetName];
    if (!sheet || !sheet['!ref']) continue;

    const range = XLSX.utils.decode_range(sheet['!ref']);
    const headerRow = [];
    for (let C = range.s.c; C <= range.e.c; ++C) {
      const cell = sheet[XLSX.utils.encode_cell({ r: range.s.r, c: C })];
      headerRow.push(cell ? String(cell.v).trim() : '');
    }

    const dirColIdx = headerRow.indexOf('剧集目录');
    if (dirColIdx === -1) continue;

    const headers = headerRow;

    for (let R = range.s.r + 1; R <= range.e.r; ++R) {
      const nameCell = sheet[XLSX.utils.encode_cell({ r: R, c: dirColIdx })];
      const name = nameCell ? String(nameCell.v).trim() : '';
      if (!name) {
        skipped++;
        continue;
      }

      const cellAddress = XLSX.utils.encode_cell({ r: R, c: dirColIdx });
      const style = nameCell?.s || {};
      const color = detectColor(style);

      // 按模式过滤
      if (mode === 'download' && (color === 'green' || color === 'archived')) {
        skipped++;
        continue;
      }
      if (mode === 'review' && color !== 'yellow') {
        skipped++;
        continue;
      }

      // 构建行数据映射
      const data = {};
      for (let C = range.s.c; C <= range.e.c; ++C) {
        const h = headers[C];
        if (h) {
          const valCell = sheet[XLSX.utils.encode_cell({ r: R, c: C })];
          data[h] = valCell?.v ?? '';
        }
      }

      const category = String(data['分类'] || '').trim();
      const seasonRaw = data['季数'] ?? data['季'];
      const season = seasonRaw !== '' && seasonRaw != null ? Number(seasonRaw) || null : null;
      const path = String(data['季目录路径'] || data['源云存储链接'] || '').replace(/.*[?&]path=([^&]+).*/, (_, p) => decodeURIComponent(p));
      const showPath = source && category && name ? `${source}/${category}/${name}` : '';

      tasks.push({
        name,
        category,
        season,
        path,
        showPath,
        key: `${sheetName}:${R + 1}`,
        data,
        color,
        sheetName,
        ref: cellAddress,
        row: R,
        style,
      });
    }
  }

  return { tasks, errors, skipped, original };
}

// ── 写入报表 ─────────────────────────────────────────────────────────

export function writeReport(session, outputPath) {
  const absOutput = resolve(outputPath);
  mkdirSync(dirname(absOutput), { recursive: true });

  // 如果是初检模式或无原始文件，从任务列表生成全新 Excel
  if (!session.original || session.mode === 'inspect') {
    return createNewReport(session.tasks, absOutput);
  }

  const { original, tasks, updates } = session;
  const workbook = XLSX.read(original, {
    type: 'buffer',
    cellStyles: true,
    cellDates: true,
  });

  // 保持已有单元格填色（xlsx-js-style 读取时将 fgColor 放在 s 上，而写出时要求嵌套在 s.fill 上）
  for (const sheetName of workbook.SheetNames) {
    const sheet = workbook.Sheets[sheetName];
    if (!sheet) continue;
    for (const key of Object.keys(sheet)) {
      if (key.startsWith('!')) continue;
      const cell = sheet[key];
      if (cell?.s && !cell.s.fill && cell.s.fgColor) {
        cell.s.fill = { patternType: cell.s.patternType || 'solid', fgColor: cell.s.fgColor };
      }
    }
  }

  for (const task of tasks) {
    const result = updates.get(task);
    if (!result) continue;

    const sheet = workbook.Sheets[task.sheetName];
    if (!sheet) continue;

    // 解析当前表头
    const range = XLSX.utils.decode_range(sheet['!ref']);
    const headerMap = new Map();
    for (let C = range.s.c; C <= range.e.c; ++C) {
      const cell = sheet[XLSX.utils.encode_cell({ r: range.s.r, c: C })];
      if (cell) headerMap.set(String(cell.v).trim(), C);
    }

    // 动态添加新列的辅助函数
    const ensureColumn = (colName) => {
      if (headerMap.has(colName)) return headerMap.get(colName);
      const newCol = range.e.c + 1;
      range.e.c = newCol;
      sheet['!ref'] = XLSX.utils.encode_range(range);
      const headerAddr = XLSX.utils.encode_cell({ r: range.s.r, c: newCol });
      sheet[headerAddr] = {
        t: 's',
        v: colName,
        s: {
          font: { name: 'Microsoft YaHei', sz: 10, bold: true },
          fill: { fgColor: { rgb: 'FFE8EEF3' } },
          alignment: { horizontal: 'center' }
        }
      };
      headerMap.set(colName, newCol);
      return newCol;
    };

    // 映射回写字段
    const fieldMap = { ...(result.fields || {}) };
    if (result.state) {
      if (headerMap.has('处理结果')) fieldMap['处理结果'] = result.state;
      if (session.mode === 'download') fieldMap['下载处理'] = result.state;
      if (session.mode === 'review') fieldMap['复核结果'] = result.state;
      if (session.mode === 'archive') fieldMap['归档结果'] = result.state;
    }
    if (result.note) {
      if (session.mode === 'download') fieldMap['下载说明'] = result.note;
      if (session.mode === 'review') fieldMap['复核说明'] = result.note;
      if (session.mode === 'archive') fieldMap['归档说明'] = result.note;
    }
    if (result.fileCheckedAt) {
      fieldMap['文件核对时间'] = result.fileCheckedAt;
    }

    // 整理修复字段处理
    if (result.repair) {
      const r = result.repair;
      fieldMap['修复状态'] = getRepairLabel(r);
      fieldMap['修复集号'] = r.episodes?.join('、') || '';
      fieldMap['修复时间'] = r.at || '';
      fieldMap['修复说明'] = r.note || '';
      // 修复记录 JSON（超过 30000 字符分片存储）
      const json = JSON.stringify(r);
      fieldMap['修复记录'] = json.slice(0, 30000);
      if (json.length > 30000) {
        fieldMap['修复记录续1'] = json.slice(30000, 60000);
      }
    }

    // 下载记录处理
    if (result.records?.length) {
      fieldMap['下载记录'] = JSON.stringify(result.records).slice(0, 30000);
    }

    // 回写字段到单元格
    for (const [colName, val] of Object.entries(fieldMap)) {
      if (val === undefined) continue;
      const colIdx = ensureColumn(colName);
      const addr = XLSX.utils.encode_cell({ r: task.row, c: colIdx });
      sheet[addr] = {
        t: typeof val === 'number' ? 'n' : 's',
        v: val,
        s: { font: { name: 'Microsoft YaHei', sz: 9 } }
      };
    }

    // 更新行填色
    if (result.color && COLORS[result.color]) {
      const fill = buildFill(result.color);
      const font = buildFont(result.color);
      const nameCol = headerMap.get('剧集目录') ?? 0;
      const addr = XLSX.utils.encode_cell({ r: task.row, c: nameCol });
      sheet[addr] = {
        ...(sheet[addr] || { t: 's', v: task.name }),
        s: { ...(sheet[addr]?.s || {}), fill, font }
      };
    }
  }

  const outputBuffer = XLSX.write(workbook, {
    type: 'buffer',
    bookType: 'xlsx',
    cellStyles: true,
  });

  writeFileSync(absOutput, outputBuffer);
  return absOutput;
}

/**
 * 初检阶段从无到有创建全新报表
 */
function createNewReport(tasks, outputPath) {
  mkdirSync(dirname(outputPath), { recursive: true });
  const headers = [
    '分类', '剧集目录', '季数', '源云存储链接',
    '视频文件数', '已识别集数', '来源登记集数',
    '已识别集号', '重复集号', '缺失集号', '无法识别集号的文件',
    'TMDB ID', '数据来源', '播出状态', '处理结果',
    '待播集号', '播期未知集号', '报告版本'
  ];

  const rows = [headers];
  const styles = [];

  for (const t of tasks) {
    const d = t.data || {};
    const row = headers.map(h => d[h] ?? '');
    rows.push(row);
    styles.push(t.color || 'other');
  }

  const ws = XLSX.utils.aoa_to_sheet(rows);

  // 设置列宽
  ws['!cols'] = headers.map(h => ({
    wch: /链接|目录|文件/.test(h) ? 35 : /集号|说明/.test(h) ? 20 : 12
  }));

  // 表头样式
  for (let c = 0; c < headers.length; c++) {
    const addr = XLSX.utils.encode_cell({ r: 0, c });
    if (ws[addr]) {
      ws[addr].s = {
        font: { name: 'Microsoft YaHei', sz: 10, bold: true, color: { rgb: 'FF1F3864' } },
        fill: { fgColor: { rgb: 'FFD9E1F2' } },
        alignment: { horizontal: 'center', vertical: 'center' }
      };
    }
  }

  // 单元格数据样式与填色
  for (let r = 0; r < styles.length; r++) {
    const color = styles[r];
    const fill = buildFill(color);
    const font = buildFont(color);
    const nameCol = headers.indexOf('剧集目录');
    const addr = XLSX.utils.encode_cell({ r: r + 1, c: nameCol });

    if (ws[addr] && fill) {
      ws[addr].s = { fill, font };
    }
  }

  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, '剧集清单');

  const buf = XLSX.write(wb, { type: 'buffer', bookType: 'xlsx', cellStyles: true });
  writeFileSync(outputPath, buf);
  return outputPath;
}
