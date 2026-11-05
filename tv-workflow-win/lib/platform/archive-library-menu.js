import { posix } from 'node:path';
import { handleInteractiveInspect } from './interactive-inspect.js';
import { runReview } from '../stages/review.js';

export async function handleArchiveLibraryMenu(rl, config, logger) {
  const root = config.paths.destination?.replace(/\/+$/, '');
  const source = config.paths.source?.replace(/\/+$/, '');
  if (!root?.startsWith('/') || !source?.startsWith('/') || root === source ||
      root.startsWith(source + '/') || source.startsWith(root + '/') ||
      root.includes('\\') || posix.normalize(root) !== root) {
    throw new Error('请先设置与源目录分开的有效归档目标 paths.destination');
  }

  const ask = (prompt) => new Promise((resolve) => rl.question(prompt, resolve));
  let reportPath;
  let preview;

  while (true) {
    console.log(`
  归档目录检查与清理 · ${root}
  [1] 初检并生成缺集报告
  [2] 预览重复版本清理
  [3] 按预览执行清理
  [0] 返回主菜单
  当前初检报告: ${reportPath || '尚未生成'}`);
    const choice = (await ask('  请选择 > ')).trim();
    if (choice === '0' || !choice) return;

    if (choice === '1') {
      const result = await handleInteractiveInspect(rl, config, logger, { archiveRoot: root });
      if (!result) continue;
      preview = undefined;
      reportPath = result.outputPath;
      if (reportPath) console.log('  请在报告中查看「缺失集号」「重复集号」和「无法识别集号的文件」。');
    } else if (choice === '2') {
      if (!reportPath) {
        console.log('  请先在本菜单初检。');
        continue;
      }
      preview = undefined;
      preview = await runReview(config, logger, reportPath, { archiveRoot: root });
      console.log(`  拟删除 ${preview.plannedFiles} 个文件。请查看预览报告中的保留与删除明细。`);
    } else if (choice === '3') {
      if (!preview?.plannedFiles) {
        console.log('  请先生成有删除项的预览。');
        continue;
      }
      const confirmation = await ask(`  预览拟删除 ${preview.plannedFiles} 个文件。核对 ${preview.outputPath} 后，输入「确认删除」继续 > `);
      if (confirmation.trim() !== '确认删除') continue;
      const approvedPlan = preview.plan;
      preview = undefined;
      await runReview(config, logger, reportPath, { dryRun: false, archiveRoot: root, approvedPlan });
      console.log('  清理结束。可再次选择 [1] 核对集号。');
    } else {
      console.log('  无效选择。');
    }
  }
}
