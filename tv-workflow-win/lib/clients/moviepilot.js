/**
 * MoviePilot REST API 客户端
 * 封装媒体搜索、剧集信息查询、下载任务管理
 * @module clients/moviepilot
 */

export class MoviePilotClient {
  /** @type {string} 服务地址 */
  #origin;
  /** @type {string} 认证令牌（Bearer 或 api_key） */
  #token;
  /** @type {object|null} 日志器 */
  #logger;
  /** @type {'bearer'|'apikey'|null} 认证方式，自动探测后缓存 */
  #authMode = null;
  /** @type {Map<string, number>|null} 站点名称到 ID 的映射缓存 */
  #siteMapCache = null;

  /**
   * @param {object} opts
   * @param {string} opts.origin - 服务地址
   * @param {string} opts.token  - 认证令牌
   * @param {object} [opts.logger]
   */
  constructor({ origin, token, logger = null }) {
    this.#origin = origin.replace(/\/+$/, '');
    this.#token = token;
    this.#logger = logger;
  }

  /* ------------------------------------------------------------------ */
  /*  公开方法                                                           */
  /* ------------------------------------------------------------------ */

  /**
   * 获取所有站点列表
   * @returns {Promise<Array<object>>}
   */
  async getSites() {
    const res = await this.#request('GET', '/api/v1/site/');
    return Array.isArray(res) ? res : (res?.data ?? []);
  }

  /**
   * 将站点名称列表解析为站点 ID 列表（利用本地缓存）
   * @param {Array<string|number>} siteNames
   * @returns {Promise<Array<number>>}
   */
  async resolveSiteIds(siteNames = []) {
    if (!siteNames || !siteNames.length) return [];
    if (!this.#siteMapCache) {
      try {
        const sites = await this.getSites();
        this.#siteMapCache = new Map(sites.map(s => [s.name, s.id]));
      } catch (e) {
        this.#log('warn', `[MoviePilot] 获取站点列表失败: ${e.message}`);
        return [];
      }
    }
    return siteNames
      .map(name => {
        if (typeof name === 'number') return name;
        return this.#siteMapCache.get(name);
      })
      .filter(Boolean);
  }

  /**
   * 搜索媒体资源
   * - 有 tmdbId 时走精确搜索 /search/media/tmdb:{id}
   * - 无 tmdbId 时走关键词搜索 /search/title
   * 支持指定 sites 参数以限定特定站点搜索
   * 自动分页（最多 10 页）并去重
   *
   * @param {number|string|null} tmdbId  - TMDB ID
   * @param {number|string|null} season  - 季号
   * @param {string|null} title          - 标题
   * @param {number|string|null} year    - 年份
   * @param {object} [options]           - { sites?: string|number[] }
   * @returns {Promise<Array<object>>} 资源上下文数组
   */
  async searchMedia(tmdbId, season, title, year, options = {}) {
    if (tmdbId) {
      return this.#searchByTmdbId(tmdbId, season, title, year, options);
    }
    return this.#searchByTitle(title, season, options);
  }

  /**
   * 获取媒体详情
   * @param {number|string} tmdbId
   * @returns {Promise<object>}
   */
  async getMediaInfo(tmdbId) {
    return this.#request('GET', `/api/v1/media/tmdb:${tmdbId}`, { type_name: '电视剧' });
  }

  /**
   * 获取指定季的剧集列表
   * @param {number|string} tmdbId
   * @param {number|string} season
   * @returns {Promise<object>}
   */
  async getSeasonEpisodes(tmdbId, season) {
    return this.#request('GET', `/api/v1/tmdb/${tmdbId}/${season}`);
  }

  /**
   * 获取当前活跃下载任务
   * @returns {Promise<Array<object>>}
   */
  async getActiveDownloads() {
    return this.#request('GET', '/api/v1/download/');
  }

  /**
   * 提交下载任务
   * @param {object} mediaInfo   - 媒体信息对象
   * @param {object} torrentInfo - 种子信息对象
   * @returns {Promise<object>}
   */
  async submitDownload(mediaInfo, torrentInfo) {
    const body = {
      media_in: mediaInfo,
      torrent_in: torrentInfo,
      downloader: null,
      save_path: null,
    };
    return this.#request('POST', '/api/v1/download/', null, body);
  }

  /* ------------------------------------------------------------------ */
  /*  整理修复相关方法 (MoviePilot v2.15.6)                             */
  /* ------------------------------------------------------------------ */

  /**
   * 获取系统全局信息（包含版本号）
   * @returns {Promise<object>}
   */
  async getSystemGlobal() {
    try {
      const res = await this.#request('GET', '/api/v1/system/env');
      const env = res?.data ?? res;
      return {
        data: {
          BACKEND_VERSION: env?.VERSION || env?.APP_VERSION || 'v2.15.6',
          ...env
        }
      };
    } catch {
      return { data: { BACKEND_VERSION: 'v2.15.6' } };
    }
  }

  /**
   * 获取转移队列中的任务
   * @returns {Promise<Array<object>>}
   */
  async getTransferQueue() {
    return this.#request('GET', '/api/v1/transfer/queue');
  }

  /**
   * 查询指定标题的转移历史
   * @param {string} title - 剧集标题关键词
   * @param {number} [page=1]
   * @param {number} [count=100]
   * @returns {Promise<object>}
   */
  async getTransferHistories(title, page = 1, count = 100) {
    return this.#request('GET', '/api/v1/history/transfer', { title, page, count });
  }

  /**
   * 删除指定的转移历史记录（注意：固定 deletesrc=false, deletedest=false 绝不删除实际文件）
   * @param {number} id - 历史记录 ID
   * @returns {Promise<object>}
   */
  async deleteTransferHistory(id) {
    return this.#request('DELETE', '/api/v1/history/transfer', { deletesrc: false, deletedest: false }, { id });
  }

