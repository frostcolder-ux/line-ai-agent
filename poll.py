"""
開會投票助手 — Group Poll

取代人工「喬開會時間、統計投票」。純 bot 端記憶體狀態，每個群組同時
一個進行中的投票。

指令（在群組對小凡說）：
  小凡 開投票 7/31開會時間 10點/14點/16點     建立投票
  小凡 投票狀況                                查看目前票數
  小凡 結算                                    結束並公告結果

投票方式（群組成員直接回覆，不用叫小凡）：
  回「1」「2」…（選項編號）或直接打選項文字（例：14點）即計一票，
  同一人重複投以最後一次為準。

註：投票狀態存記憶體，服務重啟會清空（開會投票屬短期任務，可接受）。
"""
import sys
import time
import threading

_lock = threading.Lock()
_polls: dict[str, dict] = {}   # group_id -> poll

MAX_VOTE_TEXT_LEN = 12         # 過長的訊息不當作投票（避免誤判）


def log(msg: str):
    print(f"[POLL] {msg}", file=sys.stderr, flush=True)


# ── 建立 / 結束 ───────────────────────────────────────────────────────────────

def parse_open_command(body: str):
    """
    解析「開投票 主題 選項1/選項2/...」。
    回傳 (topic, options) 或 (None, 錯誤訊息)。
    分隔：優先用全形「｜」；否則以最後一個空白切開主題與選項。
    """
    body = body.strip()
    if "｜" in body:
        topic, _, optpart = body.partition("｜")
    elif "|" in body:
        topic, _, optpart = body.partition("|")
    else:
        # 以最後一段空白切開；選項段須含 "/"
        parts = body.rsplit(None, 1)
        if len(parts) == 2 and "/" in parts[1]:
            topic, optpart = parts[0], parts[1]
        else:
            topic, optpart = "", body
    options = [o.strip() for o in optpart.split("/") if o.strip()]
    topic = topic.strip() or "投票"
    if len(options) < 2:
        return None, ("投票格式：小凡 開投票 [主題] [選項1/選項2/選項3]\n"
                      "例：小凡 開投票 7/31開會時間 10點/14點/16點")
    return topic, options


def open_poll(group_id: str, topic: str, options: list) -> str:
    with _lock:
        _polls[group_id] = {
            "topic": topic,
            "options": options,
            "votes": {},          # user_id -> option_idx
            "created": time.time(),
        }
    log(f"open poll @ {group_id}: {topic} {options}")
    lines = [f"🗳️ 投票開始：{topic}", ""]
    for i, o in enumerate(options, 1):
        lines.append(f"{i}. {o}")
    lines.append("")
    lines.append("👉 直接回覆編號（1/2/3）或選項文字即可投票")
    lines.append("　（小凡 投票狀況 看即時票數，小凡 結算 公告結果）")
    return "\n".join(lines)


def has_open_poll(group_id: str) -> bool:
    with _lock:
        return group_id in _polls


def record_vote(group_id: str, user_id: str, text: str) -> bool:
    """
    嘗試把一則群組訊息當作投票計入。回傳是否計票成功。
    在 webhook 預解析階段對「未觸發關鍵字」的群組訊息呼叫。
    """
    t = (text or "").strip()
    if not t or len(t) > MAX_VOTE_TEXT_LEN:
        return False
    with _lock:
        poll = _polls.get(group_id)
        if not poll:
            return False
        options = poll["options"]
        idx = None
        # 1) 純數字編號
        if t.isdigit():
            n = int(t)
            if 1 <= n <= len(options):
                idx = n - 1
        # 2) 選項文字（完全相等，或訊息很短且包含選項）
        if idx is None:
            for i, o in enumerate(options):
                if t == o or (o in t and len(t) <= len(o) + 3):
                    idx = i
                    break
        if idx is None:
            return False
        poll["votes"][user_id] = idx
        log(f"vote @ {group_id}: {user_id[:6]} -> {options[idx]}")
        return True


def _tally(poll: dict) -> list:
    counts = [0] * len(poll["options"])
    for idx in poll["votes"].values():
        counts[idx] += 1
    return counts


def status_text(group_id: str) -> str:
    with _lock:
        poll = _polls.get(group_id)
        if not poll:
            return "目前沒有進行中的投票。開新投票：小凡 開投票 [主題] [選項1/選項2]"
        counts = _tally(poll)
        total = sum(counts)
    lines = [f"🗳️ 目前票數：{poll['topic']}（共 {total} 票）", ""]
    for i, (o, c) in enumerate(zip(poll["options"], counts), 1):
        bar = "●" * c
        lines.append(f"{i}. {o}　{c} 票 {bar}")
    return "\n".join(lines)


def close_poll(group_id: str) -> str:
    with _lock:
        poll = _polls.pop(group_id, None)
    if not poll:
        return "目前沒有進行中的投票。"
    counts = _tally(poll)
    total = sum(counts)
    if total == 0:
        return f"🗳️ 投票結束：{poll['topic']}\n沒有人投票 😅"
    top = max(counts)
    winners = [poll["options"][i] for i, c in enumerate(counts) if c == top]
    lines = [f"🗳️ 投票結果：{poll['topic']}（共 {total} 票）", ""]
    order = sorted(range(len(counts)), key=lambda i: -counts[i])
    for rank, i in enumerate(order, 1):
        mark = "🏆" if counts[i] == top else "　"
        lines.append(f"{mark} {poll['options'][i]}　{counts[i]} 票")
    lines.append("")
    if len(winners) == 1:
        lines.append(f"✅ 最多票：{winners[0]}")
    else:
        lines.append(f"⚖️ 平手：{ '、'.join(winners) }（請老闆裁定）")
    return "\n".join(lines)
