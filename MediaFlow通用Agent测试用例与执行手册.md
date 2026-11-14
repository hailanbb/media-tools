# MediaFlow 通用 Agent 测试用例与执行手册

适用基线：MediaFlow 2.5.4，2026-11-09 修订工作区。主机示例使用 Windows PowerShell，应用操作使用项目内的 FastAPI、SQLite 和单页前端。下文路径均相对于项目根目录。接手测试的 Agent 可以只凭本文件开始执行。

## 1　从哪里开始

**给接手 Agent 的任务指令**：请针对当前工作区执行本手册的 83 条用例。先跑离线基线及八个内嵌探针，再按表补齐未覆盖的输入变体。记录实际覆盖到的接口、浏览器和外部调用。将未执行项及原因写入独立 Markdown 执行报告。没有专用外部测试资源时，继续完成其余离线用例。

1. 记录实际日期、Git 提交、工作区修改清单、Python/Node 版本和操作系统。若测试更新的版本，保留旧用例 ID，并记录接口或业务规则的变化。
2. 在新目录建立测试副本，运行第 2 节的基线。执行第 3 节八个探针，逐项保存结果。它们覆盖三条已知误删路径及其对照场景。
3. 按第 4 节准备远程服务替身，从第 5 节用例表逐行执行。每行是一个可单独报告结果的用例；同一行有多个输入时分别留证。对写操作至少检查三个观察点，即客户端答复、外部调用清单、数据库或文件清单的最终状态。
4. 默认运行全部离线用例。真实服务联调仅在有专用测试实例、测试目录和相应操作授权时执行。某项失败后继续运行其他独立用例；如果发现误删，保留故障现场并停止相应的真实写入测试。
5. 用第 7 节模板交付。**“已有测试命令返回 0”只能证明该命令覆盖的用例通过。** 本文的预期结果是验收标准，已知缺陷应如实记录 FAIL。扩展目标用 **演进** 标注，单列结果，避免把尚未承诺的新能力混入发布判定。

优先级：**P0** 涉及唯一媒体、目录边界、破坏性副作用或越权写入；**P1** 涉及核心整理结果、状态真实性、任务控制和可利用的界面注入；**P2** 涉及可用性、性能、部署和次要反馈。先执行 P0，再执行 P1。相同优先级内部不要求排序。

## 2　隔离环境与现有回归

使用已具备 `requirements.txt` 依赖的 Python 和 Node。部署目标为 Python 3.12，先记录本地实际版本。缺依赖时在测试环境安装并记录版本，避免修改项目的依赖声明。以下代码块按顺序在同一 PowerShell 会话运行。每次都用新目录，复制源码、前端和测试，不复制 .env、data、日志或 Git 元数据。

```powershell
$project = (Resolve-Path -LiteralPath 'E:\GoogleAI\ls\mediaflow').Path
$qa = Join-Path (Split-Path -Parent $project) ('mediaflow-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $qa -ErrorAction Stop | Out-Null
foreach ($name in @('app','frontend','tests','requirements.txt')) {
    Copy-Item -LiteralPath (Join-Path $project $name) -Destination $qa -Recurse
}
Set-Location -LiteralPath $qa
$env:MF_DATA_DIR = Join-Path $qa 'test-data'
$env:MF_SOURCE_ROOT = '/qa/src'
$env:MF_ARCHIVE_ROOT = '/qa/dst'
$env:MF_LIBRARY_ROOT = '/qa/dst'
$env:MF_AUTH_TOKEN = 'fixture'
$env:MF_CD2_URL = 'http://cd2.invalid'
$env:MF_MP_URL = 'http://mp.invalid'
$env:MF_CD2_TOKEN = 'fixture'
$env:MF_MP_TOKEN = 'fixture'
$env:MF_TMDB_API_KEY = 'fixture'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
python --version
node --version
git -C $project rev-parse HEAD
git -C $project status --short
```

分别记录每条命令的退出码和测试数量；不要只保留最后一条的退出码。前四条是不同测试入口。最后一条会实际调用 Node 编译每个非空内联脚本。

```powershell
python -B -m unittest discover -s tests -p 'test_*.py'
python -B tests/test_amw_and_cross_diff.py
node tests/audit_frontend.js
node tests/test_frontend_logic.js
node -e 'const fs=require("fs"),cp=require("child_process");let n=0;for(const m of fs.readFileSync("frontend/index.html","utf8").matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)){if(m[1].trim()){cp.execFileSync(process.execPath,["--check"],{input:m[1]});n++}}if(!n)throw Error("no inline scripts");console.log("parsed",n)'
```

