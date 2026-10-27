#!/usr/bin/env node
/**
 * 剧集整理工作流 — CLI 入口
 * 用法: node bin/tv-workflow.js <命令> [选项]
 */
import { loadConfig } from '../lib/config.js';
import { createLogger } from '../lib/logger.js';
import { runStage } from '../lib/stages/index.js';
import { notify } from '../lib/platform/notify.js';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
process.chdir(resolve(__dirname, '..'));

const USAGE = `
  🎬 剧集整理工作流 v1.0.0

  用法: node bin/tv-workflow.js <命令> [选项]

  命令:
    inspect              ① 剧集初检（支持交互式 -i 或指定 --category/--show/--path）
    download [--report]  ② 补缺下载（含自动整理修复与资源搜索）
    repair [--report]    🔧 整理历史修复（仅检查并解除 MoviePilot 阻塞历史重转）
    review [--report]    ③ 复核清理（核验修复闭环，清理冗余版本）
    archive [--report]   ④ 归档（移动完结剧集到目标目录，含自动对账复核）
    verify [--report]    🔍 归档后对账复核（验证目标完整性、源存量并清理残留空别名）
    auto                 全自动流程（依次执行全流程）

  选项:
    --config <path>      指定配置文件 (默认: config.json)
    --report <path>      指定输入报告路径
    --path <dirPath>     指定网盘扫描目录（可为单部剧、单分类或根目录路径）
    --category <name>    仅初检指定分类（如: 日韩动漫、国产剧、欧美剧）
    --show <keyword>     仅初检匹配剧名关键字（如: 凡人修仙传）
    -i, --interactive    交互式浏览网盘分类与剧集文件夹后再初检
    --yes                跳过预览，直接执行清理/归档
    --help               显示帮助

  初检示例 (已禁用全库初检，需定向检测):
    node bin/tv-workflow.js inspect -i                           # 交互式选剧初检 (支持动漫/电视剧分级导航)
    node bin/tv-workflow.js inspect --category 日韩动漫          # 仅初检指定分类
    node bin/tv-workflow.js inspect --show 凡人修仙传            # 仅初检包含关键字的剧集
    node bin/tv-workflow.js inspect --path "/115/Library_incs/nas/动漫/日韩动漫/葬送的芙莉莲" # 精确扫描指定目录
`.trim();

// 解析命令行参数
function parseArgs(argv) {
  const args = {
    command: null,
    config: 'config.json',
    report: null,
    path: null,
    category: null,
    show: null,
    interactive: false,
    yes: false,
    help: false
  };
  for (let i = 2; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--help' || arg === '-h') args.help = true;
    else if (arg === '--yes' || arg === '-y') args.yes = true;
    else if (arg === '-i' || arg === '--interactive') args.interactive = true;
    else if (arg === '--config' && argv[i + 1]) args.config = argv[++i];
    else if (arg === '--report' && argv[i + 1]) args.report = argv[++i];
    else if (arg === '--path' && argv[i + 1]) args.path = argv[++i];
    else if (arg === '--category' && argv[i + 1]) args.category = argv[++i];
    else if (arg === '--show' && argv[i + 1]) args.show = argv[++i];
    else if (!arg.startsWith('-') && !args.command) args.command = arg;
  }
  return args;
}

async function main() {
  const args = parseArgs(process.argv);

  if (args.help || !args.command) {
    console.log(USAGE);
    process.exit(args.help ? 0 : 1);
  }

  const validCommands = ['inspect', 'download', 'repair', 'review', 'archive', 'verify', 'audit', 'auto'];
  if (!validCommands.includes(args.command)) {
    console.error(`❌ 未知命令: ${args.command}\n`);
    console.log(USAGE);
    process.exit(1);
  }

  // 加载配置
  let config;
  try {
    config = loadConfig(args.config);
  } catch (e) {
    console.error(`❌ 配置加载失败: ${e.message}`);
    process.exit(1);
  }

  const logger = createLogger(config);
  const startTime = new Date();
  logger.info(`🕐 启动时间: ${startTime.toLocaleString('zh-CN')}`);
  logger.info(`📌 命令: ${args.command}${args.interactive ? ' --interactive' : ''}${args.report ? ' --report ' + args.report : ''}${args.yes ? ' --yes' : ''}`);

  if (args.command === 'inspect' && (args.interactive || (!args.path && !args.category && !args.show && process.stdin.isTTY))) {
    const readline = await import('node:readline');
    const { handleInteractiveInspect } = await import('../lib/platform/interactive-inspect.js');
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    try {
      await handleInteractiveInspect(rl, config, logger);
    } finally {
      rl.close();
    }
    process.exit(0);
  }

  try {
    const result = await runStage(args.command, config, logger, {
      reportPath: args.report,
      path: args.path,
      category: args.category,
      show: args.show,
      yes: args.yes,
    });

    const elapsed = ((Date.now() - startTime.getTime()) / 1000).toFixed(1);
    const message = formatResult(args.command, result, elapsed);
    logger.info(message);

    // 发送 Windows 通知
    if (config.notifications?.enabled !== false) {
      notify('✅ 剧集整理完成', message.replace(/\n/g, ' ').slice(0, 150));
    }

    process.exit(0);
  } catch (e) {
    logger.error(`\n💥 执行失败: ${e.message}`);
    if (config.notifications?.enabled !== false) {
      notify('❌ 剧集整理失败', `${args.command}: ${e.message}`.slice(0, 150));
    }
    process.exit(1);
  }
}

function formatResult(command, result, elapsed) {
  if (!result) return `⏱ 耗时 ${elapsed} 秒`;
  const lines = [`⏱ 耗时 ${elapsed} 秒`];
  if (result.outputPath) lines.push(`📄 报告: ${result.outputPath}`);
  return lines.join('\n');
}

main().catch(e => {
  console.error('💥 未捕获异常:', e);
  process.exit(1);
});
