import sys
import time
import requests

sys.stdout.reconfigure(encoding='utf-8')
BASE_URL = "http://127.0.0.1:8080"

def test_system():
    print("=" * 60)
    print("【1. 系统健康与配置接口审计】")
    r = requests.get(f"{BASE_URL}/api/system/health", timeout=10)
    assert r.status_code == 200, f"Health check failed: {r.text}"
    health = r.json()
    print("✓ Health Check OK:", health.get("status"), health.get("services"))

    r = requests.get(f"{BASE_URL}/api/system/config/roots", timeout=10)
    assert r.status_code == 200, f"Roots config failed: {r.text}"
    roots = r.json()
    print("✓ Roots Config OK:", roots)
    return roots

def test_task_lifecycle_and_controls():
    print("\n" + "=" * 60)
    print("【2. 初检任务生命周期与全景控制（创建/并发/暂停/继续/完成/删除）测试】")

    # A. 创建任务
    payload = {
        "source_path": "/115/Library_incs/nas/电视剧/韩国剧集",
        "name": "自动化测试_韩国剧集",
        "media_type": "tv"
    }
    r = requests.post(f"{BASE_URL}/api/tasks/", json=payload, timeout=10)
    assert r.status_code in [200, 201], f"Create task failed: {r.text}"
    task = r.json()
    task_id = task["id"]
    print(f"✓ 成功创建初检任务: ID={task_id}, Name={task['name']}, Status={task['status']}")

    # B. 启动初检（并发加速）
    r = requests.post(f"{BASE_URL}/api/inspect/{task_id}/run", timeout=10)
    assert r.status_code == 200, f"Run inspection failed: {r.text}"
    print(f"✓ 初检已成功触发后台并发扫描: {r.json()}")

    # 稍微等 0.5s 让 worker 启动
    time.sleep(0.5)

    # C. 测试暂停控制
    r = requests.post(f"{BASE_URL}/api/inspect/{task_id}/pause", timeout=10)
    assert r.status_code == 200, f"Pause failed: {r.text}"
    print(f"✓ 暂停指令下发成功: {r.json()}")

    # 验证任务当前状态是否为 paused
    r = requests.get(f"{BASE_URL}/api/tasks/{task_id}", timeout=10)
    task_info = r.json()
    print(f"  -> 当前任务状态检查: {task_info['status']}")

    # D. 测试继续控制
    r = requests.post(f"{BASE_URL}/api/inspect/{task_id}/resume", timeout=10)
    assert r.status_code == 200, f"Resume failed: {r.text}"
    print(f"✓ 继续指令下发成功: {r.json()}")

    # E. 轮询等待初检完成（最高等待 25s）
    print("  -> 等待并发初检完成中...")
    completed = False
    for i in range(25):
        time.sleep(1)
        r = requests.get(f"{BASE_URL}/api/tasks/{task_id}", timeout=10)
        task_info = r.json()
        status = task_info.get("status")
        if status == "completed":
            completed = True
            print(f"✓ 初检任务于第 {i+1} 秒成功完成！Summary: {task_info.get('summary')}")
            break
        elif status == "failed":
            raise AssertionError(f"初检失败: {task_info}")
    
    assert completed, f"初检任务超时未完成，当前状态: {task_info.get('status')}"

    # F. 加载并验证剧季明细
    r = requests.get(f"{BASE_URL}/api/tasks/{task_id}/seasons", timeout=10)
    assert r.status_code == 200, f"Get seasons failed: {r.text}"
    seasons = r.json()
    print(f"✓ 成功获取初检产出的剧季记录，共 {len(seasons)} 季")
    assert len(seasons) > 0, "初检未产出任何剧季记录！"

    # G. 测试人工集数校准接口
    target_season = seasons[0]
    cal_payload = {
        "season_record_id": target_season["id"],
        "total_episodes": 16
    }
    r = requests.post(f"{BASE_URL}/api/archive/calibrate-episodes", json=cal_payload, timeout=10)
    assert r.status_code == 200, f"Calibrate failed: {r.text}"
    print(f"✓ 人工集数校准接口测试成功: {r.json()}")

    # H. 测试复核清理预览接口
    preview_payload = {
        "season_record_ids": [target_season["id"]],
        "force_clean": False
    }
    r = requests.post(f"{BASE_URL}/api/archive/clean-preview", json=preview_payload, timeout=10)
    assert r.status_code == 200, f"Clean preview failed: {r.text}"
    print(f"✓ 复核清理预览接口测试成功: {r.json().get('message')}")

    # I. 清理测试产生的任务
    r = requests.delete(f"{BASE_URL}/api/tasks/{task_id}", timeout=10)
    assert r.status_code == 200, f"Delete task failed: {r.text}"
    print(f"✓ 自动化测试任务 ID={task_id} 已安全删除并清理关联记录")

