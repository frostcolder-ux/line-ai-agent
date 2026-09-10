"""
營運雷達 — Operations Radar

把「農場回報系統的真實資料」轉成主動推播，取代人工每週檢查與催繳。

主動任務（由 scheduler 在固定時間呼叫）：
  weekly_boss_digest      週一早上 → 私訊老闆：未交名單 + 採收異常
  friday_group_remind     週五下午 → 群組精準催繳：只點名未交農場
  daily_morning_report    每天早上 → 私訊老闆：待辦 + 本週進度一句話 + 異常
  friday_esg_reminder     週五早上 → 私訊老闆：ESG 文件未完成提醒

所有函式都「自帶降級」：farm-reports 連不上時，改推通用提醒或跳過，
絕不讓排程崩潰。
"""
import sys
from datetime import datetime

import farm_bridge

def log(msg: str):
    print(f"[RADAR] {msg}", file=sys.stderr, flush=True)


def _today_str() -> str:
    return datetime.now().strftime("%m/%d")


# ── 週一：老闆週報進度私訊 ─────────────────────────────────────────────────────

def weekly_boss_digest(notify_boss_fn):
    """週一早上私訊老闆本週週報進度（含未交聯絡資訊與採收異常）。"""
    data = farm_bridge.get_report_status()
    text = "☀️ 週一營運雷達\n\n" + farm_bridge.format_status_digest(data, for_boss=True)
    try:
        notify_boss_fn(text)
        log("weekly_boss_digest 已推播")
    except Exception as e:
        log(f"weekly_boss_digest 失敗：{e}")


# ── 週五：群組精準催繳 ─────────────────────────────────────────────────────────

def friday_group_remind(push_fn, group_id: str):
    """週五下午在群組貼催繳，只點名未交農場（無資料時退回通用提醒）。"""
    data = farm_bridge.get_report_status()
    text = farm_bridge.format_reminder(data)
    try:
        push_fn(group_id, text)
        log(f"friday_group_remind 已推播 → {group_id}")
    except Exception as e:
        log(f"friday_group_remind 失敗：{e}")


# ── 每天：老闆晨報 ─────────────────────────────────────────────────────────────

def daily_morning_report(notify_boss_fn, list_tasks_fn):
    """
    每天早上把「待辦 + 本週週報進度 + 採收異常」彙整成一則私訊。
    監控預警（病蟲害/意外/客訴）是即時推播，這裡只補未交與待辦的每日提醒。
    """
    parts = [f"🌱 小凡晨報　{_today_str()}"]

    # 1. 本週週報進度（一句話）
    status = farm_bridge.get_report_status()
    if status.get("ok"):
        sub = status.get("submitted_count", 0)
        total = status.get("total_farms", 0)
        miss = status.get("missing", [])
        parts.append(f"\n📋 週報：已交 {sub}/{total} 家")
        if miss:
            names = "、".join(m["farm"] for m in miss)
            parts.append(f"　未交：{names}")
        anomalies = status.get("anomalies", [])
        if anomalies:
            parts.append(f"⚠️ 採收異常：{len(anomalies)} 家")
            for a in anomalies:
                parts.append(f"　・{a['farm']}：{a['note']}")
    else:
        parts.append("\n📋 週報進度：暫時取不到（農場系統連線中）")

    # 2. 待辦事項
    try:
        tasks = list_tasks_fn(status="pending")
    except Exception:
        tasks = []
    if tasks:
        parts.append(f"\n✅ 待辦（{len(tasks)} 項）：")
        for t in tasks[:8]:
            parts.append(f"　・{t.get('description', '')}（{t.get('deadline', '')}）")
    else:
        parts.append("\n✅ 目前沒有待辦事項")

    text = "\n".join(parts)
    try:
        notify_boss_fn(text)
        log("daily_morning_report 已推播")
    except Exception as e:
        log(f"daily_morning_report 失敗：{e}")


# ── 週五：ESG 文件提醒（私訊老闆）─────────────────────────────────────────────

def friday_esg_reminder(notify_boss_fn):
    """週五早上提醒老闆哪些農場的合規文件還沒收齊。"""
    data = farm_bridge.get_esg_docs()
    if not data.get("ok"):
        log(f"friday_esg_reminder 取不到資料，略過：{data.get('error')}")
        return

    missing = [f["farm"] for f in data.get("farms", []) if not f.get("doc_count")]
    if not missing:
        log("friday_esg_reminder: 每個農場都有文件，略過推播")
        return

    text = (
        f"📄 小凡合規文件提醒　{_today_str()}\n\n"
        f"以下農場還沒有任何文件：\n"
        + "\n".join(f"　・{name}" for name in missing)
        + f"\n\n目前共收到 {data.get('total_documents', 0)} 份文件 🌿"
    )
    try:
        notify_boss_fn(text)
        log(f"friday_esg_reminder 已推播（{len(missing)} 個農場待補）")
    except Exception as e:
        log(f"friday_esg_reminder 失敗：{e}")


# ── 每週一：會議與決議追蹤 ────────────────────────────────────────────────────

def weekly_meeting_digest(notify_boss_fn):
    """週一提醒老闆本週會議，以及逾期未完成的決議待辦。"""
    data = farm_bridge.get_meetings(days_ahead=7, days_back=0)
    if not data.get("ok"):
        log(f"weekly_meeting_digest 取不到資料，略過：{data.get('error')}")
        return

    meetings = [m for m in data.get("meetings", []) if m.get("status") == "scheduled"]
    pending = data.get("pending_actions", [])
    if not meetings and not pending:
        log("weekly_meeting_digest: 本週無會議也無待辦，略過推播")
        return

    parts = [f"📅 本週會議與決議追蹤　{_today_str()}"]
    if meetings:
        parts.append(f"\n本週有 {len(meetings)} 場會議：")
        for m in meetings:
            when = (m.get("meet_at") or "").replace("T", " ")[:16]
            where = f"　{m['location']}" if m.get("location") else ""
            parts.append(f"　・{when}　{m['title']}{where}")
    else:
        parts.append("\n本週沒有排定會議")

    if pending:
        parts.append(f"\n📌 待追蹤決議（{len(pending)} 項）：")
        for a in pending[:8]:
            due = f"（{a['due_date']} 前）" if a.get("due_date") else ""
            who = f"{a['owner']}：" if a.get("owner") else ""
            parts.append(f"　・{who}{a['description']}{due}")

    try:
        notify_boss_fn("\n".join(parts))
        log(f"weekly_meeting_digest 已推播（{len(meetings)} 會議 / {len(pending)} 待辦）")
    except Exception as e:
        log(f"weekly_meeting_digest 失敗：{e}")


# ── 週五：多群組週報催繳 ──────────────────────────────────────────────────────

def friday_multi_group_remind(push_fn, groups: list):
    """
    週五對多個群組發送週報催繳提醒。
    groups: [{"group_id": "C...", "name": "群組名稱"}, ...]
    """
    data = farm_bridge.get_report_status()
    base_text = farm_bridge.format_reminder(data)

    for group in groups:
        gid = group.get("group_id", "").strip()
        if not gid:
            continue
        try:
            push_fn(gid, base_text)
            log(f"friday_multi_group_remind 已推播 → {group.get('name', gid)}")
        except Exception as e:
            log(f"friday_multi_group_remind 失敗 {gid}：{e}")
