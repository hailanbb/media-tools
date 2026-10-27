# ④ 剧集归档 · 安装

[项目首页](../../README.md) · [需求](requirements.md) · [使用](usage.md)

1. 先完成[通用安装准备](../../INSTALL.md)，登录自己的 CloudDrive2。
2. 打开[本阶段安装脚本](../../season-archive.user.js)，点击 **Raw**，复制完整代码。
3. 在 Tampermonkey 中新建脚本，删除默认模板并粘贴。升级时编辑同阶段旧脚本，不要再建一份。
4. 修改文件开头的站点匹配行，把示例主机替换为自己的主机：

```javascript
// @match        https://cloud.your-domain.com/*
```

5. 修改顶部配置区（以下地址仅为示例），保留源目录下面“分类 / 剧集 / Season N”的层级：

```javascript
const ORIGIN = 'https://cloud.your-domain.com:8920';
const SOURCE = '/115/Library_incs/nas/电视剧';
const DEST = '/115/Library_incs/nas.finish/电视剧';
```

这是现有配置行的填写示例，**修改原行，不要再插入同名变量**。`@match` 不带端口；站点常量有端口必须保留。四阶段的云盘地址与 SOURCE 应相同。

6. 按 **Ctrl+S** 保存，刷新 CloudDrive2。出现“④ 剧集归档”对应面板后，按[使用说明](usage.md)操作；安装成功不会自动执行。

DEST 及其分类目录应提前建立；SOURCE 和 DEST 不能相同、互相包含或填写云盘根目录。先检查清单，确认后才移动。
