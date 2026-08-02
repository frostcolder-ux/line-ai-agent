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

FARM_API_URL = os.environ.get("FARM_API_URL", "").strip().rstrip("/")
FARM_API_TOKEN = os.environ.get("FARM_API_TOKEN", "").strip()
TIMEOUT = 12


def log(msg: str):
    print(f"[FARM_BRIDGE] {msg}", file=sys.stderr, flush=True)


def _get(path: str, params: dict) -> dict:
    if not FARM_API_URL:
        return {"ok": False, "error": "FARM_API_URL 未設定"}
    p = dict(params or {})
    if FARM_API_TOKEN:
        p["token"] = FARM_API_TOKEN
    try:
        resp = requests.get(f"{FARM_API_URL}{path}", params=p, timeout=TIMEOUT)
        if resp.status_code == 401:
            return {"ok": False, "error": "API 認證失敗（token 不符）"}
        resp.raise_for_status()
        data = resp.json()
        data["ok"] = True
        return data
    except Exception as e:
        log(f"GET {path} 失敗：{type(e).__name__}: {e}")
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
