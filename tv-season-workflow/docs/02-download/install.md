# ② 补缺下载 · 安装

[项目首页](../../README.md) · [需求](requirements.md) · [使用](usage.md)

1. 先完成[通用安装准备](../../INSTALL.md)，登录自己的 MoviePilot。
2. 打开[本阶段安装脚本](../../season-download.user.js)，点击 **Raw**，复制完整代码。
3. 在 Tampermonkey 中新建脚本，删除默认模板并粘贴。升级时编辑同阶段旧脚本，不要再建一份。
4. 修改文件开头的站点匹配行，把示例主机替换为自己的主机：

```javascript
// @match        https://mp.your-domain.com/*
```

5. 修改顶部配置区（以下地址仅为示例），保留源目录下面“分类 / 剧集 / Season N”的层级：

```javascript
const ORIGIN = 'https://mp.your-domain.com:8920';
const CLOUD = 'https://cloud.your-domain.com:8920';
const SOURCE = '/115/Library_incs/nas/电视剧';
```

这是现有配置行的填写示例，**修改原行，不要再插入同名变量**。`@match` 不带端口；站点常量有端口必须保留。四阶段的云盘地址与 SOURCE 应相同。

6. 按 **Ctrl+S** 保存，刷新 MoviePilot。出现“② 补缺下载”对应面板后，按[使用说明](usage.md)操作；安装成功不会自动执行。

先在 MoviePilot 中配置站点、下载器和整理规则，确保手动搜索与下载可用。脚本使用当前登录页面，不会替你配置站点。


使用“整理修复”前，还需检查第二脚本顶部 `REPAIR_CONFIG.source` 和 `REPAIR_CONFIG.target`。这是 MoviePilot 文件管理中的 local 路径，详见[使用说明](usage.md#下载好了但网盘仍缺集整理修复)。其他版本暂不执行整理修复。
