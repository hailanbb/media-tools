/**
 * TMDB (The Movie Database) API 客户端
 * 封装电视剧详情、季详情、搜索接口，内置请求缓存与限流重试
 * @module clients/tmdb
 */

/** @type {string} TMDB API 基础地址 */
const BASE_URL = 'https://api.themoviedb.org';

/** @type {number} 缓存过期时间（毫秒）：1 小时 */
const CACHE_TTL = 60 * 60 * 1000;

export class TMDBClient {
  /** @type {string} TMDB API 密钥 */
  #apiKey;
  /** @type {string} 语言参数，如 zh-CN */
  #language;
  /** @type {object|null} 日志器 */
  #logger;
  /** @type {Map<string, {data: any, ts: number}>} 请求缓存 */
  #cache = new Map();

  /**
   * @param {object} opts
   * @param {string} opts.apiKey   - TMDB API 密钥（v4 Read Access Token 或 v3 api_key）
   * @param {string} [opts.language='zh-CN'] - 返回数据语言
   * @param {object} [opts.logger]
   */
  constructor({ apiKey, language = 'zh-CN', logger = null }) {
    this.#apiKey = apiKey;
    this.#language = language;
    this.#logger = logger;
  }

  /** @returns {string} */
  get apiKey() {
    return this.#apiKey;
  }

  /* ------------------------------------------------------------------ */
  /*  公开方法                                                           */
  /* ------------------------------------------------------------------ */

  /**
   * 获取电视剧详情
   * @param {number|string} tmdbId - TMDB 剧集 ID
   * @returns {Promise<object>}
   */
  async getShowDetails(tmdbId) {
    return this.#request(`/3/tv/${tmdbId}`, { language: this.#language });
  }

  /**
   * 获取电视剧指定季详情（含各集信息）
   * @param {number|string} tmdbId - TMDB 剧集 ID
   * @param {number|string} season - 季号
   * @returns {Promise<object>}
   */
  async getSeasonDetails(tmdbId, season) {
    return this.#request(`/3/tv/${tmdbId}/season/${season}`, { language: this.#language });
  }

  /**
   * 搜索电视剧
   * @param {string} title - 搜索关键词
   * @param {number|string|null} [year] - 首播年份
   * @returns {Promise<object>} 搜索结果（含 results 数组）
   */
  async searchShow(title, year = null) {
    const params = { query: title, language: this.#language };
    if (year !== null && year !== undefined) {
      params.first_air_date_year = String(year);
    }
    return this.#request('/3/search/tv', params);
  }

  /* ------------------------------------------------------------------ */
  /*  私有方法                                                           */
  /* ------------------------------------------------------------------ */

  /**
   * 统一请求处理器
   * - Bearer 令牌认证
   * - 简单内存缓存（1 小时 TTL）
   * - 429 限流指数退避重试（最多 3 次）
   *
   * @param {string} path    - API 路径
   * @param {object} [params] - 查询参数
   * @param {number} [timeout=15000]
   * @returns {Promise<any>}
   */
  async #request(path, params = {}, timeout = 15_000) {
    const url = new URL(path, BASE_URL);
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
    }

    const headers = {
      'Content-Type': 'application/json',
    };

    if (this.#apiKey) {
      if (this.#apiKey.startsWith('ey') || this.#apiKey.length > 50) {
        // TMDB v4 Read Access Token (JWT)
        headers.Authorization = `Bearer ${this.#apiKey}`;
      } else {
        // TMDB v3 API Key (32位十六进制字符串)
        url.searchParams.set('api_key', this.#apiKey);
      }
    }

    // 缓存命中检查
    const cacheKey = url.toString();
    const cached = this.#cache.get(cacheKey);
    if (cached && Date.now() - cached.ts < CACHE_TTL) {
      this.#log('info', `[TMDB] 缓存命中: ${path}`);
      return cached.data;
    }

    const maxRetries = 3;
    let lastError;

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      try {
        this.#log('info', `[TMDB] GET ${url.pathname}${url.search}`);

        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeout);

        const res = await fetch(url.toString(), {
          method: 'GET',
          headers,
          signal: controller.signal,
        });
        clearTimeout(timer);

        // 429 限流 → 指数退避
        if (res.status === 429) {
          const retryAfter = res.headers.get('Retry-After');
          const delay = retryAfter ? Number(retryAfter) * 1000 : Math.min(1000 * 2 ** attempt, 16_000);
          this.#log('warn', `[TMDB] 请求限流 (429)，${delay}ms 后重试 (${attempt + 1}/${maxRetries})`);
          if (attempt < maxRetries) {
            await this.#sleep(delay);
            continue;
          }
        }

        const text = await res.text();
        let data;
        try {
          data = JSON.parse(text);
        } catch {
          data = text;
        }

        if (!res.ok) {
          const msg = typeof data === 'object' ? (data.status_message ?? JSON.stringify(data)) : data;
          throw new Error(`TMDB 请求失败 [${res.status}]: ${msg}`);
        }

        // 写入缓存
        this.#cache.set(cacheKey, { data, ts: Date.now() });
        // 定期清理过期缓存，防止内存泄漏
        if (this.#cache.size > 500) this.#pruneCache();

        this.#log('info', `[TMDB] 响应 ${res.status}`);
        return data;
      } catch (err) {
        lastError = err;
        if (err.name === 'AbortError') {
          throw new Error(`TMDB 请求超时 (${timeout}ms): ${path}`);
        }
        if (!String(err.message).includes('429') && attempt > 0) {
          throw err;
        }
      }
    }

    throw lastError;
  }

  /**
   * 清理过期缓存条目
   */
  #pruneCache() {
    const now = Date.now();
    for (const [key, entry] of this.#cache) {
      if (now - entry.ts >= CACHE_TTL) {
        this.#cache.delete(key);
      }
    }
  }

  /**
   * 安全日志输出
   * @param {'info'|'warn'|'error'} level
   * @param  {...any} args
   */
  #log(level, ...args) {
    if (this.#logger && typeof this.#logger[level] === 'function') {
      this.#logger[level](...args);
    }
  }

  /**
   * 延迟辅助
   * @param {number} ms
   * @returns {Promise<void>}
   */
  #sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
}
