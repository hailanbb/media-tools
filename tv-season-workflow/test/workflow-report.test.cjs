// 报告回归：构造含手工备注、公式和多个工作表的 Excel，验证必要变更以外的内容保留。
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const dom=require('@xmldom/xmldom');global.DOMParser=dom.DOMParser;global.XMLSerializer=dom.XMLSerializer;
const C=require('../season-download.user.js'),I=require('../season-inspect.user.js'),R=C.reportEngine();
const bundle=fs.readFileSync(path.join(__dirname,'../vendor/xlsx-js-style/package/dist/xlsx.bundle.js'),'utf8');
const X=new Function('const exports={};const module=undefined,require=undefined,window=undefined,define=undefined;'+bundle+';return exports;')();
const options={origin:'https://clouddrive.example',source:I.SOURCE};
test('源多余共用资料删除逐项列出，不能写成移动',()=>{
  const s=R.read(fixture(),{...options,mode:'archive'}),t=s.tasks[0];
  s.updates.set(t,{state:'已归档',record:{operations:[],cleanup:{kind:'source-directory',source:t.showPath,state:'verified',at:'start',finishedAt:'end',reason:'保留目标同名资料',manifest:[{path:t.showPath,directory:true},{path:t.showPath+'/poster.jpg',size:'1 MB',modified:'2026-10-23 08:00'}]}}});
  const rows=X.utils.sheet_to_json(X.read(R.write(s)).Sheets['归档明细_1']);assert.equal(rows.length,2);assert.ok(rows.every(r=>r['操作']==='删除源目录及已列多余资料'&&r['执行结果']==='删除已验证'));assert.equal(rows[1]['文件大小'],'1 MB');
});
test('整理修复记录跨阶段分片往返，原下载日志和人工备注保留，独立输出历史删除明细',()=>{
  const s=R.read(fixture(),options),t=s.tasks[1],repair={schema:1,taskKey:t.key,state:'submitted',at:new Date().toISOString(),episodes:[2],note:'等待云盘复核',items:[{id:9,episode:2,source:'/downloads/剧.S01E02.mkv',target:'/media/剧/Season 1/剧.S01E02.mkv',history:{id:9,title:'测试剧'},steps:[{action:'仅删除整理历史',state:'verified',at:'start',finishedAt:'end'}]}],previousItems:[]};
  repair.note+='长记录'.repeat(12000);s.updates.set(t,{...result(),repair});
  const bytes=R.write(s),round=R.read(bytes,{...options,mode:'review'}),row=round.tasks.find(r=>r.ref===t.ref),parsed=C.RepairState.read(row.data);
  assert.equal(parsed.note,repair.note);assert.equal(row.data['下载任务ID'],'abc');assert.equal(row.data['核对说明'],'手工备注 3');
  const book=X.read(bytes);assert.ok(book.Sheets['整理修复明细_1']);assert.equal(X.utils.sheet_to_json(book.Sheets['整理修复明细_1'])[0]['操作'],'仅删除整理历史',JSON.stringify(X.utils.sheet_to_json(book.Sheets['整理修复明细_1'])[0]));
  round.updates.set(row,{state:'齐全',color:'green',repair:{...repair,note:'完成',state:'files-verified'},fileCheckedAt:'2026-10-22T10:00:00Z'});
  const next=R.read(R.write(round),{...options,mode:'archive'}),green=next.tasks.find(r=>r.ref===t.ref);assert.equal(C.RepairState.read(green.data).note,'完成');assert.equal(green.data['文件核查时间'],'2026-10-22T10:00:00Z');assert.equal(green.color,'green');
});
test('归档报告独立记录源空目录删除的计划、结果及时间',()=>{
  const s=R.read(fixture(),{...options,mode:'archive'}),t=s.tasks[0];
  s.updates.set(t,{state:'归档待核实',record:{operations:[],cleanup:{kind:'empty-directory',source:t.showPath,target:'',planId:'p',state:'pending',at:'start',finishedAt:'end',error:'响应丢失',manifest:[{path:t.showPath,directory:true}],reason:'全部内容迁移后清理'}}});
  const rows=X.utils.sheet_to_json(X.read(R.write(s)).Sheets['归档明细_1']);assert.equal(rows.length,1);assert.equal(rows[0]['操作'],'删除源空目录');assert.equal(rows[0]['执行结果'],'删除结果待核实');assert.equal(rows[0]['原路径'],t.showPath);assert.equal(rows[0]['请求时间（UTC）'],'start');assert.equal(rows[0]['说明'],'响应丢失');
});

