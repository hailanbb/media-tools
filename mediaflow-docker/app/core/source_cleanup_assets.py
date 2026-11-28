"""历史遗留「合并保留」目录与源端公共刮削外壳安全核验及原地清理模块。

核心原则（v2.5.85）：
1. 绝不再向源库或归档库新建或搬运任何「合并保留」子目录，杜绝污染 Emby/Jellyfin 刮削识别（误生成 season.nfo）；
2. 对历史遗留的「合并保留」目录必须先强读内容核验（最多 3 层、500 项）：仅当 100% 属于刮削元数据/图片/txt 或刮削器临时垃圾（0 视频、0 字幕、0 未知文件）时才自底向上原地安全销毁；
3. 源剧集外层收尾时，仅将归档剧根尚缺失的标准公共刮削文件直接补齐至归档剧根，同名已有刮削文件直接随源端销毁，不覆盖归档端已有资产。
"""

from __future__ import annotations

import posixpath
from typing import Any

from app.core.auxiliary_inventory import (
    _AUX_EXTENSIONS,
    is_disposable_legacy_aux_file,
    is_ignorable_junk_or_scrape_temp,
    is_standard_scrape_asset,
)


def checked_listing(files, parent):
    if not isinstance(files, list):
        raise ValueError("目录读取未返回完整清单")
    seen = set()
    norm_parent = parent.rstrip("/")
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("目录清单格式异常")
        name, path = item.get("name"), item.get("path")
        if (
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
            or path != norm_parent + "/" + name
            or not isinstance(item.get("isDirectory"), bool)
            or name.casefold() in seen
        ):
            raise ValueError("目录清单路径、类型或名称不唯一")
        seen.add(name.casefold())
    return files


async def read_auxiliary_tree(client, root, *, extensions=None):
    """强读最多 3 层、500 项，严格校验遗留目录内不含视频、字幕或未知业务文件。"""
    entries: dict[str, dict[str, Any]] = {}
    norm_root = root.rstrip("/")

    async def visit(path, depth):
        if depth > 3:
            raise ValueError("合并保留目录层级过深，尚未完成附件核验")
        files = checked_listing(await client.list_files(path, force_refresh=True), path)
        for item in files:
            if len(entries) >= 500:
                raise ValueError("合并保留目录超过附件核验数量上限")
            relative = posixpath.relpath(item["path"], norm_root)
            if not item["isDirectory"]:
                fname = item["name"]
                allowed = (
                    is_ignorable_junk_or_scrape_temp(fname)
                    or (
                        posixpath.splitext(fname)[1].lower() in (extensions or _AUX_EXTENSIONS)
                    )
                )
                if not allowed:
                    raise ValueError("合并保留内含视频或待核对文件：" + relative)
                size = item.get("size")
                if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                    raise ValueError("合并保留附件大小未核实：" + relative)
            entries[relative] = dict(item)
            if item["isDirectory"]:
                await visit(item["path"], depth + 1)

    await visit(norm_root, 0)
    return entries


def tree_signature(entries, *, relocated=False):
    return sorted(
        (
            path,
            item["isDirectory"],
            None if item["isDirectory"] else item.get("size"),
            "" if relocated else str(item.get("id", "")),
            "" if relocated else str(item.get("modified", "")),
        )
        for path, item in entries.items()
    )


async def verify_preserved_folder(client, receipt):
    """兼容旧版历史持久化回执后验（若此前旧版曾生成迁移回执，仍支持只读复核）。"""
    if receipt.get("cleaned_in_place"):
        return
    actual = await read_auxiliary_tree(client, receipt["target_path"], extensions=receipt.get("extensions"))
    if tree_signature(actual, relocated=True) != tree_signature(receipt["inventory"], relocated=True):
        raise ValueError("合并保留附件迁入后清单或大小不一致")


