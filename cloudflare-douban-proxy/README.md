# MediaFlow Cloudflare Workers 豆瓣 API 反代 (`douban.ubaby.top`)

用于为 MediaFlow 提供独立、受保护的豆瓣只读元数据中继通道，在 MoviePilot 内置豆瓣通道异常时自动降级承接，恢复后自动回切。

## 支持端点（严格上游白名单，零开放 SSRF）

| 路由 | 上游目标 | 鉴权 |
| :--- | :--- | :--- |
| `GET /health` | Worker 自身存活状态 | 无 |
| `GET /probe` | 联检 `suggest` + `rexxar` + `frodo`（测试条目 `庆余年`） | `X-Proxy-Token` |
| `GET /suggest?q=<片名>` | `movie.douban.com/j/subject_suggest` | `X-Proxy-Token` |
| `GET /rexxar/{tv\|movie}/{id}` | `m.douban.com/rexxar/api/v2/{tv\|movie}/{id}` | `X-Proxy-Token` |
| `GET /frodo/{tv\|movie}/{id}` | `frodo.douban.com/api/v2/{tv\|movie}/{id}` | `X-Proxy-Token` |
| `GET /subject/{id}` | `movie.douban.com/subject/{id}/` | `X-Proxy-Token` |

## 部署到 `douban.ubaby.top`

可通过 Wrangler CLI 或 Cloudflare API（含官方 Cloudflare 插件 MCP）部署：

1. 登录 Cloudflare（如通过 Wrangler CLI 部署）：
   ```powershell
   cmd /c wrangler login
   ```
2. 部署 Worker 并自动绑定自定义域名 `douban.ubaby.top`（`custom_domain: true` 自动签发 DNS 与 HTTPS 证书）：
   ```powershell
   Set-Location E:\GoogleAI\emby\mediaflow\cloudflare-douban-proxy
   cmd /c wrangler deploy
   ```
3. 设置访问密钥（强烈建议，防外部滥用消耗豆瓣配额）：
   ```powershell
   cmd /c wrangler secret put PROXY_TOKEN
   ```
   如需在边缘侧兜底默认豆瓣 Cookie（也可改为在 MediaFlow 网页端通过 Cookie-Editor 配置并透传 `X-Douban-Cookie`）：
   ```powershell
   cmd /c wrangler secret put DOUBAN_COOKIE
   ```
4. 验证生效：
   ```powershell
   curl https://douban.ubaby.top/health
   ```