基线参考：上次同版本工作区发现 41 项 unittest、4 个 cross_diff 专项检查、4 个前端分类场景、2 段非空内联脚本。以后应以当前实际数量为准，解释数量减少的原因。DOM 审计主要检查元素和事件名称，分类测试包含镜像实现，二者均不能替代真实页面行为测试。`tests/run_all_tests.py` 当前会调用固定访问 `127.0.0.1:8080` 的 `full_system_test.py`，其中包含创建、扫描、校准和删除任务操作，不能当作上述离线基线运行。

## 3　可复制执行的安全探针

在第 2 节的同一会话运行以下代码，可从本手册自动提取后面的 Python 代码，不需要手工转录。其他操作系统也可将标记后的第一个 Python 块另存为 `p0_probe.py`，执行 `python -B p0_probe.py <测试副本绝对路径>`。脚本对内存网盘和临时 SQLite 执行真实业务路由，禁止 Python 建立外网连接。输出 PASS / FAIL 以及 JSON 证据路径，退出码 1 表示发现不符合预期的行为。启动异常或没有生成完整八条结果时，先报告执行环境错误。

```powershell
$manual = Join-Path $project 'MediaFlow通用Agent测试用例与执行手册.md'
$body = [IO.File]::ReadAllText($manual,[Text.Encoding]::UTF8)
$m = [regex]::Match($body,'(?s)<!-- BEGIN_PROBE -->\s*```python\r?\n(.*?)\r?\n```')
if (-not $m.Success) { throw '找不到安全探针代码块' }
$probe = Join-Path $qa 'p0_probe.py'
[IO.File]::WriteAllText($probe,$m.Groups[1].Value,[Text.UTF8Encoding]::new($false))
python -B $probe $qa
```

<!-- BEGIN_PROBE -->
```python
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
```

## 4　其他用例共享的测试数据和观察方法

- **标准目录。** S 为 `/qa/src/样例剧/Season 1`，D 为 `/qa/dst/样例剧/Season 1`，父目录分别为源剧和归档剧。E01 表示 `样例剧.S01E01.1080p.mkv`。除另有说明，每个视频为 100 MiB，目标测试季为 S01、两集、已完结。种子文件含 path、name、size、isDirectory，path 使用完整 POSIX 路径。两个不同目录中的同一文件使用各自路径。
- **记录与响应。** 先 POST `/api/tasks/`，请求体为 `{"name":"fixture","source_path":"/qa/src/样例剧","media_type":"tv"}`；取真实返回 id。可用模拟目录运行初检生成季记录，也可按探针直接构造指定的数据库前置状态。每个用例从新任务/新磁盘树开始；结束后用新数据库会话读取记录、任务汇总及 OperationLog。
- **文件测试替身。** 将路由模块实际引用的 `CloudDrive2Client` 替换为内存客户端。list_files 必须返回当前目录的直接子项，未知路径抛异常；move_file 成功时从源移至目标，delete_file 记录完整路径并修改树。可按第 N 次调用注入异常、业务拒绝或“已经移动，响应丢失”。每项保存前后文件树、按序调用参数、操作结果。上面的 Disk 只实现安全探针所需操作，测试归档时补充 mkdir、move_file 等方法。
- **元数据与 MP。** 在使用方替换 MoviePilotClient / TMDBClient。固定今天为 2026-11-09（或记录传入的固定时钟），分别返回过去、未来和缺失播出日期。下载候选含 `media_info`、`meta_info`、`torrent_info`，例如 `{"media_info":{"title":"样例剧","tmdb_id":101},"meta_info":{"season_list":[1],"episode_list":[2]},"torrent_info":{"title":"样例剧.S01E02.1080p","seeders":8,"site_name":"QA","size":734003200}}`。记录清历史、提交下载和退订的调用次数与参数；测试 HTTP 200 业务失败时在底层传输或客户端解析层注入，不要直接跳过被测错误检查。
- **浏览器。** 使用项目实际 `frontend/index.html`，以同源本地测试地址加载。允许浏览器请求本地夹具，拦截其余业务网络。无样式夹具只用于功能验证；视觉验收要加载本项目真实样式/图标并记录是否成功。浏览器可用 Playwright 或等效驱动。保存控制台错误、网络请求体、界面文字及必要截图，不记录认证头或真实下载链接。
- **并发与重试。** 用两个独立 HTTP 客户端/数据库会话，使用 Event 或 Barrier 控制“均已读取旧状态、尚未提交”的位置，再同步放行。不要依靠手动连点或固定延时来证明互斥。后台任务须等待确定终态或超时，记录运行次数。
- **表格约定。** 下表 `archive/*`、`inspect/*`、`moviepilot/*`、`tasks/*`、`system/*` 均前缀 `/api/`。归档、复核、残留与批量补缺传 `{"season_record_ids":["<季ID>"]}`。整剧删除传 `{"season_record_id":"<季ID>"}`。预览应零外部写入。保护性拒绝可用 4xx 或可解释的 `ok:false`；若接口抛 500、状态被改写或仍发出写调用，则失败。校验“某集仍存在”必须按文件实际覆盖的所有集号计算。

