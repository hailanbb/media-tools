"""MediaFlow 工业级自动化全矩阵测试套件总运行器 (Master Test Suite Runner).

汇总执行以下核心测试套件：
1. [前端静态契约] audit_frontend.js (DOM ID、Inline 事件、模态框 100% 映射)
2. [初检诊断矩阵] test_inspection.py (集号解析、状态诊断、异常剧集分类)
3. [复核清理矩阵] test_review_cleaner.py (画质仲裁、残卷淘汰、孤儿伴生清理、熔断保护)
4. [归档迁移矩阵] test_archiver.py (路径映射、同季冲突、削刮资产迁移、源端彻底销毁)
5. [订阅治理矩阵] test_moviepilot.py (双轨分离决策、完结判定、活跃追更保护)
6. [系统集成矩阵] full_system_test.py (端到端 FastAPI REST API 与并发生命周期集成测试)
"""

import os
import sys
import subprocess
import time

sys.stdout.reconfigure(encoding='utf-8')

SUITES = [
    {"name": "手动处理隐藏与缺集源端删除", "cmd": [sys.executable, "tests/test_manual_actions.py"], "type": "python"},
    {"name": "1. 前端静态契约与 DOM 事件审计", "cmd": ["node", "tests/audit_frontend.js"], "type": "node"},
    {"name": "2. 前端分类决策与卡片渲染契约测试", "cmd": ["node", "tests/test_frontend_logic.js"], "type": "node"},
    {"name": "3. 业务领域核心不变量与用户心智回归测试", "cmd": [sys.executable, "tests/test_domain_invariants.py"], "type": "python"},
    {"name": "4. 初检诊断与异常剧集引擎测试", "cmd": [sys.executable, "tests/test_inspection.py"], "type": "python"},
    {"name": "5. 复核清理与孤儿伴生仲裁测试", "cmd": [sys.executable, "tests/test_review_cleaner.py"], "type": "python"},
    {"name": "6. 归档迁移与源端零残留测试", "cmd": [sys.executable, "tests/test_archiver.py"], "type": "python"},
    {"name": "7. MoviePilot 智能治理双轨分离测试", "cmd": [sys.executable, "tests/test_moviepilot.py"], "type": "python"},
    {"name": "8. 全系统端到端真实服务生命周期测试", "cmd": [sys.executable, "tests/full_system_test.py"], "type": "python"},
    {"name": "9. 物理安全底线与字幕保护专项测试", "cmd": [sys.executable, "tests/test_phase1_security.py"], "type": "python"},
    {"name": "10. 领域算法多集识别与增量合并测试", "cmd": [sys.executable, "tests/test_phase3_domain.py"], "type": "python"},
    {"name": "11. 跨库集数差分与前端契约审计测试", "cmd": [sys.executable, "tests/test_amw_and_cross_diff.py"], "type": "python"},
    {"name": "12. 执行手册全规范覆盖专项测试", "cmd": [sys.executable, "tests/test_manual_full_coverage.py"], "type": "python"},
    {"name": "13. 离线 P0 安全探针 (8/8 核心防线)", "cmd": [sys.executable, "-B", "p0_probe.py", "."], "type": "python"},
]

def run_suite():
    print("=" * 70)
    print("🚀 启动 MediaFlow 全矩阵自动化回归测试流水线")
    print("=" * 70)

    start_total = time.time()
    results = []

    for s in SUITES:
        print(f"\n▶ 正在执行套件: 【{s['name']}】...")
        t0 = time.time()
        res = subprocess.run(s["cmd"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        cost = time.time() - t0
        passed = (res.returncode == 0)

        results.append({
            "name": s["name"],
            "passed": passed,
            "cost": cost,
            "stdout": res.stdout,
            "stderr": res.stderr
        })

        if passed:
            print(f"  ✓ 测试通过！(耗时 {cost:.2f}s)")
        else:
            print(f"  ✗ 测试失败！(耗时 {cost:.2f}s)")
            print("  --- 错误详情 ---")
            print(res.stderr or res.stdout)

    total_cost = time.time() - start_total
    all_passed = all(r["passed"] for r in results)

    print("\n" + "=" * 70)
    print("📊 MediaFlow 全矩阵自动化测试执行报告卡")
    print("=" * 70)
    for r in results:
        status_symbol = "🟢 PASS" if r["passed"] else "🔴 FAIL"
        print(f"{status_symbol}  {r['name']:<42} ({r['cost']:.2f}s)")

    print("-" * 70)
    print(f"总计耗时: {total_cost:.2f}s | 状态: {'全部通过 (100% PASS)' if all_passed else '存在未通过项'}")
    print("=" * 70)

    if not all_passed:
        sys.exit(1)

if __name__ == "__main__":
    run_suite()
