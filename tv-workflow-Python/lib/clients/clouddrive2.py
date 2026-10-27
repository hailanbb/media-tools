"""
CloudDrive2 gRPC-Web 客户端 (纯原生 Python 实现，零 C++ 二进制依赖)

封装文件系统操作：列表、获取信息、移动、删除、创建目录、强制刷新等。
使用原生 requests + 轻量级 Protobuf 编解码器与 gRPC-Web 协议交互。
完全跨平台，在 Ubuntu/Debian/CentOS 及 headless 服务器上零编译零额外依赖即可运行。
"""

from datetime import datetime
import struct
import time
from typing import Any, Dict, List, Optional
import urllib.parse

import requests


def parent_dir(p: str) -> str:
    """提取路径的父目录"""
    if not p or p == "/":
        return "/"
    normalized = p.rstrip("/")
    idx = normalized.rfind("/")
    if idx <= 0:
        return "/"
    return normalized[:idx]


def base_name(p: str) -> str:
    """提取路径最后一段（文件名或目录名）"""
    if not p or p == "/":
        return ""
    normalized = p.rstrip("/")
    idx = normalized.rfind("/")
    return normalized[idx + 1:]


# ──────────────────────────────────────────────────────────────────
# 轻量 Protobuf 编解码器
# ──────────────────────────────────────────────────────────────────

def encode_varint(n: int) -> bytes:
    buf = bytearray()
    while n > 0x7F:
        buf.append((n & 0x7F) | 0x80)
        n >>= 7
    buf.append(n & 0x7F)
    return bytes(buf)


def encode_field(field_num: int, wire_type: int, data: bytes) -> bytes:
    tag = (field_num << 3) | wire_type
    return encode_varint(tag) + data


def encode_string_field(field_num: int, s: str) -> bytes:
    b = s.encode("utf-8")
    return encode_field(field_num, 2, encode_varint(len(b)) + b)


def encode_bool_field(field_num: int, b: bool) -> bytes:
    return encode_field(field_num, 0, encode_varint(1 if b else 0))


def encode_int_field(field_num: int, n: int) -> bytes:
    return encode_field(field_num, 0, encode_varint(n))


def decode_protobuf(buf: bytes) -> Dict[int, List[Any]]:
    idx = 0
    fields: Dict[int, List[Any]] = {}
    length = len(buf)
    while idx < length:
        # read key
        key = 0
        shift = 0
        while idx < length:
            b = buf[idx]
            idx += 1
            key |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        field_num = key >> 3
        wire_type = key & 7
        if wire_type == 0:
            val = 0
            shift = 0
            while idx < length:
                b = buf[idx]
                idx += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            fields.setdefault(field_num, []).append(val)
        elif wire_type == 2:
            val_len = 0
            shift = 0
            while idx < length:
                b = buf[idx]
                idx += 1
                val_len |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            val = buf[idx:idx + val_len]
            idx += val_len
            fields.setdefault(field_num, []).append(val)
        elif wire_type == 1:
            idx += 8
        elif wire_type == 5:
            idx += 4
        else:
            break
    return fields


def make_grpc_frame(pb_buf: bytes, flag: int = 0x00) -> bytes:
    """封装 gRPC 帧：1字节 Flag + 4字节大端长度 + 数据"""
    return struct.pack(">BI", flag, len(pb_buf)) + pb_buf


class GrpcError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"CloudDrive2 gRPC 错误 [{status}]: {message}")
        self.status = status
        self.message = message