## 5　用例目录

### 5.1　删除边界与附件保护

| ID / 级别 | 输入与操作 | 通过条件 |
|---|---|---|
| SAFE-01 P0 | 状态 already_archived；S 有 E01、E02，D 仅 E01。POST archive/clean-source-redundant。 | 拒绝，零删除，源集数和记录不变。探针覆盖。 |
| SAFE-02 P0 | 把 season_path、archive_target_path 同设为 D，D 有 E01、E02。请求残留清理。 | 拒绝，归档库删除调用为零。探针覆盖。 |
| SAFE-03 P0 | show_path 等于配置的源根 /qa/src，提交 archive/delete-source-show。 | 源根及任一子项均未被删除，季记录仍在。探针覆盖。 |
| SAFE-04 P0 | 状态 download_needed，S/D 均有完整视频。请求残留清理。 | 旧状态不具备删除资格，即使目标非空也拒绝。探针覆盖。 |
| SAFE-05 P0 | 状态 already_archived；D 不存在或强制刷新抛超时，各测一次。 | 拒绝，零删除；错误可区分目标不可读与确实为空。探针覆盖不存在分支。 |
| SAFE-06 P1 | S/D 均有 E01、E02，无源端独有附件；源剧父目录另有 Extras。请求清理。 | 只移除 S；D 和 Extras 保留；季状态为 cleaned_redundant。探针覆盖。 |
| SAFE-07 P0 | 对整剧删除依次注入 /qa/src.finish/剧、/qa/src/../dst/剧、/qa/src/sub/..、空源根和重叠的源/归档根。 | 越界或无法确定边界时拒绝；不接受规范化后等于受保护根的路径。记录每个输入的调用清单。 |
| SAFE-08 P0 | S 与 D 集号齐全，但 S 独有 zh.srt、Extras/花絮.mkv、未知名视频。三种独有项分开测。 | 整季不得直接递归删除；需保留或先逐件验证已迁移。任一内容无法确认时均不能宣告已清理。 |
| SAFE-09 P0 | D 有同名零字节视频，或同名条目是目录；S 是该集唯一正常视频。分别请求清理。 | 均不得作为有有效副本的证明；唯一视频保留。 |
| SAFE-10 P0 | 预览通过后给 S 加 E03 或使 D 的 E02 消失，再执行；不改变数据库旧状态。 | 以执行时清单复查，报告内容已变化，E03/E02 均不丢失。 |
| SAFE-11 P0 | 归档最后一季后，源剧剩 Extras、Specials 或任意未识别子目录。 | 不删除父目录；多个兄弟目录的名字不能影响保护结果。 |
| SAFE-12 P0 | 归档后剩 poster.jpg；移动抛异常、返回 success:false、移动后重列失败，分别执行。 | 父目录零删除。资产迁移不成功时清楚列出保留源端/待处理项。 |
| SAFE-13 P1 | 合法整剧删除，另有同任务其他剧；模拟成功、CD2 业务拒绝、外部成功后审计提交失败三组。 | 成功只清目标剧并留审计；CD2 拒绝时保留记录；末组能区分外部已执行与本地记录失败，不能诱导重复删除。 |
| SAFE-14 P1 | already_archived 但 archive_target_path 为 NULL；分别配合法根、无法映射的路径。 | 合法情况验证映射后再操作，非法情况拒绝；不因 settings 未定义而返回 500。 |

### 5.2　识别、跨库初检和复核计划

