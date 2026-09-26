"""分身（老闆本機的代理人）與小凡之間的核准往返。

分身需要老闆同意時，透過 /internal/agent-result 私訊老闆：
    「#12 要寄出基隆市政府的報價單嗎？… 回「同意 #12」「不要 #12」「改：… #12」或「先放 #12」」
「先放」是他還沒想好：分身把那件擱著、不再提醒，其他事照做。
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

REPLY_RE = re.compile(r"(先放|等等|再想|想一下|同意|可以|好|ok|OK|不要|不行|取消|改)[^#\n]{0,300}#\s*\d+")


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


# ── 半雲端：總管的任務摘要放在這裡，電腦關著小凡照樣能早晚報、回答「總管狀態」 ──────────
# 他 2026-09-26 選「半雲端」。總管（本機）每 10 分鐘 POST /internal/agent-snapshot 一份摘要；
# 小凡只負責「講」：早報 07:45、晚報 21:15、老闆私訊「總管狀態」時回覆。做事還是本機的總管。
SNAP_PATH = os.path.join(BASE_DIR, "agent_snapshot.json")
SENT_PATH = os.path.join(BASE_DIR, "agent_sent.json")


def _now_tw() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Taipei")).replace(tzinfo=None)


def save_snapshot(data: dict) -> None:
    text = json.dumps(data, ensure_ascii=False)
    at = _now_tw().strftime("%Y-%m-%d %H:%M:%S")
    if _use_pg():
        from db import get_conn
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""INSERT INTO agent_snapshot (id, data, updated_at) VALUES (1, %s, %s)
                       ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = EXCLUDED.updated_at""", (text, at))
        conn.commit()
        cur.close()
        conn.close()
        return
    with open(SNAP_PATH, "w", encoding="utf-8") as f:
        f.write(text)


