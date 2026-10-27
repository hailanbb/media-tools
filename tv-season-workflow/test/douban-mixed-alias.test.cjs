// 两个用户实测样本：混排片名、搜索别名及地区后缀。只使用公开页面最小文本。
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const C=require('../season-inspect.user.js');
const superTV={title:'睁眼一看是 SUPER TV 눈 떠보니 슈퍼TV\u200e (2025)',url:'https://movie.douban.com/subject/37279734/',tv:true};
const marry={title:'宇宙嫁给我 우주메리미\u200e (2025)',url:'https://movie.douban.com/subject/37172418/',tv:true};
async function worker({candidate,show,summary='',info,season=1,paced=false}) {
  const source=fs.readFileSync(path.join(__dirname,'../season-inspect.user.js'),'utf8'),code=source.slice(source.indexOf('  function blockedDouban()'),source.indexOf("  if (location.hostname !=="));
  const id='12345678-1234-1234-1234-123456789abc',values=new Map([['request:'+id,{show,season,manual:false,paced,created:Date.now()}]]);
  const location={hash:'#tv_audit='+id,hostname:'search.douban.com',href:'https://search.douban.com/movie/subject_search?search_text=x'};let detail=false;
  const card={textContent:candidate.title+'[剧集]'+summary,querySelector:s=>s==='.meta.abstract'?{textContent:summary}:null};
  const anchors=[{textContent:candidate.title,href:candidate.url,closest:()=>card}];
  const document={body:{innerText:'剧集搜索和详情',append(){}},createElement:()=>({style:{}}),
    querySelector:s=>s==='#info, .item-root .title a.title-text'?{}:detail&&s==='#info'?{innerText:info}:detail&&s==='h1'?{innerText:candidate.title}:null,
    querySelectorAll:s=>s==='.item-root .title a.title-text'?anchors:s==='#info [property="v:initialReleaseDate"]'?[{textContent:'2025-10-10(韩国)'}]:[]};
  const context={...C,URLSearchParams,location,document,visible:()=>true,get:(k,d)=>values.get(k)||d,put:(k,v)=>values.set(k,v),sleep:async()=>{},now:()=>new Date().toISOString()};
  vm.runInNewContext(code+'\nthis.run=doubanWorker;',context);await context.run();
  const entered=location.href===candidate.url+'#tv_audit='+id;
  if(entered){detail=true;location.hostname='movie.douban.com';await context.run();}
  return {reply:values.get('response:'+id),entered};
}
test('中英片名与韩英原名可整体匹配，兼容 NBSP、全角问号及简繁，不能漏掉英文尾词',()=>{
  for(const name of ['睁眼一看是 SUPER TV','睁眼一看是 SUPER TV\u00a0','睜眼一看是 SUPER TV','눈 떠보니 슈퍼TV'])assert.equal(C.candidateMatches(superTV,{title:name,year:'2025'},1),true,name);
  assert.equal(C.candidateMatches({...superTV,title:'눈 떠보니 슈퍼TV 睁眼一看是 SUPER TV (2025)'},{title:'睁眼一看是 SUPER TV',year:'2025'},1),true);
  for(const name of ['睁眼一看是','睁眼一看是 SUPER','SUPER TV','TV'])assert.equal(C.candidateMatches(superTV,{title:name,year:'2025'},1),false,name);
  assert.equal(C.candidateMatches(superTV,{title:'睁眼一看是 SUPER TV',year:'2024'},1),false);
  assert.equal(C.candidateMatches(superTV,{title:'睁眼一看是 SUPER TV',year:'2025'},2),false);
});
test('初检 paced worker 搜索只回传详情候选，不自行导航或读取第二页',async()=>{
  const {entered,reply}=await worker({candidate:marry,show:{title:'宇宙Marry Me？',year:'2025'},summary:'韩国 / 宇宙Marry Me?(台)',info:'集数: 12',paced:true});
  assert.equal(entered,false);assert.equal(reply.nextUrl,marry.url);assert.equal(reply.total,undefined);
});
test('初检子页发现验证码先写持久风险信号，再报告给云盘主队列',async()=>{
  const source=fs.readFileSync(path.join(__dirname,'../season-inspect.user.js'),'utf8'),code=source.slice(source.indexOf('  function blockedDouban()'),source.indexOf("  if (location.hostname !=="));
  const id='12345678-1234-1234-1234-123456789abc',values=new Map([['request:'+id,{show:{title:'测试剧',year:'2025'},season:1,paced:true,created:Date.now()}]]);
  const context={...C,URLSearchParams,location:{hash:'#tv_audit='+id,hostname:'search.douban.com'},document:{body:{innerText:'访问过于频繁，请输入验证码',append(){}},createElement:()=>({style:{}}),querySelector:()=>null,querySelectorAll:()=>[]},visible:()=>false,
    get:(k,d)=>values.get(k)||d,put:(k,v)=>values.set(k,v),sleep:async()=>{},now:()=>new Date().toISOString()};
  vm.runInNewContext(code+'\nthis.run=doubanWorker;',context);await context.run();
  assert.equal(values.get('response:'+id).blocked,true);assert.ok(values.get('douban-risk-signal-v1').until-Date.now()>1790000);
});
test('地区标注只剥离白名单末尾，不能抹去不同片名和特别篇',()=>{
  for(const suffix of ['(台)','（臺灣）','(港)','（中国大陆）'])assert.equal(C.sameTitle('宇宙Marry Me?'+suffix,'宇宙Marry Me？'),true,suffix);
  for(const suffix of ['(特别篇)','(导演版)','(新篇)'])assert.equal(C.sameTitle('宇宙Marry Me?'+suffix,'宇宙Marry Me？'),false,suffix);
  assert.equal(C.sameTitle('宇宙Marry Me Again?(台)','宇宙Marry Me？'),false);
  assert.equal(C.sameTitle('Family Guy 家庭伙伴','Family'),false);
  assert.equal(C.sameTitle('家庭伙伴 Family Guy','Guy'),false);
});
test('SUPER TV 真实页面返回 10 集，空季仍为 0/0，缺集按来源计算',async()=>{
  const {entered,reply}=await worker({candidate:superTV,show:{title:'睁眼一看是 SUPER TV\u00a0',year:'2025'},info:'制片国家/地区: 韩国\n首播: 2025-03-21(韩国)\n集数: 10\n又名: Woke Up to Super TV（台） / 一睁眼发现是Super TV\n'});
  assert.equal(entered,true);assert.equal(reply.error,undefined);assert.equal(reply.total,10);
  const result=await C.reviewIncomplete([],1,{source:'tmdb',total:9,url:'https://www.themoviedb.org/tv/123/season/1',broadcast:'未确认'},async()=>reply);
  assert.equal(result.audit.videoCount,0);assert.equal(result.audit.current,0);assert.equal(result.db.total,10);assert.equal(result.audit.missing.length,10);
});
test('宇宙 Marry Me 通过搜索摘要中的台湾译名进入详情，再按又名和年份确认 12 集',async()=>{
  const {entered,reply}=await worker({candidate:marry,show:{title:'宇宙Marry Me？',year:'2025'},summary:'韩国 / 喜剧 / 爱情 / 愿意和我结婚吗 / 宇宙Marry Me?(台) / 60分钟',info:'制片国家/地区: 韩国\n首播: 2025-10-10(韩国)\n集数: 12\n又名: 愿意和我结婚吗 / 宇宙Marry Me?(台) / Would You Marry Me?\n'});
  assert.equal(entered,true);assert.equal(reply.error,undefined);assert.equal(reply.total,12);assert.equal(reply.url,marry.url);
});
test('搜索摘要只允许进入详情，详情缺少该别名或年份不符时不得确认集数',async()=>{
  for(const variant of ['alias','year','country']) {
    const show={title:variant==='country'?'韩国':'宇宙Marry Me？',year:variant==='year'?'2024':'2025'};
    const {entered,reply}=await worker({candidate:marry,show,summary:'韩国 / 宇宙Marry Me?(台)',info:'制片国家/地区: 韩国\n首播: 2025-10-10(韩国)\n集数: 12\n又名: '+(variant==='alias'?'其他名字':'宇宙Marry Me?(台)')+'\n'});
    assert.equal(entered,true);assert.ok(reply.error,variant);assert.equal(reply.total,undefined,variant);
  }
});
test('第三脚本实际详情 worker 保留别名，清理前身份复验仍能确认这两部剧',async()=>{
  const R=require('../season-review.user.js'),id='12345678-1234-1234-1234-123456789abc';
  for(const sample of [{candidate:marry,name:'宇宙Marry Me？',total:12,aliases:'宇宙Marry Me?(台) / Would You Marry Me?'},
    {candidate:superTV,name:'睁眼一看是 SUPER TV',total:10,aliases:'Woke Up to Super TV（台）'}]) {
    const values=new Map([['request:'+id,{show:{title:sample.name,year:'2025'},season:1,manual:false,created:Date.now()}]]);
    const previous={document:global.document,location:global.location};
    const info=`制片国家/地区: 韩国\n首播: 2025-10-10\n集数: ${sample.total}\n又名: ${sample.aliases}\n`;
    global.location={hostname:'movie.douban.com',hash:'#tv_review='+id,href:sample.candidate.url+'#tv_review='+id};
    global.document={body:{innerText:info,append(){}},createElement:()=>({style:{}}),
      querySelector:s=>s==='#info, .item-root .title a.title-text'?{}:s==='#info'?{innerText:info}:s==='h1'?{innerText:sample.candidate.title}:null,
      querySelectorAll:s=>s==='#info [property="v:initialReleaseDate"]'?[{textContent:'2025-10-10'}]:[]};
    try {
      const client=R.createClient(R,{get:(k,d)=>values.get(k)||d,put:(k,v)=>values.set(k,v),del(){},checkpoint(){},ready:()=>true,message(){}});
      await client.worker();const reply=values.get('response:'+id);
      assert.equal(reply.error,undefined);assert.equal(reply.total,sample.total);assert.ok(reply.aliases.length);
      assert.equal(R.verifySource({name:sample.name+' (2025)',season:1},reply).total,sample.total);
    }finally{Object.assign(global,previous);}
  }
});
