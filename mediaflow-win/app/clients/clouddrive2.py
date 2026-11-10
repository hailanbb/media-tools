"""CloudDrive2 gRPC-Web 异步客户端。

基于 tv-workflow-Python 已验证的原生 Protobuf 编解码器，
改用 httpx.AsyncClient 实现全异步调用。
零 C++ 二进制依赖，跨平台运行。
"""

from __future__ import annotations

import asyncio
import struct
import urllib.parse
from datetime import datetime
from typing import Any

import httpx

from app.config import get_settings


# ──────────────────────────────────────────────────────────────────
# 轻量 Protobuf 编解码器
# ──────────────────────────────────────────────────────────────────

def _encode_varint(n: int) -> bytes:
    buf = bytearray()
    while n > 0x7F:
        buf.append((n & 0x7F) | 0x80)
        n >>= 7
    buf.append(n & 0x7F)
    return bytes(buf)


def _encode_field(field_num: int, wire_type: int, data: bytes) -> bytes:
    tag = (field_num << 3) | wire_type
    return _encode_varint(tag) + data


def _encode_string_field(field_num: int, s: str) -> bytes:
    b = s.encode("utf-8")
    return _encode_field(field_num, 2, _encode_varint(len(b)) + b)


def _encode_bool_field(field_num: int, b: bool) -> bytes:
    return _encode_field(field_num, 0, _encode_varint(1 if b else 0))


def _encode_int_field(field_num: int, n: int) -> bytes:
    return _encode_field(field_num, 0, _encode_varint(n))


def _decode_protobuf(buf: bytes) -> dict[int, list[Any]]:
    idx = 0
    fields: dict[int, list[Any]] = {}
    length = len(buf)
    while idx < length:
        key = 0
        shift = 0
        while idx < length:
            b = buf[idx]; idx += 1
            key |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        field_num = key >> 3
        wire_type = key & 7
        if wire_type == 0:
            val = 0; shift = 0
            while idx < length:
                b = buf[idx]; idx += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            fields.setdefault(field_num, []).append(val)
        elif wire_type == 2:
            val_len = 0; shift = 0
            while idx < length:
                b = buf[idx]; idx += 1
                val_len |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            val = buf[idx:idx + val_len]; idx += val_len
            fields.setdefault(field_num, []).append(val)
        elif wire_type == 1:
            idx += 8
        elif wire_type == 5:
            idx += 4
        else:
            break
    return fields


def _make_grpc_frame(pb_buf: bytes, flag: int = 0x00) -> bytes:
    return struct.pack(">BI", flag, len(pb_buf)) + pb_buf


# ──────────────────────────────────────────────────────────────────
# 路径辅助
# ──────────────────────────────────────────────────────────────────

def _parent_dir(p: str) -> str:
    if not p or p == "/":
        return "/"
    normalized = p.rstrip("/")
    idx = normalized.rfind("/")
    return "/" if idx <= 0 else normalized[:idx]


def _base_name(p: str) -> str:
    if not p or p == "/":
        return ""
    return p.rstrip("/").rsplit("/", 1)[-1]


# ──────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────

class GrpcError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"CloudDrive2 gRPC 错误 [{status}]: {message}")
        self.status = status
        self.message = message


# ──────────────────────────────────────────────────────────────────
# 异步客户端
# ──────────────────────────────────────────────────────────────────

