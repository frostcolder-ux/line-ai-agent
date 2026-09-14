"""交辦擷取 — 把群組裡「誰要老闆做什麼」留下來。

小凡本來就在每個工作群組裡，`monitor` 也已經把所有未觸發的訊息收進暫存、
每 5 分鐘送 Claude 判斷一次有沒有預警。**但問完就整批丟掉**——一天幾百則
工作對話從眼前流過，只被用來決定要不要報警。

這個模組做的事：在同一次分析裡順手把「交辦」留下來。不多打一次 API，
不用任何人轉傳，訊息本來就會經過這裡。

存下來之後由 brand-db（老闆本機的品牌資料庫）定期來拉，走
`/internal/work-requests`。**推不過去是因為 brand-db 在本機**，
所以方向一定是「雲端存著、本機來拉」，跟 farm_reports 的即時數據同一個模式。

去重責任在兩邊都有：這裡用 fingerprint 擋重複寫入，brand-db 那邊也會再擋一次。
因此端點可以放心地把整個時間窗都吐出去——brand-db 離線一週再回來，
資料不會少，也不會變成兩筆。
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(__file__)
JSON_PATH = os.path.join(BASE_DIR, "captured_requests.json")

# 同一個人的顯示名稱不會一直變，查一次就好（LINE 每次查都是一次 API 呼叫）
_name_cache: dict[str, str] = {}


def log(msg: str):
    print(f"[CAPTURE] {msg}", file=sys.stderr, flush=True)


def _use_pg() -> bool:
    from db import is_postgres
    return is_postgres()


def fingerprint(asker: str, said_at: str, raw: str) -> str:
    """跟 brand-db 用同一套規則算，兩邊擋掉的才會是同一批。"""
    key = f"{asker}|{said_at}|{(raw or '')[:200]}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def resolve_name(group_id: str, user_id: str, profile_fn=None) -> str:
    """user_id 長這樣：Ub20f553c1d4317f760f5b406b177c002。

    那串東西寫進「誰交辦的」欄位等於沒寫——統計「誰一直在丟工作」時
    看到一排亂碼是沒有意義的，所以這裡換成顯示名稱。查不到就退回短碼。
    """
    if not user_id:
        return ""
    key = f"{group_id}:{user_id}"
    if key in _name_cache:
        return _name_cache[key]
    name = ""
    if profile_fn:
        try:
            name = (profile_fn(group_id, user_id) or "").strip()
        except Exception as e:
            log(f"resolve_name 失敗 {user_id[:8]}: {type(e).__name__}: {e}")
    name = name or f"用戶{user_id[:6]}"
    _name_cache[key] = name
    return name


# ── 儲存 ──────────────────────────────────────────────────────────────────

FIELDS = ("group_id", "group_name", "asker", "said_at", "raw", "title",
          "detail", "kind", "project", "due", "urgency", "blocking",
          "confidence")


def _load_json() -> list:
    try:
        with open(JSON_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_json(rows: list):
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def save_many(rows: list) -> int:
    """寫入一批交辦，回傳實際新增的筆數（重複的不算）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    clean = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        row = {k: str(r.get(k) or "").strip() for k in FIELDS}
        if not row["title"]:
            continue
        row["fingerprint"] = fingerprint(row["asker"], row["said_at"],
                                         row["raw"])
        row["created_at"] = now
        clean.append(row)
    if not clean:
        return 0

    if _use_pg():
        from db import get_conn
        conn = get_conn()
        cur = conn.cursor()
        added = 0
        cols = ", ".join(FIELDS)
        marks = ", ".join(["%s"] * len(FIELDS))
        for row in clean:
            cur.execute(
                f"INSERT INTO captured_requests ({cols}, fingerprint, created_at)"
                f" VALUES ({marks}, %s, %s)"
                " ON CONFLICT (fingerprint) DO NOTHING RETURNING id",
                (*[row[k] for k in FIELDS], row["fingerprint"],
                 row["created_at"]))
            if cur.fetchone():
                added += 1
        conn.commit()
        cur.close()
        conn.close()
        log(f"PG 寫入 {added}/{len(clean)} 筆交辦")
        return added

    existing = _load_json()
    seen = {r.get("fingerprint") for r in existing}
    added_rows = [r for r in clean if r["fingerprint"] not in seen]
    for i, r in enumerate(added_rows, len(existing) + 1):
        r["id"] = i
    _save_json(existing + added_rows)
    log(f"JSON 寫入 {len(added_rows)}/{len(clean)} 筆交辦")
    return len(added_rows)


