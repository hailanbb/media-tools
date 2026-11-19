"""MediaFlow 真实服务只读契约与并发无锁冒烟测试 (Black-Box Smoke & Lock Probe).

直接向运行中的本地 MediaFlow 服务（默认 http://127.0.0.1:8080）发送前端真实格式的只读与预览请求，
验证：
1. 首页及版本徽章与 app.config.VERSION 一致，无 500 崩溃；
2. /api/tasks、/api/tasks/{id} 等基础端点耗时正常；
3. /api/archive/recommended-plan 并发预览时不阻塞 SQLite 读请求，且响应耗时与载荷字段符合契约；
4. /api/tasks/{id}/directory-groups/{gid}/merge-preview 只传 target_path 时，源端主目录与前缀样本严格同步。
"""

from __future__ import annotations

import concurrent.futures
import os
import posixpath
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.stdout.reconfigure(encoding="utf-8")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import VERSION  # noqa: E402

BASE_URL = os.environ.get("MF_BASE_URL", "http://127.0.0.1:8080").rstrip("/")


def _extract_show_title(path_or_name: str) -> str:
    import re
    base = posixpath.basename(str(path_or_name or "").replace("\\", "/").rstrip("/"))
    base = re.sub(r"\s*\[tmdbid[=:]\d+\]\s*$", "", base, flags=re.I).strip()
    base = re.sub(r"\s*\(\d{4}\)\s*$", "", base).strip()
    return base


