# 媒体脚本迁移记录

日期：2026-10-27

当前状态：油猴版保留于 [tv-season-workflow](tv-season-workflow/README.md)；订阅批量取消脚本已按后续要求移除。以下前几节记录首次迁移时的范围与验证，后续调整见文末。

来源：[hailanbb/skill-selection](https://github.com/hailanbb/skill-selection/tree/155bd1e9b5640808abce797d2de8eafd6a44b0ba)，提交 155bd1e9b5640808abce797d2de8eafd6a44b0ba。

## 迁移范围

| 源目录 | 新目录 | 文件数 |
| --- | --- | --- |
| emby-tools/tv-season-workflow | tv-season-workflow | 77 |
| emby-tools/moviepilot-unsubscribe | moviepilot-unsubscribe | 14 |

保留所有源码、生成的安装脚本、文档、测试、离线依赖和许可。仅调整两个项目 README 的仓库导航；四阶段项目的 snapshot-manifest.json 同步 README 摘要。业务文件及脚本版本不变。

## 源仓库清理

迁移验证后从源仓库移除上述两个目录；另按用户要求删除 tv-season-linux、tv-season-workflow-backend 及其两份 GitHub 检查流程。Win11 版保留。此次为普通提交，不重写历史。

## 验证

- 四阶段工作流：228 项模拟测试通过，失败 0 项。
- 订阅脚本：maintenance-core、maintenance-runtime、maintenance-worker、zero、test 五组模拟测试通过。
- 两项目重新构建后的 5 个安装脚本与原版逐字节一致。
- 91 个迁移文件中仅两个 README 导航与一份 README 摘要记录变化，其余 88 个文件保持原字节。
- 306 个相对文档链接检查通过；目标仓库既有需求文档和同期新增的 Win11/Python 项目保持原字节，首页合并增加工具入口。
- 全部第三方许可随项目保留；原审阅来源的固定提交链接保留，仍可通过历史查阅。

这些检查不包含真实下载、取消订阅、云盘删除或移动。迁移在目标仓库核验完成后，才清理源仓库当前分支；Git 历史保留。

## 同期更新保留

迁移期间目标仓库新增提交 99be9a20529ad2128a6e35bd668d9421cc1cbcd8；本次以该提交为父版本，完整保留 Win11/Python 版本和原有需求文档。首页仅增加迁入工具入口与目录项，并把需求文档的“生产环境已验证”表述还原为实际验证边界。

## 后续仓库整理（2026-10-27）

- 确认油猴版的 77 个迁移文件仍完整保留，业务文件与安装脚本未改动。
- 按用户要求移除 media-tools 当前分支的 moviepilot-unsubscribe 目录（14 个文件）。
- 从 skill-selection 当前分支移除“媒体工具”栏目及剩余 emby-tools 目录（含旧 Win11 副本），同步清理首页和脚本索引中的旧入口。Windows 版继续在 media-tools 维护。
- 首页精简为三个项目及需求文档入口；安装、配置、命令等说明放在各项目 README，采用清晰的安装与使用说明。
- 均使用普通提交，保留历史；此次不涉及真实媒体文件、下载任务或订阅操作。
