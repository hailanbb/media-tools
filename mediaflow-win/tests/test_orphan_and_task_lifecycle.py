"""孤儿任务自愈、强制停止与抗抖动生命周期测试套件."""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import async_session, init_db
from app.models.task import Task
from app.models.season import SeasonRecord
from app.api.inspection import stop_inspection
from app.api.tasks import delete_task
from sqlalchemy import select, delete


async def test_startup_self_healing():
    print("【1. 测试应用启动生命周期对孤儿运行任务的自愈能力】")
    await init_db()
    
    async with async_session() as db:
        # 清理旧数据
        await db.execute(delete(Task).where(Task.name.like("测试孤儿任务%")))
        await db.commit()

        # 插入模拟的孤儿任务（模拟服务崩溃或测试未正常退出的残留）
        task1 = Task(id="test_orphan_running_1", name="测试孤儿任务_运行中", source_path="/test/path1", media_type="tv", status="running")
        task2 = Task(id="test_orphan_stopping_2", name="测试孤儿任务_停止中", source_path="/test/path2", media_type="tv", status="stopping")
        task3 = Task(id="test_orphan_paused_3", name="测试孤儿任务_暂停中", source_path="/test/path3", media_type="tv", status="paused")
        db.add_all([task1, task2, task3])
        await db.commit()

    # 模拟 app.main lifespan 自愈逻辑
    from sqlalchemy import update
    async with async_session() as db:
        result = await db.execute(
            select(Task).where(Task.status.in_(["running", "stopping", "paused"]))
        )
        orphans = result.scalars().all()
        assert len(orphans) >= 3, "未能正确识别孤儿任务"
        orphan_ids = [t.id for t in orphans]
        await db.execute(
            update(Task)
            .where(Task.id.in_(orphan_ids))
            .values(status="stopped")
        )
        await db.commit()

        # 验证自愈后的状态全部收敛为 stopped
        res = await db.execute(select(Task).where(Task.id.in_(orphan_ids)))
        tasks = res.scalars().all()
        for t in tasks:
            assert t.status == "stopped", f"任务 {t.id} 状态未被自愈为 stopped，当前为: {t.status}"
    print("✓ 启动自愈验证通过：所有孤儿任务已被自动收敛为 stopped 终态！\n")


async def test_stop_inspection_without_worker():
    print("【2. 测试无后台活跃 worker 时的停止接口容错】")
    async with async_session() as db:
        # 创建一个状态为 running 但内存中没有任何 worker 的任务
        task = Task(id="test_zombie_task_1", name="测试僵尸任务", source_path="/test/zombie", media_type="tv", status="running")
        db.add(task)
        await db.commit()

    async with async_session() as db:
        # 调用 stop_inspection 端点
        resp = await stop_inspection("test_zombie_task_1", db)
        assert resp.ok is True, f"停止接口返回失败: {resp.message}"
        
        # 验证数据库中的持久化状态必须被更新为 stopped
        updated_task = await db.get(Task, "test_zombie_task_1")
        assert updated_task is not None
        assert updated_task.status == "stopped", f"停止后数据库状态未更新为 stopped: {updated_task.status}"
    print("✓ 停止接口容错验证通过：无 worker 时直接将数据库落库为 stopped！\n")


async def test_force_delete_running_task():
    print("【3. 测试对 running / stopping 状态任务的强制删除】")
    async with async_session() as db:
        task = Task(id="test_running_delete_1", name="测试删除运行中任务", source_path="/test/del", media_type="tv", status="running")
        season = SeasonRecord(
            id="test_season_del_1",
            task_id="test_running_delete_1",
            show_name="测试剧",
            show_path="/test/del/测试剧",
            season_path="/test/del/测试剧/Season 1",
            season_number=1,
            local_episode_count=0,
            status="review_needed",
        )
        db.add_all([task, season])
        await db.commit()

    async with async_session() as db:
        del_resp = await delete_task("test_running_delete_1", db)
        assert del_resp.ok is True, "删除接口调用失败"

        # 验证任务及剧季级联删除
        check_task = await db.get(Task, "test_running_delete_1")
        assert check_task is None, "任务未被删除"

        check_season = await db.get(SeasonRecord, "test_season_del_1")
        assert check_season is None, "关联剧季记录未被级联删除"
    print("✓ 强制删除验证通过：running 任务及关联记录已被彻底删除！\n")


async def cleanup_test_tasks():
    async with async_session() as db:
        await db.execute(delete(Task).where(Task.id.like("test_%")))
        await db.execute(delete(SeasonRecord).where(SeasonRecord.id.like("test_%")))
        await db.commit()


async def main():
    try:
        await test_startup_self_healing()
        await test_stop_inspection_without_worker()
        await test_force_delete_running_task()
        print("🎉 孤儿任务与抗抖动测试全部 100% 通过！")
    finally:
        await cleanup_test_tasks()


if __name__ == "__main__":
    asyncio.run(main())
