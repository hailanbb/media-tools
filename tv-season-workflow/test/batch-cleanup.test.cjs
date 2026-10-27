const {test}=require('node:test'),assert=require('node:assert/strict'),C=require('../season-review.user.js');
const dir='/115/Library_incs/nas/电视剧/日本剧集/测试剧 (2025)/Season 1';
const file=(name)=>({path:dir+'/'+name,name,directory:false,size:'1 GB',modified:'2026-10-22 10:00'});
function setup(count=3){const keep=[file('Keep.S01E01.mkv')],remove=Array.from({length:count},(_,i)=>({...file('Old'+i+'.mkv'),kind:'video',reason:'其他不完整版本'})),p={id:'p',taskKey:'t',path:dir,complete:true,eligible:true,keep,remove,snapshot:[...keep,...remove]};let rows=structuredClone(p.snapshot),journal=null,calls=0;return {p,get journal(){return journal;},get calls(){return calls;},get rows(){return rows;},set rows(v){rows=v;},api:{list:async()=>structuredClone(rows),removeBatch:async files=>{calls++;rows=rows.filter(r=>!files.some(f=>f.path===r.path));},load:()=>journal,save:(_k,v)=>{journal=structuredClone(v);},checkpoint(){}}};}
test('同季一次批量提交，逐文件同批次记录，超50自动拆批',async()=>{
 for(const count of [3,51]){const h=setup(count),r=await C.executeBatch(h.p,h.api);assert.equal(h.calls,Math.ceil(count/50));assert.equal(r.deleted.length,count);assert.equal(h.rows.length,1);assert.equal(h.journal.pending,null);assert.ok(h.journal.records.every(r=>r.state==='verified'&&r.batchId&&r.before.modified));}
});
test('部分成功记录已删项，剩余项持久待核实，不重复提交',async()=>{
 const h=setup();h.api.removeBatch=async files=>{h.rows=h.rows.filter(r=>r.path!==files[0].path);};await assert.rejects(C.executeBatch(h.p,h.api),/部分删除/);assert.equal(h.journal.records.length,1);assert.equal(h.journal.pending.files.length,2);await assert.rejects(C.executeBatch(h.p,h.api),/上次删除结果未确认/);
});
test('响应不明、保留文件变动均保留整批待核实；提交前取消记录为未提交',async()=>{
 for(const mode of ['throw','lost','cancel']){const h=setup();h.api.removeBatch=async()=>{if(mode==='lost'){h.rows=[];return;}throw Object.assign(Error('failure'),mode==='cancel'?{notSubmitted:true}:{});};await assert.rejects(C.executeBatch(h.p,h.api));if(mode==='cancel'){assert.equal(h.journal.pending,null);assert.ok(h.journal.records.every(r=>r.state==='not-submitted'));}else assert.equal(h.journal.pending.files.length,3);}
});
test('保存失败或清单变化不会提交；暂停后继续不重复已完成批次',async()=>{
 const bad=setup();bad.api.save=()=>{throw Error('storage');};await assert.rejects(C.executeBatch(bad.p,bad.api));assert.equal(bad.calls,0);
 const h=setup(51);h.api.checkpoint=()=>{if(h.calls)throw Object.assign(Error('暂停'),{name:'Pause'});};await assert.rejects(C.executeBatch(h.p,h.api),/暂停/);assert.equal(h.journal.records.length,50);assert.equal(h.journal.pending,null);h.api.checkpoint=()=>{};await C.executeBatch(h.p,h.api);assert.equal(h.calls,2);assert.equal(h.journal.records.length,51);
});
async function simulate(options={}){
 const files=[file('A.mkv'),file('A.nfo')],picked=new Set();let open=false,confirmations=0,cancelled=0;
 const text=textContent=>({textContent}),confirm={textContent:'删除',click(){confirmations++;if(options.after)throw Error('response lost');open=false;}},cancel={textContent:'取消',click(){cancelled++;open=false;}};
 const rows=files.map(f=>({getAttribute:()=>f.path,classList:{contains:()=>false},querySelector:s=>({'.file-name-text':text(f.name),'.file-size':text(f.size),'.file-modified':text(f.modified),'.file-icon':{dispatchEvent(){picked.has(f.path)?picked.delete(f.path):picked.add(f.path);}}}[s])}));
 const dialog={getClientRects:()=>open?[{}]:[],textContent:options.permanent?'永久删除':'删除文件',querySelector:()=>null,querySelectorAll:s=>s==='button'?[cancel,confirm]:s==='.delete-file-item'?(options.short?files.slice(0,1):files).map(f=>({querySelector:q=>q==='.delete-file-name'?text(options.wrong?'错误.mkv':f.name):q==='.delete-file-size'?text(f.size):null})):[]};
 const document={querySelector:s=>s==='button.toolbar-refresh-btn'?{click(){}}:s==='select.page-size-select'?{value:'all'}:s==='.file-list-status'?text('2 items'):null,querySelectorAll:s=>{
 if(s==='table.file-list tr.file-row[data-path]')return rows;
 if(s==='table.file-list tr.file-row.selected')return options.extra?[...rows,...rows]:rows.filter(r=>picked.has(r.getAttribute('data-path')));
 if(s==='.files-breadcrumb-bar .breadcrumb-item:not(.breadcrumb-root)')return dir.split('/').slice(1).map(text);
 if(s==='button[title="删除"]')return [{getClientRects:()=>[{}],click(){open=true;}}];
 if(s==='[aria-labelledby="delete-confirm-title"]'||s==='[role="dialog"]')return [dialog];return [];
 }};
 const previous={document:global.document,location:global.location,MouseEvent:global.MouseEvent};Object.assign(global,{document,location:{href:'https://clouddrive.example/?page=files&path='+encodeURIComponent(dir)},MouseEvent:class{}});
 try{const client=C.createClient(C,{checkpoint(){},get(){},put(){},del(){},message(){},ready:()=>true});let error;try{await client.deleteFiles(files,files);}catch(e){error=e;}return {error,confirmations,cancelled};}finally{Object.assign(global,previous);}
}
test('真实批量适配器一次确认多个文件；确认清单不全、错误名称、永久删除均不提交',async()=>{
 const ok=await simulate();assert.equal(ok.error,undefined);assert.equal(ok.confirmations,1);
 for(const option of ['short','wrong','permanent']){const r=await simulate({[option]:true});assert.equal(r.confirmations,0);assert.equal(r.error.notSubmitted,true);assert.equal(r.cancelled,1);}
 const unknown=await simulate({after:true});assert.equal(unknown.confirmations,1);assert.equal(unknown.error.notSubmitted,undefined);
});
