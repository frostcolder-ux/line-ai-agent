"""
農場資料儲存 — 採收記錄 & 待辦事項
雙模式：有 DATABASE_URL → PostgreSQL；無 → 本機 JSON 檔。
"""
import os
import json
import sys
from datetime import datetime

BASE_DIR  = os.path.dirname(__file__)
DATA_PATH = os.path.join(BASE_DIR, "farm_data.json")


def log(msg: str):
    print(f"[DATA] {msg}", file=sys.stderr, flush=True)


def _use_pg() -> bool:
    from db import is_postgres
    return is_postgres()


# ══════════════════════════════════════════════════════════════════════════════
# JSON 檔底層
# ══════════════════════════════════════════════════════════════════════════════

def _load() -> dict:
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"harvests": [], "tasks": []}


def _save(data: dict):
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# 採收記錄
# ══════════════════════════════════════════════════════════════════════════════

def record_harvest(location: str, crop: str, amount: float, unit: str) -> dict:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if _use_pg():
        from db import get_conn
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute(
            "INSERT INTO farm_harvests (location, crop, amount, unit, timestamp) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (location, crop, amount, unit, now)
        )
        new_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        log(f"PG harvest: {location} {crop} {amount}{unit}")
        return {"id": new_id, "location": location, "crop": crop,
                "amount": amount, "unit": unit, "timestamp": now}
    else:
        data   = _load()
        record = {"id": len(data["harvests"]) + 1, "location": location,
                  "crop": crop, "amount": amount, "unit": unit, "timestamp": now}
        data["harvests"].append(record)
        _save(data)
        return record


def query_harvests(location: str = None, crop: str = None) -> list:
    if _use_pg():
        from db import get_conn
        conn = get_conn()
        cur  = conn.cursor()
        sql  = "SELECT id, location, crop, amount, unit, timestamp FROM farm_harvests WHERE 1=1"
        params = []
        if location:
            sql += " AND location LIKE %s"; params.append(f"%{location}%")
        if crop:
            sql += " AND crop LIKE %s";     params.append(f"%{crop}%")
        sql += " ORDER BY id DESC LIMIT 20"
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "location": r[1], "crop": r[2],
                 "amount": r[3], "unit": r[4], "timestamp": r[5]} for r in rows]
    else:
        records = _load()["harvests"]
        if location:
            records = [r for r in records if location in r.get("location", "")]
        if crop:
            records = [r for r in records if crop in r.get("crop", "")]
        return records[-20:]


# ══════════════════════════════════════════════════════════════════════════════
# 待辦事項
# ══════════════════════════════════════════════════════════════════════════════

def add_task(description: str, deadline: str) -> dict:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if _use_pg():
        from db import get_conn
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute(
            "INSERT INTO farm_tasks (description, deadline, status, created_at) "
            "VALUES (%s, %s, 'pending', %s) RETURNING id",
            (description, deadline, now)
        )
        new_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        log(f"PG task: {description}")
        return {"id": new_id, "description": description, "deadline": deadline,
                "status": "pending", "created_at": now}
    else:
        data = _load()
        task = {"id": len(data["tasks"]) + 1, "description": description,
                "deadline": deadline, "status": "pending", "created_at": now}
        data["tasks"].append(task)
        _save(data)
        return task


def list_tasks(status: str = "pending") -> list:
    if _use_pg():
        from db import get_conn
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute(
            "SELECT id, description, deadline, status, created_at FROM farm_tasks "
            "WHERE status = %s ORDER BY id DESC",
            (status,)
        )
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "description": r[1], "deadline": r[2],
                 "status": r[3], "created_at": r[4]} for r in rows]
    else:
        tasks = _load()["tasks"]
        return [t for t in tasks if t.get("status") == status]
