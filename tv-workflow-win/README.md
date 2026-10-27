# 剧集整理 Windows 版

通过 CloudDrive2、MoviePilot 和 TMDB 检查电视剧与动漫，生成 Excel 报告，再按报告执行补缺、复核和归档。支持命令行与交互式控制台。

[返回项目列表](../README.md) · [需求与设计文档](../媒体整理需求/README.md)

## 使用前准备

- Windows 电脑，安装 **Node.js 20 或更新版本**。
- 可从这台电脑访问的 CloudDrive2 和 MoviePilot，以及对应访问令牌。
- 用于查询剧集信息的 TMDB API Key。
- 已确定的待整理源目录、归档目标目录，以及 MoviePilot 下载/整理目录。

CloudDrive2 中看到的云盘路径和 MoviePilot 容器内的路径可能不同，配置时按各服务实际路径填写。

## 安装

### 1. 安装 Node.js

从 [Node.js 官网](https://nodejs.org/)安装满足版本要求的 LTS 版本。安装完成后重新打开 PowerShell，输入：

~~~powershell
node --version
npm --version
~~~

两条命令都应显示版本号，Node.js 的主版本号应至少为 20。如果提示找不到命令，重新打开终端并检查 Node.js 安装。

### 2. 下载并打开项目目录

在[仓库首页](https://github.com/hailanbb/media-tools)点击 **Code → Download ZIP**，解压后打开其中的 tv-workflow-win 文件夹。

在资源管理器地址栏输入 powershell 并回车，终端会在当前文件夹打开。确认能看到 package.json 和 config.example.json 后安装依赖：

~~~powershell
npm install
~~~

等命令结束且没有安装失败提示，再继续下一步。安装时间取决于网络和电脑环境。

### 3. 创建配置文件

首次安装时执行：

~~~powershell
Copy-Item -LiteralPath config.example.json -Destination config.json
notepad config.json
~~~

如果已有 config.json，直接编辑原文件，跳过复制步骤。

保留模板的 JSON 结构，只替换相应值：

| 配置项 | 填写内容 |
| --- | --- |
| clouddrive2.origin | CloudDrive2 实际访问地址，包含协议和端口 |
| clouddrive2.token | CloudDrive2 访问令牌 |
| moviepilot.origin | MoviePilot 实际访问地址 |
| moviepilot.token | MoviePilot 访问令牌 |
| tmdb.apiKey | TMDB API Key |
| paths.source | CloudDrive2 中的待整理源根目录 |
| paths.destination | CloudDrive2 中的归档目标根目录 |
| paths.downloadPath | MoviePilot 使用的下载路径 |
| transferRepair.source / target | 整理修复使用的源、目标路径，需核对存储映射 |
| moviepilot.extraSearchSites | 补缺时追加搜索的站点名称 |
| schedule.enabled | 是否启用定时流程，模板默认为 false |

保存为 UTF-8。JSON 不支持注释；不要添加双斜杠说明或在最后一个字段后多加逗号。完整字段以 [config.example.json](config.example.json) 为准。

## 第一次运行

在项目目录执行：

~~~powershell
node bin/tray.js
~~~

这是终端交互菜单。首次使用建议按以下顺序：

1. 输入 **1**，先选择一部剧或一个小分类进行初检。
2. 打开 reports 目录中的报告，核对分类、路径、季号和缺集结果。
3. 需要补缺时再执行下载；提交成功后，等待 MoviePilot 实际下载和整理。
4. 先预览复核或归档计划，核对对象后再选择实际执行。

菜单选项：

| 输入 | 操作 |
| --- | --- |
| 1 | 按媒体类型、分类和剧集选择初检范围 |
| 2 | 补缺下载，包含整理修复处理 |
| r | 整理历史专项修复 |
| 3 / 5 | 复核清理预览 / 实际执行删除 |
| 4 / 6 | 归档预览 / 实际执行移动 |
| v | 归档后对账；当前实现还包含空别名清理 |
| 7 | 全流程执行，包含实际清理与归档 |
| 8 / 9 | 打开最新报表 / 日志目录 |
| 0 | 退出 |

下载、修复和执行选项会改变外部状态。当前 verify 也包含清理行为，auto 会执行实际删除与移动，使用前应明确处理范围。

## 命令行使用

在项目目录运行：

~~~powershell
# 交互选择初检范围
node bin/tv-workflow.js inspect -i

# 指定分类、剧名或云盘路径初检
node bin/tv-workflow.js inspect --category 日韩动漫
node bin/tv-workflow.js inspect --show 示例剧名
node bin/tv-workflow.js inspect --path "/云盘/媒体库/动漫/示例剧名"

# 补缺下载或专项整理修复
node bin/tv-workflow.js download
node bin/tv-workflow.js repair

# 复核与归档预览
node bin/tv-workflow.js review
node bin/tv-workflow.js archive
~~~

需要明确输入报告时，附加 --report "报告的完整路径"。实际清理或归档使用：

~~~powershell
node bin/tv-workflow.js review --report "报告.xlsx" --yes
node bin/tv-workflow.js archive --report "报告.xlsx" --yes
~~~

其他命令：

| 命令 | 行为 |
| --- | --- |
| node bin/tv-workflow.js verify | 归档对账及当前实现中的空别名清理 |
| node bin/tv-workflow.js auto | 初检、下载、实际清理、实际归档 |
| node bin/tv-workflow.js --help | 查看参数说明 |

## 报告与日志

- reports：各阶段 Excel 报告。
- logs：运行日志。
- data：下载、防重复和修复等持久记录。

报告颜色是处理入口和状态提示，具体原因查看处理结果及说明。文件数量不能单独证明集号完整；下载提交也不代表文件已经入库。已知实现差异和目标规则见[实现差异与开发路线](../媒体整理需求/07-实现差异与开发路线.md)。

## 后台与定时运行

在 config.json 中检查 schedule.enabled 和 schedule.cron，再启动控制台。定时功能依赖程序持续运行；关闭终端或电脑休眠会影响任务。当前调度支持范围以实现为准，不按完整 cron 功能推定。

定时和全流程包含实际操作，首次运行先使用手动、小范围检查。

## 常见问题

| 现象 | 检查方法 |
| --- | --- |
| node 或 npm 找不到 | 重新打开终端，检查 Node.js 版本和安装 |
| 找不到配置文件 | 确认文件名是 config.json，而不是 config.json.txt |
| 配置格式错误 | 检查引号、逗号和注释，参照配置模板 |
| 401 / 403 或连接失败 | 核对服务地址、端口和令牌；确认当前电脑能访问服务 |
| 没有找到剧集 | 检查源根、分类层级和选择范围，先指定单剧路径初检 |
| 报告无法写入 | 关闭正在用 Excel/WPS 打开的输出文件，再查看日志 |
| 下载已提交但云盘没出现 | 检查下载器与 MoviePilot 整理结果，等待文件实际进入目标目录 |

## 目录与维护

| 目录/文件 | 用途 |
| --- | --- |
| bin | 命令行及交互菜单入口 |
| lib/clients | CloudDrive2、MoviePilot、TMDB 客户端 |
| lib/core | 集号、标题、报告和修复逻辑 |
| lib/stages | 初检、下载、复核、归档与编排 |
| lib/platform | 通知、调度及本地存储 |
| config.example.json | 配置模板 |

本页安装说明依据当前包要求和入口整理；没有将需求文档中的全部目标能力视为已实现。代码问题与真实环境验证范围请结合需求文档和运行日志判断。