| ID / 级别 | 输入与操作 | 通过条件 |
|---|---|---|
| PLAN-01 P0 | 两集完结季放较大的 E01 单集、较小的同命名组 E01–E02 合集；生成清理计划。 | 保留/删除路径不重叠，E02 唯一合集不在删除列表。探针覆盖。 |
| PLAN-02 P0 | 沿用 PLAN-01，总集数改为 3，触发缺集混版分支。 | E03 继续缺失，E02 不能因合集只按首集仲裁而丢失。探针覆盖。 |
| PLAN-03 P0 | 把 PLAN-01、02 数据分别交给 archive/clean-preview 与 archive/clean，额外替换文件列表顺序再测。 | 预览零删除；执行后覆盖至少为 {1,2}；不出现预览保留、实际删除同一路径。 |
| PLAN-04 P1 | 两个完整版本均有 E01、E02，同大小 2160p 与 720p；另用完整 1080p 加仅 E01 的 2160p 重测。 | 首组保留完整 2160p；第二组保留完整 1080p；每个淘汰文件有明确替代覆盖。 |
| PLAN-05 P0 | 较差视频旁有 .zh.srt/.ass；保留视频旁有 -thumb.jpg、同名 .nfo；另有 poster.jpg、tvshow.nfo、season.nfo。 | 删除清单与实际写调用均不含上述受保护文件；已淘汰视频的专属缩略图可按既定规则单独清理。 |
| PLAN-06 P0 | 季目录分别加入 0 字节视频、.part、子目录；同时准备可清理重复集。预览/执行各一次。 | 触发保护，拒绝实际删除；不能用 force_clean 绕过有数据损失风险的条件。 |
| PLAN-07 P1 | E01 两份、E02 缺失、E03 单份，确认完结；执行复核后重读季详情和汇总。 | 可去除 E01 重复，孤本 E03 保留，缺集清单仅 E02，状态仍为 download_needed，统计一致。 |
| PLAN-08 P1 | 测 S01E01、S01E01-E03、EP02、第02集、含花絮/Extra、无集号、不同季 S02E01、中文范围集。调用初检与复核两个实际解析入口。 | 明确格式取正确季和完整覆盖集；花絮与跨季文件不能满足本季正片完整性；解析不一致须报告两侧输出。 |
| PLAN-09 P1 | 复核预览后模拟文件新增或淘汰候选已被移走，再次请求执行。 | 重新核对现实数据，不删除新出现的孤本；预览与最终计划的差异可解释。 |
| SCAN-01 P1 | 分类/剧/Season 1、无季目录的平铺剧、空目录、不可读分类目录各建一份；运行 inspect/{id}/run，轮询 progress。 | 识别真实剧季、平铺按支持规则归季；空目录无伪记录；不可读显示失败范围，不能覆盖旧完整结果后报告全成功。 |
| SCAN-02 P1 | 官方两集已完结；本地分别放 {1,2}、{1,2,2}、{1}、{1,1}。逐次诊断。 | 依次为 archive_ready、review_needed、download_needed、download_needed；缺集和重复可同时呈现。 |
| SCAN-03 P1 | 尚有未来播出集且无重复；另一组查不到总集数；另一组已知 ID 的季详情超时。 | 连载为 airing，未知元数据明确为未知；超时不能成为已完结/可归档的证据。 |
| SCAN-04 P1 | 源 E02，归档 E01，官方 2 集；总集数改为 3 再测。 | 两集时可以增量归档、跨库缺集为空；三集时只补 E03。状态、详情和补缺参数使用同一差集。 |
| SCAN-05 P1 | 官方 2 集，源 E03、归档 E01。 | E02 缺失；E03 标为越界/需复核，不得凭集合元素个数判齐。 |
| SCAN-06 P0 | 源只含 unknown.mkv 无可解析集号；归档有 E01。另让归档季读取抛异常。 | 两组都不生成可直接清理的资格；即使直接调用写接口也零删除。 |
| SCAN-07 P1 | 同名不同年份、错误 TMDB 候选、豆瓣季别名、不完整播出日期。先提供明确媒体 ID，再移除 ID。 | 明确身份优先；别名不能串季；候选身份和完结日不确定时不自动归档。记录候选输入和选中依据。 |
| SCAN-08 P1 | POST archive/calibrate-episodes，传季 ID 和 total_episodes=3；再试 0、负数与未知 ID。 | 合法校准同步缺集、季状态和任务汇总；非法输入明确拒绝，已保存的校准不受影响。 |

### 5.3　归档执行与任务生命周期

