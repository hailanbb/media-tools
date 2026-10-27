# ① 剧集初检 · 安装

[项目首页](../../README.md) · [需求](requirements.md) · [使用](usage.md)

1. 先完成[通用安装准备](../../INSTALL.md)，登录自己的 CloudDrive2。
2. 打开[本阶段安装脚本](../../season-inspect.user.js)，点击 **Raw**，复制完整代码。
3. 在 Tampermonkey 中新建脚本，删除默认模板并粘贴。升级时编辑同阶段旧脚本，不要再建一份。
4. 修改文件开头的站点匹配行，把示例主机替换为自己的主机：

```javascript
// @match        https://cloud.your-domain.com/*
```

5. 修改顶部配置区（以下地址仅为示例），保留源目录下面“分类 / 剧集 / Season N”的层级：

```javascript
const CLOUD_ORIGIN = 'https://cloud.your-domain.com:8920';
const SOURCE = '/115/Library_incs/nas/电视剧';
```

这是现有配置行的填写示例，**修改原行，不要再插入同名变量**。`@match` 不带端口；站点常量有端口必须保留。四阶段的云盘地址与 SOURCE 应相同。豆瓣相关匹配行保持不变。

6. 按 **Ctrl+S** 保存，刷新 CloudDrive2。出现“① 剧集初检”对应面板后，按[使用说明](usage.md)操作；安装成功不会自动执行。

在面板中填写自己的 TMDB 凭据并保存；不要把个人凭据写进公开仓库。豆瓣需要时由面板打开页面，手动完成登录或验证。
