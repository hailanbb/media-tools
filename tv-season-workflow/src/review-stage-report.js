/* 独立的只读复核阶段报表。保留每次尝试的快照，后续删除不会把拟删清单改成已删除。 */
(function createReviewStageReport(X) {
  'use strict';
  function write(stage) {
    if(!stage?.startedAt)throw Error('请先开始复核，再导出复核阶段报告');
    const book=X.utils.book_new(),attempts=stage.attempts||[];
    const text=v=>v==null?'未确认':Array.isArray(v)?v.join('、'):v;
    function sheet(name,headers,rows){
      const s=X.utils.aoa_to_sheet([headers,...rows.map(r=>r.map(text))]);
      s['!cols']=headers.map(h=>({wch:/路径|说明|版本|文件|链接/.test(h)?54:22}));
      s['!autofilter']={ref:X.utils.encode_range({r:0,c:0},{r:rows.length,c:headers.length-1})};
      for(const [ref,c] of Object.entries(s))if(!ref.startsWith('!')){const head=/^[A-Z]+1$/.test(ref);c.s={font:{name:'Microsoft YaHei',sz:11,color:{rgb:head?'FFFFFF':'243746'},bold:head},fill:head?{patternType:'solid',fgColor:{rgb:'24546A'}}:{patternType:'none'},alignment:{vertical:'top',wrapText:true}};}
      X.utils.book_append_sheet(book,s,name);
    }
    sheet('复核概况',['项目','内容'],[['原报告',stage.fileName],['阶段开始时间（UTC）',stage.startedAt],['最近结束时间（UTC）',stage.updatedAt],['阶段状态',stage.status],['待复核黄色项',stage.total],['已尝试不同季数',new Set(attempts.map(a=>a.key)).size],['复核尝试次数',attempts.length],['说明','仅记录点击开始/继续复核后的信息；拟删不代表已删除。实际清理结果请使用“导出复核清理报告”。']]);
    sheet('逐项复核',['序号','剧集目录','季数','季目录路径','开始时间（UTC）','结束时间（UTC）','结束环节','复核结果','豆瓣登记集数','豆瓣链接','视频文件数','已识别集数','缺失集号','拟保留版本','拟删文件数','说明'],attempts.map((a,i)=>{const r=a.result||{};return [i+1,a.name,a.season,a.path,a.startedAt,a.endedAt,a.phase,r.state,r.total??a.source?.total,r.sourceUrl||a.source?.url,r.videoCount,r.current,r.missing,r.version,(r.remove||r.record?.remove||[]).length,r.note];}));
    const groups=[],files=[];
    attempts.forEach((a,i)=>{const r=a.result||{},rec=r.record||{};
      for(const g of rec.groups||[])groups.push([i+1,a.name,a.season,g.label,g.complete?'齐全':'缺集',g.episodes,g.missing,g.latestModified]);
      for(const f of rec.keep||[])files.push([i+1,a.name,a.season,'拟保留视频',f.path,f.size,f.modified,'保留版本']);
      for(const f of rec.remove||[])files.push([i+1,a.name,a.season,'拟删除（尚未执行）',f.path,f.size,f.modified,f.reason]);
      for(const f of rec.preserved||[])files.push([i+1,a.name,a.season,'保留配套文件',f.path,f.size,f.modified,f.reason]);
    });
    sheet('版本分析',['复核序号','剧集目录','季数','版本','完整性','已有集号','缺失集号','最新云盘修改时间'],groups);
    sheet('文件计划',['复核序号','剧集目录','季数','计划类型','文件路径','文件大小','云盘修改时间','说明'],files);
    return X.write(book,{bookType:'xlsx',type:'array',compression:true});
  }
  return {write};
})
