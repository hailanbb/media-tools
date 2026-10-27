/**
 * @fileoverview 剧集标题匹配与规范化模块
 *
 * 提供繁→简转换、NFKC 正规化、标点/空白清除等文本规范化能力，
 * 以及从目录名中提取剧名、年份、TMDB ID、季号等结构化信息。
 */

import * as OpenCC from 'opencc-js';

// ── 常量 ─────────────────────────────────────────────────────────

/** 需要忽略的特殊后缀（匹配后在比较前移除） */
const SPECIAL_SUFFIXES = /(?:特别篇|总集篇|SP|OVA|OAD|番外篇|剧场版)$/i;

/** 地区标签正则，例如 "(台)" "(港)" "(日)" */
const REGION_TAG_REGEX = /\s*[（(]\s*(?:台|港|日|韩|美|英|泰|法|德|意|西|俄|印|马|新|越|菲|印尼|巴西)\s*[）)]/g;

/** Unicode 标点 + 空白字符清除正则（保留字母、数字、汉字） */
const STRIP_REGEX = /[^\p{L}\p{N}]/gu;

/** 不可见字符清除正则（零宽空格、BOM、控制字符等） */
const INVISIBLE_REGEX = /[\u200B-\u200F\u2028-\u202F\uFEFF\u00AD\u034F\u061C\u180E\u2060-\u2069\uFFF0-\uFFF8]/g;

// ── 规范化器 ─────────────────────────────────────────────────────

/**
 * 创建文本规范化函数。
 *
 * 规范化流程：
 * 1. 繁体中文（台湾用语）→ 简体中文（opencc-js tw2cn）
 * 2. Unicode NFKC 正规化
 * 3. 清除不可见字符
 * 4. 清除标点符号和空格
 * 5. 转小写
 *
 * @returns {(text: string) => string} 规范化函数
 *
 * @example
 * const norm = createNormalizer();
 * norm('進擊的巨人')  // → '进击的巨人'
 * norm('RE：从零开始') // → 're从零开始'
 */
export function createNormalizer() {
  // opencc-js：台湾繁体 → 大陆简体
  const converter = OpenCC.Converter({ from: 'tw', to: 'cn' });

  /**
   * @param {string} text - 原始文本
   * @returns {string} 规范化后的文本
   */
  return function norm(text) {
    if (!text || typeof text !== 'string') return '';

    let s = text;

    // 第 1 步：繁→简
    s = converter(s);

    // 第 2 步：NFKC 正规化（全角→半角、合字分解等）
    s = s.normalize('NFKC');

    // 第 3 步：清除不可见字符
    s = s.replace(INVISIBLE_REGEX, '');

    // 第 4 步：清除所有标点和空白
    s = s.replace(STRIP_REGEX, '');

    // 第 5 步：统一小写
    s = s.toLowerCase();

    return s;
  };
}

// ── 目录名信息提取 ───────────────────────────────────────────────

/**
 * 从剧集目录名中提取结构化信息。
 *
 * 支持格式：
 * - "龙珠 (2024) [tmdbid=12345]"
 * - "进击的巨人 (2013)"
 * - "海贼王"
 *
 * @param {string} directoryName - 目录名
 * @returns {{ title: string, year: number|null, tmdb: number|null }}
 *
 * @example
 * showInfo('龙珠 (2024) [tmdbid=12345]')
 * // → { title: '龙珠', year: 2024, tmdb: 12345 }
 */
export function showInfo(directoryName) {
  if (!directoryName || typeof directoryName !== 'string') {
    return { title: '', year: null, tmdb: null };
  }

  let rest = directoryName.trim();

  // 提取 TMDB ID（方括号包裹）
  let tmdb = null;
  const tmdbMatch = rest.match(/\[tmdbid[=:]?\s*(\d+)\]/i);
  if (tmdbMatch) {
    tmdb = Number(tmdbMatch[1]);
    rest = rest.replace(tmdbMatch[0], '').trim();
  }

  // 提取 TVDB ID（如有，也清除掉以免干扰标题提取）
  rest = rest.replace(/\[tvdbid[=:]?\s*\d+\]/i, '').trim();

  // 提取 IMDB ID
  rest = rest.replace(/\[imdbid[=:]?\s*tt\d+\]/i, '').trim();

  // 提取年份（圆括号或方括号）
  let year = null;
  const yearMatch = rest.match(/[（(]\s*((?:19|20)\d{2})\s*[）)]/);
  if (yearMatch) {
    year = Number(yearMatch[1]);
    rest = rest.replace(yearMatch[0], '').trim();
  }

  // 清除残余的方括号标签（如 [1080p] 等）
  rest = rest.replace(/\[[^\]]*\]/g, '').trim();

  // 清除残余的圆括号标签
  rest = rest.replace(/[（(][^)）]*[)）]/g, '').trim();

  // 清除尾部多余的分隔符
  rest = rest.replace(/[-–—_.\s]+$/, '').trim();

  return { title: rest, year, tmdb };
}

