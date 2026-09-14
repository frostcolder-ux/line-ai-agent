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
