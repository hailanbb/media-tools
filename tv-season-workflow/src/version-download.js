/* 按第三阶段同一文件名模板分组；仅生成补缺建议，不删除或移动云盘文件。
 * 匹配资源站版本要求发布组及明确技术参数一致，不能靠相同分辨率推断同版本。
 */
(function createVersionDownload(V) {
  'use strict';
  const range=n=>Array.from({length:n},(_,i)=>i+1);
  function signature(name) {
    const s=String(name||'').normalize('NFKC').replace(/\.(mkv|mp4|avi|ts|m2ts)$/i,'');
    const group=/-([a-z0-9][a-z0-9._]*)\s*$/i.exec(s)?.[1].toLowerCase();
    const resolution=/\b(2160|1080|720|480)[pi]\b/i.exec(s)?.[0].toLowerCase();
    const source=/\b(web[ ._-]?dl|web[ ._-]?rip|blu[ ._-]?ray|hdtv|bdrip|remux)\b/i.exec(s)?.[0].replace(/[ ._-]/g,'').toLowerCase();
    const codec=/\b(x26[45]|h[ ._-]?26[45]|hevc|avc)\b/i.exec(s)?.[0].replace(/[ ._-]/g,'').toLowerCase();
    if(!group||/^(dl|rip|webdl|webrip|hdtv|x264|x265|hevc|avc|hdr|sdr|aac|ddp|truehd|atmos|1080p|2160p)$/i.test(group)||!resolution||(!source&&!codec))return null;
    const audio=(s.match(/\b(?:ddp|dd|aac|dts(?:[ ._-]?hd)?|truehd|atmos|flac)(?:[ ._-]?\d(?:\.\d)?)?\b/ig)||[]).map(v=>v.toLowerCase().replace(/[ _-]/g,'')).sort();
    const hdr=(s.match(/\b(?:hdr10\+?|hdr|dolby[ ._-]?vision|dv|sdr|10bit|8bit)\b/ig)||[]).map(v=>v.toLowerCase()).sort();
    return JSON.stringify([group,resolution,source||'',codec||'',audio,hdr,/\brepack\b/i.test(s),/\bproper\b/i.test(s)]);
  }
  function joined(data,column){return String(data[column]||'')+Object.keys(data).filter(k=>new RegExp('^'+column+'续\\d+$').test(k)).sort((a,b)=>Number(a.slice(column.length+1))-Number(b.slice(column.length+1))).map(k=>String(data[k]||'')).join('');}
  function inspect(task) {
    const d=task.data||{},review=joined(d,'复核清理记录'),raw=joined(d,'版本文件清单');let rows;
    if(review){const r=JSON.parse(review);if(r.journal?.pending)return {blocked:'上次清理结果不明，先完成第三脚本核实'};if(Array.isArray(r.groups))rows=r.groups.flatMap(g=>g.files||[]);if(r.issues?.some(x=>/子目录|临时|无法确定|无效|大小/.test(x)))return {blocked:'复核记录存在文件或版本异常，先重新复核'};}
    if(!rows&&raw){const r=JSON.parse(raw);if(r.schema!==1||r.path!==task.path||!Array.isArray(r.files))return {blocked:'版本清单格式或季目录不一致'};rows=r.files;}
    if(!rows){if(String(d['重复集号']||'').trim()&&!/^(无|0)$/.test(String(d['重复集号']).trim())||/混版|多个版本/.test(String(d['复核结果']||'')+String(d['核对说明']||'')))return {blocked:'报告存在混版迹象但缺少版本清单，请用新版第一脚本重检或第三脚本复核后导出'};return null;}
    if(rows.length>10000)return {blocked:'版本清单过大，请人工核实'};
    const groups=new Map(),seen=new Set();
    for(const f of rows){if(f.directory||/\.(part|tmp|download|crdownload|!qb)$/i.test(f.name))return {blocked:'版本清单存在子目录或下载临时文件'};if(!/\.(mkv|mp4|avi|mov|m4v|ts|m2ts|wmv|flv|webm|mpg|mpeg|strm)$/i.test(f.name))continue;
      if(f.path!==task.path+'/'+f.name||/[\\/]/.test(f.name)||seen.has(f.path))return {blocked:'版本文件路径越界或重复'};seen.add(f.path);
      const p=V.versionOf(f,task.season);if(!p||p.episode>task.total||!Number.isFinite(p.time)||!/^\d+(?:\.\d+)?\s*(?:B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$/i.test(f.size||'')||parseFloat(f.size)<=0)return {blocked:'文件集号、修改时间、大小或版本模板无法确认'};
      if(!groups.has(p.key))groups.set(p.key,{key:p.key,label:p.label,episodes:[],latest:p.time,modified:f.modified,signature:signature(f.name),copies:{}});
      const g=groups.get(p.key);g.copies[p.episode]=(g.copies[p.episode]||0)+1;if(!g.episodes.includes(p.episode))g.episodes.push(p.episode);if(p.time>g.latest){g.latest=p.time;g.modified=f.modified;}
    }
    if(groups.size<=1)return null;
    const all=[...groups.values()],complete=all.filter(g=>g.episodes.length===task.total);
    if(complete.length>1||complete.some(g=>range(task.total).every(n=>g.copies[n]>1)))return {blocked:'多个完整版本，交第三脚本人工判断，不自动补下载'};
    if(complete.length===1)return {complete:complete[0],note:'已有独立完整版本，不再补下载；交第三脚本复核清理'};
    if(all.some(g=>!g.signature)||new Set(all.map(g=>g.signature)).size!==all.length)return {blocked:'混版缺少唯一发布组/技术标识，不能可靠匹配拟保留版本'};
    return {groups:all};
  }
  function select(task,info,contexts,choose) {
    const options=info.groups.map(g=>{const needed=range(task.total).filter(n=>!g.episodes.includes(n)&&!task.excluded.includes(n));const pool=contexts.filter(c=>signature(c.torrent_info?.title)===g.signature);const local={...task,needed};const plan=choose(local,pool,g.episodes);return {g,needed,pool,plan,canComplete:!plan.remaining?.length&&plan.selected?.length>0};});
    if(options.some(o=>o.plan.identityAmbiguous))return {blocked:'同名同年资源对应多个媒体ID，请先确认本剧ID再选择版本'};
    const available=options.filter(o=>o.plan.selected?.length);if(!available.length)return {blocked:'没有与现有版本明确匹配的缺集资源，不新增混版'};
    available.sort((a,b)=>Number(b.canComplete)-Number(a.canComplete)||b.g.latest-a.g.latest);
    if(available[1]&&available[0].canComplete===available[1].canComplete&&available[0].g.latest===available[1].g.latest)return {blocked:'可补版本的云盘最新修改时间相同，需人工指定版本'};
    const best=available[0];return {...best,note:'拟保留版本：'+best.g.label+'；缺集：'+best.needed.join('、')+'；云盘最新修改时间：'+best.g.modified+'；'+(best.canComplete?'同版本资源可补齐':'暂只能补齐部分，保持同版本')};
  }
  return {inspect,select,signature};
})
