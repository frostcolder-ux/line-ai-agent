"""分身（老闆本機的代理人）與小凡之間的核准往返。

分身需要老闆同意時，透過 /internal/agent-result 私訊老闆：
    「#12 要寄出基隆市政府的報價單嗎？… 回「同意 #12」「不要 #12」或「改：… #12」」
老闆在 LINE 私訊回覆 → 這裡存起來 → 分身巡檢時打 /internal/boss-replies 拉回去。

為什麼一定要帶「#數字」才算：老闆原本就會打「同意 A7K2」核准群組（access.parse_boss_command），
兩種指令長得很像。群組代號沒有 #，分身的編號一定有 #，這樣兩邊不會互搶。

跟 work_capture 一樣：有 DATABASE_URL 走 Postgres，沒有就退回本機 JSON。
Render 會重啟，所以一定要落地，不能放記憶體。
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(__file__)
JSON_PATH = os.path.join(BASE_DIR, "agent_replies.json")

REPLY_RE = re.compile(r"(同意|可以|好|ok|OK|不要|不行|取消|改)[^#\n]{0,300}#\s*\d+")


def log(msg: str):
    print(f"[AGENT] {msg}", file=sys.stderr, flush=True)


def looks_like_reply(text: str) -> bool:
    return bool(REPLY_RE.search(text or ""))


def _use_pg() -> bool:
    from db import is_postgres
    return is_postgres()


def save_reply(text: str) -> None:
    at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if _use_pg():
        from db import get_conn
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("INSERT INTO agent_replies (text, created_at) VALUES (%s, %s)", (text, at))
        conn.commit()
        cur.close()
        conn.close()
        return
    rows = _load()
    rows.append({"id": len(rows) + 1, "text": text, "at": at})
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def _load() -> list:
    try:
        with open(JSON_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def list_replies(days: int = 7) -> list:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    if _use_pg():
        from db import get_conn
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, text, created_at FROM agent_replies WHERE created_at >= %s ORDER BY id",
                    (since,))
        rows = [{"id": r[0], "text": r[1], "at": r[2]} for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    return [r for r in _load() if r.get("at", "") >= since]
