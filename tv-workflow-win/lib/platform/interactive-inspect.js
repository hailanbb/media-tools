/**
 * 交互式定向/指定剧集初检导航器
 * 支持媒体类型 (动漫/电视剧) -> 子分类 (日韩动漫/国产剧等) -> 剧集文件夹逐级选择
 * 根目录: /115/Library_incs/nas
 */
import { CloudDriveClient } from '../clients/clouddrive2.js';
import { runStage } from '../stages/index.js';

/**
 * 解析用户输入的数字序号列表（支持 1 或 1,3,5 或区间 1-3）
 * @param {string} str
 * @param {number} max
 * @returns {number[]}
 */
function parseIndices(str, max) {
  const parts = str.split(/[\s,，]+/);
  const result = new Set();
  for (const p of parts) {
    if (p.includes('-')) {
      const [start, end] = p.split('-').map(Number);
      if (!isNaN(start) && !isNaN(end)) {
        for (let i = Math.min(start, end); i <= Math.max(start, end); i++) {
          if (i >= 1 && i <= max) result.add(i);
        }
      }
    } else {
      const n = Number(p);
      if (!isNaN(n) && n >= 1 && n <= max) result.add(n);
    }
  }
  return [...result].sort((a, b) => a - b);
}

/**
 * 执行交互式剧集初检导航
 * @param {import('node:readline').Interface} rl
 * @param {object} config
 * @param {object} logger
 */
