/* 四阶段报告读写：以路径和季号定位任务，以颜色筛选；修改处理过的目录格及必要下载/复核事实列。
 * 使用原 XLSX 压缩包逐项更新，保留人工备注、公式、其他工作表及已有超链接。
 * 此模块不访问网络、不下载资源，也不执行云端文件操作。
 */
(function createWorkflowReport(X) {
  'use strict';
  const tags=(n,s)=>Array.from(n?.getElementsByTagNameNS('*',s)||[]);
  const children=(n,s)=>Array.from(n?.childNodes||[]).filter(c=>c.nodeType===1&&(!s||c.localName===s));
  const NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main';
  const FIELDS=['下载处理','下载资源','下载任务ID','下载覆盖集号','下载未覆盖集号','下载时间','下载说明','资源站最高集号','资源站覆盖集号','资源站查询时间','播出状态复核','播出复核依据','播出复核来源','下载记录'];
  const REVIEW_FIELDS=['复核结果','复核集数（豆瓣）','复核豆瓣链接','复核时间','复核视频文件数','复核已识别集数','复核缺失集号','保留版本','保留视频','拟删除文件','已验证删除文件','保留刮削文件','复核说明','复核清理记录'];
  const ARCHIVE_FIELDS=['归档结果','归档原路径','归档目标路径','归档云存储链接','归档时间','归档说明','归档记录'];
  const COLORS={blue:['BDD7EE','1F4E78'],yellow:['FFFF00','000000'],red:['FFC7CE','9C0006'],green:['C6EFCE','006100'],archived:['E4DFEC','7030A0']};
  const xml=s=>{
    if(/<!DOCTYPE|<!ENTITY/i.test(s))throw Error('报告包含不支持的 XML 实体声明');
    const d=new DOMParser().parseFromString(s,'application/xml');
    if(tags(d,'parsererror').length)throw Error('报告 XML 无法解析');return d;
  };
  function part(zip,path) {
    const f=zip.FileIndex[zip.FullPaths.indexOf(zip.FullPaths[0]+path)];
    if(!f)throw Error('报告缺少 '+path);
    return new TextDecoder('utf-8',{fatal:true}).decode(new Uint8Array(f.content));
  }
  function resolve(base,path) {
    const out=[];
    for(const p of (path.startsWith('/')?path.slice(1):base+path).split('/')) {
      if(p==='..'){if(!out.length)throw Error('Excel 关系路径越界');out.pop();}else if(p&&p!=='.')out.push(p);
    }
    return out.join('/');
  }
  function hueColor(rgb) {
    const s=String(rgb||'').slice(-6);if(!/^[a-f\d]{6}$/i.test(s))return null;
    const [r,g,b]=[0,2,4].map(i=>parseInt(s.slice(i,i+2),16)/255),max=Math.max(r,g,b),min=Math.min(r,g,b),d=max-min;
    const h=!d?0:60*(max===r?(g-b)/d+(g<b?6:0):max===g?(b-r)/d+2:(r-g)/d+4);
    return {h,s:max?d/max:0,v:max,rgb:s.toUpperCase()};
  }
  function colorKind(rgb) {
    const c=hueColor(rgb);if(!c)return 'unknown';
    if(c.rgb==='E4DFEC')return 'archived';
    if(c.h>=75&&c.h<=170&&c.s>=0.08&&c.v>=0.4)return 'green';
    if(c.h>=40&&c.h<=70&&c.s>=0.15&&c.v>=0.7)return 'yellow';
    return 'other';
  }
  // Excel 的主题色、索引色和 RGB 需统一解析，不能将无法解析的绿色当成待下载项。
  function fillReader(zip,styles) {
    const xfs=children(tags(styles,'cellXfs')[0],'xf'),fills=children(tags(styles,'fills')[0],'fill');
    let theme=[];
    if(zip.FullPaths.includes(zip.FullPaths[0]+'xl/theme/theme1.xml')) {
      theme=children(tags(xml(part(zip,'xl/theme/theme1.xml')),'clrScheme')[0]).map(n=>{
        const c=children(n)[0];return c?.getAttribute('lastClr')||c?.getAttribute('val');
      });
      // OOXML 主题索引是 lt1、dk1、lt2、dk2；文件内 clrScheme 顺序相反。
      [theme[0],theme[1],theme[2],theme[3]]=[theme[1],theme[0],theme[3],theme[2]];
    }
    const palette=children(tags(styles,'indexedColors')[0],'rgbColor').map(c=>c.getAttribute('rgb'));
    const standard=['000000','FFFFFF','FF0000','00FF00','0000FF','FFFF00','FF00FF','00FFFF'];
    return style=>{
      const xf=xfs[style],fill=fills[Number(xf?.getAttribute('fillId')||0)],pattern=tags(fill,'patternFill')[0];
      if(!xf||!fill)return 'unknown';
      if(!pattern||['none',''].includes(pattern.getAttribute('patternType')))return 'other';
      if(pattern.getAttribute('patternType')!=='solid')return 'unknown';
      const fg=tags(pattern,'fgColor')[0];if(!fg)return 'unknown';
      let rgb=fg.getAttribute('rgb');
      if(!rgb&&fg.hasAttribute('indexed')){const n=Number(fg.getAttribute('indexed'));rgb=palette[n]||standard[n<8?n:n-8];}
      if(!rgb&&fg.hasAttribute('theme'))rgb=theme[Number(fg.getAttribute('theme'))];
      if(rgb&&fg.hasAttribute('tint')) {
        const tint=Number(fg.getAttribute('tint')),c=String(rgb).slice(-6);
        if(!Number.isFinite(tint)||Math.abs(tint)>1)return 'unknown';
        rgb=[0,2,4].map(i=>{const v=parseInt(c.slice(i,i+2),16);return Math.round(tint<0?v*(1+tint):v*(1-tint)+255*tint).toString(16).padStart(2,'0');}).join('');
      }
      return colorKind(rgb);
    };
  }
  function target(data,link,origin,source,mode) {
    const name=String(data['剧集目录']||'').trim(),category=String(data['分类']||'').trim(),season=Number(data['季数']);
    // 初检的目录级记录没有季号；仅归档读取，保留为整剧阻挡项，绝不猜测为第一季。
    const directoryOnly=mode==='archive'&&String(data['季数']??'').trim()==='';
    if(!name||!category||/[\\/]/.test(name+category)||[name,category].some(s=>s==='.'||s==='..')||(!directoryOnly&&(!Number.isInteger(season)||season<1||season>999)))throw Error('剧名、分类或季号无效');
    const u=new URL(link),path=u.searchParams.get('path')?.replace(/\/$/,''),showPath=source+'/'+category+'/'+name;
    const leaf=path?.startsWith(showPath+'/')?path.slice(showPath.length+1):'',seasonMatch=/^Season\s+(\d+)$/i.exec(leaf);
    if(u.origin!==origin||u.username||u.password||u.hash||u.searchParams.get('page')!=='files'||(directoryOnly?path!==showPath:!seasonMatch||Number(seasonMatch[1])!==season))throw Error('源链接与本剧、本季或允许的云盘目录不一致');
    if(data['季目录路径']&&String(data['季目录路径']).replace(/^Root(?=\/)/,'')!==path)throw Error('季目录路径与源链接不一致');
    if(data['剧集目录路径']&&String(data['剧集目录路径']).replace(/^Root(?=\/)/,'')!==showPath)throw Error('剧集目录路径不一致');
    if(data['报告版本']&&data['报告版本']!=='tv-workflow/1')throw Error('不支持此报告版本');
    return {name,category,season:directoryOnly?null:season,directoryOnly,path,showPath,key:JSON.stringify([showPath,season,path]),data};
  }
  function read(bytes,{origin,source,destination='/115/Library_incs/nas.finish/电视剧',mode='download'}) {
    if(!['download','review','archive'].includes(mode))throw Error('报告处理模式不支持');
    if(bytes.byteLength>30*1024*1024)throw Error('报告不能超过 30 MB');
    const original=new Uint8Array(bytes).slice(),zip=X.CFB.read(original,{type:'array'});
    if(zip.FileIndex.reduce((n,f)=>n+(f.size||0),0)>150*1024*1024)throw Error('报告解压后过大');
    const wb=xml(part(zip,'xl/workbook.xml')),rels=xml(part(zip,'xl/_rels/workbook.xml.rels'));
    const relations=new Map(tags(rels,'Relationship').filter(r=>r.getAttribute('TargetMode')!=='External').map(r=>[r.getAttribute('Id'),resolve('xl/',r.getAttribute('Target'))]));
    const book=X.read(original,{type:'array',cellStyles:true,cellFormula:true}),fill=fillReader(zip,xml(part(zip,'xl/styles.xml')));
    const tasks=[],errors=[],sheets=new Map();let skipped=0;
    for(const entry of tags(wb,'sheet')) {
      const sheetName=entry.getAttribute('name'),sheet=book.Sheets[sheetName];if(!sheet?.['!ref'])continue;
      const range=X.utils.decode_range(sheet['!ref']);if(range.e.r>50000||range.e.c>300)throw Error('报告范围过大');
      const headers={},duplicates=[];
      for(let c=0;c<=range.e.c;c++){const h=String(sheet[X.utils.encode_cell({r:0,c})]?.v||'').trim();if(h){if(headers[h]!==undefined)duplicates.push(h);headers[h]=c;}}
      if(headers['剧集目录']===undefined)continue;
      const sheetPath=relations.get(entry.getAttribute('r:id')||entry.getAttributeNS('http://schemas.openxmlformats.org/officeDocument/2006/relationships','id'));
      if(!/^xl\/worksheets\/[^/]+\.xml$/.test(sheetPath||''))throw Error('工作表路径不支持');
      const doc=xml(part(zip,sheetPath)),cells=new Map(tags(doc,'c').map(c=>[c.getAttribute('r'),c])),links=new Map();
      const relPath=sheetPath.replace(/([^/]+)$/,'_rels/$1.rels');
      if(zip.FullPaths.includes(zip.FullPaths[0]+relPath)) {
        const rs=new Map(tags(xml(part(zip,relPath)),'Relationship').map(r=>[r.getAttribute('Id'),r.getAttribute('Target')]));
        for(const h of tags(doc,'hyperlink'))links.set(h.getAttribute('ref'),rs.get(h.getAttribute('r:id')||h.getAttributeNS('http://schemas.openxmlformats.org/officeDocument/2006/relationships','id')));
      }
      sheets.set(sheetPath,{headers,range,headerStyle:Number(cells.get('A1')?.getAttribute('s')||0),cellStyles:new Map([...cells].map(([ref,c])=>[ref,Number(c.getAttribute('s')||0)])),formulas:new Set(Object.keys(sheet).filter(k=>sheet[k]?.f))});
      for(let row=1;row<=range.e.r;row++) {
        const ref=X.utils.encode_cell({r:row,c:headers['剧集目录']}),cell=sheet[ref];if(!cell?.v)continue;
        const style=Number(cells.get(ref)?.getAttribute('s')||0),color=fill(style);
        // 归档须读取所有颜色，防止只看绿色一季而漏掉同剧的黄色/蓝色季。
        if(mode!=='archive'&&(color==='green'||color==='archived')){skipped++;continue;}
        if(mode==='review'&&color!=='yellow'&&color!=='unknown'){skipped++;continue;}
        try {
          if(duplicates.length)throw Error('重复表头：'+duplicates.join('、'));
          if(color==='unknown')throw Error('无法识别目录格填色，保留原样');
          if(tags(doc,'conditionalFormatting').length)throw Error('表格含条件格式，须改为普通填色后再处理');
          if((sheet['!merges']||[]).some(m=>row>=m.s.r&&row<=m.e.r&&headers['剧集目录']>=m.s.c&&headers['剧集目录']<=m.e.c))throw Error('目录格已合并');
          if((mode!=='download'?['分类','季数','源云存储链接']:['分类','季数','源云存储链接','来源登记集数','缺失集号']).some(h=>headers[h]===undefined))throw Error('缺少分类、季号、来源数量、缺集或源链接列');
          const data={};
          for(const [h,col] of Object.entries(headers)) {
            const c=sheet[X.utils.encode_cell({r:row,c:col})];
            if(mode==='archive'&&c?.f&&(ARCHIVE_FIELDS.includes(h)||['处理结果','工作阶段'].includes(h)||/^归档记录续\d+$/.test(h)))throw Error(h+'不能使用公式；请先保留人工公式至其他列');
            if(c?.f&&(/^整理修复/.test(h)||h==='文件核查时间'))throw Error(h+'不能使用公式');
            if(c?.f&&(['分类','剧集目录','季数','源云存储链接','来源登记集数','缺失集号','TMDB ID','下载记录','待播集号','播期未知集号','豆瓣链接','来源链接'].includes(h)||/^下载记录续\d+$/.test(h)))throw Error(h+'不能使用公式');
            data[h]=c?.v??'';
          }
          if(mode!=='archive'&&/已归档/.test(String(data['处理结果'])+String(data['工作阶段'])+String(data['归档结果']))){skipped++;continue;}
          const linkRef=X.utils.encode_cell({r:row,c:headers['源云存储链接']});
          tasks.push({...target(data,String(links.get(linkRef)||data['源云存储链接']),origin,source,mode),sheetName,sheetPath,ref,row,style,color});
        } catch(e){errors.push({sheetName,ref,error:e.message});}
      }
    }
    // 同一季出现多行时可共用防重复记录，但报告位置仍分别保留。
    return {original,tasks,errors,skipped,sheets,updates:new Map(),mode,origin,destination};
  }
  const escape=s=>String(s??'').replace(/[\x00-\x08\x0B\x0C\x0E-\x1F]/g,'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  // 仅替换指定单元格，其他单元格原始 XML 字节不动；新增下载列使用 inlineStr，避免公式执行。
  function putCell(text,ref,value,style) {
    const row=Number(/\d+$/.exec(ref)[0]),cell=typeof value==='number'&&Number.isFinite(value)?`<c r="${ref}" s="${style}" t="n"><v>${value}</v></c>`:`<c r="${ref}" s="${style}" t="inlineStr"><is><t xml:space="preserve">${escape(value)}</t></is></c>`;
    let found=false;
    text=text.replace(/<row\b[^>]*\br=["'](\d+)["'][^>]*(?:\/>|>[\s\S]*?<\/row>)/g,(all,n)=>{
      if(Number(n)!==row)return all;found=true;
      const re=new RegExp('<c\\b(?=[^>]*\\br=["\']'+ref+'["\'])[^>]*(?:\\/>|>[\\s\\S]*?<\\/c>)');
      if(re.test(all))return all.replace(re,()=>cell);
      return all.endsWith('/>')?all.slice(0,-2)+'>'+cell+'</row>':all.replace('</row>',()=>cell+'</row>');
    });
    if(!found)throw Error('报告中找不到行 '+row);return text;
  }
  // 每轮导出追加一个明细快照工作表；已有明细和人工维护页原样保留。
  // 同一会话重复导出始终从导入文件生成，因此不会重复叠加本轮明细。
  function auditRows(session) {
    const rows=[],seen=new Set(),download=session.mode==='download';
    const header=download?['报告位置','电视剧','季号','资源类型','对应集号','站点','做种数（查询时）','资源名称','下载任务ID','执行结果','请求/发现时间（UTC）','响应时间（UTC）','说明','资源容量（字节）','估算每个待补集容量（字节）','中文标记','选择依据','做种数说明']:
      ['报告位置','电视剧','季号','操作','操作批次','原路径','目标路径','文件类型','文件大小','云盘文件修改时间','处理原因','关联保留/删除视频','执行结果','请求时间（UTC）','验证时间（UTC）','说明'];
    function add(task,event) {
      const key=JSON.stringify([task.key,event]);if(seen.has(key))return;seen.add(key);
      rows.push([task.sheetName+'!'+task.ref,task.name,task.season,...event]);
    }
    for(const [task,result] of session.updates) {
      if(download) {
        const states={accepted:'已提交（不代表下载完成）',existing:'已有任务，本轮未提交',submitting:'请求已准备，结果待核实',uncertain:'提交结果待核实',rejected:'提交被拒绝'};
        for(const r of result.records||[])add(task,[{season:'单季整包',episode:'单集',episodes:'零散多集',unknown:'类型未确认'}[r.scope]||'类型未确认',(r.episodes||[]).join('、')||'未确认',r.site||'未记录',r.seeds??'未记录',r.title,r.id,states[r.state]||r.state,r.at,r.finishedAt||'',result.note||'',r.sizeBytes??'未记录',r.estimatedBytes??'不适用/未记录',r.chineseMark||'未确认',r.selectionReason||'未记录',r.seedNote||(r.seeds==null?'历史记录缺少做种数，不能按0解释':'历史保存的搜索做种数，非实时值')]);
        if(!result.records?.length)add(task,['','','','未选定资源','','',result.state,result.at,'',result.note||'','','','','','未选定下载资源，无对应做种数；具体原因见说明']);
        continue;
      }
      const record=result.record||{},journal=record.journal||record;
      if(session.mode==='review') {
        const states={verified:'删除已验证','manual-verified':'人工确认已删除',cancelled:'人工确认未删除','not-submitted':'未提交删除'};
        const events=[...journal.history||[],...journal.records||[],...(journal.pending?(journal.pending.files||[journal.pending]).map(f=>({...f,error:journal.pending.error||f.error,state:'pending'})):[])];
        for(const e of events) {
          const f=e.before||e;add(task,['删除',e.batchId||e.planId||journal.planId||'',e.path,'',e.kind==='scrape'?'刮削文件':'视频/文件',f.size||'',f.modified||'',e.reason||'',e.owner||'',states[e.state]||'删除结果待核实',e.at||'',e.finishedAt||'',e.error||'']);
        }
        const before=record.before||record;
        for(const [files,action] of [[before.remove,'计划删除'],[before.keep,'保留视频'],[record.preserved,'保留刮削文件']])for(const f of files||[]) {
          if(typeof f!=='object'||action==='计划删除'&&events.some(e=>e.path===f.path))continue;
          add(task,[action,record.planId||'',f.path,'',f.kind||'',f.size||'',f.modified||'',f.reason||'',f.owner||'',action==='计划删除'?'尚未执行':'保留','','',result.note||'']);
        }
      } else {
        const events=[...journal.history||[],...journal.operations||record.operations||[],...((journal.cleanup||record.cleanup)?[journal.cleanup||record.cleanup]:[])];
        // 兼容旧版整剧日志；旧日志没有时间/属性的字段明确留空，不能推造。
        const operations=events.length||Array.isArray(journal.operations)||Array.isArray(record.operations)?events:[{...journal,source:journal.source||record.source,target:journal.target||record.target,manifest:journal.manifest||record.manifest||[],state:journal.verified?'verified':journal.pending?'pending':'planned'}];
        for(const op of operations)for(const f of op.manifest||[]) {
          if(!f.path||!(f.path===task.path||f.path.startsWith(task.path+'/')||!f.path.includes('/Season ')))continue;
          // 根部共用文件属于整剧；同一季文件只出现在对应报告行，避免跨季误记。
          if(f.path!==task.path&&!f.path.startsWith(task.path+'/')&&/\/season\s+\d+(?:\/|$)/i.test(f.path))continue;
          add(task,[op.kind==='source-directory'?'删除源目录及已列多余资料':op.kind==='empty-directory'?'删除源空目录':'移动',op.planId||journal.planId||record.planId||'',f.path,op.target&&op.source?op.target+f.path.slice(op.source.length):'',f.directory?'目录':'文件',f.size||'',f.modified||'',op.reason||'绿色剧集归档','',(['empty-directory','source-directory'].includes(op.kind)?{verified:'删除已验证',pending:'删除结果待核实','not-submitted':'未提交删除',planned:'尚未执行'}:{verified:'移动已验证',pending:'移动结果待核实','not-submitted':'未提交移动',planned:'尚未执行'})[op.state]||op.state||'尚未执行',op.at||'',op.finishedAt||'',op.error||'']);
        }
        for(const f of record.preserved||journal.preserved||[]) {
          const skip=(record.skipped||journal.skipped||[]).find(s=>f.path===s.source||f.path.startsWith(s.source+'/'));
          if(skip&&skip.source!==task.path)continue;
          add(task,[skip?'跳过冲突':'保留',record.planId||journal.planId||'',f.path,skip?skip.target+f.path.slice(skip.source.length):'',f.directory?'目录':'文件',f.size||'',f.modified||'',skip?skip.reason:'合并季时保留源剧集共用文件','','未移动','','','不覆盖目标']);
        }
      }
      if(!rows.some(r=>r[0]===task.sheetName+'!'+task.ref))add(task,['检查','','','','','','','','',result.state,'',result.at||'',result.note||'']);
    }
    return [header,...rows];
  }
  function repairAuditRows(session) {
    const rows=[['报告位置','电视剧','季号','集号','整理历史ID','下载源文件','整理目标文件','操作','执行结果','请求时间','完成时间','说明','旧历史快照']];
    for(const [task,result] of session.updates)if(result.repair) {
      const r=result.repair;
      for(const i of [...r.previousItems||[],...r.items||[]])for(const step of i.steps?.length?i.steps:[{action:'检查/预览',state:i.state,at:r.checkedAt}])rows.push([task.sheetName+'!'+task.ref,task.name,task.season,i.episode,i.id,i.source,i.target,step.action,step.state,step.at||'',step.finishedAt||'',String(step.error||i.note||r.note||'').slice(0,25000),JSON.stringify(i.history||{})]);
      if(!r.items?.length)rows.push([task.sheetName+'!'+task.ref,task.name,task.season,'','','','','检查',r.state,r.at,'',String(r.note||'').slice(0,25000),'']);
    }
    return rows;
  }
  function appendAudit(zip,session,put,repair=false) {
    const table=repair?repairAuditRows(session):auditRows(session);if(table.length===1)return;
    if(table.length>50001)throw Error('操作明细超过 50000 行，请拆分报告');
    const wb=xml(part(zip,'xl/workbook.xml')),rels=xml(part(zip,'xl/_rels/workbook.xml.rels')),types=xml(part(zip,'[Content_Types].xml'));
    const names=new Set(tags(wb,'sheet').map(s=>s.getAttribute('name'))),ids=new Set(tags(rels,'Relationship').map(r=>r.getAttribute('Id')));
    const prefix=repair?'整理修复明细':{download:'下载明细',review:'清理明细',archive:'归档明细'}[session.mode];let n=1;const partName=repair?'tvRepairAudit':'tvAudit',relName=repair?'rIdTVRepairAudit':'rIdTVAudit';
    while(names.has(prefix+'_'+n)||ids.has(relName+n)||zip.FullPaths.includes(zip.FullPaths[0]+'xl/worksheets/'+partName+n+'.xml'))n++;
    const name=prefix+'_'+n,path='xl/worksheets/'+partName+n+'.xml',id=relName+n;
    const sheet=wb.createElementNS(NS,'sheet');sheet.setAttribute('name',name);sheet.setAttribute('sheetId',Math.max(0,...tags(wb,'sheet').map(s=>Number(s.getAttribute('sheetId'))))+1);
    sheet.setAttributeNS('http://schemas.openxmlformats.org/officeDocument/2006/relationships','r:id',id);tags(wb,'sheets')[0].appendChild(sheet);
    const rel=rels.createElementNS(rels.documentElement.namespaceURI,'Relationship');rel.setAttribute('Id',id);rel.setAttribute('Type','http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet');rel.setAttribute('Target',path.slice(3));rels.documentElement.appendChild(rel);
    const type=types.createElementNS(types.documentElement.namespaceURI,'Override');type.setAttribute('PartName','/'+path);type.setAttribute('ContentType','application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml');types.documentElement.appendChild(type);
    const end=X.utils.encode_cell({r:table.length-1,c:table[0].length-1});
    const headerStyle=session.sheets.values().next().value?.headerStyle||0;
    const data=table.map((row,r)=>'<row r="'+(r+1)+'">'+row.map((v,c)=>{
      if(String(v??'').length>32767)throw Error('操作明细单元格过长，未截断，请拆分报告');
      const ref=X.utils.encode_cell({r,c}),s=r===0?headerStyle:0;return typeof v==='number'?`<c r="${ref}" s="${s}" t="n"><v>${v}</v></c>`:`<c r="${ref}" s="${s}" t="inlineStr"><is><t xml:space="preserve">${escape(v)}</t></is></c>`;
    }).join('')+'</row>').join('');
    put(path,`<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="${NS}"><dimension ref="A1:${end}"/><cols>${table[0].map((h,i)=>`<col min="${i+1}" max="${i+1}" width="${/路径|说明|原因|资源名称|关联/.test(h)?60:/时间|电视剧/.test(h)?30:22}" customWidth="1"/>`).join('')}</cols><sheetData>${data}</sheetData><autoFilter ref="A1:${end}"/></worksheet>`);
    for(const [p,d] of [['xl/workbook.xml',wb],['xl/_rels/workbook.xml.rels',rels],['[Content_Types].xml',types]])put(p,new XMLSerializer().serializeToString(d));
  }
  function write(session) {
    if(!session.updates.size)return session.original.slice();
    const zip=X.CFB.read(session.original,{type:'array'}),styles=xml(part(zip,'xl/styles.xml'));
    const fonts=tags(styles,'fonts')[0],fills=tags(styles,'fills')[0],xfs=tags(styles,'cellXfs')[0],styleMap=new Map(),sheets=new Map();
    const reviewing=session.mode==='review',archiving=session.mode==='archive',baseFields=archiving?ARCHIVE_FIELDS:reviewing?REVIEW_FIELDS:FIELDS,recordColumn=baseFields.at(-1),continuationPrefix=recordColumn+'续';
    const recordsText=r=>JSON.stringify(reviewing||archiving?r.record||{}:r.records||[]);
    const linkParts=new Map();
    function setSourceLink(item,sheetPath,ref,value) {
      const links=tags(xml(item.text),'hyperlink');
      if(links.some(h=>h.getAttribute('ref').includes(':')&&(()=>{try{const r=X.utils.decode_range(h.getAttribute('ref')),c=X.utils.decode_cell(ref);return c.r>=r.s.r&&c.r<=r.e.r&&c.c>=r.s.c&&c.c<=r.e.c;}catch{return true;}})()))throw Error('来源链接使用范围超链接，请先拆分为单元格链接');
      const pattern=new RegExp('<hyperlink\\b(?=[^>]*\\bref=["\']'+ref+'["\'])[^>]*(?:\\/>|>[\\s\\S]*?<\\/hyperlink>)');
      if(!value){item.text=item.text.replace(pattern,'');return;}
      if(archiving) {
        const u=new URL(value),p=u.searchParams.get('path');
        if(u.origin!==session.origin||u.username||u.password||u.hash||u.searchParams.get('page')!=='files'||!p?.startsWith(session.destination+'/')||p.split('/').some(x=>x==='.'||x==='..'))throw Error('归档链接超出指定云盘目标');
      }else if(!/^https:\/\/movie\.douban\.com\/subject\/\d+\/$/.test(value))throw Error('复核来源链接不是有效豆瓣详情页');
      const relPath=sheetPath.replace(/([^/]+)$/,'_rels/$1.rels');
      if(!linkParts.has(relPath))linkParts.set(relPath,xml(zip.FullPaths.includes(zip.FullPaths[0]+relPath)?part(zip,relPath):'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'));
      const doc=linkParts.get(relPath),ids=new Set(tags(doc,'Relationship').map(n=>n.getAttribute('Id')));let n=1;while(ids.has('rIdTVReview'+n))n++;
      const id='rIdTVReview'+n,relationship=doc.createElementNS('http://schemas.openxmlformats.org/package/2006/relationships','Relationship');
      relationship.setAttribute('Id',id);relationship.setAttribute('Type','http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink');relationship.setAttribute('Target',value);relationship.setAttribute('TargetMode','External');doc.documentElement.appendChild(relationship);
      if(!/xmlns:r=/.test(item.text))item.text=item.text.replace('<worksheet','<worksheet xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"');
      const link=`<hyperlink ref="${ref}" r:id="${id}"/>`;
      if(pattern.test(item.text))item.text=item.text.replace(pattern,()=>link);
      else if(item.text.includes('</hyperlinks>'))item.text=item.text.replace('</hyperlinks>',()=>link+'</hyperlinks>');
      else if(/<hyperlinks\b[^>]*\/>/.test(item.text))item.text=item.text.replace(/<hyperlinks\b[^>]*\/>/,()=>'<hyperlinks>'+link+'</hyperlinks>');
      else {
        // hyperlinks 必须位于 sheetData/autoFilter 等节点之后、pageMargins 等节点之前。
        const later=/<(?:printOptions|pageMargins|pageSetup|headerFooter|rowBreaks|colBreaks|drawing|legacyDrawing|tableParts|extLst)\b/;
        const at=later.exec(item.text)?.index??item.text.indexOf('</worksheet>');
        if(at<0)throw Error('工作表结构不支持来源链接更新');item.text=item.text.slice(0,at)+'<hyperlinks>'+link+'</hyperlinks>'+item.text.slice(at);
      }
    }
    // Excel 单个单元格最多 32767 字符。机器记录分片写入，避免长剧/多资源报告被截断。
    let chunks=Math.max(1,...[...session.updates.values()].map(r=>Math.ceil(recordsText(r).length/30000)));
    for(const info of session.sheets.values())for(const h of Object.keys(info.headers))if(h.startsWith(continuationPrefix)&&/^\d+$/.test(h.slice(continuationPrefix.length)))chunks=Math.max(chunks,Number(h.slice(continuationPrefix.length))+1);
    if(chunks>100)throw Error('下载记录超过支持范围，请按分类拆分报告');
    const fields=[...baseFields,...Array.from({length:chunks-1},(_,i)=>continuationPrefix+(i+1))];
    const make=n=>styles.createElementNS(NS,n),add=(group,node,name)=>{const i=children(group,name).length;group.appendChild(node);group.setAttribute('count',i+1);return i;};
    function statusStyle(old,color) {
      const key=old+':'+color;if(styleMap.has(key))return styleMap.get(key);
      const xf=children(xfs,'xf')[old]?.cloneNode(true);if(!xf)throw Error('原样式不存在');
      const font=children(fonts,'font')[Number(xf.getAttribute('fontId')||0)]?.cloneNode(true);if(!font)throw Error('原字体不存在');
      for(const c of children(font,'color'))font.removeChild(c);
      const ink=make('color');ink.setAttribute('rgb','FF'+COLORS[color][1]);font.appendChild(ink);
      const fill=make('fill'),p=make('patternFill'),fg=make('fgColor');p.setAttribute('patternType','solid');fg.setAttribute('rgb','FF'+COLORS[color][0]);p.appendChild(fg);fill.appendChild(p);
      xf.setAttribute('fontId',add(fonts,font,'font'));xf.setAttribute('fillId',add(fills,fill,'fill'));xf.setAttribute('applyFill','1');xf.setAttribute('applyFont','1');
      const id=add(xfs,xf,'xf');styleMap.set(key,id);return id;
    }
    for(const [task,result] of session.updates) {
      if(!session.tasks.includes(task))throw Error('任务不属于当前报告');
      if(!sheets.has(task.sheetPath)) {
        const info=session.sheets.get(task.sheetPath),headers={...info.headers};let last=info.range.e.c;
        let text=part(zip,task.sheetPath);
        if(/<(?:[\w.-]+):(?:row|c)\b/.test(text))throw Error('不支持带前缀的工作表单元格 XML');
        const newCols=[];
        // 修复记录独立追加，不改动原下载日志或人工备注；超长快照分片保存。
        const repairUpdates=[...session.updates.values()].filter(r=>r.repair);
        const repairChunks=Math.max(1,...repairUpdates.map(r=>Math.ceil(JSON.stringify(r.repair).length/30000)),...Object.keys(headers).filter(h=>/^整理修复记录续\d+$/.test(h)).map(h=>Number(h.match(/\d+$/)[0])+1));
        const extra=repairUpdates.length?['整理修复状态','整理修复集号','整理修复时间','整理修复说明',...Array.from({length:repairChunks},(_,i)=>i?'整理修复记录续'+i:'整理修复记录')]:[];
        if([...session.updates.values()].some(r=>r.fileCheckedAt))extra.push('文件核查时间');
        for(const h of extra)if(headers[h]===undefined){headers[h]=++last;text=putCell(text,X.utils.encode_cell({r:0,c:last}),h,info.headerStyle);newCols.push('<col min="'+(last+1)+'" max="'+(last+1)+'" width="45" customWidth="1"/>');}
        for(const h of fields)if(headers[h]===undefined){headers[h]=++last;text=putCell(text,X.utils.encode_cell({r:0,c:last}),h,info.headerStyle);newCols.push(`<col min="${last+1}" max="${last+1}" width="${/说明|资源|记录/.test(h)?55:24}" customWidth="1"/>`);}
        if(newCols.length) {
          text=text.includes('</cols>')?text.replace('</cols>',()=>newCols.join('')+'</cols>'):text.replace('<sheetData',()=>'<cols>'+newCols.join('')+'</cols><sheetData');
          const end=X.utils.encode_cell({r:info.range.e.r,c:last});
          text=text.replace(/<dimension\b[^>]*\/>/,`<dimension ref="A1:${end}"/>`);
          text=text.replace(/(<autoFilter\b[^>]*\bref=["'])[^"']+(["'])/,(_m,a,b)=>a+'A1:'+end+b);
        }
        sheets.set(task.sheetPath,{text,headers});
      }
      const item=sheets.get(task.sheetPath);
      if(result.repair){
        const r=result.repair,json=JSON.stringify(r),values={'整理修复状态':({ready:'待确认修复',blocked:'待人工核实',checked:'已检查/可重新检查',submitted:'已提交整理，待云盘复核',uncertain:'结果待核实','history-delete-pending':'历史删除结果待核实','history-deleted':'历史已解除，整理待完成','transfer-pending':'整理请求待核实','files-verified':'云盘文件已复核'})[r.state]||r.state,'整理修复集号':r.episodes.join('、'),'整理修复时间':r.at,'整理修复说明':String(r.note||'').slice(0,25000)};
        for(const h of Object.keys(item.headers))if(h==='整理修复记录'||/^整理修复记录续\d+$/.test(h)){const n=h==='整理修复记录'?0:Number(h.match(/\d+$/)[0]);values[h]=json.slice(n*30000,(n+1)*30000);}
        for(const [h,v] of Object.entries(values))item.text=putCell(item.text,X.utils.encode_cell({r:task.row,c:item.headers[h]}),v,0);
      }
      if(result.fileCheckedAt)item.text=putCell(item.text,X.utils.encode_cell({r:task.row,c:item.headers['文件核查时间']}),result.fileCheckedAt,0);
      if(result.color) {
        if(!COLORS[result.color])throw Error('不支持的结果颜色');
        const st=statusStyle(task.style,result.color);
        item.text=item.text.replace(/<c\b[^>]*>/g,tag=>{
          if(!new RegExp('\\br=["\']'+task.ref+'["\']').test(tag))return tag;
          return /\bs=["'][^"']*["']/.test(tag)?tag.replace(/\bs=["'][^"']*["']/,'s="'+st+'"'):tag.replace(/\/?>$/,end=>' s="'+st+'"'+end);
        });
      }
      const compact=values=>{const out=[];for(const n of values){const tail=out.at(-1);if(tail&&n===tail[1]+1)tail[1]=n;else out.push([n,n]);}return out.map(([a,b])=>a===b?a:a+'-'+b).join('、');};
      const records=result.records||[],json=recordsText(result),values=archiving?
        [result.state,result.source||task.showPath,result.target||'',result.targetUrl||'',result.at,result.note]:reviewing?
        [result.state,result.total??'',result.sourceUrl||'',result.at,result.videoCount??'',result.current??'',result.missing==null?'未确认':compact(result.missing),result.version||'',(result.keep||[]).join('\n').slice(0,25000),(result.remove||[]).join('\n').slice(0,25000),(result.deleted||[]).join('\n').slice(0,25000),(result.preserved||[]).join('\n').slice(0,25000),result.note]:
        [result.state,records.map(r=>r.site+' | '+r.title).join('\n').slice(0,25000),records.map(r=>r.id||'').filter(Boolean).join('、').slice(0,25000),compact(result.covered||[]),result.remaining==null?'未确认':compact(result.remaining),result.at,result.note,...(result.resourceAudit?[result.resourceAudit.maximum??'',compact(result.resourceAudit.coverage||[]),result.resourceAudit.at||'',result.resourceAudit.status||'',result.resourceAudit.basis||'',result.resourceAudit.sourceUrl||'']:['资源站最高集号','资源站覆盖集号','资源站查询时间','播出状态复核','播出复核依据','播出复核来源'].map(h=>task.data[h]??''))];
      values.push(...Array.from({length:chunks},(_,i)=>json.slice(i*30000,(i+1)*30000)));
      fields.forEach((h,i)=>{item.text=putCell(item.text,X.utils.encode_cell({r:task.row,c:item.headers[h]}),values[i],0);});
      if(reviewing)setSourceLink(item,task.sheetPath,X.utils.encode_cell({r:task.row,c:item.headers['复核豆瓣链接']}),result.sourceUrl||'');
      if(archiving)setSourceLink(item,task.sheetPath,X.utils.encode_cell({r:task.row,c:item.headers['归档云存储链接']}),result.targetUrl||'');
      // 下载阶段仅在有明确连载依据时同步原播出状态；黄色任务状态优先于蓝色。
      if(!reviewing&&!archiving&&result.resourceAudit?.confirmed&&result.resourceAudit.status==='连载中'&&item.headers['播出状态']!==undefined){
        const ref=X.utils.encode_cell({r:task.row,c:item.headers['播出状态']}),info=session.sheets.get(task.sheetPath);
        if(!info.formulas.has(ref))item.text=putCell(item.text,ref,'连载中',info.cellStyles.get(ref)||0);
      }
      // 复核时更新必要事实列；手工备注和已有公式不覆盖，原单元格样式也保留。
      if(reviewing&&result.cells)for(const [h,v] of Object.entries(result.cells)) {
        if(!['视频文件数','已识别集数','来源登记集数','缺失集号','数据来源','来源链接','来源查询时间','集数核对','处理结果','豆瓣链接','播出状态','已识别集号','重复集号','无法识别集号的文件','待播集号','播期未知集号'].includes(h)||item.headers[h]===undefined)continue;
        const ref=X.utils.encode_cell({r:task.row,c:item.headers[h]}),info=session.sheets.get(task.sheetPath);
        if(!info.formulas.has(ref)) {
          item.text=putCell(item.text,ref,v,info.cellStyles.get(ref)||0);
          if(h==='来源链接'||h==='豆瓣链接')setSourceLink(item,task.sheetPath,ref,String(v||''));
        }
      }
    }
    const put=(p,s)=>X.CFB.utils.cfb_add(zip,zip.FullPaths[0]+p,new TextEncoder().encode(s));
    put('xl/styles.xml',new XMLSerializer().serializeToString(styles));for(const [p,item] of sheets)put(p,item.text);
    for(const [p,doc] of linkParts)put(p,new XMLSerializer().serializeToString(doc));
    appendAudit(zip,session,put);appendAudit(zip,session,put,true);
    return new Uint8Array(X.CFB.write(zip,{type:'array',fileType:'zip',compression:true}));
  }
  return {read,write,colorKind,FIELDS,REVIEW_FIELDS,ARCHIVE_FIELDS};
})
