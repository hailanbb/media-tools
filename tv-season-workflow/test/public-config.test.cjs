// 公开安装包支持个人目录配置；用内存列表和阻断页面替身验证，不接触真实云盘。
const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const SOURCE='/自定义云盘/待整理/剧集',DEST='/自定义云盘/已整理/剧集';
function configured(name,source=SOURCE,destination=DEST){
  const text=fs.readFileSync(path.join(__dirname,'../'+name+'.user.js'),'utf8')
    .replace("const ORIGIN='https://clouddrive.example';","const ORIGIN='https://custom.example:9443';")
    .replace("const SOURCE='/115/Library_incs/nas/电视剧';",'const SOURCE='+JSON.stringify(source)+';')
    .replace("const DEST='/115/Library_incs/nas.finish/电视剧';",'const DEST='+JSON.stringify(destination)+';');
  const module={exports:{}};new Function('module',text)(module);return module.exports;
}
test('归档拒绝相同、嵌套、根目录及非绝对配置',()=>{
  for(const [s,d] of [[SOURCE,SOURCE],[SOURCE,SOURCE+'/子目录'],[DEST+'/子目录',DEST],['/',DEST],['相对路径',DEST],[SOURCE,DEST+'/'],[SOURCE,'/归档/../外部']])assert.throws(()=>configured('season-archive',s,d),/独立的绝对目录/);
});
test('自定义中文源和目标用于归档计划及成功验证，不回退默认目录',async()=>{
  const A=configured('season-archive'),category='韩国剧集',name='配置测试剧 (2025)',source=SOURCE+'/'+category+'/'+name,target=DEST+'/'+category+'/'+name;
  assert.equal(A.SOURCE,SOURCE);assert.equal(A.DEST,DEST);
  const f=(p,n,d=false)=>({path:p+'/'+n,name:n,directory:d,size:d?'':'1 GB',modified:'2026-10-21 12:00'});
  const row={category,name,showPath:source,path:source+'/Season 1',season:1,color:'green',data:{'来源登记集数':1}};
  const tree=new Map([[DEST,[f(DEST,category,true)]],[DEST+'/'+category,[]],[SOURCE+'/'+category,[f(SOURCE+'/'+category,name,true)]],[source,[f(source,'Season 1',true)]],[row.path,[f(row.path,'测试.S01E01.mkv')]]]);
  const list=async p=>{assert.ok(tree.has(p),p);return structuredClone(tree.get(p));};
  const p=await A.plan([row],list,'custom');assert.equal(p.target,target);assert.equal(p.source,source);
  let journal=null,moves=0;
  const result=await A.execute(p,{list,checkpoint(){},load:async()=>journal,save:async(_p,v)=>{journal=structuredClone(v);},move:async()=>{
    moves++;tree.set(SOURCE+'/'+category,[]);tree.set(DEST+'/'+category,[f(DEST+'/'+category,name,true)]);
    for(const [p,files] of [...tree])if(p===source||p.startsWith(source+'/')){tree.set(target+p.slice(source.length),files.map(r=>({...r,path:target+r.path.slice(source.length)})));tree.delete(p);}
  }});
  assert.equal(moves,1);assert.equal(result.verified,true);assert.equal(journal.state,'已归档');
});
test('清理与归档页面读取仅接受配置的目录树，旧默认和相邻前缀不能进入页面',async()=>{
  const previous=global.document;let touches=0;
  global.document={querySelector(){touches++;throw Error('已进入模拟页面');}};
  try{
    for(const name of ['season-review','season-archive']){
      const C=configured(name),client=C.createClient(C,{source:SOURCE,destination:DEST,checkpoint(){},get(){},put(){},del(){},message(){},ready:()=>true});
      for(const p of ['/115/Library_incs/nas/电视剧/分类',SOURCE+'相邻/分类',SOURCE+'/../外部']){
        const before=touches;await assert.rejects(client.listing(p),/允许的电视剧源目录/);assert.equal(touches,before);
      }
      await assert.rejects(client.listing(SOURCE+'/分类/剧集/Season 1'),/已进入模拟页面/);
      if(name==='season-archive')await assert.rejects(client.listing(DEST+'/分类'),/已进入模拟页面/);
      else await assert.rejects(client.listing(DEST+'/分类'),/允许的电视剧源目录/);
    }
  }finally{global.document=previous;}
});
