const fs=require('node:fs');
const path=require('node:path');
const root=__dirname;
const ui=fs.readFileSync(path.join(root,'subscription-ui.js'),'utf8').replaceAll('v1.2','v2.4.0');
const end=ui.indexOf('// ==/UserScript==')+'// ==/UserScript=='.length;
const header=ui.slice(0,end).replace('// @grant        none','// @noframes\n// @grant        none');
const result=header+'\n\n'+['maintenance-core.js','maintenance-runtime.js'].map(f=>fs.readFileSync(path.join(root,f),'utf8')).join('\n\n')+'\n\n'+ui.slice(end).trimStart();
fs.writeFileSync(path.join(root,'moviepilot-unsubscribe.user.js'),result,'utf8');
console.log('Built v2.4.0 standalone userscript.');