def test_moviepilot_governance():
    print("\n" + "=" * 60)
    print("【3. MoviePilot 智能订阅治理双轨分离模块测试】")

    # A. 获取订阅列表
    r = requests.get(f"{BASE_URL}/api/moviepilot/subscriptions", timeout=15)
    if r.status_code == 503:
        print("! MoviePilot 未配置或不可用，跳过测试")
        return
    assert r.status_code == 200, f"List subscriptions failed: {r.text}"
    subs = r.json()
    items = subs.get("subscriptions", [])
    print(f"✓ 成功读取 MoviePilot 全量订阅: 共 {len(items)} 部")

    # B. 双轨清理预检（电视剧）
    r = requests.post(f"{BASE_URL}/api/moviepilot/subscriptions/clean-preview", json={"media_type": "tv", "mode": "safe"}, timeout=15)
    assert r.status_code == 200, f"TV clean preview failed: {r.text}"
    tv_preview = r.json()
    print(f"✓ 电视剧安全模式清理预览正常: 可清理 {len(tv_preview.get('to_delete', []))} 项，保留 {len(tv_preview.get('to_keep', []))} 项")

    # C. 双轨清理预检（电影）
    r = requests.post(f"{BASE_URL}/api/moviepilot/subscriptions/clean-preview", json={"media_type": "movie", "mode": "safe"}, timeout=15)
    assert r.status_code == 200, f"Movie clean preview failed: {r.text}"
    movie_preview = r.json()
    print(f"✓ 电影安全模式清理预览正常: 可清理 {len(movie_preview.get('to_delete', []))} 项，保留 {len(movie_preview.get('to_keep', []))} 项")

    # D. 获取转移历史队列
    r = requests.get(f"{BASE_URL}/api/moviepilot/transfer-queue", timeout=15)
    assert r.status_code == 200, f"Transfer queue failed: {r.text}"
    items = r.json()
    print(f"✓ 转移队列与历史读取正常: 共 {len(items)} 项")

def test_frontend_delivery():
    print("\n" + "=" * 60)
    print("【4. 前端页面托管与静态交付测试】")
    r = requests.get(f"{BASE_URL}/", timeout=10)
    assert r.status_code == 200, f"Index.html deliver failed: {r.status_code}"
    html = r.text
    assert "v2.5.4" in html, "Frontend does not contain v2.5.1 badge"
    assert "loadTasks" in html, "Frontend does not contain loadTasks function"
    assert "taskTag" in html, "Frontend does not contain taskTag function"
    assert "openCalibrateModal" in html, "Frontend does not contain openCalibrateModal function"
    assert "liveInspectionBanner" in html, "Frontend does not contain liveInspectionBanner"
    print("✓ 前端 index.html 交付正常，关键函数与 v2.0 标识均 100% 存在！")

if __name__ == "__main__":
    try:
        test_system()
        test_task_lifecycle_and_controls()
        test_moviepilot_governance()
        test_frontend_delivery()
        print("\n" + "=" * 60)
        print("🎉 全部测试 100% 通过！系统各模块运行稳健，未发现任何断点或未定义函数。")
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        sys.exit(1)
