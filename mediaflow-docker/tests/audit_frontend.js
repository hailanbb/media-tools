const fs = require('fs');
const htmlPath = fs.existsSync('frontend/index.html') ? 'frontend/index.html' : 'mediaflow/frontend/index.html';
const html = fs.readFileSync(htmlPath, 'utf8');

// 1. 提取所有 HTML 中的 id
const idSet = new Set();
const idRegex = /id=["']([^"']+)["']/g;
let m;
while ((m = idRegex.exec(html)) !== null) {
  idSet.add(m[1]);
}
console.log('Total HTML IDs found:', idSet.size);

// 2. 提取主 script 代码
const p3 = html.indexOf('<script', 500);
const sMain = html.substring(p3);
const endScript = sMain.indexOf('</script>');
const code = sMain.substring(sMain.indexOf('>') + 1, endScript);

// 提取 JS 中所有的 document.getElementById('...')
const getElRegex = /document\.getElementById\(\s*['"]([^'"]+)['"]\s*\)/g;
const missingIds = new Set();
const checkedIds = new Set();
while ((m = getElRegex.exec(code)) !== null) {
  const id = m[1];
  checkedIds.add(id);
  if (!idSet.has(id)) {
    // 排除模板字符串或动态组合 ID
    if (!id.includes('${')) {
      missingIds.add(id);
    }
  }
}
console.log('Total document.getElementById checked:', checkedIds.size);
console.log('Missing DOM IDs:', Array.from(missingIds));

// 3. 提取 HTML 中所有 onclick/onchange/oninput 事件并验证其函数定义
const inlineEventRegex = /on(?:click|change|input|submit)=["']([^"']+)["']/g;
const eventCalls = [];
while ((m = inlineEventRegex.exec(html)) !== null) {
  eventCalls.push(m[1].trim());
}
console.log('Total inline event attributes found:', eventCalls.length);

// 提取 JS 声明的函数
const declaredFuncs = new Set();
const funcDeclRegex = /function\s+([a-zA-Z0-9_$]+)/g;
while ((m = funcDeclRegex.exec(code)) !== null) declaredFuncs.add(m[1]);
const asyncFuncRegex = /async\s+function\s+([a-zA-Z0-9_$]+)/g;
while ((m = asyncFuncRegex.exec(code)) !== null) declaredFuncs.add(m[1]);

// 检查每个事件调用的函数
const missingHandlers = new Set();
for (const raw of eventCalls) {
  const parts = raw.split(';').map(x => x.trim()).filter(Boolean);
  for (const part of parts) {
    if (part.startsWith('event.')) continue;
    const fnMatch = part.match(/^([a-zA-Z0-9_$]+)\s*\(/);
    if (fnMatch) {
      const fn = fnMatch[1];
      if (!declaredFuncs.has(fn)) {
        missingHandlers.add(fn);
      }
    }
  }
}
console.log('Declared functions count:', declaredFuncs.size);
console.log('Missing inline event handlers:', Array.from(missingHandlers));

// 4. 模态框 ID 完整性检查
const openModalRegex = /openModal\(\s*['"]([^'"]+)['"]\s*\)/g;
const missingModals = new Set();
while ((m = openModalRegex.exec(code)) !== null) {
  const modalId = m[1];
  if (!idSet.has(modalId)) {
    missingModals.add(modalId);
  }
}
console.log('Missing Modal IDs for openModal():', Array.from(missingModals));

const closeModalRegex = /closeModal\(\s*['"]([^'"]+)['"]\s*\)/g;
while ((m = closeModalRegex.exec(code)) !== null) {
  const modalId = m[1];
  if (!idSet.has(modalId)) {
    missingModals.add(modalId);
  }
}
console.log('Missing Modal IDs for closeModal():', Array.from(missingModals));

if (missingIds.size > 0 || missingHandlers.size > 0 || missingModals.size > 0) {
  console.error('\nAUDIT FAILED: Discrepancies found!');
  process.exit(1);
} else {
  console.log('\nAUDIT PASSED: All DOM elements, Event Handlers, and Modal IDs are 100% matched!');
}
