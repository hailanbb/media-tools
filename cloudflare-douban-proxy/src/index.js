/**
 * MediaFlow Cloudflare Workers 豆瓣只读元数据反代
 *
 * 安全设计：
 * - 严格上游白名单：仅转发 suggest / rexxar / frodo / subject 四类只读影视元数据接口，绝不开放任意 URL 代理（零 SSRF）。
 * - 恒定时间密钥鉴权：配置 PROXY_TOKEN secret 后，强制校验 X-Proxy-Token 或 Authorization: Bearer（crypto.subtle.timingSafeEqual）。
 * - 流式透传：大响应体直接透传 ReadableStream，不无界缓冲；结构化 JSON 日志，绝不打印密钥或 Cookie。
 */

const FRODO_API_KEYS = [
  "0ac44ae016490db2204ce0a042db2916",
  "054022eaeae0b00e0fc068c0c0a2102a",
  "0dad551e10e6e7305943181816e87f16",
];

const BID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";

function generateRandomBid() {
  const bytes = new Uint8Array(11);
  crypto.getRandomValues(bytes);
  let out = "";
  for (let i = 0; i < bytes.length; i++) {
    out += BID_ALPHABET[bytes[i] % BID_ALPHABET.length];
  }
  return out;
}

function ensureBidInCookie(cookieHeader) {
  const cleaned = String(cookieHeader || "").trim().replace(/^cookie:\s*/i, "");
  if (!cleaned) {
    return `bid=${generateRandomBid()}`;
  }
  if (/(?:^|;\s*)bid=[^;\s]+/i.test(cleaned)) {
    return cleaned;
  }
  return `${cleaned}; bid=${generateRandomBid()}`;
}

async function verifySecretToken(provided, expected) {
  const encoder = new TextEncoder();
  const [providedHash, expectedHash] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(String(provided || ""))),
    crypto.subtle.digest("SHA-256", encoder.encode(String(expected || ""))),
  ]);
  if (typeof crypto.subtle.timingSafeEqual === "function") {
    return crypto.subtle.timingSafeEqual(providedHash, expectedHash);
  }
  const a = new Uint8Array(providedHash);
  const b = new Uint8Array(expectedHash);
  let diff = a.length ^ b.length;
  for (let i = 0; i < a.length; i++) {
    diff |= a[i] ^ b[i];
  }
  return diff === 0;
}

function extractRequestToken(request) {
  const headerToken = request.headers.get("X-Proxy-Token");
  if (headerToken) return headerToken.trim();
  const auth = request.headers.get("Authorization") || "";
  const m = auth.match(/^Bearer\s+(.+)$/i);
  return m ? m[1].trim() : "";
}

function jsonResponse(payload, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      ...extraHeaders,
    },
  });
}

function buildUpstreamHeaders(kind, subjectId, cookieHeader, extra = {}) {
  const cookie = ensureBidInCookie(cookieHeader);
  if (kind === "frodo") {
    return {
      "User-Agent": "api-client/1 com.douban.frodo/7.18.0 Android/29",
      "Referer": "https://servicewechat.com/wx2f9b06c1de1ccfca/91/page-frame.html",
      "Accept": "application/json",
      "Cookie": cookie,
      ...extra,
    };
  }
  if (kind === "rexxar" || kind === "rexxar_search") {
    const referer =
      kind === "rexxar_search"
        ? `https://m.douban.com/search/?query=${encodeURIComponent(subjectId || "")}&type=movie`
        : (subjectId ? `https://m.douban.com/movie/subject/${subjectId}/` : "https://movie.douban.com/");
    return {
      "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
      "Referer": referer,
      "Accept": "application/json, text/plain, */*",
      "X-Requested-With": "XMLHttpRequest",
      "Cookie": cookie,
      ...extra,
    };
  }
  return {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Referer": kind === "classic_search" ? "https://www.douban.com/search?cat=1002" : "https://movie.douban.com/",
    "Accept": kind === "suggest" || kind === "classic_search" ? "application/json, text/plain, */*" : "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cookie": cookie,
    ...extra,
  };
}

function decodeHtmlEntities(text) {
  return String(text || "")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, " ");
}

