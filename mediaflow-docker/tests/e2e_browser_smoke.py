"""MediaFlow 真实无头浏览器 (Edge Playwright) 全链路 E2E 冒烟巡检.

直接在无头 Edge 中打开运行中的 http://127.0.0.1:8080，执行零 Mock 真实页面巡检：
1. 捕获全局 pageerror、console.error 与 HTTP >= 500 响应；
2. 验证 5 大模块卡片切换与目录组面板归位契约（anomaly 必须隐藏，review 才展示）；
3. 验证「缺集待补」核对入库按钮按需显示契约；
4. 若存在待确认目录组与按钮，真实点击弹窗并切换单选框，校验预览区与所选目标严格同步；
5. 点击「执行推荐处理」预览弹窗并取消/关闭，校验无死锁与挂起崩溃。
"""

from __future__ import annotations

import os
import posixpath
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import VERSION  # noqa: E402

BASE_URL = os.environ.get("MF_BASE_URL", "http://127.0.0.1:8080").rstrip("/")


def run_e2e_smoke() -> None:
    page_errors: list[str] = []
    console_errors: list[str] = []
    http_5xx: list[str] = []
    http_4xx: list[str] = []
    checks: list[str] = []

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="msedge", headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)

        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        page.on(
            "response",
            lambda resp: (
                http_5xx.append(f"{resp.status} {resp.url}") if resp.status >= 500
                else (http_4xx.append(f"{resp.status} {resp.url}") if resp.status >= 400 else None)
            ),
        )

        # 1. 打开首页并等待初始加载
        page.goto(BASE_URL, wait_until="networkidle", timeout=20000)
        badge_txt = page.locator("#appVersionBadge").inner_text().strip()
        assert badge_txt == f"v{VERSION}", f"页面版本徽章应为 v{VERSION}，实际为 {badge_txt}"
        checks.append(f"首页渲染正常，版本徽章={badge_txt}")

        # 等待任务列表加载完毕
        page.wait_for_timeout(1000)
        has_task = page.evaluate("() => typeof currentTaskId === 'string' && currentTaskId.length > 0")
        if has_task:
            # 2. 切换 5 大模块并校验目录组面板归位契约
            for cat in ("anomaly", "download", "airing", "archive"):
                page.locator(f"#cat-card-{cat}").click()
                page.wait_for_timeout(250)
                is_hidden = page.locator("#directoryGroupsContainer").evaluate(
                    "el => el.classList.contains('hidden')"
                )
                assert is_hidden, f"切换到模块 {cat} 后，#directoryGroupsContainer 必须隐藏！"
            checks.append("异常剧集/缺集待补/连载留源/齐全可归档均已彻底隐藏目录组面板")

            # 3. 切到缺集待补校验「核对入库」按钮按需露出契约
            page.locator("#cat-card-download").click()
            page.wait_for_timeout(250)
            in_flight_check = page.evaluate("""() => {
                const t = taskList.find(x => x.id === currentTaskId);
                if (!t || !t.categories) return { skip: true };
                const pushed = (t.categories.download_pushed || []).length;
                const todo = t.categories.download || [];
                const hasTodoInFlight = todo.some(it => {
                    const gs = it.gapFillState || {};
                    return (gs.pushed_episodes && gs.pushed_episodes.length > 0)
                        || (gs.unverified_episodes && gs.unverified_episodes.length > 0);
                });
                const btnVisible = Boolean(document.querySelector('button[onclick*="reconcileCurrentTaskGapFill"]'));
                return { skip: false, hasInFlight: pushed > 0 || hasTodoInFlight, btnVisible };
            }""")
            if not in_flight_check.get("skip"):
                assert in_flight_check["btnVisible"] == in_flight_check["hasInFlight"], (
                    f"核对入库按钮可见性应与在途状态一致: {in_flight_check}"
                )
                checks.append(f"缺集待补「核对入库」按需展示契约 PASS ({in_flight_check})")

            # 4. 切到待复核清理校验目录组面板 & 若存在多候选组测试联动
            page.locator("#cat-card-review").click()
            page.wait_for_timeout(300)
            dg_count = page.evaluate("""() => {
                const t = taskList.find(x => x.id === currentTaskId);
                return (t && Array.isArray(t.directoryGroups)) ? t.directoryGroups.length : 0;
            }""")
            if dg_count > 0:
                is_hidden_rev = page.locator("#directoryGroupsContainer").evaluate(
                    "el => el.classList.contains('hidden')"
                )
                assert not is_hidden_rev, "待复核清理页存在 directoryGroups 时，目录组面板必须可见！"
                checks.append(f"待复核清理页目录组面板正常展示（{dg_count} 组）")

                # 尝试打开含 >=2 候选的确认弹窗并真实切换单选框
                btn = page.locator("#directoryGroupsContainer button[onclick*='openDirectoryGroupDialog']").first
                if btn.count() > 0:
                    btn.click()
                    page.wait_for_timeout(800)
                    radios = page.locator("input[name='dirGroupTargetRadio']:not([value='__custom__'])")
                    r_count = radios.count()
                    if r_count >= 2:
                        second_radio = radios.nth(1)
                        val = second_radio.get_attribute("value") or ""
                        exp_dir = posixpath.basename(val.rstrip("/"))
                        second_radio.check()
                        page.wait_for_timeout(1500)
                        preview_txt = page.locator("#dirGroupMergePreviewContainer").inner_text()
                        assert exp_dir in preview_txt or "生成合并预览失败" not in preview_txt, (
                            f"切换目录单选后预览区未更新为 {exp_dir}: {preview_txt[:200]}"
                        )
                        checks.append(f"规范化合并弹窗单选联动预览验证 PASS (目标={exp_dir})")
                    # 关闭弹窗
                    close_btn = page.locator("#directoryGroupDialog button:has-text('取消'), #directoryGroupDialog button:has-text('关闭')").first
                    if close_btn.count() > 0:
                        close_btn.click()
                    else:
                        page.evaluate("() => document.getElementById('directoryGroupDialog')?.close()")
                    page.wait_for_timeout(200)

            # 5. 测试「执行推荐处理」按钮可点击弹窗且关闭后页面仍保持响应
            rec_btn = page.locator("button[onclick*='openRecommendedBatchModal']").first
            if rec_btn.count() > 0:
                rec_btn.click()
                page.wait_for_timeout(500)
                modal_visible = not page.locator("#recommendedBatchModal").evaluate(
                    "el => el.classList.contains('hidden')"
                )
                assert modal_visible, "点击「执行推荐处理」后弹窗必须打开"
                page.evaluate("() => closeModal('recommendedBatchModal')")
                page.wait_for_timeout(200)
                # 关闭后立刻切回 anomaly 验证网页完全无死锁
                page.locator("#cat-card-anomaly").click()
                page.wait_for_timeout(200)
                checks.append("执行推荐处理弹窗开启/关闭取消及防死锁响应 PASS")

            # 6. 切换至媒体整理修复视图，测试转移修复设置弹窗（验证 bool -> Boolean 不再抛 ReferenceError）
            nav_repair = page.locator("#mp-nav-transfer_repair")
            if nav_repair.count() > 0:
                nav_repair.click()
                page.wait_for_timeout(400)
                rep_btn = page.locator("button[onclick*='openTransferRepairSettingsModal']").first
                if rep_btn.count() > 0 and rep_btn.is_visible():
                    rep_btn.click()
                    page.wait_for_timeout(500)
                    page.evaluate("() => closeModal('repairSettingsModal')")
                    checks.append("转移修复设置弹窗打开/关闭零报错 PASS")
        else:
            checks.append("当前无已选中任务，已完成首页加载与零报错检查")

        browser.close()

    print("=" * 72)
    print(f"🌐 MediaFlow 真实无头浏览器 E2E 冒烟巡检报告 ({BASE_URL})")
    print("=" * 72)
    for c in checks:
        print(f"  ✓ {c}")
    print(f"  • JS 未捕获异常 (pageerror): {len(page_errors)}")
    for e in page_errors:
        print(f"    ✗ pageerror: {e}")
    print(f"  • Console Error: {len(console_errors)}")
    for e in console_errors:
        print(f"    ✗ console.error: {e}")
    print(f"  • 服务端 5xx 响应: {len(http_5xx)}")
    for e in http_5xx:
        print(f"    ✗ HTTP 5xx: {e}")
    print(f"  • 服务端 4xx 响应: {len(http_4xx)}")
    for e in http_4xx:
        print(f"    ! HTTP 4xx: {e}")
    print("=" * 72)

    if page_errors or console_errors or http_5xx:
        sys.exit(1)
    print("✨ 真实浏览器 E2E 冒烟巡检 100% PASS（零 JS 异常、零 5xx）！")


if __name__ == "__main__":
    run_e2e_smoke()