| ID / 级别 | 输入与操作 | 通过条件 |
|---|---|---|
| ARC-01 P1 | S 有 E01/E02，D 不存在，源剧有 Season 2。对 archive_ready 执行 archive/execute。 | 仅正确 Season 1 移动至目标父目录，Season 2 及共享资产保留；数据库路径、状态与日志对应同一季。 |
| ARC-02 P1 | S 只有 E02，D 已有 E01，调用增量归档。 | D 最终有 E01/E02，原 E01 不变；只有源季为空才清源目录，日志记录已移动的文件。 |
| ARC-03 P0 | S/D 同名视频，或同名 subtitle/NFO 内容不同。各测一次。 | 视频冲突阻断；非视频冲突不能静默覆盖唯一内容。比较文件标识/内容指纹，不只比较文件数量。 |
| ARC-04 P0 | 目标父目录读取超时，或两端重叠且目录名不同。 | 无足够证据时无移动/覆盖/删除调用；不得将异常解释成空目标。 |
| ARC-05 P1 | 两个文件依次移动。分别注入全部拒绝、一个成功一个拒绝、成功后响应丢失。 | 全拒绝不能返回成功；部分完成可识别，重试前先核实两端，不重复移动已完成项；结果未知不得记成 completed。 |
| ARC-06 P1 | 正常清空父目录、剩下自定义子目录、季内还有未迁移文件三组；归档完成后查询前端。 | 第一组仅删除确认空目录；后两组保留。若应归档的文件仍未成功，任务不能显示全季归档成功。 |
| TASK-01 P1 | 创建任务，GET 单项/列表；完成初检后按 status 取 seasons，随后 DELETE tasks/{id}。 | 201 且 ID 唯一，列表与详情一致；过滤无串项；删除只移除任务及关联季记录，网盘调用为零。 |
| TASK-02 P1 | 同一 pending 任务的两个 run 请求分别读到 pending，阻塞双方 commit 再同时放行。 | 只有一个有效 worker。另一次明确拒绝或复用同一运行，外部扫描次数为 1。 |
| TASK-03 P1 | 准备多部剧和并发槽；暂停后释放已在途的调用，观察待开工项。暂停中再 run，再 resume。 | 已在途请求可收尾；排队的新剧不继续开工；重复 run 不拉起第二个 worker，resume 继续原任务。 |
| TASK-04 P1 | 分别在目录发现期和分析中途 stop，再查询 progress、任务列表、页面详情。 | 不再派发后续扫描，终态一致，已保存的部分结果可查看；旧 worker 不得覆盖后续运行结果。 |
| TASK-05 P1 | 已有两季及一项人工校准；reinspect 后仅第一季扫描成功、第二季读取失败。 | 明确显示错误范围，旧的有效季与人工校准可追溯；失败不能伪装为完整替换。 |
| TASK-06 P1 | 运行中模拟服务重启，再打开页面并调用 resume/run；同时检查不存在的任务 ID。 | 不出现永久 running 或“已恢复”却没有 worker；给出可执行的恢复/重跑动作，未知任务不产生后台工作。 |
| TASK-07 P2 | 对 completed/pending 任务分别 pause、resume、stop，并交错请求 delete。 | 不合法转换被拒绝或明确为无操作；数据库、进度与可用按钮一致，无孤立 worker。 |

### 5.4　MoviePilot 补缺、下载、退订与转移记录

