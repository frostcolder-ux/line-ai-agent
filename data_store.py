"""
farm data persistence — harvests & tasks
儲存格式：farm_data.json（JSON 檔）
注意：Render 免費方案每次重新部署會重置檔案系統，
      正式上線建議改用 PostgreSQL（Render 有免費方案）。
"""
import os
import json
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(__file__)
DATA_PATH = os.path.join(BASE_DIR, "farm_data.json")


def log(msg: str):
    print(f"[DATA] {msg}", flush=True, file=sys.stderr)


# ── 底層讀寫 ─────────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"harvests": [], "tasks": []}


def _save(data: dict):
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── 採收記錄 ──────────────────────────────────────────────────────────────────

def record_harvest(location: str, crop: str, amount: float, unit: str) -> dict:
    """新增一筆採收記錄，回傳完整記錄 dict。"""
    data = _load()
    record = {
        "id": len(data["harvests"]) + 1,
        "location": location,
        "crop": crop,
        "amount": amount,
        "unit": unit,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    data["harvests"].append(record)
    _save(data)
    log(f"Harvest recorded: {location} {crop} {amount}{unit}")
    return record


def query_harvests(location: str = None, crop: str = None) -> list:
    """查詢採收記錄，可依地點或作物篩選，回傳最近 20 筆。"""
    records = _load()["harvests"]
    if location:
        records = [r for r in records if location in r.get("location", "")]
    if crop:
        records = [r for r in records if crop in r.get("crop", "")]
    return records[-20:]


# ── 待辦事項 ──────────────────────────────────────────────────────────────────

def add_task(description: str, deadline: str) -> dict:
    """新增待辦事項，回傳完整 task dict。"""
    data = _load()
    task = {
        "id": len(data["tasks"]) + 1,
        "description": description,
        "deadline": deadline,
        "status": "pending",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    data["tasks"].append(task)
    _save(data)
    log(f"Task added: {description} (deadline: {deadline})")
    return task


def list_tasks(status: str = "pending") -> list:
    """列出待辦事項，預設只列出 pending 狀態。"""
    tasks = _load()["tasks"]
    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    return tasks
