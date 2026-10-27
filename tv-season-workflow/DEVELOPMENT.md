# 构建与测试

[项目首页](README.md)

普通用户只安装根目录四个 `.user.js`。以下仅供维护者使用。

## 本地维护

需要 Node.js 20 或更新版本。在本目录执行：

```sh
npm install
npm run build
npm test
```

先改 src，再构建安装文件；直接修改生成文件会在重建时丢失。源码、中文文档采用 UTF-8 无 BOM、LF；第三方文件保持分发内容及许可。

## 模块

| 文件 | 职责 |
| --- | --- |
| src/season-inspect.js | ① 初检、来源判断、缓存和豆瓣补查 |
| src/version-download.js | 复用第三阶段模板分组，为混版目录选择同版本补缺资源 |
| src/season-download.js | ② 资源选择、提交与防重复 |
| src/season-review.js / cleanup-core.js / cloud-review-client.js | ③ 面板、清理计划与执行、云盘及豆瓣页面适配 |
| src/season-archive.js / archive-core.js / cloud-archive-client.js | ④ 面板、移动计划与核实、云盘移动适配 |
| src/workflow-report.js | Excel 导入、保留原内容、颜色与操作明细 |
| src/panel-layout.js | 四阶段拖动、右下角收起、位置恢复 |
| src/douban-policy.js / inspect-navigation.js | 初检豆瓣访问策略、有限导航重试 |
| build.cjs / vendor / test | 打包、离线依赖及回归验证 |

构建提取共用只读逻辑并嵌入各阶段独立安装文件。公开版站点使用 .example；每个入口顶部集中配置地址和路径。第三、第四阶段适配层接收配置，归档源/目标必须独立；报告链接核验也使用相同配置。

## 验证边界

测试覆盖集号与多语言匹配、来源切换、豆瓣预算、资源排序、防重复、报告保留、单完整版本清理、同季跳过、操作中断恢复及面板交互。清理/移动测试使用模拟目录和页面，不修改真实云盘；下载测试不提交真实任务。

发布前应检查：构建可重现、四个安装文件语法、测试、相对文档链接、中文编码、示例配置、个人地址排除及差异范围。文件摘要见 [snapshot-manifest.json](snapshot-manifest.json)，版本和实际验证记录见 [CHANGELOG.md](CHANGELOG.md)。不同部署的页面与接口变化仍可能需要适配。
