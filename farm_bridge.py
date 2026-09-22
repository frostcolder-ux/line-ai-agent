"""
農場回報系統橋接 — Farm Reports Bridge

小凡透過本模組呼叫 farm-reports 系統的內部 API，取得「真實」的
週報繳交狀態與採收資料（不再是本機假資料）。

環境變數：
  FARM_API_URL    farm-reports 服務網址，例：https://farm-reports.onrender.com
  FARM_API_TOKEN  內部 API 密鑰，須等於 farm-reports 的 INTERNAL_API_TOKEN

未設定 FARM_API_URL 時，所有函式回傳 {"ok": False, "error": "..."}，
呼叫端自行降級顯示提示，不會讓 bot 崩潰。
"""
import os
import sys
import requests

# 農場回報系統網址：預設指向正式站，讓小凡免設定即可連線。
# （此為公開網址、非機密；要改指到別的環境時用環境變數覆蓋。）
DEFAULT_FARM_API_URL = "https://data.selvansimpact.com"
FARM_API_URL = (os.environ.get("FARM_API_URL", "").strip()
                or DEFAULT_FARM_API_URL).rstrip("/")
FARM_API_TOKEN = os.environ.get("FARM_API_TOKEN", "").strip()
# ★ 至少 30 秒：farm-reports 在 Render 免費方案，醒著時單次回應也要 12.5 秒，
#   設 12 秒會讓資料「靜靜地」抓不到（見 farm_reports 的冷啟動紀錄）。
TIMEOUT = 30


def log(msg: str):
    print(f"[FARM_BRIDGE] {msg}", file=sys.stderr, flush=True)


_AUTH_HINT = "API 認證失敗：請在 Render 設定 FARM_API_TOKEN，值需等於農場系統的 INTERNAL_API_TOKEN"


def _get(path: str, params: dict) -> dict:
    if not FARM_API_URL:
        return {"ok": False, "error": "FARM_API_URL 未設定"}
    p = dict(params or {})
    if FARM_API_TOKEN:
        p["token"] = FARM_API_TOKEN
    try:
        resp = requests.get(f"{FARM_API_URL}{path}", params=p, timeout=TIMEOUT)
        if resp.status_code == 401:
            return {"ok": False, "error": _AUTH_HINT}
        resp.raise_for_status()
        data = resp.json()
        data["ok"] = True
        return data
    except Exception as e:
        log(f"GET {path} 失敗：{type(e).__name__}: {e}")
        return {"ok": False, "error": f"連線失敗：{type(e).__name__}"}


def _post(path: str, payload: dict) -> dict:
    """寫入端點。農場系統對寫入一律要求 token，沒設就會 401。"""
    if not FARM_API_URL:
        return {"ok": False, "error": "FARM_API_URL 未設定"}
    params = {"token": FARM_API_TOKEN} if FARM_API_TOKEN else {}
    try:
        resp = requests.post(f"{FARM_API_URL}{path}", params=params,
                             json=payload or {}, timeout=TIMEOUT)
        if resp.status_code == 401:
            return {"ok": False, "error": _AUTH_HINT}
        resp.raise_for_status()
        data = resp.json()
        data.setdefault("ok", True)
        return data
    except Exception as e:
        log(f"POST {path} 失敗：{type(e).__name__}: {e}")
        return {"ok": False, "error": f"連線失敗：{type(e).__name__}"}


# ── 原始資料 ─────────────────────────────────────────────────────────────────

def get_report_status(week_start: str = "") -> dict:
    """本週各農場週報繳交狀態。week_start 空白＝本週。"""
    params = {}
    if week_start:
        params["week_start"] = week_start
    return _get("/internal/report-status", params)


def query_real_harvest(farm: str = "", weeks: int = 4) -> dict:
    """近 N 週採收量。farm 為農場名稱片段，空白＝全部。"""
    return _get("/internal/harvest", {"farm": farm, "weeks": weeks})


# ── 給 LINE 用的文字排版 ──────────────────────────────────────────────────────