async def clean_legacy_auxiliary_folder(client, source: str, inventory: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """原地自底向上安全清理已核验纯刮削/临时垃圾的历史遗留「合并保留」目录（绝不迁入归档库）。"""
    norm_source = source.rstrip("/")
    current = await read_auxiliary_tree(client, norm_source)
    if inventory is not None and tree_signature(current) != tree_signature(inventory):
        raise ValueError("合并保留附件清单已变化，请重新执行清源核验")

    # 先删全部文件，再按相对路径深度倒序删空子目录，最后删合并保留根目录
    file_items = [it for it in current.values() if not it["isDirectory"]]
    dir_items = sorted(
        [it for it in current.values() if it["isDirectory"]],
        key=lambda x: (-len(x["path"].split("/")), x["path"]),
    )

    for f_it in file_items:
        res = await client.delete_file(f_it["path"])
        if not isinstance(res, dict) or res.get("success") is not True:
            raise ValueError(f"清理遗留合并保留文件未确认成功：{f_it['name']}")

    for d_it in dir_items:
        rem = checked_listing(await client.list_files(d_it["path"], force_refresh=True), d_it["path"])
        if rem:
            raise ValueError(f"遗留合并保留子目录未清空：{d_it['name']}")
        res = await client.delete_file(d_it["path"])
        if not isinstance(res, dict) or res.get("success") is not True:
            raise ValueError(f"清理遗留合并保留子目录未确认成功：{d_it['name']}")

    rem_root = checked_listing(await client.list_files(norm_source, force_refresh=True), norm_source)
    if rem_root:
        raise ValueError("遗留合并保留根目录仍有未清空内容")
    res_root = await client.delete_file(norm_source)
    if not isinstance(res_root, dict) or res_root.get("success") is not True:
        raise ValueError("遗留合并保留根目录删除未确认成功")

    parent = posixpath.dirname(norm_source)
    remaining = checked_listing(await client.list_files(parent, force_refresh=True), parent)
    if any(f["path"] == norm_source for f in remaining):
        raise ValueError("遗留合并保留目录删除后仍可见")

    return {
        "source_path": norm_source,
        "target_path": "",
        "cleaned_in_place": True,
        "file_count": len(file_items),
        "deleted_dirs": len(dir_items) + 1,
    }


async def sync_missing_root_assets_and_clean_shell(
    client,
    source_show: str,
    archive_show: str,
    *,
    extensions=None,
    before_move=None,
) -> dict[str, Any]:
    """清理源剧集外层外壳（v2.5.85）：
    1. 若源剧根含遗留「合并保留」，强读核验纯刮削后原地销毁（绝不搬入归档库）；
    2. 源剧根的标准公共刮削资产：归档剧根缺失的直接补齐至归档剧根，归档剧根已有的原地销毁；
    3. 核验源剧根彻底为空后删除源剧根并复查父目录。
    """
    norm_source = source_show.rstrip("/")
    norm_archive = archive_show.rstrip("/")
    source_items = checked_listing(await client.list_files(norm_source, force_refresh=True), norm_source)
    archive_items = checked_listing(await client.list_files(norm_archive, force_refresh=True), norm_archive)
    archive_names_cf = {f["name"].casefold(): f for f in archive_items if not f["isDirectory"]}

    cleaned_hebing_files = 0
    for it in source_items:
        if it["isDirectory"]:
            if it["name"] != "合并保留":
                raise ValueError(f"源剧根仍有非预期子目录：{it['name']}")
            inv = await read_auxiliary_tree(client, it["path"], extensions=extensions)
            hb_res = await clean_legacy_auxiliary_folder(client, it["path"], inv)
            cleaned_hebing_files += int(hb_res.get("file_count") or 0)

    # 重新强读源剧根处理顶层文件
    top_files = checked_listing(await client.list_files(norm_source, force_refresh=True), norm_source)
    migrated_names: list[str] = []
    deleted_redundant_names: list[str] = []

    receipt = {
        "source_path": norm_source,
        "target_path": norm_archive,
        "cleaned_in_place": True,
        "file_count": len(top_files) + cleaned_hebing_files,
        "migrated_assets": migrated_names,
    }
    if before_move is not None:
        await before_move(receipt)

    for f_it in top_files:
        if f_it["isDirectory"]:
            raise ValueError(f"源剧根仍有未清空子目录：{f_it['name']}")
        fname = f_it["name"]
        if is_ignorable_junk_or_scrape_temp(fname):
            del_r = await client.delete_file(f_it["path"])
            if not isinstance(del_r, dict) or del_r.get("success") is not True:
                raise ValueError(f"清理源剧根临时文件失败：{fname}")
            deleted_redundant_names.append(fname)
            continue

        ext = posixpath.splitext(fname)[1].lower()
        if extensions is not None and ext not in extensions:
            raise ValueError(f"源剧根存在待核对未知附件：{fname}")
        if not is_standard_scrape_asset(fname) and not is_disposable_legacy_aux_file(fname):
            raise ValueError(f"源剧根存在未知附件：{fname}")

        if is_standard_scrape_asset(fname) and int(f_it.get("size") or 0) > 0 and fname.casefold() not in archive_names_cf:
            mv = await client.move_file(f_it["path"], norm_archive, skip_existing=True)
            if not isinstance(mv, dict) or mv.get("success") is not True:
                raise ValueError(f"补齐公共刮削资产至归档剧根未确认成功：{fname}")
            after_arc = checked_listing(await client.list_files(norm_archive, force_refresh=True), norm_archive)
            arrived = next((x for x in after_arc if not x["isDirectory"] and x["name"] == fname), None)
            if arrived is None or int(arrived.get("size") or 0) != int(f_it.get("size") or 0):
                raise ValueError(f"公共刮削资产迁入归档剧根后核验不一致：{fname}")
            archive_names_cf[fname.casefold()] = arrived
            migrated_names.append(fname)
        else:
            del_r = await client.delete_file(f_it["path"])
            if not isinstance(del_r, dict) or del_r.get("success") is not True:
                raise ValueError(f"清理源剧根冗余刮削文件未确认成功：{fname}")
            deleted_redundant_names.append(fname)

    rem_after = checked_listing(await client.list_files(norm_source, force_refresh=True), norm_source)
    if rem_after:
        raise ValueError("源剧根刮削文件处理后仍非空")
    del_show = await client.delete_file(norm_source)
    if not isinstance(del_show, dict) or del_show.get("success") is not True:
        raise ValueError("源剧根空壳删除回执未确认成功")

    source_parent = posixpath.dirname(norm_source)
    siblings = checked_listing(await client.list_files(source_parent, force_refresh=True), source_parent)
    if any(f["path"] == norm_source for f in siblings):
        raise ValueError("源剧根空壳删除后仍可见")

    receipt["migrated_assets"] = migrated_names
    receipt["deleted_redundant_assets"] = deleted_redundant_names
    return receipt
