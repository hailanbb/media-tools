import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# 用法：python -B p0_probe.py <含 app 和 frontend 的测试副本目录>
root = Path(sys.argv[1]).resolve(strict=True)
assert (root / "app/api/archive.py").is_file()
work = Path(tempfile.mkdtemp(prefix="mf-probe-", dir=root))
os.chdir(work)
sys.path.insert(0, str(root))
os.environ.update(MF_DATA_DIR=str(work), MF_SOURCE_ROOT="/qa/src",
    MF_ARCHIVE_ROOT="/qa/dst", MF_LIBRARY_ROOT="/qa/dst",
    MF_AUTH_TOKEN="fixture", MF_CD2_URL="", MF_CD2_TOKEN="",
    MF_MP_URL="", MF_MP_TOKEN="", MF_TMDB_API_KEY="")
def deny_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo"}:
        raise RuntimeError("offline probe attempted networking")

import httpx
from app.main import app
from app.database import async_session, init_db, engine
from app.models.task import Task
from app.models.season import SeasonRecord
from app.core.review_engine import generate_review_clean_plan

class Disk:
    def __init__(self, entries):
        self.entries = entries
        self.deleted = []
        self.reads = []
    async def list_files(self, path, force_refresh=False):
        self.reads.append(path)
        if path not in self.entries:
            raise FileNotFoundError(path)
        return list(self.entries[path])
    async def delete_file(self, path):
        self.deleted.append(path)
        self.entries.pop(path, None)
        for items in self.entries.values():
            items[:] = [item for item in items if item["path"] != path]
        return {"success": True}
    async def close(self):
        pass

def item(path, size=100, directory=False):
    return {"name": path.rsplit("/", 1)[-1], "path":path,
            "isDirectory": directory, "size":size}

async def check_route(http, case, mode):
    show = "/qa/src/" + case
    src = show + "/Season 1"
    dst = "/qa/dst/" + case + "/Season 1"
    state = "already_archived"
    if mode == "same":
        src = dst
        show = dst.rsplit("/",1)[0]
    if mode == "root":
        show = "/qa/src"
    if mode == "state":
        state = "download_needed"
    names = ["X.S01E01.mkv","X.S01E02.mkv"]
    target_names = names[:1] if mode == "stale" else names
    disk = Disk({src:[item(src+"/"+n) for n in names],
                 dst:[item(dst+"/"+n) for n in target_names],
                 show:[item(src,directory=True), item(show+"/Extras",directory=True)]})
    if mode == "unreadable":
        disk.entries.pop(dst)
    async with async_session() as db:
        db.add(Task(id=case, name=case, source_path=show))
        await db.flush()
        db.add(SeasonRecord(id=case,task_id=case,show_name=case,show_path=show,
            season_path=src,season_number=1,status=state,archive_target_path=dst,
            total_episodes=2,local_episode_count=2,season_finished=True,
            recognized_episodes_json="[1,2]"))
        await db.commit()
    path, body = ("delete-source-show", {"season_record_id":case}) if mode=="root" else (
        "clean-source-redundant", {"season_record_ids":[case]})
    with patch("app.api.archive.CloudDrive2Client",return_value=disk):
        resp = await http.post("/api/archive/"+path,json=body)
    try:
        out=resp.json()
    except ValueError:
        out={}
    async with async_session() as db:
        rec=await db.get(SeasonRecord,case)
        after=rec.status if rec else None
    if mode=="covered":
        ok=(resp.status_code==200 and out.get("ok") is True
            and disk.deleted==[src] and after=="cleaned_redundant")
    else:
        refused=(resp.status_code==200 and out.get("ok") is False) or resp.status_code in (400,403,409,422)
        ok=refused and not disk.deleted and after==state
    return ok, {"http":resp.status_code,"response":out,"reads":disk.reads,
                "deleted":disk.deleted,"db_after":after}

def overlapping(total):
    prefix="/qa/src/Show/Season 1/X."
    single=item(prefix+"S01E01.1080p.mkv",200)
    pack=item(prefix+"S01E01-E02.1080p.mkv",100)
    plan=generate_review_clean_plan([single,pack],1,total)
    kept={x["path"] for x in plan["to_keep"]}
    deleted={x["path"] for x in plan["to_delete"]}
    return (not kept.intersection(deleted) and pack["path"] not in deleted,
            {"keep":sorted(kept),"delete":sorted(deleted)})

async def main():
    # Windows 的 asyncio 会在创建事件循环时建立内部回环 socketpair。
    sys.addaudithook(deny_network)
    await init_db()
    results=[]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),
        base_url="http://fixture",headers={"Authorization":"Bearer fixture"}) as http:
        # 启动自检可区分错误鉴权与业务拒绝
        warm=await http.get("/api/tasks/")
        if warm.status_code!=200:
            raise RuntimeError("harness initialization failed: "+str(warm.status_code))
        for case,mode in [("SAFE-01","stale"),("SAFE-02","same"),("SAFE-03","root"),
                          ("SAFE-04","state"),("SAFE-05","unreadable"),("SAFE-06","covered")]:
            ok,detail=await check_route(http,case,mode)
            results.append({"id":case,"result":"PASS" if ok else "FAIL","evidence":detail})
        for case,total in [("PLAN-01",2),("PLAN-02",3)]:
            ok,detail=overlapping(total)
            results.append({"id":case,"result":"PASS" if ok else "FAIL","evidence":detail})
    await engine.dispose()
    out=work/"probe-results.json"
    out.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding="utf-8")
    for r in results:
        print(r["id"],r["result"])
    print("evidence:",out)
    return int(any(r["result"]!="PASS" for r in results))
if __name__=="__main__":
    sys.exit(asyncio.run(main()))
