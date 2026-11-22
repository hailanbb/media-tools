"""清源时保全已验证的附件目录；不以目录名代替内容核验。"""
import posixpath
import uuid

from app.core.auxiliary_inventory import _AUX_EXTENSIONS


def checked_listing(files, parent):
    if not isinstance(files, list):
        raise ValueError("目录读取未返回完整清单")
    seen = set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("目录清单格式异常")
        name, path = item.get("name"), item.get("path")
        if (not isinstance(name, str) or not name or name in {".", ".."}
                or "/" in name or "\\" in name
                or path != parent + "/" + name
                or not isinstance(item.get("isDirectory"), bool)
                or name.casefold() in seen):
            raise ValueError("目录清单路径、类型或名称不唯一")
        seen.add(name.casefold())
    return files


async def read_auxiliary_tree(client, root, *, extensions=None):
    """强读最多 3 层、500 项附件，记录相对路径以供跨端核验。"""
    entries = {}

    async def visit(path, depth):
        if depth > 3:
            raise ValueError("合并保留目录层级过深，尚未完成附件核验")
        files = checked_listing(await client.list_files(path, force_refresh=True), path)
        for item in files:
            if len(entries) >= 500:
                raise ValueError("合并保留目录超过附件核验数量上限")
            relative = posixpath.relpath(item["path"], root)
            if not item["isDirectory"]:
                if posixpath.splitext(item["name"])[1].lower() not in (extensions or _AUX_EXTENSIONS):
                    raise ValueError("合并保留内含视频或待核对文件：" + relative)
                size = item.get("size")
                if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                    raise ValueError("合并保留附件大小未核实：" + relative)
            entries[relative] = dict(item)
            if item["isDirectory"]:
                await visit(item["path"], depth + 1)

    await visit(root, 0)
    return entries


def tree_signature(entries, *, relocated=False):
    return sorted((path, item["isDirectory"],
                   None if item["isDirectory"] else item.get("size"),
                   "" if relocated else str(item.get("id", "")),
                   "" if relocated else str(item.get("modified", "")))
                  for path, item in entries.items())


async def verify_preserved_folder(client, receipt):
    actual = await read_auxiliary_tree(client, receipt["target_path"], extensions=receipt.get("extensions"))
    if tree_signature(actual, relocated=True) != tree_signature(receipt["inventory"], relocated=True):
        raise ValueError("合并保留附件迁入后清单或大小不一致")


async def preserve_auxiliary_folder(client, source, archive_target, inventory, *, before_move=None):
    current = await read_auxiliary_tree(client, source)
    if tree_signature(current) != tree_signature(inventory):
        raise ValueError("合并保留附件清单已变化，请重新执行清源核验")
    target_files = checked_listing(await client.list_files(archive_target, force_refresh=True), archive_target)
    existing = next((f for f in target_files if f["name"].casefold() == "合并保留"), None)
    target_parent = archive_target
    if existing:
        if not existing["isDirectory"]:
            raise ValueError("归档端合并保留名称被文件占用")
        # 保留两个目录的原始内容，不合并覆盖同名附件；每次新保全有独立去向。
        target_parent = existing["path"] + "/清源-" + uuid.uuid4().hex
        created = await client.mkdir(target_parent)
        if not isinstance(created, dict) or created.get("success") is not True:
            raise ValueError("附件保全目录创建未确认成功")
        if checked_listing(await client.list_files(target_parent, force_refresh=True), target_parent):
            raise ValueError("附件保全目标目录非空，禁止覆盖")
    receipt = dict(source_path=source, target_path=target_parent + "/合并保留",
                   file_count=sum(not f["isDirectory"] for f in inventory.values()), inventory=inventory)
    # 移动前持久化去向及清单：进程中断／部分成功后的重试仍须核验附件。
    if before_move is not None:
        await before_move(receipt)
    moved = await client.move_file(source, target_parent, skip_existing=True)
    if not isinstance(moved, dict) or moved.get("success") is not True:
        raise ValueError("合并保留附件迁移未确认成功")
    await verify_preserved_folder(client, receipt)
    parent = posixpath.dirname(source)
    remaining = checked_listing(await client.list_files(parent, force_refresh=True), parent)
    if any(f["path"] == source for f in remaining):
        raise ValueError("合并保留附件迁移后源目录仍存在")
    return receipt


async def preserve_source_show_shell(client, source, archive_show, *, extensions, before_move=None):
    """已排除其他季及视频后，将整个资料外壳迁至归档独立位置，避免遗漏同名资产。"""
    inventory = await read_auxiliary_tree(client, source, extensions=extensions)
    listing = checked_listing(await client.list_files(archive_show, force_refresh=True), archive_show)
    shelf = archive_show + "/合并保留"
    existing = next((f for f in listing if f["name"].casefold() == "合并保留"), None)
    if existing and not existing["isDirectory"]:
        raise ValueError("归档剧根合并保留名称被文件占用")
    if existing:
        shelf = existing["path"]
    else:
        result = await client.mkdir(shelf)
        if not isinstance(result, dict) or result.get("success") is not True:
            raise ValueError("剧根附件保全目录创建未确认成功")
    parent = shelf + "/清源-" + uuid.uuid4().hex
    result = await client.mkdir(parent)
    if not isinstance(result, dict) or result.get("success") is not True:
        raise ValueError("剧根附件独立目录创建未确认成功")
    if checked_listing(await client.list_files(parent, force_refresh=True), parent):
        raise ValueError("剧根附件独立目录非空，禁止覆盖")
    current = await read_auxiliary_tree(client, source, extensions=extensions)
    if tree_signature(current) != tree_signature(inventory):
        raise ValueError("剧根附件清单已变化，保留源目录")
    receipt = dict(source_path=source, target_path=parent + "/" + posixpath.basename(source),
                   file_count=sum(not f["isDirectory"] for f in inventory.values()),
                   inventory=inventory, extensions=sorted(extensions))
    if before_move:
        await before_move(receipt)
    moved = await client.move_file(source, parent, skip_existing=True)
    if not isinstance(moved, dict) or moved.get("success") is not True:
        raise ValueError("剧根附件迁移未确认成功")
    await verify_preserved_folder(client, receipt)
    source_parent = posixpath.dirname(source)
    remaining = checked_listing(await client.list_files(source_parent, force_refresh=True), source_parent)
    if any(f["path"] == source for f in remaining):
        raise ValueError("剧根附件迁移后源目录仍存在")
    return receipt