def format_status_digest(data: dict, *, for_boss: bool = True) -> str:
    """把 report-status 轉成一則 LINE 訊息。for_boss=True 顯示未交聯絡資訊。"""
    if not data.get("ok"):
        return f"⚠️ 無法取得週報狀態：{data.get('error', '未知錯誤')}"

    ws = data.get("week_start", "")
    total = data.get("total_farms", 0)
    sub_n = data.get("submitted_count", 0)
    miss = data.get("missing", [])
    anomalies = data.get("anomalies", [])

    lines = [f"📋 週報進度（本週起 {ws}）",
             f"已交 {sub_n}/{total} 家"]

    if miss:
        lines.append("")
        lines.append(f"🔴 未交（{len(miss)} 家）：")
        for m in miss:
            if for_boss and (m.get("contact_name") or m.get("contact_phone")):
                who = m.get("contact_name", "")
                tel = m.get("contact_phone", "")
                tail = f"（{who} {tel}）".replace(" )", ")").replace("（ ", "（")
                lines.append(f"・{m['farm']}{tail}")
            else:
                lines.append(f"・{m['farm']}")
    else:
        lines.append("🎉 全部農場都交了！")

    if anomalies:
        lines.append("")
        lines.append(f"⚠️ 採收異常（{len(anomalies)} 家）：")
        for a in anomalies:
            lines.append(f"・{a['farm']}：{a['note']}")

    return "\n".join(lines)


def format_reminder(data: dict) -> str:
    """群組催繳訊息：只點名未交農場，不含聯絡個資。"""
    if not data.get("ok"):
        # 取不到資料時退回通用提醒，仍可正常催繳
        return "📋 各位夥伴好，麻煩今天下班前回報本週契作進度，謝謝配合！🌿"

    miss = data.get("missing", [])
    if not miss:
        return "✅ 本週週報全部都收到了，感謝各位夥伴準時回報！🌿"

    names = "、".join(m["farm"] for m in miss)
    return (f"📋 本週週報催繳提醒\n"
            f"還沒收到以下農場的回報，麻煩今天下班前補上，謝謝！🌿\n"
            f"👉 {names}")


def format_harvest(data: dict) -> str:
    """採收查詢結果排版。"""
    if not data.get("ok"):
        return f"⚠️ 無法查詢採收資料：{data.get('error', '未知錯誤')}"

    farms = data.get("farms", [])
    weeks = data.get("weeks", 0)
    if not farms:
        return f"查無「{data.get('query_farm', '')}」的採收資料。"

    lines = [f"🌾 近 {weeks} 週採收量（{data.get('query_farm', '')}）"]
    for f in farms:
        lines.append("")
        lines.append(f"【{f['farm']}】合計 {f['total_kg']} kg")
        for w in f.get("weekly", []):
            if w["harvest_kg"] > 0:
                lines.append(f"  {w['week_start']}　{w['harvest_kg']} kg")
    return "\n".join(lines)


# ── 影響力數據 / 合規文件 / 出貨 / 會議 ─────────────────────────────────────────

def get_impact(weeks: int = 4, farm: str = "") -> dict:
    """近 N 週 ESG 影響力彙總。"""
    return _get("/internal/impact", {"weeks": weeks, "farm": farm})


def get_esg_docs() -> dict:
    """各農場合規文件收集狀態。"""
    return _get("/internal/esg-docs", {})


def get_shipments(status: str = "", days: int = 30) -> dict:
    """近 N 天出貨單與物流狀態。"""
    return _get("/internal/shipments", {"status": status, "days": days})


def get_meetings(status: str = "", days_ahead: int = 30, days_back: int = 14) -> dict:
    """會議列表與未完成決議待辦。"""
    return _get("/internal/meetings", {"status": status,
                                       "days_ahead": days_ahead,
                                       "days_back": days_back})


def create_meeting(title: str, meet_at: str, location: str = "",
                   scope: str = "", agenda: str = "") -> dict:
    """建立會議。meet_at 需為 ISO 格式，例 2026-09-20T14:00。"""
    return _post("/internal/meetings", {
        "title": title, "meet_at": meet_at, "location": location,
        "scope": scope, "agenda": agenda,
    })


def record_minutes(meeting_id: int, minutes: str, actions: list | None = None) -> dict:
    """記錄會議紀錄與決議待辦。"""
    return _post(f"/internal/meetings/{meeting_id}/minutes", {
        "minutes": minutes, "actions": actions or [],
    })


