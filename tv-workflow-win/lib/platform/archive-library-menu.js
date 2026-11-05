import { posix, join } from 'node:path';
import { readdirSync, statSync } from 'node:fs';
import { PROJECT_ROOT } from '../config.js';
import { readReport } from '../core/report-adapter.js';
import { handleInteractiveInspect } from './interactive-inspect.js';
import { runReview } from '../stages/review.js';

export function loadArchivePreview(reportPath, root) {
  const { tasks } = readReport(reportPath, { mode: 'archive' });
  const plan = new Map();
  let plannedFiles = 0;
  for (const task of tasks) {
    if (task.data['复核结果'] !== '待确认清理（预览）') continue;
    const remove = String(task.data['拟删除文件'] || '').split(/\r?\n/).filter(Boolean).sort();
    const keep = String(task.data['拟保留正片'] || '').split(/\r?\n/).filter(Boolean).sort();
    const paths = [...remove, ...keep];
    if (!remove.length || !keep.length || plan.has(task.path) ||
        !task.path.startsWith(root + '/') || new Set(paths).size !== paths.length ||
        task.path.includes('\\') || posix.normalize(task.path) !== task.path ||
        !paths.every(path => !path.includes('\\') && posix.normalize(path) === path &&
          posix.dirname(path) === task.path)) {
      throw new Error('预览报告的文件明细不完整、重复或超出归档范围，请重新生成预览');
    }
    plan.set(task.path, JSON.stringify({ remove, keep }));
    plannedFiles += remove.length;
  }
  return { outputPath: reportPath, plan, plannedFiles };
}

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

  async function selectReport(previewOnly = false) {
    const directory = join(PROJECT_ROOT, 'reports', '归档目录');
    let files;
    try {
      files = readdirSync(directory)
        .filter(name => name.startsWith('归档目录初检报告') && name.endsWith('.xlsx') &&
          !name.includes('_复核清理') && (!previewOnly || name.endsWith('_复核预览.xlsx')))
        .map(name => ({ name, path: join(directory, name) }))
        .filter(file => statSync(file.path).isFile())
        .sort((a, b) => statSync(b.path).mtimeMs - statSync(a.path).mtimeMs);
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      files = [];
    }
    if (!files.length) {
      console.log(previewOnly ? '  暂无预览报告，请先选择初检报告并按 [2] 预览。' : '  暂无已有报告，请先按 [1] 初检。');
      return false;
    }
    files.forEach((file, index) => console.log(`  [${index + 1}] ${file.name}`));
    const answer = (await ask('  请选择已有报告序号（直接回车取消） > ')).trim();
    if (!answer) return false;
    const selected = files[Number(answer) - 1];
    if (!/^\d+$/.test(answer) || !selected) {
      console.log('  无效报告序号。');
      return false;
    }
    const loaded = selected.name.endsWith('_复核预览.xlsx') ? loadArchivePreview(selected.path, root) : undefined;
    reportPath = selected.path;
    preview = loaded;
    console.log(`  已选择报告: ${reportPath}`);
    if (preview) console.log(`  已恢复预览，拟删除 ${preview.plannedFiles} 个文件；执行前将重新核对。`);
    return true;
  }

  while (true) {
    console.log(`
  归档目录检查与清理 · ${root}
  [1] 初检并生成缺集报告
  [2] 预览重复版本清理
  [3] 按预览执行清理
  [4] 选择已有初检/预览报告
  [0] 返回主菜单
  当前报告: ${reportPath || '未选择（可读取已有报告）'}`);
    const choice = (await ask('  请选择 > ')).trim();
    if (choice === '0' || !choice) return;

    if (choice === '1') {
      const result = await handleInteractiveInspect(rl, config, logger, { archiveRoot: root });
      if (!result) continue;
      preview = undefined;
      reportPath = result.outputPath;
      if (reportPath) console.log('  请在报告中查看「缺失集号」「重复集号」和「无法识别集号的文件」。');
    } else if (choice === '2') {
      if (!reportPath && !await selectReport()) continue;
      preview = undefined;
      preview = await runReview(config, logger, reportPath, { archiveRoot: root });
      console.log(`  拟删除 ${preview.plannedFiles} 个文件。请查看预览报告中的保留与删除明细。`);
    } else if (choice === '3') {
      if (!preview && !await selectReport(true)) continue;
      if (!preview?.plannedFiles) {
        console.log('  当前预览没有可执行的删除项，可按 [2] 重新预览或按 [4] 切换报告。');
        continue;
      }
      const confirmation = await ask(`  预览拟删除 ${preview.plannedFiles} 个文件。核对 ${preview.outputPath} 后，输入「确认删除」继续 > `);
      if (confirmation.trim() !== '确认删除') continue;
      const approvedPlan = preview.plan;
      preview = undefined;
      await runReview(config, logger, reportPath, { dryRun: false, archiveRoot: root, approvedPlan });
      console.log('  清理结束。可再次选择 [1] 核对集号。');
    } else if (choice === '4') {
      await selectReport();
    } else {
      console.log('  无效选择。');
    }
  }
}