| ID / 级别 | 输入与操作 | 通过条件 |
|---|---|---|
| MP-01 P1 | 构造缺 E02 的季，通过页面点击补缺。分别用 season_record_ids 和兼容字段 record_ids 调用 download-gap-fill；每次独立夹具。 | 页面提交正确 ID；两种合法格式均能到达业务处理；单次操作不因同时携带两字段而重复提交。 |
| MP-02 P1 | 缺 E02；候选 A 同季 E02、8 做种，B 同季 E02、1 做种，C 错季、20 做种，D 同季 E01。 | 仅 A 被提交，日志有资源名、站点、覆盖集；不提交低做种、错季或未覆盖缺集的资源。 |
| MP-03 P1 | 无记录、空数组、无资源、所有候选均拒绝，分别从路由走到页面。另测返回 success:false 而无 ok 的兼容结构。 | 四种情况均非成功；无记录不发外部写请求；页面对未知结构保守处理，计数与原因可见。 |
| MP-04 P1 | 一季需两条资源，MP 一条成功、一条失败；交换返回顺序。 | 日志含两条资源明细，准确显示部分成功；最终统计不随数组顺序改变。已成功资源不应被盲目重试。 |
| MP-05 P1 | MP 活跃队列已含同剧同季 E02；另测同名不同 ID、不同季和标题含 2026/1080p。 | 仅实际在途的目标集被扣除；年份、分辨率和其他剧不参与集号覆盖。 |
| MP-06 P1 | 剧集状态或路径相同的两个页面同时提交，延迟 MP 队列可见性；另测首次提交成功但响应断开。 | 记录实际外部提交数；同一任务/季/集/资源不应重复下载。若不能证明去重，报告可重复提交的时间窗口。 |
| MP-07 P1 | POST moviepilot/download，传 title、download_url 和 clean_transfer_history=false。底层 HTTP 分别返回 success:true、200+success:false、超时。 | 成功仅对应已确认提交；失败及不确定结果不得显示“已推送”。三个场景均不清历史。 |
| MP-08 P0 | 预置同剧 S01E01、S01E02、S02E02 三条明确历史。POST transfer-history/inspect-and-clean，body 为 title、season=1、episodes=[2]、execute=false，再以 true 执行。 | 预览只列 S01E02 且零写；执行仅删除匹配的历史 ID，每次调用 deletesrc=false、deletedest=false；无 CD2 文件删除。 |
| MP-09 P1 | 对缺季号/集号的历史、整季包、标题近似的两部剧，触发下载前清历史。模拟清历史成功但随后下载拒绝。 | 对不确定记录不自动误清；整季范围清楚可查；清理与下载两个结果分别可见。已删记录不应因下载失败而“消失在日志里”。 |
| MP-10 P1 | 订阅列表含：电视剧 12/12 暂停、8/24 三天内有更新、洗版、老电影已完成、本年新片。分别 POST subscriptions/clean-preview，传 media_type=tv/movie、mode=safe。 | 预览仅列本类型符合资格项；活动追更、洗版及新片保护按规则保留，预览无任何退订调用。检查返回的 to_clean、名称、ID 与计数。 |
| MP-11 P1 | POST subscriptions/batch-delete，传 sub_ids=[9], titles=["样例剧"]，MP 成功一次；再测两项一成一败、单项 DELETE subscriptions/9 返回业务拒绝。 | 每笔只计一次。单项成功为 1 成功/0 失败且有真实名称的审计；混合为 1/1。拒绝不写成功日志；不删除媒体文件。 |
| MP-12 P1 | GET transfer-queue 并选择一条待修记录，POST transfer-repair/{item_id}。准备有效及过期 ID。 | 成功显示必须对应一次可检查的实际修复提交；过期或未实现应显式说明，不能只回“已提交”。 |
| MP-13 P2 | POST search-resources 传 keyword、tmdb_id、season；分别返回 0、1、60 条或上游异常，再 GET downloads 和分页 transfer-history。 | 资源搜索遵守当前 50 条上限，空列表与错误可区分；手动选择与提交同一条；活动下载/历史正确透传测试页内容。 |
| MP-14 P1 | 禁用 MP 配置，测试 subscriptions、search-resources、download、download-gap-fill。 | 返回明确的未配置状态（当前路由通常为 503），不向任何站点发请求，不把未配置显示成“没有待办”。 |

### 5.5　前端、批处理与移动交互