class CloudDriveClient:
    """CloudDrive2 原生 gRPC-Web 客户端"""

    def __init__(self, origin: str, token: str, logger: Any = None):
        self.origin = origin.rstrip("/")
        self.token = token
        self.logger = logger
        self.session = requests.Session()

    def _log(self, level: str, msg: str) -> None:
        if self.logger and hasattr(self.logger, level):
            getattr(self.logger, level)(msg)

    def _call_rpc(self, method: str, req_pb: bytes, timeout: int = 30) -> List[bytes]:
        url = f"{self.origin}/clouddrive.CloudDriveFileSrv/{method}"
        frame = make_grpc_frame(req_pb)
        max_retries = 3

        headers = {
            "Content-Type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/grpc-web+proto",
        }

        last_error = None
        for attempt in range(max_retries + 1):
            try:
                res = self.session.post(url, data=frame, headers=headers, timeout=timeout)

                # 429 限流退避
                if res.status_code == 429:
                    delay = min(1.0 * (2 ** attempt), 16.0)
                    self._log("warn", f"[CloudDrive2] 请求限流 (429)，{delay}s 后重试 ({attempt + 1}/{max_retries})")
                    if attempt < max_retries:
                        time.sleep(delay)
                        continue

                if res.status_code != 200:
                    raise RuntimeError(f"CloudDrive2 HTTP 错误 [{res.status_code}]: {res.text}")

                # 检查 Headers 中的 Trailers-only gRPC 状态
                if "grpc-status" in res.headers:
                    status = int(res.headers["grpc-status"])
                    msg = urllib.parse.unquote(res.headers.get("grpc-message", ""))
                    if status != 0:
                        raise GrpcError(status, msg)

                # 解码 gRPC-Web 数据流帧
                raw = res.content
                ptr = 0
                data_frames = []
                while ptr + 5 <= len(raw):
                    flag, payload_len = struct.unpack_from(">BI", raw, ptr)
                    payload = raw[ptr + 5: ptr + 5 + payload_len]
                    ptr += 5 + payload_len

                    if flag == 0x00:
                        data_frames.append(payload)
                    elif flag == 0x80:
                        trailer_str = payload.decode("utf-8", errors="replace")
                        status = 0
                        msg = ""
                        for line in trailer_str.split("\r\n"):
                            lower = line.lower()
                            if lower.startswith("grpc-status:"):
                                try:
                                    status = int(line.split(":")[1].strip())
                                except Exception:
                                    pass
                            elif lower.startswith("grpc-message:"):
                                msg = urllib.parse.unquote(line[line.find(":") + 1:].strip())
                        if status != 0:
                            raise GrpcError(status, msg)

                return data_frames

            except GrpcError:
                raise
            except Exception as e:
                last_error = e
                if attempt >= max_retries:
                    raise last_error
                time.sleep(1.0)

        if last_error:
            raise last_error
        return []

    def list_files(self, dir_path: str, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        列出目录下所有文件/子目录
        """
        norm_path = dir_path.rstrip("/") if dir_path != "/" else "/"
        if not norm_path:
            norm_path = "/"

        req_pb = (
            encode_string_field(1, norm_path) +
            encode_bool_field(2, force_refresh)
        )

        frames = self._call_rpc("GetSubFiles", req_pb)
        results = []

        for frame in frames:
            reply = decode_protobuf(frame)
            sub_files = reply.get(1, [])  # repeated CloudDriveFile subFiles = 1
            for sf_buf in sub_files:
                f = decode_protobuf(sf_buf)
                id_buf = (f.get(1) or [b""])[0]
                name_buf = (f.get(2) or [b""])[0]
                path_buf = (f.get(3) or [b""])[0]
                size_val = (f.get(4) or [0])[0]
                is_dir_val = (f.get(30) or [0])[0]

                name = id_buf.decode("utf-8", errors="replace") if False else name_buf.decode("utf-8", errors="replace")
                full_path = (
                    path_buf.decode("utf-8", errors="replace")
                    if path_buf
                    else (f"/{name}" if norm_path == "/" else f"{norm_path}/{name}")
                )
                is_dir = bool(is_dir_val == 1)

                modified = ""
                wt_buf = (f.get(7) or [None])[0]
                if wt_buf:
                    wt = decode_protobuf(wt_buf)
                    sec = (wt.get(1) or [0])[0]
                    if sec > 0:
                        modified = datetime.fromtimestamp(sec).isoformat()

                results.append({
                    "id": id_buf.decode("utf-8", errors="replace") if id_buf else "",
                    "name": name,
                    "path": full_path,
                    "directory": is_dir,
                    "isDirectory": is_dir,
                    "size": int(size_val) if size_val else 0,
                    "modified": modified,
                })

        return results

    def refresh_dir(self, dir_path: str) -> List[Dict[str, Any]]:
        """刷新目录（强制从云端底层重新拉取文件树）"""
        norm_path = dir_path.rstrip("/") if dir_path != "/" else "/"
        self._log("debug", f"[CloudDrive2] 页面刷新路径: {norm_path}")
        return self.list_files(norm_path, force_refresh=True)

    def get_file_info(self, file_path: str) -> Optional[Dict[str, Any]]:
        """获取单个文件或目录的元信息，不存在返回 None"""
        norm_path = file_path.rstrip("/") if file_path != "/" else "/"
        if norm_path == "/":
            return {
                "id": "root", "name": "", "path": "/",
                "directory": True, "isDirectory": True, "size": 0, "modified": ""
            }

        p_dir = parent_dir(norm_path)
        b_name = base_name(norm_path)
        req_pb = (
            encode_string_field(1, p_dir) +
            encode_string_field(2, b_name)
        )

        try:
            frames = self._call_rpc("FindFileByPath", req_pb)
            if not frames:
                return None
            f = decode_protobuf(frames[0])
            id_buf = (f.get(1) or [b""])[0]
            name_buf = (f.get(2) or [b""])[0]
            path_buf = (f.get(3) or [b""])[0]
            size_val = (f.get(4) or [0])[0]
            is_dir_val = (f.get(30) or [0])[0]

            modified = ""
            wt_buf = (f.get(7) or [None])[0]
            if wt_buf:
                wt = decode_protobuf(wt_buf)
                sec = (wt.get(1) or [0])[0]
                if sec > 0:
                    modified = datetime.fromtimestamp(sec).isoformat()

            name = name_buf.decode("utf-8", errors="replace") if name_buf else b_name
            full_path = path_buf.decode("utf-8", errors="replace") if path_buf else norm_path

            return {
                "id": id_buf.decode("utf-8", errors="replace") if id_buf else "",
                "name": name,
                "path": full_path,
                "directory": bool(is_dir_val == 1),
                "isDirectory": bool(is_dir_val == 1),
                "size": int(size_val) if size_val else 0,
                "modified": modified,
            }
        except GrpcError as ge:
            if ge.status == 5:  # NOT_FOUND
                return None
            raise

    def exists(self, file_path: str) -> bool:
        """检查路径是否存在"""
        try:
            info = self.get_file_info(file_path)
            return info is not None
        except Exception:
            return False

    def _decode_file_op_result(self, buf: bytes) -> Dict[str, Any]:
        fields = decode_protobuf(buf)
        success_val = (fields.get(1) or [0])[0]
        err_msg_buf = (fields.get(2) or [b""])[0]
        paths = [b.decode("utf-8", errors="replace") for b in fields.get(3, [])]
        return {
            "success": bool(success_val == 1),
            "errorMessage": err_msg_buf.decode("utf-8", errors="replace") if err_msg_buf else "",
            "resultFilePaths": paths,
        }

    def move_file(self, source_path: str, target_parent_path: str) -> Dict[str, Any]:
        """
        移动文件/目录到目标父目录
        """
        norm_src = source_path.rstrip("/")
        norm_dest = target_parent_path.rstrip("/") or "/"

        req_pb = (
            encode_string_field(1, norm_src) +
            encode_string_field(2, norm_dest) +
            encode_int_field(3, 0) +   # ConflictPolicy = Overwrite (0)
            encode_bool_field(4, True) # moveAcrossClouds = True
        )

        self._log("info", f"[CloudDrive2] 移动: {norm_src} -> {norm_dest}")
        frames = self._call_rpc("MoveFile", req_pb)
        if not frames:
            return {"success": True, "resultFilePaths": []}

        res = self._decode_file_op_result(frames[0])
        if not res["success"]:
            raise RuntimeError(f"CloudDrive2 移动文件失败: {res['errorMessage']}")
        return res

    def delete_file(self, file_path: str) -> Dict[str, Any]:
        """
        删除文件/目录（移至回收站）
        """
        norm_path = file_path.rstrip("/")
        req_pb = encode_string_field(1, norm_path)

        self._log("info", f"[CloudDrive2] 删除: {norm_path}")
        frames = self._call_rpc("DeleteFile", req_pb)
        if not frames:
            return {"success": True}

        res = self._decode_file_op_result(frames[0])
        if not res["success"]:
            raise RuntimeError(f"CloudDrive2 删除失败: {res['errorMessage']}")
        return res

    def mkdir(self, dir_path: str) -> Dict[str, Any]:
        """创建目录"""
        norm_path = dir_path.rstrip("/")
        p_dir = parent_dir(norm_path)
        f_name = base_name(norm_path)

        req_pb = (
            encode_string_field(1, p_dir) +
            encode_string_field(2, f_name)
        )

        self._log("info", f"[CloudDrive2] 创建目录: {norm_path}")
        frames = self._call_rpc("CreateFolder", req_pb)
        if not frames:
            return {"success": True}

        outer = decode_protobuf(frames[0])
        res_buf = (outer.get(2) or [None])[0]
        if res_buf:
            res = self._decode_file_op_result(res_buf)
            if not res["success"]:
                raise RuntimeError(f"CloudDrive2 创建目录失败: {res['errorMessage']}")

        return {"success": True}
