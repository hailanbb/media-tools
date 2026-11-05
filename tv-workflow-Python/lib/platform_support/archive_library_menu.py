"""归档目录初检与清理菜单。"""

import posixpath

from .interactive_inspect import ask, handle_interactive_inspect
from ..stages.review import run_review


def handle_archive_library_menu(config, logger):
    paths = config.get("paths", {}) if isinstance(config, dict) else getattr(config, "paths", {})
    root = (paths.get("destination") or "").rstrip("/")
    source = (paths.get("source") or "").rstrip("/")
    if (not root.startswith("/") or not source.startswith("/") or root == source
            or root.startswith(source + "/") or source.startswith(root + "/")
            or "\\" in root or posixpath.normpath(root) != root):
        raise ValueError("请先设置与源目录分开的有效归档目标 paths.destination")

    report_path = None
    preview = None
    while True:
        print(f"""
  归档目录检查与清理 · {root}
  [1] 初检并生成缺集报告
  [2] 预览重复版本清理
  [3] 按预览执行清理
  [0] 返回主菜单
  当前初检报告: {report_path or '尚未生成'}""")
        choice = ask("  请选择 > ")
        if choice in ("0", ""):
            return
        if choice == "1":
            result = handle_interactive_inspect(config, logger, archive_root=root)
            if result is None:
                continue
            preview = None
            report_path = result.get("outputPath")
            if report_path:
                print("  请在报告中查看「缺失集号」「重复集号」和「无法识别集号的文件」。")
        elif choice == "2":
            if not report_path:
                print("  请先在本菜单初检。")
                continue
            preview = None
            preview = run_review(config, logger, report_path, {"archiveRoot": root})
            print(f"  拟删除 {preview['plannedFiles']} 个文件。请查看预览报告中的保留与删除明细。")
        elif choice == "3":
            if not preview or not preview.get("plannedFiles"):
                print("  请先生成有删除项的预览。")
                continue
            confirmation = ask(
                f"  预览拟删除 {preview['plannedFiles']} 个文件。核对 {preview['outputPath']} 后，"
                "输入「确认删除」继续 > "
            )
            if confirmation != "确认删除":
                continue
            approved_plan = preview["plan"]
            preview = None
            run_review(config, logger, report_path, {
                "dryRun": False, "archiveRoot": root, "approvedPlan": approved_plan,
            })
            print("  清理结束。可再次选择 [1] 核对集号。")
        else:
            print("  无效选择。")
