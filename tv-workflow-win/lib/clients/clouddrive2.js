/**
 * CloudDrive2 gRPC-Web 客户端
 * 封装文件系统操作：列表、获取信息、移动、删除、创建目录等
 * 使用原生 fetch 与轻量级 Protobuf 编解码（无第三方 gRPC 二进制依赖）
 * @module clients/clouddrive2
 */

/**
 * 提取路径的父目录
 * @param {string} p - 文件/目录路径
 * @returns {string} 父目录路径
 */
function parent(p) {
  if (!p || p === '/') return '/';
  const normalized = p.replace(/\/+$/, '');
  const idx = normalized.lastIndexOf('/');
  if (idx <= 0) return '/';
  return normalized.substring(0, idx);
}

/**
 * 提取路径最后一段（文件名或目录名）
 * @param {string} p - 文件/目录路径
 * @returns {string} 末段名称
 */
function basename(p) {
  if (!p || p === '/') return '';
  const normalized = p.replace(/\/+$/, '');
  const idx = normalized.lastIndexOf('/');
  return normalized.substring(idx + 1);
}

/* ------------------------------------------------------------------ */
/*  轻量 Protobuf 编解码器                                              */
/* ------------------------------------------------------------------ */

function encodeVarint(val) {
  const bytes = [];
  let n = typeof val === 'bigint' ? val : BigInt(val);
  while (n > 0x7fn) {
    bytes.push(Number((n & 0x7fn) | 0x80n));
    n >>= 7n;
  }
  bytes.push(Number(n));
  return Buffer.from(bytes);
}

function encodeField(fieldNum, wireType, dataBuf) {
  const tag = (fieldNum << 3) | wireType;
  return Buffer.concat([encodeVarint(tag), dataBuf]);
}

function encodeStringField(fieldNum, str) {
  const buf = Buffer.from(str, 'utf-8');
  return encodeField(fieldNum, 2, Buffer.concat([encodeVarint(buf.length), buf]));
}

function encodeBoolField(fieldNum, val) {
  return encodeField(fieldNum, 0, encodeVarint(val ? 1 : 0));
}

function encodeIntField(fieldNum, val) {
  return encodeField(fieldNum, 0, encodeVarint(val));
}

function decodeProtobuf(buf) {
  let idx = 0;
  const fields = new Map();
  while (idx < buf.length) {
    let key = 0n;
    let shift = 0n;
    while (idx < buf.length) {
      const b = BigInt(buf[idx++]);
      key |= (b & 0x7fn) << shift;
      if (!(b & 0x80n)) break;
      shift += 7n;
    }
    const fieldNum = Number(key >> 3n);
    const wireType = Number(key & 7n);
    if (wireType === 0) {
      // Varint
      let val = 0n;
      shift = 0n;
      while (idx < buf.length) {
        const b = BigInt(buf[idx++]);
        val |= (b & 0x7fn) << shift;
        if (!(b & 0x80n)) break;
        shift += 7n;
      }
      if (!fields.has(fieldNum)) fields.set(fieldNum, []);
      fields.get(fieldNum).push(val);
    } else if (wireType === 2) {
      // Length-delimited
      let len = 0;
      shift = 0n;
      while (idx < buf.length) {
        const b = BigInt(buf[idx++]);
        len |= Number((b & 0x7fn) << shift);
        if (!(b & 0x80n)) break;
        shift += 7n;
      }
      const val = buf.subarray(idx, idx + len);
      idx += len;
      if (!fields.has(fieldNum)) fields.set(fieldNum, []);
      fields.get(fieldNum).push(val);
    } else if (wireType === 1) {
      idx += 8;
    } else if (wireType === 5) {
      idx += 4;
    } else {
      break;
    }
  }
  return fields;
}

function makeGrpcFrame(pbBuf, flag = 0x00) {
  const frame = Buffer.alloc(5 + pbBuf.length);
  frame.writeUInt8(flag, 0);
  frame.writeUInt32BE(pbBuf.length, 1);
  pbBuf.copy(frame, 5);
  return frame;
}

/* ------------------------------------------------------------------ */
/*  CloudDriveClient 主类                                              */
/* ------------------------------------------------------------------ */

export class CloudDriveClient {
  /** @type {string} 服务地址（协议+主机+端口，无尾部斜杠） */
  #origin;
  /** @type {string} Bearer 认证令牌 */
  #token;
  /** @type {object|null} 日志器 */
  #logger;

