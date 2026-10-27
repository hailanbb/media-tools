// 真实页面最小样本：只保留公开标题、首播、别名和集数，不保存账号或评论。
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const C=require('../season-inspect.user.js');
const first={title:'主妇侦探社 살롱 드 홈즈\u200e (2025)',url:'https://movie.douban.com/subject/36218305/',tv:true};
const second={title:'主妇侦探社 第二季 살롱드홈즈2\u200e (2027)',url:'https://movie.douban.com/subject/37447985/',tv:true};
const show={title:'主妇侦探社',year:'2025'};
test('主妇侦探社真实中韩双标题，首季与第二季各自唯一匹配',()=>{
  assert.equal(C.chooseCandidate([first,second],show,1,{inspectDetails:true}),first);
  assert.equal(C.chooseCandidate([first,second],show,2,{inspectDetails:true}),second);
  assert.equal(C.candidateMatches({...first,releaseYears:['2025']},show,1),true);
  assert.equal(C.candidateMatches(second,show,1),false);
});
test('简繁中文与英文/韩文双标题支持正反顺序和英文目录，不能按英文前缀误配',()=>{
  for(const [title,expected] of [
    ['主婦偵探社 살롱 드 홈즈 (2025)','主妇侦探社'],
    ['主婦偵探社 Salon de Holmes (2025)','主妇侦探社'],
    ['Salon de Holmes 主婦偵探社 (2025)','主妇侦探社'],
    ['主婦偵探社 Salon de Holmes (2025)','Salon de Holmes'],
    ['主婦偵探社 主妇侦探社 (2025)','主妇侦探社'],
    ['歡迎光臨 二代咖啡 歡迎光臨 二代咖啡 (2022)','欢迎光临 二代咖啡'],
    ['X医生 Doctor X (2025)','X医生'],
    ['想和喜欢的男人分手 好きなオトコと別れたい (2025)','想和喜欢的男人分手'],
    ['想和喜歡的男人分手 好きなオトコと別れたい (2025)','想和喜欢的男人分手'],
    ['想和喜歡的男人分手 好きなオトコと別れたい (2025)','好きなオトコと別れたい'],
    ['主婦偵探社 살롱 드 홈즈 (2025)','살롱 드 홈즈'],
    ['Salon de Holmes 살롱 드 홈즈 (2025)','Salon de Holmes'],
    ['살롱 드 홈즈 Salon de Holmes (2025)','살롱 드 홈즈'],
    ['ONE PIECE ワンピース (2025)','ONE PIECE'],
    ['ONE PIECE ﾜﾝﾋﾟｰｽ (2025)','ONE PIECE'],
  ])assert.equal(C.candidateMatches({title,tv:true},{title:expected,year:/2022/.test(title)?'2022':'2025'},1),true,title);
  for(const [title,expected] of [['Family Guy (2025)','Family'],['Family Guy 家庭伙伴 (2025)','Family'],['主婦偵探社外傳 Salon de Holmes (2025)','主妇侦探社'],['主婦偵探社 特别篇 Salon de Holmes (2025)','主妇侦探社'],['主婦偵探社 主婦奇案 (2025)','主妇侦探社']])assert.equal(C.candidateMatches({title,tv:true},{title:expected,year:'2025'},1),false,title);
});
test('扩展语言仍拒绝错误年份、错误季、非剧集和同名多条目',()=>{
  assert.equal(C.candidateMatches({...first,title:first.title.replace('2025','2024')},show,1),false);
  assert.equal(C.candidateMatches(first,show,2),false);
  assert.equal(C.candidateMatches({...first,tv:false},show,1),false);
  assert.equal(C.chooseCandidate([first,{...first,url:'https://movie.douban.com/subject/99/'}],show,1,{inspectDetails:true}),null);
  assert.equal(C.candidateMatches({title:'另一译名 (2025)',aliases:['主婦偵探社'],tv:true},show,1),true);
  const han={title:'三人夫妻 三人夫婦 (2025)',tv:true,url:first.url},hs={title:'三人夫妻',year:'2025'};
  assert.equal(C.chooseCandidate([han],hs,1,{inspectDetails:true}),han);
  assert.equal(C.candidateMatches(han,hs,1),false);
  assert.equal(C.candidateMatches({...han,japanese:true},hs,1),true);
});
test('真实 worker 搜索到正确第一季，再解析豆瓣详情的 10 集；第三脚本共享同一规则',async()=>{
  const source=fs.readFileSync(path.join(__dirname,'../season-inspect.user.js'),'utf8');
  const code=source.slice(source.indexOf('  function blockedDouban()'),source.indexOf("  if (location.hostname !=="));
  const id='12345678-1234-1234-1234-123456789abc',values=new Map([['request:'+id,{show,season:1,manual:false,created:Date.now()}]]);
  const location={hash:'#tv_audit='+id,hostname:'search.douban.com',href:'https://search.douban.com/movie/subject_search?search_text=x'};
  const info='制片国家/地区: 韩国\n首播: 2025-06-16(韩国)\n集数: 10\n又名: Salon de Holmes / 沙龙福尔摩斯\n';
  let detail=false;
  const anchors=[first,second].map(c=>({textContent:c.title,href:c.url,closest:()=>({textContent:c.title+'[剧集]',querySelector:()=>null})}));
  const document={body:{innerText:'主妇侦探社',append(){}},createElement:()=>({style:{}}),
    querySelector:s=>s==='#info, .item-root .title a.title-text'?{}:detail&&s==='#info'?{innerText:info}:detail&&s==='h1'?{innerText:first.title}:null,
    querySelectorAll:s=>s==='.item-root .title a.title-text'?anchors:s==='#info [property="v:initialReleaseDate"]'?[{textContent:'2025-06-16(韩国)'}]:[]};
  const context={...C,URLSearchParams,location,document,visible:()=>true,get:(k,d)=>values.get(k)||d,put:(k,v)=>values.set(k,v),sleep:async()=>{},now:()=>new Date().toISOString()};
  vm.runInNewContext(code+'\nthis.run=doubanWorker;',context);
  await context.run();assert.equal(location.href,first.url+'#tv_audit='+id);assert.equal(values.has('response:'+id),false);
  detail=true;location.hostname='movie.douban.com';await context.run();const reply=values.get('response:'+id);
  assert.equal(reply.error,undefined);assert.equal(reply.total,10);assert.equal(reply.url,first.url);assert.equal(reply.broadcast,'未确认');
  const R=require('../season-review.user.js');assert.equal(R.candidateMatches({...reply,tv:true},show,1),true);
});