function directoryFixture(change=()=>{}) {
  return fixture((_book,sheet)=>{
    for(const n of [3,4]) {
      sheet['C'+n]={t:'s',v:''};
      const link=sheet['D'+n];
      const u=new URL(link.l?.Target||link.v);
      const show=u.searchParams.get('path').replace(/\/Season 1$/,'');
      u.searchParams.set('path',show);link.v=u.href;link.l={Target:u.href};
      sheet['AA'+n].v=show;sheet['AB'+n].v=show;
    }
    change(sheet);
  });
}
test('归档接受空季号且链接为剧根的目录记录，保留原色及阻挡身份',()=>{
  const s=R.read(directoryFixture(),{...options,mode:'archive'});
  assert.deepEqual(s.errors,[]);assert.equal(s.tasks.length,4);
  assert.deepEqual(s.tasks.filter(t=>t.directoryOnly).map(t=>[t.ref,t.season,t.color]),[['B3',null,'other'],['B4',null,'yellow']]);
  const t=s.tasks[1];s.updates.set(t,{state:'暂缓归档',color:null,note:'季号未确认'});
  const book=X.read(R.write(s),{cellStyles:true});assert.equal(book.Sheets['剧集检查'].B3.s.fgColor.rgb,'BDD7EE');
  assert.ok(X.utils.sheet_to_json(book.Sheets['剧集检查']).some(r=>r['归档结果']==='暂缓归档'));
});
test('目录记录不放宽其他阶段、越界链接或空季号季链接校验',()=>{
  assert.equal(R.read(directoryFixture(),options).errors.length,2);
  for(const mutate of [s=>{s.D3.l.Target=s.D3.l.Target.replace('clouddrive.example','foreign.example');},s=>{const u=new URL(s.D3.l.Target);u.searchParams.set('path',u.searchParams.get('path')+'/Season 1');s.D3.l.Target=u.href;}]) {
    const s=R.read(directoryFixture(mutate),{...options,mode:'archive'});assert.equal(s.errors.length,1);assert.equal(s.errors[0].ref,'B3');
  }
});