  /**
   * @param {object} opts
   * @param {string} opts.origin  - 服务地址，如 https://cd1.example.com:8920
   * @param {string} opts.token   - API Token / Bearer 令牌
   * @param {object} [opts.logger] - 日志器
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
   * 列出目录下所有文件/子目录
   * @param {string} dirPath - 目录绝对路径，如 '/115/Library_incs/nas/电视剧'
   * @param {{ forceRefresh?: boolean }} [options] - 是否强制从云端同步最新列表
   * @returns {Promise<Array<{path:string, name:string, directory:boolean, isDirectory:boolean, size:number, modified:string, id:string}>>}
   */
  async listFiles(dirPath, { forceRefresh = false } = {}) {
    const normalizedPath = dirPath.replace(/\/+$/, '') || '/';
    const reqPb = Buffer.concat([
      encodeStringField(1, normalizedPath),
      encodeBoolField(2, Boolean(forceRefresh)), // forceRefresh
    ]);

    const dataFrames = await this.#callRpc('GetSubFiles', reqPb);
    const results = [];

    for (const frame of dataFrames) {
      const reply = decodeProtobuf(frame);
      const subFiles = reply.get(1) || []; // repeated CloudDriveFile subFiles = 1
      for (const sfBuf of subFiles) {
        const f = decodeProtobuf(sfBuf);
        const idBuf = (f.get(1) || [])[0];
        const nameBuf = (f.get(2) || [])[0];
        const pathBuf = (f.get(3) || [])[0];
        const sizeVal = (f.get(4) || [0n])[0];
        const isDirVal = (f.get(30) || [])[0];

        const name = nameBuf ? nameBuf.toString('utf-8') : '';
        const fullPath = pathBuf ? pathBuf.toString('utf-8') : (normalizedPath === '/' ? `/${name}` : `${normalizedPath}/${name}`);
        const isDir = isDirVal !== undefined ? isDirVal === 1n : false;

        let modified = '';
        const wtBuf = (f.get(7) || [])[0]; // writeTime (google.protobuf.Timestamp)
        if (wtBuf) {
          const wt = decodeProtobuf(wtBuf);
          const sec = Number((wt.get(1) || [0n])[0]);
          if (sec > 0) {
            modified = new Date(sec * 1000).toISOString();
          }
        }

        results.push({
          id: idBuf ? idBuf.toString('utf-8') : '',
          name,
          path: fullPath,
          directory: isDir,
          isDirectory: isDir,
          size: Number(sizeVal) || 0,
          modified,
        });
      }
    }

    return results;
  }

  /**
   * 刷新目录（强制从底层网盘拉取最新文件树）
   * 对应 CloudDrive2 网页端的“进入路径并进行页面刷新”
   * @param {string} dirPath - 目录路径
   * @returns {Promise<Array<{path:string, name:string, directory:boolean, isDirectory:boolean, size:number, modified:string, id:string}>>}
   */
  async refreshDir(dirPath) {
    const normalized = dirPath.replace(/\/+$/, '') || '/';
    this.#log('debug', `[CloudDrive2] 页面刷新路径: ${normalized}`);
    return this.listFiles(normalized, { forceRefresh: true });
  }

  /**
   * 获取单个文件或目录的元信息
   * @param {string} filePath - 文件或目录路径
   * @returns {Promise<object|null>} 文件信息对象，若不存在返回 null
   */
  async getFileInfo(filePath) {
    const normalized = filePath.replace(/\/+$/, '') || '/';
    if (normalized === '/') {
      return {
        id: 'root',
        name: '',
        path: '/',
        directory: true,
        isDirectory: true,
        size: 0,
        modified: '',
      };
    }

    const parentPath = parent(normalized);
    const name = basename(normalized);
    const reqPb = Buffer.concat([
      encodeStringField(1, parentPath),
      encodeStringField(2, name),
    ]);

    try {
      const frames = await this.#callRpc('FindFileByPath', reqPb);
      if (!frames.length) return null;

      const f = decodeProtobuf(frames[0]);
      const idBuf = (f.get(1) || [])[0];
      const nameBuf = (f.get(2) || [])[0];
      const pathBuf = (f.get(3) || [])[0];
      const sizeVal = (f.get(4) || [0n])[0];
      const isDirVal = (f.get(30) || [])[0];

      let modified = '';
      const wtBuf = (f.get(7) || [])[0];
      if (wtBuf) {
        const wt = decodeProtobuf(wtBuf);
        const sec = Number((wt.get(1) || [0n])[0]);
        if (sec > 0) modified = new Date(sec * 1000).toISOString();
      }

      return {
        id: idBuf ? idBuf.toString('utf-8') : '',
        name: nameBuf ? nameBuf.toString('utf-8') : name,
        path: pathBuf ? pathBuf.toString('utf-8') : normalized,
        directory: isDirVal !== undefined ? isDirVal === 1n : false,
        isDirectory: isDirVal !== undefined ? isDirVal === 1n : false,
        size: Number(sizeVal) || 0,
        modified,
      };
    } catch (err) {
      if (err.grpcStatus === 5) {
        return null; // Not found
      }
      throw err;
    }
  }

