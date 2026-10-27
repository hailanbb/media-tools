const assert = require('node:assert/strict');
const c = require('../maintenance-core.js');

const chooseSizes=items=>c.chooseResource(items.map(x=>({title:'剧',season:1,site:'天空',...x})),{name:'剧',season:1});
assert.equal(chooseSizes([{seeders:39,size:'16.91 GB'},{seeders:12,size:'13.69 GB'},{seeders:4,size:'1 GB'},{seeders:5,size:'12 GB'}]).seeders,5);
assert.equal(chooseSizes([{seeders:4,size:'20 GB'},{seeders:3,size:'1 GB'}]).seeders,4);
assert.equal(chooseSizes([{seeders:5,size:'1 GB'},{seeders:8,size:'1000 MB'}]).seeders,8);
assert.equal(chooseSizes([{seeders:50,size:'未知'},{seeders:5,size:'2 GB'}]).seeders,5);
assert.equal(chooseSizes([{seeders:5,size:'未知'},{seeders:8}]).seeders,8);

assert.equal(c.chooseResource([
  {title:'零日攻击',season:1,site:'彩虹岛',seeders:50,size:'12 GB'},
  {title:'零日攻击',season:1,site:'馒头',seeders:39,size:'16.91 GB'},
  {title:'零日攻击',season:1,site:'未勾选站点',seeders:999,size:'1 GB'}
],{name:'零日攻击',season:1,sites:['天空','观众','馒头','彩虹岛']}).site,'彩虹岛');

const row = (index,name,year,progress) => c.parseSubscription({index,name,year,progress});
assert.deepEqual(c.SEARCH_SITES,['天空','观众','馒头']);
assert.equal(c.normalizeTitle('  A\u00a0B  '), 'A B');
assert.deepEqual(row(100,'银河 S02','2025','0 / 12'), {index:100,displayName:'银河 S02',name:'银河',season:2,year:2025,downloaded:0,total:12,key:'银河|S02|2025'});
assert.equal(c.classify(row(99,'剧 S01','2020','1/2')),'protected');
assert.equal(c.classify(row(100,'剧 S01','2026','1/2')),'skip-year');
assert.equal(c.classify(row(100,'剧 S01','2025','2/2')),'complete');
assert.equal(c.classify(row(100,'剧 S01','2025','0/2')),'cancel-zero');
assert.equal(c.classify(row(100,'剧 S01','2025','1/2')),'download-then-cancel');
for (const bad of [{index:100,name:'剧 S01',year:2025,progress:'0/0'},{index:100,name:'剧 S01',year:2025,progress:'x/2'},{index:100,name:'剧',year:2025,progress:'1/2'}]) assert.throws(()=>c.parseSubscription(bad));
assert.equal(c.classify(row(100,'剧 S01','2025','3/2')),'download-then-cancel');
assert.equal(c.chooseResource([{title:'剧',season:1,site:'其他',seeders:99},{title:'剧',season:2,site:'天空',seeders:100},{title:'剧',season:1,site:'天空',seeders:3},{title:'剧',season:1,site:'观众',seeders:9},{title:'剧',season:1,site:'观众',seeders:9}],{name:'剧',season:1}).seeders,9);
assert.equal(c.chooseResource([{title:'Ａ剧',season:1,site:'天空',seeders:4}],{name:'A剧',season:1}).title,'Ａ剧');
assert.throws(()=>c.chooseResource([{title:'剧',season:2,site:'天空',seeders:100}],{name:'剧',season:1}));
assert.throws(()=>c.chooseResource([{title:'剧',season:1,site:'天空',seeders:null},{title:'剧',season:1,site:'观众',seeders:''}],{name:'剧',season:1}));
assert.equal(c.chooseResource([{title:'零日攻击',season:1,site:'天空',seeders:20,total:6},{title:'零日攻击',season:1,site:'观众',seeders:7,total:10},{title:'零日攻击',season:1,site:'天空',seeders:30,total:null}],{name:'零日攻击',season:1,total:10}).seeders,7);
assert.equal(c.chooseResource([{title:'零日攻击',season:1,site:'天空',seeders:39,size:'16.91 GB',total:10},{title:'零日攻击',season:1,site:'观众',seeders:29,size:'60.96 GB',total:10},{title:'零日攻击',season:1,site:'馒头',seeders:39,size:'16.91 GB',total:10},{title:'零日攻击',season:1,site:'馒头',seeders:39,size:'13.69 GB',total:10}],{name:'零日攻击',season:1,total:10}).site,'馒头');
assert.equal(c.chooseResource([{title:'剧',season:1,site:'天空',seeders:10,size:'1 GiB'},{title:'剧',season:1,site:'观众',seeders:10,size:'1024 MiB'},{title:'剧',season:1,site:'馒头',seeders:10,size:'1025 MiB'}],{name:'剧',season:1}).site,'天空');
assert.equal(c.chooseResource([{title:'剧',season:1,site:'天空',seeders:'9'},{title:'剧',season:1,site:'观众',seeders:'100'}],{name:'剧',season:1}).seeders,'100');
const report={id:'=ID',startedAt:'2026-01-01',endedAt:'2026-01-02',status:'完成',records:[{index:100,name:'剧',season:1,year:2025,downloaded:1,total:2,action:'下载后取消',status:'完成',detail:'ok',resourceTitle:'+资源',site:'天空',seeders:3,time:'t'}]};
assert.match(c.reportMarkdown(report),/前 100 项/); const csv=c.reportCSV(report); assert.equal(csv.charCodeAt(0),0xFEFF); assert.match(csv,/'=ID/); assert.match(csv,/'\+资源/);
console.log('PASS maintenance-core parse/classify/resource/report boundaries');
