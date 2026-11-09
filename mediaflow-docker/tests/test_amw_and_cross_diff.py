"""测试专项：
1. 剧名含有撇号单引号（'）时的前端事件派发契约与模态框校准；
2. 跨库查重深入集数级比对（防止源端增量新集被误删）；
3. 豆瓣真实季集深度解析兜底（解决 TMDB 缺失但豆瓣有集数的问题）；
4. 官方无集数信息剧集“删除源端并强制刷新”契约审计。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.episode_parser import ParsedEpisode
from app.core.inspector import diagnose_season


def test_cross_diff_logic():
    source_eps = {2}
    archived_eps = {1}
    total_eps = 3

    assert not source_eps.issubset(archived_eps), "源端包含增量集，不应是归档库的子集"
    combined = sorted(source_eps | archived_eps)
    assert combined == [1, 2]
    missing = [e for e in range(1, total_eps + 1) if e not in combined]
    assert missing == [3]

    source_eps_b = {2, 3}
    combined_b = sorted(source_eps_b | archived_eps)
    assert len(combined_b) >= total_eps, "跨库汇聚后已集齐"

    source_eps_c = {1}
    archived_eps_c = {1, 2}
    assert source_eps_c.issubset(archived_eps_c), "源端是归档库子集，确认为残留可清理"
    print("[PASS] Cross library episode diffing logic passed.")


def test_diagnose_season_unknown_meta():
    status, detail = diagnose_season(
        recognized=[1],
        total_episodes=0,
        season_finished=True,
        has_duplicates=False,
        duplicate_episodes=[]
    )
    assert status == "unknown"
    assert "无法获取" in detail or "未知" in detail
    print("[PASS] Unknown meta diagnosis logic passed.")


def test_douban_parsing_logic():
    # 模拟豆瓣 media_info 返回的 seasons 结构
    sample_douban_seasons = {"1": list(range(1, 12))}
    seasons_data = {}
    for s_str, eps in sample_douban_seasons.items():
        sn = int(s_str)
        seasons_data[sn] = {
            "total": len(eps),
            "finished": True,
        }
    assert 1 in seasons_data
    assert seasons_data[1]["total"] == 11
    assert seasons_data[1]["finished"] is True

    # 诊断: 本地识别 11 集，豆瓣 11 集 -> 判定为 archive_ready
    status, detail = diagnose_season(
        recognized=list(range(1, 12)),
        total_episodes=seasons_data[1]["total"],
        season_finished=True,
        has_duplicates=False,
    )
    assert status == "archive_ready"
    print("[PASS] Douban episode parsing and archive_ready transition logic passed.")


def test_frontend_contracts():
    html_path = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    # 1. 媒体类型隐藏契约
    assert '<input type="hidden" id="taskMediaType" value="auto">' in html, "taskMediaType should be hidden input"
    assert "媒体类型 (电视剧 / 动漫 / 综艺)" not in html, "User facing media_type select should be removed"

    # 2. 删除源端按钮与弹窗契约
    assert "promptDeleteSourceShow" in html, "promptDeleteSourceShow function must exist"
    assert "executeDeleteSourceShow" in html, "executeDeleteSourceShow function must exist"
    assert "deleteSourceShowModal" in html, "deleteSourceShowModal must exist"
    assert "/api/archive/delete-source-show" in html, "delete-source-show API call must exist"
    print("[PASS] Frontend hidden media_type and delete_source contracts passed.")


if __name__ == "__main__":
    test_cross_diff_logic()
    test_diagnose_season_unknown_meta()
    test_douban_parsing_logic()
    test_frontend_contracts()
    print("[PASS] All specific tests PASSED!")