  /**
   * 检查路径是否存在
   * @param {string} filePath - 目标路径
   * @returns {Promise<boolean>}
   */
  async exists(filePath) {
    try {
      const info = await this.getFileInfo(filePath);
      return Boolean(info);
    } catch {
      return false;
    }
  }

  /**
   * 移动文件/目录到目标父目录
   * @param {string} sourcePath      - 源路径
   * @param {string} targetParentPath - 目标父目录路径
   * @returns {Promise<{success: boolean, resultFilePaths: string[]}>}
   */
  async moveFile(sourcePath, targetParentPath) {
    const normSource = sourcePath.replace(/\/+$/, '');
    const normDest = targetParentParent(targetParentPath);

    const reqPb = Buffer.concat([
      encodeStringField(1, normSource),       // repeated string theFilePaths = 1
      encodeStringField(2, normDest),         // string destPath = 2
      encodeIntField(3, 0),                  // ConflictPolicy = Overwrite (0)
      encodeBoolField(4, true),               // moveAcrossClouds = true
    ]);

    this.#log('info', `[CloudDrive2] 移动: ${normSource} -> ${normDest}`);
    const frames = await this.#callRpc('MoveFile', reqPb);
    if (!frames.length) return { success: true, resultFilePaths: [] };

    const res = this.#decodeFileOpResult(frames[0]);
    if (!res.success) {
      throw new Error(`CloudDrive2 移动文件失败: ${res.errorMessage}`);
    }
    return res;
  }

  /**
   * 删除文件/目录（移至回收站）
   * @param {string} filePath - 要删除的路径
   * @returns {Promise<{success: boolean}>}
   */
  async deleteFile(filePath) {
    const normPath = filePath.replace(/\/+$/, '');
    const reqPb = Buffer.concat([
      encodeStringField(1, normPath), // path = 1
    ]);

    this.#log('info', `[CloudDrive2] 删除: ${normPath}`);
    const frames = await this.#callRpc('DeleteFile', reqPb);
    if (!frames.length) return { success: true };

    const res = this.#decodeFileOpResult(frames[0]);
    if (!res.success) {
      throw new Error(`CloudDrive2 删除失败: ${res.errorMessage}`);
    }
    return res;
  }

  /**
   * 创建目录（递归/单级）
   * @param {string} dirPath - 要创建的目录路径
   * @returns {Promise<{success: boolean}>}
   */
  async mkdir(dirPath) {
    const normPath = dirPath.replace(/\/+$/, '');
    const parentPath = parent(normPath);
    const folderName = basename(normPath);

    const reqPb = Buffer.concat([
      encodeStringField(1, parentPath), // parentPath = 1
      encodeStringField(2, folderName), // folderName = 2
    ]);

    this.#log('info', `[CloudDrive2] 创建目录: ${normPath}`);
    const frames = await this.#callRpc('CreateFolder', reqPb);
    if (!frames.length) return { success: true };

    const outer = decodeProtobuf(frames[0]);
    const resBuf = (outer.get(2) || [])[0]; // FileOperationResult result = 2;
    if (resBuf) {
      const res = this.#decodeFileOpResult(resBuf);
      if (!res.success) {
        throw new Error(`CloudDrive2 创建目录失败: ${res.errorMessage}`);
      }
    }
    return { success: true };
  }

  /* ------------------------------------------------------------------ */
  /*  底层 gRPC-Web 通信处理器                                           */
  /* ------------------------------------------------------------------ */

