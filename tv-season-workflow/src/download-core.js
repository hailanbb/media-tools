/* 下载决策与任务状态机。
 * 1. 仅匹配本剧、本季；完整包优先，做种数不足 2 的不选。
 * 2. 同类优先中文标记；做种不少于 5 且容量均已知时优先小容量，否则优先做种数。
 *    零散多集按每个待补集的容量估算比较，每选一个资源后重新计算剩余缺集。
 * 3. 写入“准备提交”记录后才发出下载请求；超时保留结果不明，禁止盲目重试。
 * 4. 对外报告仅含白名单字段，种子链接、站点 Cookie 和登录凭据不进入记录。
 */
(function createDownloadCore(Titles,Versions,Broadcast) {
  'use strict';
  const norm=s=>Titles.norm(Titles.cleanTitleText(s));
  const uniq=a=>[...new Set(a)].sort((a,b)=>a-b);
  const positive=n=>Number.isInteger(n)&&n>0&&n<=10000;
  const range=n=>Array.from({length:n},(_,i)=>i+1);
  const bytes=n=>Number.isSafeInteger(Number(n))&&Number(n)>0?Number(n):null;
  // 只认明确语言标签/字幕标记，不以片名含汉字推断字幕；负面或冲突标记降为未确认。
  function chineseMark(t,meta) {
    const labels=Array.isArray(t.labels)?t.labels.filter(v=>typeof v==='string'):[];
    const texts=[...labels,t.title,t.description,meta?.subtitle].filter(v=>typeof v==='string');
    if(texts.some(v=>/(?:无|没有|暂无|不含|不带)(?:中文|中字|简中|繁中|字幕)|no(?:chinese|chs|cht|subtitles|subs)/i.test(norm(v))))return '';
    const marked=v=>/中文字幕|中文配音|中字|中英双字|中英字幕|简繁字幕|简体字幕|繁体字幕|简中|繁中|国语|国配/.exec(norm(v))?.[0]||(/\b(?:chs|cht|chi|zho|mandarin)\b/i.exec(v)?.[0]||'');
    for(const label of labels){const mark=/^(?:中文|简体|繁体|chinese)$/i.test(norm(label))?norm(label):marked(label);if(mark)return '标签：'+mark;}
    for(const text of texts.slice(labels.length)){const mark=marked(text);if(mark)return '标题/副标题：'+mark;}
    return '';
  }
  function pick(candidates,needed=null) {
    if(!candidates.length)return null;
    const chinese=candidates.filter(c=>c.chineseMark),languagePool=chinese.length?chinese:candidates;
    const healthy=languagePool.filter(c=>c.seeds>=5),pool=healthy.length?healthy:languagePool;
    const bySize=healthy.length>0&&pool.every(c=>c.sizeBytes!==null);
    const count=c=>needed?c.episodes.filter(n=>needed.includes(n)).length:1;
    const cost=c=>c.sizeBytes/count(c);
    const sorted=[...pool].sort((a,b)=>(bySize?cost(a)-cost(b):b.seeds-a.seeds)||(bySize?b.seeds-a.seeds:0)||a.key.localeCompare(b.key));
    const best=sorted[0];
    return {...best,estimatedBytes:needed&&best.sizeBytes!==null?cost(best):null,
      selectionReason:(chinese.length?'优先明确中文标记；':'无明确中文标记，使用其他合格资源；')+(bySize?(needed?'做种≥5，按每个待补集估算容量升序':'做种≥5，按整季总容量升序'):healthy.length?'高做种候选容量有未知，按做种数降序':'做种不足5，按做种数降序')};
  }
  class Pause extends Error {constructor(message='已暂停；点击继续本轮恢复'){super(message);this.name='Pause';}}
  function episodes(value,{blankUnknown=false}={}) {
    const text=String(value??'').trim();
    if(!text)return blankUnknown?null:[];
    if(/^(无|无缺集|齐全)$/.test(text))return [];
    if(/未确认|未知|待核实|无法/.test(text))return null;
    const values=[];
    for(const part of text.split(/[、,，;；\s]+/).filter(Boolean)) {
      const m=/^(?:E)?(\d{1,4})(?:[-~～至](?:E)?(\d{1,4}))?$/i.exec(part);
      if(!m)throw Error('集号格式无法确认：'+part.slice(0,25));
      const a=Number(m[1]),b=Number(m[2]||m[1]);
      if(!positive(a)||!positive(b)||b<a)throw Error('集号范围无效');
      for(let n=a;n<=b;n++)values.push(n);
    }
    return uniq(values);
  }
  function taskFromRow(row) {
    const d=row.data,total=Number(d['来源登记集数']);
    if(!positive(total))throw Error('来源登记集数未确认，请先补充初检结果');
    const title=Titles.cleanTitleText(row.name.replace(/\[tmdbid=\d+\]/ig,'').replace(/[（(](?:19|20)\d{2}[)）]/g,''));
    const year=/[（(]((?:19|20)\d{2})[)）]/.exec(row.name)?.[1]||'';
    const directoryID=/\[tmdbid=(\d+)\]/i.exec(row.name)?.[1]||'',fieldID=String(d['TMDB ID']||'').trim();
    if(fieldID&&!/^\d+$/.test(fieldID))throw Error('TMDB ID 格式错误');
    if(fieldID&&directoryID&&fieldID!==directoryID)throw Error('目录中的 TMDB ID 与报告不一致');
    const tmdb=fieldID||directoryID;
    let douban='';
    const dbLink=d['豆瓣链接']||(d['数据来源']==='douban'?d['来源链接']:'');
    if(dbLink){try{const u=new URL(dbLink);if(u.protocol==='https:'&&u.hostname==='movie.douban.com'&&!u.port&&!u.username&&!u.password)douban=/^\/subject\/(\d+)\/$/.exec(u.pathname)?.[1]||'';}catch{}}
    const future=episodes(d['待播集号']),undated=episodes(d['播期未知集号']);
    if(future===null||undated===null)throw Error('待播或播期未知集号格式不明确');
    const excluded=uniq([...future,...undated]),missing=episodes(d['缺失集号'],{blankUnknown:true});
    if([...excluded,...missing||[]].some(n=>n>total))throw Error('集号超出来源登记数量');
    const needed=missing?.filter(n=>!excluded.includes(n))??null;
    const continuation=Object.keys(d).filter(h=>/^下载记录续\d+$/.test(h)).sort((a,b)=>Number(a.slice(5))-Number(b.slice(5))).map(h=>String(d[h]||'')).join('');
    return {...row,title,year,total,tmdb,douban,excluded,needed,records:readRecords(String(d['下载记录']||'')+continuation),previousState:String(d['下载处理']||'')};
  }
  // 从报告和本地日志重建安全字段；不接受从表格注入下载 URL、API 路径或额外请求参数。
  function safeRecord(r) {
    if(!r||!['accepted','existing','submitting','uncertain','rejected'].includes(r.state)||typeof r.key!=='string'||r.key.length>3000)throw Error('下载记录无效');
    if(!Array.isArray(r.episodes)||r.episodes.some(n=>!positive(n)))throw Error('下载记录的集号无效');
    return {key:r.key,state:r.state,id:String(r.id||'').slice(0,256),title:String(r.title||'').slice(0,1000),site:String(r.site||'').slice(0,150),
      episodes:uniq(r.episodes),seeds:Number.isFinite(r.seeds)?r.seeds:null,at:String(r.at||'').slice(0,40),
      scope:['season','episode','episodes'].includes(r.scope)?r.scope:'unknown',finishedAt:String(r.finishedAt||'').slice(0,40),sizeBytes:bytes(r.sizeBytes),
      estimatedBytes:typeof r.estimatedBytes==='number'&&Number.isFinite(r.estimatedBytes)&&r.estimatedBytes>0?r.estimatedBytes:null,
      chineseMark:String(r.chineseMark||'').slice(0,80),selectionReason:String(r.selectionReason||'').slice(0,300),seedNote:String(r.seedNote||'').slice(0,180)};
  }
  function readRecords(value) {
    if(value==null||value==='')return [];
    const parsed=typeof value==='string'?JSON.parse(value):value;
    if(!Array.isArray(parsed)||parsed.length>2000)throw Error('下载记录数量或格式异常');
    return parsed.map(safeRecord);
  }
  function sameIdentity(task,media) {
    if(!media||!['电视剧','tv','TV'].includes(media.type))return false;
    if(identityConflict(task,media))return false;
    if(task.tmdb)return String(media.tmdb_id||'')===task.tmdb;
    if(task.douban)return String(media.douban_id||'')===task.douban;
    return !!task.year&&String(media.year||'')===task.year&&[media.title,media.original_title].some(t=>norm(t)===norm(task.title));
  }
  // 已明确冲突的 ID 不能被同名、别名、年份或种子标题覆盖。
  function identityConflict(task,media) {
    return ['tmdb','douban'].some(k=>task[k]&&media?.[k+'_id']&&String(media[k+'_id'])!==task[k]);
  }
  function unresolvedIdentity(task,media) {
    if(!media||!['电视剧','tv','TV'].includes(media.type)||identityConflict(task,media))return false;
    if(task.tmdb||task.douban)return !sameIdentity(task,media);
    // 无 ID 的同年不同译名不能确认是错剧，也不能确认是同剧；交人工补充 ID。
    return !task.year||!media.year||String(media.year)===task.year;
  }
  function seasonList(meta) {
    // 不把 MoviePilot 对无季号资源的默认 S01 当作明确分季依据。
    if(!positive(meta?.begin_season))return [];
    const end=meta.end_season==null?meta.begin_season:meta.end_season;
    if(!positive(end)||end<meta.begin_season||end-meta.begin_season>100)return [];
    const list=range(end-meta.begin_season+1).map(n=>meta.begin_season+n-1);
    if(meta.season_list?.length&&JSON.stringify(uniq(meta.season_list))!==JSON.stringify(list))return [];
    return list;
  }
  function resourceKey(t) {return JSON.stringify([String(t.site||t.site_name||''),String(t.title||''),Number(t.size)||0,String(t.pubdate||'')]);}
  function candidate(task,context) {
    const m=context?.media_info,meta=context?.meta_info,t=context?.torrent_info;
    if(!t)return {kind:'rejected',reason:'资源记录不完整'};
    if(!sameIdentity(task,m))return {kind:unresolvedIdentity(task,m)?'ambiguous':'rejected',reason:'媒体身份未能确认；请核实本剧媒体 ID'};
    const seasons=seasonList(meta);
    if(seasons.length!==1||seasons[0]!==task.season)return {kind:'rejected',reason:'季号不明、混季或季号不符'};
    const rawSeasons=[...String(t.title||'').matchAll(/(?:^|[^a-z0-9])S(\d{1,3})(?!\d)/ig)].map(m=>Number(m[1]));
    if(rawSeasons.some(n=>n!==task.season))return {kind:'ambiguous',reason:'资源原始标题与识别季号冲突'};
    const seeds=typeof t.seeders==='number'?t.seeders:/^\d+$/.test(String(t.seeders))?Number(t.seeders):NaN;
    if(!Number.isFinite(seeds)||seeds<0)return {kind:'ambiguous',reason:'做种数无法确认'};
    if(seeds<2)return {kind:'low',reason:'做种数小于 2'};
    if(typeof t.enclosure!=='string'||!t.enclosure)return {kind:'ambiguous',reason:'没有可用下载入口'};
    let eps=Array.isArray(meta.episode_list)?uniq(meta.episode_list):[];
    if(eps.some(n=>!positive(n)||n>task.total))return {kind:'ambiguous',reason:'资源集号超出本季范围'};
    // 完整包必须有集号覆盖证据或明确“全 N 集”且数量与本季一致；单独 S01 不足以证明完整。
    const raw=String(t.title||'')+'\n'+String(t.description||'');
    const allCounts=[...raw.matchAll(/(?:全\s*(\d{1,4})\s*集|\b(\d{1,4})\s*episodes?\s*complete\b)/ig)].map(m=>Number(m[1]||m[2]));
    if(!eps.length&&allCounts.length&&allCounts.every(n=>n===task.total)&&(!meta.total_episode||meta.total_episode===task.total))eps=range(task.total);
    if(allCounts.some(n=>n!==task.total))return {kind:'ambiguous',reason:'资源宣称的全季集数与报告不一致'};
    if(!eps.length)return {kind:'ambiguous',reason:'无法确认资源实际包含哪些集'};
    if(meta.begin_episode!=null&&(!positive(meta.begin_episode)||!eps.includes(meta.begin_episode)))return {kind:'ambiguous',reason:'资源集号证据冲突'};
    if(meta.end_episode!=null&&(!positive(meta.end_episode)||!eps.includes(meta.end_episode)))return {kind:'ambiguous',reason:'资源结束集号冲突'};
    if(eps.some(n=>task.excluded.includes(n)))return {kind:'rejected',reason:'资源包含报告中的待播或播期未知集'};
    const full=eps.length===task.total&&eps.every((n,i)=>n===i+1);
    return {kind:'eligible',key:resourceKey(t),context,episodes:eps,full,seeds,title:String(t.title),site:String(t.site_name||t.site||''),sizeBytes:bytes(t.size),chineseMark:chineseMark(t,meta)};
  }
  function choose(task,contexts,covered=[]) {
    if(!task.tmdb&&!task.douban) {
      const matched=contexts.map(c=>c?.media_info).filter(m=>sameIdentity(task,m));
      if(['tmdb_id','douban_id'].some(k=>new Set(matched.map(m=>String(m[k]||'')).filter(Boolean)).size>1))return {selected:[],remaining:task.needed,ambiguous:true,identityAmbiguous:true};
    }
    const all=contexts.map(c=>candidate(task,c)),eligible=[...new Map(all.filter(c=>c.kind==='eligible'&&!c.episodes.some(n=>covered.includes(n))).map(c=>[c.key,c])).values()];
    let remaining=task.needed?.filter(n=>!covered.includes(n))??null;
    if(remaining?.length===0)return {selected:[],remaining,ambiguous:false};
    const pack=!covered.length?pick(eligible.filter(c=>c.full)):null;
    if(pack)return {selected:[pack],remaining:[],ambiguous:false};
    // 缺集未知时不猜零散集；整季包已在上一步独立检查。
    if(remaining===null)return {selected:[],remaining:null,ambiguous:true};
    const selected=[];
    while(remaining.length) {
      // 只比较真正补上剩余集目的资源，避免无关中文标签或小容量资源影响选择。
      const c=pick(eligible.filter(c=>!(c.full&&covered.length)&&c.episodes.some(n=>remaining.includes(n))),remaining);
      if(!c)break;selected.push(c);remaining=remaining.filter(n=>!c.episodes.includes(n));
    }
    return {selected,remaining,ambiguous:all.some(c=>c.kind==='ambiguous')};
  }
  function activeMatch(task,item,selected) {
    // 优先用现有下载的媒体 ID；缺少媒体 ID 时仅认与候选完全相同的种子标题。
    const media=item.media||{},title=String(item.title||'');
    if(identityConflict(task,media))return null;
    const exacts=selected.filter(c=>c.title===title),sameSite=exacts.filter(c=>item.site_name&&c.site===item.site_name),exact=sameSite[0]||exacts[0];
    // 已有任务的历史做种不可恢复；只在标题及站点唯一对应时引用本轮搜索快照。
    if(exact)return {episodes:exact.episodes,title,site:item.site_name||exact.site,key:exact.key,scope:exact.full?'season':exact.episodes.length===1?'episode':'episodes',sizeBytes:exact.sizeBytes,chineseMark:exact.chineseMark,selectionReason:'已存在下载任务，本轮未选择或提交',
      seeds:sameSite.length===1?exact.seeds:null,seedNote:sameSite.length===1?'同标题及站点唯一匹配的本轮搜索值，非原任务创建时做种数':'已有任务，但资源站点不能唯一对应，做种数未确认'};
    const named=!!task.year&&norm(item.name)===norm(task.title)&&String(item.year||'')===task.year;
    if(!sameIdentity(task,media)&&!named)return null;
    const ss=[...String(item.season_episode||'').matchAll(/S(\d{1,3})/ig)].map(m=>Number(m[1]));
    if(!ss.length||ss.some(n=>n!==task.season))return null;
    // 下载管理中的明确集号直接用于排除；裸 S01 不能证明整季已下载，保守阻止本季重复添加。
    const label=String(item.season_episode||'').trim(),match=/^S\d{1,3}\s*E(\d{1,4})(?:\s*[-~]\s*E?(\d{1,4}))?$/i.exec(label);
    let covered=[];
    if(match){const a=Number(match[1]),b=Number(match[2]||match[1]);if(positive(a)&&positive(b)&&b>=a&&b<=task.total)covered=range(b-a+1).map(n=>a+n-1);}
    return {episodes:covered,title:title||task.title,site:item.site_name||'',key:'existing:'+String(item.hash||title),scope:covered.length===1?'episode':covered.length?'episodes':'unknown',selectionReason:'下载管理已有任务，本轮未提交',seedNote:'下载管理现有任务，未查询对应资源的做种数'};
  }
  function outcome(task,records,{state,note='',color=null,remaining}={}) {
    const success=records.filter(r=>['accepted','existing'].includes(r.state));
    const covered=uniq(success.flatMap(r=>r.episodes));
    const resourceAudit=task.resourceAudit;
    if(resourceAudit?.confirmed&&resourceAudit.status==='连载中'&&!success.length&&color!=='yellow')color='blue';
    return {resourceAudit,state:state||'待核实',note:task.versionChoice?task.versionChoice.note+'；'+note:note,color,records:records.map(safeRecord),covered,
      remaining:remaining===undefined?task.needed?.filter(n=>!covered.includes(n))??null:remaining,at:new Date().toISOString()};
  }
  async function processTask(task,{search,active,submit,load,save,checkpoint,broadcast,progress=()=>{}}) {
    const versionInfo=Versions?.inspect(task);
    let records=readRecords(load(task.key)||[]);
    const combined=new Map(task.records.map(r=>[r.key,r]));for(const r of records)combined.set(r.key,r);records=[...combined.values()];
    if(versionInfo?.blocked)return outcome(task,records,{state:'版本待核实',note:versionInfo.blocked});
    if(versionInfo?.complete)return outcome(task,records,{state:'已有完整版本，待清理',color:'yellow',note:versionInfo.note+'：'+versionInfo.complete.label});
    if(versionInfo?.groups)task={...task,needed:range(task.total).filter(n=>!task.excluded.includes(n))};

    if(versionInfo?.groups&&records.length)return outcome(task,records,{state:'版本补缺待复核',color:'yellow',note:'混版目录已有本季下载记录，请先完成下载及第三脚本复核，避免用不同版本记录抵扣集数'});
    if(records.some(r=>r.episodes.some(n=>n>task.total)))return outcome(task,records,{state:'待核实',note:'历史下载集号与本次来源数量不一致'});
    // uncertain/submitting 跨刷新保留，人工核实前绝不再次提交。
    if(records.some(r=>['submitting','uncertain'].includes(r.state)))return outcome(task,records,{state:'提交结果待核实',note:'已有可能成功的提交，请在 MoviePilot 核实；本次不重复下载',color:records.some(r=>r.state==='accepted')?'yellow':null});
    if(records.some(r=>r.state==='existing'&&!r.episodes.length))return outcome(task,records,{state:'已有下载任务，待复核',color:'yellow',note:'已记录本季现有下载，但集号范围未确认；不能重复添加'});
    if(!records.length&&/已提交|已有下载|结果待核实/.test(task.previousState))return outcome(task,records,{state:'待复核',note:'报告记载已提交，但缺少任务记录；请核实，避免重复下载'});
    if(task.needed?.length===0)return outcome(task,records,{state:'无需下载',note:'报告没有已确认的缺失集号；重复版本或播出状态问题交第三脚本复核'});
    const recorded=uniq(records.filter(r=>['accepted','existing'].includes(r.state)).flatMap(r=>r.episodes));
    if(task.needed?.length&&task.needed.every(n=>recorded.includes(n)))return outcome(task,records,{state:'已提交下载，待复核',color:'yellow',note:'防重复记录已覆盖报告缺集，本次不再搜索或提交；若仍缺文件，请点击“检查整理问题”'});
    // 先读下载管理，再搜资源站；任务列表读取失败时不允许提交。
    const coverage=()=>uniq([...(task.versionChoice?.g.episodes||[]),...records.filter(r=>['accepted','existing'].includes(r.state)).flatMap(r=>r.episodes)]);
    async function inspectActive(verified=[]) {
      checkpoint();progress('核查下载管理中的现有任务');const current=await active();checkpoint();
      if(!Array.isArray(current))throw Error('无法核查 MoviePilot 现有任务，停止提交');
      let changed=false;
      for(const item of current){const match=activeMatch(task,item,verified);if(!match)continue;
        if(!records.some(r=>r.key===match.key||(item.hash&&r.id===String(item.hash)))){records.push(safeRecord({...match,state:'existing',id:item.hash||'',at:new Date().toISOString()}));changed=true;}
      }
      if(changed)save(task.key,records);
      if(versionInfo?.groups&&records.some(r=>r.state==='existing'))return outcome(task,records,{state:'已有下载任务，版本待复核',color:'yellow',note:'混版目录已有本季任务，先等现有任务完成并复核版本，本轮不追加'});
      if(records.some(r=>r.state==='existing'&&!r.episodes.length))return outcome(task,records,{state:'已有下载任务，待复核',color:'yellow',note:'下载管理已有本季任务，但覆盖集号未明确；本轮不追加，避免重复下载'});
      if(task.needed?.every(n=>coverage().includes(n)))return outcome(task,records,{state:'已有下载任务，待复核',color:'yellow',note:'下载管理或已提交记录已覆盖全部缺集，本轮无需追加'});
      return null;
    }
    const prior=await inspectActive();if(prior)return prior;
    checkpoint();progress('查询本剧本季资源');
    let contexts=await search(task);checkpoint();
    if(!Array.isArray(contexts))throw Error('MoviePilot 搜索结果格式不支持');
    // 覆盖统计不受做种数或语言筛选影响；只采信身份和季号明确的资源集号。
    const coverageEpisodes=uniq(contexts.flatMap(c=>{
      if(!sameIdentity(task,c.media_info))return [];const seasons=seasonList(c.meta_info);if(seasons.length!==1||seasons[0]!==task.season)return [];
      if([...String(c.torrent_info?.title||'').matchAll(/(?:^|[^a-z0-9])S(\d{1,3})(?!\d)/ig)].some(m=>Number(m[1])!==task.season))return [];
      const eps=c.meta_info?.episode_list;return Array.isArray(eps)&&eps.every(n=>positive(n)&&n<=task.total)?eps:[];
    }));
    let evidence=null;
    if(coverageEpisodes.length&&Math.max(...coverageEpisodes)<task.total){
      progress('资源集号与登记总数有差异，复核本季播出信息');
      try{evidence=typeof broadcast==='function'?await broadcast(task):{error:'当前接口未提供播出复核'};}catch(e){if(e.name==='Pause')throw e;evidence={error:e.message||'来源查询失败'};}
      checkpoint();
      if(evidence?.media&&!sameIdentity(task,evidence.media))evidence={error:'来源媒体身份与本剧不一致'};
    }
    const resourceAudit=Broadcast?.assess(task,coverageEpisodes,evidence);
    if(resourceAudit){const excluded=uniq([...task.excluded,...resourceAudit.exclude]);task={...task,resourceAudit,excluded,needed:task.needed?.filter(n=>!excluded.includes(n))??null};}
    if(task.needed?.length===0)return outcome(task,records,{state:'本季暂无可补的已播缺集',note:'来源复核后缺集均为待播或播期未确认，本轮不提交'});
    if(versionInfo?.groups){
      const selection=Versions.select(task,versionInfo,contexts,choose);
      if(selection.blocked)return outcome(task,records,{state:'版本待核实',note:selection.blocked});
      task={...task,needed:selection.needed,versionChoice:selection};contexts=selection.pool;
      progress(selection.note);
    }
    const accepted=records.filter(r=>['accepted','existing'].includes(r.state));
    let plan=choose(task,contexts,uniq(accepted.flatMap(r=>r.episodes)));
    if(plan.identityAmbiguous)return outcome(task,records,{state:'媒体身份待核实',note:'同名同年的结果对应多个媒体 ID，请先在报告中确认本剧 ID；本次不下载',color:accepted.length?'yellow':null});
    // 现有任务不受新下载的最低做种门槛限制；比较时保留真实做种数，不伪造报告值。
    const verified=contexts.map(context=>{const t=context.torrent_info||{},c=candidate(task,{...context,torrent_info:{...t,seeders:2,enclosure:t.enclosure||'existing-comparison-only'}});const n=typeof t.seeders==='number'?t.seeders:/^\d+$/.test(String(t.seeders))?Number(t.seeders):NaN;return {...c,seeds:Number.isFinite(n)&&n>=0?n:null};}).filter(c=>c.kind==='eligible');
    // 搜索期间或多资源提交之间可能出现新任务，每次提交前重读并重新计算未覆盖集号。
    while(true) {
      if(task.needed?.every(n=>coverage().includes(n)))break;
      const existing=await inspectActive(verified);if(existing)return existing;
      plan=choose(task,contexts,coverage());const c=plan.selected[0];if(!c)break;
      checkpoint();
      const record=safeRecord({key:c.key,state:'submitting',id:'',title:c.title,site:c.site,episodes:c.episodes,seeds:c.seeds,at:new Date().toISOString(),scope:c.full?'season':c.episodes.length===1?'episode':'episodes',sizeBytes:c.sizeBytes,estimatedBytes:c.estimatedBytes,chineseMark:c.chineseMark,selectionReason:task.versionChoice?task.versionChoice.note+'；'+c.selectionReason:c.selectionReason,seedNote:'提交前的站点搜索快照，不代表当前实时做种数'});
      records=records.filter(r=>r.key!==c.key);records.push(record);
      save(task.key,records); // 持久化成功是提交前置条件，保存失败时不发出请求。
      progress('提交下载：'+c.title);
      let reply;
      try {reply=await submit(c.context);}catch {
        record.state='uncertain';record.finishedAt=new Date().toISOString();save(task.key,records);
        return outcome(task,records,{state:'提交结果待核实',note:'下载请求未取得确定响应；请核实后再处理，不自动重试',color:records.some(r=>r.state==='accepted')?'yellow':null});
      }
      record.finishedAt=new Date().toISOString();
      if(reply?.success===true&&typeof reply.data?.download_id==='string'&&reply.data.download_id) {
        record.state='accepted';record.id=reply.data.download_id;save(task.key,records);
      } else if(reply?.success===false) {
        record.state='rejected';save(task.key,records);
        return outcome(task,records,{state:'下载提交失败',note:'MoviePilot 明确拒绝此任务；未标红为无资源',color:records.some(r=>r.state==='accepted')?'yellow':null});
      } else {
        record.state='uncertain';save(task.key,records);
        return outcome(task,records,{state:'提交结果待核实',note:'响应缺少可核对的下载任务 ID，不认定成功、不重复提交',color:records.some(r=>r.state==='accepted')?'yellow':null});
      }
    }
    const successful=records.some(r=>['accepted','existing'].includes(r.state));
    if(successful)return outcome(task,records,{state:records.some(r=>r.state==='accepted')?'已提交下载，待复核':'已有下载任务，待复核',color:'yellow',note:plan.remaining?.length?'已有任务或本轮提交只覆盖部分缺集，其余集号见下载未覆盖集号':'已有任务或本轮提交已覆盖缺集，等待实际下载完成后再复核'});
    if(plan.ambiguous)return outcome(task,records,{state:'待核实',note:'媒体身份、资源集号、做种数或报告缺集信息不足，不能自动确认可下载范围'});
    return outcome(task,records,{state:'无合格资源',color:'red',note:'未找到匹配本剧本季且做种人数不少于 2 的可用资源',remaining:plan.remaining});
  }
  // 复用 MoviePilot 对插件公开的 API 实例，认证由站点自己的拦截器负责。
  // 不读取 localStorage 令牌，不创建 API 密钥，不把异常对象（可能含认证头）写入报告。
  function createAdapter(api,origin) {
    if(!api||typeof api.get!=='function'||typeof api.post!=='function')throw Error('此 MoviePilot 页面未提供插件接口 MoviePilotAPI，请升级或检查前端版本');
    const base=new URL(api.defaults?.baseURL||'/api/v1/',origin);
    if(base.origin!==origin)throw Error('MoviePilot API 指向其他域名，停止执行');
    const get=async(path,params,timeout=180000)=>{
      try{return await api.get(path,{params,timeout});}catch(e){const status=Number(e?.response?.status)||0;throw Error(status===401||status===403?'MoviePilot 登录已失效或权限不足，请手动登录后继续':`MoviePilot 查询失败${status?'（HTTP '+status+'）':''}`);}
    };
    return {
      search:async task=>{
        const id=task.tmdb?'tmdb:'+task.tmdb:task.douban?'douban:'+task.douban:'';
        const results=[],seen=new Set();
        // 无 ID 时名称检索具有分页，读完后才能比较做种数；达到上限或重复页就不提交。
        for(let page=0;page<10;page++) {
          const result=id?await get('search/media/'+encodeURIComponent(id),{mtype:'电视剧',season:String(task.season),title:task.title,year:task.year,_ts:Date.now()}):
            await get('search/title',{keyword:task.title+' S'+String(task.season).padStart(2,'0'),page,_ts:Date.now()});
          if(result?.success===false&&/^(未搜索到任何资源|没有搜索到任何资源|No resources found)[。.!！]?$/.test(String(result.message||'')))return results;
          if(result?.success!==true||!Array.isArray(result.data))throw Error('MoviePilot 未完成有效搜索；保留原色，请在站点核查搜索状态');
          if(id)return result.data;
          if(!result.data.length)return results;
          const keys=result.data.map(c=>resourceKey(c.torrent_info||{}));
          if(keys.every(k=>seen.has(k)))throw Error('MoviePilot 名称检索返回重复页，无法确认已读取所有资源');
          result.data.forEach((c,i)=>{if(!seen.has(keys[i])){seen.add(keys[i]);results.push(c);}});
        }
        throw Error('MoviePilot 名称检索超过 10 页，请补充媒体 ID 后重试');
      },
      broadcast:async task=>{
        if(!task.tmdb)return {error:'缺少TMDB ID，未自动查询播出来源；请先用第一脚本补齐身份'};
        const media=await get('media/'+encodeURIComponent('tmdb:'+task.tmdb),{type_name:'电视剧',_ts:Date.now()},30000);
        if(!sameIdentity(task,media))return {error:'MoviePilot媒体详情身份不一致，未采用其播出状态'};
        const episodes=await get('tmdb/'+encodeURIComponent(task.tmdb)+'/'+task.season,{_ts:Date.now()},30000);
        if(!Array.isArray(episodes))return {error:'MoviePilot本季集目接口返回格式不支持'};
        return {media,episodes};
      },
      active:()=>get('download/',{},30000),
      submit:context=>api.post('download/',{media_in:context.media_info,torrent_in:context.torrent_info,downloader:null,save_path:null},{timeout:120000})
    };
  }
  return {Pause,episodes,taskFromRow,sameIdentity,candidate,choose,processTask,readRecords,safeRecord,outcome,createAdapter,activeMatch};
})