def complete_action(action_id: int) -> dict:
    """把某項會議決議待辦標記為完成。"""
    return _post(f"/internal/meeting-actions/{action_id}/done", {})


def get_review_digest(days: int = 7) -> dict:
    """週報自動審查摘要：自動通過、轉人工與原因、關懷提醒、常見建議。"""
    return _get("/internal/review-digest", {"days": days})


def format_review_digest(data: dict) -> str:
    """老闆看的週報審查週報。關懷提醒只有農場與人數，不含姓名。"""
    if not data.get("ok"):
        return f"⚠️ 無法取得週報審查摘要：{data.get('error', '未知錯誤')}"
    days = data.get("days", 7)
    passed, held, waiting = data.get("passed", []), data.get("held", []), data.get("waiting", [])
    lines = [f"🤖 週報自動審查（近 {days} 天）"]
    if not data.get("enabled", True):
        lines.append("（自動審查目前關閉，全部為人工審核）")
    lines.append(f"✅ 自動通過 {len(passed)} 份　👀 轉人工 {len(held)} 份")
    if waiting:
        lines.append(f"\n⏳ 還在等你審核（{len(waiting)} 份）：")
        for h in waiting[:8]:
            reasons = "；".join(h.get("reasons", []))
            lines.append(f"・{h['farm']} {h['week'][5:]} 週：{reasons[:80]}")
        if len(waiting) > 8:
            lines.append(f"　…還有 {len(waiting) - 8} 份")
    elif held:
        lines.append("轉人工的都已處理完 👍")
    warn = [(p["farm"], w) for p in passed for w in p.get("warnings", [])]
    if warn:
        lines.append("\n📝 通過但有提醒：")
        for farm, w in warn[:5]:
            lines.append(f"・{farm}：{w[:60]}")
    care = data.get("care_alerts", [])
    if care:
        by_farm = {}
        for c in care:
            by_farm[c["farm"]] = by_farm.get(c["farm"], 0) + c.get("n", 1)
        lines.append("\n💛 關懷提醒（心情很低或壓力很高）：")
        lines.append("　" + "、".join(f"{f} {n} 位" for f, n in by_farm.items()) + "，細節請到後台週報查看")
    tops = data.get("top_suggestions", [])
    if tops:
        lines.append("\n💡 最常給農場的建議：")
        for text, n in tops[:3]:
            lines.append(f"・{text}…（{n} 份）")
    total = data.get("pending_total")
    if total:
        lines.append(f"\n目前全部待審：{total} 份 → 後台「週報審核管理」")
    return "\n".join(lines)


# ── 排版 ──────────────────────────────────────────────────────────────────────

def format_impact(data: dict) -> str:
    if not data.get("ok"):
        return f"⚠️ 無法取得影響力數據：{data.get('error', '未知錯誤')}"

    t = data.get("totals", {})
    weeks = data.get("weeks", 0)
    lines = [f"📊 影響力數據（近 {weeks} 週・{data.get('query_farm', '')}）",
             f"期間：{data.get('period_start', '')} ~ {data.get('period_end', '')}",
             f"週報筆數：{data.get('report_count', 0)}"]

    if not data.get("report_count"):
        lines.append("\n（這段期間還沒有週報資料）")
        return "\n".join(lines)

    lines.append("\n【社會面】")
    lines.append(f"・弱勢就業工時：{t.get('employment_hours', 0)} 小時")
    lines.append(f"・總工作時數：{t.get('worked_hours', 0)} 小時")
    lines.append(f"・最高同時工作人數：{t.get('peak_workers', 0)} 人")
    lines.append(f"・轉銜一般職場：{t.get('open_employment', 0)} 人")
    lines.append(f"・獨立完成任務：{t.get('independent_tasks', 0)} 次")
    if t.get("mood_avg") is not None:
        lines.append(f"・平均心情星級：{t['mood_avg']} / 5")
    if t.get("visitor_count"):
        lines.append(f"・企業來訪：{t.get('corporate_visits', 0)} 場、"
                     f"{t['visitor_count']} 人次、"
                     f"{t.get('visit_activity_hours', 0)} 小時")
    if t.get("safety_incidents"):
        lines.append(f"⚠️ 安全事件：{t['safety_incidents']} 件")

    lines.append("\n【環境面】")
    lines.append(f"・採收量：{t.get('harvest_kg', 0)} 公斤")
    lines.append(f"・堆肥化：{t.get('compost_kg', 0)} 公斤")
    lines.append(f"・新觀察物種：{t.get('new_species_count', 0)} 種")
    lines.append(f"・無農藥週數：{t.get('pesticide_free_weeks', 0)} 週")
    return "\n".join(lines)