function normalizeRexxarSearchItems(payload) {
  const rawList = [];
  if (Array.isArray(payload)) {
    rawList.push(...payload);
  } else if (payload && typeof payload === "object") {
    if (Array.isArray(payload.subjects?.items)) rawList.push(...payload.subjects.items);
    if (Array.isArray(payload.subjects)) rawList.push(...payload.subjects);
    if (Array.isArray(payload.items)) rawList.push(...payload.items);
    if (Array.isArray(payload.data)) rawList.push(...payload.data);
  }
  const seen = new Set();
  const out = [];
  for (const entry of rawList) {
    if (!entry || typeof entry !== "object") continue;
    const item = entry.target && typeof entry.target === "object" ? entry.target : entry;
    const id = String(item.id || "").trim();
    if (!/^\d+$/.test(id) || seen.has(id)) continue;
    const title = String(item.title || item.name || "").trim();
    if (!title) continue;
    const yearMatch = String(item.year || item.card_subtitle || "").match(/\b((?:19|20)\d{2})\b/);
    const epRaw = item.episodes_count ?? item.episode ?? "";
    seen.add(id);
    out.push({
      id,
      title,
      sub_title: String(item.original_title || item.sub_title || "").trim(),
      year: yearMatch ? yearMatch[1] : "",
      type: String(item.type || entry.target_type || "").trim(),
      episode: /^\d+$/.test(String(epRaw)) ? String(epRaw) : "",
    });
  }
  return out;
}

