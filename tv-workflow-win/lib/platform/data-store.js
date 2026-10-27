/**
 * JSON 文件持久化存储
 * 用于保存下载记录、清理日志和归档状态等需要跨会话保留的数据
 */
import { readFileSync, writeFileSync, unlinkSync, readdirSync, mkdirSync, renameSync } from 'node:fs';
import { join, basename } from 'node:path';

/**
 * 创建数据存储实例
 * @param {string} dataDir - 数据目录路径
 * @returns {{ get, put, del, list }}
 */
export function createDataStore(dataDir) {
  mkdirSync(dataDir, { recursive: true });

  /** 将 key 转为安全文件名 */
  function keyToFile(key) {
    const safe = String(key)
      .replace(/[<>:"/\\|?*\x00-\x1f]/g, '_')
      .replace(/\s+/g, '_')
      .slice(0, 200);
    return join(dataDir, safe + '.json');
  }

  return {
    /**
     * 读取数据
     * @param {string} key
     * @param {*} fallback - 不存在时的默认值
     */
    get(key, fallback = null) {
      try {
        const raw = readFileSync(keyToFile(key), 'utf-8');
        return JSON.parse(raw);
      } catch {
        return fallback;
      }
    },

    /**
     * 写入数据（原子写：先写临时文件再重命名）
     * @param {string} key
     * @param {*} value
     */
    put(key, value) {
      const file = keyToFile(key);
      const tmp = file + '.tmp';
      writeFileSync(tmp, JSON.stringify(value, null, 2), 'utf-8');
      renameSync(tmp, file);
    },

    /**
     * 删除数据
     * @param {string} key
     */
    del(key) {
      try { unlinkSync(keyToFile(key)); } catch { /* 不存在也无妨 */ }
    },

    /**
     * 列出所有 key
     * @returns {string[]}
     */
    list() {
      try {
        return readdirSync(dataDir)
          .filter(f => f.endsWith('.json') && !f.endsWith('.tmp'))
          .map(f => basename(f, '.json'));
      } catch {
        return [];
      }
    }
  };
}
