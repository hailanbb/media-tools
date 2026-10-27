"""
交互式定向/指定剧集初检导航器

支持媒体类型 (动漫/电视剧) -> 子分类 (日韩动漫/国产剧等) -> 剧集文件夹逐级选择。
根目录: /115/Library_incs/nas
完全适配 Ubuntu 终端标准输入输出（UTF-8 字符流与 ANSI 控制台）。
"""

import re
import sys
from typing import Any, List, Optional, Set

from ..clients.clouddrive2 import CloudDriveClient


def parse_indices(s: str, max_val: int) -> List[int]:
    """解析用户输入的数字序号列表（支持 1 或 1,3,5 或区间 1-3）"""
    parts = re.split(r"[\s,，]+", s.strip())
    result: Set[int] = set()
    for p in parts:
        if "-" in p:
            sub = p.split("-")
            if len(sub) == 2:
                try:
                    start = int(sub[0])
                    end = int(sub[1])
                    for i in range(min(start, end), max(start, end) + 1):
                        if 1 <= i <= max_val:
                            result.add(i)
                except ValueError:
                    pass
        else:
            try:
                n = int(p)
                if 1 <= n <= max_val:
                    result.add(n)
            except ValueError:
                pass
    return sorted(result)


def ask(prompt_str: str) -> str:
    """标准控制台安全输入"""
    try:
        sys.stdout.write(prompt_str)
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            return ""
        return line.strip()
    except (KeyboardInterrupt, EOFError):
        return ""


