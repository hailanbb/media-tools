"""Validate preservation folders by contents, never by directory name alone."""
import posixpath

_AUX_EXTENSIONS = {".nfo", ".jpg", ".jpeg", ".png", ".webp", ".txt"}

async def mark_verified_auxiliary_folders(files, client):
    result = [dict(f) for f in files]
    async def verify(path, depth, budget):
        if depth > 3 or budget[0] <= 0:
            return False
        children = await client.list_files(path, force_refresh=True)
        if not isinstance(children, list):
            return False
        for child in children:
            budget[0] -= 1
            child_path = str(child.get("path") or "")
            if budget[0] < 0 or not child_path.startswith(path.rstrip("/") + "/"):
                return False
            if child.get("isDirectory"):
                if not await verify(child_path, depth + 1, budget):
                    return False
            elif posixpath.splitext(child.get("name", ""))[1].lower() not in _AUX_EXTENSIONS:
                return False
        return True
    for item in result:
        item.pop("verified_auxiliary_only", None)
        if item.get("isDirectory") and item.get("name") == "合并保留" and item.get("path"):
            try:
                item["verified_auxiliary_only"] = await verify(item["path"], 0, [500])
            except Exception:
                item["verified_auxiliary_only"] = False
    return result
