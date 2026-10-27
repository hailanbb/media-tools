/**
 * @fileoverview 集号解析模块 — 从文件名和报告字段中提取集号
 *
 * 移植自原仓库 season-inspect.js / download-core.js 的纯算法部分，
 * 增强了中文数字支持与更健壮的裸数字提取逻辑。
 */

// ── 文件扩展名正则 ─────────────────────────────────────────────

/** 视频文件扩展名 */
export const MEDIA_REGEX = /\.(?:mp4|mkv|avi|mov|m4v|ts|m2ts|wmv|flv|webm|mpg|mpeg|strm|rmvb|vob|iso)$/i;

/** 刮削文件（NFO/图片）扩展名 */
export const SCRAPE_REGEX = /\.(?:nfo|jpg|jpeg|png|webp|avif|bmp|gif|tbn)$/i;

/** 共用刮削文件名（不属于单个视频） */
export const SHARED_SCRAPE = /^(?:tvshow|season(?:\d+)?|movie|poster|fanart|folder|banner|logo|clearlogo|clearart|landscape|disc|season(?:\d+)?[-_.](?:poster|fanart|banner|landscape))$/i;

// ── 中文数字工具 ────────────────────────────────────────────────

/**
 * 中文数字 → 阿拉伯数字映射
 * @type {Record<string, number>}
 */
const CN_DIGIT = {
  零: 0, 〇: 0, 一: 1, 二: 2, 两: 2, 三: 3, 四: 4,
  五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10,
};

/**
 * 将简单中文数字字符串转为整数（支持到 99）。
 * @param {string} cn - 中文数字串，如 "十二", "二十", "五"
 * @returns {number|null}
 */
function cnToInt(cn) {
  if (!cn) return null;
  const chars = [...cn];
  if (chars.length === 1) return CN_DIGIT[chars[0]] ?? null;
  if (chars[0] === '十') return 10 + (CN_DIGIT[chars[1]] ?? 0);
  const ti = chars.indexOf('十');
  if (ti > 0) {
    const tens = CN_DIGIT[chars[ti - 1]] ?? 0;
    const ones = ti + 1 < chars.length ? (CN_DIGIT[chars[ti + 1]] ?? 0) : 0;
    return tens * 10 + ones;
  }
  return null;
}

// ── 集号提取 ────────────────────────────────────────────────────

/**
 * 从文件名中提取集号。
 *
 * 按优先级依次匹配：
 * 1. S01E05 格式（季号可选校验）
 * 2. 第N集/话/回/章（阿拉伯数字 + 中文数字）
 * 3. EP05 / E05 格式
 * 4. 尾部 " - 05" 格式
 * 5. 裸数字文件名
 *
 * @param {string} filename - 文件名（含扩展名）
 * @param {number|null} [season=null] - 当前季号，用于校验 SxxExx 中的季号
 * @returns {number|null} 集号，无法识别时返回 null
 *
 * @example
 * episodeFromName('S01E05.mkv', 1)       // → 5
 * episodeFromName('剧名 - S01E05 - 05.mkv', 1) // → 5
 * episodeFromName('第5集.mkv')            // → 5
 * episodeFromName('EP05.mkv')             // → 5
 * episodeFromName('05.mkv')               // → 5
 */