def handle_interactive_inspect(config: Any, logger: Any) -> None:
    """执行交互式剧集初检导航"""
    from ..stages.dispatcher import run_stage

    cd2_cfg = config.get("clouddrive2", {}) if isinstance(config, dict) else getattr(config, "clouddrive2", {})
    cd2 = CloudDriveClient(
        origin=cd2_cfg.get("origin", ""),
        token=cd2_cfg.get("token", ""),
        logger=logger,
    )

    paths_cfg = config.get("paths", {}) if isinstance(config, dict) else getattr(config, "paths", {})
    source_root = (paths_cfg.get("source") or "/115/Library_incs/nas").rstrip("/")

    print(f"\n⏳ 正在获取网盘媒体库根目录结构 ({source_root})...")
    try:
        root_entries = cd2.list_files(source_root)
    except Exception as e:
        logger.error(f"读取源目录失败: {e}")
        return

    # 过滤有效媒体类型目录（保留 动漫, 电视剧 等）
    valid_types = [
        f for f in root_entries
        if f.get("directory") and not re.search(r"mount-check|电影|^\.", f.get("name", ""), re.IGNORECASE)
    ]
    if not valid_types:
        print("⚠️ 源根目录下未发现有效媒体类型文件夹")
        return

    line_sep = "═" * 54
    print(f"\n{line_sep}")
    print(f"📁 可选媒体库类型 (根目录: {source_root})")
    print(line_sep)
    for idx, t in enumerate(valid_types, start=1):
        num = str(idx).rjust(2)
        print(f"  [{num}] {t.get('name')} ({t.get('path')})")
    print(line_sep)

    type_prompt = (
        "  👉 请选择媒体库类型:\n"
        "     • 输入序号 (如: 1 选动漫, 2 选电视剧)\n"
        "     • 输入名称 (如: 动漫 或 电视剧)\n"
        "     • 或直接输入剧名关键词/网盘绝对路径直达初检\n"
        "     (直接回车取消) > "
    )
    type_input = ask(type_prompt)
    if not type_input:
        print("  ⚠️ 操作已取消")
        return

    # 1. 直接输入网盘绝对路径
    if type_input.startswith("/"):
        logger.info(f"🎯 直接扫描指定路径: {type_input}")
        run_stage("inspect", config, logger, {"path": type_input})
        return

    # 2. 匹配媒体类型
    target_type = None
    try:
        type_idx = int(type_input)
        if 1 <= type_idx <= len(valid_types):
            target_type = valid_types[type_idx - 1]
    except ValueError:
        pass

    if not target_type:
        for t in valid_types:
            t_name = t.get("name", "")
            if t_name.lower() == type_input.lower() or type_input.lower() in t_name.lower():
                target_type = t
                break

    # 3. 若未匹配到类型，视为全局剧名关键词检索
    if not target_type:
        logger.info(f"🔍 未匹配到类型，按剧名关键词跨动漫/电视剧检索: \"{type_input}\"")
        run_stage("inspect", config, logger, {"show": type_input})
        return

    # 4. 获取该媒体类型下的子分类
    print(f"\n⏳ 正在获取 [{target_type.get('name')}] 下的子分类列表...")
    try:
        type_entries = cd2.list_files(target_type.get("path"))
        sub_categories = [f for f in type_entries if f.get("directory")]
    except Exception as e:
        logger.error(f"读取分类目录 {target_type.get('path')} 失败: {e}")
        return

    if not sub_categories:
        print(f"⚠️ [{target_type.get('name')}] 下未发现子分类")
        return

    print(f"\n{line_sep}")
    print(f"📂 [{target_type.get('name')}] 子分类列表:")
    print(line_sep)
    for idx, cat in enumerate(sub_categories, start=1):
        num = str(idx).rjust(2)
        print(f"  [{num}] {cat.get('name')}")
    print(line_sep)

    cat_prompt = (
        "  👉 请选择子分类以查看剧集:\n"
        "     • 输入分类序号 (如: 1)\n"
        "     • 输入分类名称 (如: 日韩动漫)\n"
        "     • 或输入剧名关键词快速查找\n"
        "     (直接回车取消) > "
    )
    cat_input = ask(cat_prompt)
    if not cat_input:
        print("  ⚠️ 操作已取消")
        return

    target_cat = None
    try:
        cat_idx = int(cat_input)
        if 1 <= cat_idx <= len(sub_categories):
            target_cat = sub_categories[cat_idx - 1]
    except ValueError:
        pass

    if not target_cat:
        for c in sub_categories:
            c_name = c.get("name", "")
            if c_name.lower() == cat_input.lower() or cat_input.lower() in c_name.lower():
                target_cat = c
                break

    if not target_cat:
        logger.info(f"🔍 按关键词在 [{target_type.get('name')}] 检索: \"{cat_input}\"")
        run_stage("inspect", config, logger, {"path": target_type.get("path"), "show": cat_input})
        return

    # 5. 列出该子分类下的具体剧集/动漫文件夹
    cat_name = target_cat.get("name")
    cat_path = target_cat.get("path")
    print(f"\n⏳ 正在获取 [{target_type.get('name')} / {cat_name}] 剧集列表...")
    try:
        cat_files = cd2.list_files(cat_path)
        show_dirs = [f for f in cat_files if f.get("directory")]
    except Exception as e:
        logger.error(f"读取目录 {cat_path} 失败: {e}")
        return

    if not show_dirs:
        print(f"⚠️ 分类 [{cat_name}] 下暂无剧集文件夹")
        return

    print(f"\n{line_sep}")
    print(f"📂 [{target_type.get('name')} / {cat_name}] 列表 (共 {len(show_dirs)} 部):")
    print(line_sep)

    max_display = 60
    display_count = min(len(show_dirs), max_display)
    for i in range(display_count):
        num = str(i + 1).rjust(3)
        print(f"  [{num}] {show_dirs[i].get('name')}")
    if len(show_dirs) > max_display:
        print(f"  ... 其余 {len(show_dirs) - max_display} 部已折叠 (直接输入剧名关键词可快速过滤) ...")
    print(line_sep)

    # 6. 提示选择或输入具体剧集
    show_prompt = (
        "  👉 请选择要初检的剧集/动漫:\n"
        "     • 输入序号 (单选如 1，多选如 1,3，或区间 1-5)\n"
        "     • 输入 \"all\" 初检该子分类全部剧集\n"
        "     • 输入剧名关键词或完整文件夹名称\n"
        "     (直接回车取消) > "
    )
    show_choice = ask(show_prompt)
    if not show_choice:
        print("  ⚠️ 操作已取消")
        return

    # 处理 "all"
    if show_choice.lower() == "all":
        logger.info(f"🎯 初检整子分类: [{target_type.get('name')} / {cat_name}] (共 {len(show_dirs)} 部)")
        run_stage("inspect", config, logger, {"path": cat_path})
        return

    # 处理数字序号
    is_number_selection = bool(re.match(r"^(\d+)([\s,，-]\d+)*$", show_choice))
    if is_number_selection:
        selected_indices = parse_indices(show_choice, len(show_dirs))
        if len(selected_indices) == 1:
            selected = show_dirs[selected_indices[0] - 1]
            logger.info(f"🎯 初检指定剧集: [{cat_name}] {selected.get('name')}")
            run_stage("inspect", config, logger, {"path": selected.get("path")})
            return
        elif len(selected_indices) > 1:
            print(f"🎯 已选中 {len(selected_indices)} 部:")
            for idx in selected_indices:
                print(f"   - {show_dirs[idx - 1].get('name')}")
            for idx in selected_indices:
                item = show_dirs[idx - 1]
                run_stage("inspect", config, logger, {"path": item.get("path")})
            return

    # 处理文本输入（完全匹配与模糊匹配）
    exact_match = next((d for d in show_dirs if d.get("name", "").lower() == show_choice.lower()), None)
    if exact_match:
        logger.info(f"🎯 匹配到文件夹: {exact_match.get('name')}")
        run_stage("inspect", config, logger, {"path": exact_match.get("path")})
        return

    fuzzy_matches = [d for d in show_dirs if show_choice.lower() in d.get("name", "").lower()]
    if len(fuzzy_matches) == 1:
        logger.info(f"🎯 关键词精准匹配到 1 个文件夹: {fuzzy_matches[0].get('name')}")
        run_stage("inspect", config, logger, {"path": fuzzy_matches[0].get("path")})
        return
    elif len(fuzzy_matches) > 1:
        print(f"\n🔍 关键词 \"{show_choice}\" 在该分类匹配到 {len(fuzzy_matches)} 个文件夹:")
        for idx, m in enumerate(fuzzy_matches, start=1):
            print(f"  [{idx}] {m.get('name')}")
        sub_choice = ask(f"  👉 请输入序号选择 (1-{len(fuzzy_matches)})，直接回车初检全部匹配项: ")
        try:
            sub_idx = int(sub_choice)
            if 1 <= sub_idx <= len(fuzzy_matches):
                pick = fuzzy_matches[sub_idx - 1]
                logger.info(f"🎯 初检指定剧集: {pick.get('name')}")
                run_stage("inspect", config, logger, {"path": pick.get("path")})
                return
        except ValueError:
            pass

        logger.info(f"🎯 初检全部 {len(fuzzy_matches)} 个匹配项...")
        for m in fuzzy_matches:
            run_stage("inspect", config, logger, {"path": m.get("path")})
        return

    # 均未匹配，按关键词初检
    logger.info(f"🎯 未在 [{cat_name}] 中发现同名文件夹，尝试按关键词初检: \"{show_choice}\"")
    run_stage("inspect", config, logger, {"path": cat_path, "show": show_choice})

