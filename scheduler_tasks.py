"""
排程推播模組 — Scheduled Tasks
使用 APScheduler BackgroundScheduler，在指定時間自動發送 LINE Push Message。

從 config.json 的 "scheduled_tasks" 陣列讀取任務設定，支援動態重新載入。
"""
import sys
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

def log(msg: str):
    print(f"[SCHEDULER] {msg}", flush=True, file=sys.stderr)

# 全域 scheduler 實例（程式生命週期內只建立一次）
_scheduler = BackgroundScheduler(timezone="Asia/Taipei")


def _make_job(push_fn, group_id: str, message: str, name: str):
    """
    為每個排程任務建立一個獨立的閉包函式。
    注意：必須用閉包（closure）而非 lambda，
    避免所有 job 都參照同一個變數。
    """
    def _job():
        log(f"Running scheduled task: {name}")
        try:
            push_fn(group_id, message)
            log(f"  ✓ Pushed to {group_id}")
        except Exception as e:
            log(f"  ✗ Failed: {e}")
    _job.__name__ = f"task_{name}"
    return _job


def setup_scheduler(push_fn, get_config):
    """
    初始化排程器，從 config 讀取任務並註冊。

    config.json 中 scheduled_tasks 的格式範例：
    [
      {
        "id": "weekly_contract",           # 唯一識別碼
        "name": "每週契作進度催收",          # 顯示名稱
        "group_id": "C...",                 # LINE 群組 ID
        "message": "📋 請回報本週進度...",   # 推播內容
        "enabled": true,                    # 是否啟用
        "cron": {                           # APScheduler CronTrigger 參數
          "day_of_week": "fri",
          "hour": 15,
          "minute": 0
        }
      }
    ]

    cron 欄位對應 APScheduler 的 CronTrigger，支援：
      second, minute, hour, day, month, day_of_week
      day_of_week: mon/tue/wed/thu/fri/sat/sun 或 0-6
    """
    cfg = get_config()
    tasks = cfg.get("scheduled_tasks", [])

    registered = 0
    for task in tasks:
        if not task.get("enabled", True):
            continue

        task_id  = task.get("id", "")
        name     = task.get("name", task_id)
        group_id = task.get("group_id", "").strip()
        message  = task.get("message", "").strip()
        cron_cfg = task.get("cron", {})

        if not group_id or not message or not cron_cfg:
            log(f"Skip incomplete task: {task_id} (missing group_id / message / cron)")
            continue

        job_fn = _make_job(push_fn, group_id, message, name)

        _scheduler.add_job(
            job_fn,
            CronTrigger(**cron_cfg, timezone="Asia/Taipei"),
            id=task_id,
            replace_existing=True,
            name=name,
        )
        log(f"Registered: [{task_id}] {name} → cron={cron_cfg}")
        registered += 1

    if not _scheduler.running:
        _scheduler.start()
        log(f"Scheduler started with {registered} task(s)")
    else:
        log(f"Scheduler already running, added/updated {registered} task(s)")

    return _scheduler


def _primary_group_id(get_config) -> str:
    """從 config 的 scheduled_tasks 找出主要契作群組 ID（第一個有 group_id 的）。"""
    for t in get_config().get("scheduled_tasks", []):
        gid = (t.get("group_id") or "").strip()
        if gid:
            return gid
    return ""


def setup_radar(notify_boss_fn, push_fn, list_tasks_fn, get_config):
    """
    註冊「營運雷達」動態排程任務（讀農場回報系統真實資料後主動推播）。
    與 setup_scheduler 的靜態任務並存，共用同一個 _scheduler。

    時間（Asia/Taipei）：
      週一 08:00        weekly_boss_digest    老闆週報進度私訊
      每天 07:30        daily_morning_report  老闆晨報
      週五 15:00        friday_group_remind   群組精準催繳
    """
    import radar

    if not _scheduler.running:
        _scheduler.start()

    # 週一老闆週報進度
    _scheduler.add_job(
        lambda: radar.weekly_boss_digest(notify_boss_fn),
        CronTrigger(day_of_week="mon", hour=8, minute=0, timezone="Asia/Taipei"),
        id="radar_weekly_boss_digest", replace_existing=True,
        name="週一老闆週報進度",
    )

    # 每日晨報
    _scheduler.add_job(
        lambda: radar.daily_morning_report(notify_boss_fn, list_tasks_fn),
        CronTrigger(hour=7, minute=30, timezone="Asia/Taipei"),
        id="radar_daily_morning", replace_existing=True,
        name="老闆每日晨報",
    )

    # 週五群組精準催繳
    group_id = _primary_group_id(get_config)
    if group_id:
        _scheduler.add_job(
            lambda: radar.friday_group_remind(push_fn, group_id),
            CronTrigger(day_of_week="fri", hour=15, minute=0, timezone="Asia/Taipei"),
            id="radar_friday_remind", replace_existing=True,
            name="週五群組精準催繳",
        )
        log(f"Radar registered: friday remind → {group_id}")
    else:
        log("Radar: 找不到群組 ID，跳過週五催繳（可在 config 加 scheduled_tasks）")

    log("Radar jobs registered ✓ (週一08:00 / 每日07:30 / 週五15:00)")
    return _scheduler


def add_group_task(push_fn, group_id: str, group_name: str = ""):
    """
    動態新增一個群組的週排程任務，同時更新 config.json 並立即生效。
    小凡被加入新群組時呼叫此函式。
    """
    import json, os
    config_path = os.path.join(os.path.dirname(__file__), "config.json")

    # 讀取現有設定
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}

    tasks = cfg.setdefault("scheduled_tasks", [])

    # 避免重複加入
    if any(t.get("group_id") == group_id for t in tasks):
        log(f"Group {group_id} already has a scheduled task, skipping")
        return False

    task_id = f"auto_{group_id[-8:]}"
    display = group_name or group_id[-8:]
    new_task = {
        "id": task_id,
        "name": f"週報催收－{display}",
        "group_id": group_id,
        "message": "📋 各位夥伴好，麻煩今天下班前回報本週契作進度，謝謝配合！🌿",
        "enabled": True,
        "cron": {"day_of_week": "fri", "hour": 15, "minute": 0},
    }
    tasks.append(new_task)

    # 寫回 config.json
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    log(f"New group task saved to config: {task_id}")

    # 立即註冊到執行中的 scheduler
    if _scheduler.running:
        job_fn = _make_job(push_fn, group_id, new_task["message"], new_task["name"])
        _scheduler.add_job(
            job_fn,
            CronTrigger(day_of_week="fri", hour=15, minute=0, timezone="Asia/Taipei"),
            id=task_id,
            replace_existing=True,
            name=new_task["name"],
        )
        log(f"Scheduler job registered immediately: {task_id}")

    return True


def reload_tasks(push_fn, get_config):
    """重新載入排程任務。"""
    log("Reloading scheduled tasks...")
    for job in _scheduler.get_jobs():
        job.remove()
    setup_scheduler(push_fn, get_config)


def shutdown():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        log("Scheduler stopped")