def load_snapshot() -> dict:
    if _use_pg():
        from db import get_conn
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT data FROM agent_snapshot WHERE id = 1")
        row = cur.fetchone()
        cur.close()
        conn.close()
        return json.loads(row[0]) if row else {}
    try:
        with open(SNAP_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def claim_send(key: str) -> bool:
    """同一份報告一天只發一次：gunicorn 多個 worker 各自有排程器，先搶到的發，其他跳過。"""
    at = _now_tw().strftime("%Y-%m-%d %H:%M:%S")
    if _use_pg():
        from db import get_conn
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("INSERT INTO agent_sent (key, at) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", (key, at))
        ok = cur.rowcount == 1
        conn.commit()
        cur.close()
        conn.close()
        return ok
    try:
        with open(SENT_PATH, encoding="utf-8") as f:
            sent = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        sent = {}
    if key in sent:
        return False
    sent[key] = at
    with open(SENT_PATH, "w", encoding="utf-8") as f:
        json.dump(sent, f)
    return True


_WEEK = "一二三四五六日"


def _money(v) -> str:
    return f"NT${round(v or 0):,}"


def _freshness(snap: dict) -> str:
    at = snap.get("generated_at", "")
    if not at:
        return "（還沒有收到總管的資料）"
    try:
        age = _now_tw() - datetime.strptime(at, "%Y-%m-%d %H:%M")
    except ValueError:
        return f"（資料更新於 {at}）"
    if age > timedelta(hours=24):
        return f"注意：電腦超過一天沒開，以下是 {at[5:]} 的資料；總管要等電腦開機才會繼續做事。"
    return f"（資料更新於 {at[5:]}）"


def _approvals(snap: dict) -> list:
    aps = [a for a in snap.get("approvals", []) if a.get("status") == "pending"]
    lines = [f"#{a['id']} {a['question']}" for a in aps[:6]]
    parked = len([a for a in snap.get("approvals", []) if a.get("status") == "parked"])
    if parked:
        lines.append(f"（另有 {parked} 件你先放著的）")
    return lines


def _boss_tasks(snap: dict, until: str) -> list:
    out = []
    for t in snap.get("tasks", []):
        if t.get("assignee") == "boss":
            due = t.get("due") or ""
            if not due or due <= until:
                tail = f"（期限 {due[5:]}）" if due else ""
                out.append(f"#{t['id']} {t['title']}{tail}")
    return out[:6]


def _overdue(snap: dict, today: str) -> list:
    return [f"#{t['id']} {t['title']}（{t['due'][5:]}）" for t in snap.get("tasks", [])
            if t.get("due") and t["due"] < today and t.get("assignee") != "boss"][:5]


def morning_text(snap: dict) -> str:
    now = _now_tw()
    today, tomorrow = now.strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d")
    parts = [f"【總管早報 {now.month}/{now.day}（{_WEEK[now.weekday()]}）】", _freshness(snap)]
    note = (snap.get("notes") or {}).get("morning", {})
    if note.get("date") == today:
        parts += ["", note.get("text", "")]
    aps = _approvals(snap)
    if aps:
        parts += ["", "等你決定（回「同意 #編號」「不要 #編號」「先放 #編號」）："] + aps
    mine = _boss_tasks(snap, tomorrow)
    if mine:
        parts += ["", "要你親自做（今天、明天到期或沒期限）："] + mine
    late = _overdue(snap, today)
    if late:
        parts += ["", "過期還沒做完的："] + late
    fin = snap.get("finance") or {}
    if fin.get("overdue"):
        parts += ["", "逾期未入帳：" + "、".join(f"{o['counterparty']} {_money(o['amount'])}" for o in fin["overdue"][:4])]
    if fin.get("pending_docs"):
        parts.append(f"收支資料夾有 {fin['pending_docs']} 張單據等記帳")
    if snap.get("new_directives"):
        parts.append(f"還有 {snap['new_directives']} 則你的交代等總管處理")
    return "\n".join(parts)


def evening_text(snap: dict) -> str:
    now = _now_tw()
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    parts = [f"【總管晚報 {now.month}/{now.day}】", _freshness(snap)]
    done = snap.get("done_24h", [])
    parts += ["", "今天做完：" if done else "今天沒有做完的任務。"] + [f"#{t['id']} {t['title']}" for t in done[:8]]
    note = (snap.get("notes") or {}).get("evening", {})
    if note.get("date") == now.strftime("%Y-%m-%d"):
        parts += ["", note.get("text", "")]
    stuck = [t for t in snap.get("tasks", []) if t.get("status") in ("waiting_approval", "waiting_local")]
    if stuck:
        pending = len([a for a in snap.get("approvals", []) if a.get("status") == "pending"])
        parts += ["", f"卡住等人：{len(stuck)} 件（等你決定 {pending} 件）"]
    due = [f"#{t['id']} {t['title']}" for t in snap.get("tasks", []) if t.get("due") == tomorrow]
    if due:
        parts += ["", "明天到期："] + due[:6]
    return "\n".join(parts)


def status_text(snap: dict) -> str:
    """老闆私訊「總管狀態」時的回覆：短。"""
    now = _now_tw()
    tasks = snap.get("tasks", [])
    running = [t for t in tasks if t.get("status") == "running"]
    parts = [f"【總管狀態】{_freshness(snap)}",
             f"進行中 {len(running)} 件、排隊 {len([t for t in tasks if t.get('status') in ('todo', 'proposed')])} 件"]
    for t in running[:5]:
        parts.append(f"・#{t['id']} {t['title']} {t.get('pct', 0)}%" + (f"｜下一步：{t['next_step']}" if t.get("next_step") else ""))
    aps = _approvals(snap)
    if aps:
        parts += ["等你決定："] + aps
    mine = _boss_tasks(snap, (now + timedelta(days=7)).strftime("%Y-%m-%d"))
    if mine:
        parts += ["要你親自做（7 天內）："] + mine
    return "\n".join(parts)


STATUS_RE = re.compile(r"^\s*(總管狀態|總管進度|總管今天|總管)\s*[?？]?\s*$")


def is_status_query(text: str) -> bool:
    return bool(STATUS_RE.match(text or ""))


def setup_agent_jobs(scheduler, notify_boss) -> None:
    from apscheduler.triggers.cron import CronTrigger

    def _send(kind: str, build) -> None:
        key = f"{kind}:{_now_tw().strftime('%Y-%m-%d')}"
        try:
            snap = load_snapshot()
            if not snap or not claim_send(key):
                return
            notify_boss(build(snap)[:4800])
            log(f"sent {key}")
        except Exception as e:
            log(f"{key} error: {type(e).__name__}: {e}")

    scheduler.add_job(lambda: _send("morning", morning_text), CronTrigger(hour=7, minute=45, timezone="Asia/Taipei"),
                      id="agent_morning", replace_existing=True)
    scheduler.add_job(lambda: _send("evening", evening_text), CronTrigger(hour=21, minute=15, timezone="Asia/Taipei"),
                      id="agent_evening", replace_existing=True)
    log("agent brief jobs scheduled (07:45 / 21:15)")