def list_recent(days: int = 14, limit: int = 500) -> list:
    """近 N 天抓到的交辦。給 brand-db 拉資料用。

    刻意不做「只給沒同步過的」——brand-db 自己會用指紋去重，
    整個時間窗都吐出去比較不怕它離線幾天。
    """
    since = (datetime.now() - timedelta(days=max(1, days))
             ).strftime("%Y-%m-%d %H:%M:%S")
    if _use_pg():
        from db import get_conn
        conn = get_conn()
        cur = conn.cursor()
        cols = ", ".join(FIELDS)
        cur.execute(
            f"SELECT id, {cols}, fingerprint, created_at FROM captured_requests"
            " WHERE created_at >= %s ORDER BY id DESC LIMIT %s",
            (since, limit))
        names = ["id", *FIELDS, "fingerprint", "created_at"]
        rows = [dict(zip(names, r)) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    return [r for r in reversed(_load_json())
            if r.get("created_at", "") >= since][:limit]


# ── 收件佇列：訊息一進來就寫資料庫，不留在記憶體 ──────────────────────────
#
# 原本的做法是 monitor 把訊息囤在記憶體，每 5 分鐘分析一次。
# 那在長駐的機器上沒問題，但這支跑在 Render 免費方案——會休眠、會重啟，
# 2026-09-14 實測行程在 5 分鐘內就被換掉一次，囤著的訊息全部蒸發。
# 所以改成：收到就進 pending_messages，分析時再從資料庫撈。
# 行程死幾次都沒關係，訊息還在。

def queue_message(group_id: str, group_name: str, user_id: str, who: str,
                  text: str, said_at: str) -> bool:
    """把一則群組訊息排進待分析佇列。webhook 路徑上呼叫，要快也要不會炸。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = (group_id, group_name, user_id, who, (text or "")[:2000],
           said_at or now, now)
    try:
        if _use_pg():
            from db import get_conn
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO pending_messages"
                "(group_id,group_name,user_id,who,text,said_at,created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s)", row)
            conn.commit()
            cur.close()
            conn.close()
            return True
        rows = _load_pending()
        rows.append(dict(zip(
            ("group_id", "group_name", "user_id", "who", "text", "said_at",
             "created_at"), row), id=len(rows) + 1, processed=0))
        _save_pending(rows)
        return True
    except Exception as e:                 # 佇列失敗絕不能讓 webhook 掛掉
        log(f"queue_message 失敗：{type(e).__name__}: {e}")
        return False


PENDING_JSON = os.path.join(BASE_DIR, "pending_messages.json")


def _load_pending() -> list:
    try:
        with open(PENDING_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_pending(rows: list):
    with open(PENDING_JSON, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def take_pending(limit: int = 200) -> dict:
    """取出還沒分析的訊息，依群組分組。回傳 {group_id: (group_name, [rows])}。"""
    if _use_pg():
        from db import get_conn
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, group_id, group_name, user_id, who, text, said_at"
            " FROM pending_messages WHERE processed = 0"
            " ORDER BY id ASC LIMIT %s", (limit,))
        names = ("id", "group_id", "group_name", "user_id", "who", "text",
                 "said_at")
        rows = [dict(zip(names, r)) for r in cur.fetchall()]
        cur.close()
        conn.close()
    else:
        rows = [r for r in _load_pending() if not r.get("processed")][:limit]

    out: dict = {}
    for r in rows:
        gid = r.get("group_id", "")
        out.setdefault(gid, (r.get("group_name") or gid, []))[1].append(r)
    return out


def mark_processed(ids: list) -> None:
    if not ids:
        return
    if _use_pg():
        from db import get_conn
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE pending_messages SET processed = 1"
                    " WHERE id = ANY(%s)", (list(ids),))
        conn.commit()
        cur.close()
        conn.close()
        return
    rows = _load_pending()
    idset = set(ids)
    for r in rows:
        if r.get("id") in idset:
            r["processed"] = 1
    _save_pending(rows)


def pending_count() -> int:
    if _use_pg():
        from db import get_conn
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM pending_messages WHERE processed = 0")
        n = cur.fetchone()[0]
        cur.close()
        conn.close()
        return int(n)
    return len([r for r in _load_pending() if not r.get("processed")])


def process_pending(analyze_fn, group_name_fn=None, limit: int = 200) -> dict:
    """把佇列裡的訊息分析成交辦。

    analyze_fn(group_id, messages) -> dict（monitor._analyze 的簽章）。
    分析失敗的那一組**不標記已處理**，下次會再試——寧可重跑也不要靜靜丟掉。
    """
    batches = take_pending(limit)
    if not batches:
        return {"groups": 0, "messages": 0, "captured": 0}

    captured = n_msgs = 0
    for gid, (gname, rows) in batches.items():
        msgs = [{"user_id": r.get("user_id", ""), "who": r.get("who", ""),
                 "text": r.get("text", ""), "said_at": r.get("said_at", "")}
                for r in rows]
        n_msgs += len(msgs)
        try:
            result = analyze_fn(gid, msgs)
        except Exception as e:
            log(f"分析 {gid} 失敗，保留佇列下次再試：{type(e).__name__}: {e}")
            continue
        if result is None:
            # 分析沒成功（解析不出 JSON、API 掛了…）就不要標記已處理，
            # 否則訊息會被靜靜吃掉——這正是 2026-09-14 踩到的坑。
            log(f"{gid} 這批沒有分析成功，保留在佇列下次再試")
            continue
        reqs = result.get("requests") or []
        for r in reqs:
            if isinstance(r, dict):
                r["group_id"] = gid
                r["group_name"] = (group_name_fn(gid) if group_name_fn
                                   else gname)
        captured += save_many(reqs)
        mark_processed([r["id"] for r in rows])
    log(f"處理 {n_msgs} 則訊息，抓到 {captured} 筆交辦")
    return {"groups": len(batches), "messages": n_msgs, "captured": captured}


def stats() -> dict:
    rows = list_recent(days=3650, limit=100000)
    by_asker: dict[str, int] = {}
    for r in rows:
        by_asker[r.get("asker", "")] = by_asker.get(r.get("asker", ""), 0) + 1
    return {
        "total": len(rows),
        "last_14_days": len(list_recent(14, 100000)),
        "by_asker": sorted(by_asker.items(), key=lambda kv: -kv[1])[:10],
        "storage": "postgres" if _use_pg() else "json",
    }
