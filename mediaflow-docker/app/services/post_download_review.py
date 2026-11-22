"""补缺实物到位后定向复核。只读取媒体，沿用初检的身份、期段和版本规则。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.core.inspector import run_inspection
from app.core.episode_parser import is_video_file, parse_episode
from app.clients.clouddrive2 import GrpcError


def evidence_of(rec) -> dict:
    try:
        value = json.loads(rec.evidence_json or '{}')
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def review_pending(rec) -> bool:
    return evidence_of(rec).get('post_download_review', {}).get('state') == 'pending'


def set_pending(rec, reason: str) -> None:
    evidence = evidence_of(rec)
    evidence['post_download_review'] = {
        'state': 'pending', 'reason': reason, 'updated_at': datetime.now(timezone.utc).isoformat(),
    }
    rec.evidence_json = json.dumps(evidence, ensure_ascii=False)
    rec.status_detail = f'补缺后待复核：{reason}；暂不归档或重复推送'


def inventory_fingerprint(files: list[dict]) -> str:
    if not isinstance(files, list) or any(not isinstance(f, dict) or not f.get('name') for f in files):
        raise ValueError('目录未返回完整文件清单')
    rows = sorted((tuple(str(f.get(k, '')) for k in ('name', 'path', 'isDirectory', 'size', 'id', 'modified'))
                   for f in files))
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode('utf-8')).hexdigest()


class ReviewReader:
    """识别引擎只得到读取接口；强读并检测复核期间的目录变化。"""
    def __init__(self, client, archive_root=''):
        self.client = client
        self.archive_root = str(archive_root or '').rstrip('/')
        self.snapshots: dict[str, list[dict]] = {}
        self.failed = False

    async def list_files(self, path, **kwargs):
        try:
            if self.archive_root and (path == self.archive_root or path.startswith(self.archive_root + '/')):
                files = await read_archive_listing(self.client, path)
            else:
                files = await self.client.list_files(path, force_refresh=True)
            current = inventory_fingerprint(files)
            if path in self.snapshots and inventory_fingerprint(self.snapshots[path]) != current:
                raise ValueError('复核期间目录发生变化')
            self.snapshots[path] = [dict(f) for f in files]
            return files
        except Exception:
            self.failed = True
            raise


async def read_archive_listing(client, path):
    """仅明确不存在可作为归档端零覆盖；拒绝把超时、权限或其他错误当作空目录。"""
    try:
        files = await client.list_files(path, force_refresh=True)
        inventory_fingerprint(files)
        return files
    except FileNotFoundError:
        return []
    except GrpcError as exc:
        if exc.status == 5:
            return []
        raise


def unresolved_files(files: list[dict], season: int, total: int | None) -> list[dict]:
    """不能把错季、不明视频或超号正片默认为整季包附赠内容。"""
    problems = []
    for item in files:
        if item.get('isDirectory') or not is_video_file(item['name']):
            continue
        parsed = parse_episode(item['name'], item.get('path', ''), item.get('size', 0))
        if parsed.is_extra or parsed.is_special:
            continue
        reason = ''
        if parsed.file_size_bytes <= 0:
            reason = '视频大小为零或未知'
        elif parsed.season_number not in (None, season):
            reason = '文件季号不符'
        elif not parsed.episode_numbers:
            reason = '视频集号无法识别'
        elif total and any(ep > total for ep in parsed.episode_numbers):
            reason = '正片集号超出本季总数，不能自动当作特辑'
        if reason:
            problems.append({'name': item['name'], 'reason': reason})
    return problems


async def review_after_download(db, rec, *, cd2_client, mp_client=None) -> dict[str, Any]:
    """成功仅表示复核完成，最终去向仍由统一分类器裁决。"""
    if cd2_client is None:
        set_pending(rec, '目录读取客户端不可用')
        return {'ok': False, 'reason': '目录读取客户端不可用'}
    settings = get_settings()
    reader = ReviewReader(cd2_client, settings.archive_root)
    try:
        result = await run_inspection(
            task_id=rec.task_id, source_path=rec.season_path,
            only_season_path=rec.season_path, cd2_client=reader, mp_client=mp_client,
            archive_root=settings.archive_root, global_source_root=settings.source_root,
            db=db, concurrency=1, persist_metadata=False,
        )
        diag = next((s for s in result.seasons if s.season_path == rec.season_path), None)
        if reader.failed or result.errors or diag is None or rec.season_path not in reader.snapshots:
            raise ValueError('本季或归档目录读取不完整')
        if str(diag.tmdb_id or '') != str(rec.tmdb_id or '') or diag.season_number != rec.season_number:
            raise ValueError('复核返回的作品或季号发生变化，需要核对身份')
        source_files = reader.snapshots[rec.season_path]
        nested_problems = []
        for item in source_files:
            if item.get('isDirectory'):
                child = item.get('path') or f"{rec.season_path.rstrip('/')}/{item['name']}"
                contents = await reader.list_files(child)
                # 合并保留等纯附件目录不拦截；含视频或更深层目录则不能盲目整季归档。
                if any(f.get('isDirectory') or is_video_file(f['name']) for f in contents):
                    nested_problems.append({'name': item['name'], 'reason': '子目录含视频或未核对的嵌套内容'})
        # 即使识别引擎内部吞掉读取异常，也不能放行；结束时复核各目录指纹。
        for path, before in list(reader.snapshots.items()):
            if reader.archive_root and (path == reader.archive_root or path.startswith(reader.archive_root + '/')):
                after = await read_archive_listing(cd2_client, path)
            else:
                after = await cd2_client.list_files(path, force_refresh=True)
            if inventory_fingerprint(before) != inventory_fingerprint(after):
                raise ValueError('目录仍在变化，等待整理完成')
        if (diag.evidence or {}).get('meta_status') in ('fetch_failed', 'query_failed'):
            raise ValueError('资料核验未完成')

        old_evidence = evidence_of(rec)
        evidence = dict(diag.evidence or {})
        for key in ('last_correction_receipt', 'douban_binding', 'hold_for_task'):
            if key in old_evidence:
                evidence.setdefault(key, old_evidence[key])
        identity_blocking = bool((diag.conflict or {}).get('blocking'))
        problems = unresolved_files(source_files, rec.season_number, diag.total_episodes) + nested_problems
        # 新增未识别视频等问题不能被“全齐”或初检的超号特辑容错掩盖。
        if problems:
            diag.status = 'identity_review'
            diag.status_detail = f'补缺后发现 {len(problems)} 个视频需核实：' + '；'.join(p['reason'] for p in problems[:3])
            diag.conflict = {**(diag.conflict or {}), 'blocking': True, 'kind': 'post_download_content',
                             'message': diag.status_detail, 'files': problems}
        if source_files and not evidence.get('review_plan_preview') and not (diag.conflict or {}).get('blocking'):
            raise ValueError('未获得新文件的版本复核结果')

        rec.total_episodes = diag.total_episodes
        rec.local_episode_count = diag.local_episode_count
        rec.recognized_episodes_json = json.dumps(diag.recognized_episodes)
        rec.missing_episodes_json = json.dumps(diag.missing_episodes)
        rec.duplicate_episodes_json = json.dumps(diag.duplicate_episodes)
        rec.season_finished = diag.season_finished
        rec.status = diag.status
        rec.status_detail = '补缺后复核：' + diag.status_detail
        rec.archive_target_path = diag.archive_target_path
        rec.conflict_json = json.dumps(diag.conflict or {}, ensure_ascii=False)
        # 核对不改变身份键或下载回执；映射按原有效口径继续使用。
        identity_ok = not identity_blocking and evidence.get('meta_status') not in (
            'metadata_unverified', 'identity_conflict', 'mixed_works', 'not_listed') and bool(
                evidence.get('metadata_binding_verified') or (evidence.get('manual_confirmation') or {}).get('identity_confirmed'))
        evidence['post_download_review'] = {
            'state': 'completed', 'updated_at': datetime.now(timezone.utc).isoformat(),
            'coverage_verified': identity_ok,
            'file_count': len(source_files), 'source_fingerprint': inventory_fingerprint(source_files),
            'problems': problems, 'reason': rec.status_detail,
        }
        rec.evidence_json = json.dumps(evidence, ensure_ascii=False)
        rec.last_checked_at = datetime.now(timezone.utc)
        # 仅本季身份无冲突时，新的实物覆盖可用于结清逐集下载台账。
        present = set(diag.recognized_episodes) if identity_ok else set()
        if identity_ok:
            present.update((evidence.get('dual_archive_info') or {}).get('archived_episodes') or [])
        return {'ok': True, 'coverage_verified': identity_ok, 'present_episodes': sorted(present), 'status': rec.status}
    except Exception as exc:
        # 保留既有确认与台账，等待下次核对，不写“已归档”或释放重推。
        reason = str(exc) if isinstance(exc, ValueError) else f'定向复核未完成（{type(exc).__name__}）'
        set_pending(rec, reason)
        return {'ok': False, 'reason': reason}