def format_esg_docs(data: dict) -> str:
    if not data.get("ok"):
        return f"⚠️ 無法取得文件狀態：{data.get('error', '未知錯誤')}"

    farms = data.get("farms", [])
    lines = [f"📄 合規文件收集狀態（共 {data.get('total_documents', 0)} 份）"]
    missing = [f for f in farms if f.get("doc_count", 0) == 0]

    for f in farms:
        n = f.get("doc_count", 0)
        mark = "✅" if n else "❌"
        types = "、".join(f.get("doc_types", []))
        lines.append(f"{mark} {f['farm']}：{n} 份" + (f"（{types}）" if types else ""))

    if missing:
        lines.append(f"\n⚠️ 完全沒有文件：{'、'.join(m['farm'] for m in missing)}")
    if data.get("shared_documents"):
        lines.append(f"\n📎 全體適用文件：{len(data['shared_documents'])} 份")
    return "\n".join(lines)


def format_shipments(data: dict) -> str:
    if not data.get("ok"):
        return f"⚠️ 無法取得出貨資料：{data.get('error', '未知錯誤')}"

    rows = data.get("shipments", [])
    if not rows:
        return f"近 {data.get('days', 0)} 天沒有出貨紀錄。"

    lines = [f"🚚 出貨狀態（近 {data.get('days', 0)} 天，共 {data.get('count', 0)} 單）",
             f"待出貨 {data.get('pending_count', 0)} 單"]
    if data.get("missing_tracking_count"):
        lines.append(f"⚠️ 已出貨但缺貨運單號：{data['missing_tracking_count']} 單")

    for s in rows[:12]:
        items = "、".join(f"{i['name']}×{i['quantity']:g}" for i in s.get("items", [])[:3])
        tail = f" 單號 {s['tracking_no']}" if s.get("tracking_no") else ""
        lines.append(f"\n【{s.get('shipment_no') or s['id']}】{s.get('status_label', '')}"
                     f"　{s.get('ship_date', '')}")
        if items:
            lines.append(f"  {items}")
        if s.get("logistics_provider") or tail:
            lines.append(f"  {s.get('logistics_provider', '')}{tail}")
    if len(rows) > 12:
        lines.append(f"\n…另有 {len(rows) - 12} 單")
    return "\n".join(lines)


def format_meetings(data: dict) -> str:
    if not data.get("ok"):
        return f"⚠️ 無法取得會議資料：{data.get('error', '未知錯誤')}"

    meetings = data.get("meetings", [])
    pending = data.get("pending_actions", [])
    if not meetings and not pending:
        return "目前沒有排定的會議，也沒有待追蹤的決議事項。"

    lines = [f"📅 會議（共 {data.get('count', 0)} 場，"
             f"未來 {data.get('upcoming_count', 0)} 場）"]
    for m in meetings[:10]:
        when = (m.get("meet_at") or "").replace("T", " ")[:16]
        lines.append(f"\n【{m['title']}】{when}")
        if m.get("location"):
            lines.append(f"  地點：{m['location']}")
        if m.get("scope"):
            lines.append(f"  與會：{m['scope']}")
        lines.append(f"  狀態：{m.get('status', '')}")

    if pending:
        lines.append(f"\n📌 待追蹤決議（{len(pending)} 項）：")
        for a in pending[:10]:
            due = f"（{a['due_date']} 前）" if a.get("due_date") else ""
            who = f"{a['owner']}：" if a.get("owner") else ""
            lines.append(f"・[{a['action_id']}] {who}{a['description']}{due}")
    return "\n".join(lines)
