"""誰可以使喚小凡。

★ 為什麼需要這一層
  小凡原本不看發話者是誰，只看訊息裡有沒有「小凡」。所以任何加它好友的人，
  私訊「小凡 這週誰還沒交週報」就查得到內部資料、「小凡 交給總管 …」就能把
  指令塞進老闆本機的代理人；把它拉進自己的群組，它還會自動開週排程推播
  （吃訊息額度），並開始把那個群組的每一則對話送去 AI 分析、存進資料庫。
  官網的香草遊戲會把陌生人帶來加好友，這個洞必須先補。

★ 三種身分
  boss     老闆本人：全部功能，包含「交給總管」
  group    已核准的工作群組：維持原本的功能；群組裡其他人說「交給總管」只會
           存成一般交辦，等老闆確認，不直接交給代理人
  outside  其他所有人（陌生好友、還沒核准的群組）：不進 AI、不查資料、
           不監控、不排程。私訊只回一則固定說明

★ 新群組一律要核准
  小凡被拉進新群組時先安靜，私訊老闆一組四碼代號；老闆回「核准 A7K2」才開始
  服務，回「拒絕 A7K2」就退出那個群組。代號是為了讓老闆在 LINE 上回一句話就好，
  不必去後台貼一長串群組 ID。

存放跟專案其他模組一樣：有 DATABASE_URL 走 Postgres，沒有就用本機 JSON
（Render 免費方案會重啟，檔案留不住，所以線上一定是 Postgres）。
"""
import json
import os
import random
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(__file__)
JSON_PATH = os.path.join(BASE_DIR, "line_access.json")

# 代號不用 0O1I，念出來、手打都不會弄錯
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

BOSS = "boss"
GROUP = "group"
OUTSIDE = "outside"


def log(msg: str):
    print(f"[ACCESS] {msg}", file=sys.stderr, flush=True)


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


def _rows() -> list[dict]:
    """全部群組紀錄（已核准與待核准）。"""
    if not _use_pg():
        return list(_load_json().values())
    from db import get_conn
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT group_id, name, code, status, created_at, decided_at FROM line_access ORDER BY created_at")
        rows = [
            {"group_id": r[0], "name": r[1], "code": r[2], "status": r[3],
             "created_at": r[4], "decided_at": r[5]}
            for r in cur.fetchall()
        ]
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        log(f"_rows error: {type(e).__name__}: {e}")
        return []