test('下载明细区分整季/单集/零散多集，保留站点、做种及每个资源的对应集号',()=>{
  const s=R.read(fixture(),options),r=result();r.records=['season','episode','episodes'].map((scope,i)=>({...r.records[0],key:'r'+i,scope,episodes:i===0?[1,2,3,4]:i===1?[2]:[2,4],finishedAt:'2026-10-21T00:01:00Z',seeds:i===2?null:4}));
  s.updates.set(s.tasks[0],r);const book=X.read(R.write(s),{type:'array'}),detail=X.utils.sheet_to_json(book.Sheets['下载明细_1']);
  assert.deepEqual(detail.map(r=>r['资源类型']),['单季整包','单集','零散多集']);assert.equal(detail[0]['做种数（查询时）'],4);assert.equal(detail[2]['做种数（查询时）'],'未记录');
  assert.equal(detail[0]['站点'],'测试站');assert.equal(detail[1]['对应集号'],'2');assert.match(detail[0]['执行结果'],/不代表下载完成/);
  assert.equal(detail[0]['响应时间（UTC）'],'2026-10-21T00:01:00Z');
});
test('下载明细追加容量、中文标记及选择依据，历史记录未知字段不虚构',()=>{
  const s=R.read(fixture(),options),r=result();r.records.push({...r.records[0],key:'next',sizeBytes:2000,estimatedBytes:1000,chineseMark:'标签：中字',selectionReason:'做种≥5，按每个待补集估算容量升序'});s.updates.set(s.tasks[0],r);
  const detail=X.utils.sheet_to_json(X.read(R.write(s)).Sheets['下载明细_1']);assert.equal(detail[0]['资源容量（字节）'],'未记录');assert.equal(detail[1]['资源容量（字节）'],2000);
  assert.equal(detail[1]['估算每个待补集容量（字节）'],1000);assert.equal(detail[1]['中文标记'],'标签：中字');assert.match(detail[1]['选择依据'],/容量升序/);
});
test('未选定资源的待核实/无需下载行明确说明做种数不适用，不再留空或填0',()=>{
  const s=R.read(fixture(),options);s.updates.set(s.tasks[0],{state:'无需下载',records:[],note:'没有确认缺集'});s.updates.set(s.tasks[1],{state:'待核实',records:[],note:'来源数量未知'});
  const detail=X.utils.sheet_to_json(X.read(R.write(s)).Sheets['下载明细_1']);
  assert.equal(detail.length,2);assert.ok(detail.every(r=>r['做种数（查询时）']==='未选定资源'&&r['做种数说明'].includes('无对应做种数')));
});
test('明细快照跨导入保留，重复导出不叠加；公式形状的资源名作为文字写入',()=>{
  const s=R.read(fixture(),options),r=result();r.records[0].title='=HYPERLINK("https://example.invalid")';s.updates.set(s.tasks[0],r);
  const first=R.write(s),again=R.write(s);assert.deepEqual(X.read(first).SheetNames,X.read(again).SheetNames);
  const next=R.read(first,options);next.updates.set(next.tasks[0],result());const out=R.write(next),book=X.read(out);
  assert.ok(book.Sheets['下载明细_1']);assert.ok(book.Sheets['下载明细_2']);assert.equal(book.Sheets['下载明细_1'].H2.f,undefined);assert.equal(book.Sheets['下载明细_1'].H2.v,r.records[0].title);
  const before=entries(first),after=entries(out);for(const [p,b] of before)if(/tvAudit1\.xml$/.test(p))assert.deepEqual(after.get(p),b);
  assert.equal(R.read(out,options).tasks.length,2);
});
test('删除明细包含失败尝试、待核实请求、文件属性和配套视频，不混写为成功',()=>{
  const s=R.read(fixture(),{...options,mode:'review'}),t=s.tasks[0],f={path:t.path+'/多余.nfo',name:'多余.nfo',size:'1 KB',modified:'2026-10-21 09:00'};
  s.updates.set(t,{state:'待核实',record:{journal:{planId:'cleanup',history:[{path:f.path,before:f,kind:'scrape',owner:'视频.mkv',reason:'专属刮削文件',state:'not-submitted',at:'start',finishedAt:'end',error:'取消'}],records:[],pending:{path:f.path,before:f,reason:'专属刮削文件',at:'retry'}}}});
  const book=X.read(R.write(s)),detail=X.utils.sheet_to_json(book.Sheets['清理明细_1']);assert.equal(detail.length,2);
  assert.equal(detail[0]['执行结果'],'未提交删除');assert.equal(detail[1]['执行结果'],'删除结果待核实');assert.equal(detail[0]['文件大小'],'1 KB');assert.equal(detail[0]['关联保留/删除视频'],'视频.mkv');assert.equal(detail[0]['说明'],'取消');
});
test('归档明细展开视频和刮削文件，冲突季逐项记录跳过及目标路径',()=>{
  const s=R.read(fixture(),{...options,mode:'archive'}),t=s.tasks[0],target=t.showPath.replace('/nas/','/nas.finish/');
  const files=['片.S01E01.mkv','片.S01E01.nfo'].map(name=>({name,path:t.path+'/'+name,size:'1 GB',modified:'云盘修改时间'}));
  s.updates.set(t,{state:'同季冲突，已跳过',record:{operations:[],preserved:files,skipped:[{source:t.path,target:target+'/Season 01',reason:'同季冲突'}]}});
  let detail=X.utils.sheet_to_json(X.read(R.write(s)).Sheets['归档明细_1']);assert.equal(detail.length,2);assert.ok(detail.every(r=>r['操作']==='跳过冲突'&&r['执行结果']==='未移动'));assert.equal(detail[0]['目标路径'],target+'/Season 01/'+files[0].name);
  s.updates.set(t,{state:'已归档',record:{operations:[{source:t.path,target:target+'/Season 1',manifest:files,state:'verified',at:'start',finishedAt:'end',planId:'move'}]}});
  detail=X.utils.sheet_to_json(X.read(R.write(s)).Sheets['归档明细_1']);assert.equal(detail.length,2);assert.ok(detail.every(r=>r['执行结果']==='移动已验证'));assert.equal(detail[1]['原路径'],files[1].path);
});
test('归档读取所有颜色与季记录，成功标紫并追加真实目标超链接，保留源链接与个人备注',()=>{
  const bytes=fixture(),session=R.read(bytes,{...options,mode:'archive'});assert.equal(session.tasks.length,4);assert.equal(session.errors.length,0);
  const row=session.tasks[0],target='/115/Library_incs/nas.finish/电视剧/'+row.category+'/'+row.name;
  const targetUrl=options.origin+'/?page=files&path='+encodeURIComponent(target+'/Season 1');
  session.updates.set(row,{state:'已归档',color:'archived',source:row.showPath,target,targetUrl,at:'2026-10-21',note:'已核实',record:{verified:true,manifest:[{name:'中文.mkv'}]}});
  const out=R.write(session),book=X.read(out,{type:'array',cellStyles:true,cellFormula:true}),sheet=book.Sheets['剧集检查'];
  assert.equal(sheet.B2.s.fgColor.rgb,'E4DFEC');assert.equal(sheet.P2.v,'手工备注 1');assert.equal(sheet.AG3.f,'1+2');assert.equal(sheet.AH2.v,'已归档');
  const before=entries(bytes),after=entries(out);
  for(const [p,b] of before)if(!auditPackagePart(p)&&!/\/xl\/(styles\.xml|worksheets\/sheet1\.xml|worksheets\/_rels\/sheet1\.xml.rels)$/.test(p))assert.deepEqual(after.get(p),b,p);
  const xml=[...after].find(([p])=>p.endsWith('/xl/styles.xml'))[1].toString('utf8');assert.match(xml,/FF7030A0/);
  const rel=[...after].find(([p])=>p.endsWith('/xl/worksheets/_rels/sheet1.xml.rels'))[1].toString('utf8');assert.ok(rel.includes(targetUrl.replaceAll('&','&amp;')));
  const round=R.read(out,{...options,mode:'archive'});assert.equal(round.tasks[0].color,'archived');assert.equal(round.tasks[0].path,row.path);assert.equal(round.tasks[0].data['归档目标路径'],target);
});
test('归档结果不明只增加说明，绿色和原始内容保留；非法目标链接拒绝写入',()=>{
  const s=R.read(fixture(),{...options,mode:'archive'}),row=s.tasks[0];
  s.updates.set(row,{state:'归档待核实',color:null,note:'源仍存在',record:{pending:true}});
  assert.equal(X.read(R.write(s),{type:'array',cellStyles:true}).Sheets['剧集检查'].B2.s.fgColor.rgb,'C6EFCE');
  s.updates.set(row,{state:'已归档',color:'archived',targetUrl:'https://example.com/',record:{}});assert.throws(()=>R.write(s),/归档链接/);
});
function fixture(edit=()=>{}) {
  const rows=[1,2,3,4].map(n=>({category:'日本剧集',showName:'测试剧 '+n+' (2025) [tmdbid='+n+']',season:1,total:4,missing:[2,4],
    path:I.SOURCE+'/日本剧集/测试剧 '+n+' (2025) [tmdbid='+n+']/Season 1',status:'缺集',note:'手工备注 '+n,tmdbId:String(n),reportVersion:'tv-workflow/1'}));
  const book=I.resultWorkbook(rows),sheet=book.Sheets['剧集检查'];
  sheet.B2.s.fill={patternType:'solid',fgColor:{rgb:'C6EFCE'}};
  sheet.B3.s.fill={patternType:'solid',fgColor:{rgb:'BDD7EE'}};
  sheet.B4.s.fill={patternType:'solid',fgColor:{rgb:'FFFF00'}};
  sheet.B5.s.fill={patternType:'solid',fgColor:{rgb:'E4DFEC'}};
  sheet.AG3={t:'n',v:3,f:'1+2'};
  X.utils.book_append_sheet(book,X.utils.aoa_to_sheet([['人工维护'],['原样保留']]),'个人备注');
  edit(book,sheet);return new Uint8Array(X.write(book,{type:'array',bookType:'xlsx',compression:true}));
}
test('自定义云盘端口与中文归档根目录写入有效目标链接，并拒绝旧默认地址',()=>{
  const origin='https://custom.example:9443',source='/云盘/待整理/剧集',destination='/云盘/已整理/剧集';
  const replace=s=>s.replaceAll(options.origin,origin).replaceAll(encodeURIComponent(I.SOURCE),encodeURIComponent(source)).replaceAll(I.SOURCE,source);
  const bytes=fixture((_book,sheet)=>{for(const [key,cell] of Object.entries(sheet))if(!key.startsWith('!')){if(typeof cell.v==='string')cell.v=replace(cell.v);if(cell.l?.Target)cell.l.Target=replace(cell.l.Target);}});
  const s=R.read(bytes,{origin,source,destination,mode:'archive'});assert.equal(s.errors.length,0);assert.equal(s.tasks.length,4);
  const row=s.tasks[0],target=destination+'/'+row.category+'/'+row.name,targetUrl=origin+'/?page=files&path='+encodeURIComponent(target+'/Season 1');
  s.updates.set(row,{state:'已归档',color:'archived',target,targetUrl,record:{verified:true}});
  const out=R.write(s),round=R.read(out,{origin,source,destination,mode:'archive'});assert.equal(round.tasks[0].data['归档目标路径'],target);
  const rel=[...entries(out)].find(([p])=>p.endsWith('/xl/worksheets/_rels/sheet1.xml.rels'))[1].toString('utf8');assert.ok(rel.includes(targetUrl.replaceAll('&','&amp;')));
  for(const bad of [targetUrl.replace(origin,options.origin),origin+'/?page=files&path='+encodeURIComponent('/115/Library_incs/nas.finish/电视剧/日本剧集/测试剧')]){s.updates.get(row).targetUrl=bad;assert.throws(()=>R.write(s),/归档链接/);}
});
function entries(bytes) {
  const zip=X.CFB.read(bytes,{type:'array'});
  return new Map(zip.FullPaths.map((p,i)=>[p,Buffer.from(zip.FileIndex[i].content||[])]));
}
const auditPackagePart=p=>/\/(?:\[Content_Types\]\.xml|xl\/workbook\.xml|xl\/_rels\/workbook\.xml.rels)$/.test(p);
function result(color='yellow') {return {state:'已提交下载，待复核',color,covered:[2],remaining:[4],at:'2026-10-21T00:00:00Z',note:'只覆盖一部分',records:[{key:'test',state:'accepted',id:'abc',title:'测试 S01E02',site:'测试站',episodes:[2],seeds:4,at:'2026-10-21T00:00:00Z'}]};}
test('非绿非归档项进入队列，蓝色和旧黄色保留准确的原始定位',()=>{
  const s=R.read(fixture(),options);assert.equal(s.tasks.length,2);assert.equal(s.skipped,2);assert.equal(s.errors.length,0);
  assert.deepEqual(s.tasks.map(t=>t.ref),['B3','B4']);assert.ok(s.tasks[0].path.endsWith('/Season 1'));
});
test('仅改处理项目录颜色并追加下载列，原备注、公式、链接及其他包内文件不动',()=>{
  const original=fixture(),s=R.read(original,options);s.updates.set(s.tasks[0],result());
  const out=R.write(s),parsed=X.read(out,{type:'array',cellStyles:true,cellFormula:true}).Sheets;
  assert.equal(parsed['剧集检查'].B3.s.fgColor.rgb,'FFFF00');assert.equal(parsed['剧集检查'].B2.s.fgColor.rgb,'C6EFCE');
  assert.equal(parsed['剧集检查'].P3.v,'手工备注 2');assert.equal(parsed['剧集检查'].AG3.f,'1+2');
  assert.equal(parsed['剧集检查'].AH1.v,'下载处理');assert.equal(parsed['剧集检查'].AH3.v,'已提交下载，待复核');
  const before=entries(original),after=entries(out);
  for(const [p,bytes] of before)if(!auditPackagePart(p)&&!/\/xl\/(styles\.xml|worksheets\/sheet1\.xml)$/.test(p))assert.deepEqual(after.get(p),bytes,p);
  const round=R.read(out,options);assert.equal(round.tasks[0].data['下载任务ID'],'abc');assert.equal(round.tasks[0].data['下载未覆盖集号'],'4');
});
test('重复导入不重复加列，红色和不改色可同时写回',()=>{
  const s=R.read(fixture(),options);s.updates.set(s.tasks[0],result('red'));s.updates.set(s.tasks[1],{...result(),color:null,state:'待核实'});
  const first=R.write(s),next=R.read(first,options);next.updates.set(next.tasks[0],{...result(),records:[],state:'无合格资源',color:'red'});
  const out=R.write(next),sheet=X.read(out,{type:'array',cellStyles:true}).Sheets['剧集检查'];
  assert.equal(sheet.B3.s.fgColor.rgb,'FFC7CE');assert.equal(sheet.B4.s.fgColor.rgb,'FFFF00');assert.equal(sheet['!ref'],'A1:AU5');
  assert.equal(R.read(out,options).tasks[0].data['下载记录'],'[]');
});
test('无更新时导出原字节，公式源链接、ID、合并标记和越界路径均拒绝',()=>{
  const bytes=fixture();assert.deepEqual(R.write(R.read(bytes,options)),bytes);
  const variants=[s=>{s.D3.f='HYPERLINK("evil")';},s=>{s.AC3.f='123';},s=>{s['!merges']=[{s:{r:2,c:1},e:{r:2,c:2}}];},s=>{s.D3.l.Target='https://other.invalid/?page=files&path=/x';}];
  for(const edit of variants){const s=R.read(fixture((_b,s)=>edit(s)),options);assert.equal(s.tasks.length,1);assert.equal(s.errors.length,1);}
});
test('标准索引绿色也跳过，不把未知色或条件格式误当白色',()=>{
  const zip=X.CFB.read(fixture(),{type:'array'}),p=zip.FullPaths[0]+'xl/styles.xml';
  const before=new TextDecoder().decode(zip.FileIndex[zip.FullPaths.indexOf(p)].content);
  const after=before.replace(/<fgColor rgb="(?:FF)?BDD7EE"\s*\/>/,'<fgColor indexed="3"/>');assert.notEqual(before,after);
  X.CFB.utils.cfb_add(zip,p,new TextEncoder().encode(after));
  const s=R.read(new Uint8Array(X.CFB.write(zip,{type:'array',fileType:'zip'})),options);
  assert.equal(s.skipped,3);assert.equal(s.tasks.length,1);
  assert.equal(R.colorKind('FFC6EFCE'),'green');assert.equal(R.colorKind('FF00FF00'),'green');assert.equal(R.colorKind('BDD7EE'),'other');
});
test('超长机器记录按单元格限制分片，重读后完整恢复，短记录覆盖后清空尾片',()=>{
  const s=R.read(fixture(),options),r=result();
  r.records=Array.from({length:80},(_,i)=>({...r.records[0],key:'k'+i,title:'中文标题'.repeat(160)}));
  s.updates.set(s.tasks[0],r);const bytes=R.write(s),again=R.read(bytes,options),t=C.taskFromRow(again.tasks[0]);
  assert.equal(t.records.length,80);assert.equal(t.records[79].key,'k79');
  const parsed=X.read(bytes,{type:'array'}).Sheets['剧集检查'];
  for(const [k,v] of Object.entries(parsed))if(!k.startsWith('!')&&typeof v.v==='string')assert.ok(v.v.length<=32767,k);
  again.updates.set(again.tasks[0],result());const last=R.read(R.write(again),options);assert.equal(C.taskFromRow(last.tasks[0]).records.length,1);
});
test('报告写入只接受当前会话任务，不接受外部伪造的工作表路径',()=>{
  const s=R.read(fixture(),options);s.updates.set({...s.tasks[0],sheetPath:'xl/other.xml'},result());assert.throws(()=>R.write(s),/不属于/);
});