function extractSubjectsFromHtml(htmlText) {
  const text = String(htmlText || "");
  const seen = new Set();
  const out = [];
  // 匹配豆瓣 link2 跳转链接与 /movie/subject/{id} 或 /subject/{id}
  const anchorRe = /<a\b[^>]*href=["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi;
  let m;
  while ((m = anchorRe.exec(text)) !== null) {
    const rawHref = decodeHtmlEntities(m[1] || "");
    let decodedHref = rawHref;
    try {
      decodedHref = decodeURIComponent(rawHref);
    } catch {
      decodedHref = rawHref;
    }
    const idMatch = decodedHref.match(/\/subject\/(\d+)/);
    if (!idMatch) continue;
    const id = idMatch[1];
    const cleanTitle = decodeHtmlEntities(String(m[2] || "").replace(/<[^>]+>/g, " "))
      .replace(/\s+/g, " ")
      .trim();
    if (!cleanTitle || seen.has(id)) continue;
    seen.add(id);
    out.push({
      id,
      title: cleanTitle,
      sub_title: "",
      year: "",
      type: "",
      episode: "",
    });
    if (out.length >= 25) break;
  }
  return out;
}

async function handleFullSearch(query, cookieHeader) {
  const attempts = [
    {
      name: "rexxar_search_movie",
      url: `https://m.douban.com/rexxar/api/v2/search/movie?q=${encodeURIComponent(query)}&start=0&count=20`,
      kind: "rexxar_search",
      mode: "rexxar_json",
    },
    {
      name: "rexxar_search",
      url: `https://m.douban.com/rexxar/api/v2/search?q=${encodeURIComponent(query)}&type=movie&start=0&count=20`,
      kind: "rexxar_search",
      mode: "rexxar_json",
    },
    {
      name: "classic_j_search",
      url: `https://www.douban.com/j/search?q=${encodeURIComponent(query)}&start=0&cat=1002`,
      kind: "classic_search",
      mode: "classic_json",
    },
    {
      name: "mobile_search_html",
      url: `https://m.douban.com/search/?query=${encodeURIComponent(query)}&type=movie`,
      kind: "rexxar_search",
      mode: "html",
    },
  ];

  let blockedStatus = 0;
  let lastChannel = "none";
  for (const step of attempts) {
    try {
      const resp = await fetch(step.url, {
        method: "GET",
        headers: buildUpstreamHeaders(step.kind, query, cookieHeader),
        redirect: "manual",
      });
      lastChannel = step.name;
      if (resp.status === 302 || resp.status === 403) {
        blockedStatus = resp.status;
        await resp.body?.cancel();
        continue;
      }
      if (!resp.ok) {
        await resp.body?.cancel();
        continue;
      }
      const text = await resp.text();
      let items = [];
      if (step.mode === "rexxar_json") {
        try {
          items = normalizeRexxarSearchItems(JSON.parse(text));
        } catch {
          items = [];
        }
      } else if (step.mode === "classic_json") {
        try {
          const parsed = JSON.parse(text);
          const joined = Array.isArray(parsed?.items) ? parsed.items.join("\n") : text;
          items = extractSubjectsFromHtml(joined);
        } catch {
          items = [];
        }
      } else {
        items = extractSubjectsFromHtml(text);
      }
      if (items.length > 0) {
        return jsonResponse(items, 200, {
          "Cache-Control": "public, max-age=300",
          "X-Douban-Upstream-Channel": step.name,
          "X-Douban-Upstream-Status": "200",
        });
      }
    } catch {
      continue;
    }
  }

  if (blockedStatus) {
    return jsonResponse(
      { ok: false, error: `Douban upstream blocked (${blockedStatus})` },
      blockedStatus,
      {
        "X-Douban-Upstream-Channel": lastChannel,
        "X-Douban-Upstream-Status": String(blockedStatus),
      }
    );
  }
  return jsonResponse([], 200, {
    "X-Douban-Upstream-Channel": lastChannel,
    "X-Douban-Upstream-Status": "200",
  });
}

function buildProxyResponse(upstreamResp, channelName) {
  const headers = new Headers();
  const ct = upstreamResp.headers.get("content-type") || "application/octet-stream";
  headers.set("Content-Type", ct);
  headers.set("Cache-Control", upstreamResp.ok ? "public, max-age=300" : "no-store");
  headers.set("X-Douban-Upstream-Channel", channelName);
  headers.set("X-Douban-Upstream-Status", String(upstreamResp.status));
  return new Response(upstreamResp.body, {
    status: upstreamResp.status,
    headers,
  });
}

async function fetchFrodoWithFallbackKeys(mediaKind, subjectId, cookieHeader) {
  let lastResp = null;
  for (const apiKey of FRODO_API_KEYS) {
    const targetUrl = `https://frodo.douban.com/api/v2/${mediaKind}/${subjectId}?apiKey=${encodeURIComponent(apiKey)}`;
    const resp = await fetch(targetUrl, {
      method: "GET",
      headers: buildUpstreamHeaders("frodo", subjectId, cookieHeader),
      redirect: "manual",
    });
    if (resp.ok) {
      return resp;
    }
    lastResp = resp;
    // 403 error code 004 表示账号/IP 要求登录，更换公用 key 也不会成功，直接结束重试
    if (resp.status === 403 || resp.status === 404) {
      break;
    }
    await resp.body?.cancel();
  }
  return lastResp;
}

async function handleProbe(cookieHeader) {
  const probeSubject = "25853071"; // 庆余年
  const checks = [
    {
      name: "suggest",
      url: "https://movie.douban.com/j/subject_suggest?q=%E5%BA%86%E4%BD%99%E5%B9%B4",
      kind: "suggest",
    },
    {
      name: "rexxar_tv",
      url: `https://m.douban.com/rexxar/api/v2/tv/${probeSubject}`,
      kind: "rexxar",
    },
    {
      name: "frodo_tv",
      url: `https://frodo.douban.com/api/v2/tv/${probeSubject}?apiKey=${FRODO_API_KEYS[0]}`,
      kind: "frodo",
    },
  ];

  const results = {};
  for (const c of checks) {
    const started = Date.now();
    try {
      const resp = await fetch(c.url, {
        method: "GET",
        headers: buildUpstreamHeaders(c.kind, probeSubject, cookieHeader),
        redirect: "manual",
      });
      let okJson = false;
      let sampleTitle = "";
      if (resp.ok) {
        const text = await resp.text();
        try {
          const parsed = JSON.parse(text);
          if (Array.isArray(parsed) && parsed.length > 0) {
            okJson = true;
            sampleTitle = String(parsed[0].title || "");
          } else if (parsed && typeof parsed === "object" && (parsed.title || parsed.id)) {
            okJson = true;
            sampleTitle = String(parsed.title || "");
          }
        } catch {
          okJson = false;
        }
      } else {
        await resp.body?.cancel();
      }
      results[c.name] = {
        ok: resp.ok && okJson,
        http_status: resp.status,
        latency_ms: Date.now() - started,
        sample_title: sampleTitle,
      };
    } catch (err) {
      results[c.name] = {
        ok: false,
        http_status: 0,
        latency_ms: Date.now() - started,
        error: err instanceof Error ? err.message.slice(0, 120) : "fetch_failed",
      };
    }
  }
  const anyOk = Object.values(results).some((r) => r.ok);
  return jsonResponse({ ok: anyOk, results }, anyOk ? 200 : 502);
}

export async function handleRequest(request, env) {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/+$/, "") || "/";

  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204 });
  }
  if (request.method !== "GET") {
    return jsonResponse({ ok: false, error: "Method Not Allowed" }, 405);
  }

  // 轻量健康端点不暴露任何凭据
  if (path === "/" || path === "/health") {
    return jsonResponse({
      ok: true,
      service: "mediaflow-douban-proxy",
      auth_required: Boolean(env && env.PROXY_TOKEN),
      has_server_cookie: Boolean(env && env.DOUBAN_COOKIE),
    });
  }

  // 若配置了 PROXY_TOKEN，除 /health 外全部路由强制恒定时间校验
  const expectedToken = env && env.PROXY_TOKEN ? String(env.PROXY_TOKEN).trim() : "";
  if (expectedToken) {
    const provided = extractRequestToken(request);
    if (!provided || !(await verifySecretToken(provided, expectedToken))) {
      return jsonResponse({ ok: false, error: "Unauthorized" }, 401);
    }
  }

  const reqCookie = request.headers.get("X-Douban-Cookie") || (env && env.DOUBAN_COOKIE) || "";

  if (path === "/probe") {
    return handleProbe(reqCookie);
  }

  if (path === "/search") {
    const q = (url.searchParams.get("q") || "").trim();
    if (!q || q.length > 120) {
      return jsonResponse({ ok: false, error: "Missing or invalid query parameter q" }, 400);
    }
    return handleFullSearch(q, reqCookie);
  }

  if (path === "/suggest") {
    const q = (url.searchParams.get("q") || "").trim();
    if (!q || q.length > 120) {
      return jsonResponse({ ok: false, error: "Missing or invalid query parameter q" }, 400);
    }
    const targetUrl = `https://movie.douban.com/j/subject_suggest?q=${encodeURIComponent(q)}`;
    const resp = await fetch(targetUrl, {
      method: "GET",
      headers: buildUpstreamHeaders("suggest", "", reqCookie),
      redirect: "manual",
    });
    return buildProxyResponse(resp, "suggest");
  }

  const rexxarMatch = path.match(/^\/rexxar\/(tv|movie)\/(\d+)$/);
  if (rexxarMatch) {
    const [, mediaKind, subjectId] = rexxarMatch;
    const targetUrl = `https://m.douban.com/rexxar/api/v2/${mediaKind}/${subjectId}`;
    const resp = await fetch(targetUrl, {
      method: "GET",
      headers: buildUpstreamHeaders("rexxar", subjectId, reqCookie),
      redirect: "manual",
    });
    return buildProxyResponse(resp, `rexxar_${mediaKind}`);
  }

  const frodoMatch = path.match(/^\/frodo\/(tv|movie)\/(\d+)$/);
  if (frodoMatch) {
    const [, mediaKind, subjectId] = frodoMatch;
    const resp = await fetchFrodoWithFallbackKeys(mediaKind, subjectId, reqCookie);
    return buildProxyResponse(resp, `frodo_${mediaKind}`);
  }

  const subjectMatch = path.match(/^\/subject\/(\d+)$/);
  if (subjectMatch) {
    const [, subjectId] = subjectMatch;
    const targetUrl = `https://movie.douban.com/subject/${subjectId}/`;
    const resp = await fetch(targetUrl, {
      method: "GET",
      headers: buildUpstreamHeaders("subject", subjectId, reqCookie),
      redirect: "manual",
    });
    return buildProxyResponse(resp, "subject");
  }

  return jsonResponse({ ok: false, error: "Not Found" }, 404);
}

export default {
  async fetch(request, env, _ctx) {
    try {
      const resp = await handleRequest(request, env);
      const url = new URL(request.url);
      console.log(JSON.stringify({
        event: "douban_proxy_request",
        path: url.pathname,
        status: resp.status,
      }));
      return resp;
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown error";
      console.error(JSON.stringify({
        event: "douban_proxy_error",
        path: new URL(request.url).pathname,
        error: message.slice(0, 200),
      }));
      return jsonResponse({ ok: false, error: "Upstream proxy error" }, 502);
    }
  },
};