  /**
   * 执行 gRPC-Web POST 调用
   * @param {string} method - 如 'GetSubFiles', 'FindFileByPath'
   * @param {Buffer} reqPb - Protobuf 序列化请求体
   * @param {number} [timeout=30000] - 超时毫秒
   * @returns {Promise<Buffer[]>} 数据帧 Buffer 数组
   */
  async #callRpc(method, reqPb, timeout = 30_000) {
    const url = `${this.#origin}/clouddrive.CloudDriveFileSrv/${method}`;
    const frame = makeGrpcFrame(reqPb);
    const maxRetries = 3;
    let lastError;

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      try {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeout);

        const res = await fetch(url, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/grpc-web+proto',
            'x-grpc-web': '1',
            Authorization: `Bearer ${this.#token}`,
            Accept: 'application/grpc-web+proto',
          },
          body: frame,
          signal: controller.signal,
        });
        clearTimeout(timer);

        // 429 限流 → 退避重试
        if (res.status === 429) {
          const delay = Math.min(1000 * 2 ** attempt, 16_000);
          this.#log('warn', `[CloudDrive2] 请求限流 (429)，${delay}ms 后重试 (${attempt + 1}/${maxRetries})`);
          if (attempt < maxRetries) {
            await this.#sleep(delay);
            continue;
          }
        }

        if (!res.ok) {
          throw new Error(`CloudDrive2 HTTP 错误 [${res.status}]: ${res.statusText}`);
        }

        // 检查 Headers 中的 Trailers-only gRPC 状态
        if (res.headers.has('grpc-status')) {
          const status = parseInt(res.headers.get('grpc-status'), 10);
          const msg = decodeURIComponent(res.headers.get('grpc-message') || '');
          if (status !== 0) {
            const err = new Error(`CloudDrive2 gRPC 错误 [${status}]: ${msg}`);
            err.grpcStatus = status;
            err.grpcMessage = msg;
            throw err;
          }
        }

        // 解码 gRPC-Web 数据流帧
        const arrayBuf = await res.arrayBuffer();
        const raw = Buffer.from(arrayBuf);
        let ptr = 0;
        const dataFrames = [];

        while (ptr + 5 <= raw.length) {
          const flag = raw.readUInt8(ptr);
          const len = raw.readUInt32BE(ptr + 1);
          const payload = raw.subarray(ptr + 5, ptr + 5 + len);
          ptr += 5 + len;

          if (flag === 0x00) {
            // 普通数据帧
            dataFrames.push(payload);
          } else if (flag === 0x80) {
            // Trailers 尾部帧
            const trailerStr = payload.toString('utf-8');
            const lines = trailerStr.split('\r\n');
            let status = 0;
            let msg = '';
            for (const line of lines) {
              const lower = line.toLowerCase();
              if (lower.startsWith('grpc-status:')) {
                status = parseInt(line.split(':')[1].trim(), 10);
              } else if (lower.startsWith('grpc-message:')) {
                msg = decodeURIComponent(line.substring(line.indexOf(':') + 1).trim());
              }
            }
            if (status !== 0) {
              const err = new Error(`CloudDrive2 gRPC 错误 [${status}]: ${msg}`);
              err.grpcStatus = status;
              err.grpcMessage = msg;
              throw err;
            }
          }
        }

        return dataFrames;
      } catch (err) {
        lastError = err;
        if (err.name === 'AbortError') {
          throw new Error(`CloudDrive2 请求超时 (${timeout}ms): ${method}`);
        }
        // 如果是 gRPC 业务错误（如 NOT_FOUND），直接抛出不重试
        if (err.grpcStatus !== undefined) {
          throw err;
        }
        if (attempt >= maxRetries) {
          throw lastError;
        }
      }
    }

    throw lastError;
  }

  /**
   * 解码 FileOperationResult
   * @param {Buffer} buf
   * @returns {{success: boolean, errorMessage: string, resultFilePaths: string[]}}
   */
  #decodeFileOpResult(buf) {
    const fields = decodeProtobuf(buf);
    const successVal = (fields.get(1) || [0n])[0];
    const errMsgBuf = (fields.get(2) || [])[0];
    const paths = (fields.get(3) || []).map((b) => b.toString('utf-8'));
    return {
      success: successVal === 1n,
      errorMessage: errMsgBuf ? errMsgBuf.toString('utf-8') : '',
      resultFilePaths: paths,
    };
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

function targetParentParent(targetParentPath) {
  return targetParentPath.replace(/\/+$/, '') || '/';
}

export { parent, basename };