test('Season 01 和小写季目录与初检识别规则一致',()=>{
  for(const leaf of ['Season 01','season 1']) {
    const s=R.read(fixture((_b,s)=>{
      const u=new URL(s.D3.v);u.searchParams.set('path',u.searchParams.get('path').replace(/Season 1$/,leaf));s.D3.v=u.href;s.D3.l.Target=u.href;s.AB3.v=s.AB3.v.replace(/Season 1$/,leaf);}),options);
    assert.equal(s.tasks.length,2);assert.equal(s.errors.length,0);
  }
});

test('复核模式只选择黄色项，写绿并更新必要事实列，保留原公式和备注',()=>{
  const bytes=fixture((_b,s)=>{s.E4={t:'n',v:3,f:'1+2'};}),session=R.read(bytes,{...options,mode:'review'});
  assert.deepEqual(session.tasks.map(t=>t.ref),['B4']);
  session.updates.set(session.tasks[0],{state:'齐全，清理已验证',color:'green',total:4,sourceUrl:'https://movie.douban.com/subject/123/',at:'2026-10-21',videoCount:4,current:4,missing:[],
    keep:['保留.mkv'],remove:['多余.mkv'],deleted:['多余.mkv'],preserved:['season.nfo'],record:{source:'douban',verified:true},note:'已核对',
    cells:{'视频文件数':4,'已识别集数':4,'来源登记集数':4,'缺失集号':'无','数据来源':'douban','处理结果':'齐全','核对说明':'不得覆盖人工备注'}});
  const out=R.write(session),sheet=X.read(out,{type:'array',cellStyles:true,cellFormula:true}).Sheets['剧集检查'];
  assert.equal(sheet.B4.s.fgColor.rgb,'C6EFCE');assert.equal(sheet.E4.f,'1+2');assert.equal(sheet.F4.v,4);assert.equal(sheet.F4.t,'n');assert.equal(sheet.H4.v,'无');
  assert.equal(sheet.P4.v,'手工备注 3');assert.equal(sheet.AH1.v,'复核结果');assert.equal(sheet.AI4.v,4);assert.equal(sheet.AI4.t,'n');
  assert.equal(R.read(out,{...options,mode:'review'}).tasks.length,0);
});

