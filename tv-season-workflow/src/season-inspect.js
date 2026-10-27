// ==UserScript==
// @name         剧集工作流 ① · 初检（只读）
// @namespace    local.tv-season-inspect
// @version      1.2.0
// @description  手动初检；TMDB 主查、豆瓣分季缓存与逐页限速，风控暂停、待补查和进度恢复。无云端写操作。
// @match        https://clouddrive.example/*
// @match        https://movie.douban.com/*
// @match        https://search.douban.com/movie/subject_search*
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_deleteValue
// @grant        GM_openInTab
// @grant        GM_xmlhttpRequest
// @connect      api.themoviedb.org
// @run-at       document-idle
// @noframes
// ==/UserScript==

/* 四阶段工作流 ①：只读初检。
 * 安装默认停止；手动选择分类，按修改时间从旧到新逐剧、逐季扫描。
 * TMDB ID 优先；来源不可用、来源异常或当季状态不明时使用豆瓣。本地缺集本身不触发复核。
 * 豆瓣结果分季缓存，搜索和详情分别限速；风控停止豆瓣队列，云盘扫描继续。
 * 绿色＝本季播完且齐全；蓝色＝本季连载。集数、播出状态和本地完整性独立记录。
 * 此脚本没有云端写操作，也不包含第二阶段下载或第三阶段删除功能。
 * 修改 src 源码后运行 build.cjs；生成文件中的第三方许可必须保留。
 */
