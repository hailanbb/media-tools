#!/usr/bin/env node
/**
 * 剧集整理工作流 — 交互式后台控制台 + 定时调度器
 * 支持 TTY 模式（交互菜单）和无 TTY 模式（纯后台定时）
 */
import { loadConfig } from '../lib/config.js';
import { createLogger } from '../lib/logger.js';
import { runStage, runAuto } from '../lib/stages/index.js';
import { handleInteractiveInspect } from '../lib/platform/interactive-inspect.js';
import { notify } from '../lib/platform/notify.js';
import { createScheduler } from '../lib/platform/scheduler.js';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { execSync } from 'node:child_process';

const __dirname = dirname(fileURLToPath(import.meta.url));
process.chdir(resolve(__dirname, '..'));

const BANNER = `
${'═'.repeat(47)}
  🎬 剧集整理工作流 v1.0.0 — 后台运行中
${'═'.repeat(47)}`;

const MENU = `
  [1] 选剧/分类初检（浏览选择媒体库、分类与剧集，支持多选）
  [2] 立即补缺下载（含自动整理修复）
  [r] MoviePilot 整理历史专项修复
  [3] 立即复核清理（预览模式）
  [4] 立即执行归档（预览模式）
  [5] 复核清理（执行删除）
  [6] 执行归档（实际移动并自动对账复核）
  [v] 归档后对账复核（验证目标完整性与清理残留空别名）
  [7] 全自动流程
  [8] 打开最新报表
  [9] 打开日志目录
  [0] 退出
${'═'.repeat(47)}`;

async function main() {
  // 加载配置
  const configPath = process.argv[2] || 'config.json';
  let config;
  try {
    config = loadConfig(configPath);
  } catch (e) {
    console.error(`❌ 配置加载失败: ${e.message}`);
    console.error('请复制 config.example.json 为 config.json 并填写令牌信息');
    process.exit(1);
  }

  const logger = createLogger(config);

  // 包装全自动流程（含通知）
  async function autoWithNotify() {
    try {
      const result = await runAuto(config, logger);
      notify('✅ 全自动流程完成', '初检→下载→清理→归档 已执行');
      return result;
    } catch (e) {
      notify('❌ 全自动流程失败', e.message?.slice(0, 150));
      throw e;
    }
  }

  // 启动定时调度器
  const scheduler = createScheduler(config, logger, autoWithNotify);
  scheduler.start();

  // 判断运行模式
  if (!process.stdin.isTTY) {
    // 无 TTY 模式：纯后台，只有定时任务
    logger.info('🖥 后台模式启动（无交互终端）');
    logger.info('按 Ctrl+C 退出');

    process.on('SIGINT', () => {
      scheduler.stop();
      logger.info('👋 已退出');
      process.exit(0);
    });
    return; // 保持进程运行
  }

  // TTY 模式：显示交互菜单
  console.log(BANNER);
  const cronExpr = config.schedule?.cron || '0 2 * * *';
  const parts = cronExpr.split(/\s+/);
  console.log(`  定时任务: 每日 ${(parts[1] || '2').padStart(2, '0')}:${(parts[0] || '0').padStart(2, '0')} 自动执行`);
  console.log(MENU);

  const rl = createInterface({ input: process.stdin, output: process.stdout });
  let busy = false;

  function prompt() {
    rl.question('  请选择 > ', async (answer) => {
      if (busy) {
        console.log('  ⏳ 上一个任务仍在执行中...');
        prompt();
        return;
      }

      const choice = answer.trim();
      busy = true;

      try {
        switch (choice) {
          case '1':
          case 's':
          case 'S':
            await handleInteractiveInspect(rl, config, logger);
            break;
          case '2':
            await runStage('download', config, logger);
            break;
          case 'r':
          case 'R':
            await runStage('repair', config, logger);
            break;
          case '3':
            await runStage('review', config, logger, { yes: false });
            break;
          case '4':
            await runStage('archive', config, logger, { yes: false });
            break;
          case '5':
            await runStage('review', config, logger, { yes: true });
            break;
          case '6':
            await runStage('archive', config, logger, { yes: true });
            break;
          case 'v':
          case 'V':
            await runStage('verify', config, logger);
            break;
          case '7':
            await autoWithNotify();
            break;
          case '8':
            openLatestReport();
            break;
          case '9':
            try { execSync('explorer logs'); } catch { logger.warn('无法打开日志目录'); }
            break;
          case '0':
            scheduler.stop();
            logger.info('👋 再见');
            rl.close();
            process.exit(0);
            break;
          default:
            console.log('  ❓ 无效选择');
        }
      } catch (e) {
        logger.error(`执行失败: ${e.message}`);
      } finally {
        busy = false;
        console.log(MENU);
        prompt();
      }
    });
  }

  prompt();

  // 优雅退出
  process.on('SIGINT', () => {
    scheduler.stop();
    rl.close();
    logger.info('\n👋 已退出');
    process.exit(0);
  });
}

import { readdirSync } from 'node:fs';

function openLatestReport() {
  try {
    const files = readdirSync('reports')
      .filter(f => f.endsWith('.xlsx'))
      .sort()
      .reverse();
    if (files.length) {
      execSync(`start "" "${resolve('reports', files[0])}"`);
    } else {
      console.log('  📭 暂无报表');
    }
  } catch {
    console.log('  📭 暂无报表');
  }
}

main().catch(e => {
  console.error('💥 启动失败:', e.message);
  process.exit(1);
});