def run_smoke() -> dict[str, Any]:
    report: dict[str, Any] = {"checks": [], "warnings": [], "failures": []}
    timeout = httpx.Timeout(45.0, connect=5.0)

    with httpx.Client(base_url=BASE_URL, timeout=timeout, follow_redirects=True) as client:
        # 1. 首页与版本号
        t0 = time.perf_counter()
        r_home = client.get("/")
        cost_home = time.perf_counter() - t0
        assert r_home.status_code == 200, f"GET / 返回 {r_home.status_code}"
        expected_badge = f"v{VERSION}"
        if expected_badge not in r_home.text:
            report["failures"].append(f"首页 HTML 未包含当前版本徽章 {expected_badge}")
        report["checks"].append(f"GET / -> 200 ({cost_home:.2f}s), 版本徽章={expected_badge}")

        # 2. 获取任务列表
        t0 = time.perf_counter()
        r_tasks = client.get("/api/tasks")
        cost_tasks = time.perf_counter() - t0
        if r_tasks.status_code != 200:
            report["failures"].append(f"GET /api/tasks 失败: {r_tasks.status_code} {r_tasks.text[:200]}")
            return report
        tasks_data = r_tasks.json()
        tasks = tasks_data if isinstance(tasks_data, list) else (tasks_data.get("items") or tasks_data.get("tasks") or [])
        report["checks"].append(f"GET /api/tasks -> {len(tasks)} 个任务 ({cost_tasks:.2f}s)")

        # 3. 转移修复设置只读校验
        r_rep = client.get("/api/moviepilot/transfer-repair/settings")
        if r_rep.status_code != 200:
            report["failures"].append(f"GET /api/moviepilot/transfer-repair/settings 失败: {r_rep.status_code}")
        else:
            report["checks"].append("GET /api/moviepilot/transfer-repair/settings -> 200")

        if not tasks:
            report["warnings"].append("当前数据库暂无已扫描任务，跳过任务详情与并发预览压测")
            return report

        completed_tasks = [t for t in tasks if t.get("status") == "completed"]
        target_task = completed_tasks[0] if completed_tasks else tasks[0]
        tid = target_task["id"]

        # 4. 读取任务详情与季记录
        t0 = time.perf_counter()
        r_detail = client.get(f"/api/tasks/{tid}")
        cost_detail = time.perf_counter() - t0
        if r_detail.status_code != 200:
            report["failures"].append(f"GET /api/tasks/{tid} 失败: {r_detail.status_code} {r_detail.text[:200]}")
            return report
        detail = r_detail.json()
        report["checks"].append(f"GET /api/tasks/{tid} -> 200 ({cost_detail:.2f}s)")

        t0 = time.perf_counter()
        r_seasons = client.get(f"/api/tasks/{tid}/seasons")
        cost_seasons = time.perf_counter() - t0
        if r_seasons.status_code != 200:
            report["failures"].append(f"GET /api/tasks/{tid}/seasons 失败: {r_seasons.status_code} {r_seasons.text[:200]}")
            return report
        seasons_payload = r_seasons.json()
        seasons_list = seasons_payload if isinstance(seasons_payload, list) else (seasons_payload.get("seasons") or seasons_payload.get("items") or [])
        r_dgs = client.get(f"/api/tasks/{tid}/directory-groups")
        dir_groups = []
        if r_dgs.status_code == 200:
            dg_json = r_dgs.json()
            dir_groups = dg_json if isinstance(dg_json, list) else (dg_json.get("groups") or dg_json.get("items") or [])
        report["checks"].append(
            f"GET /api/tasks/{tid}/seasons -> 200 ({cost_seasons:.2f}s), seasons={len(seasons_list)}, directory_groups={len(dir_groups)}"
        )

        # 5. 并发无锁探针：发起 /api/archive/recommended-plan 同时并发读 /api/tasks
        def _req_recommended() -> tuple[float, httpx.Response]:
            ts = time.perf_counter()
            with httpx.Client(base_url=BASE_URL, timeout=httpx.Timeout(90.0, connect=5.0), follow_redirects=True) as c2:
                resp = c2.post("/api/archive/recommended-plan", json={"task_id": tid})
                return time.perf_counter() - ts, resp

        def _req_heartbeat(idx: int) -> tuple[int, float, int]:
            ts = time.perf_counter()
            with httpx.Client(base_url=BASE_URL, timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=True) as c3:
                resp = c3.get("/api/tasks/")
                return idx, time.perf_counter() - ts, resp.status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            fut_plan = pool.submit(_req_recommended)
            time.sleep(0.15)
            hb_futs = [pool.submit(_req_heartbeat, i) for i in range(4)]
            hb_results = [f.result() for f in hb_futs]
            cost_plan, r_plan = fut_plan.result()

        max_hb = max(x[1] for x in hb_results)
        if any(x[2] != 200 for x in hb_results) or max_hb > 3.0:
            report["failures"].append(f"并发探针异常：预览期间 /api/tasks 最大延迟 {max_hb:.2f}s，状态={[x[2] for x in hb_results]}")
        else:
            report["checks"].append(f"并发锁探针 PASS：预览期间 4 并发读最大耗时 {max_hb:.2f}s")

        if r_plan.status_code != 200:
            report["failures"].append(f"POST /api/archive/recommended-plan 失败: {r_plan.status_code} {r_plan.text[:250]}")
        else:
            plan_data = r_plan.json()
            payload_kb = len(r_plan.content) / 1024.0
            if cost_plan > 45.0:
                report["failures"].append(f"/recommended-plan 耗时过高 ({cost_plan:.2f}s > 45s)")
            for ci in plan_data.get("clean_items") or []:
                if "file_fingerprints" in ci:
                    report["failures"].append("响应载荷泄露了重型字段 file_fingerprints 给前端")
                    break
                if "keep_count" not in ci:
                    report["failures"].append("clean_items 缺少 keep_count 字段")
                    break
            report["checks"].append(
                f"POST /api/archive/recommended-plan -> 200 ({cost_plan:.2f}s, {payload_kb:.1f}KB), counts={plan_data.get('counts')}"
            )

        # 6. 规范化合并目录切换一致性契约验证（只传 target_path，不传 source_target_path）
        pending_multi = [
            g for g in dir_groups
            if g.get("status") == "pending_confirm" and len(g.get("candidate_targets") or []) >= 2
        ]
        if pending_multi:
            g = pending_multi[0]
            gid = g["id"]
            cands = g["candidate_targets"][:2]
            for c in cands:
                tpath = c.get("target_path") or ""
                if not tpath:
                    continue
                t0 = time.perf_counter()
                r_mp = client.post(
                    f"/api/tasks/{tid}/directory-groups/{gid}/merge-preview",
                    json={"target_path": tpath, "season_number": 1, "selected_prefix": ""},
                )
                cost_mp = time.perf_counter() - t0
                if r_mp.status_code != 200:
                    report["failures"].append(f"merge-preview 失败 ({gid}): {r_mp.status_code} {r_mp.text[:200]}")
                    continue
                mp = (r_mp.json() or {}).get("plan") or {}
                src_tgt = mp.get("source_target_show_path") or ""
                exp_dir = posixpath.basename(tpath.rstrip("/"))
                got_dir = posixpath.basename(src_tgt.rstrip("/"))
                if exp_dir.casefold() != got_dir.casefold():
                    report["failures"].append(
                        f"规范化合并目标不一致 (group={gid}): 勾选 {exp_dir}，但预览源端主目录为 {got_dir}"
                    )
                exp_title = _extract_show_title(exp_dir)
                samples = mp.get("prefix_samples") or []
                if exp_title and not any(exp_title in s for s in samples):
                    report["failures"].append(
                        f"目标前缀样本未跟随选中目录 (group={gid}): 期望包含 {exp_title}，实际={samples}"
                    )
                report["checks"].append(
                    f"merge-preview 目标联动 ({gid}, 选中={exp_dir}) -> 源端={got_dir}, 样本={samples[:2]} ({cost_mp:.2f}s)"
                )
        else:
            report["warnings"].append("当前任务暂无含 >=2 个候选的待确认目录组，跳过实时 merge-preview 切换验证")

    return report


if __name__ == "__main__":
    print("=" * 72)
    print(f"🔍 MediaFlow 真实服务只读契约与并发探针 ({BASE_URL})")
    print("=" * 72)
    res = run_smoke()
    for line in res["checks"]:
        print(f"  ✓ {line}")
    for w in res["warnings"]:
        print(f"  ⚠ {w}")
    for err in res["failures"]:
        print(f"  ✗ FAIL: {err}")
    print("=" * 72)
    if res["failures"]:
        sys.exit(1)
    print("✨ 真实服务只读契约与并发无锁探针全部通过！")
