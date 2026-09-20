"""香草遊戲的通知名單。

官網的免費遊戲「一個產季」玩完一季後，會請玩家傳一句「香草通知」給思凡的
LINE 官方帳號。傳了就進這份名單，之後遊戲出新內容時只發給名單上的人。

★ 為什麼不用 LINE 登入
  登入要另開 LINE Login channel、要處理回呼與 session，而且玩家多半是在 LINE
  內建瀏覽器裡打開遊戲的。「加好友＋傳一句話」在那個情境下只要點兩下，
  同意也最清楚——是他自己傳的那句話。

★ 為什麼回覆不花錢
  他先傳訊息給我們，我們用 reply token 回覆，這不計入官方帳號的訊息則數。
  之後主動發新內容通知才會算，而且只算名單上的人。

★ 個資
  只存 LINE 的使用者代碼與時間，不存姓名、不存遊戲存檔，也不跟遊戲進度綁在一起。
  傳「取消香草通知」或封鎖官方帳號就停止；取消時直接把那一筆刪掉。
"""
import json
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(__file__)
JSON_PATH = os.path.join(BASE_DIR, "herb_notify.json")

SUBSCRIBE_WORDS = ("香草通知", "我要香草通知", "訂閱香草通知")
UNSUBSCRIBE_WORDS = ("取消香草通知", "停止香草通知", "退出香草通知")

WELCOME = """好，新內容開放時我會傳一則訊息給你。

我只留下你的 LINE 帳號代碼和時間，不會看你的遊戲進度，也不會用在別的地方。
不想收的時候，傳「取消香草通知」或封鎖我就好。

還沒玩過的話：https://www.selvansimpact.com/herb-season"""

BYE = "好，我把你從香草遊戲的通知名單移除了，之後不會再發。想再收到就傳「香草通知」。"


def log(msg: str):
    print(f"[HERB] {msg}", file=sys.stderr, flush=True)


def _use_pg() -> bool:
    from db import is_postgres
    return is_postgres()


def _load_json() -> dict:
    try:
        with open(JSON_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_json(data: dict):
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def wants(text: str) -> str | None:
    """這句話是要訂閱還是取消。都不是就回 None。

    先看取消——「取消香草通知」裡面也有「香草通知」四個字。
    """
    t = (text or "").strip()
    if any(w in t for w in UNSUBSCRIBE_WORDS):
        return "off"
    if any(w in t for w in SUBSCRIBE_WORDS):
        return "on"
    return None


def subscribe(user_id: str) -> bool:
    """加進名單。回傳 True 代表這次才加入（之前就在名單上就回 False）。"""
    if not user_id:
        return False
    if not _use_pg():
        data = _load_json()
        fresh = user_id not in data
        data[user_id] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save_json(data)
        return fresh
    from db import get_conn
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO herb_notify (user_id, created_at) VALUES (%s, %s)
           ON CONFLICT (user_id) DO NOTHING""",
        (user_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    fresh = cur.rowcount == 1
    conn.commit()
    cur.close()
    conn.close()
    log(f"subscribe {user_id[:10]}... fresh={fresh}")
    return fresh


def unsubscribe(user_id: str) -> bool:
    """從名單移除。取消就是刪掉，不留「已取消」的紀錄——留著也沒有用途。"""
    if not user_id:
        return False
    if not _use_pg():
        data = _load_json()
        existed = data.pop(user_id, None) is not None
        _save_json(data)
        return existed
    from db import get_conn
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM herb_notify WHERE user_id = %s", (user_id,))
    existed = cur.rowcount == 1
    conn.commit()
    cur.close()
    conn.close()
    log(f"unsubscribe {user_id[:10]}... existed={existed}")
    return existed


def recipients() -> list[str]:
    if not _use_pg():
        return list(_load_json().keys())
    from db import get_conn
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM herb_notify ORDER BY created_at")
        ids = [r[0] for r in cur.fetchall()]
        cur.close()
        conn.close()
        return ids
    except Exception as e:
        log(f"recipients error: {type(e).__name__}: {e}")
        return []


def count() -> int:
    return len(recipients())


def chunks(ids: list[str], size: int = 150) -> list[list[str]]:
    """LINE 的 multicast 一次最多 500 人；抓 150 保守一點，失敗時重送的範圍也小。"""
    return [ids[i:i + size] for i in range(0, len(ids), size)]
