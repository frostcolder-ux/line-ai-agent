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


def reload_tasks(push_fn, get_config):
    """
    重新載入排程任務（例如管理員在後台修改設定後呼叫）。
    先移除所有現有任務，再重新註冊。
    """
    log("Reloading scheduled tasks...")
    for job in _scheduler.get_jobs():
        job.remove()
    setup_scheduler(push_fn, get_config)


def shutdown():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        log("Scheduler stopped")
