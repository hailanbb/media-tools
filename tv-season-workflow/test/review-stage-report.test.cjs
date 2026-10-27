const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const C=require('../season-review.user.js'),R=C.stageReportEngine();
const bundle=fs.readFileSync(path.join(__dirname,'../vendor/xlsx-js-style/package/dist/xlsx.bundle.js'),'utf8');
const X=new Function('const exports={};const module=undefined,require=undefined,window=undefined,define=undefined;'+bundle+';return exports;')();
test('复核阶段报表含概况、每次尝试、版本和拟删文件；未知数量不伪造0',()=>{
 const stage={fileName:'检查.xlsx',total:2,startedAt:'2026-10-22T00:00:00Z',updatedAt:'2026-10-22T00:03:00Z',status:'已完成',attempts:[
 {key:'a',name:'剧甲',season:1,path:'/甲/Season 1',phase:'读取文件列表',result:{state:'目录不存在，已跳过',note:'路径未找到'}},
 {key:'b',name:'剧乙',season:1,path:'/乙/Season 1',phase:'分析版本与清理计划',result:{state:'待确认清理',total:2,videoCount:3,current:2,missing:[],version:'1080p-A',record:{groups:[{label:'1080p-A',complete:true,episodes:[1,2],missing:[],latestModified:'2026-10-20 10:00'}],keep:[{path:'/乙/1.mkv',size:'1 GB',modified:'2026-10-20 10:00'}],remove:[{path:'/乙/混版.mkv',size:'1 GB',modified:'2026-10-19 10:00',reason:'其他不完整版本'}]}}}
 ]};
 const before=JSON.stringify(stage),b=X.read(R.write(stage),{type:'array',cellStyles:true});assert.deepEqual(b.SheetNames,['复核概况','逐项复核','版本分析','文件计划']);
 const rows=X.utils.sheet_to_json(b.Sheets['逐项复核']);assert.equal(rows[0]['视频文件数'],'未确认');assert.equal(rows[1]['拟删文件数'],1);
 const files=X.utils.sheet_to_json(b.Sheets['文件计划']);assert.equal(files[1]['计划类型'],'拟删除（尚未执行）');assert.equal(JSON.stringify(stage),before);assert.ok(b.Sheets['逐项复核']['!autofilter']);
});
test('未开始不能导出；暂停阶段和同一季的多次尝试完整保留',()=>{
 assert.throws(()=>R.write(null),/先开始复核/);
 const b=X.read(R.write({startedAt:'now',status:'已暂停，可继续',total:2,attempts:[{key:'a',name:'=HYPERLINK("x")',result:{state:'已暂停'}},{key:'a',name:'=HYPERLINK("x")',result:{state:'缺集'}}]}));
 const rows=X.utils.sheet_to_json(b.Sheets['逐项复核']);assert.equal(rows.length,2);assert.equal(b.Sheets['逐项复核'].B2.f,undefined);
 const summary=X.utils.sheet_to_json(b.Sheets['复核概况']);assert.equal(summary.find(r=>r['项目']==='已尝试不同季数')['内容'],1);
});
