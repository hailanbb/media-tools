# MediaFlow 版本管理

当前应用版本为 v2.5.8，发布日期为 2026-11-10。

- 应用版本以 app/config.py 的 VERSION 为准，首页静态版本标记同时更新，运行时从健康接口读取版本。
- 每次发布先更新 README 版本历史，再提交源码、文档和测试，并创建对应的附注标签。
- GitHub 主分支维护 mediaflow-win 和 mediaflow-docker 两份共用程序，两份的 app、frontend 和 tests 内容应保持一致。
- .env、数据库、日志与探针临时目录属于运行数据。程序升级前保存数据库备份，已有数据库中的人工处理标记和条目绑定随程序配置一起管理。
- 本机开发仓库位于 E:/GoogleAI/emby/mediaflow，GitHub 完整仓库克隆位于 E:/GoogleAI/emby/media-tools。开发仓库的发布标签与 GitHub 标签使用同一版本号，两者提交哈希因目录结构不同而各自独立。
- 回看旧版本使用 git show v2.5.8，切换版本前先保存当前改动。发布代码后重启本地服务才能载入新的后端程序。

本次验证在 Windows Python 3.11 环境执行离线套件。Docker 镜像与真实网盘删除的端到端验证尚未执行。