test('复核模式无需旧来源集数，长清理记录也按列分片保留',()=>{
  const bytes=fixture((_b,s)=>{s.G4.v='';}),session=R.read(bytes,{...options,mode:'review'});
  assert.equal(session.tasks.length,1);
  const record={keep:Array.from({length:200},(_,i)=>'保留文件'+i+'一'.repeat(200)),remove:[]};
  session.updates.set(session.tasks[0],{state:'待确认清理',color:null,record});
  const reread=R.read(R.write(session),{...options,mode:'review'}),data=reread.tasks[0].data;
  assert.deepEqual(JSON.parse(data['复核清理记录']+data['复核清理记录续1']),record);
});

test('复核替换来源后，显示 URL 与点击目标同步更新，源云盘链接不变',()=>{
  const old='https://www.themoviedb.org/tv/123/season/1',url='https://movie.douban.com/subject/123/';
  const session=R.read(fixture((_b,s)=>{s.M4={t:'s',v:old,l:{Target:old}};}),{...options,mode:'review'});
  session.updates.set(session.tasks[0],{state:'已核对',sourceUrl:url,record:{},cells:{'来源链接':url,'豆瓣链接':url}});
  const out=R.write(session),sheet=X.read(out,{type:'array',cellStyles:true}).Sheets['剧集检查'];
  assert.equal(sheet.M4.v,url);assert.equal(sheet.M4.l.Target,url);assert.equal(sheet.Y4.l.Target,url);assert.equal(sheet.AJ4.l.Target,url);
  const reread=R.read(out,{...options,mode:'review'});assert.equal(reread.tasks[0].path,session.tasks[0].path);
});

