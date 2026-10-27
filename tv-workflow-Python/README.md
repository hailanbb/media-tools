# 剧集整理 Python 版

面向 Ubuntu/Linux 的剧集与动漫整理工具，通过 CloudDrive2、MoviePilot 和 TMDB 执行初检、补缺、复核和归档。提供终端菜单、tv 命令和可选 systemd 后台服务。

[返回项目列表](../README.md) · [需求与设计文档](../媒体整理需求/README.md)

## 使用前准备

- 能使用终端或 SSH 的 Ubuntu/Linux 主机。
- 可访问的 CloudDrive2、MoviePilot，以及对应令牌和 TMDB API Key。
- 已确定的云盘源目录、归档目录与 MoviePilot 下载/整理路径。
- 执行安装脚本时所需的系统管理权限。

安装脚本使用 apt 安装缺失的系统依赖，主要面向 Ubuntu/Debian。其他发行版需要自行准备 Python 和依赖；不据此承诺全部 Linux 环境可直接安装。

## 安装与配置

### 1. 下载并进入目录

从[仓库首页](https://github.com/hailanbb/media-tools)下载 ZIP，解压后将 tv-workflow-Python 目录上传到服务器，例如：

    /opt/tv-workflow-Python

以下命令均以这个位置为例；使用其他目录时替换为实际路径。

~~~bash
cd /opt/tv-workflow-Python
~~~

确认当前目录中有 install.sh、config.example.json 和 requirements.txt。

### 2. 填写连接配置

首次安装时复制模板并编辑：

~~~bash
cp config.example.json config.json
nano config.json
~~~

已有 config.json 时跳过复制，直接编辑原文件。

| 配置项 | 填写内容 |
| --- | --- |
| clouddrive2.origin / token | CloudDrive2 地址与访问令牌 |
| moviepilot.origin / token | MoviePilot 地址与访问令牌 |
| tmdb.apiKey | TMDB API Key |
| paths.source | CloudDrive2 中的待整理源根 |
| paths.destination | CloudDrive2 中的归档目标根 |
| paths.downloadPath | MoviePilot 下载目录 |
| transferRepair.source / target | 整理修复的源、目标路径及存储映射 |
| schedule.enabled / cron | 定时开关与计划，先核对是否需要开启 |

配置文件必须是有效 UTF-8 JSON，不添加注释。服务地址应能从服务器访问；云盘路径和容器内路径按各自实际含义填写。完整字段见 [config.example.json](config.example.json)。

在 nano 中按 Ctrl+O、回车保存，再按 Ctrl+X 退出。

### 3. 运行安装脚本

在项目目录执行：

~~~bash
sudo bash install.sh
~~~

脚本会检查 Python/pip、安装 [requirements.txt](requirements.txt) 中的依赖、设置入口执行权限，并注册 /usr/local/bin/tv。它会尝试系统级依赖安装，必要时建立项目内的 .venv；安装完成后查看终端是否出现失败提示。

安装脚本不会替你填写服务信息。发现 config.json 也不代表其中的地址和令牌已经验证。

### 4. 打开菜单

~~~bash
tv
~~~

应看到数字菜单。若 tv 找不到，在项目目录尝试：

~~~bash
./tv-workflow.sh
~~~

这个启动器会选择项目内虚拟环境或系统 Python，无须手动 activate。

## 第一次使用

1. 输入 **1**，选择一部剧或一个小分类进行初检。
2. 查看 reports 里的报告，先核对范围、路径、季号与缺集。
3. 需要补缺时再执行下载，并等待实际下载与整理。
4. 先预览复核/归档清单，再决定实际执行。

| 菜单输入 | 操作 |
| --- | --- |
| 1 | 选择媒体库、分类与剧集初检 |
| 2 | 补缺下载，包含整理修复 |
| r | 整理历史专项修复 |
| 3 / 5 | 复核预览 / 实际删除 |
| 4 / 6 | 归档预览 / 实际移动 |
| v | 归档对账及当前实现中的空别名清理 |
| 7 | 全流程，包含实际清理和归档 |
| 8 / 9 | 查看报表路径 / 运行日志 |
| 0 | 退出 |

下载、修复和执行选项会改变外部状态。verify 当前也包含清理行为；auto 会执行实际删除与移动。

## 命令行使用

tv 注册成功后可从任意目录调用；也可在项目目录使用 ./tv-workflow.sh 加相同参数。

~~~bash
# 交互选择或定向初检
tv inspect -i
tv inspect --category 日韩动漫
tv inspect --show 示例剧名
tv inspect --path "/云盘/媒体库/动漫/示例剧名"

# 补缺下载或专项修复
tv download
tv repair

# 复核与归档预览
tv review
tv archive
~~~

需要明确输入报告时附加 --report "报告路径"。核对计划后，实际执行命令为：

~~~bash
tv review --report "报告.xlsx" --yes
tv archive --report "报告.xlsx" --yes
~~~

| 其他命令 | 行为 |
| --- | --- |
| tv verify | 归档对账及空别名清理 |
| tv auto | 初检、下载、实际清理、实际归档 |
| tv --help | 查看参数 |

## 报告与日志

reports 保存各阶段报告，logs 保存日志，data 保存持久记录。下载提交成功与实际文件入库是不同状态，复核应以实际文件为准。

目标规则、动漫差异和已有代码的审阅记录见[需求文档目录](../媒体整理需求/README.md)；其中的验收用例不表示当前 Python 版已经全部通过。

## 更新已安装版本

1. 记录实际安装路径；保留 config.json、data、reports 和 logs，先备份配置。
2. 如启用了后台服务，先停止服务。
3. 更新代码文件，保留服务器实际配置，不用示例配置覆盖它。
4. 在安装目录重新运行安装脚本，然后先做小范围初检。
5. 检查通过后，再启动需要的后台服务。

已有 systemd 服务时可用：

~~~bash
sudo systemctl stop tv-workflow.service
# 完成代码更新后，在项目目录执行：
sudo bash install.sh
sudo systemctl start tv-workflow.service
~~~

## 后台与开机启动

先完成手动检查，再考虑启用后台。项目提供 [systemd 示例](systemd/tv-workflow.service)，其默认安装路径是 /opt/tv-workflow-Python，并使用 /usr/bin/python3 启动。

安装前检查示例中的 User、WorkingDirectory、ExecStart 和日志路径。使用其他安装目录或项目 .venv 时，改为实际路径与 Python；为运行用户准备可写的日志目录。

确认配置后再安装并启用：

~~~bash
sudo cp systemd/tv-workflow.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tv-workflow.service
~~~

检查状态：

~~~bash
sudo systemctl status tv-workflow.service
sudo journalctl -u tv-workflow.service -f
~~~

定时是否执行由项目配置和当前实现共同决定。后台流程含实际操作，启用前核对源、目标与运行范围。

## 常见问题

| 现象 | 检查方法 |
| --- | --- |
| tv 找不到 | 在项目目录用 ./tv-workflow.sh；检查安装脚本是否成功注册命令 |
| Python 模块缺失 | 查看依赖安装结果，确认启动器使用的 Python 与安装依赖的环境一致 |
| 前台正常、systemd 失败 | 检查服务的工作目录、Python 路径、运行用户和日志权限 |
| 配置不存在或格式错误 | 检查 config.json 位置、UTF-8 编码、引号与逗号 |
| 连接失败或 401/403 | 核对服务器到服务的网络、地址、端口与令牌 |
| 没有找到剧集 | 检查源根和目录层级，先指定单剧路径 |
| 报告或日志无法写入 | 检查运行用户对项目目录及输出目录的写权限 |

## 工作流程

    初检 → 补缺下载与整理修复 → 复核清理 → 归档与对账

各阶段用报告衔接。页面/API 版本变化、编号与路径差异需要结合实际错误核实；本说明未替代真实 Linux 部署验证。
