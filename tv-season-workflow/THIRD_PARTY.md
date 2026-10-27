# 来源与第三方许可

本目录是仓库维护的剧集工作流四个阶段的发布副本，不是 MoviePilot、CloudDrive2、TMDB 或豆瓣的官方产品。

| 依赖 | 版本 | 来源 | 保留的许可文件 |
| --- | --- | --- | --- |
| OpenCC-js | 1.4.2 | https://github.com/nk2028/opencc-js | [LICENSE](vendor/package/LICENSE)、[第三方声明](vendor/package/THIRD_PARTY_LICENSES.md)、[Apache-2.0](vendor/package/LICENSES/Apache-2.0.txt) |
| xlsx-js-style | 1.2.0 | https://github.com/gitbrent/xlsx-js-style | [LICENSE](vendor/xlsx-js-style/package/LICENSE)、[分发许可](vendor/xlsx-js-style/package/dist/LICENSE) |
| @xmldom/xmldom | 见 package.json 的固定版本 | https://github.com/xmldom/xmldom | 仅测试依赖，安装脚本不依赖该包；以 npm 分发许可为准。 |

OpenCC 和 Excel 库及其许可已嵌入安装脚本；普通用户不必另装依赖。第三方许可不能被推定为本项目全部代码的统一许可，本次不新增对自有代码的授权声明。

发布内容：四个安装脚本、维护模块、构建脚本、回归测试、必要依赖代码/许可和分阶段需求、安装、使用文档。未包含个人报告、站点凭据、运行缓存、浏览器数据或原始会话。

公开副本将个人站点替换为 `.example` 示例地址，配置常量前置；业务判断来自已验证本地版本。文件摘要见 [snapshot-manifest.json](snapshot-manifest.json)。