(function () {
  'use strict';
  // 首次安装只改这里的站点地址、源路径，以及顶部 @match（域名不带端口）。
  const CLOUD_ORIGIN = 'https://clouddrive.example';
  const CLOUD_HOSTNAME = new URL(CLOUD_ORIGIN).hostname;
  const SOURCE = '/115/Library_incs/nas/电视剧';
  const toSimplified = /* build:opencc */ null;
  let excelLibrary;
  function excelEngine() { return excelLibrary ||= /* build:xlsx */ null; }
  const NS = 'tv-inspect-v1:';
  const MEDIA = /\.(?:mp4|mkv|avi|mov|m4v|ts|m2ts|wmv|flv|webm|mpg|mpeg|strm)$/i;
  const norm = s => toSimplified(String(s || '').normalize('NFKC')).replace(/[\u200b-\u200f\u202a-\u202e\ufeff]/g, '').replace(/[\s\p{P}\p{S}]/gu, '').toLowerCase();
  const parent = p => p.slice(0, p.lastIndexOf('/'));
  const base = p => p.slice(p.lastIndexOf('/') + 1);
  const mapKey = (show, season) => `${show.tmdb || norm(show.title) + ':' + show.year}:S${season}`;
  const seasonNumber = s => /^Season\s+(\d+)$/i.exec(s.trim())?.[1] * 1 || null;
  function showInfo(name) {
    const year = /[（(]((?:19|20)\d{2})[)）]/.exec(name)?.[1] || '';
    const tmdb = /\[tmdbid=(\d+)\]/i.exec(name)?.[1] || '';
    const title = name.replace(/\[tmdbid=\d+\]/ig, '').replace(/[（(](?:19|20)\d{2}[)）]/g, '').trim();
    return { title, year, tmdb };
  }
  function chineseNumber(s) {
    if (/^\d+$/.test(s)) return Number(s);
    const d = { '零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9 };
    if (s === '十') return 10;
    if (s.includes('十')) { const [a, b] = s.split('十'); return (a ? d[a] : 1) * 10 + (b ? d[b] : 0); }
    return d[s] ?? null;
  }
  function titleIdentity(text) {
    const clean = String(text).replace(/[\u200b-\u200f]/g, '').trim();
    const year = /[（(]((?:19|20)\d{2})[)）]/.exec(clean)?.[1] || '';
    const cn = /第\s*([\d一二三四五六七八九十两]+)\s*季/.exec(clean);
    const en = /\bSeason\s+(\d+)\b/i.exec(clean);
    const season = cn ? chineseNumber(cn[1]) : en ? Number(en[1]) : null;
    const title = clean.replace(/[（(](?:19|20)\d{2}[)）]/g, '').replace(/第\s*[\d一二三四五六七八九十两]+\s*季/g, '').replace(/\bSeason\s+\d+\b/ig, '').trim();
    return { title, year, season };
  }
  // 供各阶段共用的片名文本清理；不处理云盘路径，不移除季号/年份/特别篇标签。
  function cleanTitleText(value) {
    return String(value||'').normalize('NFKC').replace(/[\u200b-\u200f\u202a-\u202e\ufeff]/g,'')
      .replace(/\s*\((?:台|臺|港|台湾|臺灣|香港|大陆|大陸|中国大陆|中國大陸)\)\s*$/u,'').trim();
  }
  function sameTitle(candidateTitle, expectedTitle, allowHanPair = false) {
    const title = cleanTitleText(titleIdentity(candidateTitle).title);
    const c = norm(title), s = norm(cleanTitleText(titleIdentity(expectedTitle).title));
    if (!s) return false;
    if (c === s || c === s + s) return true;
    if (/(?:特别篇|特別篇|总集篇|總集篇|剧场版|劇場版|スペシャル)/.test(title)) return false;
    // 简繁在 norm 中统一；只在完整双语片名之间分段，不把英文单词前缀当作剧名。
    // 中文名允许 X医生 这类拉丁字母混排；外文段须有日/韩/拉丁文字证据。
    // 纯汉字中日双标题仍只在搜索时暂准，详情须有日本制片证据。
    const chinese = value => /\p{Script=Han}/u.test(value) && /^[\p{Script=Han}\p{Script=Latin}\p{M}\s\p{P}\p{N}]+$/u.test(value) &&
      !value.split(/\s+/).some(word=>/\p{Script=Latin}/u.test(word)&&!/\p{Script=Han}/u.test(word));
    const foreign = value => {
      value=value.normalize('NFKC'); // 半角片假名统一；日文长音符属于 Common，需要明确允许。
      if(/[\p{Script=Hiragana}\p{Script=Katakana}]/u.test(value)&&/^[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\u30fc\p{M}\s\p{P}\p{N}]+$/u.test(value))return 'ja';
      if(/\p{Script=Hangul}/u.test(value)&&/^[\p{Script=Hangul}\p{M}\s\p{P}\p{N}]+$/u.test(value))return 'ko';
      if(/\p{Script=Latin}/u.test(value)&&/^[\p{Script=Latin}\p{M}\s\p{P}\p{N}]+$/u.test(value))return 'latin';
      return null;
    };
    // 片名内部也可混排英文：“睁眼一看是 SUPER TV / 눈 떠보니 슈퍼TV”。
    // 外文段必须从日韩文字开始，防止把未匹配的英文尾词挪进后一片名。
    const mixedChinese=value=>/\p{Script=Han}/u.test(value)&&/^[\p{Script=Han}\p{Script=Latin}\p{M}\s\p{P}\p{N}]+$/u.test(value);
    const mixedOriginal=value=>(/^\p{Script=Hangul}/u.test(value)&&/^[\p{Script=Hangul}\p{Script=Latin}\p{M}\s\p{P}\p{N}]+$/u.test(value))||
      (/^[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/u.test(value)&&/[\p{Script=Hiragana}\p{Script=Katakana}]/u.test(value)&&/^[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Latin}\u30fc\p{M}\s\p{P}\p{N}]+$/u.test(value));
    return [...title.matchAll(/\s+/g)].some(m => {
      const left = title.slice(0, m.index).trim(), right = title.slice(m.index + m[0].length).trim();
      const lf=foreign(left),rf=foreign(right);
      return !!left && !!right && ((chinese(left)&&rf)||(lf&&chinese(right))||(lf&&rf&&lf!==rf)||
        (mixedChinese(left)&&mixedOriginal(right))||(mixedOriginal(left)&&mixedChinese(right))||
        (allowHanPair && /^[\p{Script=Han}\s\p{P}\p{N}]+$/u.test(left + right))) &&
        (norm(left) === s || norm(right) === s);
    });
  }
  function candidateMatches(candidate, show, season, { inspectDetails = false } = {}) {
    // 搜索阶段可以进入详情核验，但最终必须确认具体季；相似标题不能替代身份匹配。
    const c = titleIdentity(candidate.title);
    const namesMatch = [candidate.title, ...(candidate.aliases || [])].some(t => sameTitle(t, show.title, inspectDetails || candidate.japanese === true));
    const seasonMatch = season === 1 ? c.season === null || c.season === 1 : c.season === season;
    const yearMatch = !!show.year && (c.year === show.year || (candidate.releaseYears || []).includes(show.year));
    return !!candidate.tv && namesMatch && seasonMatch && (season !== 1 || inspectDetails || yearMatch);
  }
  function chooseCandidate(candidates, show, season, options = {}) {
    const matches = [...new Map(candidates.filter(c => candidateMatches(c, show, season, options)).map(c => [c.url, c])).values()];
    return matches.length === 1 ? matches[0] : null;
  }
  function matchFailure(candidates, show, season, detail = false) {
    if (!candidates.length) return '豆瓣未返回可读取的剧集候选';
    if (!candidates.some(c => c.tv)) return '搜索候选没有剧集标记，无法确认类型';
    if (!candidates.some(c => [c.title, ...(c.aliases || [])].some(t => sameTitle(t, show.title)))) return '剧名或别名不一致（已处理简繁体及重复双标题）';
    const matching = candidates.filter(c => candidateMatches(c, show, season, { inspectDetails: true }));
    if (!matching.length) return `候选季数与 Season ${season} 不一致，需要核实分季或改名情况`;
    if (new Set(matching.map(c => c.url)).size > 1) return '存在多个同名同季条目，无法唯一确认';
    if (detail && season === 1) return `目录年份 ${show.year || '缺失'} 与豆瓣标题及首播年份均不一致`;
    return '需要人工核实豆瓣条目';
  }
  function episodeFromName(name, season) {
    // “S01E01 - 01.mkv”尾部重复数字是同一集；真正的合并集、跨季及歧义编号不猜测。
    const pairs = [...name.matchAll(/(?:^|[^a-z0-9])S(\d{1,3})[ ._-]*E(\d{1,4})(?!\d)/ig)];
    if (pairs.length) {
      const ids = [...new Set(pairs.map(m => `${Number(m[1])}:${Number(m[2])}`))];
      if (ids.length !== 1 || Number(pairs[0][1]) !== season) return null;
      // An explicit second E is still a combined episode. Permit only an exact
      // repeated numeric suffix at the end: "S01E01 - 01.mkv" is one episode.
      if (/(?:S\d+[ ._-]*E\d+)[ ._-]*E\d+/i.test(name)) return null;
      for (const pair of pairs) {
        const tail = name.slice(pair.index + pair[0].length);
        if (/^\s*[-~～]\s*\d/.test(tail)) {
          const repeated = /^\s+-\s+(\d{1,4})\.[a-z0-9]+$/i.exec(tail);
          if (!repeated || Number(repeated[1]) !== Number(pair[2])) return null;
        }
      }
      return Number(pairs[0][2]) || null;
    }
    const cn = [...name.matchAll(/第\s*(\d{1,4})\s*集/g)];
    if (cn.length === 1) return Number(cn[0][1]) || null;
    const en = [...name.matchAll(/(?:^|[\s._-])(?:EP?|Episode)[ ._-]*(\d{1,4})(?!\d)/ig)];
    if (en.length === 1) return Number(en[0][1]) || null;
    // Only accept a purely numeric basename; never guess from year/resolution/release tags.
    const numeric = /^(\d{1,4})\.[a-z0-9]+$/i.exec(name);
    return numeric ? Number(numeric[1]) || null : null;
  }
  function auditEpisodes(rows, season, total) {
    // 数量相等不足以证明齐全：先去重集号，再检查缺集、越界和无法识别文件。
    const videos = rows.filter(r => !r.directory && MEDIA.test(r.name));
    const unknown = [], counts = new Map();
    for (const r of videos) {
      const ep = episodeFromName(r.name, season);
      if (!ep) unknown.push(r.name); else counts.set(ep, (counts.get(ep) || 0) + 1);
    }
    const episodes = [...counts.keys()].sort((a, b) => a - b);
    const duplicates = episodes.filter(n => counts.get(n) > 1);
    const validTotal = Number.isInteger(total) && total > 0 && total <= 10000;
    const missing = validTotal && !unknown.length ? Array.from({ length: total }, (_, i) => i + 1).filter(n => !counts.has(n)) : null;
    const extra = validTotal ? episodes.filter(n => n > total) : [];
    const nested = rows.filter(r => r.directory).map(r => r.name);
    const issues = [];
    if (unknown.length) issues.push(`有 ${unknown.length} 个视频无法识别集号`);
    if (duplicates.length) issues.push(`集号重复：${duplicates.join('、')}`);
    if (extra.length) issues.push(`超出来源登记集数：${extra.join('、')}`);
    if (nested.length) issues.push(`季内存在子目录：${nested.join('、')}`);
    if (!validTotal) issues.push('来源登记集数未确认');
    return { videoCount: videos.length, current: episodes.length, episodes, unknown, duplicates, extra, missing, issues,
      integrity: issues.length ? '待核实' : missing.length ? '缺集' : '齐全' };
  }
  // Broadcast evidence is independent of the local file count.
  function broadcastState(db) {
    return ['已播完','连载中','待播'].includes(db.broadcast) ? db.broadcast : '未确认';
  }
  function auditSource(files,season,db) {
    const audit=auditEpisodes(files,season,db.total);
    const future=new Set([...(db.futureEpisodes||[]),...(db.undatedEpisodes||[])]);
    audit.uncollected=audit.missing;
    if(audit.missing)audit.missing=audit.missing.filter(n=>!future.has(n));
    const dated=db.source==='tmdb'||db.airedEvidence===true;
    audit.airedMissing=dated?audit.missing:null;
    audit.broadcast=broadcastState(db);
    audit.airedIntegrity=dated&&!(db.undatedEpisodes||[]).length&&audit.missing!==null
      ? (audit.airedMissing.length?'已播缺集':'已播齐全') : '未确认';
    if(audit.integrity==='缺集'&&!audit.missing.length) audit.integrity=(db.undatedEpisodes||[]).length?'待核实':'已播齐全';
    return audit;
  }
  async function reviewIncomplete(files,season,db,queryDouban) {
    // 豆瓣数量覆盖本季判断；失败保留 TMDB 原值，数量/状态冲突时不沿用完结结论。
    const audit=auditSource(files,season,db);
    if(db.source!=='tmdb'||(db.completionConfirmed&&!audit.extra.length&&!db.error))return {db,audit,review:null};
    const original={total:db.total,url:db.url,checkedAt:db.checkedAt,missing:audit.missing,broadcast:broadcastState(db)};
    const confirmed=await queryDouban();
    if(confirmed.error||!Number.isInteger(confirmed.total)||confirmed.total<1||confirmed.total>10000||!validSubjectUrl(confirmed.url)) {
      const reason=confirmed.error||'未取得有效的豆瓣本季集数';
      return {db:{...db,error:(confirmed.deferred?'豆瓣待补查，保留 TMDB 登记集数：':'豆瓣复核失败，保留 TMDB 登记集数及缺集供参考：')+reason,candidates:confirmed.candidates||[],pendingDouban:!!confirmed.deferred,retryAt:confirmed.retryAt||null},
        audit:{...audit,integrity:'待核实'},review:{state:confirmed.deferred?'pending':'failed',tmdb:original,reason}};
    }
    const sameCount=confirmed.total===db.total;
    const explicit=broadcastState(confirmed);
    const evidenceConflict=explicit!=='未确认'&&broadcastState(db)!=='未确认'&&explicit!==broadcastState(db);
    const broadcast=evidenceConflict?'未确认':explicit!=='未确认'?explicit:sameCount?broadcastState(db):'未确认';
    const keepSchedule=sameCount&&!evidenceConflict;
    const merged={...confirmed,source:'douban',tmdbUrl:db.url,broadcast,airedEvidence:keepSchedule,
      futureEpisodes:keepSchedule?db.futureEpisodes:[],undatedEpisodes:keepSchedule?db.undatedEpisodes:[],
      broadcastEvidence:evidenceConflict?'TMDB 与豆瓣播出状态冲突，需人工核实':explicit!=='未确认'?confirmed.broadcastEvidence:sameCount?db.broadcastEvidence:'两来源集数不同，不能沿用 TMDB 的播完判定',
      matchNote:[confirmed.matchNote,'已按豆瓣本季集数重新核对；播出状态单独判定'].filter(Boolean).join('；')};
    return {db:merged,audit:auditSource(files,season,merged),review:{state:'confirmed',tmdb:original}};
  }
  function parseDoubanBroadcast(text) {
    // Never infer completion from episode count, premiere date or synopsis.
    const m=/(?:^|\n)\s*(?:播出状态|完结状态)\s*[:：]\s*(已完结|已播完|连载中|播出中|未播出|待播)\s*(?:\n|$)/.exec(text);
    const broadcast=!m?'未确认':/已完结|已播完/.test(m[1])?'已播完':/连载中|播出中/.test(m[1])?'连载中':'待播';
    return {broadcast,broadcastEvidence:m?'豆瓣详情明确标注：'+m[1]:'豆瓣提供集数，但没有可确认的本季播出状态'};
  }
  function validSubjectUrl(value) {
    try { const u = new URL(value); return u.protocol === 'https:' && u.hostname === 'movie.douban.com' && !u.port && !u.username && !u.password && /^\/subject\/\d+\/$/.test(u.pathname) ? u.origin + u.pathname : null; } catch { return null; }
  }
  function parseTmdbSeason(series, data, show, season, checkedAt) {
    // 按本季季终标记判断，可正确处理“第一季结束但电视剧仍续订”的情况。
    if(String(series?.id)!==show.tmdb || data?.season_number!==season)throw new Error('TMDB 返回的剧集或季数不符');
    const summary=series.seasons?.find(s=>s.season_number===season), episodes=data.episodes;
    if(!summary || !Array.isArray(episodes) || !episodes.length || episodes.length>10000)throw new Error('TMDB 未收录可用的本季集目');
    const numbers=episodes.map(e=>e.episode_number).sort((a,b)=>a-b);
    if(episodes.some(e=>e.season_number!==season) || numbers.some((n,i)=>n!==i+1) || summary.episode_count!==episodes.length)throw new Error('TMDB 季集目不完整或登记数量不一致');
    const today=new Date(Date.parse(checkedAt)+8*3600*1000).toISOString().slice(0,10);
    const futureEpisodes=episodes.filter(e=>e.air_date && e.air_date>=today).map(e=>e.episode_number);
    const undatedEpisodes=episodes.filter(e=>!/^\d{4}-\d{2}-\d{2}$/.test(e.air_date||'')).map(e=>e.episode_number);
    const last=episodes.find(e=>e.episode_number===numbers.at(-1));
    const completionConfirmed=!futureEpisodes.length&&!undatedEpisodes.length&&(series.status==='Ended'||last?.episode_type==='finale');
    const started=episodes.some(e=>/^\d{4}-\d{2}-\d{2}$/.test(e.air_date||'')&&e.air_date<today);
    const broadcast=completionConfirmed?'已播完':futureEpisodes.length?(started?'连载中':'待播'):'未确认';
    const broadcastEvidence=completionConfirmed?'TMDB 剧集结束或本季季终标记，且本季所有集目播出日均早于核对日':
      broadcast==='连载中'?'TMDB 本季有已播集目和今天或未来播出的集目':broadcast==='待播'?'TMDB 本季尚无早于核对日的已播集目':'TMDB 未提供足够的本季播完证据';
    return {broadcast,broadcastEvidence,source:'tmdb',total:episodes.length,title:series.name||show.title,url:`https://www.themoviedb.org/tv/${show.tmdb}/season/${season}`,checkedAt,
      registeredEpisodes:numbers,futureEpisodes,undatedEpisodes,completionConfirmed,sourceStatus:String(series.status||''),
      matchNote:completionConfirmed?'TMDB 已结束状态或季终标记，且登记集目均早于核对日':'TMDB 当前登记集目；全季完结尚未确认'};
  }
  async function preferTmdb(loadTmdb,loadDouban) {
    let failure;
    try {return await loadTmdb();} catch(e) {if(e.name==='Paused')throw e;failure=e.message;}
    const result=await loadDouban();
    const valid=!result.error&&Number.isInteger(result.total)&&result.total>0&&result.total<=10000&&validSubjectUrl(result.url);
    return {...result,total:valid?result.total:null,source:'douban',fallbackReason:failure,error:valid?'':result.error||'豆瓣未提供有效本季集数和链接'};
  }
  function tmdbRequestSpec(path, credentials, useKey=false) {
    if(!/^\/(?:authentication|tv\/\d+(?:\/season\/\d+)?)$/.test(path))throw new Error('TMDB 接口路径不在允许范围内');
    const token=String(credentials.token||'').trim(),key=String(credentials.key||'').trim();
    const url=new URL('https://api.themoviedb.org/3'+path);url.searchParams.set('language','zh-CN');
    const headers={accept:'application/json'};
    if(token&&!useKey)headers.Authorization='Bearer '+token;
    else if(key)url.searchParams.set('api_key',key);
    else throw new Error('尚未填写 TMDB 读访问令牌或 API 密钥');
    return {url:url.href,headers};
  }
  function parseTotal(text) { const m = /(?:^|\n)\s*集数\s*[:：]\s*(\d+)\s*(?:\n|$)/m.exec(text); return m && Number(m[1]) > 0 && Number(m[1]) <= 10000 ? Number(m[1]) : null; }
  function doubanAccessProblem({ text = '', hasContent = false, hasLoginForm = false }) {
    if (!hasContent && (hasLoginForm || /请先登录|登录后(?:才能|即可|继续)|需要登录才能|登录豆瓣帐号/.test(text))) return '豆瓣需要登录。请先在豆瓣网页手动登录，再回到云存储确认准备完成并继续。';
    if (!hasContent && /验证码|异常请求|访问过于频繁|检测到有异常请求|访问豆瓣的方式有点像机器人/.test(text)) return '豆瓣要求人工验证。请在豆瓣网页手动完成后，再回到云存储确认准备完成并继续。';
    return null;
  }
  function validateListing(rows, path, count, {emptyConfirmed=false}={}) {
    if (!Number.isInteger(count) || count < 0 || (!count&&!emptyConfirmed) || rows.length !== count) throw new Error('列表尚未加载完成、空目录未确认或未读取全部条目');
    if (new Set(rows.map(r => r.path)).size !== count || rows.some(r => parent(r.path) !== path || base(r.path) !== r.name)) throw new Error('目录路径与列表不一致');
    return rows;
  }
  function createListingSettler(path) {
    // 只有明确空目录提示及稳定列表才接受 0；非空目录必须确认读取全部条目。
    let previous='',since=0;
    return ({rows,count,loading=false,emptyConfirmed=false,allSelected=false},at) => {
      const reset=()=>{previous='';since=at;return null;};
      if(loading || (count!==0&&!allSelected))return reset();
      try{validateListing(rows,path,count,{emptyConfirmed});}catch{return reset();}
      const fingerprint=JSON.stringify(rows);
      if(fingerprint!==previous){previous=fingerprint;since=at;return null;}
      return at-since>=(count===0?600:1800)?rows:null;
    };
  }
  function csvCell(value) {
    let s = value == null ? '' : Array.isArray(value) ? value.join('、') : String(value);
    if (/^[\s]*[=+@-]/.test(s)) s = "'" + s;
    return '"' + s.replace(/"/g, '""') + '"';
  }
  function cloudLink(path) {
    const p=String(path||'').replace(/^Root(?=\/)/,'');
    if(!p.startsWith(SOURCE+'/')||p.split('/').some(s=>s==='.'||s==='..'))return '';
    // Keep Chinese names readable and shorter for spreadsheet hyperlinks; encode
    // query delimiters, quotes, percent signs and spaces so the path stays one value.
    return CLOUD_ORIGIN+'/?page=files&path='+p.replace(/[^\p{L}\p{N}\/._~()-]/gu,c=>encodeURIComponent(c));
  }
  function csvCloudLink(path) {
    const url=cloudLink(path);
    if(!url)return csvCell('');
    // Only this generated, fixed-origin URL may become a formula. All other cells
    // retain csvCell's formula-injection protection. Display the actual URL as well.
    return '"'+(`=HYPERLINK("${url}","${url}")`).replace(/"/g,'""')+'"';
  }
  const REPORT_FIELDS=[
    ['分类','category'],['剧集目录','showName'],['季数','season'],['源云存储链接','cloudLink'],
    ['视频文件数','videoCount'],['已识别集数','current'],['来源登记集数','total'],['缺失集号','missing'],
    ['数据来源','source'],['集数核对','integrity'],['播出状态','broadcast'],['处理结果','status'],
    ['来源链接','sourceUrl'],['来源查询时间','checkedAt'],['详情标题','sourceTitle'],['核对说明','note'],
    ['已识别集号','episodes'],['重复集号','duplicates'],['无法识别集号的文件','unknown'],['尚未收齐集号（含待播）','uncollected'],
    ['待播集号','futureEpisodes'],['播期未知集号','undatedEpisodes'],['已播集数核对','airedIntegrity'],
    ['播出判定依据','broadcastEvidence'],['豆瓣链接','doubanUrl'],['TMDB链接','tmdbUrl'],
    ['剧集目录路径','showPath'],['季目录路径','path'],['TMDB ID','tmdbId'],
    ['记录ID','recordId'],['报告版本','reportVersion'],['工作阶段','stage'],['扫描时间','scannedAt']
  ];
  const REPORT_VERSION='tv-workflow/1';
  function modifiedTime(value) {
    const m=/^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value||'');
    if(!m)return NaN;
    const [,y,mo,d,h,mi,se='00']=m, date=new Date(`${y}-${mo}-${d}T${h}:${mi}:${se}+08:00`);
    if(!Number.isFinite(+date))return NaN;
    const local=new Date(+date+8*3600*1000).toISOString().slice(0,19);
    return local===`${y}-${mo}-${d}T${h}:${mi}:${se}`?+date:NaN;
  }
  function sortModified(rows) {
    // 无效时间中止该列表处理，避免悄悄按名称改序；相同时间再以季号和名称打破平局。
    if(rows.some(r=>!Number.isFinite(modifiedTime(r.modified))))throw Error('部分目录修改时间无法解析，已暂停以免顺序错误');
    return [...rows].sort((a,b)=>modifiedTime(a.modified)-modifiedTime(b.modified)||(seasonNumber(a.name)||0)-(seasonNumber(b.name)||0)||a.name.localeCompare(b.name));
  }
  function resultStatus(r) {
    if(r.broadcast==='连载中')return '连载中';
    if(r.broadcast==='待播')return '待播';
    if(r.integrity==='齐全')return r.broadcast==='已播完'?'齐全':'集号齐全，播出状态待核实';
    return r.integrity;
  }
  function resultColor(r) {
    // 颜色只作用于剧集目录格。播出中即蓝；绿色还要求无重复、无未知集号等异常。
    if(r.broadcast==='连载中')return {fill:'BDD7EE',font:'1F4E78'};
    if(r.broadcast==='已播完'&&r.integrity==='齐全'&&r.status==='齐全')return {fill:'C6EFCE',font:'006100'};
    return null;
  }
  function reportValue(r,key) {
    const value=key==='cloudLink'?cloudLink(r.path):key==='missing'?(r.missing==null?'未确认':r.missing.length?r.missing:'无'):r[key];
    return value==null?'':Array.isArray(value)?value.join('、'):value;
  }
  function resultCsv(results) {
    return '\ufeff'+[REPORT_FIELDS.map(([name])=>csvCell(name)).join(','),...results.map(r=>REPORT_FIELDS.map(([,key])=>key==='cloudLink'?csvCloudLink(r.path):csvCell(reportValue(r,key))).join(','))].join('\r\n')+'\r\n';
  }
  function resultWorkbook(results) {
    // 文件清单分片到每格30000字符，保留长季清单；不改变既有列位置。
    const split=text=>{const parts=[];for(let start=0;start<text.length;){let end=Math.min(start+30000,text.length);if(end<text.length&&/[\uD800-\uDBFF]/.test(text[end-1]))end--;parts.push(text.slice(start,end));start=end;}return parts;};
    const snapshots=results.map(r=>split(r.versionSnapshot||'')),chunks=Math.max(0,...snapshots.map(p=>p.length));
    const fields=[...REPORT_FIELDS,...Array.from({length:chunks},(_,i)=>[i?'版本文件清单续'+i:'版本文件清单','versionChunk'+i])];
    results=results.map((r,index)=>({...r,...Object.fromEntries(Array.from({length:chunks},(_,i)=>['versionChunk'+i,snapshots[index][i]||'']))}));
    const X=excelEngine(),book=X.utils.book_new();
    const sheet=X.utils.aoa_to_sheet([fields.map(([name])=>name),...results.map(r=>fields.map(([,key])=>reportValue(r,key)))]);
    const widths=fields.map(([label])=>/链接|路径|依据|说明|文件|记录ID/.test(label)?52:/标题|目录/.test(label)?40:18);
    sheet['!cols']=widths.map(wch=>({wch}));sheet['!rows']=[{hpt:30},...results.map(()=>({hpt:42}))];
    sheet['!autofilter']={ref:`A1:${X.utils.encode_col(fields.length-1)}${results.length+1}`};
    for(let row=0;row<=results.length;row++)for(let col=0;col<fields.length;col++) {
      const cell=sheet[X.utils.encode_cell({r:row,c:col})];if(!cell)continue;
      cell.s={font:{name:'Microsoft YaHei',sz:11,color:{rgb:row===0?'FFFFFF':'243746'}},alignment:{vertical:'center',wrapText:true}};
      if(row===0){cell.s.fill={patternType:'solid',fgColor:{rgb:'24546A'}};cell.s.font.bold=true;continue;}
      const color=col===1?resultColor(results[row-1]):null;
      if(color){cell.s.fill={patternType:'solid',fgColor:{rgb:color.fill}};cell.s.font.color={rgb:color.font};}
      if([2,4,5,6].includes(col)&&cell.v!==''){cell.t='n';cell.v=Number(cell.v);cell.s.numFmt='0';}
      if(['cloudLink','sourceUrl','doubanUrl','tmdbUrl'].includes(fields[col][1])&&/^https:\/\//.test(String(cell.v))) {cell.l={Target:String(cell.v)};cell.s.font.color={rgb:'0563C1'};cell.s.font.underline=true;}
    }
    X.utils.book_append_sheet(book,sheet,'剧集检查');return book;
  }
  function resultXlsx(results) {
    return excelEngine().write(resultWorkbook(results),{bookType:'xlsx',type:'array',compression:true});
  }
  function clampPosition(position, width, height, viewportWidth, viewportHeight) {
    return { x: Math.max(0, Math.min(Number.isFinite(position.x) ? position.x : 18, Math.max(0,viewportWidth-width))),
      y: Math.max(0, Math.min(Number.isFinite(position.y) ? position.y : 18, Math.max(0,viewportHeight-height))) };
  }
  const Core = { SOURCE, REPORT_FIELDS, REPORT_VERSION, sortModified, modifiedTime, resultStatus, resultColor, broadcastState, parseDoubanBroadcast, norm, parent, base, mapKey, seasonNumber, showInfo, titleIdentity, sameTitle, candidateMatches, chooseCandidate, matchFailure, episodeFromName, auditEpisodes,auditSource,reviewIncomplete,validSubjectUrl,parseTmdbSeason,preferTmdb,tmdbRequestSpec,parseTotal, doubanAccessProblem, validateListing,createListingSettler, csvCell,cloudLink,csvCloudLink,resultCsv,resultWorkbook,resultXlsx,clampPosition };
  Core.createDoubanPolicy=/* build:douban-policy */ null;
  Core.createDoubanQuery=/* build:douban-query */ null;
  if (typeof module !== 'undefined' && module.exports) { module.exports = Core; return; }

  const get = (key, fallback) => GM_getValue(NS + key, fallback);
  const put = (key, value) => GM_setValue(NS + key, value);
  const del = key => GM_deleteValue(NS + key);
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const now = () => new Date().toISOString();
  const uuid = () => crypto.randomUUID();
  const visible = e => !!e && !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  function setInput(input, value) {
    const proto = input.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }
  function blockedDouban() {
    return doubanAccessProblem({ text: document.body.innerText.slice(0,6000),
      hasContent: !!document.querySelector('#info, .item-root .title a.title-text'),
      hasLoginForm: [...document.querySelectorAll('input[type="password"], iframe[src*="accounts.douban.com/passport"]')].some(visible) });
  }
  async function doubanWorker() {
    const hash = new URLSearchParams(location.hash.slice(1));
    const id = hash.get('tv_audit');
    if (!id || !/^[0-9a-f-]{36}$/.test(id)) return;
    const request = get('request:' + id, null);
    if (!request || Date.now() - request.created > 180000 || get('response:' + id, null)) return;
    const reply = value => { if (get('request:' + id, null)) {
      // 子页先落盘风控信号，即使云盘页同时刷新，也不会丢失冷却要求。
      if(request.paced&&value.blocked){
        // 网页若明确要求更长等待，遵守可读取的时长；不额外发 HTTP 请求探测响应头。
        const wait=/(?:请在|请于|等待|wait|retry after|try again in)\s*(\d{1,4})\s*(小时|小時|分钟|分|秒|hours?|minutes?|seconds?)/i.exec(document.body.innerText.slice(0,6000));
        const unit=wait?.[2]||'',duration=wait?Number(wait[1])*(/小时|小時|hour/i.test(unit)?3600000:/分钟|分|minute/i.test(unit)?60000:1000):0;
        const until=Math.max(value.retryAt||0,Date.now()+(value.blockedKind==='login'?0:1800000),Date.now()+duration);
        value={...value,retryAt:until};put('douban-risk-signal-v1',{at:Date.now(),until,reason:value.error});
      }
      put('response:' + id, { ...value, checkedAt: now() });
    } };
    const tip = document.createElement('div');
    tip.textContent = '剧集整理助手正在读取本页。遇到验证时请回云存储查看提示。';
    tip.style.cssText = 'position:fixed;bottom:12px;right:12px;padding:12px;background:#173d4b;color:white;z-index:2147483646;font:14px sans-serif;max-width:320px';
    document.body.append(tip);
    for (let i = 0; i < 60; i++) {
      if (!get('request:' + id, null)) return;
      const accessProblem = blockedDouban();
      if (accessProblem) { reply({ blocked: true, blockedKind:/需要登录/.test(accessProblem)?'login':'risk',error: accessProblem }); return; }
      if(request.paced&&!document.querySelector('#info, .item-root .title a.title-text')) {
        const text=document.body.innerText.slice(0,3000);
        if(/Too Many Requests|Forbidden|Access Denied|访问被拒绝|访问受限|请求过于频繁/i.test(text)){reply({blocked:true,error:'豆瓣拒绝访问或限制频率，请等待冷却并人工确认'});return;}
        if(/ERR_CONNECTION|ERR_NETWORK|ERR_TIMED_OUT|网络连接失败|网络连接超时/.test(text)){reply({errorKind:'network',error:'豆瓣页面网络连接失败'});return;}
      }
      if (location.hostname === 'search.douban.com') {
        const anchors = [...document.querySelectorAll('.item-root .title a.title-text')];
        if (anchors.length) {
          await sleep(1500);
          const candidates = [...document.querySelectorAll('.item-root .title a.title-text')].map(a => {
            const card=a.closest('.item-root');
            // 搜索摘要的斜线分项只作为候选线索（含国家/类型等）；不能直接确认身份。
            // 进入详情后重新读取“又名”，必须再次匹配，搜索摘要里的文本不继承为详情别名。
            const aliases=(card?.querySelector('.meta.abstract')?.textContent||'').split('/').map(s=>s.trim()).filter(Boolean);
            return { title:a.textContent.trim(),url:validSubjectUrl(a.href),tv:/\[\s*剧集\s*\]/.test(card?.textContent||''),aliases };
          }).filter(c => c.url);
          // The title year may be a festival year. Inspect a unique title/season first;
          // accept its identity only after checking the detail page's release years.
          const chosen = chooseCandidate(candidates, request.show, request.season, { inspectDetails: true });
          if (!chosen) { reply({ error: matchFailure(candidates, request.show, request.season), candidates }); return; }
          if(request.paced){reply({nextUrl:chosen.url,candidates});return;} // 初检由主队列重新预留详情页额度。
          put('request:' + id, { ...request, candidates });
          location.href = chosen.url + '#tv_audit=' + id;
          return;
        }
        if (/没有找到|没找到|无搜索结果/.test(document.body.innerText)) { reply({ error: '豆瓣没有搜索到对应条目' }); return; }
      } else if (document.querySelector('#info') && document.querySelector('h1')) {
        const title = document.querySelector('h1').innerText.trim();
        const infoText = document.querySelector('#info').innerText;
        const total = parseTotal(infoText);
        const url = validSubjectUrl(location.href);
        const releaseYears = [...new Set([...document.querySelectorAll('#info [property="v:initialReleaseDate"]')].flatMap(e => e.textContent.match(/(?:19|20)\d{2}/g) || []))];
        const aliases = (/(?:^|\n)\s*又名\s*[:：]\s*([^\n]+)/.exec(infoText)?.[1] || '').split('/').map(s => s.trim()).filter(Boolean);
        const country = /(?:^|\n)\s*制片国家\/地区\s*[:：]\s*([^\n]+)/.exec(infoText)?.[1] || '';
        const japanese = country.split('/').some(s => s.trim() === '日本');
        const detail = { title, url, tv: !!total, releaseYears, aliases, japanese };
        if (!url || !total) { reply({ error: '豆瓣条目未提供可确认的总集数', title, url, candidates: request.candidates || [] }); return; }
        if ((!request.manual && !candidateMatches(detail, request.show, request.season)) || (request.manual && !(request.season===1 ? titleIdentity(title).season===null||titleIdentity(title).season===1 : titleIdentity(title).season===request.season))) { reply({ error: matchFailure([detail], request.show, request.season, true), title, url, candidates: request.candidates || [] }); return; }
        const matchNote = !request.manual && request.season === 1 && titleIdentity(title).year !== request.show.year ? `豆瓣标题年份 ${titleIdentity(title).year}，按详情首播年份 ${request.show.year} 确认` : '';
        reply({ ...parseDoubanBroadcast(infoText), total, title, url, manual: !!request.manual, releaseYears, aliases, japanese, matchNote, candidates: request.candidates || [] });
        return;
      }
      await sleep(500);
    }
    reply({ blocked: !request.paced,errorKind:'parse',error: '豆瓣页面未能正常加载，或网页结构已变化；请检查查询标签页' });
  }
  if (location.hostname !== CLOUD_HOSTNAME) { doubanWorker().catch(e => console.warn('[剧集核对]', e.message)); return; }
  if (location.origin !== CLOUD_ORIGIN) return;

  let running = false, stopping = false, job = null, panel, ui;
  const tmdbSeriesCache=new Map();
  const doubanPolicy=Core.createDoubanPolicy(Core,{get,put});
  const doubanQuery=Core.createDoubanQuery(Core,doubanPolicy,{page:readDoubanPage,ready:()=>doubanOpened&&ui.ready.checked,checkpoint,sleep,message});
  let supplementOptions=null;
  let tmdbDisabledReason='';
  function tmdbHttp(path,credentials,useKey=false) {
    const spec=tmdbRequestSpec(path,credentials,useKey);
    return new Promise((resolve,reject)=>{
      let request,settled=false;
      const end=(error,data)=>{if(settled)return;settled=true;clearTimeout(timer);if(error)reject(error);else resolve(data);};
      const timer=setTimeout(()=>{const error=new Error('TMDB 请求超时');end(error);request?.abort();},15000);
      try {
        request=GM_xmlhttpRequest({method:'GET',...spec,anonymous:true,redirect:'error',responseType:'json',
          onload:r=>{
            if(r.status!==200){const error=new Error(`TMDB HTTP ${Number(r.status)||0}`);error.status=r.status;end(error);return;}
            if(!r.response||typeof r.response!=='object'){end(new Error('TMDB 响应不是有效 JSON'));return;}
            end(null,r.response);
          },onerror:()=>end(new Error('TMDB 网络请求失败')),ontimeout:()=>end(new Error('TMDB 请求超时')),onabort:()=>end(new Error('TMDB 请求已中止'))});
      }catch{end(new Error('TMDB 请求无法发起，请检查脚本的跨域访问权限'));}
    });
  }
  async function tmdbApi(path) {
    if(tmdbDisabledReason)throw new Error(tmdbDisabledReason);
    const credentials=get('tmdb-credentials',{token:'',key:''});
    try {return await tmdbHttp(path,credentials);}catch(e){
      if((e.status===401||e.status===403)&&credentials.token&&credentials.key) {
        try{return await tmdbHttp(path,credentials,true);}catch(second){e=second;}
      }
      if([401,403,429].includes(e.status))tmdbDisabledReason=e.status===429?'TMDB 请求受限，本轮暂停 TMDB 查询':'TMDB 凭据未通过验证，本轮改用豆瓣';
      throw new Error(tmdbDisabledReason||`${e.message}（接口：${path}）${e.status===404?'；请核对目录中的 TMDB ID 和季号，条目可能已移除或更换 ID':''}`);
    }
  }
  async function queryTmdb(show,season) {
    if(!/^\d+$/.test(show.tmdb))throw new Error('目录中没有可用的 TMDB ID');
    checkpoint();
    let series=tmdbSeriesCache.get(show.tmdb);
    if(!series){series=await tmdbApi('/tv/'+show.tmdb);tmdbSeriesCache.set(show.tmdb,series);}
    checkpoint();
    const data=await tmdbApi(`/tv/${show.tmdb}/season/${season}`);
    checkpoint();
    return parseTmdbSeason(series,data,show,season,now());
  }
  const setupPanelLayout=/* build:panel-layout */ null;
  // Preparation is deliberately per-page, not a persistent claim that login is valid.
  let doubanOpened = false;
  class Paused extends Error {constructor(message){super(message);this.name='Paused';}}
  function checkpoint() { if (stopping) throw new Paused('已暂停，可继续当前剧集'); }
  async function waitFor(fn, message, timeout = 45000) {
    const end = Date.now() + timeout;
    while (Date.now() < end) { checkpoint(); const r = fn(); if (r) return r; await sleep(300); }
    throw new Error(message);
  }
  function currentPath() { return new URL(location.href).searchParams.get('path')?.replace(/\/$/, '') || '/'; }
  function allowed(path) { return [SOURCE].some(root => path === root || path.startsWith(root + '/')) && !path.split('/').some(p => p === '.' || p === '..'); }
  function readRows() {
    return [...document.querySelectorAll('table.file-list tr.file-row[data-path]')].map(e => ({ path: e.getAttribute('data-path'), name: e.querySelector('.file-name-text')?.textContent.trim() || '', directory: e.classList.contains('directory'), modified: e.querySelector('.file-modified')?.textContent.trim() || '' }));
  }
  function readCount() { const m = /^\s*([\d,]+)\s*items\b/.exec(document.querySelector('.file-list-status')?.textContent || ''); return m ? Number(m[1].replace(/,/g, '')) : null; }
  function breadcrumbPath() { return '/' + [...document.querySelectorAll('.files-breadcrumb-bar .breadcrumb-item:not(.breadcrumb-root)')].map(e => e.textContent.trim()).join('/'); }
  async function navigate(path) {
    checkpoint();
    if (!allowed(path)) throw new Error('拒绝访问配置范围之外的目录');
    try {
      if ([...document.querySelectorAll('[role="dialog"]')].some(visible)) throw new Error('请先关闭云盘页面已有的对话框');
      if (currentPath() !== path || breadcrumbPath() !== path) {
        // 页面重新渲染时一次点击可能失效。每次重新取得可见控件，最多重试三次；
        // 输入框已经打开时直接复用，不能反复点击编辑按钮把它关闭。
        const readyInput=()=>[...document.querySelectorAll('.files-breadcrumb-bar input.breadcrumb-path-input')].find(e=>visible(e)&&!e.disabled&&!e.readOnly&&e.isConnected!==false);
        let input=readyInput();
        for(let attempt=0;!input&&attempt<3;attempt++) {
          checkpoint();
          const edit=await waitFor(()=>[...document.querySelectorAll('.files-breadcrumb-bar .breadcrumb-edit-btn')].find(e=>visible(e)&&!e.disabled&&e.isConnected!==false),'文件浏览器路径编辑按钮不可用',10000);
          input=readyInput();if(input)break;
          edit.click();
          try{input=await waitFor(readyInput,'路径输入框未出现',5000);}
          catch(e){if(e instanceof Paused)throw e;if(attempt===2)throw new Error('路径输入框未出现（已尝试 3 次）');}
        }
        checkpoint();setInput(input, path);
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));
        await waitFor(() => currentPath() === path && breadcrumbPath() === path, '路径导航没有完成');
      }
    }catch(e) {
      if(e instanceof Paused)throw e;
      // 导航故障属于工作页面状态问题；暂停保留当前剧，不能将其跳过或写为空目录。
      throw new Paused('云盘导航已暂停：'+e.message+'；目标：'+path+'。请确认文件浏览器可操作、关闭弹窗，再点击“继续本轮”。');
    }
    const listMode = document.querySelector('button[aria-label="列表视图"]');
    if (listMode && visible(listMode)) listMode.click();
    const filter = document.querySelector('.unified-header .uh-filter-input');
    if (filter?.value) setInput(filter, '');
  }
  async function listing(path, sort = false) {
    await navigate(path);
    const refresh = await waitFor(() => document.querySelector('button.toolbar-refresh-btn'), '找不到“+添加”右侧刷新按钮');
    refresh.click();
    const select = await waitFor(() => document.querySelector('select.page-size-select'), '分页控件未出现');
    if (![...select.options].some(o => o.value === 'all')) throw new Error('页面不支持读取全部条目');
    if (select.value !== 'all') setInput(select, 'all');
    if (sort) {
      const header = await waitFor(() => document.querySelector('.unified-header .uh-modified'), '修改时间排序控件未出现');
      for (let n = 0; n < 3 && !header.textContent.includes('▲'); n++) { header.click(); await sleep(400); checkpoint(); }
      if (!header.textContent.includes('▲')) throw new Error('未能切换为修改时间从旧到新');
    }
    const settle=createListingSettler(path);
    return waitFor(() => {
      if (currentPath() !== path || breadcrumbPath() !== path) throw new Paused('工作页面被切换，请回到文件浏览器后继续');
      const rows = readRows(), count = readCount();
      if (sort && !document.querySelector('.unified-header .uh-modified')?.textContent.includes('▲')) throw new Paused('排序被改变，请继续重试');
      const loading=[...document.querySelectorAll('.file-list-scroll-area .loading-bar')].some(visible);
      const emptyConfirmed=[...document.querySelectorAll('table.file-list .empty-state .empty-content p')].some(e=>visible(e)&&/^(?:此文件夹为空|This folder is empty)[.!。！]?$/i.test(e.textContent.trim()));
      return settle({rows,count,loading,emptyConfirmed,allSelected:document.querySelector('select.page-size-select')?.value==='all'},Date.now());
    }, '列表未读全、空目录未确认或加载超时；不会把读取失败当作 0 个视频', 45000);
  }
  function save() { if (job) {job.updatedAt=now();put('progress-v2',{schema:2,job});put('progress-available',true);} render(); }
  function recordResult(result,persist=true) {
    if(!Array.isArray(job?.results))return;
    const i=job.results.findIndex(r=>r.recordId===result.recordId);if(i<0)job.results.push(result);else job.results[i]=result;if(persist)save();
  }
  function message(text) { ui.message.textContent = text; }
  async function querySources(show, season) {
    checkpoint();
    const reply=await preferTmdb(()=>queryTmdb(show,season),async()=>{
      try{return await queryDoubanRemote(show,season);}catch(e){if(e instanceof Paused)e.message='TMDB 未取得可用结果，豆瓣备用需要处理：'+e.message;throw e;}
    });
    return reply;
  }
  async function queryDoubanRemote(show,season,reference=null) {
    return doubanQuery.query(show,season,{reference,...supplementOptions});
  }
  async function readDoubanPage(url,requestInfo) {
    // 只在额度预留成功之后创建请求，因此等待额度不消耗 90 秒页面响应期限。
    const id=uuid(),request={...requestInfo,created:Date.now()};
    put('request:' + id, request);
    let tab, reply;
    try {
      tab = GM_openInTab(url + '#tv_audit=' + id, { active: false, insert: true, setParent: true });
      reply = await waitFor(() => get('response:' + id, null), '豆瓣查询页没有响应，请检查登录、验证码或脚本运行权限', 90000);
      if(reply.blocked)ui.ready.checked=false;
      return {...reply,source:'douban'};
    } catch(e){if(!(e instanceof Paused))ui.ready.checked=false;throw e;}
    finally { del('request:' + id); del('response:' + id); if(!reply?.blocked)tab?.close(); }
  }
  function blankResult(showRow, season, path) {
    const show=showInfo(showRow.name);
    return { category:job.category,showName:showRow.name,show,season,path,showPath:showRow.path,
      tmdbId:show.tmdb,recordId:JSON.stringify([showRow.path,season,path]),reportVersion:REPORT_VERSION,stage:'初检',
      videoCount:null,current:null,total:null,missing:null,integrity:'待核实',broadcast:'未确认',status:'待核实',
      source:'',sourceUrl:'',sourceTitle:'',checkedAt:'',scannedAt:now(),note:'' };
  }
  async function scanShow(showRow,onlySeason=null) {
    // 每季一条记录；先保存本地数量，再查询来源。没有 Season 目录时不猜第一季。
    const rows=await listing(showRow.path,true);
    const seasons=sortModified(rows.filter(r=>r.directory&&seasonNumber(r.name)));
    if(!seasons.length) {
      const noVideo=!rows.some(f=>f.directory||MEDIA.test(f.name));
      return [{...blankResult(showRow,'',showRow.path),...(noVideo?{videoCount:0,current:0}:{}),
        status:noVideo?(rows.length?'无视频':'空目录'):'待核实',
        note:'没有可识别的 Season N 目录，不能猜测季号；Season 0 / 特别篇另行处理'}];
    }
    const unclassified=rows.filter(r=>(r.directory&&!seasonNumber(r.name))||(!r.directory&&MEDIA.test(r.name)));
    const results=[];
    for(const seasonRow of seasons) {
      checkpoint();
      const n=seasonNumber(seasonRow.name),result=blankResult(showRow,n,seasonRow.path);
      if(onlySeason&&n!==onlySeason)continue;
      message(`正在核对 ${showRow.name} / ${seasonRow.name}`);
      try {
        const files=await listing(seasonRow.path);
        result.versionSnapshot=JSON.stringify({schema:1,path:seasonRow.path,scannedAt:result.scannedAt,files:files.map(f=>({name:f.name,path:f.path,directory:!!f.directory,size:f.size,modified:f.modified}))});
        Object.assign(result,auditEpisodes(files,n,null));
        const primary=await querySources(result.show,n);
        const required=supplementOptions?.force?{...primary,completionConfirmed:false}:primary;
        const {db,audit,review}=await reviewIncomplete(files,n,required,async()=>{
          message(`来源异常或本季播完状态未确认，正在豆瓣复核 ${showRow.name} / ${seasonRow.name}`);
          return queryDoubanRemote(result.show,n,primary);
        });
        Object.assign(result,audit,{review,total:db.total||null,source:db.source,sourceUrl:db.url||'',checkedAt:db.checkedAt||'',
          pendingDouban:!!(db.deferred||db.pendingDouban),retryAt:db.retryAt||null,sourceRead:db.fromCache?'豆瓣本地缓存':db.source==='douban'&&!db.error?'豆瓣实时查询':db.source==='tmdb'?'TMDB 实时查询':'待补查',
          sourceTitle:db.title||'',futureEpisodes:db.futureEpisodes||[],undatedEpisodes:db.undatedEpisodes||[],
          broadcastEvidence:db.broadcastEvidence||'没有足够的本季播出状态证据',
          doubanUrl:db.source==='douban'?db.url||'':'',tmdbUrl:db.source==='tmdb'?db.url||'':db.tmdbUrl||'',
          note:[result.videoCount===0?'已确认本季无视频，数量记录为 0；仍查询来源集数':'',...audit.issues,db.error,db.matchNote,db.cacheNote,
            db.retryAt?'豆瓣下次可尝试时间：'+new Date(db.retryAt).toLocaleString('zh-CN'):'',
            db.fallbackReason?'TMDB 未取得可用信息：'+db.fallbackReason:''].filter(Boolean).join('；')});
        result.status=result.pendingDouban?'豆瓣待补查':resultStatus(result);
      }catch(e){if(e instanceof Paused)throw e;result.status='读取失败';result.note=e.message;}
      if(unclassified.length)result.note+='；剧集层存在未归类视频或目录，整剧归档前须核实：'+unclassified.map(r=>r.name).join('、');
      results.push(result);if(Array.isArray(job.results))recordResult(result);
    }
    return results;
  }
  async function exclusive(work) {
    if (running) return;
    if (!navigator.locks) { message('当前环境不支持任务互斥锁，请使用新版 Chrome 的 HTTPS 页面。'); return; }
    await navigator.locks.request('tv-season-audit-cloud', { ifAvailable: true }, async lock => {
      if (!lock) { message('另一个云存储标签页正在执行，请先暂停那里的任务。'); return; }
      running = true; stopping = false; render();
      try { await work(); } catch (e) { if (job && job.status === '运行中') { job.status = '已暂停'; job.error = e.message; save(); } message(e.message); }
      finally { running = false; render(); }
    });
  }
  async function runJob() {
    // 已完成剧集才推进索引；当前剧暂停则重扫该剧，不重复追加之前完成的剧集。
    tmdbDisabledReason='';tmdbSeriesCache.clear();
    job.status = '运行中'; job.error = ''; save();
    if (!job.queue) {
      message('按修改时间从旧到新读取完整分类列表…');
      const rows = (await listing(SOURCE + '/' + job.category, true)).filter(r => r.directory);
      job.queue = sortModified(rows);
      save();
    }
    for (; job.index < job.queue.length;) {
      checkpoint();
      const showRow = job.queue[job.index];
      let results;
      try { results = await scanShow(showRow); }
      catch (e) { if (e instanceof Paused) throw e; results = [{ ...blankResult(showRow, '', showRow.path), status: '读取失败', note: e.message }]; }
      for(const result of results)recordResult(result,false); job.index++;
      job.readFailureStreak = results.every(r => r.status === '读取失败') ? (job.readFailureStreak || 0) + 1 : 0;
      save();
      if (job.readFailureStreak >= 3) { job.readFailureStreak = 0; throw new Paused('连续三部剧读取失败，已暂停。请检查连接或页面结构后继续。'); }
    }
    job.status = '已完成'; job.finishedAt = now(); save();
    message('云盘扫描完成。豆瓣待补查 '+job.results.filter(r=>r.pendingDouban).length+' 季；可导出当前报告，准备好后点击“仅补查豆瓣待处理项”。');
    await navigate(SOURCE + '/' + job.category);
  }
  async function supplement(rows,force=false) {
    if(!job)return;
    supplementOptions={wait:true,force};
    try {
      for(const row of rows) {
        checkpoint();if(doubanPolicy.status().hold){message('豆瓣处于暂停状态，请冷却结束并人工确认后恢复');break;}
        const fresh=await scanShow({name:row.showName,path:row.showPath,directory:true},row.season);
        if(!fresh.some(r=>r.season===row.season)){row.pendingDouban=true;row.note='原季目录已不存在，未更新为齐全';row.status='待核实';save();}
      }
      message('补查结束，仍待处理 '+job.results.filter(r=>r.pendingDouban).length+' 季；可导出报告。');
    }finally{supplementOptions=null;save();}
  }
  function restoreProgress(value) {
    if(value?.schema!==2||value.job?.mode!=='read-only'||!Array.isArray(value.job.results)||!Number.isInteger(value.job.index)||value.job.index<0||typeof value.job.id!=='string'||!Number.isFinite(Date.parse(value.job.startedAt)))throw Error('保存的进度格式不支持');
    const restored=value.job,root=SOURCE+'/'+restored.category;
    if(!restored.category||/[\\/]/.test(restored.category)||['.','..'].includes(restored.category))throw Error('保存的分类路径无效');
    if(restored.queue!==null&&(!Array.isArray(restored.queue)||restored.index>restored.queue.length||restored.queue.some(r=>!r.directory||parent(r.path)!==root||base(r.path)!==r.name||['.','..'].includes(r.name))))throw Error('保存的待执行目录越界');
    if(restored.results.some(r=>!r.path?.startsWith(root+'/')||r.path.split('/').some(p=>p==='.'||p==='..')||r.showPath!==root+'/'+r.showName))throw Error('保存的报告路径无效');
    job=restored;job.status='已暂停';job.error='';save();message('进度已恢复，尚未运行。请点击继续本轮或仅补查豆瓣待处理项。');
  }
  function el(tag, text, props = {}) { const n = document.createElement(tag); if (text != null) n.textContent = text; Object.assign(n, props); return n; }
  function button(label, action) { const b = el('button', label); b.addEventListener('click', () => Promise.resolve().then(action).catch(e => message(e.message))); return b; }
  function download(name, content, type) { const url = URL.createObjectURL(new Blob([content], { type })); const a = el('a', null, { href: url, download: name }); a.click(); setTimeout(() => URL.revokeObjectURL(url), 10000); }
  function exportFile(kind) {
    if (!job) return;
    const name = `剧集初检_${job.category}_${job.startedAt.slice(0,10)}_${job.id.slice(0,8)}`;
    if(kind==='xlsx'){download(name+'.xlsx',resultXlsx(job.results),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');return;}
    download(name + '.' + kind, kind === 'csv' ? resultCsv(job.results) : JSON.stringify(job, null, 2), kind === 'csv' ? 'text/csv;charset=utf-8' : 'application/json;charset=utf-8');
  }
  function render() {
    if (!ui) return;
    const busy=running;
    ui.load.disabled = ui.prepare.disabled = busy;
    ui.ready.disabled = running;
    ui.start.disabled = ui.single.disabled = busy;
    ui.pause.disabled = !running;
    ui.resume.disabled = busy || !job || job.status === '已完成';
    ui.restore.disabled=busy||!get('progress-available',false);
    ui.supplement.disabled=busy||!job?.results.some(r=>r.pendingDouban);
    ui.unlock.disabled=busy;
    const budget=doubanPolicy.status();ui.budget.textContent=`豆瓣页面访问：近 1 小时 ${budget.hour}/40，近 24 小时 ${budget.day}/200；待补查 ${job?.results.filter(r=>r.pendingDouban).length||0} 季`+(budget.hold?'；已暂停：'+budget.reason:'');
    ui.category.disabled = running;
    for(const b of ui.tmdbButtons) b.disabled=busy;
    ui.tmdbToken.disabled=ui.tmdbKey.disabled=busy;
    ui.status.textContent = job ? `${job.category}${job.scope === 'single' ? '（单剧验证）' : ''} · ${job.status === '运行中' && !running ? '上次中断，可继续' : job.status} · ${job.index}/${job.queue?.length ?? '?'} 部` : '尚未开始';
    ui.summary.textContent = job ? `绿色齐全 ${job.results.filter(r=>resultColor(r)?.fill==='C6EFCE').length} 季 · 连载 ${job.results.filter(r=>r.broadcast==='连载中').length} 季 · 缺集 ${job.results.filter(r=>r.integrity==='缺集').length} 季 · 待核实 ${job.results.filter(r=>r.integrity==='待核实'||r.broadcast==='未确认').length} 季` : '此版本仅检查，不包含移动、删除或创建云端目录的代码。';
    ui.results.replaceChildren();
    if ((job?.results.length || 0) > 100) ui.results.append(el('small', '面板显示最近 100 项；导出文件包含全部结果。'));
    for (const r of (job?.results || []).slice(-100)) {
      const row = el('div', null, { className: 'result' });
      row.append(el('strong', `${r.showName} · ${r.season ? 'Season ' + r.season : '目录'}`), el('p', `${r.status}｜已识别 ${r.current ?? '?'} / ${r.source==='tmdb'?'TMDB':r.source==='douban'?'豆瓣':'来源未查询'} ${r.total ?? '?'}｜缺失 ${r.missing == null ? '未确认' : r.missing.length ? r.missing.join('、') : '无'}`), el('small', r.note));
      if (r.sourceUrl||r.doubanUrl) row.append(el('a', '数据来源', { href:r.sourceUrl||r.doubanUrl, target: '_blank', rel: 'noopener' }));
      if (r.season) row.append(button('绑定豆瓣链接', () => {
        if (running) { message('请先暂停后再绑定链接。'); return; }
        const url = prompt(`为“${r.showName} / Season ${r.season}”绑定豆瓣链接。\n请确认对应剧集和季数；需要豆瓣备用或复核时使用此链接。`, r.source==='douban'?r.sourceUrl:r.doubanUrl||'');
        if (url === null) return;
        const valid = validSubjectUrl(url.trim());
        if (!valid) throw new Error('请输入 https://movie.douban.com/subject/数字/ 格式的详情页链接');
        doubanPolicy.bind(r.show,r.season,valid);r.pendingDouban=true;r.status='豆瓣待补查';save();
        message('链接已保存为待验证候选。请点击补查；详情仍须核对完整剧名、年份和季号。');
      }));
      if(r.season){const force=button('强制重查此季豆瓣',()=>exclusive(()=>supplement([r],true)));force.disabled=busy;row.append(force);}
      ui.results.append(row);
    }
    if (job?.error) ui.message.textContent = job.error;
  }
  function categories(options) {
    const previous = ui.category.value;
    ui.category.replaceChildren(...options.map(name => el('option', name, { value: name })));
    if (options.includes(previous)) ui.category.value = previous;
  }
  function mount() {
    panel = el('div', null, { id: 'tv-inspect-panel' });
    document.body.append(panel);
    const root = panel.attachShadow({ mode: 'open' });
    const style = el('style', `:host{position:fixed;right:18px;bottom:18px;z-index:2147483645;font:14px/1.5 system-ui,sans-serif;color:#18343d}*{box-sizing:border-box}.box{width:440px;max-width:94vw;max-height:85vh;overflow:hidden;background:#f8fbfc;border:1px solid #a9c3ca;border-radius:14px;box-shadow:0 10px 40px #0003;padding:14px}.body{max-height:calc(85vh - 108px);overflow:auto}.drag-handle{cursor:move;touch-action:none;user-select:none}h2{font-size:18px;margin:0 0 6px}p{margin:6px 0}small{display:block;color:#576e77;overflow-wrap:anywhere}button,select{font:inherit;border:1px solid #afc5cb;border-radius:6px;background:white;padding:6px 9px;margin:4px 4px 4px 0;color:#173d4b}button{cursor:pointer}button:disabled{opacity:.45;cursor:default}.primary{background:#165568;color:white}a{color:#166a85;display:inline-block;margin:4px 10px 4px 0}.message{padding:9px;background:#e7f1f5;margin:8px 0;overflow-wrap:anywhere}.result{padding:10px 0;border-top:1px solid #d7e3e7}.result strong{display:block;font-size:13px}.result p{font-size:13px}.results{max-height:280px;overflow:auto}.min .body{display:none}.min{width:240px}details{border-top:1px solid #d7e3e7;padding-top:8px;margin-top:8px}`);
    const box = el('section', null, { className: 'box' }), body = el('div', null, { className: 'body' });
    const heading = el('h2', '① 剧集初检 · 只读');
    let panelLayout;
    const toggle = button('收起', () => panelLayout.toggle());
    const dragHandle=el('header',null,{className:'drag-handle',title:'按住标题区域拖动'});
    dragHandle.append(heading,toggle);
    root.append(style, box); box.append(dragHandle, body);
    ui = { category: el('select'), status: el('p'), summary: el('small'),budget:el('small'), message: el('div', 'TMDB 主查；豆瓣按需查询、缓存优先。刷新后可手动恢复进度，默认不运行。', { className: 'message' }), results: el('div', null, { className: 'results' }) };
    const tmdbDetails=el('details'),tmdbSummary=el('summary','TMDB 访问设置');
    const credentials=get('tmdb-credentials',{token:'',key:''});
    ui.tmdbToken=el('input',null,{type:'password',value:credentials.token||'',placeholder:'API 读访问令牌',autocomplete:'off'});
    ui.tmdbKey=el('input',null,{type:'password',value:credentials.key||'',placeholder:'API 密钥',autocomplete:'off'});
    ui.tmdbToken.style.width=ui.tmdbKey.style.width='100%';
    const tokenLabel=el('label','API 读访问令牌');tokenLabel.append(ui.tmdbToken);
    const keyLabel=el('label','API 密钥');keyLabel.append(ui.tmdbKey);
    ui.tmdbStatus=el('small',credentials.token||credentials.key?'已保存 TMDB 凭据（不导出）':'尚未保存 TMDB 凭据');
    ui.tmdbButtons=[button('保存 TMDB 凭据',()=>{
      const token=ui.tmdbToken.value.trim().replace(/^Bearer\s+/i,''),key=ui.tmdbKey.value.trim();
      if(!token&&!key)throw new Error('请至少填写读访问令牌或 API 密钥之一');
      if(/\s/.test(token)||/\s/.test(key))throw new Error('凭据中不能包含空白字符');
      put('tmdb-credentials',{token,key});tmdbDisabledReason='';tmdbSeriesCache.clear();ui.tmdbStatus.textContent='TMDB 凭据已本地保存；优先使用读访问令牌。';
    }),button('测试 TMDB 连接',async()=>{
      ui.tmdbStatus.textContent='正在测试已保存的凭据…';
      tmdbDisabledReason='';
      try {const r=await tmdbApi('/authentication');ui.tmdbStatus.textContent=r.success?'TMDB 连接与认证成功。':'TMDB 认证未确认，请检查凭据。';}
      catch(e){ui.tmdbStatus.textContent=e.message;}
    }),button('清除 TMDB 凭据',()=>{del('tmdb-credentials');ui.tmdbToken.value=ui.tmdbKey.value='';tmdbDisabledReason='';tmdbSeriesCache.clear();ui.tmdbStatus.textContent='本地 TMDB 凭据已清除。';})];
    tmdbDetails.append(tmdbSummary,tokenLabel,el('br'),keyLabel,el('br'),...ui.tmdbButtons,ui.tmdbStatus,el('small','两种凭据任选其一；填写两种时，令牌认证失败会尝试 API 密钥。仅存于当前脚本的本地设置，不写入报告。'),el('a','TMDB 数据来源与署名',{href:'https://www.themoviedb.org',target:'_blank',rel:'noopener'}),el('small','This product uses the TMDB API but is not endorsed or certified by TMDB.'));
    ui.ready = el('input', null, { type: 'checkbox' });
    ui.ready.addEventListener('change', () => {if(ui.ready.checked)doubanOpened=true;render();});
    const readyLabel = el('label');
    readyLabel.append(ui.ready, document.createTextNode(' 豆瓣已可访问（如需登录，已手动完成）'));
    ui.prepare = button('打开豆瓣／登录准备', () => exclusive(async() => {
      const permit=doubanPolicy.reserve();
      if(!permit.allowed){message('未打开新页面：'+(permit.reason||'访问额度等待')+'。请在已有豆瓣标签页检查；下次可尝试 '+new Date(permit.next).toLocaleString('zh-CN'));return;}
      GM_openInTab('https://movie.douban.com/', { active: true, insert: true, setParent: true });
      doubanOpened = true; ui.ready.checked = false;
      if (job) job.error = '';
      message('已打开豆瓣首页。如出现登录界面，请先手动登录。确认页面可访问后回到这里勾选准备完成，再点击开始或继续；打开首页不会启动扫描。');
      render();
    }));
    ui.restore=button('恢复上次进度',()=>exclusive(async()=>restoreProgress(get('progress-v2',null))));
    ui.supplement=button('仅补查豆瓣待处理项',()=>exclusive(()=>supplement(job?.results.filter(r=>r.pendingDouban)||[])));
    ui.unlock=button('人工确认豆瓣已恢复',()=>exclusive(async()=>{
      if(!ui.ready.checked)throw Error('请先在已有豆瓣页面完成人工登录/验证，再勾选“豆瓣已可访问”');
      doubanPolicy.resume();message('豆瓣暂停已解除，尚未发起查询。请点击仅补查豆瓣待处理项。');render();
    }));
    ui.load = button('读取分类', () => exclusive(async () => {
      message('正在读取临时目录的分类…');
      const rows = await listing(SOURCE);
      const names = rows.filter(r => r.directory).map(r => r.name);
      put('categories', names); categories(names); message('分类已更新。');
    }));
    ui.start = button('开始检查', () => exclusive(async () => {
      const category = ui.category.value;
      if (!category || /[\\/]/.test(category)) throw new Error('请先读取并选择分类');
      if (job && job.status !== '已完成' && !confirm('本轮任务未完成。开始新检查会替换当前页面结果，是否继续？')) return;
      job = { id: uuid(), category, startedAt: now(), status: '运行中', queue: null, index: 0, results: [], mode: 'read-only' };
      save(); await runJob();
    }));
    ui.start.className = 'primary';
    ui.single = button('检查当前剧集', () => exclusive(async () => {
      const path = currentPath();
      if (!path.startsWith(SOURCE + '/')) throw new Error('请先在临时目录中打开某部剧或其 Season 目录');
      const [category, showName] = path.slice(SOURCE.length + 1).split('/');
      if (!category || !showName) throw new Error('请先打开某部剧或其 Season 目录，再点击单剧检查');
      if (job && job.status !== '已完成' && !confirm('本轮任务未完成。开始单剧检查会替换当前页面结果，是否继续？')) return;
      job = { id: uuid(), category, startedAt: now(), status: '运行中', scope: 'single', queue: [{ name: showName, path: SOURCE + '/' + category + '/' + showName, directory: true }], index: 0, results: [], mode: 'read-only' };
      save(); await runJob();
    }));
    ui.resume = button('继续本轮', () => exclusive(async () => { if (job) await runJob(); }));
    ui.pause = button('暂停', () => { stopping = true; message('正在暂停；保留已完成结果，当前剧集继续时会重新核对。'); });
    body.append(ui.summary,tmdbDetails,ui.prepare, el('br'), readyLabel, el('br'),ui.unlock,ui.budget, ui.load, ui.category, el('br'),ui.start, ui.single, ui.pause, ui.resume,ui.restore,ui.supplement, button('导出 Excel（绿 / 蓝标记）', () => exportFile('xlsx')),button('导出 CSV', () => exportFile('csv')), button('导出完整 JSON', () => exportFile('json')),ui.status, ui.message, ui.results);
    categories(get('categories', [])); render();
    panelLayout=setupPanelLayout({host:panel,box,body,handle:dragHandle,toggle,load:()=>get('panel-layout-v1',null)||get('panel-position',null),save:value=>put('panel-layout-v1',value)});
  }
  mount();
})();