def _write(row: dict):
    if not _use_pg():
        data = _load_json()
        data[row["group_id"]] = row
        _save_json(data)
        return
    from db import get_conn
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO line_access (group_id, name, code, status, created_at, decided_at)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (group_id) DO UPDATE SET
             name = EXCLUDED.name, code = EXCLUDED.code,
             status = EXCLUDED.status, decided_at = EXCLUDED.decided_at""",
        (row["group_id"], row["name"], row["code"], row["status"],
         row["created_at"], row.get("decided_at", "")),
    )
    conn.commit()
    cur.close()
    conn.close()


def approved_ids() -> set[str]:
    return {r["group_id"] for r in _rows() if r.get("status") == "approved"}


def is_approved(group_id: str) -> bool:
    """群組有沒有被核准。空字串（私訊）一律不是。"""
    return bool(group_id) and group_id in approved_ids()


def role_of(source, boss_id: str) -> str:
    """這則訊息的來源屬於哪一種身分。source 是 LINE SDK 的 event.source。"""
    group_id = getattr(source, "group_id", None) or getattr(source, "room_id", None) or ""
    user_id = getattr(source, "user_id", "") or ""
    if not group_id and boss_id and user_id == boss_id:
        return BOSS
    if group_id and is_approved(group_id):
        # 群組裡的老闆仍然是老闆（交給總管、核准指令都要能用）
        return BOSS if (boss_id and user_id == boss_id) else GROUP
    return OUTSIDE


def seed_from_config(config: dict) -> int:
    """
    把設定檔裡已經在用的群組列為已核准。

    白名單上線那一刻，表是空的——如果不先補，現有的工作群組會全部變成「外人」，
    小凡在真正該講話的地方整個安靜下來。所以啟動時把 config 裡的
    partner_groups 與 scheduled_tasks 用到的群組視為既有的，直接核准。
    只補沒有紀錄的群組，老闆後來拒絕過的不會被這裡翻回來。
    """
    known = {r["group_id"]: r for r in _rows()}
    added = 0
    pairs: list[tuple[str, str]] = []
    for g in config.get("partner_groups") or []:
        pairs.append(((g.get("group_id") or "").strip(), g.get("name") or ""))
    for t in config.get("scheduled_tasks") or []:
        pairs.append(((t.get("group_id") or "").strip(), t.get("name") or ""))
    for group_id, name in pairs:
        if not group_id or group_id in known:
            continue
        _write({
            "group_id": group_id,
            "name": name or group_id[-8:],
            "code": _new_code({r.get("code", "") for r in known.values()}),
            "status": "approved",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "decided_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        known[group_id] = {"group_id": group_id}
        added += 1
    if added:
        log(f"seeded {added} groups from config")
    return added


def _new_code(existing: set[str]) -> str:
    for _ in range(50):
        code = "".join(random.choice(CODE_ALPHABET) for _ in range(4))
        if code not in existing:
            return code
    return datetime.now().strftime("%H%M")


def remember_pending(group_id: str, name: str = "") -> dict:
    """記下一個還沒核准的群組，回傳那筆紀錄（含代號）。同一個群組重複進來不會換代號。"""
    rows = _rows()
    for r in rows:
        if r["group_id"] == group_id and r.get("status") == "pending":
            return r
    row = {
        "group_id": group_id,
        "name": name or group_id[-8:],
        "code": _new_code({r.get("code", "") for r in rows}),
        "status": "pending",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "decided_at": "",
    }
    _write(row)
    log(f"pending group {group_id[-8:]} code={row['code']}")
    return row


def find_by_code(code: str) -> dict | None:
    code = (code or "").strip().upper()
    return next((r for r in _rows() if (r.get("code") or "").upper() == code), None)


def decide(code: str, status: str) -> dict | None:
    """把某個代號的群組設成 approved 或 rejected。回傳那筆紀錄。"""
    row = find_by_code(code)
    if not row:
        return None
    row["status"] = status
    row["decided_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _write(row)
    log(f"group {row['group_id'][-8:]} → {status}")
    return row


def summary() -> str:
    """給老闆看的清單。"""
    rows = _rows()
    ok = [r for r in rows if r.get("status") == "approved"]
    pending = [r for r in rows if r.get("status") == "pending"]
    lines = [f"已核准的群組（{len(ok)}）"]
    lines += [f"· {r['name']}" for r in ok] or ["（沒有）"]
    lines.append("")
    lines.append(f"等你決定的（{len(pending)}）")
    lines += [f"· {r['name']}　代號 {r['code']}" for r in pending] or ["（沒有）"]
    if pending:
        lines.append("")
        lines.append("回「核准 代號」開始服務，回「拒絕 代號」我就退出那個群組。")
    return "\n".join(lines)


def parse_group_decision(text: str) -> str | None:
    """
    老闆人就在那個還沒核准的群組裡時，直接說「小凡 核准」就算數。

    這裡不需要代號——群組就是訊息的來源，不會搞錯是哪一個。代號是為了讓他在
    私訊裡指名遠處的群組才存在的。
    """
    t = (text or "").strip().strip("！!。．.，,")
    if t in ("核准", "同意", "可以"):
        return "approve"
    if t in ("拒絕", "不要", "退出"):
        return "reject"
    return None


def parse_boss_command(text: str) -> tuple[str, str] | None:
    """
    老闆的管理指令。回 (動作, 代號)；不是指令就回 None。

    後面一定要接代號才算指令——不然老闆隨口說一句「核准這件事」就會被當成指令，
    而他真正想要的是小凡照平常那樣回話。
    """
    import re
    t = (text or "").strip().strip("！!。．.")
    # 中間可以夾「代號」「群組」「：」——人不會每次都照最精簡的格式打。
    # 2026-09-20 實際踩到：老闆傳「拒絕代號 F3VH」完全沒反應，因為只認「拒絕 F3VH」。
    m = re.match(
        r"^(核准|同意|可以|拒絕|不要|退出)\s*(?:這個|那個)?\s*(?:群組)?\s*(?:的)?\s*(?:代號)?\s*[:：,，]?\s*([A-Za-z0-9]{3,6})$",
        t,
    )
    if m:
        return ("approve" if m.group(1) in ("核准", "同意", "可以") else "reject", m.group(2))
    if t in ("群組清單", "群組列表", "哪些群組", "待核准", "群組"):
        return ("list", "")
    # 看得出是在做決定、但代號讀不出來（打錯、漏掉、長度不對）：回清單讓他看到正確的代號。
    # 不要沉默——沉默看起來就跟壞掉一樣。
    # 條件收緊到「有提代號或有英數字」，免得把「核准這件事再跟我說」這種正常對話吃掉。
    if re.match(r"^(核准|同意|拒絕|不要|退出)", t) and ("代號" in t or re.search(r"[A-Za-z0-9]{2,8}", t)):
        return ("list", "")
    return None