test('批量删除成功与待核实文件逐行导出，保留共同批次号',()=>{
 const s=R.read(fixture(),{...options,mode:'review'}),t=s.tasks[0],event=n=>({path:t.path+'/'+n+'.mkv',before:{size:'1 GB',modified:'2026-10-22 10:00'},batchId:'batch-1',planId:'plan-1',kind:'video',reason:'多余版本',at:'start'});
 s.updates.set(t,{state:'部分删除，待核实',record:{journal:{records:[{...event('A'),state:'verified'}],pending:{batchId:'batch-1',files:[event('B'),event('C')],error:'部分文件仍存在'}}}});
 const detail=X.utils.sheet_to_json(X.read(R.write(s)).Sheets['清理明细_1']);assert.equal(detail.length,3);assert.ok(detail.every(r=>r['操作批次']==='batch-1'));assert.equal(detail[0]['执行结果'],'删除已验证');assert.equal(detail[1]['执行结果'],'删除结果待核实');assert.equal(detail[2]['原路径'],t.path+'/C.mkv');
});

test('下载阶段连载蓝色、黄色优先及资源站覆盖/复核依据写入报告，旧备注保留',()=>{
 const s=R.read(fixture(),options),t=s.tasks[0];s.updates.set(t,{state:'暂无可下载缺集',color:'blue',records:[],resourceAudit:{maximum:42,coverage:[1,3,42],at:'2026-10-22',status:'连载中',confirmed:true,basis:'本季已有已播集且有未来集目',sourceUrl:'https://www.themoviedb.org/tv/123/season/1'}});
 const book=X.read(R.write(s),{type:'array',cellStyles:true}),rows=X.utils.sheet_to_json(book.Sheets['剧集检查']);const row=rows.find(r=>r['剧集目录']===t.name);assert.equal(row['资源站最高集号'],42);assert.equal(row['资源站覆盖集号'],'1、3、42');assert.equal(row['播出状态'],'连载中');assert.equal(row['播出状态复核'],'连载中');assert.equal(book.Sheets['剧集检查'][t.ref].s.fgColor.rgb,'BDD7EE');
 s.updates.get(t).color='yellow';assert.equal(X.read(R.write(s),{type:'array',cellStyles:true}).Sheets['剧集检查'][t.ref].s.fgColor.rgb,'FFFF00');
});