export async function handleInteractiveInspect(rl, config, logger) {
  const cd2 = new CloudDriveClient({
    origin: config.clouddrive2.origin,
    token: config.clouddrive2.token,
    logger,
  });

  const sourceRoot = (config.paths.source || '/115/Library_incs/nas').replace(/\/+$/, '');
  console.log(`\n⏳ 正在获取网盘媒体库根目录结构 (${sourceRoot})...`);

  let rootEntries = [];
  try {
    rootEntries = await cd2.listFiles(sourceRoot);
  } catch (err) {
    logger.error(`读取源目录失败: ${err.message}`);
    return;
  }

  // 过滤有效媒体类型目录（排除 mount-check, 电影等，保留 动漫, 电视剧）
  const validTypes = rootEntries.filter(f => f.directory && !/mount-check|电影|^\./i.test(f.name));
  if (!validTypes.length) {
    console.log('⚠️ 源根目录下未发现有效媒体类型文件夹');
    return;
  }

  const ask = (q) => new Promise(res => {
    let resolved = false;
    const finish = (val) => {
      if (!resolved) {
        resolved = true;
        res(val);
      }
    };
    if (rl.closed) return finish('');
    try {
      rl.question(q, (ans) => finish(ans));
      rl.once('close', () => finish(''));
    } catch {
      finish('');
    }
  });

  console.log(`\n${'═'.repeat(54)}`);
  console.log(`📁 可选媒体库类型 (根目录: ${sourceRoot})`);
  console.log(`${'═'.repeat(54)}`);
  validTypes.forEach((t, idx) => {
    const num = String(idx + 1).padStart(2, ' ');
    console.log(`  [${num}] ${t.name} (${t.path})`);
  });
  console.log(`${'═'.repeat(54)}`);

  const typePrompt =
    `  👉 请选择媒体库类型:\n` +
    `     • 输入序号 (如: 1 选动漫, 2 选电视剧)\n` +
    `     • 输入名称 (如: 动漫 或 电视剧)\n` +
    `     • 或直接输入剧名关键词/网盘绝对路径直达初检\n` +
    `     (直接回车取消) > `;

  const typeInput = (await ask(typePrompt)).trim();
  if (!typeInput) {
    console.log('  ⚠️ 操作已取消');
    return;
  }

  // 1. 如果用户直接输入了网盘绝对路径
  if (typeInput.startsWith('/')) {
    logger.info(`🎯 直接扫描指定路径: ${typeInput}`);
    await runStage('inspect', config, logger, { path: typeInput });
    return;
  }

  // 2. 匹配媒体类型
  let targetType = null;
  const typeIdx = parseInt(typeInput, 10);
  if (!isNaN(typeIdx) && typeIdx >= 1 && typeIdx <= validTypes.length) {
    targetType = validTypes[typeIdx - 1];
  } else {
    targetType = validTypes.find(t => t.name.toLowerCase() === typeInput.toLowerCase()) ||
                 validTypes.find(t => t.name.includes(typeInput));
  }

  // 3. 若第一步未匹配到类型，视为全局剧名关键词直接检索
  if (!targetType) {
    logger.info(`🔍 未匹配到类型，按剧名关键词跨动漫/电视剧检索: "${typeInput}"`);
    await runStage('inspect', config, logger, { show: typeInput });
    return;
  }

  // 4. 获取该媒体类型下的子分类（例如 动漫 -> 国产动漫、日韩动漫、欧美动漫）
  console.log(`\n⏳ 正在获取 [${targetType.name}] 下的子分类列表...`);
  let subCategories = [];
  try {
    const typeEntries = await cd2.listFiles(targetType.path);
    subCategories = typeEntries.filter(f => f.directory);
  } catch (err) {
    logger.error(`读取分类目录 ${targetType.path} 失败: ${err.message}`);
    return;
  }

  if (!subCategories.length) {
    console.log(`⚠️ [${targetType.name}] 下未发现子分类`);
    return;
  }

  console.log(`\n${'═'.repeat(54)}`);
  console.log(`📂 [${targetType.name}] 子分类列表:`);
  console.log(`${'═'.repeat(54)}`);
  subCategories.forEach((cat, idx) => {
    const num = String(idx + 1).padStart(2, ' ');
    console.log(`  [${num}] ${cat.name}`);
  });
  console.log(`${'═'.repeat(54)}`);

  const catPrompt =
    `  👉 请选择子分类以查看剧集:\n` +
    `     • 输入分类序号 (如: 1)\n` +
    `     • 输入分类名称 (如: 日韩动漫)\n` +
    `     • 或输入剧名关键词快速查找\n` +
    `     (直接回车取消) > `;

  const catInput = (await ask(catPrompt)).trim();
  if (!catInput) {
    console.log('  ⚠️ 操作已取消');
    return;
  }

  // 检查是否选定子分类
  let targetCat = null;
  const catIdx = parseInt(catInput, 10);
  if (!isNaN(catIdx) && catIdx >= 1 && catIdx <= subCategories.length) {
    targetCat = subCategories[catIdx - 1];
  } else {
    targetCat = subCategories.find(c => c.name.toLowerCase() === catInput.toLowerCase()) ||
                subCategories.find(c => c.name.includes(catInput));
  }

  // 若未匹配子分类，视为在该媒体类型下搜索剧名
  if (!targetCat) {
    logger.info(`🔍 按关键词在 [${targetType.name}] 检索: "${catInput}"`);
    await runStage('inspect', config, logger, { path: targetType.path, show: catInput });
    return;
  }

  // 5. 列出该子分类下的具体剧集/动漫文件夹
  console.log(`\n⏳ 正在获取 [${targetType.name} / ${targetCat.name}] 剧集列表...`);
  let showDirs = [];
  try {
    const catFiles = await cd2.listFiles(targetCat.path);
    showDirs = catFiles.filter(f => f.directory);
  } catch (err) {
    logger.error(`读取目录 ${targetCat.path} 失败: ${err.message}`);
    return;
  }

  if (!showDirs.length) {
    console.log(`⚠️ 分类 [${targetCat.name}] 下暂无剧集文件夹`);
    return;
  }

  console.log(`\n${'═'.repeat(54)}`);
  console.log(`📂 [${targetType.name} / ${targetCat.name}] 列表 (共 ${showDirs.length} 部):`);
  console.log(`${'═'.repeat(54)}`);

  const maxDisplay = 60;
  const displayCount = Math.min(showDirs.length, maxDisplay);
  for (let i = 0; i < displayCount; i++) {
    const num = String(i + 1).padStart(3, ' ');
    console.log(`  [${num}] ${showDirs[i].name}`);
  }
  if (showDirs.length > maxDisplay) {
    console.log(`  ... 其余 ${showDirs.length - maxDisplay} 部已折叠 (直接输入剧名关键词可快速过滤) ...`);
  }
  console.log(`${'═'.repeat(54)}`);

  // 6. 提示用户选择或输入具体剧集
  const showPrompt =
    `  👉 请选择要初检的剧集/动漫:\n` +
    `     • 输入序号 (单选如 1，多选如 1,3，或区间 1-5)\n` +
    `     • 输入 "all" 初检该子分类全部剧集\n` +
    `     • 输入剧名关键词或完整文件夹名称\n` +
    `     (直接回车取消) > `;

  const showChoice = (await ask(showPrompt)).trim();
  if (!showChoice) {
    console.log('  ⚠️ 操作已取消');
    return;
  }

  // 处理 "all"
  if (showChoice.toLowerCase() === 'all') {
    logger.info(`🎯 初检整子分类: [${targetType.name} / ${targetCat.name}] (共 ${showDirs.length} 部)`);
    await runStage('inspect', config, logger, { path: targetCat.path });
    return;
  }

  // 处理序号 (支持单数字、逗号多选、破折号区间如 1-3)
  const isNumberSelection = /^(\d+)([\s,，-]\d+)*$/.test(showChoice);
  if (isNumberSelection) {
    const selectedIndices = parseIndices(showChoice, showDirs.length);
    if (selectedIndices.length === 1) {
      const selected = showDirs[selectedIndices[0] - 1];
      logger.info(`🎯 初检指定剧集: [${targetCat.name}] ${selected.name}`);
      await runStage('inspect', config, logger, { path: selected.path });
      return;
    } else if (selectedIndices.length > 1) {
      console.log(`🎯 已选中 ${selectedIndices.length} 部:`);
      for (const idx of selectedIndices) {
        const item = showDirs[idx - 1];
        console.log(`   - ${item.name}`);
        await runStage('inspect', config, logger, { path: item.path });
      }
      return;
    }
  }

  // 处理文本匹配（文件夹完整名称 或 模糊关键词）
  const exactMatch = showDirs.find(d => d.name.toLowerCase() === showChoice.toLowerCase());
  if (exactMatch) {
    logger.info(`🎯 匹配到文件夹: ${exactMatch.name}`);
    await runStage('inspect', config, logger, { path: exactMatch.path });
    return;
  }

  const fuzzyMatches = showDirs.filter(d => d.name.toLowerCase().includes(showChoice.toLowerCase()));
  if (fuzzyMatches.length === 1) {
    logger.info(`🎯 关键词精准匹配到 1 个文件夹: ${fuzzyMatches[0].name}`);
    await runStage('inspect', config, logger, { path: fuzzyMatches[0].path });
    return;
  } else if (fuzzyMatches.length > 1) {
    console.log(`\n🔍 关键词 "${showChoice}" 在该分类匹配到 ${fuzzyMatches.length} 个文件夹:`);
    fuzzyMatches.forEach((m, idx) => console.log(`  [${idx + 1}] ${m.name}`));
    const subChoice = (await ask(`  👉 请输入序号选择 (1-${fuzzyMatches.length})，直接回车初检全部匹配项: `)).trim();
    const subIdx = parseInt(subChoice, 10);
    if (!isNaN(subIdx) && subIdx >= 1 && subIdx <= fuzzyMatches.length) {
      const pick = fuzzyMatches[subIdx - 1];
      logger.info(`🎯 初检指定剧集: ${pick.name}`);
      await runStage('inspect', config, logger, { path: pick.path });
    } else {
      logger.info(`🎯 初检全部 ${fuzzyMatches.length} 个匹配项...`);
      for (const m of fuzzyMatches) {
        await runStage('inspect', config, logger, { path: m.path });
      }
    }
    return;
  }

  // 均未匹配，按关键词初检
  logger.info(`🎯 未在 [${targetCat.name}] 中发现同名文件夹，尝试按关键词初检: "${showChoice}"`);
  await runStage('inspect', config, logger, { path: targetCat.path, show: showChoice });
}