  /**
   * 预览手动转移操作
   * @param {object} req - 转移请求参数
   * @returns {Promise<object>}
   */
  async previewTransfer(req) {
    return this.#request('POST', '/api/v1/transfer/manual', null, { ...req, preview: true });
  }

  /**
   * 执行手动转移
   * @param {object} req - 转移请求参数
   * @returns {Promise<object>}
   */
  async executeManualTransfer(req) {
    return this.#request('POST', '/api/v1/transfer/manual', null, { ...req, preview: false });
  }

  /**
   * 查询存储路径下的文件列表
   * @param {string} path - 目录路径
   * @param {string} [storage='local']
   * @returns {Promise<Array<object>>}
   */
  async listStorage(path, storage = 'local') {
    const res = await this.#request('POST', '/api/v1/storage/list', null, { storage, path, type: 'dir' });
    return Array.isArray(res) ? res : (res?.data ?? []);
  }

  /* ------------------------------------------------------------------ */
  /*  私有搜索实现                                                       */
  /* ------------------------------------------------------------------ */

  /**
   * 按 TMDB ID 精确搜索
   * @param {number|string} tmdbId
   * @param {number|string|null} season
   * @param {string|null} title
   * @param {number|string|null} year
   * @param {object} [options] - { sites?: string|number[] }
   * @returns {Promise<Array<object>>}
   */
  async #searchByTmdbId(tmdbId, season, title, year, { sites } = {}) {
    const params = { mtype: '电视剧' };
    if (season !== null && season !== undefined) params.season = season;
    if (title) params.title = title;
    if (year) params.year = year;
    if (sites) {
      params.sites = Array.isArray(sites) ? sites.join(',') : String(sites);
    }

    const res = await this.#request('GET', `/api/v1/search/media/tmdb:${tmdbId}`, params);
    const data = res?.data ?? res;
    return Array.isArray(data) ? data : (data?.items ?? data?.results ?? []);
  }

  /**
   * 按标题关键词搜索，自动分页去重
   * @param {string} title
   * @param {number|string|null} season
   * @param {object} [options] - { sites?: string|number[] }
   * @returns {Promise<Array<object>>}
   */
  async #searchByTitle(title, season, { sites } = {}) {
    // 构建搜索关键词，带季号后缀
    const seasonStr = season != null ? ` S${String(season).padStart(2, '0')}` : '';
    const keyword = `${title}${seasonStr}`;

    const results = [];
    /** @type {Set<string>} 用于去重的标识集合 */
    const seen = new Set();
    const maxPages = 10;

    for (let page = 1; page <= maxPages; page++) {
      const params = { keyword, page };
      if (sites) {
        params.sites = Array.isArray(sites) ? sites.join(',') : String(sites);
      }
      let res;
      try {
        res = await this.#request('GET', '/api/v1/search/title', params);
      } catch (err) {
        this.#log('warn', `[MoviePilot] 搜索第 ${page} 页失败: ${err.message}`);
        break;
      }

      const data = res?.data ?? res;
      const items = Array.isArray(data) ? data : (data?.items ?? data?.results ?? []);
      if (!items.length) break;

      for (const item of items) {
        // 以 enclosure/magnet/hash 作为去重键
        const key = item.enclosure ?? item.magnet ?? item.hash ?? JSON.stringify(item);
        if (!seen.has(key)) {
          seen.add(key);
          results.push(item);
        }
      }

      // 如果返回数量少于一页预期量，说明没有更多数据
      if (items.length < 20) break;
    }

    return results;
  }

  /* ------------------------------------------------------------------ */
  /*  统一请求处理器                                                     */
  /* ------------------------------------------------------------------ */

  /**
   * 发送 HTTP 请求
   * - 自动支持 X-API-KEY 或 Bearer 认证
   * - 401/403 抛出可读错误
   *
   * @param {'GET'|'POST'|'PUT'|'DELETE'} method
   * @param {string} path       - 请求路径
   * @param {object|null} params - 查询参数
   * @param {object|null} body   - 请求体
   * @param {number} [timeout=30000]
   * @returns {Promise<any>}
   */
  async #request(method, path, params = null, body = null, timeout = 30_000) {
    const url = new URL(path, this.#origin);

    // 查询参数
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
      }
    }

    // 认证方式处理：如果 token 是 JWT（以 ey 开头），则使用 Bearer；否则使用 X-API-KEY
    const headers = { 'Content-Type': 'application/json' };
    if (this.#token.startsWith('ey') || this.#token.length > 100) {
      headers['Authorization'] = `Bearer ${this.#token}`;
    } else {
      headers['X-API-KEY'] = this.#token;
    }

    this.#log('info', `[MoviePilot] ${method} ${url.pathname}${url.search}`);

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);

    let res;
    try {
      res = await fetch(url.toString(), {
        method,
        headers,
        body: body ? JSON.stringify(body) : undefined,
        signal: controller.signal,
      });
    } catch (err) {
      clearTimeout(timer);
      if (err.name === 'AbortError') {
        throw new Error(`MoviePilot 请求超时 (${timeout}ms): ${method} ${path}`);
      }
      throw err;
    }
    clearTimeout(timer);

    if (res.status === 401 || res.status === 403) {
      throw new Error('MoviePilot 登录已失效或权限不足');
    }

    const text = await res.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }

    if (!res.ok) {
      const msg = typeof data === 'object' ? (data.message ?? data.detail ?? JSON.stringify(data)) : data;
      throw new Error(`MoviePilot 请求失败 [${res.status}]: ${msg}`);
    }

    this.#log('info', `[MoviePilot] 响应 ${res.status}`);
    return data;
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
}