/**
 * 从季目录名中提取季号。
 *
 * 支持格式：
 * - "Season 3" / "season3" / "Season 03"
 * - "S03" / "s3"
 * - "第3季" / "第三季"
 * - "Specials" → 0
 *
 * @param {string} dirName - 季目录名
 * @returns {number|null} 季号，无法确定返回 null
 *
 * @example
 * seasonNumber('Season 3')  // → 3
 * seasonNumber('S03')       // → 3
 * seasonNumber('Specials')  // → 0
 */
export function seasonNumber(dirName) {
  if (!dirName || typeof dirName !== 'string') return null;

  const name = dirName.trim();

  // "Specials" / "SP" / "特别篇" → Season 0
  if (/^(?:Specials?|SP|特别篇)$/i.test(name)) return 0;

  // "Season XX" 格式
  const seasonMatch = name.match(/^Season\s*(\d{1,3})$/i);
  if (seasonMatch) return Number(seasonMatch[1]);

  // "SXX" 格式（必须是整个目录名，避免误匹配）
  const sMatch = name.match(/^S(\d{1,3})$/i);
  if (sMatch) return Number(sMatch[1]);

  // 中文 "第X季"
  const cnMatch = name.match(/^第\s*(\d{1,3})\s*季$/);
  if (cnMatch) return Number(cnMatch[1]);

  // 中文数字 "第X季"
  const cnCharMatch = name.match(/^第\s*([零〇一二两三四五六七八九十]+)\s*季$/);
  if (cnCharMatch) {
    return cnCharToInt(cnCharMatch[1]);
  }

  return null;
}

/**
 * 简单中文数字转整数（复用逻辑，支持到 99）。
 * @param {string} cn
 * @returns {number|null}
 */
function cnCharToInt(cn) {
  const map = { 零: 0, 〇: 0, 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10 };
  const chars = [...cn];
  if (chars.length === 1) return map[chars[0]] ?? null;
  if (chars[0] === '十') return 10 + (map[chars[1]] ?? 0);
  const tenIdx = chars.indexOf('十');
  if (tenIdx > 0) {
    const tens = map[chars[tenIdx - 1]] ?? 0;
    const ones = tenIdx + 1 < chars.length ? (map[chars[tenIdx + 1]] ?? 0) : 0;
    return tens * 10 + ones;
  }
  return null;
}

/**
 * 清除标题中的地区标签。
 *
 * 例如 "(台)" "(港)" 等地区/译名标记会干扰标题匹配，需要移除。
 *
 * @param {string} value - 原始标题文本
 * @returns {string} 清理后的标题
 *
 * @example
 * cleanTitleText('进击的巨人(台)')  // → '进击的巨人'
 */
export function cleanTitleText(value) {
  if (!value || typeof value !== 'string') return '';
  return value.replace(REGION_TAG_REGEX, '').trim();
}

/**
 * 比较两个标题是否指向同一部剧。
 *
 * 处理逻辑：
 * 1. 规范化两端文本
 * 2. 移除特殊后缀（特别篇/总集篇等）
 * 3. 处理"双标题"情况（标题重复出现，如 "龙珠龙珠" 实际是 "龙珠"）
 * 4. 比较结果
 *
 * @param {string} a - 标题 A
 * @param {string} b - 标题 B
 * @param {(text: string) => string} norm - 规范化函数（由 createNormalizer 创建）
 * @returns {boolean} 是否匹配
 *
 * @example
 * const norm = createNormalizer();
 * sameTitle('龍珠', '龙珠', norm)          // → true（繁简一致）
 * sameTitle('进击的巨人 特别篇', '进击的巨人', norm) // → true
 */
export function sameTitle(a, b, norm) {
  if (!a || !b || !norm) return false;

  let na = norm(cleanTitleText(a));
  let nb = norm(cleanTitleText(b));

  // 空串不匹配
  if (!na || !nb) return false;

  // 直接相等
  if (na === nb) return true;

  // 移除特殊后缀后再比较
  na = na.replace(SPECIAL_SUFFIXES, '');
  nb = nb.replace(SPECIAL_SUFFIXES, '');
  if (na === nb) return true;

  // 双标题检测：如果一个标题恰好是另一个的两倍重复
  if (isDuplicateTitle(na, nb)) return true;
  if (isDuplicateTitle(nb, na)) return true;

  // 子串包含检测（较长标题包含较短标题，且较短标题长度 >= 2）
  if (na.length >= 2 && nb.length >= 2) {
    if (na.length > nb.length && na.includes(nb) && nb.length >= na.length * 0.5) return true;
    if (nb.length > na.length && nb.includes(na) && na.length >= nb.length * 0.5) return true;
  }

  return false;
}

/**
 * 检测 longer 是否是 shorter 的双重重复。
 * 例如: "龙珠龙珠" 与 "龙珠" → true
 * @param {string} longer
 * @param {string} shorter
 * @returns {boolean}
 */
function isDuplicateTitle(longer, shorter) {
  if (longer.length !== shorter.length * 2) return false;
  return longer === shorter + shorter;
}
