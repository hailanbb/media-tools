"""归档目录初检与清理菜单。"""

import posixpath

from .interactive_inspect import ask, handle_interactive_inspect
from ..stages.review import run_review
from ..config import PROJECT_ROOT
from ..core.report_adapter import read_report


def load_archive_preview(report_path, root):
    session = read_report(report_path, mode="archive")
    plan = {}
    planned_files = 0
    for task in session.tasks:
        if task.data.get("复核结果") != "待确认清理（预览）":
            continue
        remove = sorted(filter(None, str(task.data.get("拟删除文件") or "").splitlines()))
        keep = sorted(filter(None, str(task.data.get("拟保留正片") or "").splitlines()))
        paths = remove + keep
        if (not remove or not keep or task.path in plan
                or not task.path.startswith(root + "/") or len(set(paths)) != len(paths)
                or "\\" in task.path or posixpath.normpath(task.path) != task.path
                or not all("\\" not in path and posixpath.normpath(path) == path
                           and posixpath.dirname(path) == task.path for path in paths)):
            raise ValueError("预览报告的文件明细不完整、重复或超出归档范围，请重新生成预览")
        plan[task.path] = {"remove": remove, "keep": keep}
        planned_files += len(remove)
    return {"outputPath": report_path, "plan": plan, "plannedFiles": planned_files}


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

    def select_report(preview_only=False):
        nonlocal report_path, preview
        directory = PROJECT_ROOT / "reports" / "归档目录"
        files = sorted(
            (path for path in directory.glob("归档目录初检报告*.xlsx")
             if path.is_file() and "_复核清理" not in path.name
             and (not preview_only or path.name.endswith("_复核预览.xlsx"))),
            key=lambda path: path.stat().st_mtime, reverse=True,
        )
        if not files:
            print("  暂无预览报告，请先选择初检报告并按 [2] 预览。" if preview_only
                  else "  暂无已有报告，请先按 [1] 初检。")
            return False
        for index, path in enumerate(files, start=1):
            print(f"  [{index}] {path.name}")
        answer = ask("  请选择已有报告序号（直接回车取消） > ")
        if not answer:
            return False
        if not answer.isascii() or not answer.isdigit() or not 1 <= int(answer) <= len(files):
            print("  无效报告序号。")
            return False
        selected = files[int(answer) - 1]
        loaded = load_archive_preview(str(selected), root) if selected.name.endswith("_复核预览.xlsx") else None
        report_path = str(selected)
        preview = loaded
        print(f"  已选择报告: {report_path}")
        if preview:
            print(f"  已恢复预览，拟删除 {preview['plannedFiles']} 个文件；执行前将重新核对。")
        return True

    while True:
        print(f"""
  归档目录检查与清理 · {root}
  [1] 初检并生成缺集报告
  [2] 预览重复版本清理
  [3] 按预览执行清理
  [4] 选择已有初检/预览报告
  [0] 返回主菜单
  当前报告: {report_path or '未选择（可读取已有报告）'}""")
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
            if not report_path and not select_report():
                continue
            preview = None
            preview = run_review(config, logger, report_path, {"archiveRoot": root})
            print(f"  拟删除 {preview['plannedFiles']} 个文件。请查看预览报告中的保留与删除明细。")
        elif choice == "3":
            if preview is None and not select_report(preview_only=True):
                continue
            if not preview or not preview.get("plannedFiles"):
                print("  当前预览没有可执行的删除项，可按 [2] 重新预览或按 [4] 切换报告。")
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
        elif choice == "4":
            select_report()
        else:
            print("  无效选择。")