| ID / 级别 | 输入与操作 | 通过条件 |
|---|---|---|
| UI-01 P1 | 并发拦截任务、健康、根配置三个请求；前两个分别延迟或失败。首次加载、空列表及切换任务各测一次。 | 三个请求可独立开始；已返回的任务可用；错误可辨认，无旧剧名/旧路径闪现或未处理脚本异常。 |
| UI-02 P1 | 将五种批处理各选 6 项，记录每次写请求的 ID 数、顺序、同时在途数与界面日志。五种为归档、复核、源端残留、补缺、退订。 | 每次写请求仅含当前一项，在途数不大于 1；日志可对应名称和结果，无重复或遗漏。刷新后的后端状态与成功项一致。 |
| UI-03 P1 | 选 6 项，放行第 1 项，挂起第 2 项时点“中止后续任务”，随后放行第 2 项。 | 写请求恰好 2 次，3–6 项零调用。显示已完成/失败/未执行的实际数量；已成功项持久化，点击结束刷新不会重发。 |
| UI-04 P1 | 第 2 项返回 HTTP 502，第 3 项 HTTP 200+ok:false，第 4 项跳过；其他成功。最后一项提交后暂不返回。 | 单项失败不使后续项或 UI 崩溃；计数与夹具逐项一致；最后一项返回前不能显示全部完成。 |
| UI-05 P1 | 快速切换任务 A→B，让 A 的详情比 B 后返回；在含“未知元数据/拆分冲突/真残留”的异常列表尝试全选。 | B 的标题、明细、计数一致；仅真残留允许源端清理，不能把校准项转作删除。还须验证后端独立拒绝非法 ID。 |
| UI-06 P1 | 目录及任务名含 O'Brien、双引号、反斜线、中文，以及无害标记 `<b id="fixture-mark">fixture</b>`。测试目录选择、任务卡片、预览文件名、结果日志。 | 原样显示与选中，DOM 不出现 fixture-mark 元素，点击路径与原输入相同。用 textContent/DOM 查询判定，保留截图或 DOM 证据。 |
| UI-07 P1 | 已归档、同季 conflict、已清理、连载、无元数据、stopped 各一条；打开结果页并比对后端。 | 终态不会落到“连载”；列表、卡片和允许操作一致；停止任务已保存的部分结果可读。 |
| UI-08 P1 | 形成正在执行的长项，关闭页面后重开；模拟服务端已成功但页面未收到响应。 | 重开显示实际已确认结果，不自动重复删除或下载；不确定项提示核实，未调度项不计成功。 |
| UI-09 P2 | 360×740、390×844、768×1024、1440×900，以长片名、长路径、200 行日志打开抽屉、预览与进度弹窗。 | 手机首屏可见任务内容，抽屉可开关；分类可达，操作按钮可滚动到，无遮挡；桌面双栏可用。记录真实样式是否加载。 |
| UI-10 P2 | 用键盘 Tab/Enter/Esc 操作抽屉与确认弹窗；切换深浅主题并刷新。 | 可清楚识别焦点和当前操作，退出弹窗不触发写请求，主题持久化，输入框中 Enter 不误删。 |
| UI-11 P1 | 在测试副本给主内联脚本加入同作用域重复 let，运行语法检查，以无改动副本作对照。 | 修改版编译失败且退出码非 0；原版通过。质量流程能挡住白屏回归。不要污染工作区。 |
| UI-12 P2 | 用 395 个模拟项（每项延迟 10–50ms），第 200 项失败一次，完整运行；再独立运行中止用例。 | 完整运行请求 395 次，394 成功、1 失败；每项 ID、顺序和结果可追溯。记录耗时及页面响应，勿将缩短的模拟延迟解释为真实网盘性能。 |

### 5.6　系统、鉴权、部署与恢复

| ID / 级别 | 输入与操作 | 通过条件 |
|---|---|---|
| SYS-01 P0 | 设置测试令牌，GET /、GET tasks/、POST archive/execute。对受保护路由分别用缺失、错误、正确令牌。 | 首页可加载；前两种凭据下业务 API 拒绝且零外部写；正确凭据访问授权资源。另在真实页面验证其能完成鉴权并加载任务。 |
| SYS-02 P1 | 清除健康缓存或启新进程；CD2 地址不可达但已配置，TMDB transport 备好 200；再交换成功/失败。 | 上游真实探测至少一次且显示正确状态，异常不泄露令牌；缓存期内结果可复用，过期后变化能反映。记录实际出网替身调用数。 |
| SYS-03 P1 | 返回 roots，目录浏览用中文空格路径并尝试越界定位；记录所选 path 创建任务，测试非法路径转入写操作。 | 正常路径往返一致、响应无秘密值；浏览允许的读范围与操作允许的写范围分别判定。界面选择不能绕过写边界。 |
| SYS-04 P2 | 临时日志含中文、多个时间点，GET system/logs?lines=10；再测缺文件、超出最大值、无权限。 | 返回所请求尾部、顺序正确；超界输入被校验，读取失败可区分空日志。页面和报告不暴露凭据。 |
| SYS-05 P1 | CD2/MP 客户端底层注入 HTTP 限流、超时、业务失败以及无有效业务结果；记录只读调用与移动/删除的尝试次数。 | 读可有限重试，明确业务失败不得变成成功；写请求响应不明时先核实真实结果，不能重复造成副作用。拒绝和超时可区分。 |
| SYS-06 P2 | 在副本中构造缺少依赖、未配置 CD2/MP、外部服务长期慢三组启动；检查首页、health 及任务错误。 | 应用配置错误能定位；依赖服务故障与应用进程存活可区分，单个探测不长期阻塞整个界面。记录实际耗时和容器探针阈值。 |
| SYS-07 P1 | 用临时 DB 创建任务并正常退出，重启后读回中文名称和状态；模拟在网盘成功、本地 commit 前中断。 | 正常数据可读；恢复后可区分未确认与失败，重跑前对账，不出现二次删除。外部与本地双写并非单一事务。 |
| SYS-08 P2 | **部署环境**：独立 Compose 项目、临时数据卷、未占用端口、占位配置。构建镜像、启动、访问首页和只读 API、重启，再验证数据库。 | 镜像内 Python 3.12 可运行，端口映射正确，重启后数据持久，容器健康状态与实际服务一致。缺 Docker 时标 BLOCKED。 |
| SYS-09 P2 | **演进**：若实现持久操作 ID、迁移或恢复入口，则用旧版本测试库副本升级，并重复提交同一操作键。 | 记录和人工校准保存，升级可重复执行；同一键最多一次外部写。尚无此功能标 N/A，并记录版本依据。 |