class CloudDrive2Client:
    """CloudDrive2 gRPC-Web 异步客户端。"""

    def __init__(self) -> None:
        settings = get_settings()
        self.origin = settings.cd2_url.rstrip("/")
        self.token = settings.cd2_token
        self._client = httpx.AsyncClient(timeout=30.0, verify=False)

    async def _call_rpc(self, method: str, req_pb: bytes, timeout: int = 30) -> list[bytes]:
        """调用 CD2 gRPC-Web RPC 方法。"""
        url = f"{self.origin}/clouddrive.CloudDriveFileSrv/{method}"
        frame = _make_grpc_frame(req_pb)
        headers = {
            "Content-Type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/grpc-web+proto",
        }

        max_retries = 3 if method in ("GetSubFiles", "FindFileByPath") else 0
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                res = await self._client.post(url, content=frame, headers=headers, timeout=timeout)

                # 429 限流退避
                if res.status_code == 429:
                    if attempt < max_retries:
                        await asyncio.sleep(min(1.0 * (2 ** attempt), 16.0))
                        continue

                if res.status_code != 200:
                    raise RuntimeError(f"CloudDrive2 HTTP 错误 [{res.status_code}]: {res.text}")

                if "application/grpc-web" not in res.headers.get("content-type", "").lower():
                    raise RuntimeError("CloudDrive2 返回了非 gRPC-Web 响应")
                terminal_status = "grpc-status" in res.headers
                # Headers 中的 gRPC 状态
                if "grpc-status" in res.headers:
                    status = int(res.headers["grpc-status"])
                    msg = urllib.parse.unquote(res.headers.get("grpc-message", ""))
                    if status != 0:
                        raise GrpcError(status, msg)

                # 解码 gRPC-Web 数据帧
                raw = res.content
                ptr = 0
                data_frames: list[bytes] = []
                while ptr + 5 <= len(raw):
                    flag, payload_len = struct.unpack_from(">BI", raw, ptr)
                    if ptr + 5 + payload_len > len(raw):
                        raise RuntimeError("CloudDrive2 响应帧不完整")
                    payload = raw[ptr + 5: ptr + 5 + payload_len]
                    ptr += 5 + payload_len

                    if flag == 0x00:
                        data_frames.append(payload)
                    elif flag == 0x80:
                        trailer_str = payload.decode("utf-8", errors="replace")
                        status = 0; msg = ""
                        terminal_status = False
                        for line in trailer_str.split("\r\n"):
                            lower = line.lower()
                            if lower.startswith("grpc-status:"):
                                terminal_status = True
                                try: status = int(line.split(":")[1].strip())
                                except ValueError:
                                    raise RuntimeError("CloudDrive2 返回无效状态码")
                            elif lower.startswith("grpc-message:"):
                                msg = urllib.parse.unquote(line[line.find(":") + 1:].strip())
                        if status != 0:
                            raise GrpcError(status, msg)

                if ptr != len(raw) or not terminal_status:
                    raise RuntimeError("CloudDrive2 响应缺少有效终态")
                return data_frames

            except GrpcError:
                raise
            except Exception as e:
                last_error = e
                if attempt >= max_retries:
                    raise
                await asyncio.sleep(1.0)

        if last_error:
            raise last_error
        return []

    def _parse_file_entry(self, sf_buf: bytes, parent_path: str) -> dict[str, Any]:
        """解析单个文件/目录 protobuf 条目。"""
        f = _decode_protobuf(sf_buf)
        id_buf = (f.get(1) or [b""])[0]
        name_buf = (f.get(2) or [b""])[0]
        path_buf = (f.get(3) or [b""])[0]
        size_val = (f.get(4) or [0])[0]
        is_dir_val = (f.get(30) or [0])[0]

        name = name_buf.decode("utf-8", errors="replace") if name_buf else ""
        full_path = (
            path_buf.decode("utf-8", errors="replace")
            if path_buf
            else (f"/{name}" if parent_path == "/" else f"{parent_path}/{name}")
        )
        is_dir = bool(is_dir_val == 1)

        modified = ""
        wt_buf = (f.get(7) or [None])[0]
        if wt_buf:
            wt = _decode_protobuf(wt_buf)
            sec = (wt.get(1) or [0])[0]
            if sec > 0:
                modified = datetime.fromtimestamp(sec).isoformat()

        return {
            "id": id_buf.decode("utf-8", errors="replace") if id_buf else "",
            "name": name,
            "path": full_path,
            "isDirectory": is_dir,
            "size": int(size_val) if size_val else 0,
            "modified": modified,
        }

    async def list_files(self, dir_path: str, force_refresh: bool = False) -> list[dict[str, Any]]:
        """列出目录下所有文件/子目录。"""
        norm_path = dir_path.rstrip("/") if dir_path != "/" else "/"
        if not norm_path:
            norm_path = "/"

        req_pb = (
            _encode_string_field(1, norm_path) +
            _encode_bool_field(2, force_refresh)
        )
        frames = await self._call_rpc("GetSubFiles", req_pb)
        results: list[dict[str, Any]] = []

        for frame in frames:
            reply = _decode_protobuf(frame)
            sub_files = reply.get(1, [])
            for sf_buf in sub_files:
                results.append(self._parse_file_entry(sf_buf, norm_path))

        return results

    async def list_subdirectories(self, dir_path: str) -> list[dict[str, Any]]:
        """仅列出子目录（前端目录浏览器使用）。"""
        all_items = await self.list_files(dir_path)
        return [item for item in all_items if item["isDirectory"]]

    async def get_file_info(self, file_path: str) -> dict[str, Any] | None:
        """获取单个文件/目录的元信息。"""
        norm_path = file_path.rstrip("/") if file_path != "/" else "/"
        if norm_path == "/":
            return {"id": "root", "name": "", "path": "/", "isDirectory": True, "size": 0, "modified": ""}

        p_dir = _parent_dir(norm_path)
        b_name = _base_name(norm_path)
        req_pb = _encode_string_field(1, p_dir) + _encode_string_field(2, b_name)

        try:
            frames = await self._call_rpc("FindFileByPath", req_pb)
            if not frames:
                return None
            f = _decode_protobuf(frames[0])
            id_buf = (f.get(1) or [b""])[0]
            name_buf = (f.get(2) or [b""])[0]
            path_buf = (f.get(3) or [b""])[0]
            size_val = (f.get(4) or [0])[0]
            is_dir_val = (f.get(30) or [0])[0]

            modified = ""
            wt_buf = (f.get(7) or [None])[0]
            if wt_buf:
                wt = _decode_protobuf(wt_buf)
                sec = (wt.get(1) or [0])[0]
                if sec > 0:
                    modified = datetime.fromtimestamp(sec).isoformat()

            return {
                "id": id_buf.decode("utf-8", errors="replace") if id_buf else "",
                "name": name_buf.decode("utf-8", errors="replace") if name_buf else b_name,
                "path": path_buf.decode("utf-8", errors="replace") if path_buf else norm_path,
                "isDirectory": bool(is_dir_val == 1),
                "size": int(size_val) if size_val else 0,
                "modified": modified,
            }
        except GrpcError as ge:
            if ge.status == 5:  # NOT_FOUND
                return None
            raise

    async def move_file(self, source_path: str, target_parent_path: str) -> dict[str, Any]:
        """移动文件/目录到目标父目录。"""
        norm_src = source_path.rstrip("/")
        norm_dest = target_parent_path.rstrip("/") or "/"
        req_pb = (
            _encode_string_field(1, norm_src) +
            _encode_string_field(2, norm_dest) +
            _encode_int_field(3, 0) +    # ConflictPolicy = Overwrite
            _encode_bool_field(4, True)   # moveAcrossClouds
        )
        frames = await self._call_rpc("MoveFile", req_pb)
        if not frames:
            return {"success": True, "resultFilePaths": []}
        return self._decode_file_op_result(frames[0])

    async def delete_file(self, file_path: str) -> dict[str, Any]:
        """删除文件/目录（移至回收站）。"""
        norm_path = file_path.rstrip("/")
        req_pb = _encode_string_field(1, norm_path)
        frames = await self._call_rpc("DeleteFile", req_pb)
        if not frames:
            return {"success": True}
        res = self._decode_file_op_result(frames[0])
        if not res.get("success", False):
            raise RuntimeError(f"CloudDrive2 删除失败: {res.get('errorMessage') or '未知错误'}")
        return res

    async def mkdir(self, dir_path: str) -> dict[str, Any]:
        """创建目录。"""
        norm_path = dir_path.rstrip("/")
        p_dir = _parent_dir(norm_path)
        f_name = _base_name(norm_path)
        req_pb = _encode_string_field(1, p_dir) + _encode_string_field(2, f_name)
        frames = await self._call_rpc("CreateFolder", req_pb)
        if not frames:
            return {"success": True}
        outer = _decode_protobuf(frames[0])
        res_buf = (outer.get(2) or [None])[0]
        if res_buf:
            res = self._decode_file_op_result(res_buf)
            if not res["success"]:
                raise RuntimeError(f"CloudDrive2 创建目录失败: {res['errorMessage']}")
        return {"success": True}

    @staticmethod
    def _decode_file_op_result(buf: bytes) -> dict[str, Any]:
        fields = _decode_protobuf(buf)
        success_val = (fields.get(1) or [0])[0]
        err_msg_buf = (fields.get(2) or [b""])[0]
        paths = [b.decode("utf-8", errors="replace") for b in fields.get(3, [])]
        return {
            "success": bool(success_val == 1),
            "errorMessage": err_msg_buf.decode("utf-8", errors="replace") if err_msg_buf else "",
            "resultFilePaths": paths,
        }

    async def close(self) -> None:
        await self._client.aclose()
