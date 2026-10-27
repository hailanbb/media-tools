(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.MPMaintenanceCore = factory();
})(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';

  const SEARCH_SITES = Object.freeze(['天空', '观众', '馒头']);
  const SITES = new Set(SEARCH_SITES);
  const clean = value => String(value == null ? '' : value).normalize('NFKC').replace(/\s+/gu, ' ').trim();

  function normalizeTitle(value) { return clean(value); }

  function sizeBytes(value) {
    const text = clean(value);
    const m = /^(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$/iu.exec(text);
    if (!m) return Infinity;
    const unit = m[2].toLowerCase();
    const base = /^k|^m|^g|^t/.test(unit) && /ib$/i.test(m[2]) ? 1024 : 1000;
    const power = {b:0,kb:1,mb:2,gb:3,tb:4,kib:1,mib:2,gib:3,tib:4}[unit];
    return Number(m[1]) * Math.pow(base, power);
  }

  function parseSubscription(input) {
    if (!input || !Number.isInteger(input.index) || input.index < 0) throw new Error('订阅 index 无效');
    const displayName = clean(input.name);
    const m = /^(.*?)\s*S(\d{1,3})$/iu.exec(displayName);
    if (!m || !clean(m[1])) throw new Error('订阅名称缺少有效季号 Sxx');
    const name = clean(m[1]);
    const season = Number(m[2]);
    const yearText = clean(input.year);
    if (!/^\d{4}$/u.test(yearText)) throw new Error('订阅年份无效');
    const year = Number(yearText);
    const progress = clean(input.progress);
    const p = /^(\d+)\s*\/\s*(\d+)$/u.exec(progress);
    if (!p) throw new Error('订阅进度无效');
    const downloaded = Number(p[1]), total = Number(p[2]);
    if (!Number.isSafeInteger(downloaded) || !Number.isSafeInteger(total) || total <= 0) {
      throw new Error('订阅进度数值无效');
    }
    return {index: input.index, displayName, name, season, year, downloaded, total,
      key: `${name}|S${String(season).padStart(2, '0')}|${year}`};
  }

  function classify(row) {
    if (row.index < 100) return 'protected';
    if (row.year >= 2026) return 'skip-year';
    if (row.downloaded === row.total) return 'complete';
    if (row.downloaded === 0) return 'cancel-zero';
    return 'download-then-cancel';
  }

  function chooseResource(list, job) {
    const allowedSites=Array.isArray(job?.sites)?new Set(job.sites):SITES;
    const title = normalizeTitle(job && (job.name || job.displayName));
    const season = Number(job && job.season);
    const matching = (Array.isArray(list) ? list : []).filter(item => {
      const rawSeeders = item && item.seeders;
      const seeders = (typeof rawSeeders === 'number' || (typeof rawSeeders === 'string' && rawSeeders.trim() !== '')) ? Number(rawSeeders) : NaN;
      return item && allowedSites.has(item.site) && normalizeTitle(item.title) === title &&
        Number.isInteger(item.season) && item.season === season && Number.isFinite(seeders) &&
        Number.isInteger(seeders) && seeders >= 0;
    });
    const hasConflict = matching.some(item => item.total != null && item.total !== Number(job.total));
    const candidates = matching.filter(item => hasConflict ? item.total === Number(job.total) :
      (job.total == null || item.total == null || item.total === Number(job.total)));
    if (!candidates.length) throw new Error('已选站点没有匹配的整季资源');
    const wellSeeded=candidates.filter(item=>Number(item.seeders)>=5);
    const pool=wellSeeded.length?wellSeeded:candidates;
    return pool.map((item, index) => ({item, index})).sort((a, b) => {
      const seedDiff = Number(b.item.seeders) - Number(a.item.seeders);
      const sizeDiff = sizeBytes(a.item.size) - sizeBytes(b.item.size);
      return (wellSeeded.length?(sizeDiff||seedDiff):(seedDiff||sizeDiff)) || a.index - b.index;
    })[0].item;
  }

  const md = value => String(value == null ? '' : value).replace(/([\\`*_{}\[\]()#+.!|>])/g, '\\$1').replace(/\r?\n/gu, ' ');
  const actionLabel=value=>({'skip-name':'自定义名单跳过',protected:'保护前100项',invalid:'信息无法识别','skip-year':'跳过2026年及之后',complete:'集数相等','cancel-zero':'零下载取消订阅','download-then-cancel':'搜索下载后取消订阅'}[value]||value);
  function reportMarkdown(report) {
    const r = report || {}, records = Array.isArray(r.records) ? r.records : [];
    const lines = ['# MoviePilot 订阅维护记录', '', `- 记录 ID：${md(r.id)}`, `- 开始：${md(r.startedAt)}`, `- 结束：${md(r.endedAt)}`, `- 状态：${md(r.status)}`, '', '启动时前 100 项（第 1–100 张）受保护，未执行取消或下载操作。', '', '| 原始序号 | 名称 | 季 | 年 | 已下载/总数 | 动作 | 状态 | 详情 | 资源 | 站点 | 做种数 | 时间 |', '|---:|---|---:|---:|---:|---|---|---|---|---|---:|---|'];
    for (const x of records) lines.push(`| ${md(x.index+1)} | ${md(x.name)} | ${md(x.season)} | ${md(x.year)} | ${md(x.downloaded==null?'':`${x.downloaded}/${x.total}`)} | ${md(actionLabel(x.action))} | ${md(x.status)} | ${md(x.detail)} | ${md(x.resourceTitle)} | ${md(x.site)} | ${md(x.seeders)} | ${md(x.time)} |`);
    return lines.join('\n');
  }

  function csvCell(value) {
    let s = String(value == null ? '' : value);
    if (/^[\s\t]*[=+\-@]/u.test(s)) s = `'${s}`;
    return /[",\r\n]/u.test(s) ? `"${s.replace(/"/gu, '""')}"` : s;
  }
  function reportCSV(report) {
    const r = report || {}, rows = [['记录ID','开始时间','结束时间','任务状态','原始序号','名称','季','年','已下载','总数','动作','条目状态','详情','资源标题','站点','做种数','时间']];
    for (const x of Array.isArray(r.records) ? r.records : []) rows.push([r.id,r.startedAt,r.endedAt,r.status,x.index+1,x.name,x.season,x.year,x.downloaded,x.total,actionLabel(x.action),x.status,x.detail,x.resourceTitle,x.site,x.seeders,x.time]);
    if(rows.length===1)rows.push([r.id,r.startedAt,r.endedAt,r.status,'','','','','','','','未处理','尚未完成清单读取','','','','']);
    return '\uFEFF' + rows.map(row => row.map(csvCell).join(',')).join('\r\n') + '\r\n';
  }

  return {SEARCH_SITES, normalizeTitle, parseSubscription, classify, chooseResource, reportMarkdown, reportCSV};
});