### 5.7　跨模块旅程

| ID / 级别 | 输入与操作 | 通过条件 |
|---|---|---|
| E2E-01 P1 | 在隔离应用中创建任务→扫描已完结两集→预览→执行重复清理→重新获取季状态→归档→刷新页面。源端有 Extras，视频 E01 两个版本、E02 一份。 | 先 review_needed，去重后 archive_ready，归档后目标仅保留完整版本；字幕、Extras 与日志正确；所有页面数据可追溯到本次任务。 |
| E2E-02 P1 | 源 E02，归档 E01，官方三集→初检→补 E03→模拟到货→重新扫描→增量合并。 | 只下载 E03 一次，归档最终覆盖 {1,2,3}，不重复补 E01，不覆盖已归档文件，清理后没有错误的“连载”记录。 |
| E2E-03 P0 | 预览残留判定时两库完整；确认前让归档目录不可读或丢失 E02，同时新增独有字幕→从真实页面确认。 | 浏览器、后端、模拟网盘与数据库共同证明未删除源端；失败能在该项日志定位，同批其他独立项不受影响。 |
| E2E-04 P1 | **外部联调，可选**：在专用 CD2 测试树创建可丢弃样例，在专用 MP 实例创建测试订阅；走一次归档和退订，然后从两端独立查询。 | 外部服务实际状态、应用页面和日志一致；只影响授权样例。缺少专用目标时记录未联调，不用离线结果代替。 |

## 6　与历次审查发现的映射

| 审查项 | 核心用例 | 审查项 | 核心用例 |
|---|---|---|---|
| F01 空壳判断 | SAFE-11、SAFE-12、ARC-06 | F02 残留误删 | SAFE-01～10、SCAN-06、E2E-03 |
| F03 补缺契约 | MP-01～04 | F04 删除审计 | SAFE-13、SYS-07 |
| F05 业务失败误报 | ARC-05、MP-07、MP-11、SYS-05 | F06 字幕与缩略图 | PLAN-05、SAFE-08 |
| F07 多集合并 | PLAN-01～04、PLAN-08 | F08 跨库增量 | SCAN-04～05、ARC-02～05、E2E-02 |
| F09 生命周期 | TASK-02～07、SYS-07 | F10 元数据 | SCAN-03、SCAN-07 |
| F11 HTML 注入 | UI-06 | F12 鉴权及根路径 | SYS-01、SAFE-02～03、SAFE-07 |
| F13 批处理可靠性 | UI-02～04、UI-08、UI-12 | F14 补缺防重 | MP-05～06 |
| F15 健康检查 | SYS-02、SYS-06、SYS-08 | F16 分类汇总 | PLAN-07、UI-05、UI-07 |
| F17 回归覆盖 | 第 2、3 节、UI-11、E2E-01～03 | 新增退订/转移问题 | MP-11～12 |

## 7　结果记录与交付

每次执行另存一份 `MediaFlow测试执行报告-日期-运行编号.md`。开头写清被测版本、是否包含未提交修改、操作系统、运行时版本、夹具和实际执行范围。关闭临时服务后再清理本次创建的测试资源；保留报告和最小失败证据。

| 用例 ID / 输入变体 | 结果 | 预期与实测差异 | 证据 | 影响 / 复测 |
|---|---|---|---|---|
| 示例：SAFE-01 / 目标缺 E02 | 待填写 | 应拒绝；填写 HTTP 状态、ok、源季终态 | 请求体、探针 JSON、delete_file 调用路径及次数 | P0；记录修复版本与复跑结果 |

结果取值为 PASS、FAIL、BLOCKED（缺少执行条件）、NOT RUN（未执行）和 N/A（不适用于当前版本，须说明理由）。复合用例有任一必测变体失败则整行 FAIL；有必测变体未执行则不能整行 PASS。独立报告“离线核心、浏览器、Docker、真实 CD2/MP、演进目标”，并给出各自分母。P0 失败时列明应暂停的具体写操作，其他已验证功能可单独评估。发布意见必须依据本次实测，不复制上次审查的结论。