export function episodeFromName(filename, season = null) {
  if (!filename || !MEDIA_REGEX.test(filename)) return null;
  const name = filename.replace(/\.[^.]+$/, '');

  // 动漫专属过滤：忽略 NCOP/NCED/PV/CM/Preview/Menu 等特典与非正片视频
  if (/(?:^|[\s._\-[\]])(?:NCOP|NCED|PV|CM|Preview|Menu|Creditless|Textless|CleanOP|CleanED)\d*/i.test(name)) {
    return null;
  }

  // ① S01E05 格式（最常见，季号匹配则优先）
  if (season != null) {
    const sePattern = new RegExp(
      `(?:^|[^a-z0-9])S0*${season}\\s*E(\\d{1,4})(?!\\d)`, 'i',
    );
    const seMatch = sePattern.exec(name);
    if (seMatch) return Number(seMatch[1]) || null;
  } else {
    // 无指定季号时，直接匹配任意 SxxExx
    const seMatch = /(?:^|[^a-z0-9])S\d{1,4}\s*E(\d{1,4})(?!\d)/i.exec(name);
    if (seMatch) return Number(seMatch[1]) || null;
  }

  // ② 第N集/话/話/回/章（阿拉伯数字，兼容动漫日文“話”）
  const cnMatch = /第\s*(\d{1,4})\s*[集话話回章]/.exec(name);
  if (cnMatch) return Number(cnMatch[1]) || null;

  // ② 第N集/话/話/回/章（中文数字）
  const cnCharMatch = /第\s*([零〇一二两三四五六七八九十]+)\s*[集话話回章]/.exec(name);
  if (cnCharMatch) {
    const n = cnToInt(cnCharMatch[1]);
    if (n != null && n > 0) return n;
  }

  // ③ EP05 / E05 格式
  const epMatch = /(?:^|[\s._-])(?:EP?)(\d{1,4})(?!\d)/i.exec(name);
  if (epMatch) return Number(epMatch[1]) || null;

  // ④ 文件名中或尾部 " - 05" 格式（常见于动漫压制组 [Group] Title - 05 [1080p]）
  const midDashMatch = /(?:^|[\s._])-\s*(\d{1,4})(?!\d)/.exec(name);
  if (midDashMatch) return Number(midDashMatch[1]) || null;

  // ⑤ 独立方括号集号格式（常见于动漫，如 [01] 或 [01v2]）
  const bracketMatch = /\[(\d{1,4})(?:v\d+)?\]/.exec(name);
  if (bracketMatch) {
    const n = Number(bracketMatch[1]);
    // 过滤掉可能是年份 19xx/20xx 或分辨率 720/1080/2160
    if (n > 0 && n < 1900 && n !== 720 && n !== 1080 && n !== 2160) {
      return n;
    }
  }

  // ⑥ 纯数字文件名
  if (/^\d{1,4}$/.test(name.trim())) return Number(name.trim()) || null;

  return null;
}

// ── 集号字段解析 ────────────────────────────────────────────────

/**
 * 解析报告中的集号字段。
 *
 * 支持格式：
 * - 逗号/顿号/分号分隔: "2、4" "1,3,5" "2；4"
 * - 连续范围: "2-5" "3～8" "2至5"
 * - 混合: "1,3,5-8"
 * - 带 E 前缀: "E03-E06"
 *
 * @param {string|number|null|undefined} value - 字段值
 * @param {{ blankUnknown?: boolean }} [options]
 * @returns {number[]|null} 排序去重的集号数组；null 表示未确认
 * @throws {Error} 格式无法解析时抛出中文错误
 *
 * @example
 * parseEpisodeField('2、4')      // → [2, 4]
 * parseEpisodeField('1,3,5-8')   // → [1, 3, 5, 6, 7, 8]
 * parseEpisodeField('未确认')     // → null
 * parseEpisodeField('齐全')       // → []
 */
export function parseEpisodeField(value, { blankUnknown = false } = {}) {
  const text = String(value ?? '').trim();
  if (!text) return blankUnknown ? null : [];
  if (/^(无|无缺集|齐全)$/.test(text)) return [];
  if (/未确认|未知|待核实|无法/.test(text)) return null;

  const values = [];
  for (const part of text.split(/[、,，;；\s]+/).filter(Boolean)) {
    const m = /^(?:E)?(\d{1,4})(?:[-~～至](?:E)?(\d{1,4}))?$/i.exec(part);
    if (!m) throw new Error(`集号格式无法确认：「${part.slice(0, 25)}」（原文: "${text}"）`);
    const a = Number(m[1]);
    const b = Number(m[2] || m[1]);
    if (!Number.isInteger(a) || a < 1 || !Number.isInteger(b) || b < a || b > 10000) {
      throw new Error(`集号范围无效：${a}-${b}（原文: "${text}"）`);
    }
    for (let n = a; n <= b; n++) values.push(n);
  }
  return [...new Set(values)].sort((a, b) => a - b);
}

// ── 工具函数 ────────────────────────────────────────────────────

/** 生成 1..n 的连续整数数组 */
export function range(n) {
  return Array.from({ length: n }, (_, i) => i + 1);
}

/** 去重并排序 */
export function uniq(arr) {
  return [...new Set(arr)].sort((a, b) => a - b);
}

/** 判断正整数（1 ~ 10000） */
export function positive(n) {
  return Number.isInteger(n) && n > 0 && n <= 10000;
}
